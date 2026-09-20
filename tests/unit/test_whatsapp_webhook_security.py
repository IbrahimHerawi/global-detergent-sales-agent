"""Unit tests for secret-safe Meta webhook verification helpers."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from app.services.whatsapp_webhook_security import (
    verify_webhook_signature,
    verify_webhook_subscription,
)

VERIFY_TOKEN = "verify-token-not-real"
APP_SECRET = "meta-app-secret-not-real"
RAW_BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def signature(body: bytes = RAW_BODY, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_subscription_verification_requires_exact_mode_token_and_challenge() -> None:
    assert verify_webhook_subscription(
        "subscribe",
        VERIFY_TOKEN,
        "123456789",
        VERIFY_TOKEN,
    )


@pytest.mark.parametrize(
    ("mode", "provided_token", "challenge", "configured_token"),
    [
        ("SUBSCRIBE", VERIFY_TOKEN, "123", VERIFY_TOKEN),
        ("subscribe", "wrong", "123", VERIFY_TOKEN),
        ("subscribe", None, "123", VERIFY_TOKEN),
        ("subscribe", VERIFY_TOKEN, None, VERIFY_TOKEN),
        ("subscribe", VERIFY_TOKEN, "", VERIFY_TOKEN),
        ("subscribe", VERIFY_TOKEN, "123", None),
        ("subscribe", "<verify-token>", "123", "<verify-token>"),
    ],
)
def test_subscription_verification_fails_closed(
    mode: str | None,
    provided_token: str | None,
    challenge: str | None,
    configured_token: str | None,
) -> None:
    assert not verify_webhook_subscription(
        mode,
        provided_token,
        challenge,
        configured_token,
    )


def test_signature_verification_uses_raw_body_hmac_sha256() -> None:
    assert verify_webhook_signature(RAW_BODY, signature(), APP_SECRET)


@pytest.mark.parametrize(
    ("body", "header", "secret"),
    [
        (RAW_BODY + b" ", signature(), APP_SECRET),
        (RAW_BODY, None, APP_SECRET),
        (RAW_BODY, "", APP_SECRET),
        (RAW_BODY, "sha1=" + "0" * 40, APP_SECRET),
        (RAW_BODY, "sha256=" + "0" * 64, APP_SECRET),
        (RAW_BODY, signature(), "wrong-secret"),
        (RAW_BODY, signature(), None),
        (RAW_BODY, signature(), "<meta-app-secret>"),
    ],
)
def test_signature_verification_rejects_invalid_or_unconfigured_inputs(
    body: bytes,
    header: str | None,
    secret: str | None,
) -> None:
    assert not verify_webhook_signature(body, header, secret)


def test_signature_verification_requires_raw_bytes() -> None:
    with pytest.raises(TypeError, match="raw_body must be bytes"):
        verify_webhook_signature("not bytes", signature(), APP_SECRET)  # type: ignore[arg-type]
