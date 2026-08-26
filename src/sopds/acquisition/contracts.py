"""Database-free contracts for acquiring original book files."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class AcquisitionError(Exception):
    """Base class for failures whose public presentation must remain path-free."""


class AcquisitionNotFoundError(AcquisitionError):
    """The requested book is not present in the active catalog."""


class AcquisitionUnavailableError(AcquisitionError):
    """The catalog knows the original, but its archive is unavailable."""


class AcquisitionUnsafePathError(AcquisitionError):
    """A catalog path could escape or ambiguously address the library root."""


class AcquisitionMemberNotFoundError(AcquisitionError):
    """The exact catalog member is absent from its archive."""


class AcquisitionAmbiguousMemberError(AcquisitionError):
    """An archive contains more than one exact match for the catalog member."""


class AcquisitionEncryptedMemberError(AcquisitionError):
    """The original member requires decryption."""


class AcquisitionDirectoryMemberError(AcquisitionError):
    """The catalog member denotes a directory rather than a file."""


class AcquisitionSymlinkMemberError(AcquisitionError):
    """The catalog member is represented by a ZIP symbolic link."""


class AcquisitionSizeMismatchError(AcquisitionError):
    """Catalog, ZIP metadata, or streamed byte counts disagree."""


class AcquisitionCorruptError(AcquisitionError):
    """The ZIP archive or selected member is corrupt or truncated."""


class AcquisitionStoreShutdownError(AcquisitionError):
    """The original store is shutting down and rejects new work."""


@dataclass(frozen=True, slots=True)
class AcquisitionTarget:
    generation_id: int
    public_id: str
    title: str
    expected_size: int
    original_format: str
    archive_relative_path: str
    member_filename: str


@runtime_checkable
class AsyncByteStream(Protocol):
    """A caller-owned stream that must be closed, including after cancellation."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class AcquisitionRepository(Protocol):
    async def acquisition_target(self, public_id: str) -> AcquisitionTarget | None: ...


class OriginalStore(Protocol):
    async def open(self, target: AcquisitionTarget) -> AsyncByteStream: ...

    async def shutdown(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AcquiredOriginal:
    filename: str
    media_type: str
    content_length: int
    stream: AsyncByteStream


class Acquisition(Protocol):
    async def acquire(self, public_id: str) -> AcquiredOriginal: ...
