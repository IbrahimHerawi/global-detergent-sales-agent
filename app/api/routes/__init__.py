"""API route package."""

from app.api.routes.health import router as health_router
from app.api.routes.products import router as products_router

__all__ = ["health_router", "products_router"]
