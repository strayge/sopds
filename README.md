# SOPDS

SOPDS is a private, self-hosted ebook catalog backed by INPX metadata and ZIP
archives. It provides a web interface, an OPDS 1.2 catalog, original-file
downloads, and an optional allowlisted Telegram bot.

The web and OPDS interfaces have no authentication. The Compose file publishes
port 8000 on all host interfaces. Run it only on a trusted network, restrict the
port with a firewall, or place it behind an authenticating reverse proxy.

## Run with Docker Compose

The container runs as UID 1000. Prepare the deployment files and directories:

```shell
cp config.example.toml config.toml
mkdir -p library data
sudo chown -R 1000:1000 config.toml library data
sudo chmod 600 config.toml
```

Edit `config.toml`, then place the INPX file and its referenced ZIP archives in
`library/`. Preserve the archive paths and names expected by the INPX file.

Start SOPDS:

```shell
docker compose up --build -d
```

Open <http://localhost:8000/>. The OPDS catalog is available at
<http://localhost:8000/opds/>.

The deployment uses these mounts:

- `config.toml` → `/config/config.toml`, read-only;
- `library/` → `/library`, read-only;
- `data/` → `/data`, writable.

UID 1000 must be able to read `config.toml` and `library/`, and read and write
`data/`. Adapt the ownership commands when the container runtime uses a
different UID mapping.

Run exactly one SOPDS container. Do not add Uvicorn workers or scale the Compose
service because imports and Telegram polling are process-local.

## Configuration

Copy and edit `config.example.toml`. SOPDS reads only this TOML file, does not
support environment-variable overrides, and rejects unknown options.

SOPDS emits origin-relative OPDS and download links, so clients keep using the
host through which they reached the catalog. If a reverse proxy exposes SOPDS
under a path prefix, include that prefix in `server.base_url`; its scheme and
host are not included in generated links. Paths in the example configuration
are container paths.

To enable Telegram, set `telegram.enabled = true`, provide the bot token, and
add at least one numeric chat ID to `allowed_chat_ids`. Unauthorized chats are
ignored without a reply.

## Catalog operation

SOPDS checks the INPX source in the background at startup and periodically
afterward. The web interface remains available while an import runs. If the
INPX file is missing or invalid, SOPDS keeps the previous catalog, or starts
with an empty catalog when none exists.

Use **Import changes** to check the source immediately or **Force import** to
rebuild the catalog. A failed import leaves the previous catalog available. If
the management page reports that it has expired, reload it and retry the
operation.

Books whose ZIP archives are missing are marked as missed. INPX records marked as
deleted are hidden. Both categories are excluded from catalog search by default;
the web search can include either category explicitly. After upgrading an existing
database to this version, run **Force import** once to populate searchable hidden
metadata. Periodic checks update archive availability without rebuilding catalog
metadata.

Book pages, OPDS clients, and Telegram download original files directly from
the ZIP archives. No converted formats are currently offered.

OPDS author, series, and title browsing groups lists larger than 100 entries by
normalized name prefixes. Prefixes with only one possible continuation are
collapsed until the next useful branch. Authors with series are divided into
series, standalone books, and all books; authors without series open their book
list directly.

## Selected-book ZIP downloads

Downloadable catalog rows can be selected across searches and pages. SOPDS
stores only the ordered public book IDs in browser `localStorage`; selections
are local to that browser profile and origin. They are not cookies, accounts,
or server-side state and do not synchronize to another browser or origin.

Open `/selected` to review the current selection, exclude books, choose a ZIP
layout, and download the available originals. Unchecked books remain visible
until the page is reloaded. If the page reports that it has expired, reload it
and retry the download.

The three built-in layouts use the first listed author, include the normalized
original extension, and pad purely numeric series numbers to at least two
digits. For a book by `Ava Reader` titled `First Tide` in `Harbor Cycle` number
`1`, the exact examples are:

- **Nested:** `Ava Reader/Harbor Cycle/01 - First Tide.fb2`
- **Flatten:** `Ava Reader/Harbor Cycle 01 - First Tide.fb2`
- **List:** `Ava Reader. Harbor Cycle 01 - First Tide.fb2`

Books without a series omit the series and number. Portable sanitization can
make otherwise different names equal; the preview automatically highlights all
such collisions without displaying a generated path on every row, and the ZIP
uses deterministic numeric suffixes such as ` (2)` before the extension.

Unknown IDs and books whose originals are missed or otherwise unavailable stay
visible and removable in the preview but are silently omitted from the ZIP.
The archive contains no omission report. If every selection is omitted, SOPDS
returns an error instead of an empty ZIP.

A download is limited to 10,000 books and 10 GB of eligible originals. Allow up
to 10 GB of temporary disk space for each simultaneous download. Interrupted
or failed downloads are cleaned up automatically.

## Telegram

Authorized chats can:

- send plain text to search;
- open book details and download the original file.

Files over 50 MiB are reported as too large. The bot does not provide an HTTP
fallback link.

## Backup

Stop SOPDS before copying `data/sopds.sqlite3`; SQLite uses WAL sidecar files
while running. Back up the INPX file and ZIP archives separately because they
remain the catalog's source of authority.

Keep enough free space in `data/` for the current and replacement catalog
generations plus temporary SQLite growth.

## Develop locally

Requires Python 3.14 and pip.

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.dev.freeze.txt
cp config.example.toml config.toml
# Change container paths in config.toml to local paths.
PYTHONPATH=src python -m sopds --config config.toml --reload
```

Run the checks:

```shell
ruff check .
ruff format --check .
mypy src tests
PYTHONPATH=src lint-imports
pytest
```

## Database migrations

Define schema changes in `src/sopds/db/models.py`, then generate a native Tortoise
migration instead of writing one manually:

```shell
PYTHONPATH=src tortoise -c your_module.TORTOISE_ORM makemigrations catalog -n concise_name
pytest tests/test_database.py
```

The referenced configuration object should be created with `build_tortoise_config()`.

## Refresh dependencies

Regenerate the pinned requirement files with uv:

```shell
./scripts/refresh-freeze.sh
```
