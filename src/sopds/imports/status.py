"""Public import coordinator status and result values."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sopds.imports.fingerprint import SourceFingerprint


class ImportTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class ImportState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ImportOutcome(StrEnum):
    IMPORTED = "imported"
    UNCHANGED = "unchanged"
    CONTENT_UNCHANGED = "content_unchanged"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ALREADY_RUNNING = "already_running"


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    interrupted_runs: int
    failed_generations: int
    removed_generations: int


@dataclass(frozen=True, slots=True)
class GenerationCleanupSummary:
    removed_generations: int


@dataclass(frozen=True, slots=True)
class ArchiveAvailabilitySummary:
    checked: int
    available: int
    unavailable: int
    changed_to_available: int
    changed_to_unavailable: int


@dataclass(frozen=True, slots=True)
class ImportStatus:
    run_id: int
    trigger: ImportTrigger
    state: ImportState
    started_at: datetime
    finished_at: datetime | None
    attempted_fingerprint: SourceFingerprint | None
    records_read: int
    records_imported: int
    records_deleted: int
    records_rejected: int
    error_summary: str | None
    generation_id: int | None


@dataclass(frozen=True, slots=True)
class ImportResult:
    outcome: ImportOutcome
    status: ImportStatus | None


class ImportStatusProvider(Protocol):
    async def get_status(self) -> ImportStatus | None: ...

    def is_import_active(self) -> bool: ...

    def start_manual_import(self) -> bool: ...
