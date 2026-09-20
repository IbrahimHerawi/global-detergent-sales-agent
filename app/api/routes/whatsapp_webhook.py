"""Authenticated Meta WhatsApp webhook ingress."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.services.whatsapp_webhook_security import (
    verify_webhook_signature,
    verify_webhook_subscription,
)

router = APIRouter(prefix="/api/v1/webhooks", tags=["whatsapp-webhook"])


def get_webhook_settings() -> Settings:
    """Expose webhook configuration as an overridable route dependency."""
    return get_settings()


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify_whatsapp_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_webhook_settings)],
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> PlainTextResponse:
    """Return Meta's exact challenge only for a valid subscription request."""
    _redact_verification_token_from_scope(request)
    configured_token = _secret_value(settings.whatsapp_verify_token)
    if not verify_webhook_subscription(
        mode,
        verify_token,
        challenge,
        configured_token,
    ):
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    assert challenge is not None
    return PlainTextResponse(challenge)


@router.post("/whatsapp", status_code=200)
async def receive_whatsapp_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_webhook_settings)],
) -> Response:
    """Authenticate the bounded raw payload before downstream processing."""
    raw_body = await _read_limited_body(request, settings.max_webhook_body_size)
    signature = request.headers.get("x-hub-signature-256")
    app_secret = _secret_value(settings.meta_app_secret)
    if not verify_webhook_signature(raw_body, signature, app_secret):
        raise HTTPException(status_code=403, detail="Webhook signature verification failed")

    await process_verified_whatsapp_webhook(raw_body)
    return Response(status_code=200)


async def process_verified_whatsapp_webhook(_raw_body: bytes) -> None:
    """Seam for the later parsing and business-orchestration task."""


async def _read_limited_body(request: Request, maximum_size: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = -1
        if declared_size > maximum_size:
            raise HTTPException(status_code=413, detail="Webhook request body is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_size:
            raise HTTPException(status_code=413, detail="Webhook request body is too large")
    return bytes(body)


def _secret_value(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def _redact_verification_token_from_scope(request: Request) -> None:
    """Prevent the query token from reaching server access-log request lines."""
    raw_query = request.scope.get("query_string")
    if not isinstance(raw_query, bytes) or not raw_query:
        return
    query = parse_qsl(raw_query.decode("utf-8", errors="replace"), keep_blank_values=True)
    if not any(name == "hub.verify_token" for name, _value in query):
        return
    request.scope["query_string"] = urlencode(
        [(name, "[REDACTED]" if name == "hub.verify_token" else value) for name, value in query]
    ).encode("ascii")


__all__ = [
    "get_webhook_settings",
    "process_verified_whatsapp_webhook",
    "receive_whatsapp_webhook",
    "router",
    "verify_whatsapp_webhook",
]
