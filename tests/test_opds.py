"""Namespace-aware contract tests for the OPDS presentation adapter."""

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sopds.catalog.contracts import (
    AuthorBookCounts,
    BookSummary,
    CatalogFilters,
    CatalogPage,
    CatalogRequest,
    CatalogSnapshot,
    NavigationItem,
    NavigationPage,
    NavigationRequest,
)
from sopds.config import AppConfig
from sopds.conversion.adapters import (
    EpubToAzw3Converter,
    Fb2ToAzw3Converter,
    Fb2ToEpubConverter,
)
from sopds.conversion.registry import ConverterRegistry
from sopds.opds.render import (
    ACQUISITION_REL,
    ACQUISITION_TYPE,
    ATOM,
    DC,
    NAVIGATION_TYPE,
    OPENSEARCH,
    SEARCH_TYPE,
)
from sopds.opds.routes import router

_UPDATED = datetime(2025, 2, 3, 4, 5, 6, 123456, tzinfo=UTC)


class _Catalog:
    def __init__(self) -> None:
        self.requests: list[CatalogRequest] = []
        self.navigation_requests: list[NavigationRequest] = []
        self.group_titles = False

    async def snapshot(self) -> CatalogSnapshot:
        return CatalogSnapshot(7, _UPDATED)

    async def browse(self, request: CatalogRequest) -> CatalogPage:
        self.requests.append(request)
        return CatalogPage(
            (
                BookSummary(
                    public_id="book/one",
                    title="A & <Book>\x01",
                    authors=(),
                    series="Series",
                    series_number="2",
                    language="ru",
                    original_format="fb2",
                    size=123,
                    member_filename="book-one.fb2",
                    genres=(("sf", "Science & fiction"),),
                    published_date=date(2020, 1, 2),
                    libid="lib&1",
                    rating=5,
                    keywords="one, two",
                    updated_at=_UPDATED,
                ),
            ),
            "signed-next" if request.cursor is None else None,
            _UPDATED,
        )

    async def navigation(self, request: NavigationRequest) -> NavigationPage:
        self.navigation_requests.append(request)
        if request.kind == "authors" and request.prefix == "formatted":
            return NavigationPage(
                (NavigationItem("Surname,Given,Middle,", "Surname,Given,Middle,", 12),),
                None,
                _UPDATED,
            )
        if request.kind == "authors" and request.prefix == "з":
            return NavigationPage(
                (NavigationItem("зна", "Зна…", 120),),
                None,
                _UPDATED,
                prefix="з",
                grouped=True,
            )
        if request.kind == "titles" and self.group_titles and not request.prefix:
            return NavigationPage(
                (NavigationItem("a", "A…", 120),),
                None,
                _UPDATED,
                grouped=True,
            )
        if request.kind == "titles":
            return NavigationPage(
                (),
                None,
                _UPDATED,
                books=(
                    BookSummary(
                        public_id="book/one",
                        title="A & <Book>\x01",
                        authors=("Surname,Given,",),
                        series="Series",
                        series_number="2",
                        language="ru",
                        original_format="fb2",
                        size=123,
                        member_filename="book-one.fb2",
                        genres=(("sf", "Science & fiction"),),
                        published_date=date(2020, 1, 2),
                        libid="lib&1",
                        rating=5,
                        keywords="one, two",
                        updated_at=_UPDATED,
                    ),
                ),
            )
        return NavigationPage(
            (NavigationItem("A & B", "A & B"),),
            "navigation-next" if request.cursor is None else None,
            _UPDATED,
        )

    async def details(self, _public_id: str) -> None:
        return None

    async def author_book_counts(self, author: str) -> AuthorBookCounts:
        if author == "Solo,Author,":
            return AuthorBookCounts(0, 4, 4, _UPDATED)
        return AuthorBookCounts(2, 3, 5, _UPDATED)

    async def filters(self) -> CatalogFilters:
        return CatalogFilters((), (), ())


def _config() -> AppConfig:
    root = Path.cwd()
    return AppConfig.model_validate(
        {
            "server": {"base_url": "https://catalog.example/base/"},
            "catalog": {
                "inpx_path": root / "catalog.inpx",
                "archive_root": root,
            },
            "database": {"url": "postgresql://sopds@postgres:5432/sopds"},
            "telegram": {},
            "conversion": {"cache_dir": root / "cache"},
        }
    )


def _app() -> tuple[FastAPI, _Catalog]:
    app = FastAPI()
    catalog = _Catalog()
    app.state.catalog = catalog
    app.state.config = _config()
    app.include_router(router)
    return app, catalog


def test_root_redirect_navigation_kinds_and_configured_relative_urls() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        redirect = client.get("/opds", follow_redirects=False)
        response = client.get("/opds/", headers={"Host": "attacker.invalid"})

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/base/opds/"
    assert response.headers["content-type"] == f"{NAVIGATION_TYPE}; charset=UTF-8"
    root = ET.fromstring(response.content)  # noqa: S314
    links = root.findall("atom:entry/atom:link", {"atom": ATOM})
    assert [link.get("type") for link in links] == [
        NAVIGATION_TYPE,
        NAVIGATION_TYPE,
        NAVIGATION_TYPE,
        NAVIGATION_TYPE,
        NAVIGATION_TYPE,
    ]
    assert links[0].get("href") == "/base/opds/titles/"
    assert [
        entry.findtext("atom:title", namespaces={"atom": ATOM})
        for entry in root.findall("atom:entry", {"atom": ATOM})
    ] == [
        "Books",
        "Authors",
        "Genres",
        "Series",
        "Languages",
    ]
    assert all(
        (link.get("href") or "").startswith("/base/")
        for link in root.findall("atom:link", {"atom": ATOM})
    )
    assert "catalog.example" not in response.text
    assert "attacker.invalid" not in response.text
    assert root.findtext("atom:author/atom:name", namespaces={"atom": ATOM}) == "SOPDS"
    assert root.find("atom:link[@rel='up']", {"atom": ATOM}) is None
    search_link = root.find("atom:link[@rel='search']", {"atom": ATOM})
    assert search_link is not None
    assert search_link.get("type") == SEARCH_TYPE
    assert search_link.get("href") == "/base/opds/books/?q={searchTerms}"
    assert root.findtext("atom:updated", namespaces={"atom": ATOM}) == (
        "2025-02-03T04:05:06.123456+00:00"
    )
    entries = root.findall("atom:entry", {"atom": ATOM})
    assert all(entry.find("atom:content", {"atom": ATOM}) is None for entry in entries)
    assert all(
        (entry.findtext("atom:id", namespaces={"atom": ATOM}) or "").startswith("urn:sopds:")
        for entry in entries
    )


def test_explicit_catalog_redirects_ignore_host_and_preserve_encoded_query() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        for path in ("books", "authors", "genres", "series", "titles", "languages"):
            response = client.get(
                f"/opds/{path}?cursor=a%2Fb%2Bc",
                headers={"Host": "attacker.invalid"},
                follow_redirects=False,
            )
            assert response.status_code == 307
            assert response.headers["location"] == (f"/base/opds/{path}/?cursor=a%2Fb%2Bc")
            assert "attacker.invalid" not in response.headers["location"]


def test_acquisition_feed_has_complete_inline_original_metadata_and_safe_xml() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        response = client.get(
            "/opds/books/?q=term&author=A%20%26%20B&series=S&genre=sf&language=ru&original_format=fb2"
        )

    assert response.headers["content-type"] == f"{ACQUISITION_TYPE}; charset=UTF-8"
    root = ET.fromstring(response.content)  # noqa: S314
    entry = root.find("atom:entry", {"atom": ATOM})
    assert entry is not None
    assert entry.findtext("atom:title", namespaces={"atom": ATOM}) == "A & <Book>�"
    assert root.findtext("atom:author/atom:name", namespaces={"atom": ATOM}) == "SOPDS"
    assert (entry.findtext("atom:id", namespaces={"atom": ATOM}) or "").startswith(
        "urn:sopds:book:"
    )
    assert "book/one" not in (entry.findtext("atom:id", namespaces={"atom": ATOM}) or "")
    assert entry.findtext("atom:author/atom:name", namespaces={"atom": ATOM}) == "Unknown author"
    assert entry.findtext("dc:language", namespaces={"dc": DC}) == "ru"
    assert entry.findtext("dc:issued", namespaces={"dc": DC}) == "2020-01-02"
    assert entry.findtext("dc:identifier", namespaces={"dc": DC}) == "lib&1"
    assert entry.findtext("dc:format", namespaces={"dc": DC}) == ("application/x-fictionbook+xml")
    content_element = entry.find("atom:content", {"atom": ATOM})
    assert content_element is not None
    assert content_element.get("type") == "html"
    content = content_element.text or ""
    assert "<b>Series:</b> Series<br/>" in content
    assert "<b>No in Series:</b> 2<br/>" in content
    assert "<b>File:</b> book-one.fb2<br/>" in content
    assert "<b>File size:</b> 1 KB<br/>" in content
    assert "<b>File date:</b> 2020-01-02<br/>" in content
    assert '<p class="book">Rating: 5<br/>Keywords: one, two</p>' in content
    category = entry.find("atom:category", {"atom": ATOM})
    assert category is not None
    assert category.get("term") == "Science & fiction"
    acquisitions = entry.findall(f"atom:link[@rel='{ACQUISITION_REL}']", {"atom": ATOM})
    assert len(acquisitions) == 1
    assert acquisitions[0].get("type") == "application/x-fictionbook+xml"
    assert acquisitions[0].get("length") == "123"
    assert acquisitions[0].get("href") == ("/base/books/book%2Fone/download")
    assert "convert" not in response.text.casefold()
    request = catalog.requests[0]
    assert request.author == "A & B"
    assert request.series == "S"


def test_opds_acquisitions_follow_registered_source_matrix_without_conversion() -> None:
    app, catalog = _app()
    converter_runner = AsyncMock()
    app.state.converter_registry = ConverterRegistry(
        (
            Fb2ToEpubConverter(runner=converter_runner),
            Fb2ToAzw3Converter(runner=converter_runner),
            EpubToAzw3Converter(runner=converter_runner),
        )
    )
    matrix = (
        ("fb2-book", "fb2", True),
        ("epub-book", "epub", True),
        ("azw3-book", "azw3", True),
        ("pdf-book", "pdf", True),
        ("missed-book", "fb2", False),
    )

    async def browse(request: CatalogRequest) -> CatalogPage:
        catalog.requests.append(request)
        return CatalogPage(
            tuple(
                BookSummary(
                    public_id=public_id,
                    title=public_id,
                    authors=("Author",),
                    series=None,
                    series_number=None,
                    language="en",
                    original_format=source_format,
                    size=10,
                    downloadable=downloadable,
                    updated_at=_UPDATED,
                )
                for public_id, source_format, downloadable in matrix
            ),
            None,
            _UPDATED,
        )

    catalog.browse = browse  # type: ignore[method-assign]
    with TestClient(app) as client:
        response = client.get("/opds/books/")

    root = ET.fromstring(response.content)  # noqa: S314
    entries = root.findall("atom:entry", {"atom": ATOM})
    acquisitions = {
        entry.findtext("atom:title", namespaces={"atom": ATOM}): [
            (link.get("href"), link.get("type"), link.get("length"))
            for link in entry.findall(f"atom:link[@rel='{ACQUISITION_REL}']", {"atom": ATOM})
        ]
        for entry in entries
    }
    assert acquisitions == {
        "fb2-book": [
            ("/base/books/fb2-book/download", "application/x-fictionbook+xml", "10"),
            ("/base/books/fb2-book/download/epub", "application/epub+zip", None),
            (
                "/base/books/fb2-book/download/azw3",
                "application/vnd.amazon.ebook",
                None,
            ),
        ],
        "epub-book": [
            ("/base/books/epub-book/download", "application/epub+zip", "10"),
            (
                "/base/books/epub-book/download/azw3",
                "application/vnd.amazon.ebook",
                None,
            ),
        ],
        "azw3-book": [
            (
                "/base/books/azw3-book/download",
                "application/vnd.amazon.ebook",
                "10",
            )
        ],
        "pdf-book": [("/base/books/pdf-book/download", "application/pdf", "10")],
        "missed-book": [],
    }
    converter_runner.assert_not_awaited()


def test_grouped_navigation_links_to_child_prefix_and_meaningful_parent() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        response = client.get("/opds/authors/", params={"prefix": "з", "parent": "\u0430"})

    root = ET.fromstring(response.content)  # noqa: S314
    entry_link = root.find("atom:entry/atom:link", {"atom": ATOM})
    assert entry_link is not None
    assert entry_link.get("type") == NAVIGATION_TYPE
    entry = root.find("atom:entry", {"atom": ATOM})
    assert entry is not None
    assert entry.findtext("atom:title", namespaces={"atom": ATOM}) == "Зна…"
    assert entry.findtext("atom:content", namespaces={"atom": ATOM}) == "120 authors"
    assert entry_link.get("href") == ("/base/opds/authors/?prefix=%D0%B7%D0%BD%D0%B0&parent=%D0%B7")
    up_link = root.find("atom:link[@rel='up']", {"atom": ATOM})
    assert up_link is not None
    assert up_link.get("href") == "/base/opds/authors/?prefix=%D0%B0"
    assert catalog.navigation_requests == [NavigationRequest("authors", prefix="з")]


def test_author_navigation_formats_inpx_components_without_duplicate_content() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        response = client.get("/opds/authors/", params={"prefix": "formatted"})

    root = ET.fromstring(response.content)  # noqa: S314
    entry = root.find("atom:entry", {"atom": ATOM})
    assert entry is not None
    assert entry.findtext("atom:title", namespaces={"atom": ATOM}) == ("Surname Given Middle")
    assert entry.findtext("atom:content", namespaces={"atom": ATOM}) == "12 books"


def test_root_prefix_group_returns_to_title_navigation_root() -> None:
    app, catalog = _app()
    catalog.group_titles = True
    with TestClient(app) as client:
        grouped = client.get("/opds/titles/")
        leaf = client.get("/opds/titles/", params={"prefix": "a", "parent_root": "1"})

    grouped_root = ET.fromstring(grouped.content)  # noqa: S314
    child_entry = grouped_root.find("atom:entry", {"atom": ATOM})
    assert child_entry is not None
    assert child_entry.findtext("atom:title", namespaces={"atom": ATOM}) == "A…"
    assert child_entry.findtext("atom:content", namespaces={"atom": ATOM}) == "120 books"
    child_link = child_entry.find("atom:link", {"atom": ATOM})
    assert child_link is not None
    assert child_link.get("href") == "/base/opds/titles/?prefix=a&parent_root=1"
    leaf_root = ET.fromstring(leaf.content)  # noqa: S314
    up_link = leaf_root.find("atom:link[@rel='up']", {"atom": ATOM})
    assert up_link is not None
    assert up_link.get("href") == "/base/opds/titles/"


def test_title_navigation_leaf_is_an_acquisition_feed() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        response = client.get("/opds/titles/")

    assert response.headers["content-type"] == f"{ACQUISITION_TYPE}; charset=UTF-8"
    root = ET.fromstring(response.content)  # noqa: S314
    entry = root.find("atom:entry", {"atom": ATOM})
    assert entry is not None
    assert entry.findtext("atom:title", namespaces={"atom": ATOM}) == "A & <Book>�"
    assert entry.findtext("atom:author/atom:name", namespaces={"atom": ATOM}) == ("Surname Given")
    acquisition = entry.find(f"atom:link[@rel='{ACQUISITION_REL}']", {"atom": ATOM})
    assert acquisition is not None
    assert acquisition.get("href") == "/base/books/book%2Fone/download"
    assert catalog.navigation_requests == [NavigationRequest("titles")]


def test_author_catalog_groups_series_and_standalone_books_with_counts() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        response = client.get("/opds/authors/catalog/", params={"author": "Surname,Given,"})

    root = ET.fromstring(response.content)  # noqa: S314
    entries = root.findall("atom:entry", {"atom": ATOM})
    assert [entry.findtext("atom:title", namespaces={"atom": ATOM}) for entry in entries] == [
        "By series",
        "Books without series",
        "All books",
    ]
    assert [entry.findtext("atom:content", namespaces={"atom": ATOM}) for entry in entries] == [
        "2 series",
        "3 books",
        "5 books",
    ]
    links = [entry.find("atom:link", {"atom": ATOM}) for entry in entries]
    assert all(link is not None for link in links)
    assert [link.get("type") for link in links if link is not None] == [
        NAVIGATION_TYPE,
        ACQUISITION_TYPE,
        ACQUISITION_TYPE,
    ]
    assert [link.get("href") for link in links if link is not None] == [
        "/base/opds/series/?author=Surname%2CGiven%2C",
        "/base/opds/books/?author=Surname%2CGiven%2C&without_series=1",
        "/base/opds/books/?author=Surname%2CGiven%2C",
    ]


def test_author_without_series_redirects_directly_to_all_books() -> None:
    app, _ = _app()
    with TestClient(app) as client:
        response = client.get(
            "/opds/authors/catalog/",
            params={"author": "Solo,Author,"},
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == "/base/opds/books/?author=Solo%2CAuthor%2C"


def test_author_series_navigation_keeps_author_filter() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        response = client.get("/opds/series/", params={"author": "A & B"})

    root = ET.fromstring(response.content)  # noqa: S314
    link = root.find("atom:entry/atom:link", {"atom": ATOM})
    assert link is not None
    assert link.get("href") == "/base/opds/books/?series=A+%26+B&author=A+%26+B"
    up = root.find("atom:link[@rel='up']", {"atom": ATOM})
    assert up is not None
    assert up.get("href") == "/base/opds/authors/catalog/?author=A+%26+B"
    assert catalog.navigation_requests == [NavigationRequest("series", author="A & B")]


def test_navigation_destinations_drop_cursor_and_opensearch_keeps_template_literal() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        navigation = client.get("/opds/authors/?cursor=opaque")
        search = client.get("/opds/search.xml")

    root = ET.fromstring(navigation.content)  # noqa: S314
    entry_link = root.find("atom:entry/atom:link", {"atom": ATOM})
    assert entry_link is not None
    assert entry_link.get("href") == ("/base/opds/authors/catalog/?author=A+%26+B")
    self_link = root.find("atom:link[@rel='self']", {"atom": ATOM})
    assert self_link is not None and "cursor=opaque" in (self_link.get("href") or "")
    assert catalog.navigation_requests == [NavigationRequest("authors", "opaque")]

    assert search.headers["content-type"] == (
        "application/opensearchdescription+xml; charset=UTF-8"
    )
    description = ET.fromstring(search.content)  # noqa: S314
    url = description.find("opensearch:Url", {"opensearch": OPENSEARCH})
    assert url is not None
    assert url.get("type") == ACQUISITION_TYPE
    assert url.get("template") == ("/base/opds/books/?q={searchTerms}")
