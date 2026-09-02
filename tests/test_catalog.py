"""Catalog normalization, visibility, filtering, and keyset query tests."""

import asyncio
import base64
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sopds.catalog.contracts import (
    BookAvailability,
    CatalogBook,
    CatalogFilters,
    CatalogInputError,
    CatalogRequest,
    CatalogStaleCursorError,
    SearchField,
)
from sopds.catalog.search import normalize_search_text, normalize_text, query_tokens
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
    ImportRun,
    Series,
)
from sopds.db.repository import CatalogRepository
from sopds.imports.status import ImportState, ImportTrigger
from tests.conftest import isolated_database_config, reset_test_database


@asynccontextmanager
async def _catalog() -> AsyncIterator[tuple[CatalogService, CatalogRepository]]:
    database = isolated_database_config()
    await reset_test_database(database)
    await apply_migrations(database)
    context = await initialize_database(database)
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
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
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
    deleted = await Book.create(
        using_db=connection,
        id=102,
        generation=active,
        public_id="deleted",
        archive=available,
        member_filename="deleted.fb2",
        title="Deleted unique",
        title_sort="deleted unique",
        size=1,
        language="yy",
        original_format="azw3",
        hidden=True,
    )
    hidden_unavailable = await Book.create(
        using_db=connection,
        id=104,
        generation=active,
        public_id="hidden-unavailable",
        archive=unavailable,
        member_filename="hidden-unavailable.fb2",
        title="Hidden unavailable",
        title_sort="hidden unavailable",
        size=1,
        language="zz",
        original_format="fb2",
        hidden=True,
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
    for book, generation_id in (
        (hidden, active.id),
        (deleted, active.id),
        (hidden_unavailable, active.id),
        (staged, staging.id),
    ):
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            [book.id, generation_id, normalize_text(book.title), "", "", "", book.language],
        )
    await repository.materialize_generation_summaries(active.id)
    await repository.materialize_generation_summaries(staging.id)


async def _seed_search_window(
    repository: CatalogRepository, count: int
) -> tuple[list[tuple[int, str, str]], CatalogGeneration, Archive]:
    connection = repository._connection
    active = await CatalogGeneration.create(using_db=connection, id=1, state=GenerationState.ACTIVE)
    await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=active.id)
    archive = await Archive.create(
        using_db=connection,
        id=1,
        generation=active,
        relative_path="window.zip",
        available=True,
    )
    entries = [
        (index + 1, f"matching title {index // 2:04}", f"window-{count - index:04}")
        for index in range(count)
    ]
    await Book.bulk_create(
        [
            Book(
                id=book_id,
                generation=active,
                public_id=public_id,
                archive=archive,
                member_filename=f"{public_id}.fb2",
                title=title_sort,
                title_sort=title_sort,
                size=1,
                original_format="fb2",
            )
            for book_id, title_sort, public_id in reversed(entries)
        ],
        batch_size=500,
        using_db=connection,
    )
    await connection.execute_many(
        "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [
            [book_id, active.id, title_sort, "", "", "", ""]
            for book_id, title_sort, _public_id in reversed(entries)
        ],
    )
    return sorted(entries, key=lambda row: (row[1], row[2])), active, archive


async def test_catalog_statistics_describe_active_generation_and_database(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
        empty = await catalog.statistics()
        assert empty.total_books == 0
        assert empty.hidden_books == 0
        assert empty.missed_books == 0
        assert empty.active_books == 0
        assert empty.generation_activated_at is None
        assert empty.database_size_bytes > 0

        await _seed(repository)
        activated_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
        await (
            CatalogGeneration.filter(id=1)
            .using_db(repository._connection)
            .update(activated_at=activated_at)
        )
        await ImportRun.create(
            using_db=repository._connection,
            trigger=ImportTrigger.MANUAL,
            state=ImportState.SUCCEEDED,
            finished_at=activated_at,
            records_read=65,
            records_imported=56,
            records_deleted=9,
            staging_generation_id=1,
        )

        statistics = await catalog.statistics()
        assert statistics.total_books == 65
        assert statistics.hidden_books == 9
        assert statistics.missed_books == 1
        assert statistics.active_books == 55
        assert statistics.generation_activated_at == activated_at
        assert statistics.database_size_bytes >= empty.database_size_bytes

        await repository.vacuum()
        assert (await catalog.statistics()).database_size_bytes > 0


async def test_summary_reads_follow_archive_availability_without_rematerializing(
    tmp_path: Path,
) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)
        assert [option.value for option in (await catalog.filters()).languages] == ["en", "ru"]
        assert (await catalog.statistics()).missed_books == 1

        await repository.update_archive_availability({1: False})

        assert (await catalog.filters()).languages == ()
        statistics = await catalog.statistics()
        assert statistics.missed_books == 56
        assert statistics.active_books == 0

        await repository.update_archive_availability({1: True})
        assert [option.value for option in (await catalog.filters()).languages] == ["en", "ru"]
        assert (await catalog.statistics()).missed_books == 1


async def test_acquisition_targets_are_one_active_available_snapshot(tmp_path: Path) -> None:
    async with _catalog() as (_catalog_service, repository):
        await _seed(repository)
        connection = repository._connection
        superseded = await CatalogGeneration.create(
            using_db=connection, id=3, state=GenerationState.SUPERSEDED
        )
        old_archive = await Archive.create(
            using_db=connection,
            id=4,
            generation=superseded,
            relative_path="old.zip",
            available=True,
        )
        await Book.create(
            using_db=connection,
            id=103,
            generation=superseded,
            public_id="old",
            archive=old_archive,
            member_filename="old.fb2",
            title="Old",
            title_sort="old",
            size=9,
            original_format="fb2",
        )

        targets = await repository.acquisition_targets(["book-001"])
        expected_targets = await repository.acquisition_targets(
            ["book-001"], expected_generation_id=1
        )
        assert targets["book-001"] == expected_targets["book-001"]
        assert await repository.acquisition_targets(["book-001"], expected_generation_id=2) == {}
        target = targets["book-001"]
        assert target.generation_id == 1
        assert target.archive_relative_path == "available.zip"
        assert target.member_filename == "book-001.fb2"
        assert target.expected_size == 101
        assert await repository.acquisition_targets(["hidden", "staged", "old", "missing"]) == {}

        await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=2)
        await CatalogGeneration.filter(id=1).using_db(connection).delete()
        assert target.generation_id == 1
        assert target.title == "Ёжик"
        activated_targets = await repository.acquisition_targets(["staged"])
        activated = activated_targets["staged"]
        assert activated.generation_id == 2
        assert await repository.acquisition_targets(["staged"], expected_generation_id=2) == {
            "staged": activated
        }
        assert await repository.acquisition_targets(["staged"], expected_generation_id=1) == {}
        assert await repository.acquisition_targets(["book-001"], expected_generation_id=1) == {}


async def test_acquisition_targets_are_bounded_and_exclude_unavailable_books(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _catalog() as (_catalog_service, repository):
        await _seed(repository)
        monkeypatch.setattr("sopds.db.repository.PUBLIC_ID_LOOKUP_BATCH_SIZE", 2)

        targets = await repository.acquisition_targets(
            ["book-000", "book-001", "book-002", "book-001", "hidden", "missing"]
        )

        assert set(targets) == {"book-000", "book-001", "book-002"}
        assert targets["book-001"].generation_id == 1
        assert targets["book-001"].archive_relative_path == "available.zip"
        assert targets["book-001"].member_filename == "book-001.fb2"


async def test_bulk_summaries_include_all_current_records_in_input_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)
        monkeypatch.setattr("sopds.db.repository.PUBLIC_ID_LOOKUP_BATCH_SIZE", 2)

        batch = await catalog.bulk_summaries(
            [
                "deleted",
                "unknown",
                "hidden",
                "book-001",
                "hidden-unavailable",
            ]
        )

        assert batch.generation_id == 1
        assert [book.public_id for book in batch.books] == [
            "deleted",
            "hidden",
            "book-001",
            "hidden-unavailable",
        ]
        assert [book.availability for book in batch.books] == [
            BookAvailability.HIDDEN,
            BookAvailability.MISSED,
            BookAvailability.ACTIVE,
            BookAvailability.HIDDEN,
        ]
        assert [book.downloadable for book in batch.books] == [True, False, True, False]


async def test_bulk_summaries_retry_one_activation_change(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)
        original_lookup = repository.summaries_by_public_ids
        generation_ids: list[int] = []

        async def activate_then_lookup(
            generation_id: int,
            public_ids: Sequence[str],
        ) -> list[CatalogBook]:
            generation_ids.append(generation_id)
            books = await original_lookup(generation_id, public_ids)
            if generation_id == 1:
                await (
                    CatalogState.filter(id=1)
                    .using_db(repository._connection)
                    .update(active_generation_id=2)
                )
            return books

        repository.summaries_by_public_ids = activate_then_lookup  # type: ignore[method-assign]

        batch = await catalog.bulk_summaries(["book-001", "staged"])

        assert generation_ids == [1, 2]
        assert batch.generation_id == 2
        assert [book.public_id for book in batch.books] == ["staged"]


async def test_bulk_summaries_reject_repeated_activation_changes(tmp_path: Path) -> None:
    async with _catalog() as (
        catalog,
        repository,
    ):
        await _seed(repository)
        original_lookup = repository.summaries_by_public_ids
        generation_ids: list[int] = []

        async def change_activation_after_lookup(
            generation_id: int,
            public_ids: Sequence[str],
        ) -> list[CatalogBook]:
            generation_ids.append(generation_id)
            books = await original_lookup(generation_id, public_ids)
            await (
                CatalogState.filter(id=1)
                .using_db(repository._connection)
                .update(active_generation_id=2 if generation_id == 1 else 1)
            )
            return books

        repository.summaries_by_public_ids = (  # type: ignore[method-assign]
            change_activation_after_lookup
        )

        with pytest.raises(CatalogInputError, match="Catalog changed while loading"):
            await catalog.bulk_summaries(["book-001", "staged"])

        assert generation_ids == [1, 2]


@pytest.mark.parametrize("public_id", ["", "x" * 65, "bad\x00id"])
async def test_bulk_summaries_reject_invalid_public_ids(
    tmp_path: Path,
    public_id: str,
) -> None:
    async with _catalog() as (catalog, _repository):
        with pytest.raises(CatalogInputError, match="Invalid public book ID"):
            await catalog.bulk_summaries([public_id])


def test_normalization_and_safe_search_terms() -> None:
    assert normalize_text("  ЁЖИК \uff21  ") == "  ежик a  "
    assert normalize_search_text("Café ЁЖИК") == "cafe ежик"
    assert query_tokens("Ёжик, BOOK_2!") == ("ежик", "book", "2")
    assert query_tokens('title:"x" OR *; --') == ("title", "x", "or")
    assert query_tokens("..._") == ()
    with pytest.raises(CatalogInputError):
        query_tokens("x" * 201)
    with pytest.raises(CatalogInputError):
        query_tokens(" ".join(f"w{index}" for index in range(17)))


async def test_catalog_visibility_search_filters_details_and_keyset(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)

        first = await catalog.browse(CatalogRequest())
        assert len(first.books) == 50
        assert first.next_cursor is not None
        largest_page = await catalog.browse(CatalogRequest(page_size=1_000))
        assert len(largest_page.books) == 55
        assert largest_page.next_cursor is None
        with pytest.raises(CatalogInputError, match="Invalid catalog page size"):
            await catalog.browse(CatalogRequest(page_size=1_001))
        ten = await catalog.browse(CatalogRequest(page_size=10))
        assert len(ten.books) == 10
        assert ten.next_cursor is not None
        with pytest.raises(CatalogInputError, match="does not match"):
            await catalog.browse(CatalogRequest(cursor=ten.next_cursor, page_size=50))
        second_ten = await catalog.browse(CatalogRequest(cursor=ten.next_cursor, page_size=10))
        assert len(second_ten.books) == 10
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
        assert [
            book.public_id
            for book in (
                await catalog.browse(CatalogRequest(query="ежик", search_field=SearchField.TITLE))
            ).books
        ] == ["book-001"]
        assert not (
            await catalog.browse(CatalogRequest(query="first", search_field=SearchField.TITLE))
        ).books
        assert [
            book.public_id
            for book in (
                await catalog.browse(CatalogRequest(query="first", search_field=SearchField.AUTHOR))
            ).books
        ] == ["book-001"]
        assert [
            book.public_id
            for book in (
                await catalog.browse(CatalogRequest(query="елки", search_field=SearchField.SERIES))
            ).books
        ] == ["book-001"]
        assert not (await catalog.browse(CatalogRequest(query="еж"))).books
        assert [
            book.public_id
            for book in (await catalog.browse(CatalogRequest(query="first елки"))).books
        ] == ["book-001"]
        assert not (await catalog.browse(CatalogRequest(query="first missing"))).books
        combined_requests = (
            CatalogRequest(query="ежик", language="ru"),
            CatalogRequest(query="ежик", genre="sf"),
            CatalogRequest(query="ежик", original_format="fb2"),
            CatalogRequest(query="ежик", author="First Ёжов"),
            CatalogRequest(query="ежик", series="Ёлки"),
        )
        for request in combined_requests:
            assert [book.public_id for book in (await catalog.browse(request)).books] == [
                "book-001"
            ]
        assert [
            book.public_id
            for book in (
                await catalog.browse(CatalogRequest(query="book 000", without_series=True))
            ).books
        ] == ["book-000"]
        assert not (await catalog.browse(CatalogRequest(query="hidden"))).books
        assert not (await catalog.browse(CatalogRequest(query="deleted"))).books
        assert not (await catalog.browse(CatalogRequest(query="staged"))).books

        missed = await catalog.browse(CatalogRequest(query="hidden", include_missed=True))
        assert [book.public_id for book in missed.books] == ["hidden"]
        assert missed.books[0].availability is BookAvailability.MISSED
        assert missed.books[0].downloadable is False
        deleted = await catalog.browse(CatalogRequest(query="deleted", include_hidden=True))
        assert [book.public_id for book in deleted.books] == ["deleted"]
        assert deleted.books[0].availability is BookAvailability.HIDDEN
        assert deleted.books[0].downloadable is True

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
        assert await catalog.details("deleted") is None
        missed_detail = await catalog.details("hidden", include_missed=True)
        assert missed_detail is not None
        assert missed_detail.availability is BookAvailability.MISSED
        assert missed_detail.downloadable is False
        hidden_detail = await catalog.details("deleted", include_hidden=True)
        assert hidden_detail is not None
        assert hidden_detail.availability is BookAvailability.HIDDEN
        assert hidden_detail.downloadable is True
        hidden_unavailable_detail = await catalog.details("hidden-unavailable", include_hidden=True)
        assert hidden_unavailable_detail is not None
        assert hidden_unavailable_detail.availability is BookAvailability.HIDDEN
        assert hidden_unavailable_detail.downloadable is False
        assert await catalog.details("staged", include_missed=True, include_hidden=True) is None

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


async def test_search_window_caps_and_reports_keyset_overflow(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
        expected, active, archive = await _seed_search_window(repository, 1_000)
        connection = repository._connection
        original_summaries = repository.summaries
        hydrated_batches: list[list[int]] = []

        async def tracked_summaries(
            generation_id: int,
            book_ids: list[int],
            *,
            include_missed: bool = False,
            include_hidden: bool = False,
        ) -> list[CatalogBook]:
            hydrated_batches.append(book_ids)
            return await original_summaries(
                generation_id,
                book_ids,
                include_missed=include_missed,
                include_hidden=include_hidden,
            )

        repository.summaries = tracked_summaries  # type: ignore[method-assign]
        complete = await catalog.browse(CatalogRequest(query="matching", page_size=1_000))

        expected_public_ids = [public_id for _book_id, _title_sort, public_id in expected]
        assert [book.public_id for book in complete.books] == expected_public_ids
        assert len(set(expected_public_ids)) == 1_000
        assert complete.next_cursor is None

        overflow = await Book.create(
            using_db=connection,
            id=1_001,
            generation=active,
            public_id="window-overflow",
            archive=archive,
            member_filename="window-overflow.fb2",
            title="Matching title 9999",
            title_sort="matching title 9999",
            size=1,
            original_format="fb2",
        )
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            [overflow.id, active.id, overflow.title_sort, "", "", "", ""],
        )

        truncated = await catalog.browse(CatalogRequest(query="matching", page_size=1_000))

        assert [book.public_id for book in truncated.books] == expected_public_ids
        assert truncated.next_cursor is not None
        remainder = await catalog.browse(
            CatalogRequest(query="matching", page_size=1_000, cursor=truncated.next_cursor)
        )
        assert [book.public_id for book in remainder.books] == ["window-overflow"]
        assert remainder.next_cursor is None
        assert [len(batch) for batch in hydrated_batches] == [1_000, 1_000, 1]
        assert all(len(set(batch)) == len(batch) for batch in hydrated_batches)


@pytest.mark.parametrize("change", ["availability", "cleanup"])
async def test_hydration_omits_books_that_stop_being_visible(tmp_path: Path, change: str) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)
        original_summaries = repository.summaries
        changed = False

        async def summaries(
            generation_id: int,
            book_ids: list[int],
            *,
            include_missed: bool = False,
            include_hidden: bool = False,
        ) -> list[CatalogBook]:
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
            return await original_summaries(
                generation_id,
                book_ids,
                include_missed=include_missed,
                include_hidden=include_hidden,
            )

        repository.summaries = summaries  # type: ignore[method-assign]
        page = await catalog.browse(CatalogRequest())

        expected_count = 0 if change == "availability" else 49
        assert len(page.books) == expected_count
        assert all(book.public_id != "book-000" for book in page.books)


async def test_browse_retries_activation_change_but_cursor_becomes_stale(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)
        first_page = await catalog.browse(CatalogRequest())
        assert first_page.next_cursor is not None
        original_summaries = repository.summaries
        activated = False

        async def activate_then_hydrate(
            generation_id: int,
            book_ids: list[int],
            *,
            include_missed: bool = False,
            include_hidden: bool = False,
        ) -> list[CatalogBook]:
            nonlocal activated
            if not activated:
                activated = True
                await (
                    CatalogState.filter(id=1)
                    .using_db(repository._connection)
                    .update(active_generation_id=2)
                )
            return await original_summaries(
                generation_id,
                book_ids,
                include_missed=include_missed,
                include_hidden=include_hidden,
            )

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


@pytest.mark.parametrize(
    ("public_id", "expected_title"),
    [("staged", "Staged unique"), ("book-001", None)],
)
async def test_details_retries_activation_change_during_hydration(
    tmp_path: Path, public_id: str, expected_title: str | None
) -> None:
    async with _catalog() as (catalog, repository):
        await _seed(repository)
        original_detail = repository.detail
        generation_ids: list[int] = []

        async def activate_then_return_detail(
            generation_id: int,
            public_id: str,
            *,
            include_missed: bool = False,
            include_hidden: bool = False,
        ) -> CatalogBook | None:
            generation_ids.append(generation_id)
            detail = await original_detail(
                generation_id,
                public_id,
                include_missed=include_missed,
                include_hidden=include_hidden,
            )
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


async def test_details_rejects_two_activation_changes(tmp_path: Path) -> None:
    async with _catalog() as (
        catalog,
        repository,
    ):
        await _seed(repository)
        original_detail = repository.detail
        generation_ids: list[int] = []

        async def change_activation_after_detail(
            generation_id: int,
            public_id: str,
            *,
            include_missed: bool = False,
            include_hidden: bool = False,
        ) -> CatalogBook | None:
            generation_ids.append(generation_id)
            detail = await original_detail(
                generation_id,
                public_id,
                include_missed=include_missed,
                include_hidden=include_hidden,
            )
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


async def test_filters_cache_avoids_repeated_repository_scan(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
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


async def test_concurrent_filter_cache_misses_are_single_flight(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
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


async def test_filters_retry_when_generation_changes_during_scan(tmp_path: Path) -> None:
    async with _catalog() as (catalog, repository):
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
