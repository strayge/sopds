# SOPDS

SOPDS turns an INPX ebook library into a private, self-hosted catalog for web
browsers, OPDS reader apps, and an optional Telegram bot.

> [!IMPORTANT]
> SOPDS does not provide user accounts or authentication. Run it only on a
> trusted network, restrict access with a firewall, or place it behind an
> authenticating reverse proxy.

## Features

- **Fast catalog search** — find books by title, author, or series and narrow
  results by language, genre, format, and availability.
- **Compact reader-focused web interface** — browse results, inspect book
  details, and return without losing the current search, filters, or page.
- **Reader-ready downloads** — keep the stored format as the primary action,
  with EPUB and AZW3 choices when SOPDS can produce them.
- **Multiple-book ZIP downloads** — build one original-, EPUB-, or AZW3-format
  archive from books selected across different searches and result pages.
- **OPDS 1.2 catalog** — browse the library and use its additional EPUB and
  AZW3 acquisitions from compatible ebook readers.
- **Telegram access** — allow selected chats to search for books and choose an
  available download format from a bot.
- **Safe catalog updates** — refresh the library while readers continue using
  the previous catalog if an update fails.

## Using the web catalog

### Find and download a book

Use the search field and filters to narrow the catalog. Open a result to see its
full metadata, or use the source-format button, such as **FB2** or **EPUB**, to
get the stored file unchanged. When more formats are available, the adjacent
menu offers **EPUB** or **AZW3** without repeating the source format.

FB2 books can become EPUB2 or AZW3. Existing EPUB files pass through unchanged
when EPUB is selected and can become AZW3. Existing AZW3 files pass through
unchanged when AZW3 is selected. Other combinations are not offered.

### Read a stored book

On a book's details page, **Read** is a secondary action for downloadable stored
FB2 and EPUB sources; the original-format download remains primary. It opens the
reader in a new browser tab. The reader supports stored FB2 and reflowable EPUB
2/3 without converting the book. An FB2 cover and introductory annotation are
shown at the start when the stored book provides them. Converted downloads and
other source formats cannot be read there.

The compact toolbar shows the book title, **Contents**, a **Pages** or **Scroll**
action, reading progress, **Smaller text**, and **Larger text**. The mode action
names the view it will switch to. The first use defaults to Scroll mode; if you
select Pages, that preference is reopened in this browser. Switching modes keeps
your approximate position. Scroll mode is section-local: wheel or touch movement
scrolls the current section, and an outward gesture at its edge automatically
moves to the adjacent section. Its right-edge scrollbar shows progress through
the whole book; dragging previews a percentage and chapter name when available,
and releasing jumps to that position. Pages mode provides **Previous** and **Next**
controls, with a page dock generally available and edge controls on desktop.
Arrow Left/Right, Page Up/Down, Space for the next page, and touch navigation
are also supported. In Scroll mode, Space keeps the last complete line as the
first line of the next screen when ordinary body text allows it. The
reader follows the browser's light or dark appearance.

The reader automatically resumes a saved position, remembers the selected reader
mode, and remembers the font-size setting in this browser. These settings are not
synchronized between browsers, devices, or users. Clearing SOPDS site data removes
them.

Fixed-layout and encrypted EPUB files are not supported. Sources larger than 64
MiB cannot be opened in the reader; use the original download instead. If a book
cannot be opened, use **Retry**, **Download original**, or **Back to book**.

Current Chromium and Firefox on desktop are manually validated. Android Chromium
and Firefox are the target baseline, but physical-device validation remains
pending.

The catalog normally shows books whose source archives are available. Use
**Include hidden** or **Include missing** when you need to inspect exceptional
records:

- **Hidden** books were marked as deleted by the source catalog.
- **Missed** books refer to source archives that are not currently available.

Unavailable books remain discoverable but cannot be downloaded.

### Download several books

Select downloadable books from any catalog result page. The **Selected** entry
in the navigation shows how many books are currently selected.

On the selected-books page you can:

- uncheck books that should not be included in the next ZIP;
- remove individual books or clear the selection;
- review unavailable, unsupported, and conflicting books;
- choose a ZIP layout and one output format;
- download all currently included supported books together.

Unchecked rows remain visible until the page is reloaded, making it easy to
change your mind. The selection belongs to the current browser profile and does
not synchronize to other browsers or addresses.

Available ZIP layouts are:

- **Nested folders** — organize books by author and series.
- **Author folders** — keep author folders while placing series information in
  filenames.
- **Single list** — place every book at the archive root.

The source option is labeled with that format, such as **FB2**, when all checked,
downloadable books share it; mixed sources use **Original**. EPUB and AZW3 are
shown when at least one included book supports them. A selected EPUB or AZW3 ZIP
contains only that format: unsupported books are identified and excluded.
Because preview does not run conversions, converted selections report **source
size**, not the final ZIP size.

Filename conflicts are resolved automatically. Unknown or unavailable books
are also left out. If no selected book can be downloaded in the chosen format,
SOPDS reports an error instead of producing an empty archive.

A single ZIP can contain up to 10,000 books and 10 GB of eligible source files.
If the page reports that it has expired, reload it and retry the download.

## Using an OPDS reader

Add the OPDS catalog address shown in the deployment instructions to any
OPDS 1.2-compatible reader. You can browse by author, series, or title, search
the catalog, open book metadata, and use the original acquisition plus any
additional EPUB or AZW3 acquisitions supported for that book.

## Using Telegram

When Telegram support is enabled, authorized chats can:

- send text to search the catalog;
- open matching book details;
- use format buttons for the source format, EPUB, or AZW3 when available.

Unauthorized chats are ignored. The 50 MiB limit applies to the actual file
sent: originals are checked before upload, while converted files are checked
after conversion. Larger files are reported as too large and are not replaced
with an external download link.

## Managing the catalog

SOPDS checks the configured INPX source when it starts and then at the configured
interval. Readers can continue browsing while a catalog update runs.

The management page provides these actions:

- **Import changes** — check the source immediately and import it when needed.
- **Force import** — rebuild the catalog even when the source appears unchanged.
- **Vacuum database** — reclaim database space during maintenance.

A failed update leaves the previous catalog available. If no catalog has ever
been imported, SOPDS starts with an empty catalog. If the management page
reports that it has expired, reload it and retry the operation.

## Deploy with Docker Compose

### Prepare files

The container runs as UID 1000. From the repository directory:

```shell
cp config.example.toml config.toml
mkdir -p library data
sudo chown -R 1000:1000 config.toml library data
sudo chmod 600 config.toml
```

Edit `config.toml`, then place the INPX file and all ZIP archives it references
inside `library/`. Preserve the paths and filenames expected by the INPX source.

### Start SOPDS

```shell
docker compose up --build -d
```

On the Docker host, open the web catalog at <http://localhost:8000/>. From
another device, replace `localhost` with the server's reachable hostname or IP
address.

Use the same reachable address with `/opds/` appended when configuring an OPDS
reader. For example:

```text
http://books.example.test:8000/opds/
```

The Compose deployment publishes port 8000 on all host interfaces. Restrict
that port when the catalog should not be reachable by the whole network.

Run exactly one SOPDS container. Multi-worker and horizontally scaled
deployments are not supported. The Docker image currently supports only
`linux/amd64` because its converter binaries are architecture-specific.

### Mounted storage

The default deployment mounts:

- `config.toml` at `/config/config.toml` as read-only configuration;
- `library/` at `/library` as the read-only source library;
- `data/` at `/data` as writable application data.

UID 1000 must be able to read the configuration and library and write to the
data directory. Adapt ownership when your container runtime uses a different
UID mapping.

Allow enough free space in `data/` for catalog updates and cached conversion
artifacts. Conversions briefly stage source and output files there; successful
artifacts remain cached until their configured expiry, so simultaneous and
varied format requests increase storage use. Multiple-book downloads use the
container's temporary filesystem. A maximum-size download can require slightly
more than 10 GB after ZIP overhead, and simultaneous downloads multiply that
requirement.

## Configuration

SOPDS reads `config.toml` and rejects unknown settings. Environment-variable
overrides are not supported.

The main settings control:

- the listening host, port, and externally visible base address;
- the INPX source, archive directory, and automatic check interval;
- the SQLite database and conversion-cache locations and cache lifetime;
- optional Telegram access and the allowed chat IDs.

Paths in `config.example.toml` are container paths used by the default Compose
deployment.

Publish SOPDS at the root of its hostname; reverse-proxy path prefixes are not
supported by the web interface. Use the externally reachable catalog address in
`server.base_url` and when configuring clients.

To enable Telegram, set `telegram.enabled = true`, provide the bot token, and
list at least one numeric chat ID in `allowed_chat_ids`.

## Run locally without Docker

SOPDS requires Python 3.14.

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.freeze.txt
cp config.example.toml config.toml
# Change the container paths in config.toml to local paths.
PYTHONPATH=src python -m sopds --config config.toml
```

Open <http://localhost:8000/> after startup.

Original-only local runs need no converter binaries. Converted downloads require
installing the pinned compatible `fb2cng` 1.6.1 (`fbc`) and Kindling 0.38.0
(`kindling-cli`) binaries at `/usr/local/bin/fbc` and
`/usr/local/bin/kindling-cli`; Docker bundles them.

## Backup and restore

Stop SOPDS before creating a backup, then copy:

- `config.toml`;
- the database file configured by `database.path`;
- the INPX source and all referenced ZIP archives.

The INPX file and ZIP archives remain the source library, while the database
contains the catalog built from that source. Restore both when moving the
service to another host.

## Converter software

- [fb2cng](https://github.com/rupor-github/fb2cng) — GPL-3.0
- [Kindling](https://github.com/ciscoriordan/kindling) — MIT
