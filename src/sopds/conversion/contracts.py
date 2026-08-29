"""Database-free contracts for conversion orchestration and artifacts."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sopds.acquisition.contracts import AsyncByteStream, SourceRevision

_FORMAT = re.compile(r"[a-z0-9][a-z0-9_-]*")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ConversionError(Exception):
    """Base class for path-free conversion failures."""


class UnsupportedConversionError(ConversionError):
    """No registered converter supports the requested format pair."""


class SourceChangedError(ConversionError):
    """The original changed after its revision was described."""


class SourceUnavailableError(ConversionError):
    """The original cannot currently be acquired."""


class ConversionSourceError(ConversionError):
    """The source failed safety, integrity, or I/O checks during conversion."""


class ConverterExecutionError(ConversionError):
    """A converter failed while processing a source."""


class ConversionTimeoutError(ConversionError):
    """A converter exceeded its execution limit."""


class InvalidConversionOutputError(ConversionError):
    """A converter did not create a valid artifact."""


class ConversionShutdownError(ConversionError):
    """Conversion is shutting down and rejects new work."""


def normalize_format(value: str) -> str:
    """Canonicalize only simple, path-free format identifiers."""
    raw = value.strip()
    if raw.startswith("."):
        raw = raw[1:]
    normalized = raw.casefold()
    if not raw.isascii() or not _FORMAT.fullmatch(normalized):
        raise ValueError("Invalid format identifier")
    return normalized


def _validated_token(value: str, label: str) -> str:
    normalized = value.strip()
    if not _IDENTITY.fullmatch(normalized):
        raise ValueError(f"Invalid converter {label}")
    return normalized


@dataclass(frozen=True, slots=True)
class ConverterIdentity:
    name: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validated_token(self.name, "name"))
        object.__setattr__(self, "version", _validated_token(self.version, "version"))


@dataclass(frozen=True, slots=True)
class ConversionCapability:
    source_format: str
    target_format: str
    target_media_type: str
    target_extension: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_format", normalize_format(self.source_format))
        object.__setattr__(self, "target_format", normalize_format(self.target_format))
        media_type = self.target_media_type.strip().casefold()
        if not media_type or "/" not in media_type or "\r" in media_type or "\n" in media_type:
            raise ValueError("Invalid target media type")
        object.__setattr__(self, "target_media_type", media_type)
        object.__setattr__(self, "target_extension", normalize_format(self.target_extension))


@dataclass(frozen=True, slots=True)
class ConversionSourceKey:
    public_id: str
    revision: SourceRevision
    source_format: str
    target_format: str
    converter: ConverterIdentity

    def __post_init__(self) -> None:
        if not self.public_id or "\x00" in self.public_id:
            raise ValueError("Invalid public identifier")
        object.__setattr__(self, "source_format", normalize_format(self.source_format))
        object.__setattr__(self, "target_format", normalize_format(self.target_format))


@dataclass(frozen=True, slots=True)
class CacheCleanupSummary:
    removed_files: int
    failed_entries: int


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    content_length: int
    stream: AsyncByteStream


@dataclass(frozen=True, slots=True)
class ConversionResult:
    filename: str
    media_type: str
    content_length: int
    stream: AsyncByteStream


class Converter(Protocol):
    @property
    def identity(self) -> ConverterIdentity: ...

    @property
    def capabilities(self) -> tuple[ConversionCapability, ...]: ...

    async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
        """Enforce an adapter-owned deadline and report it as ConversionTimeoutError."""
        ...


ArtifactProducer = Callable[[Path], Awaitable[None]]
