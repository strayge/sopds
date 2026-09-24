"""Canonical language identities shared by imports and catalog adapters."""


def normalize_language(value: str | None) -> str | None:
    """Collapse valid variants while preserving malformed source metadata for diagnosis."""
    if value is None:
        return None

    normalized = value.strip().lower()
    if not normalized:
        return None

    subtags = normalized.split("-")
    if (
        len(subtags) > 1
        and 2 <= len(subtags[0]) <= 8
        and subtags[0].isascii()
        and subtags[0].isalpha()
        and all(
            1 <= len(subtag) <= 8 and subtag.isascii() and subtag.isalnum()
            for subtag in subtags[1:]
        )
    ):
        return subtags[0]
    return normalized
