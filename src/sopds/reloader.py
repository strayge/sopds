"""Development process supervision with lossless restart throttling."""

import logging
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic
from types import FrameType

_LOGGER = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 0.5
_RESTART_INTERVAL_SECONDS = 10.0
_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_FileSignature = tuple[int, int, int]
_FileSnapshot = dict[Path, _FileSignature]


@dataclass
class RestartSchedule:
    """Preserve a final restart while coalescing edits made during the cooldown."""

    minimum_interval_seconds: float
    last_restart_started_at: float | None = None
    pending_at: float | None = None

    def notify_change(self, now: float) -> float:
        if self.pending_at is None:
            earliest_restart = now
            if self.last_restart_started_at is not None:
                earliest_restart = max(
                    earliest_restart,
                    self.last_restart_started_at + self.minimum_interval_seconds,
                )
            self.pending_at = earliest_restart
        return self.pending_at

    def take_due(self, now: float) -> bool:
        if self.pending_at is None or now < self.pending_at:
            return False
        self.pending_at = None
        self.last_restart_started_at = now
        return True

    def wait_seconds(self, now: float, maximum: float) -> float:
        if self.pending_at is None:
            return maximum
        return min(maximum, max(0.0, self.pending_at - now))


def _file_signature(path: Path) -> _FileSignature | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _snapshot_files(package_path: Path, config_path: Path) -> _FileSnapshot:
    snapshot: _FileSnapshot = {}
    for path in package_path.rglob("*.py"):
        signature = _file_signature(path)
        if signature is not None:
            snapshot[path.resolve()] = signature

    config_signature = _file_signature(config_path)
    if config_signature is not None:
        snapshot[config_path] = config_signature
    return snapshot


def _start_child(config_path: Path) -> subprocess.Popen[bytes]:
    command = [sys.executable, "-m", "sopds", "--config", str(config_path)]
    process = subprocess.Popen(command, start_new_session=True)  # noqa: S603
    _LOGGER.info(f"Reload child started phase=reload child_pid={process.pid}")
    return process


def _stop_child(process: subprocess.Popen[bytes], shutdown_signal: int) -> None:
    if process.poll() is not None:
        return

    process.send_signal(shutdown_signal)
    try:
        process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _LOGGER.warning(
            f"Reload child shutdown timed out phase=reload child_pid={process.pid} action=kill"
        )
        process.kill()
        process.wait()


def run_reload_supervisor(config_path: Path, package_path: Path) -> None:
    """
    Keep invalid intermediate edits recoverable without changing normal application startup.
    """
    absolute_config_path = config_path.absolute()
    resolved_package_path = package_path.resolve()
    snapshot = _snapshot_files(resolved_package_path, absolute_config_path)
    schedule = RestartSchedule(_RESTART_INTERVAL_SECONDS)
    shutdown_requested = Event()
    shutdown_signal: int | None = None

    def request_shutdown(signum: int, _frame: FrameType | None) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signum
        shutdown_requested.set()

    watched_signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform != "win32":
        watched_signals.append(signal.SIGHUP)
    previous_handlers = {signum: signal.getsignal(signum) for signum in watched_signals}
    for signum in watched_signals:
        signal.signal(signum, request_shutdown)

    process: subprocess.Popen[bytes] | None = None
    try:
        process = _start_child(absolute_config_path)
        child_exit_reported = False
        while not shutdown_requested.is_set():
            now = monotonic()
            shutdown_requested.wait(schedule.wait_seconds(now, _POLL_INTERVAL_SECONDS))
            if shutdown_requested.is_set():
                break

            now = monotonic()
            current_snapshot = _snapshot_files(resolved_package_path, absolute_config_path)
            changed = current_snapshot != snapshot
            snapshot = current_snapshot
            was_pending = schedule.pending_at is not None

            if changed:
                pending_at = schedule.notify_change(now)
                if not was_pending:
                    if pending_at <= now:
                        _LOGGER.info(
                            "Reload-triggering change detected phase=reload action=restart"
                        )
                    else:
                        delay_seconds = max(0.0, pending_at - now)
                        _LOGGER.info(
                            f"Reload-triggering change detected phase=reload action=schedule "
                            f"delay_seconds={delay_seconds:.1f}"
                        )

            if schedule.take_due(now):
                if was_pending:
                    _LOGGER.info("Scheduled reload started phase=reload action=restart")
                _stop_child(process, signal.SIGTERM)
                if shutdown_requested.is_set():
                    break
                process = _start_child(absolute_config_path)
                child_exit_reported = False

            exit_code = process.poll()
            if exit_code is not None and not child_exit_reported:
                _LOGGER.warning(
                    f"Reload child exited phase=reload child_pid={process.pid} "
                    f"exit_code={exit_code} action=wait_for_change"
                )
                child_exit_reported = True
    finally:
        if process is not None:
            _stop_child(process, shutdown_signal or signal.SIGTERM)
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
