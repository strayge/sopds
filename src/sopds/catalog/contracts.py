"""Database-free catalog values shared with presentation adapters."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol


class CatalogInputError(ValueError):
    """Reject unsafe or inconsistent user-controlled catalog input."""


class CatalogStaleCursorError(CatalogInputError):
    """Reject pagination state whose catalog generation is no longer active."""


class SearchField(StrEnum):
    ALL = "all"
    TITLE = "title"
    AUTHOR = "author"
    SERIES = "series"


@dataclass(frozen=True, slots=True)
class CatalogRequest:
    query: str = ""
    language: str | None = None
    genre: str | None = None
    original_format: str | None = None
    cursor: str | None = None
    author: str | None = None
    series: str | None = None
    without_series: bool = False
    search_field: SearchField = SearchField.ALL
    page_size: int = 50


@dataclass(frozen=True, slots=True)
class BookSummary:
    public_id: str
    title: str
    authors: tuple[str, ...]
    series: str | None
    series_number: str | None
    language: str | None
    original_format: str
    size: int = 0
    genres: tuple[tuple[str, str], ...] = ()
    published_date: date | None = None
    libid: str | None = None
    rating: int | None = None
    keywords: str | None = None
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)


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
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FilterOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    generation_id: int | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CatalogStatistics:
    total_books: int
    hidden_books: int
    missed_books: int
    active_books: int
    generation_activated_at: datetime | None
    database_size_bytes: int


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    kind: str
    cursor: str | None = None
    prefix: str = ""
    exact: bool = False
    author: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationItem:
    value: str
    label: str
    count: int | None = None
    exact: bool = False
    count_kind: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationPage:
    items: tuple[NavigationItem, ...]
    next_cursor: str | None
    updated_at: datetime
    prefix: str = ""
    grouped: bool = False
    books: tuple[BookSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorBookCounts:
    series: int
    without_series: int
    total: int
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    languages: tuple[FilterOption, ...]
    genres: tuple[FilterOption, ...]
    original_formats: tuple[FilterOption, ...]


class Catalog(Protocol):
    async def check_readiness(self) -> None: ...

    async def browse(self, request: CatalogRequest) -> CatalogPage: ...

    async def details(self, public_id: str) -> BookDetail | None: ...

    async def filters(self) -> CatalogFilters: ...

    async def snapshot(self) -> CatalogSnapshot: ...

    async def statistics(self) -> CatalogStatistics: ...

    async def navigation(self, request: NavigationRequest) -> NavigationPage: ...

    async def author_book_counts(self, author: str) -> AuthorBookCounts: ...
