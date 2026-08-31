# SOPDS

SOPDS turns an INPX ebook library into a private, self-hosted catalog for the
web, OPDS readers, and an optional Telegram bot.

> [!IMPORTANT]
> SOPDS has no authentication. Use a trusted network, firewall, or
> authenticating reverse proxy.

## Features

- Fast search and filters for large libraries.
- Compact Flat, Tree, and Table web views.
- Original, EPUB, and AZW3 downloads.
- Multi-book ZIP downloads.
- Built-in FB2 and EPUB reader.
- OPDS 1.2 and optional Telegram access.
- Safe catalog updates that preserve the previous catalog on failure.

## Web catalog

Use **EN** or **RU** to change the interface language. SOPDS follows the first
supported browser language, falls back to English, and remembers an explicit
choice in that browser. Book metadata is never translated.

### Search and download

- Search by title, author, or series; filter by language, genre, format, or
  availability.
- Each search loads up to 1,000 books. Refine the search when more match.
- **Flat** is a reading list, **Tree** groups by author and series, and **Table**
  provides sortable columns.
- **Title**, **Author**, and **Series** apply local filters to loaded books;
  **Clear** removes them.
- Open a book for full metadata. Use its format button for the original file and
  the adjacent menu for available EPUB or AZW3 conversions.
- Catalog view, sorting, and local filters are stored in the page address.

Supported conversions are FB2 to EPUB2 or AZW3 and EPUB to AZW3. Existing EPUB
and AZW3 files pass through unchanged when selected as output.

**Include hidden** shows source-deleted records. **Include missing** shows
records labeled **Missed**, whose source archives are unavailable. Missed books
cannot be downloaded.

### Read books

**Read** supports stored FB2 and reflowable EPUB 2/3 files. Results offer it up
to 64 MiB; book details also offer it for larger files but open a recovery page
with **Download original** and **Back to book**.

The reader provides **Contents**, **Pages** and **Scroll** modes, progress, text
sizing, keyboard controls, and touch controls. Position, mode, and text size are saved
in the current browser. Use **Retry**, **Download original**, or **Back to book**
after an error.

Fixed-layout or encrypted EPUB files and converted books are unsupported.
Desktop Chromium and Firefox are manually validated. Android Chromium and
Firefox are targeted but not yet validated on physical devices.

### Download several books

Select books, then open **Selected** to choose a view, output format, and ZIP
layout. Uncheck books to exclude them or use **Clear all**. Selections belong to
the current browser profile.

ZIP layouts are **Author + series folders**, **Author folders**, and **Single
list**. **Original** preserves mixed source formats. EPUB and AZW3 archives
exclude unsupported, unknown, or unavailable books. Filename conflicts are
resolved automatically.

A ZIP can contain up to 10,000 books and 10 GB of eligible source files. Reload
and retry if the page reports that it has expired.

## OPDS

Add the deployment's `/opds/` address to an OPDS 1.2-compatible reader. It
supports browsing, search, metadata, and available download formats.

## Telegram

Approved chats can search, open book details, and request available formats.
Other chats are ignored. Telegram files are limited to 50 MiB; larger files are
reported as too large.

## Catalog management

SOPDS checks the INPX source at startup and at the configured interval. Readers
can keep using the previous catalog while an import runs or after it fails. If
no import has ever succeeded, the catalog is empty.

The management page provides **Import changes**, **Force import**, and **Vacuum
database**. Reload and retry if the page reports that it has expired.

## Docker Compose

The container runs as UID 1000. Prepare the deployment:

```shell
cp config.example.toml config.toml
mkdir -p library data
sudo chown -R 1000:1000 config.toml library data
sudo chmod 600 config.toml
```

Edit `config.toml`, then place the INPX file and referenced ZIP archives in
`library/` with the paths expected by the source.

Start SOPDS:

```shell
docker compose up --build -d
```

Open <http://localhost:8000/>. Replace `localhost` with the server hostname or
IP address for other devices. The OPDS address is the same URL with `/opds/`.

Compose exposes port 8000 on all interfaces. Restrict it when needed. Run one
container only; multi-worker and scaled deployments are unsupported. The image
supports `linux/amd64` because converters are architecture-specific.

Default mounts:

- `config.toml` → `/config/config.toml` (read-only);
- `library/` → `/library` (read-only);
- `data/` → `/data` (writable).

UID 1000 needs the corresponding access. Allow space in `data/` for catalog
updates and cached conversions. Multi-book ZIPs use temporary container storage;
a maximum-size ZIP needs slightly more than 10 GB.

## Configuration

SOPDS reads `config.toml`, rejects unknown settings, and has no environment
variable overrides. Settings cover the server URL, INPX source, archives,
import interval, SQLite database, conversion-cache location and lifetime, and
optional Telegram access.

The example uses Docker paths. Publish SOPDS at the hostname root; web path
prefixes are unsupported. Set `server.base_url` to the reachable catalog URL.

To enable Telegram, set `telegram.enabled = true`, provide a bot token, and add
numeric IDs to `allowed_chat_ids`.

## Local run

SOPDS requires Python 3.14.

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.freeze.txt
cp config.example.toml config.toml
# Change container paths in config.toml to local paths.
PYTHONPATH=src python -m sopds --config config.toml
```

Open <http://localhost:8000/>. Conversions require `fb2cng` 1.6.1 (`fbc`) and
Kindling 0.38.0 (`kindling-cli`) in `/usr/local/bin/`; Docker includes both.

## Backup and restore

Stop SOPDS and copy `config.toml`, the database configured by `database.path`,
the INPX source, and all referenced ZIP archives. Restore the source library and
database together.

## Acknowledgments

- [fb2cng](https://github.com/rupor-github/fb2cng) (GPL-3.0) — FB2 to EPUB.
- [Kindling](https://github.com/ciscoriordan/kindling) (MIT) — EPUB to AZW3.
- [Foliate-js](https://github.com/johnfactotum/foliate-js) (MIT) — web reader.
- [zip.js](https://github.com/gildas-lormeau/zip.js) (BSD-3-Clause) — EPUB ZIP
  support.
- [htmx](https://htmx.org/) (0BSD) — dynamic web updates.
- [IBM Plex Sans](https://github.com/IBM/plex),
  [Literata](https://github.com/googlefonts/literata), and
  [Noto Serif](https://github.com/notofonts/latin-greek-cyrillic) (OFL-1.1) —
  web typography.
