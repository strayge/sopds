"""Persistence-backed OPDS catalog query tests."""

from pathlib import Path

import pytest

from sopds.catalog.contracts import (
    CatalogInputError,
    CatalogRequest,
    CatalogStaleCursorError,
    NavigationRequest,
)
from sopds.db.models import Archive, Author, Book, BookAuthor, CatalogState
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
