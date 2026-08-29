"""Issue short-lived bearer tokens for anonymous browser POST protection."""

import base64
import binascii
import hashlib
import hmac
import secrets
import time

CSRF_TOKEN_TTL_SECONDS = 3_600
CSRF_KEY_BYTES = 32
_NONCE_BYTES = 16
_SIGNATURE_BYTES = hashlib.sha256().digest_size
_PAYLOAD_BYTES = 8 + _NONCE_BYTES
_TOKEN_BYTES = _PAYLOAD_BYTES + _SIGNATURE_BYTES
_SIGNING_CONTEXT = b"sopds-csrf-v1\0"


def issue_csrf_token(key: bytes, *, now: int | None = None) -> str:
    """Limit a leaked page token without retaining server-side token state."""
    if len(key) < CSRF_KEY_BYTES:
        raise ValueError("CSRF signing key is too short")
    issued_at = int(time.time()) if now is None else now
    if not 0 <= issued_at < 2**64:
        raise ValueError("CSRF token timestamp is out of range")
    payload = issued_at.to_bytes(8, "big") + secrets.token_bytes(_NONCE_BYTES)
    signature = hmac.digest(key, _SIGNING_CONTEXT + payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def validate_csrf_token(key: bytes, token: str, *, now: int | None = None) -> bool:
    """Accept authentic tokens for one hour, including retries within that window."""
    if len(key) < CSRF_KEY_BYTES or not token or not token.isascii():
        return False
    try:
        encoded = token.encode("ascii")
        raw = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except ValueError, binascii.Error:
        return False
    if len(raw) != _TOKEN_BYTES:
        return False
    if base64.urlsafe_b64encode(raw).rstrip(b"=") != encoded:
        return False
    payload = raw[:_PAYLOAD_BYTES]
    supplied_signature = raw[_PAYLOAD_BYTES:]
    expected_signature = hmac.digest(key, _SIGNING_CONTEXT + payload, "sha256")
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return False
    issued_at = int.from_bytes(payload[:8], "big")
    checked_at = int(time.time()) if now is None else now
    age = checked_at - issued_at
    return 0 <= age <= CSRF_TOKEN_TTL_SECONDS
