"""No-network acceptance coverage across import, catalog, OPDS, and acquisition."""

import asyncio
import json
import re
import time
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from sopds.app import create_app
from sopds.config import AppConfig
from sopds.db.migrations_runner import apply_migrations
from sopds.imports.service import derive_public_id
from sopds.web.csrf import issue_csrf_token

_SEPARATOR = "\x04"


def _inpx_record(
    *,
    authors: str,
    title: str,
    series: str,
    series_number: str,
    filename: str,
    size: int,
    library_id: str,
    extension: str = "fb2",
) -> bytes:
    fields = (
        authors,
        "sf:",
        title,
        series,
        series_number,
        filename,
        str(size),
        library_id,
        "0",
        extension,
        "2024-01-02",
        "en",
        "5",
        "acceptance,smoke",
    )
    return (_SEPARATOR.join(fields) + _SEPARATOR).encode() + b"\r\n"


def _write_catalog(config: AppConfig, original: bytes) -> None:
    archive_path = config.catalog.archive_root / "nested" / "books.zip"
    archive_path.parent.mkdir(parents=True)
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("book.fb2", original)

    with ZipFile(config.catalog.inpx_path, "w", ZIP_DEFLATED) as inpx:
        inpx.writestr(
            "nested/books.inp",
            _inpx_record(
                authors="Acceptance Author:",
                title="Acceptance Beacon",
                series="Acceptance Series",
                series_number="1",
                filename="book",
                size=len(original),
                library_id="acceptance-1",
            ),
        )


def _write_selected_catalog(config: AppConfig) -> dict[str, bytes]:
    originals = {
        "series.fb2": b"<book>series original</book>",
        "standalone.epub": b"standalone original bytes\x00\xff",
        "slash.fb2": b"slash title original",
        "backslash.fb2": b"backslash title original",
    }
    archive_path = config.catalog.archive_root / "nested" / "books.zip"
    archive_path.parent.mkdir(parents=True)
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for member, original in originals.items():
            archive.writestr(member, original)

    available_records = (
        _inpx_record(
            authors="Primary,Author,:Ignored,Coauthor,:",
            title="Opening Tide",
            series="Harbor Cycle",
            series_number="1",
            filename="series",
            size=len(originals["series.fb2"]),
            library_id="selected-series",
        ),
        _inpx_record(
            authors="Solo,Reader,:",
            title="Standalone",
            series="",
            series_number="",
            filename="standalone",
            size=len(originals["standalone.epub"]),
            library_id="selected-standalone",
            extension="epub",
        ),
        _inpx_record(
            authors="Collision,Author,:",
            title="A/B",
            series="",
            series_number="",
            filename="slash",
            size=len(originals["slash.fb2"]),
            library_id="selected-slash",
        ),
        _inpx_record(
            authors="Collision,Author,:",
            title="A\\B",
            series="",
            series_number="",
            filename="backslash",
            size=len(originals["backslash.fb2"]),
            library_id="selected-backslash",
        ),
    )
    missed_record = _inpx_record(
        authors="Missing,Source,:",
        title="Unavailable Original",
        series="Lost Shelf",
        series_number="12",
        filename="gone",
        size=123,
        library_id="selected-missed",
    )
    with ZipFile(config.catalog.inpx_path, "w", ZIP_DEFLATED) as inpx:
        inpx.writestr("nested/books.inp", b"".join(available_records))
        inpx.writestr("missing/lost.inp", missed_record)
    return originals


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
        payload_match = re.search(
            r'<script id="catalog-result-payload" type="application/json" '
            r"data-catalog-payload>(.*?)</script>",
            search.text,
            re.S,
        )
        assert payload_match is not None
        payload = json.loads(payload_match.group(1))
        assert payload["truncated"] is False
        assert len(payload["books"]) == 1
        book = payload["books"][0]
        assert book["title"] == "Acceptance Beacon"
        assert book["detailUrl"] == f"/books/{public_id}"
        assert book["readUrl"] == f"/books/{public_id}/read"
        assert book["originalDownload"]["url"] == f"/books/{public_id}/download"
        assert "return_to" not in search.text

        detail = client.get(book["detailUrl"])
        assert detail.status_code == 200
        assert "Acceptance Author" in detail.text
        assert "Acceptance Series" in detail.text
        assert "Back to results" in detail.text
        assert 'data-testid="detail-back-link" href="/" data-detail-back' in detail.text
        assert (
            f'class="book-detail__download-action" '
            f'href="/books/{public_id}/download"' in detail.text
        )

        management = client.get("/manage")
        assert management.status_code == 200
        assert "Current generation added" in management.text
        assert 'class="local-datetime" datetime="' in management.text
        assert "<dt>State</dt><dd>succeeded</dd>" in management.text

        root_feed = client.get("/opds/")
        assert root_feed.status_code == 200
        assert "profile=opds-catalog;kind=navigation" in root_feed.headers["content-type"]
        assert 'href="/opds/books/?q={searchTerms}"' in root_feed.text
        assert 'href="/opds/titles/"' in root_feed.text

        title_feed = client.get("/opds/titles/")
        assert title_feed.status_code == 200
        assert "profile=opds-catalog;kind=acquisition" in title_feed.headers["content-type"]
        assert "Acceptance Beacon" in title_feed.text

        acquisition_feed = client.get("/opds/books/", params={"q": "Beacon"})
        assert acquisition_feed.status_code == 200
        assert "profile=opds-catalog;kind=acquisition" in acquisition_feed.headers["content-type"]
        assert "Acceptance Beacon" in acquisition_feed.text
        assert f'href="/books/{public_id}/download"' in acquisition_feed.text

        open_search = client.get("/opds/search.xml")
        assert open_search.status_code == 200
        assert "application/opensearchdescription+xml" in open_search.headers["content-type"]
        assert 'template="/opds/books/?q={searchTerms}"' in open_search.text

        download = client.get(f"/books/{public_id}/download")
        assert download.status_code == 200
        assert download.content == original
        assert download.headers["content-type"] == "application/x-fictionbook+xml"
        assert download.headers["content-length"] == str(len(original))
        assert 'filename="Acceptance Beacon.fb2"' in download.headers["content-disposition"]
        assert download.headers["x-content-type-options"] == "nosniff"


def test_selected_preview_and_zip_download_use_real_catalog_stack(
    app_config: AppConfig,
) -> None:
    originals = _write_selected_catalog(app_config)
    asyncio.run(apply_migrations(app_config.database.path))
    public_ids = {
        member: derive_public_id("default", "nested/books.zip", member) for member in originals
    }
    missed_id = derive_public_id("default", "missing/lost.zip", "gone.fb2")
    unknown_id = "unknown-selected-book"
    collision_ids = sorted((public_ids["slash.fb2"], public_ids["backslash.fb2"]), reverse=True)
    selected_ids = [
        public_ids["series.fb2"],
        public_ids["standalone.epub"],
        *collision_ids,
        missed_id,
        unknown_id,
    ]
    eligible_size = sum(len(original) for original in originals.values())

    app = create_app(app_config)
    with TestClient(app) as client:
        csrf_token = issue_csrf_token(app.state.csrf_key)
        deadline = time.monotonic() + 10
        status = client.get("/imports/status")
        while "<dt>State</dt><dd>succeeded</dd>" not in status.text:
            assert time.monotonic() < deadline, status.text
            time.sleep(0.01)
            status = client.get("/imports/status")

        preview = client.post(
            "/selected/preview",
            json={"ids": selected_ids, "preset": "nested"},
        )
        assert preview.status_code == 200
        assert 'data-selected-count="6"' in preview.text
        assert 'data-downloadable-count="4"' in preview.text
        assert f'data-total-size="{eligible_size}"' in preview.text
        assert preview.text.count('data-status="downloadable"') == 4
        assert preview.text.count('data-status="unavailable"') == 1
        assert preview.text.count('data-status="unknown"') == 1
        assert preview.text.count('data-collision="true"') == 2
        assert "Opening Tide" in preview.text
        assert "Primary Author" in preview.text
        assert "Ignored Coauthor" in preview.text
        assert "Unavailable Original" in preview.text
        assert "Missed" in preview.text
        assert "Unknown selection" in preview.text
        assert unknown_id in preview.text
        assert "1 unavailable book is excluded from the ZIP." in preview.text
        assert "1 unknown selection is excluded from the ZIP." in preview.text
        assert "Archive name collisions affect 2 books" in preview.text
        assert preview.text.count("Archive name conflicts; ZIP names will be made unique.") == 2
        for internal_path in (
            "Primary Author/Harbor Cycle/01 - Opening Tide.fb2",
            "Solo Reader/Standalone.epub",
            "Collision Author/A_B.fb2",
            "Collision Author/A_B (2).fb2",
        ):
            assert internal_path not in preview.text

        collision_sources = {
            public_ids["slash.fb2"]: originals["slash.fb2"],
            public_ids["backslash.fb2"]: originals["backslash.fb2"],
        }
        for preset, series_path, standalone_path, collision_prefix in (
            (
                "nested",
                "Primary Author/Harbor Cycle/01 - Opening Tide.fb2",
                "Solo Reader/Standalone.epub",
                "Collision Author/A_B",
            ),
            (
                "flatten",
                "Primary Author/Harbor Cycle 01 - Opening Tide.fb2",
                "Solo Reader/Standalone.epub",
                "Collision Author/A_B",
            ),
            (
                "list",
                "Primary Author. Harbor Cycle 01 - Opening Tide.fb2",
                "Solo Reader. Standalone.epub",
                "Collision Author. A_B",
            ),
        ):
            expected = {
                series_path: originals["series.fb2"],
                standalone_path: originals["standalone.epub"],
            }
            for index, public_id in enumerate(sorted(collision_sources)):
                suffix = "" if index == 0 else " (2)"
                expected[f"{collision_prefix}{suffix}.fb2"] = collision_sources[public_id]

            response = client.post(
                "/selected/download",
                data={
                    "ids": json.dumps(selected_ids),
                    "preset": preset,
                    "csrf_token": csrf_token,
                },
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/zip"
            assert response.headers["content-length"] == str(len(response.content))
            assert response.headers["x-content-type-options"] == "nosniff"
            assert 'filename="selected-books.zip"' in response.headers["content-disposition"]
            assert "filename*=UTF-8''selected-books.zip" in response.headers["content-disposition"]
            with ZipFile(BytesIO(response.content)) as archive:
                assert archive.testzip() is None
                assert set(archive.namelist()) == set(expected)
                assert {member: archive.read(member) for member in archive.namelist()} == expected
                assert missed_id not in archive.namelist()
                assert unknown_id not in archive.namelist()
                assert not any("omission" in member.casefold() for member in archive.namelist())

        empty = client.post(
            "/selected/download",
            data={
                "ids": json.dumps([missed_id, unknown_id]),
                "preset": "nested",
                "csrf_token": csrf_token,
            },
        )
        assert empty.status_code == 422
        assert "No selected books are available for download" in empty.text
        assert empty.headers["content-type"].startswith("text/html")
        assert "content-disposition" not in empty.headers
        assert not empty.content.startswith(b"PK")
