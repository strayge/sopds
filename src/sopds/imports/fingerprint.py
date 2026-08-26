"""Bounded, off-event-loop source fingerprint operations."""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

_HASH_CHUNK_BYTES = 1024 * 1024


class SourceUnstableError(OSError):
    """Reject a digest when source metadata changes around the hash pass."""


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    size: int
    mtime_ns: int
    sha256: str | None = None

    def same_metadata(self, other: SourceFingerprint) -> bool:
        return self.size == other.size and self.mtime_ns == other.mtime_ns


def _stat(path: Path) -> SourceFingerprint:
    info = path.stat()
    if not path.is_file():
        raise OSError("Configured INPX source is not a regular file")
    return SourceFingerprint(size=info.st_size, mtime_ns=info.st_mtime_ns)


def _hash(path: Path, metadata: SourceFingerprint) -> SourceFingerprint:
    before = _stat(path)
    if not before.same_metadata(metadata):
        raise SourceUnstableError("Catalog source changed before hashing")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    after = _stat(path)
    if not after.same_metadata(before):
        raise SourceUnstableError("Catalog source changed while hashing")
    return SourceFingerprint(after.size, after.mtime_ns, digest.hexdigest())


async def stat_source(path: Path) -> SourceFingerprint:
    """Keep filesystem metadata calls away from the serving event loop."""
    return await asyncio.to_thread(_stat, path)


async def hash_source(path: Path, metadata: SourceFingerprint) -> SourceFingerprint:
    """Hash incrementally so even very large sources use bounded memory."""
    return await asyncio.to_thread(_hash, path, metadata)
