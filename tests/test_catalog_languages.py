"""Language identity normalization tests."""

import pytest

from sopds.catalog.languages import normalize_language


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (" \t ", None),
        (" RU ", "ru"),
        ("EN-US", "en"),
        ("zh-Hant-TW", "zh"),
        ("hye", "hye"),
        ("ru-", "ru-"),
        ("ru~", "ru~"),
        ("en_US", "en_us"),
    ],
)
def test_language_identity_normalization(value: str | None, expected: str | None) -> None:
    assert normalize_language(value) == expected
