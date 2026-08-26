"""Immutable values crossing the INPX parser boundary."""

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class PhysicalBookLocator:
    """Identifies the physical archive member without assigning a source namespace."""

    archive_relative_path: PurePosixPath
    member_filename: str


@dataclass(frozen=True, slots=True)
class InpxExtensionField:
    """Preserves a declared field that this version of SOPDS does not interpret."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class InpxRecord:
    """Keeps source metadata lossless except for explicit INPX syntax delimiters."""

    locator: PhysicalBookLocator
    authors: tuple[str, ...]
    genres: tuple[str, ...]
    title: str
    series: str | None
    series_number: str | None
    size: int
    library_id: str | None
    deleted: bool
    extension: str
    date: str | None
    language: str | None
    library_rating: int | None
    keywords: str | None
    extension_fields: tuple[InpxExtensionField, ...]
