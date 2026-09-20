"""Secret-safe verification helpers for the Meta WhatsApp webhook boundary."""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

META_SIGNATURE_PREFIX: Final = "sha256="
_SHA256_HEX_LENGTH: Final = 64


def verify_webhook_subscription(
    mode: str | None,
    verify_token: str | None,
    challenge: str | None,
    configured_token: str | None,
) -> bool:
    """Validate Meta's GET subscription handshake without exposing its token."""
    if mode != "subscribe" or not challenge or not _usable_secret(configured_token):
        return False
    if verify_token is None:
        return False
    assert configured_token is not None
    return hmac.compare_digest(verify_token, configured_token)


def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str | None,
) -> bool:
    """Authenticate a raw Meta payload using ``X-Hub-Signature-256``."""
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    if signature_header is None or not _usable_secret(app_secret):
        return False

    assert app_secret is not None
    expected = (
        META_SIGNATURE_PREFIX
        + hmac.new(
            app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    )

    # Meta's current format is exactly ``sha256=`` plus a 64-character hex
    # digest. Comparing the complete header also authenticates the algorithm.
    if len(signature_header) != len(META_SIGNATURE_PREFIX) + _SHA256_HEX_LENGTH:
        return False
    return hmac.compare_digest(expected, signature_header)


def _usable_secret(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    stripped = value.strip()
    return not (stripped.startswith("<") and stripped.endswith(">"))


__all__ = [
    "META_SIGNATURE_PREFIX",
    "verify_webhook_signature",
    "verify_webhook_subscription",
]
