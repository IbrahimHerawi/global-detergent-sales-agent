"""API route package."""

from app.api.routes.dev_chat import router as dev_chat_router
from app.api.routes.health import router as health_router
from app.api.routes.products import router as products_router
from app.api.routes.quotations import router as quotations_router
from app.api.routes.whatsapp import router as whatsapp_webhook_router

__all__ = [
    "dev_chat_router",
    "health_router",
    "products_router",
    "quotations_router",
    "whatsapp_webhook_router",
]
