"""No-network acceptance coverage across import, catalog, OPDS, and acquisition."""

import asyncio
import time
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from sopds.app import create_app
from sopds.config import AppConfig
from sopds.db.migrations_runner import apply_migrations
from sopds.imports.service import derive_public_id

_SEPARATOR = "\x04"


def _write_catalog(config: AppConfig, original: bytes) -> None:
    archive_path = config.catalog.archive_root / "nested" / "books.zip"
    archive_path.parent.mkdir(parents=True)
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("book.fb2", original)

    fields = (
        "Acceptance Author:",
        "sf:",
        "Acceptance Beacon",
        "Acceptance Series",
        "1",
        "book",
        str(len(original)),
        "acceptance-1",
        "0",
        "fb2",
        "2024-01-02",
        "en",
        "5",
        "acceptance,smoke",
    )
    with ZipFile(config.catalog.inpx_path, "w", ZIP_DEFLATED) as inpx:
        inpx.writestr(
            "nested/books.inp",
            (_SEPARATOR.join(fields) + _SEPARATOR).encode() + b"\r\n",
        )


def test_real_application_acceptance_path(app_config: AppConfig) -> None:
    original = b"<FictionBook><body>acceptance original</body></FictionBook>"
    _write_catalog(app_config, original)
    asyncio.run(apply_migrations(app_config.database.path))
    public_id = derive_public_id("default", "nested/books.zip", "book.fb2")

    with TestClient(create_app(app_config)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        deadline = time.monotonic() + 10
        status = client.get("/imports/status")
        while "<dt>State</dt><dd>succeeded</dd>" not in status.text:
            assert time.monotonic() < deadline, status.text
            time.sleep(0.01)
            status = client.get("/imports/status")

        search = client.get("/", params={"q": "Beacon"})
        assert search.status_code == 200
        assert "Acceptance Beacon" in search.text
        assert f"/books/{public_id}" in search.text

        detail = client.get(f"/books/{public_id}")
        assert detail.status_code == 200
        assert "Acceptance Author" in detail.text
        assert "Acceptance Series" in detail.text

        root_feed = client.get("/opds/")
        assert root_feed.status_code == 200
        assert "profile=opds-catalog;kind=navigation" in root_feed.headers["content-type"]
        assert "http://testserver/opds/search.xml" in root_feed.text

        acquisition_feed = client.get("/opds/books/", params={"q": "Beacon"})
        assert acquisition_feed.status_code == 200
        assert "profile=opds-catalog;kind=acquisition" in acquisition_feed.headers["content-type"]
        assert "Acceptance Beacon" in acquisition_feed.text
        assert f"http://testserver/books/{public_id}/download" in acquisition_feed.text

        open_search = client.get("/opds/search.xml")
        assert open_search.status_code == 200
        assert "application/opensearchdescription+xml" in open_search.headers["content-type"]
        assert "http://testserver/opds/books/?q={searchTerms}" in open_search.text

        download = client.get(f"/books/{public_id}/download")
        assert download.status_code == 200
        assert download.content == original
        assert download.headers["content-type"] == "application/x-fictionbook+xml"
        assert download.headers["content-length"] == str(len(original))
        assert 'filename="Acceptance Beacon.fb2"' in download.headers["content-disposition"]
        assert download.headers["x-content-type-options"] == "nosniff"
