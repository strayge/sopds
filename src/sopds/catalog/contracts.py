"""Database-free catalog values shared with presentation adapters."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class CatalogInputError(ValueError):
    """Reject unsafe or inconsistent user-controlled catalog input."""


class CatalogStaleCursorError(CatalogInputError):
    """Reject pagination state whose catalog generation is no longer active."""


@dataclass(frozen=True, slots=True)
class CatalogRequest:
    query: str = ""
    language: str | None = None
    genre: str | None = None
    original_format: str | None = None
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class BookSummary:
    public_id: str
    title: str
    authors: tuple[str, ...]
    series: str | None
    series_number: str | None
    language: str | None
    original_format: str


@dataclass(frozen=True, slots=True)
class BookDetail:
    public_id: str
    title: str
    authors: tuple[str, ...]
    genres: tuple[tuple[str, str], ...]
    series: str | None
    series_number: str | None
    size: int
    libid: str | None
    published_date: date | None
    language: str | None
    original_format: str
    rating: int | None
    keywords: str | None


@dataclass(frozen=True, slots=True)
class CatalogPage:
    books: tuple[BookSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class FilterOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    languages: tuple[FilterOption, ...]
    genres: tuple[FilterOption, ...]
    original_formats: tuple[FilterOption, ...]


class Catalog(Protocol):
    async def browse(self, request: CatalogRequest) -> CatalogPage: ...

    async def details(self, public_id: str) -> BookDetail | None: ...

    async def filters(self) -> CatalogFilters: ...
