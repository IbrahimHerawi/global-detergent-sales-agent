"""Authenticated and durably processed Meta WhatsApp webhook ingress."""

from __future__ import annotations

import json
import logging
from json import JSONDecodeError
from time import perf_counter
from typing import Annotated, Final
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import SecretStr

from app.api.dependencies import get_whatsapp_processing_coordinator
from app.core.config import Settings, get_settings
from app.core.exceptions import TechnicalError
from app.services.whatsapp_parser import parse_whatsapp_webhook
from app.services.whatsapp_processing_coordinator import WhatsAppProcessingCoordinator
from app.services.whatsapp_webhook_security import (
    verify_webhook_signature,
    verify_webhook_subscription,
)

META_WEBHOOK_ACK_TARGET_SECONDS: Final = 20.0
RETRY_AFTER_SECONDS: Final = 1

logger = logging.getLogger(__name__)
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
    if not verify_webhook_subscription(mode, verify_token, challenge, configured_token):
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    assert challenge is not None
    return PlainTextResponse(challenge)


@router.post("/whatsapp", status_code=200)
async def receive_whatsapp_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_webhook_settings)],
    coordinator: Annotated[
        WhatsAppProcessingCoordinator | None,
        Depends(get_whatsapp_processing_coordinator),
    ],
) -> Response:
    """Authenticate, parse, synchronously process, then acknowledge the webhook."""
    started_at = perf_counter()
    response_status = 500
    result_count = 0
    try:
        raw_body = await _read_limited_body(request, settings.max_webhook_body_size)
        signature = request.headers.get("x-hub-signature-256")
        app_secret = _secret_value(settings.meta_app_secret)
        if not verify_webhook_signature(raw_body, signature, app_secret):
            response_status = 403
            raise HTTPException(
                status_code=403,
                detail="Webhook signature verification failed",
            )

        try:
            payload = json.loads(raw_body)
        except (JSONDecodeError, UnicodeDecodeError):
            response_status = 400
            raise HTTPException(status_code=400, detail="Invalid webhook JSON") from None

        parsed = parse_whatsapp_webhook(
            payload,
            max_text_length=settings.max_inbound_text_length,
        )
        actionable = sum(outcome.message is not None for outcome in parsed.outcomes)
        if actionable:
            if coordinator is None:
                raise TechnicalError("WhatsApp processing is not configured")
            result_count = len(await coordinator.process_webhook(parsed))

        response_status = 200
        return Response(status_code=200)
    except HTTPException as error:
        response_status = error.status_code
        raise
    except TechnicalError as error:
        response_status = 503
        public_error = error.to_public_dict()
        logger.error(
            "whatsapp_webhook_processing_failed",
            extra={"error_code": error.code},
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": public_error,
                "request_id": getattr(request.state, "request_id", None),
            },
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    finally:
        duration = perf_counter() - started_at
        log_method = logger.warning if duration > META_WEBHOOK_ACK_TARGET_SECONDS else logger.info
        log_method(
            "whatsapp_webhook_acknowledgement",
            extra={
                "duration": round(duration, 6),
                "status": str(response_status),
                "result_count": result_count,
            },
        )


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
    "META_WEBHOOK_ACK_TARGET_SECONDS",
    "get_webhook_settings",
    "receive_whatsapp_webhook",
    "router",
    "verify_whatsapp_webhook",
]
