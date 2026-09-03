# SOPDS

SOPDS turns an INPX ebook library into a private, self-hosted catalog for the
web, OPDS readers, and an optional Telegram bot.

> [!IMPORTANT]
> SOPDS has no authentication. Use a trusted network, firewall, or
authenticating reverse proxy.

## Features

- Search and filter large ebook libraries.
- Flat, Tree, and Table catalog views.
- Original, EPUB, and AZW3 downloads.
- Multi-book ZIP downloads.
- Built-in FB2 and EPUB reader.
- OPDS 1.2 and optional Telegram access.
- Safe catalog updates that keep the previous catalog available on failure.

## Quick start with Docker Compose

SOPDS runs as UID 1000. Prepare the configuration and storage directories:

```shell
cp config.example.toml config.toml
mkdir -p library data/db
sudo chown -R 1000:1000 config.toml library data
sudo chmod 600 config.toml
```

Edit `config.toml`. Set `server.base_url` to the address readers will use and
set `catalog.inpx_path` to the INPX file under `/library`. Place that file and
its referenced ZIP archives in `library/`.

Start SOPDS:

```shell
docker compose up --build -d
```

Open <http://localhost:8000/>. For access from another device, replace
`localhost` with the server hostname or IP address. Add `/opds/` to the same URL
to connect an OPDS reader.

Compose exposes port 8000 on all interfaces. Restrict it when needed. Run only
one SOPDS container; multi-worker and scaled deployments are unsupported. The
Docker image supports `linux/amd64`.

## Using the catalog

Search by title, author, or series and filter by language, genre, format, or
availability. **Flat** shows a reading list, **Tree** groups books by author and
series, and **Table** provides sortable columns.

Open a book to view its metadata. Its format button downloads the original
file, while the adjacent menu offers available EPUB or AZW3 versions. SOPDS can
convert FB2 to EPUB or AZW3 and EPUB to AZW3.

**Read** opens supported FB2 and reflowable EPUB 2/3 books in the browser.
Fixed-layout and encrypted EPUB files are not supported.

To download several books, select them and open **Selected**. Choose the output
format and ZIP layout, then exclude any unwanted books before downloading. A
ZIP can contain up to 10,000 books and 10 GB of eligible source files.

**Include hidden** shows records deleted from the source. **Include missing**
shows records labeled **Missed** whose source archives are unavailable. Missed
books remain searchable but cannot be downloaded.

## Catalog management

SOPDS checks the INPX source at startup and at the configured interval. Readers
can continue using the previous catalog while an import runs or after one
fails.

The management page provides:

- **Import changes** to check for and import source changes.
- **Force import** to re-import an unchanged source.
- **Vacuum database** to run database maintenance without changing the catalog.

## OPDS and Telegram

Add the deployment's `/opds/` URL to an OPDS 1.2-compatible reader for browsing,
search, metadata, and downloads.

To enable Telegram, set `telegram.enabled = true` in `config.toml`, provide a
bot token, and add numeric chat IDs to `allowed_chat_ids`. Only approved chats
receive responses. Telegram files are limited to 50 MiB.

## Configuration

SOPDS reads `config.toml`. The example configuration includes settings for:

- the public server URL;
- the INPX file, archive directory, and update interval;
- the PostgreSQL connection;
- Telegram access;
- converted-book caching.

The example uses paths and a database hostname intended for Docker Compose. A
host installation must use paths and a PostgreSQL URL reachable from that host.
Publish SOPDS at the hostname root; URL path prefixes are unsupported.

## Local run

SOPDS requires Python 3.14 and a reachable PostgreSQL server.

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.freeze.txt
cp config.example.toml config.toml
# Change Docker paths and database.url for the host environment.
PYTHONPATH=src python -m sopds --config config.toml
```

Conversions require `fb2cng` 1.6.1 (`fbc`) and Kindling 0.38.0
(`kindling-cli`) in `/usr/local/bin/`. The Docker image includes both.

## Acknowledgments

- [fb2cng](https://github.com/rupor-github/fb2cng) (GPL-3.0)
- [Kindling](https://github.com/ciscoriordan/kindling) (MIT)
- [Foliate-js](https://github.com/johnfactotum/foliate-js) (MIT)
- [zip.js](https://github.com/gildas-lormeau/zip.js) (BSD-3-Clause)
- [htmx](https://htmx.org/) (0BSD)
- [IBM Plex Sans](https://github.com/IBM/plex),
  [Literata](https://github.com/googlefonts/literata), and
  [Noto Serif](https://github.com/notofonts/latin-greek-cyrillic) (OFL-1.1)
