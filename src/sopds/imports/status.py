"""Public import coordinator status and result values."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sopds.db.models import ImportState, ImportTrigger
from sopds.imports.fingerprint import SourceFingerprint


class ImportOutcome(StrEnum):
    IMPORTED = "imported"
    UNCHANGED = "unchanged"
    CONTENT_UNCHANGED = "content_unchanged"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ALREADY_RUNNING = "already_running"


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
