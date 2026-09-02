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

Individual malformed book records are excluded when they make up no more than
10% of the source, with one rejected record always allowed. The management page
shows the **Rejected** count. An import with no valid records or more than 10%
rejected records fails and leaves the previous catalog active.

The management page provides **Import changes**, **Force import**, and **Vacuum
database**. **Vacuum database** performs safe PostgreSQL `VACUUM (ANALYZE)`
maintenance without changing catalog contents or overlapping an import. Reload and
retry if the page reports that it has expired.

## Docker Compose

The SOPDS container runs as UID 1000. Prepare the deployment:

```shell
cp config.example.toml config.toml
mkdir -p library data/db
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

Compose starts PostgreSQL as the `postgres` service and waits for it to become
healthy before starting SOPDS. PostgreSQL has no published host port and is
reachable by SOPDS as `postgres` only on the internal `database` network. The
Compose database uses passwordless trust on that private network; do not publish
its port or attach unrelated services to that network.

The first PostgreSQL deployment starts with an empty catalog. After you place
the INPX source and archives in `library/`, SOPDS automatically checks and
imports them at startup; the existing SQLite catalog is not migrated. Use
**Import changes** to retry a failed startup check or apply changes before the
next scheduled check, and **Force import** to re-import an unchanged source.

Compose exposes port 8000 on all interfaces. Restrict it when needed. Run one
container only; multi-worker and scaled deployments are unsupported. The image
supports `linux/amd64` because converters are architecture-specific.

Default mounts:

- `config.toml` → `/config/config.toml` (read-only);
- `library/` → `/library` (read-only);
- `data/` → `/data` (writable; conversion cache and temporary application data);
- `data/db/` → `/var/lib/postgresql` (PostgreSQL catalog data).

PostgreSQL manages the ownership and contents of `data/db/` after its first
startup; do not modify or recursively change ownership of that directory while
the database is initialized. Allow space in `data/` for both cached conversions
and the PostgreSQL catalog and indexes. Multi-book ZIPs use temporary container
storage; a maximum-size ZIP needs slightly more than 10 GB.

## Configuration

SOPDS reads `config.toml`, rejects unknown settings, and has no environment
variable overrides. Settings cover the server URL, INPX source, archives,
import interval, PostgreSQL database URL, conversion-cache location and
lifetime, and optional Telegram access.

`database.url` must point to a PostgreSQL server reachable by the SOPDS process.
The example URL uses the Compose service name `postgres` and is intended for
container deployment; a process running directly on the host needs its own
reachable PostgreSQL URL.

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
# Change container paths and database.url to values reachable from this process.
PYTHONPATH=src python -m sopds --config config.toml
```

The Compose hostname `postgres` is available only inside Compose's internal
network. A host-run process must use a separately reachable PostgreSQL server.

Open <http://localhost:8000/>. Conversions require `fb2cng` 1.6.1 (`fbc`) and
Kindling 0.38.0 (`kindling-cli`) in `/usr/local/bin/`; Docker includes both.

## Backup and restore

Keep these items together in each backup set:

- a custom-format PostgreSQL dump;
- `config.toml`;
- the INPX source;
- all referenced ZIP archives.

Create the backup through the Compose `postgres` service. The command stops
SOPDS while collecting the database, configuration, and library, then restarts
it. Do not change `config.toml` or `library/` until the command finishes. Do not
copy the live `data/db/` files as a database backup.

```shell
set -eu
umask 077
backup_root=backups
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_path="$backup_root/sopds-$timestamp"
backup_tmp="$backup_root/.sopds-$timestamp.tmp.$$"

mkdir -p "$backup_root"
test ! -e "$backup_path"
mkdir "$backup_tmp"
restart_sopds=false
cleanup() {
  status=$?
  trap - 0
  rm -rf "$backup_tmp"
  if [ "$restart_sopds" = true ]; then
    docker compose up -d sopds || true
  fi
  exit "$status"
}
trap cleanup 0
trap 'exit 1' HUP INT TERM

restart_sopds=true
docker compose stop sopds
docker compose exec -T postgres \
  pg_dump --format=custom --username=sopds --dbname=sopds \
  > "$backup_tmp/catalog.dump"
cp config.toml "$backup_tmp/config.toml"
tar -czf "$backup_tmp/library.tar.gz" -C library .
mv "$backup_tmp" "$backup_path"
docker compose up -d sopds
restart_sopds=false
```

The timestamped directory appears only after all three items succeed. Temporary
files are removed and SOPDS is restarted if backup fails.

To restore, select one complete timestamped directory. Keep PostgreSQL running
while SOPDS is stopped, restore the saved configuration and library, and then
restore the matching dump:

```shell
set -eu
backup_path=backups/sopds-YYYYMMDDTHHMMSSZ
restore_stamp=$(date -u +%Y%m%dT%H%M%SZ)
restore_tmp="library.restore.$$"
previous_config="config.toml.before-restore-$restore_stamp"
previous_library="library.before-restore-$restore_stamp"
failed_config="config.toml.failed-restore-$restore_stamp"
failed_library="library.failed-restore-$restore_stamp"

test ! -e "$previous_config"
test ! -e "$previous_library"
test ! -e "$failed_config"
test ! -e "$failed_library"
mkdir "$restore_tmp"
cleanup() { rm -rf "$restore_tmp" config.toml.restore; }
trap 'cleanup; exit 1' HUP INT TERM
trap cleanup 0

tar -xzf "$backup_path/library.tar.gz" -C "$restore_tmp"
cp "$backup_path/config.toml" config.toml.restore
docker compose stop sopds
mv config.toml "$previous_config"
mv library "$previous_library"
mv config.toml.restore config.toml
mv "$restore_tmp" library

if docker compose exec -T postgres \
  pg_restore --clean --if-exists --no-owner --exit-on-error --single-transaction \
  --username=sopds --dbname=sopds < "$backup_path/catalog.dump"
then
  docker compose up -d sopds
else
  status=$?
  mv config.toml "$failed_config"
  mv library "$failed_library"
  mv "$previous_config" config.toml
  mv "$previous_library" library
  printf '%s\n' \
    "Restore failed; previous config.toml and library restored; SOPDS remains stopped." >&2
  exit "$status"
fi
```

After verifying a successful restore, remove the matching
`config.toml.before-restore-*` file and `library.before-restore-*` directory. If
`pg_restore` fails, its attempted pair is retained as
`config.toml.failed-restore-*` and `library.failed-restore-*`, while the previous
configuration and library are put back automatically and SOPDS remains stopped.
If the Compose project is down, start the database first with
`docker compose up -d postgres`.

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
