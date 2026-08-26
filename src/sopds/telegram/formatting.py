"""Plain-text Telegram rendering with bounded, normalized user metadata."""

import unicodedata

from sopds.catalog.contracts import BookDetail, BookSummary

TELEGRAM_TEXT_LIMIT = 4_096
BUTTON_LABEL_LIMIT = 64
METADATA_LIMIT = 512


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


def results_text(books: tuple[BookSummary, ...]) -> str:
    if not books:
        return "No books found."
    lines = ["Search results:"]
    for index, book in enumerate(books, 1):
        title = sanitize(book.title) or "Untitled"
        authors = sanitize(", ".join(book.authors)) or "Unknown author"
        source_format = sanitize(book.original_format, limit=32) or "unknown"
        lines.append(f"{index}. {title} — {authors} [{source_format}]")
    return truncate("\n".join(lines), TELEGRAM_TEXT_LIMIT)


def detail_text(book: BookDetail) -> str:
    values = [
        ("Title", book.title),
        ("Authors", ", ".join(book.authors)),
        ("Format", book.original_format),
        ("Language", book.language or ""),
        ("Series", " ".join(part for part in (book.series, book.series_number) if part)),
        ("Genres", ", ".join(label for _code, label in book.genres)),
        ("Published", book.published_date.isoformat() if book.published_date else ""),
        ("Size", str(book.size) if book.size >= 0 else ""),
        ("Library ID", book.libid or ""),
        ("Rating", str(book.rating) if book.rating is not None else ""),
        ("Keywords", book.keywords or ""),
    ]
    lines = [f"{label}: {sanitize(value)}" for label, value in values if value]
    return truncate("\n".join(lines) or "Book details unavailable.", TELEGRAM_TEXT_LIMIT)


def safe_filename(value: str) -> str:
    cleaned = sanitize(value, limit=180).replace("/", "_").replace("\\", "_")
    return cleaned.strip(". ") or "book"
