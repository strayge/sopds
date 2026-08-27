"""Persistence-backed OPDS catalog query tests."""

from pathlib import Path

import pytest

from sopds.catalog.contracts import (
    CatalogInputError,
    CatalogRequest,
    CatalogStaleCursorError,
    NavigationRequest,
)
from sopds.db.models import (
    Archive,
    Author,
    Book,
    BookAuthor,
    CatalogGeneration,
    CatalogState,
    GenerationState,
    Series,
)
from tests.test_catalog import _catalog, _seed


async def test_exact_name_filters_and_navigation_hide_unavailable_and_staged_data(
    tmp_path: Path,
) -> None:
    async with _catalog(tmp_path / "opds-catalog.sqlite3") as (catalog, repository):
        await _seed(repository)

        by_author = await catalog.browse(CatalogRequest(author="First Ёжов"))
        by_series = await catalog.browse(CatalogRequest(series="Ёлки"))
        searched = await catalog.browse(
            CatalogRequest(query="ежик", author="First Ёжов", series="Ёлки")
        )
        assert [book.public_id for book in by_author.books] == ["book-001"]
        assert [book.public_id for book in by_series.books] == ["book-001"]
        assert [book.public_id for book in searched.books] == ["book-001"]

        authors = await catalog.navigation(NavigationRequest("authors"))
        genres = await catalog.navigation(NavigationRequest("genres"))
        series = await catalog.navigation(NavigationRequest("series"))
        languages = await catalog.navigation(NavigationRequest("languages"))
        assert {item.value for item in authors.items} == {"First Ёжов", "Second Author"}
        assert [(item.value, item.label) for item in genres.items] == [("sf", "Science fiction")]
        assert [item.value for item in series.items] == ["Ёлки"]
        assert [item.value for item in languages.items] == ["en", "ru"]
        assert "zz" not in {item.value for item in languages.items}
        assert "xx" not in {item.value for item in languages.items}


async def test_author_and_series_navigation_include_available_book_counts(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "opds-navigation-counts.sqlite3") as (
        catalog,
        repository,
    ):
        await _seed(repository)
        connection = repository._connection
        generation = await CatalogGeneration.filter(id=1).using_db(connection).get()
        archive = await Archive.filter(id=1).using_db(connection).get()
        series = await Series.filter(id=1).using_db(connection).get()
        author = await Author.filter(id=2).using_db(connection).get()
        extra_book = await Book.create(
            using_db=connection,
            id=200,
            generation=generation,
            public_id="extra-book",
            archive=archive,
            member_filename="extra-book.fb2",
            title="Extra book",
            title_sort="extra book",
            series=series,
            size=100,
            original_format="fb2",
        )
        await BookAuthor.create(
            using_db=connection,
            id=200,
            book=extra_book,
            author=author,
            position=0,
        )
        standalone_book = await Book.create(
            using_db=connection,
            id=201,
            generation=generation,
            public_id="standalone-book",
            archive=archive,
            member_filename="standalone-book.fb2",
            title="Standalone book",
            title_sort="standalone book",
            size=100,
            original_format="fb2",
        )
        await BookAuthor.create(
            using_db=connection,
            id=201,
            book=standalone_book,
            author=author,
            position=0,
        )
        other_series_book = await Book.create(
            using_db=connection,
            id=202,
            generation=generation,
            public_id="other-series-book",
            archive=archive,
            member_filename="other-series-book.fb2",
            title="Other series book",
            title_sort="other series book",
            series=series,
            size=100,
            original_format="fb2",
        )
        other_author = await Author.filter(id=1).using_db(connection).get()
        await BookAuthor.create(
            using_db=connection,
            id=202,
            book=other_series_book,
            author=other_author,
            position=0,
        )

        authors = await catalog.navigation(NavigationRequest("authors"))
        series_page = await catalog.navigation(NavigationRequest("series"))
        author_series = await catalog.navigation(NavigationRequest("series", author="First Ёжов"))
        counts = await catalog.author_book_counts("First Ёжов")
        standalone = await catalog.browse(CatalogRequest(author="First Ёжов", without_series=True))

        assert {item.value: item.count for item in authors.items} == {
            "First Ёжов": 3,
            "Second Author": 2,
        }
        assert [(item.value, item.count) for item in series_page.items] == [("Ёлки", 3)]
        assert [(item.value, item.count) for item in author_series.items] == [("Ёлки", 2)]
        assert (counts.series, counts.without_series, counts.total) == (1, 1, 3)
        assert [book.public_id for book in standalone.books] == ["standalone-book"]


async def test_availability_revision_invalidates_browse_and_navigation_cursors(
    tmp_path: Path,
) -> None:
    async with _catalog(tmp_path / "opds-availability.sqlite3") as (catalog, repository):
        await _seed(repository)
        first_books = await catalog.browse(CatalogRequest())
        first_authors = await catalog.navigation(NavigationRequest("authors"))
        before = await repository.active_snapshot()

        await repository.update_archive_availability({1: True})
        assert await repository.active_snapshot() == before

        await repository.update_archive_availability({1: False})
        after = await repository.active_snapshot()
        assert after.generation_id == before.generation_id
        assert after.updated_at > before.updated_at
        assert not (await catalog.browse(CatalogRequest())).books
        assert not (await catalog.navigation(NavigationRequest("authors"))).items

        assert first_books.next_cursor is not None
        with pytest.raises(CatalogStaleCursorError):
            await catalog.browse(CatalogRequest(cursor=first_books.next_cursor))
        if first_authors.next_cursor is not None:
            with pytest.raises(CatalogStaleCursorError):
                await catalog.navigation(NavigationRequest("authors", first_authors.next_cursor))

        await repository.update_archive_availability({1: True})
        archive = await Archive.filter(id=1).using_db(repository._connection).get()
        assert archive.available is True


async def test_navigation_uses_signed_generation_and_kind_bound_keysets(tmp_path: Path) -> None:
    async with _catalog(tmp_path / "opds-navigation.sqlite3") as (catalog, repository):
        await _seed(repository)
        connection = repository._connection
        book = await Book.filter(id=1).using_db(connection).get()
        for index in range(53):
            author = await Author.create(
                using_db=connection,
                id=100 + index,
                generation_id=1,
                name=f"Author {index:03}",
                name_sort=f"author {index:03}",
            )
            await BookAuthor.create(
                using_db=connection,
                id=100 + index,
                book=book,
                author=author,
                position=index,
            )

        first = await catalog.navigation(NavigationRequest("authors"))
        assert len(first.items) == 50
        assert first.next_cursor is not None
        second = await catalog.navigation(NavigationRequest("authors", first.next_cursor))
        assert len(second.items) == 5
        assert {item.value for item in first.items}.isdisjoint(item.value for item in second.items)

        tampered = ("A" if first.next_cursor[0] != "A" else "B") + first.next_cursor[1:]
        with pytest.raises(CatalogInputError):
            await catalog.navigation(NavigationRequest("authors", tampered))
        with pytest.raises(CatalogInputError):
            await catalog.navigation(NavigationRequest("series", first.next_cursor))

        await repository.update_archive_availability({1: False})
        with pytest.raises(CatalogStaleCursorError):
            await catalog.navigation(NavigationRequest("authors", first.next_cursor))

        await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=2)
        with pytest.raises(CatalogStaleCursorError):
            await catalog.navigation(NavigationRequest("authors", first.next_cursor))


async def test_adaptive_navigation_compresses_prefixes_for_authors_series_and_titles(
    tmp_path: Path,
) -> None:
    async with _catalog(tmp_path / "opds-prefixes.sqlite3") as (catalog, repository):
        connection = repository._connection
        generation = await CatalogGeneration.create(
            using_db=connection, id=1, state=GenerationState.ACTIVE
        )
        await (
            CatalogState.filter(id=1)
            .using_db(connection)
            .update(active_generation_id=generation.id)
        )
        archive = await Archive.create(
            using_db=connection,
            id=1,
            generation=generation,
            relative_path="available.zip",
            available=True,
        )
        for index in range(101):
            author = await Author.create(
                using_db=connection,
                id=index + 1,
                generation=generation,
                name=f"Знаток {index:03}",
                name_sort=f"знаток {index:03}",
            )
            series = await Series.create(
                using_db=connection,
                id=index + 1,
                generation=generation,
                name=f"Знак {index:03}",
                name_sort=f"знак {index:03}",
            )
            book = await Book.create(
                using_db=connection,
                id=index + 1,
                generation=generation,
                public_id=f"book-{index:03}",
                archive=archive,
                member_filename=f"book-{index:03}.fb2",
                title=f"Заголовок {index:03}",
                title_sort=f"заголовок {index:03}",
                series=series,
                size=100,
                original_format="fb2",
            )
            await BookAuthor.create(
                using_db=connection,
                id=index + 1,
                book=book,
                author=author,
                position=0,
            )

        authors = await catalog.navigation(NavigationRequest("authors"))
        series_page = await catalog.navigation(NavigationRequest("series"))
        titles = await catalog.navigation(NavigationRequest("titles"))

        assert authors.grouped is True
        assert authors.prefix == "знаток "
        assert [(item.value, item.count) for item in authors.items] == [
            ("знаток 0", 100),
            ("знаток 1", 1),
        ]
        assert series_page.grouped is True
        assert series_page.prefix == "знак "
        assert [(item.value, item.count) for item in series_page.items] == [
            ("знак 0", 100),
            ("знак 1", 1),
        ]
        assert titles.grouped is True
        assert titles.prefix == "заголовок "
        assert [(item.value, item.count) for item in titles.items] == [
            ("заголовок 0", 100),
            ("заголовок 1", 1),
        ]

        author_leaf = await catalog.navigation(NavigationRequest("authors", prefix="знаток 0"))
        title_leaf = await catalog.navigation(NavigationRequest("titles", prefix="заголовок 0"))
        assert author_leaf.grouped is False
        assert len(author_leaf.items) == 50
        assert author_leaf.next_cursor is not None
        assert title_leaf.grouped is False
        assert len(title_leaf.books) == 50
        assert title_leaf.next_cursor is not None

        next_titles = await catalog.navigation(
            NavigationRequest(
                "titles",
                cursor=title_leaf.next_cursor,
                prefix="заголовок 0",
            )
        )
        assert len(next_titles.books) == 50
        assert {book.public_id for book in title_leaf.books}.isdisjoint(
            book.public_id for book in next_titles.books
        )
