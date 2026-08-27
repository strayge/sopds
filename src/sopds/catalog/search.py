"""Pure catalog text normalization and safe FTS expression construction."""

import re
import unicodedata

from sopds.catalog.contracts import CatalogInputError, SearchField

MAX_QUERY_CHARS = 200
MAX_QUERY_TOKENS = 16
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("\u0451", "\u0435")


def query_tokens(value: str) -> tuple[str, ...]:
    if len(value) > MAX_QUERY_CHARS:
        raise CatalogInputError(f"Search query must be at most {MAX_QUERY_CHARS} characters")
    tokens = tuple(_WORDS.findall(normalize_text(value)))
    if len(tokens) > MAX_QUERY_TOKENS:
        raise CatalogInputError(f"Search query must contain at most {MAX_QUERY_TOKENS} words")
    return tokens


def normalized_query(value: str) -> str:
    return " ".join(query_tokens(value))


def fts_match_expression(
    tokens: tuple[str, ...], search_field: SearchField = SearchField.ALL
) -> str | None:
    """Build FTS syntax exclusively from extracted alphanumeric tokens."""
    if not tokens:
        return None
    column = {
        SearchField.ALL: "{title authors series}",
        SearchField.TITLE: "title",
        SearchField.AUTHOR: "authors",
        SearchField.SERIES: "series",
    }[search_field]
    return " AND ".join(f'{column} : "{token}"' for token in tokens)
