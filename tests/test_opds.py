"""Namespace-aware contract tests for the OPDS presentation adapter."""

from datetime import UTC, date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sopds.catalog.contracts import (
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
from sopds.opds.render import (
    ACQUISITION_REL,
    ACQUISITION_TYPE,
    ATOM,
    DC,
    NAVIGATION_TYPE,
    OPENSEARCH,
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
        if request.kind == "authors" and request.prefix == "з":
            return NavigationPage(
                (NavigationItem("зна", "Зна… (120)", 120),),
                None,
                _UPDATED,
                prefix="з",
                grouped=True,
            )
        if request.kind == "titles" and self.group_titles and not request.prefix:
            return NavigationPage(
                (NavigationItem("a", "A… (120)", 120),),
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
                        authors=(),
                        series="Series",
                        series_number="2",
                        language="ru",
                        original_format="fb2",
                        size=123,
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
            "database": {"path": root / "catalog.sqlite3"},
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
    assert root.findtext("atom:updated", namespaces={"atom": ATOM}) == (
        "2025-02-03T04:05:06.123456Z"
    )
    entries = root.findall("atom:entry", {"atom": ATOM})
    assert all(entry.findtext("atom:content", namespaces={"atom": ATOM}) for entry in entries)
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
    content = entry.findtext("atom:content", namespaces={"atom": ATOM}) or ""
    assert "Series: Series #2" in content
    assert "Size: 123 bytes" in content
    assert "Rating: 5" in content
    assert "Keywords: one, two" in content
    acquisitions = entry.findall(f"atom:link[@rel='{ACQUISITION_REL}']", {"atom": ATOM})
    assert len(acquisitions) == 1
    assert acquisitions[0].get("type") == "application/x-fictionbook+xml"
    assert acquisitions[0].get("length") == "123"
    assert acquisitions[0].get("href") == ("/base/books/book%2Fone/download")
    assert "convert" not in response.text.casefold()
    request = catalog.requests[0]
    assert request.author == "A & B"
    assert request.series == "S"


def test_grouped_navigation_links_to_child_prefix_and_meaningful_parent() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        response = client.get("/opds/authors/", params={"prefix": "з", "parent": "\u0430"})

    root = ET.fromstring(response.content)  # noqa: S314
    entry_link = root.find("atom:entry/atom:link", {"atom": ATOM})
    assert entry_link is not None
    assert entry_link.get("type") == NAVIGATION_TYPE
    assert entry_link.get("href") == ("/base/opds/authors/?prefix=%D0%B7%D0%BD%D0%B0&parent=%D0%B7")
    up_link = root.find("atom:link[@rel='up']", {"atom": ATOM})
    assert up_link is not None
    assert up_link.get("href") == "/base/opds/authors/?prefix=%D0%B0"
    assert catalog.navigation_requests == [NavigationRequest("authors", prefix="з")]


def test_root_prefix_group_returns_to_title_navigation_root() -> None:
    app, catalog = _app()
    catalog.group_titles = True
    with TestClient(app) as client:
        grouped = client.get("/opds/titles/")
        leaf = client.get("/opds/titles/", params={"prefix": "a", "parent_root": "1"})

    grouped_root = ET.fromstring(grouped.content)  # noqa: S314
    child_link = grouped_root.find("atom:entry/atom:link", {"atom": ATOM})
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
    acquisition = entry.find(f"atom:link[@rel='{ACQUISITION_REL}']", {"atom": ATOM})
    assert acquisition is not None
    assert acquisition.get("href") == "/base/books/book%2Fone/download"
    assert catalog.navigation_requests == [NavigationRequest("titles")]


def test_navigation_destinations_drop_cursor_and_opensearch_keeps_template_literal() -> None:
    app, catalog = _app()
    with TestClient(app) as client:
        navigation = client.get("/opds/authors/?cursor=opaque")
        search = client.get("/opds/search.xml")

    root = ET.fromstring(navigation.content)  # noqa: S314
    entry_link = root.find("atom:entry/atom:link", {"atom": ATOM})
    assert entry_link is not None
    assert entry_link.get("href") == ("/base/opds/books/?author=A+%26+B")
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
