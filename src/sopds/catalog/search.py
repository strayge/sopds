"""Pure catalog text normalization for sorting and PostgreSQL search."""

import re
import unicodedata

from sopds.catalog.contracts import CatalogInputError

MAX_QUERY_CHARS = 200
MAX_QUERY_TOKENS = 16
# Three fields feed all_vector, so 32 KiB each leaves ample space below PostgreSQL's
# 1 MiB tsvector limit even after practical lexeme and position overhead.
SEARCH_PROJECTION_MAX_BYTES = 32 * 1024
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("\u0451", "\u0435")


def normalize_search_text(value: str) -> str:
    """Fold Latin accents without changing non-Latin combining distinctions."""
    normalized = normalize_text(value)
    folded: list[str] = []
    latin_starter = False
    for character in unicodedata.normalize("NFD", normalized):
        if unicodedata.combining(character):
            if not latin_starter:
                folded.append(character)
            continue
        latin_starter = unicodedata.name(character, "").startswith("LATIN ")
        folded.append(character)
    return unicodedata.normalize("NFC", "".join(folded))


def normalize_search_projection(value: str) -> str:
    """Keep indexed and queried text on one punctuation-insensitive lexical projection."""
    return " ".join(_WORDS.findall(normalize_search_text(value)))


def bound_search_projection(value: str) -> str:
    """Cap a lexical projection without splitting a token or UTF-8 sequence."""
    if len(value.encode("utf-8")) <= SEARCH_PROJECTION_MAX_BYTES:
        return value

    kept: list[str] = []
    used_bytes = 0
    for token in value.split():
        token_bytes = len(token.encode("utf-8"))
        separator_bytes = 1 if kept else 0
        if used_bytes + separator_bytes + token_bytes > SEARCH_PROJECTION_MAX_BYTES:
            break
        kept.append(token)
        used_bytes += separator_bytes + token_bytes
    return " ".join(kept)


def query_tokens(value: str) -> tuple[str, ...]:
    if len(value) > MAX_QUERY_CHARS:
        raise CatalogInputError(f"Search query must be at most {MAX_QUERY_CHARS} characters")
    tokens = tuple(normalize_search_projection(value).split())
    if len(tokens) > MAX_QUERY_TOKENS:
        raise CatalogInputError(f"Search query must contain at most {MAX_QUERY_TOKENS} words")
    return tokens
