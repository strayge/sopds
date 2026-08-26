"""Singleton import coordination, change checks, and recovery."""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from sopds.db.repository import DEFAULT_BATCH_SIZE, CatalogRepository
from sopds.imports.availability import archive_availability_rows
from sopds.imports.fingerprint import SourceFingerprint, hash_source, stat_source
from sopds.imports.service import CatalogImportService
from sopds.imports.status import (
    ImportOutcome,
    ImportResult,
    ImportState,
    ImportStatus,
    ImportTrigger,
)

_LOGGER = logging.getLogger(__name__)


class ImportCoordinator:
    """Reject overlapping requests in the single-process, single-Uvicorn-worker runtime.

    The lock is intentionally process-local. Production must retain the documented one-process,
    one-worker deployment invariant unless coordination is replaced with a database lease.
    """

    def __init__(
        self,
        repository: CatalogRepository,
        source_path: Path,
        archive_root: Path,
        *,
        namespace: str = "default",
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._repository = repository
        self._source_path = source_path
        self._archive_root = archive_root
        self._namespace = namespace
        self._service = CatalogImportService(
            repository, source_path, archive_root, namespace=namespace, batch_size=batch_size
        )
        self._import_lock = asyncio.Lock()
        self._manual_task: asyncio.Task[ImportResult] | None = None
        self._manual_reserved = False

    async def recover(self) -> None:
        await self._repository.ensure_source(self._namespace, self._source_path)
        await self._repository.recover()

    async def get_status(self) -> ImportStatus | None:
        return await self._repository.latest_status()

    async def check_for_changes(self) -> ImportResult:
        return await self._request(ImportTrigger.SCHEDULED, force=False)

    async def force_import(self) -> ImportResult:
        return await self._request(ImportTrigger.MANUAL, force=True)

    async def _run_reserved_manual_import(self) -> ImportResult:
        try:
            return await self._request(ImportTrigger.MANUAL, force=True)
        finally:
            if asyncio.current_task() is self._manual_task:
                self._manual_reserved = False

    def start_manual_import(self) -> bool:
        """Reserve admission synchronously so later scheduled checks cannot overtake it."""
        if (
            self._import_lock.locked()
            or self._manual_reserved
            or (self._manual_task is not None and not self._manual_task.done())
        ):
            _LOGGER.info("Manual catalog import rejected because another import is running")
            return False
        _LOGGER.info("Manual catalog import accepted")
        self._manual_reserved = True
        try:
            task = asyncio.create_task(
                self._run_reserved_manual_import(), name="manual-catalog-import"
            )
        except Exception:
            self._manual_reserved = False
            raise
        self._manual_task = task
        task.add_done_callback(self._manual_done)
        return True

    def _manual_done(self, task: asyncio.Task[ImportResult]) -> None:
        if self._manual_task is task:
            self._manual_task = None
            self._manual_reserved = False
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            _LOGGER.exception("Manual catalog import task failed")

    async def shutdown(self) -> None:
        task = self._manual_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._manual_reserved = False

    async def _request(self, trigger: ImportTrigger, *, force: bool) -> ImportResult:
        current = asyncio.current_task()
        owns_manual_reservation = (
            trigger is ImportTrigger.MANUAL
            and self._manual_reserved
            and current is self._manual_task
        )
        if (self._manual_reserved and not owns_manual_reservation) or self._import_lock.locked():
            _LOGGER.info(
                "Catalog import request rejected because another import is running (trigger=%s)",
                trigger.value,
            )
            return ImportResult(ImportOutcome.ALREADY_RUNNING, await self.get_status())
        await self._import_lock.acquire()
        try:
            _LOGGER.info(
                "Catalog source check started (trigger=%s, force=%s)",
                trigger.value,
                force,
            )
            if trigger is ImportTrigger.SCHEDULED:
                await self.refresh_archive_availability()
            await self._repository.ensure_source(self._namespace, self._source_path)
            try:
                metadata = await stat_source(self._source_path)
            except OSError as error:
                return await self._record_source_failure(trigger, error)
            successful = await self._repository.successful_fingerprint()
            if not force and successful is not None and metadata.same_metadata(successful):
                _LOGGER.info("Catalog source metadata is unchanged")
                return ImportResult(ImportOutcome.UNCHANGED, await self.get_status())
            try:
                fingerprint = await hash_source(self._source_path, metadata)
            except OSError as error:
                return await self._record_source_failure(trigger, error, metadata)
            if not force and successful is not None and fingerprint.sha256 == successful.sha256:
                await self._repository.update_fingerprint_metadata(fingerprint)
                _LOGGER.info("Catalog source content is unchanged")
                return ImportResult(ImportOutcome.CONTENT_UNCHANGED, await self.get_status())
            result = await self._service.import_source(trigger, fingerprint)
            await self._repository.cleanup_inactive()
            _LOGGER.info(
                "Catalog import request finished (trigger=%s, outcome=%s)",
                trigger.value,
                result.outcome.value,
            )
            return result
        finally:
            self._import_lock.release()

    async def _record_source_failure(
        self,
        trigger: ImportTrigger,
        error: OSError,
        fingerprint: SourceFingerprint | None = None,
    ) -> ImportResult:
        _LOGGER.warning("Catalog source check failed: %s", type(error).__name__)
        run_id = await self._repository.create_run(trigger, fingerprint)
        await self._repository.finish_failed(
            run_id,
            None,
            ImportState.FAILED,
            "Could not read the configured catalog source",
            (0, 0, 0, 0),
        )
        return ImportResult(ImportOutcome.FAILED, await self.get_status())

    async def refresh_archive_availability(self) -> None:
        archives = await self._repository.active_archives()
        values = await asyncio.to_thread(archive_availability_rows, self._archive_root, archives)
        await self._repository.update_archive_availability(values)
