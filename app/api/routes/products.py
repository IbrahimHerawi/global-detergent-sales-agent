"""Internal, non-production product inspection endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_product_service
from app.services.product_service import (
    ProductDetailsOutput,
    ProductListOutput,
    ProductService,
)

router = APIRouter(prefix="/api/v1/products", tags=["development-products"])

OptionalFilter = Annotated[str | None, Query(min_length=1, max_length=200)]


@router.get("")
async def list_products(
    product_service: Annotated[ProductService, Depends(get_product_service)],
    category: OptionalFilter = None,
    search: OptionalFilter = None,
) -> ProductListOutput:
    """List active summaries, filtering candidates before ranked selection."""
    if search is None:
        return product_service.list_products(category)
    matches = product_service.search_products(search, category)
    return {"products": matches["matches"]}


@router.get("/{product_id}")
async def get_product(
    product_id: str,
    product_service: Annotated[ProductService, Depends(get_product_service)],
) -> ProductDetailsOutput:
    """Return all stored, customer-safe facts for one active product."""
    return product_service.get_product_details(product_id)


__all__ = ["router"]
