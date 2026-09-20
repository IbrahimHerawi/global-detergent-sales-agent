"""Compatibility import for the WhatsApp webhook route module."""

from app.api.routes.whatsapp import (
    META_WEBHOOK_ACK_TARGET_SECONDS,
    get_webhook_settings,
    receive_whatsapp_webhook,
    router,
    verify_whatsapp_webhook,
)

__all__ = [
    "META_WEBHOOK_ACK_TARGET_SECONDS",
    "get_webhook_settings",
    "receive_whatsapp_webhook",
    "router",
    "verify_whatsapp_webhook",
]
