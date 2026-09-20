"""HTTP boundary tests for Meta webhook verification and authentication."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from app.api.routes import whatsapp_webhook as webhook_routes
from app.api.routes.whatsapp_webhook import get_webhook_settings
from app.core.config import Settings
from app.main import app
from starlette.types import Receive, Scope, Send

WEBHOOK_PATH = "/api/v1/webhooks/whatsapp"
VERIFY_TOKEN = "verify-token-not-real"
APP_SECRET = "meta-app-secret-not-real"
RAW_BODY = b'{"object":"whatsapp_business_account","entry":[]}'
WebhookProcessor = Callable[[bytes], Awaitable[None]]


class ScopeCapture:
    """Record the final ASGI query string seen by an outer server boundary."""

    def __init__(self) -> None:
        self.query_string: bytes | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)
        query_string = scope.get("query_string")
        self.query_string = query_string if isinstance(query_string, bytes) else None


def webhook_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "whatsapp_verify_token": VERIFY_TOKEN,
        "meta_app_secret": APP_SECRET,
        "max_webhook_body_size": 1_024,
    }
    values.update(updates)
    return Settings.model_validate(values)


def signature(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_webhook_settings] = lambda: webhook_settings()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as instance:
            yield instance
    finally:
        app.dependency_overrides = original_overrides


@pytest.fixture
def processing_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[bytes]:
    calls: list[bytes] = []

    async def record(raw_body: bytes) -> None:
        calls.append(raw_body)

    processor: WebhookProcessor = record
    monkeypatch.setattr(webhook_routes, "process_verified_whatsapp_webhook", processor)
    return calls


async def test_get_verification_returns_exact_plain_text_challenge(
    client: httpx.AsyncClient,
) -> None:
    challenge = "9485762301"

    response = await client.get(
        WEBHOOK_PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": challenge,
        },
    )

    assert response.status_code == 200
    assert response.content == challenge.encode("utf-8")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"hub.verify_token": VERIFY_TOKEN, "hub.challenge": "123"},
        {"hub.mode": "subscribe", "hub.challenge": "123"},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
        {
            "hub.mode": "Subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123",
        },
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "123",
        },
        {
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "",
        },
    ],
)
async def test_get_verification_rejects_incorrect_or_missing_values(
    client: httpx.AsyncClient,
    params: dict[str, str],
) -> None:
    response = await client.get(WEBHOOK_PATH, params=params)

    assert response.status_code == 403


async def test_get_verification_fails_closed_without_configured_token(
    client: httpx.AsyncClient,
) -> None:
    app.dependency_overrides[get_webhook_settings] = lambda: webhook_settings(
        whatsapp_verify_token=None
    )

    response = await client.get(
        WEBHOOK_PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123",
        },
    )

    assert response.status_code == 403


async def test_get_verification_redacts_token_from_server_access_log_scope() -> None:
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_webhook_settings] = lambda: webhook_settings()
    capture = ScopeCapture()
    transport = httpx.ASGITransport(app=capture, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                WEBHOOK_PATH,
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": VERIFY_TOKEN,
                    "hub.challenge": "123",
                },
            )
    finally:
        app.dependency_overrides = original_overrides

    assert response.status_code == 200
    assert capture.query_string is not None
    assert VERIFY_TOKEN.encode() not in capture.query_string
    assert b"hub.verify_token=%5BREDACTED%5D" in capture.query_string


async def test_post_valid_signature_processes_exact_raw_body(
    client: httpx.AsyncClient,
    processing_calls: list[bytes],
) -> None:
    response = await client.post(
        WEBHOOK_PATH,
        content=RAW_BODY,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature(RAW_BODY),
        },
    )

    assert response.status_code == 200
    assert processing_calls == [RAW_BODY]


@pytest.mark.parametrize(
    ("body", "header"),
    [
        (RAW_BODY, None),
        (RAW_BODY, "sha256=" + "0" * 64),
        (RAW_BODY, "sha1=" + "0" * 40),
        (RAW_BODY + b" ", signature(RAW_BODY)),
        (RAW_BODY, signature(RAW_BODY, secret="wrong-secret")),
    ],
)
async def test_post_rejects_invalid_signatures_before_processing(
    client: httpx.AsyncClient,
    processing_calls: list[bytes],
    body: bytes,
    header: str | None,
) -> None:
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers["X-Hub-Signature-256"] = header

    response = await client.post(WEBHOOK_PATH, content=body, headers=headers)

    assert response.status_code == 403
    assert processing_calls == []


async def test_post_fails_closed_without_configured_app_secret(
    client: httpx.AsyncClient,
    processing_calls: list[bytes],
) -> None:
    app.dependency_overrides[get_webhook_settings] = lambda: webhook_settings(meta_app_secret=None)

    response = await client.post(
        WEBHOOK_PATH,
        content=RAW_BODY,
        headers={"X-Hub-Signature-256": signature(RAW_BODY)},
    )

    assert response.status_code == 403
    assert processing_calls == []


async def test_post_rejects_oversized_body_before_signature_or_processing(
    client: httpx.AsyncClient,
    processing_calls: list[bytes],
) -> None:
    maximum_size = 16
    oversized = b"x" * (maximum_size + 1)
    app.dependency_overrides[get_webhook_settings] = lambda: webhook_settings(
        max_webhook_body_size=maximum_size
    )

    response = await client.post(
        WEBHOOK_PATH,
        content=oversized,
        headers={"X-Hub-Signature-256": signature(oversized)},
    )

    assert response.status_code == 413
    assert processing_calls == []


async def test_post_stream_limit_catches_underreported_content_length(
    client: httpx.AsyncClient,
    processing_calls: list[bytes],
) -> None:
    maximum_size = 16
    oversized = b"x" * (maximum_size + 1)
    app.dependency_overrides[get_webhook_settings] = lambda: webhook_settings(
        max_webhook_body_size=maximum_size
    )

    response = await client.post(
        WEBHOOK_PATH,
        content=oversized,
        headers={
            "Content-Length": "1",
            "X-Hub-Signature-256": signature(oversized),
        },
    )

    assert response.status_code == 413
    assert processing_calls == []


async def test_rejections_do_not_log_tokens_secrets_or_signatures(
    client: httpx.AsyncClient,
    processing_calls: list[bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    supplied_signature = signature(RAW_BODY, secret="wrong-secret")

    get_response = await client.get(
        WEBHOOK_PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "logged-token-must-not-appear",
            "hub.challenge": "123",
        },
    )
    post_response = await client.post(
        WEBHOOK_PATH,
        content=RAW_BODY,
        headers={"X-Hub-Signature-256": supplied_signature},
    )

    assert get_response.status_code == 403
    assert post_response.status_code == 403
    assert processing_calls == []
    application_logs = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app.")
    )
    assert VERIFY_TOKEN not in application_logs
    assert APP_SECRET not in application_logs
    assert "logged-token-must-not-appear" not in application_logs
    assert supplied_signature not in application_logs
