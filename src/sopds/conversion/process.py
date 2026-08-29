"""Cancellation-safe bounded subprocess execution for converter adapters."""

import asyncio
import logging
import os
import signal
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from sopds.conversion.contracts import ConversionTimeoutError, ConverterExecutionError

PROCESS_TIMEOUT_SECONDS = 300.0
_TERMINATE_GRACE_SECONDS = 2.0
_DIAGNOSTIC_LIMIT = 64 * 1024
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes


async def _drain(reader: asyncio.StreamReader | None) -> bytes:
    retained = bytearray()
    if reader is None:
        return b""
    while chunk := await reader.read(64 * 1024):
        retained.extend(chunk)
        if len(retained) > _DIAGNOSTIC_LIMIT:
            del retained[: len(retained) - _DIAGNOSTIC_LIMIT]
    return bytes(retained)


def _signal_process_group(
    process: asyncio.subprocess.Process, signal_number: signal.Signals
) -> None:
    with suppress(OSError):
        os.killpg(process.pid, signal_number)


def _process_group_exists(process: asyncio.subprocess.Process) -> bool:
    try:
        os.killpg(process.pid, 0)
    except OSError:
        return False
    return True


async def _terminate(process: asyncio.subprocess.Process) -> None:
    _signal_process_group(process, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TERMINATE_GRACE_SECONDS
    # Descendant-only process groups provide no asyncio completion event to await.
    while _process_group_exists(process) and loop.time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
    if _process_group_exists(process):
        _signal_process_group(process, signal.SIGKILL)
    await process.wait()


async def _finish_cancel_cleanup[T](task: asyncio.Future[T]) -> T | None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    try:
        return task.result()
    except BaseException:
        return None


async def _finish_drains_bounded(tasks: Sequence[asyncio.Task[bytes]]) -> None:
    _, pending = await asyncio.wait(tasks, timeout=_TERMINATE_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _cleanup_process(
    process: asyncio.subprocess.Process, drain_tasks: Sequence[asyncio.Task[bytes]]
) -> None:
    await _terminate(process)
    await _finish_drains_bounded(drain_tasks)


async def _cleanup_created_process(process: asyncio.subprocess.Process) -> None:
    drain_tasks = (
        asyncio.create_task(_drain(process.stdout)),
        asyncio.create_task(_drain(process.stderr)),
    )
    await _cleanup_process(process, drain_tasks)


async def run_process(argv: Sequence[str]) -> ProcessResult:
    """Run one argv-only converter command while owning its process group to reaping."""
    if not argv or any(not isinstance(argument, str) or "\x00" in argument for argument in argv):
        raise ConverterExecutionError("Converter execution failed")
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        created_process = await _finish_cancel_cleanup(creation)
        if created_process is not None:
            cleanup = asyncio.create_task(_cleanup_created_process(created_process))
            await _finish_cancel_cleanup(cleanup)
        raise
    except Exception as error:
        raise ConverterExecutionError("Converter execution failed") from error

    stdout_task = asyncio.create_task(_drain(process.stdout))
    stderr_task = asyncio.create_task(_drain(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    owned_tasks = (wait_task, stdout_task, stderr_task)
    try:
        _, pending = await asyncio.wait(
            owned_tasks,
            timeout=PROCESS_TIMEOUT_SECONDS,
            return_when=asyncio.ALL_COMPLETED,
        )
        if pending:
            await _cleanup_process(process, (stdout_task, stderr_task))
            await asyncio.gather(wait_task, return_exceptions=True)
            raise ConversionTimeoutError("Converter timed out")
        stdout = stdout_task.result()
        stderr = stderr_task.result()
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_cleanup_process(process, (stdout_task, stderr_task)))
        await _finish_cancel_cleanup(cleanup)
        await _finish_cancel_cleanup(wait_task)
        raise
    except ConversionTimeoutError:
        raise
    except BaseException as error:
        if process.returncode is None:
            await _cleanup_process(process, (stdout_task, stderr_task))
        await asyncio.gather(*owned_tasks, return_exceptions=True)
        raise ConverterExecutionError("Converter execution failed") from error

    if process.returncode != 0:
        _LOGGER.warning(
            "Converter exited unsuccessfully returncode=%d stdout=%r stderr=%r",
            process.returncode,
            stdout,
            stderr,
        )
        raise ConverterExecutionError("Converter execution failed")
    return ProcessResult(stdout=stdout, stderr=stderr)


__all__ = ["ProcessResult", "run_process"]
