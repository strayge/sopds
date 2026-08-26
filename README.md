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

Set `server.base_url` to the externally reachable URL. SOPDS uses it for OPDS
and download links, so it must match the reverse-proxy URL when one is used.
Paths in the example configuration are container paths.

To enable Telegram, set `telegram.enabled = true`, provide the bot token, and
add at least one numeric chat ID to `allowed_chat_ids`. Unauthorized chats are
ignored without a reply.

## Catalog operation

SOPDS checks the INPX source in the background at startup and periodically
afterward. The web interface remains available while an import runs. If the
INPX file is missing or invalid, SOPDS keeps the previous catalog, or starts
with an empty catalog when none exists.

Use **Import now** in the web interface to force an import. A new catalog is
built separately and activated atomically. A failure before activation leaves
the previous catalog active.

Books whose ZIP archives are missing are hidden. Periodic checks update archive
availability without rebuilding catalog metadata.

Book pages, OPDS clients, and Telegram download original files directly from
the ZIP archives. No converted formats are currently offered.

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
PYTHONPATH=src python -m sopds --config config.toml
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
