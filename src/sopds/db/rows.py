"""Typed values crossing the catalog import persistence boundary."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class ArchiveRow:
    id: int
    generation_id: int
    relative_path: str
    available: bool


@dataclass(frozen=True, slots=True)
class AuthorRow:
    id: int
    generation_id: int
    name: str
    name_sort: str


@dataclass(frozen=True, slots=True)
class GenreRow:
    id: int
    generation_id: int
    code: str
    label: str
    label_sort: str


@dataclass(frozen=True, slots=True)
class SeriesRow:
    id: int
    generation_id: int
    name: str
    name_sort: str


@dataclass(frozen=True, slots=True)
class BookRow:
    id: int
    generation_id: int
    public_id: str
    archive_id: int
    member_filename: str
    title: str
    title_sort: str
    series_id: int | None
    series_number: str | None
    size: int
    libid: str | None
    published_date: date | None
    language: str | None
    original_format: str
    rating: int | None
    keywords: str | None


@dataclass(frozen=True, slots=True)
class BookAuthorRow:
    id: int
    book_id: int
    author_id: int
    position: int


@dataclass(frozen=True, slots=True)
class BookGenreRow:
    id: int
    book_id: int
    genre_id: int


@dataclass(frozen=True, slots=True)
class BookSearchRow:
    book_id: int
    generation_id: int
    title: str
    authors: str
    series: str
    genres: str
    language: str

    def fts_parameters(self) -> list[int | str]:
        """Convert typed values only where they cross the model-free FTS adapter."""
        return [
            self.book_id,
            self.generation_id,
            self.title,
            self.authors,
            self.series,
            self.genres,
            self.language,
        ]


@dataclass(frozen=True, slots=True)
class CatalogWriteBatch:
    archives: tuple[ArchiveRow, ...]
    authors: tuple[AuthorRow, ...]
    genres: tuple[GenreRow, ...]
    series: tuple[SeriesRow, ...]
    books: tuple[BookRow, ...]
    book_authors: tuple[BookAuthorRow, ...]
    book_genres: tuple[BookGenreRow, ...]
    search_rows: tuple[BookSearchRow, ...]
