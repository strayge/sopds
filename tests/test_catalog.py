"""Catalog normalization, visibility, filtering, and keyset query tests."""

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from sopds.catalog.contracts import (
    BookDetail,
    BookSummary,
    CatalogFilters,
    CatalogInputError,
    CatalogRequest,
    CatalogStaleCursorError,
)
from sopds.catalog.search import fts_match_expression, normalize_text, query_tokens
from sopds.catalog.service import CatalogService
from sopds.db.connection import close_database, initialize_database
from sopds.db.migrations_runner import apply_migrations
from sopds.db.models import (
    Archive,
    Author,
    Book,
    BookAuthor,
    BookGenre,
    CatalogGeneration,
    CatalogState,
    GenerationState,
    Genre,
    Series,
)
from sopds.db.repository import CatalogRepository


@asynccontextmanager
async def _catalog(path: Path) -> AsyncIterator[tuple[CatalogService, CatalogRepository]]:
    await apply_migrations(path)
    context = await initialize_database(path)
    repository = CatalogRepository(context.db())
    try:
        yield CatalogService(repository, b"test-cursor-key"), repository
    finally:
        await close_database(context)


async def _seed(repository: CatalogRepository) -> None:
    connection = repository._connection
    active = await CatalogGeneration.create(using_db=connection, id=1, state=GenerationState.ACTIVE)
    staging = await CatalogGeneration.create(
        using_db=connection, id=2, state=GenerationState.IMPORTING
    )
    await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=active.id)
    available = await Archive.create(
        using_db=connection,
        id=1,
        generation=active,
        relative_path="available.zip",
        available=True,
    )
    unavailable = await Archive.create(
        using_db=connection,
        id=2,
        generation=active,
        relative_path="missing.zip",
        available=False,
    )
    staging_archive = await Archive.create(
        using_db=connection,
        id=3,
        generation=staging,
        relative_path="staging.zip",
        available=True,
    )
    series = await Series.create(
        using_db=connection, id=1, generation=active, name="Ёлки", name_sort="елки"
    )
    authors = [
        await Author.create(
            using_db=connection,
            id=index,
            generation=active,
            name=name,
            name_sort=normalize_text(name),
        )
        for index, name in enumerate(("Second Author", "First Ёжов"), start=1)
    ]
    genres = [
        await Genre.create(
            using_db=connection,
            id=index,
            generation=active,
            code=code,
            label=label,
            label_sort=normalize_text(label),
        )
        for index, (code, label) in enumerate(
            (("sf", "Science fiction"), ("prose", "Prose")), start=1
        )
    ]
    for index in range(55):
        book = await Book.create(
            using_db=connection,
            id=index + 1,
            generation=active,
            public_id=f"book-{index:03}",
            archive=available,
            member_filename=f"book-{index:03}.fb2",
            title="Ёжик" if index == 1 else f"Book {index:03}",
            title_sort="ежик" if index == 1 else f"book {index:03}",
            series=series if index == 1 else None,
            series_number="2" if index == 1 else None,
            size=100 + index,
            language="ru" if index == 1 else "en",
            original_format="epub" if index == 2 else "fb2",
        )
        if index == 1:
            await BookAuthor.create(
                using_db=connection, id=1, book=book, author=authors[1], position=0
            )
            await BookAuthor.create(
                using_db=connection, id=2, book=book, author=authors[0], position=1
            )
            await BookGenre.create(using_db=connection, id=1, book=book, genre=genres[0])
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                book.id,
                active.id,
                normalize_text(book.title),
                "first ежов second author" if index == 1 else "",
                "елки" if index == 1 else "",
                "sf" if index == 1 else "",
                book.language or "",
            ],
        )
    hidden = await Book.create(
        using_db=connection,
        id=100,
        generation=active,
        public_id="hidden",
        archive=unavailable,
        member_filename="hidden.fb2",
        title="Hidden unique",
        title_sort="hidden unique",
        size=1,
        language="zz",
        original_format="mobi",
    )
    staged = await Book.create(
        using_db=connection,
        id=101,
        generation=staging,
        public_id="staged",
        archive=staging_archive,
        member_filename="staged.fb2",
        title="Staged unique",
        title_sort="staged unique",
        size=1,
        language="xx",
        original_format="txt",
    )
    for book, generation_id in ((hidden, active.id), (staged, staging.id)):
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES (?,?,?,?,?,?,?)",
            [book.id, generation_id, normalize_text(book.title), "", "", "", book.language],
        )


def test_normalization_and_safe_fts_terms() -> None:
    assert normalize_text("  ЁЖИК \uff21  ") == "  ежик a  "
    assert query_tokens("Ёжик, BOOK_2!") == ("ежик", "book", "2")
    assert fts_match_expression(("ежик", "book")) == (
        '{title authors series} : "ежик" AND {title authors series} : "book"'
    )
    assert query_tokens('title:"x" OR *; --') == ("title", "x", "or")
    assert query_tokens("..._") == ()
    with pytest.raises(CatalogInputError):
        query_tokens("x" * 201)
    with pytest.raises(CatalogInputError):
        query_tokens(" ".join(f"w{index}" for index in range(17)))


@pytest.mark.asyncio
async def test_catalog_visibility_search_filters_details_and_keyset(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "catalog.sqlite3") as (catalog, repository):
        await _seed(repository)

        first = await catalog.browse(CatalogRequest())
        assert len(first.books) == 50
        assert first.next_cursor is not None
        second = await catalog.browse(CatalogRequest(cursor=first.next_cursor))
        assert len(second.books) == 5
        assert {book.public_id for book in first.books}.isdisjoint(
            book.public_id for book in second.books
        )
        assert [book.title for book in (*first.books, *second.books)] == sorted(
            (book.title for book in (*first.books, *second.books)), key=normalize_text
        )

        assert [
            book.public_id for book in (await catalog.browse(CatalogRequest(query="ежик"))).books
        ] == ["book-001"]
        assert not (await catalog.browse(CatalogRequest(query="еж"))).books
        assert [
            book.public_id
            for book in (await catalog.browse(CatalogRequest(query="first елки"))).books
        ] == ["book-001"]
        assert not (await catalog.browse(CatalogRequest(query="first missing"))).books
        assert not (await catalog.browse(CatalogRequest(query="hidden"))).books
        assert not (await catalog.browse(CatalogRequest(query="staged"))).books

        assert [
            book.public_id for book in (await catalog.browse(CatalogRequest(language="ru"))).books
        ] == ["book-001"]
        assert [
            book.public_id for book in (await catalog.browse(CatalogRequest(genre="sf"))).books
        ] == ["book-001"]
        assert [
            book.public_id
            for book in (await catalog.browse(CatalogRequest(original_format="epub"))).books
        ] == ["book-002"]
        assert not (await catalog.browse(CatalogRequest(language="r"))).books

        detail = await catalog.details("book-001")
        assert detail is not None
        assert detail.authors == ("First Ёжов", "Second Author")
        assert detail.genres == (("sf", "Science fiction"),)
        assert await catalog.details("hidden") is None
        assert await catalog.details("staged") is None

        filters = await catalog.filters()
        assert [option.value for option in filters.languages] == ["en", "ru"]
        assert [option.value for option in filters.genres] == ["sf"]
        assert [option.value for option in filters.original_formats] == ["epub", "fb2"]

        with pytest.raises(CatalogInputError):
            await catalog.browse(CatalogRequest(cursor="not-base64"))
        with pytest.raises(CatalogInputError):
            await catalog.browse(CatalogRequest(query="other", cursor=first.next_cursor))

        assert first.next_cursor is not None
        raw = bytearray(
            base64.urlsafe_b64decode(first.next_cursor + "=" * (-len(first.next_cursor) % 4))
        )
        raw[5] ^= 1
        tampered = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with pytest.raises(CatalogInputError, match="Invalid catalog cursor"):
            await catalog.browse(CatalogRequest(cursor=tampered))

        restarted = CatalogService(repository, b"different-cursor-key")
        with pytest.raises(CatalogInputError, match="Invalid catalog cursor"):
            await restarted.browse(CatalogRequest(cursor=first.next_cursor))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["availability", "cleanup"])
async def test_hydration_omits_books_that_stop_being_visible(tmp_path: Path, change: str) -> None:
    async with _catalog(tmp_path / f"{change}.sqlite3") as (catalog, repository):
        await _seed(repository)
        original_summaries = repository.summaries
        changed = False

        async def summaries(generation_id: int, book_ids: list[int]) -> list[BookSummary]:
            nonlocal changed
            if not changed:
                changed = True
                if change == "availability":
                    await (
                        Archive.filter(id=1)
                        .using_db(repository._connection)
                        .update(available=False)
                    )
                else:
                    await Book.filter(id=book_ids[0]).using_db(repository._connection).delete()
            return await original_summaries(generation_id, book_ids)

        repository.summaries = summaries  # type: ignore[method-assign]
        page = await catalog.browse(CatalogRequest())

        expected_count = 0 if change == "availability" else 49
        assert len(page.books) == expected_count
        assert all(book.public_id != "book-000" for book in page.books)


@pytest.mark.asyncio
async def test_browse_retries_activation_change_but_cursor_becomes_stale(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "activation.sqlite3") as (catalog, repository):
        await _seed(repository)
        first_page = await catalog.browse(CatalogRequest())
        assert first_page.next_cursor is not None
        original_summaries = repository.summaries
        activated = False

        async def activate_then_hydrate(
            generation_id: int, book_ids: list[int]
        ) -> list[BookSummary]:
            nonlocal activated
            if not activated:
                activated = True
                await (
                    CatalogState.filter(id=1)
                    .using_db(repository._connection)
                    .update(active_generation_id=2)
                )
            return await original_summaries(generation_id, book_ids)

        repository.summaries = activate_then_hydrate  # type: ignore[method-assign]
        retried = await catalog.browse(CatalogRequest())
        assert [book.public_id for book in retried.books] == ["staged"]

        await (
            CatalogState.filter(id=1)
            .using_db(repository._connection)
            .update(active_generation_id=1)
        )
        activated = False
        with pytest.raises(CatalogStaleCursorError):
            await catalog.browse(CatalogRequest(cursor=first_page.next_cursor))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_id", "expected_title"),
    [("staged", "Staged unique"), ("book-001", None)],
)
async def test_details_retries_activation_change_during_hydration(
    tmp_path: Path, public_id: str, expected_title: str | None
) -> None:
    async with _catalog(tmp_path / f"detail-{public_id}.sqlite3") as (catalog, repository):
        await _seed(repository)
        original_detail = repository.detail
        generation_ids: list[int] = []

        async def activate_then_return_detail(
            generation_id: int, public_id: str
        ) -> BookDetail | None:
            generation_ids.append(generation_id)
            detail = await original_detail(generation_id, public_id)
            if generation_id == 1:
                await (
                    CatalogState.filter(id=1)
                    .using_db(repository._connection)
                    .update(active_generation_id=2)
                )
            return detail

        repository.detail = activate_then_return_detail  # type: ignore[method-assign]

        detail = await catalog.details(public_id)

        assert generation_ids == [1, 2]
        actual_title = None if detail is None else detail.title
        assert actual_title == expected_title


@pytest.mark.asyncio
async def test_details_rejects_two_activation_changes(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "detail-repeated-activation.sqlite3") as (
        catalog,
        repository,
    ):
        await _seed(repository)
        original_detail = repository.detail
        generation_ids: list[int] = []

        async def change_activation_after_detail(
            generation_id: int, public_id: str
        ) -> BookDetail | None:
            generation_ids.append(generation_id)
            detail = await original_detail(generation_id, public_id)
            next_generation_id = 2 if generation_id == 1 else 1
            await (
                CatalogState.filter(id=1)
                .using_db(repository._connection)
                .update(active_generation_id=next_generation_id)
            )
            return detail

        repository.detail = change_activation_after_detail  # type: ignore[method-assign]

        with pytest.raises(CatalogInputError, match="Catalog changed while loading"):
            await catalog.details("book-001")

        assert generation_ids == [1, 2]


@pytest.mark.asyncio
async def test_filters_cache_avoids_repeated_repository_scan(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "filter-cache.sqlite3") as (catalog, repository):
        await _seed(repository)
        original_filters = repository.catalog_filters
        calls = 0

        async def catalog_filters(generation_id: int) -> CatalogFilters:
            nonlocal calls
            calls += 1
            return await original_filters(generation_id)

        repository.catalog_filters = catalog_filters  # type: ignore[method-assign]

        first = await catalog.filters()
        second = await catalog.filters()

        assert second is first
        assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_filter_cache_misses_are_single_flight(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "filter-single-flight.sqlite3") as (catalog, repository):
        await _seed(repository)
        original_filters = repository.catalog_filters
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def catalog_filters(generation_id: int) -> CatalogFilters:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return await original_filters(generation_id)

        repository.catalog_filters = catalog_filters  # type: ignore[method-assign]
        first_task = asyncio.create_task(catalog.filters())
        await started.wait()
        second_task = asyncio.create_task(catalog.filters())
        await asyncio.sleep(0)
        assert calls == 1

        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert second is first
        assert calls == 1


@pytest.mark.asyncio
async def test_filters_retry_when_generation_changes_during_scan(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "filter-activation.sqlite3") as (catalog, repository):
        await _seed(repository)
        original_filters = repository.catalog_filters
        generation_ids: list[int] = []

        async def activate_then_filter(generation_id: int) -> CatalogFilters:
            generation_ids.append(generation_id)
            filters = await original_filters(generation_id)
            if generation_id == 1:
                await (
                    CatalogState.filter(id=1)
                    .using_db(repository._connection)
                    .update(active_generation_id=2)
                )
            return filters

        repository.catalog_filters = activate_then_filter  # type: ignore[method-assign]

        filters = await catalog.filters()

        assert generation_ids == [1, 2]
        assert [option.value for option in filters.languages] == ["xx"]
        assert [option.value for option in filters.original_formats] == ["txt"]
