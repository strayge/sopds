"""Short-lived stateless CSRF token tests."""

import pytest

from sopds.web.csrf import CSRF_TOKEN_TTL_SECONDS, issue_csrf_token, validate_csrf_token

_KEY = b"a" * 32


def test_token_is_reusable_for_exactly_one_hour() -> None:
    token = issue_csrf_token(_KEY, now=1_000)

    assert validate_csrf_token(_KEY, token, now=1_000)
    assert validate_csrf_token(_KEY, token, now=1_000 + CSRF_TOKEN_TTL_SECONDS)
    assert not validate_csrf_token(_KEY, token, now=1_001 + CSRF_TOKEN_TTL_SECONDS)
    assert not validate_csrf_token(_KEY, token, now=999)


def test_tokens_are_unique_and_invalid_after_restart() -> None:
    first = issue_csrf_token(_KEY, now=1_000)
    second = issue_csrf_token(_KEY, now=1_000)

    assert first != second
    assert not validate_csrf_token(b"b" * 32, first, now=1_000)


@pytest.mark.parametrize("token", ["", "not base64!", "AA", "A" * 75 + "="])
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert not validate_csrf_token(_KEY, token, now=1_000)


def test_tampered_token_is_rejected() -> None:
    token = issue_csrf_token(_KEY, now=1_000)
    replacement = "A" if token[-1] != "A" else "B"

    assert not validate_csrf_token(_KEY, token[:-1] + replacement, now=1_000)


def test_short_signing_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="too short"):
        issue_csrf_token(b"short", now=1_000)

    assert not validate_csrf_token(b"short", "anything", now=1_000)
