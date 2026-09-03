"""Telegram rendering with bounded, normalized user metadata."""

import unicodedata
from html import escape

from sopds.catalog.contracts import CatalogBook
from sopds.conversion.contracts import normalize_format

TELEGRAM_TEXT_LIMIT = 4_096
BUTTON_LABEL_LIMIT = 64
METADATA_LIMIT = 512
_RESULT_AUTHOR_LIMIT = 64
_RESULT_SERIES_LIMIT = 64
_RESULT_TITLE_LIMIT = 96
_MAX_BOOK_ID = 2**63 - 1


def sanitize(value: str, *, limit: int = METADATA_LIMIT) -> str:
    """Normalize untrusted metadata and remove invisible or layout-changing controls."""
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf"} else char for char in normalized
    )
    compact = " ".join(cleaned.split())
    return truncate(compact, limit)


def utf16_length(value: str) -> int:
    """Return Telegram's length measure without splitting Python code points."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def truncate(value: str, limit: int) -> str:
    """Bound text by UTF-16 code units while reserving one unit for an ellipsis."""
    if limit < 1:
        return ""
    if utf16_length(value) <= limit:
        return value
    budget = limit - 1
    used = 0
    prefix_chars = 0
    for char in value:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > budget:
            break
        used += units
        prefix_chars += 1
    return value[:prefix_chars].rstrip() + "…"


def button_label(value: str) -> str:
    return truncate(sanitize(value), BUTTON_LABEL_LIMIT) or "Untitled"


def source_format_label(value: str) -> str:
    try:
        return normalize_format(value).upper()
    except ValueError:
        return "FILE"


def _valid_catalog_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_BOOK_ID


def book_command(book_id: int | None) -> str | None:
    return f"/b{book_id}" if _valid_catalog_id(book_id) else None


def author_command(author_id: int | None) -> str | None:
    return f"/a{author_id}" if _valid_catalog_id(author_id) else None


def series_command(series_id: int | None) -> str | None:
    return f"/s{series_id}" if _valid_catalog_id(series_id) else None


def catalog_id_from_command(value: str, prefix: str) -> int | None:
    command_prefix = f"/{prefix}"
    if not value.startswith(command_prefix):
        return None
    raw_id = value[len(command_prefix) :]
    if not raw_id.isascii() or not raw_id.isdecimal() or raw_id.startswith("0"):
        return None
    catalog_id = int(raw_id)
    return catalog_id if _valid_catalog_id(catalog_id) else None


def _author_name(value: str, *, limit: int = METADATA_LIMIT) -> str:
    return sanitize(value.replace(",", " "), limit=limit)


def _result_authors(authors: tuple[str, ...]) -> str:
    visible = [escape(_author_name(author, limit=_RESULT_AUTHOR_LIMIT)) for author in authors[:2]]
    visible = [author for author in visible if author]
    if not visible:
        return "Unknown author"
    if len(authors) > 2:
        visible.append(f"+{len(authors) - 2}")
    return ", ".join(visible)


def results_text(books: tuple[CatalogBook, ...]) -> str:
    if not books:
        return "No books found."
    lines: list[str] = []
    for book in books:
        if lines:
            lines.append("")
        lines.append(_result_authors(book.authors))

        series = escape(sanitize(book.series or "", limit=_RESULT_SERIES_LIMIT))
        number = escape(sanitize(book.series_number or "", limit=32))
        title = escape(sanitize(book.title, limit=_RESULT_TITLE_LIMIT) or "Untitled")
        heading = f"{series}{f' #{number}' if number else ''} " if series else ""
        lines.append(f"{heading}<b>{title}</b>")

        facts: list[str] = []
        if book.published_date is not None:
            facts.append(f"<i>{book.published_date.isoformat()}</i>")
        if book.size > 0:
            facts.append(f"{max(1, (book.size + 512) // 1024)}KB")
        command = book_command(book.book_id)
        if command is not None:
            facts.append(command)
        if facts:
            lines.append(" • ".join(facts))
    return "\n".join(lines)


def detail_text(book: CatalogBook) -> str:
    title = escape(sanitize(book.title) or "—")
    lines = [f"<b>{title}</b>"]
    for index, author in enumerate(book.authors[:5]):
        value = escape(_author_name(author))
        if not value:
            continue
        author_id = book.author_ids[index] if index < len(book.author_ids) else None
        command = author_command(author_id)
        lines.append(f"{value}{f' {command}' if command else ''}")
    if len(book.authors) > 5:
        lines.append(f"+{len(book.authors) - 5}")
    series = escape(sanitize(book.series or ""))
    if series:
        number = escape(sanitize(book.series_number or "", limit=32))
        command = series_command(book.series_id)
        value = f"{series}{f' #{number}' if number else ''}"
        lines.append(f"{value}{f' {command}' if command else ''}")
    facts: list[str] = []
    if book.published_date:
        facts.append(book.published_date.isoformat())
    if book.size > 0:
        facts.append(f"{max(1, (book.size + 512) // 1024)}KB")
    if book.language:
        language = escape(sanitize(book.language, limit=32))
        if language:
            facts.append(language)
    if facts:
        lines.append(" • ".join(facts))
    genres = escape(sanitize(", ".join(label for _code, label in book.genres)))
    if genres:
        lines.append(genres)
    keywords = escape(sanitize(book.keywords or ""))
    if keywords:
        lines.append(keywords)
    return truncate("\n".join(lines), TELEGRAM_TEXT_LIMIT)


def safe_filename(value: str) -> str:
    cleaned = sanitize(value, limit=180).replace("/", "_").replace("\\", "_")
    return cleaned.strip(". ") or "book"
