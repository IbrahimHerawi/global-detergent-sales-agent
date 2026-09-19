"""Safe product browsing, details, search, and catalog-only recommendations."""

from __future__ import annotations

from typing import TypedDict

from app.core.exceptions import ProductNotFoundError
from app.repositories.product_repository import ProductRepository
from app.schemas.product import Product


class ProductSummary(TypedDict):
    """Stable JSON-compatible product summary."""

    product_id: str
    sku: str
    name: str
    category: str
    short_description: str


class ProductListOutput(TypedDict):
    """Stable product-browsing output."""

    products: list[ProductSummary]


class ProductSearchOutput(TypedDict):
    """Stable product-search output."""

    matches: list[ProductSummary]


class ProductRecommendationOutput(TypedDict):
    """Stable catalog-only recommendation output."""

    recommendations: list[ProductSummary]


class PackagingOutput(TypedDict):
    """Stable packaging output."""

    size: str
    unit: str


class PriceOutput(TypedDict):
    """Stable price output with a JSON-safe decimal string."""

    amount: str
    currency: str


class ProductTechnicalDetailsOutput(TypedDict):
    """Stable optional technical-detail output."""

    color: str | None
    fragrance: str | None
    ph: str | None


class ProductDetailsOutput(TypedDict):
    """Stable output containing every stored product field."""

    product_id: str
    sku: str
    name: str
    category: str
    short_description: str
    description: str
    applications: list[str]
    packaging: PackagingOutput
    price: PriceOutput
    instructions: str | None
    safety_information: str | None
    technical_details: ProductTechnicalDetailsOutput
    active_ingredients: list[str]
    recommended_surfaces: list[str]
    contact_time: str | None
    dilution_ratio: str | None
    certifications: list[str]
    approved_claims: list[str]
    keywords: list[str]
    active: bool


class ProductService:
    """Expose active catalog products without adding unstored product claims."""

    __slots__ = ("_repository",)

    def __init__(self, repository: ProductRepository | None = None) -> None:
        self._repository = repository if repository is not None else ProductRepository()

    def list_products(self, category: str | None = None) -> ProductListOutput:
        """List active products, optionally within one stored category."""
        products = (
            self._repository.get_active()
            if category is None
            else [
                product for product in self._repository.get_by_category(category) if product.active
            ]
        )
        return {"products": [_product_summary(product) for product in products]}

    def search_products(self, query: str) -> ProductSearchOutput:
        """Return deterministic repository search results as concise summaries."""
        return {
            "matches": [_product_summary(product) for product in self._repository.search(query)]
        }

    def get_product_details(self, product_id: str) -> ProductDetailsOutput:
        """Return every stored fact for one active product."""
        product = self._repository.get_by_id(product_id)
        if product is None or not product.active:
            raise ProductNotFoundError("Unknown or inactive product requested")
        return _product_details(product)

    def recommend_products(self, requirement: str) -> ProductRecommendationOutput:
        """Recommend only deterministic search matches, without inferred suitability claims."""
        products = self._repository.search(requirement)
        return {"recommendations": [_product_summary(product) for product in products]}


def _product_summary(product: Product) -> ProductSummary:
    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "short_description": product.short_description,
    }


def _product_details(product: Product) -> ProductDetailsOutput:
    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "short_description": product.short_description,
        "description": product.description,
        "applications": list(product.applications),
        "packaging": {
            "size": product.packaging.size,
            "unit": product.packaging.unit,
        },
        "price": {
            "amount": str(product.price.amount),
            "currency": product.price.currency,
        },
        "instructions": product.instructions,
        "safety_information": product.safety_information,
        "technical_details": {
            "color": product.technical_details.color,
            "fragrance": product.technical_details.fragrance,
            "ph": product.technical_details.ph,
        },
        "active_ingredients": list(product.active_ingredients),
        "recommended_surfaces": list(product.recommended_surfaces),
        "contact_time": product.contact_time,
        "dilution_ratio": product.dilution_ratio,
        "certifications": list(product.certifications),
        "approved_claims": list(product.approved_claims),
        "keywords": list(product.keywords),
        "active": product.active,
    }
