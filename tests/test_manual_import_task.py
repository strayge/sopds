"""Manual import task supervision tests."""

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from sopds.db.repository import CatalogRepository
from sopds.imports.coordinator import ImportCoordinator
from sopds.imports.fingerprint import SourceFingerprint
from sopds.imports.service import CatalogImportService
from sopds.imports.status import ImportOutcome, ImportResult, ImportTrigger


async def test_manual_start_is_nonblocking_singleton_and_shutdown_awaits_finalization() -> None:
    coordinator = ImportCoordinator(
        cast(CatalogRepository, object()), Path("catalog.inpx"), Path("archives")
    )
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def blocked_import(*, force: bool) -> ImportResult:
        assert not force
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        return ImportResult(ImportOutcome.IMPORTED, None)

    coordinator._run_reserved_manual_import = blocked_import  # type: ignore[method-assign]

    assert not coordinator.is_import_active()
    assert coordinator.start_manual_import()
    await entered.wait()
    assert coordinator.is_import_active()
    assert not coordinator.start_manual_import()
    assert not finalized.is_set()

    await coordinator.shutdown()

    assert finalized.is_set()
    assert not coordinator.is_import_active()


async def test_manual_reservation_cannot_be_overtaken_by_scheduled_check(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "catalog.inpx"
    source_path.write_bytes(b"catalog")
    repository_mock = MagicMock()
    repository_mock.latest_status = AsyncMock(return_value=None)
    repository_mock.ensure_source = AsyncMock()
    repository_mock.successful_fingerprint = AsyncMock(return_value=None)
    repository_mock.cleanup_inactive = AsyncMock()
    coordinator = ImportCoordinator(
        cast(CatalogRepository, repository_mock), source_path, tmp_path / "archives"
    )
    manual_ran = asyncio.Event()

    async def import_source(trigger: ImportTrigger, fingerprint: SourceFingerprint) -> ImportResult:
        assert trigger is ImportTrigger.MANUAL
        assert fingerprint.sha256 is not None
        manual_ran.set()
        return ImportResult(ImportOutcome.IMPORTED, None)

    service_mock = MagicMock()
    service_mock.import_source = AsyncMock(side_effect=import_source)
    coordinator._service = cast(CatalogImportService, service_mock)

    assert coordinator.start_manual_import()
    scheduled = await coordinator.check_for_changes()
    await manual_ran.wait()
    task = coordinator._manual_task
    assert task is not None
    manual_result = await task
    await asyncio.sleep(0)

    assert scheduled.outcome is ImportOutcome.ALREADY_RUNNING
    assert manual_result.outcome is ImportOutcome.IMPORTED
    repository_mock.ensure_source.assert_awaited_once()
    service_mock.import_source.assert_awaited_once()
    assert not coordinator._manual_reserved
