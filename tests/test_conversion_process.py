"""Subprocess runner timeout, cancellation, diagnostics, and failure tests."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from sopds.conversion import process
from sopds.conversion.contracts import ConversionTimeoutError, ConverterExecutionError
from sopds.conversion.process import run_process


async def test_runner_drains_output_but_retains_only_bounded_tail() -> None:
    result = await run_process(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'a' * 100000 + b'end')",
        )
    )

    assert len(result.stdout) == 64 * 1024
    assert result.stdout.endswith(b"end")
    assert result.stderr == b""


async def test_runner_maps_missing_executable_and_nonzero_exit_without_argv() -> None:
    with pytest.raises(ConverterExecutionError, match=r"^Converter execution failed$") as missing:
        await run_process(("/definitely/missing/sopds-converter",))
    assert "/definitely" not in str(missing.value)

    with pytest.raises(ConverterExecutionError, match=r"^Converter execution failed$") as failed:
        await run_process((sys.executable, "-c", "import sys; print('private-path'); sys.exit(7)"))
    assert "private-path" not in str(failed.value)


async def test_runner_maps_ordinary_spawn_exception_without_private_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_spawn(*_args: str, **_kwargs: object) -> None:
        raise RuntimeError("private /source/path")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(ConverterExecutionError, match=r"^Converter execution failed$") as raised:
        await run_process(("converter", "private-argument"))

    assert "/source/path" not in str(raised.value)


async def test_runner_cancellation_survives_late_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_spawn(*_args: str, **_kwargs: object) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("late private failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    task = asyncio.create_task(run_process(("converter",)))
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_runner_cancellation_survives_late_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_entered = asyncio.Event()
    spawn_release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def complete_spawn(*_args: str, **_kwargs: object) -> object:
        spawn_entered.set()
        await spawn_release.wait()
        return object()

    async def fail_cleanup(_process: object) -> None:
        cleanup_entered.set()
        await cleanup_release.wait()
        raise RuntimeError("late cleanup failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", complete_spawn)
    monkeypatch.setattr(process, "_cleanup_created_process", fail_cleanup)
    task = asyncio.create_task(run_process(("converter",)))
    await spawn_entered.wait()
    task.cancel()
    spawn_release.set()
    await cleanup_entered.wait()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_runner_times_out_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "PROCESS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(process, "_TERMINATE_GRACE_SECONDS", 0.05)

    with pytest.raises(ConversionTimeoutError, match=r"^Converter timed out$"):
        await run_process((sys.executable, "-c", "import time; time.sleep(30)"))


async def test_timeout_includes_draining_pipes_inherited_by_an_exited_parents_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "PROCESS_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(process, "_TERMINATE_GRACE_SECONDS", 0.5)
    ready = tmp_path / "child-ready"
    terminated = tmp_path / "child-terminated"
    code = """
import subprocess
import sys
import time
from pathlib import Path

child_code = '''
import signal
import sys
import time
from pathlib import Path

ready = Path(sys.argv[1])
terminated = Path(sys.argv[2])
def terminate(*_args):
    terminated.write_text('terminated')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
ready.write_text('ready')
time.sleep(30)
'''
subprocess.Popen([
    sys.executable,
    '-c',
    child_code,
    sys.argv[1],
    sys.argv[2],
])
ready = Path(sys.argv[1])
while not ready.exists():
    time.sleep(0.001)
"""

    with pytest.raises(ConversionTimeoutError, match=r"^Converter timed out$"):
        await asyncio.wait_for(
            run_process((sys.executable, "-c", code, os.fspath(ready), os.fspath(terminated))),
            2,
        )

    assert ready.exists()
    assert terminated.exists()


async def test_runner_escalates_for_a_process_group_child_ignoring_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "_TERMINATE_GRACE_SECONDS", 0.05)
    ready = tmp_path / "child-ready"
    survived = tmp_path / "child-survived"
    code = """
import subprocess
import sys
import time
subprocess.Popen([
    sys.executable,
    '-c',
    \"import signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).write_text('ready'); time.sleep(0.4); Path(sys.argv[2]).write_text('survived')\",
    sys.argv[1],
    sys.argv[2],
])
time.sleep(30)
"""
    task = asyncio.create_task(
        run_process((sys.executable, "-c", code, os.fspath(ready), os.fspath(survived)))
    )
    for _ in range(200):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.sleep(0.45)
    assert not survived.exists()


async def test_runner_cancellation_terminates_the_process_group(tmp_path: Path) -> None:
    parent_marker = tmp_path / "parent-terminated"
    child_marker = tmp_path / "child-terminated"
    ready_marker = tmp_path / "ready"
    child_ready_marker = tmp_path / "child-ready"
    code = """
import signal
import subprocess
import sys
import time
from pathlib import Path

parent = Path(sys.argv[1])
child = Path(sys.argv[2])
ready = Path(sys.argv[3])
child_ready = Path(sys.argv[4])
def stop_parent(*_args):
    parent.write_text('terminated')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop_parent)
subprocess.Popen([
    sys.executable,
    '-c',
    \"import signal,sys,time; from pathlib import Path; p=Path(sys.argv[1]); ready=Path(sys.argv[2]); signal.signal(signal.SIGTERM, lambda *_: (p.write_text('terminated'), sys.exit(0))); ready.write_text('ready'); time.sleep(30)\",
    str(child),
    str(child_ready),
])
ready.write_text('ready')
time.sleep(30)
"""
    task = asyncio.create_task(
        run_process(
            (
                sys.executable,
                "-c",
                code,
                os.fspath(parent_marker),
                os.fspath(child_marker),
                os.fspath(ready_marker),
                os.fspath(child_ready_marker),
            )
        )
    )
    for _ in range(200):
        if ready_marker.exists() and child_ready_marker.exists():
            break
        if task.done():
            raise AssertionError("process exited before cancellation")
        await asyncio.sleep(0.01)
    assert ready_marker.exists()
    assert child_ready_marker.exists()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    for _ in range(100):
        if parent_marker.exists() and child_marker.exists():
            break
        await asyncio.sleep(0.01)
    assert parent_marker.read_text() == "terminated"
    assert child_marker.read_text() == "terminated"
