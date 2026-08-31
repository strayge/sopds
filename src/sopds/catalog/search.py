"""Pure catalog text normalization for sorting and PostgreSQL search."""

import re
import unicodedata

from sopds.catalog.contracts import CatalogInputError

MAX_QUERY_CHARS = 200
MAX_QUERY_TOKENS = 16
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


def query_tokens(value: str) -> tuple[str, ...]:
    if len(value) > MAX_QUERY_CHARS:
        raise CatalogInputError(f"Search query must be at most {MAX_QUERY_CHARS} characters")
    tokens = tuple(_WORDS.findall(normalize_search_text(value)))
    if len(tokens) > MAX_QUERY_TOKENS:
        raise CatalogInputError(f"Search query must contain at most {MAX_QUERY_TOKENS} words")
    return tokens
