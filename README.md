# SOPDS

A private, self-hosted ebook catalog backed by INPX metadata and ZIP archives.
The project is being rewritten with FastAPI, Jinja/HTMX, OPDS, and an
allowlisted Telegram bot.

## Run with Docker

Requires Docker and Docker Compose.

```shell
cp config.example.toml config.toml
mkdir -p library
# Place local.inpx and its ZIP archives in ./library.
docker compose up --build
```

Open <http://localhost:8000>.

Compose mounts `config.toml` and `library/` read-only. SQLite and conversion
artifacts are stored in `./data`.

## Develop locally

Requires Python 3.14 and pip.

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.dev.freeze.txt
cp config.example.toml config.toml
# Adjust config.toml to use host paths.
PYTHONPATH=src python -m sopds --config config.toml
```

Run the checks with:

```shell
ruff check .
ruff format --check .
mypy src tests
PYTHONPATH=src lint-imports
pytest
```

## Refresh dependencies

`pyproject.toml` defines the `runtime` and `dev` dependency groups. Regenerate
the pinned requirements files with uv:

```shell
./scripts/refresh-freeze.sh
```
