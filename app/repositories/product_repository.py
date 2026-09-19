"""Read-only repository and indexes for validated product data."""

from __future__ import annotations

import json
from collections.abc import Iterable
from json import JSONDecodeError
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.core.config import get_settings
from app.core.exceptions import InvalidStaticDataError
from app.schemas.product import Product

_PRODUCT_CATALOG_ADAPTER = TypeAdapter(list[Product])


class ProductRepository:
    """Load one product catalog and expose defensive-copy lookups."""

    __slots__ = ("_active_products", "_products", "_products_by_id", "_products_by_sku")

    def __init__(self, path: Path | None = None) -> None:
        configured_path = get_settings().product_data_path if path is None else path
        products = _load_products(configured_path)

        products_by_id: dict[str, Product] = {}
        products_by_sku: dict[str, Product] = {}
        sku_indexes: dict[str, int] = {}

        for index, product in enumerate(products):
            if product.id in products_by_id:
                raise InvalidStaticDataError(
                    f"Duplicate product ID in '{configured_path}' at index {index}: '{product.id}'"
                )

            normalized_sku = _normalize_sku(product.sku)
            if normalized_sku in products_by_sku:
                first_index = sku_indexes[normalized_sku]
                raise InvalidStaticDataError(
                    f"Duplicate normalized product SKU in '{configured_path}' at indexes "
                    f"{first_index} and {index}: '{product.sku}'"
                )

            products_by_id[product.id] = product
            products_by_sku[normalized_sku] = product
            sku_indexes[normalized_sku] = index

        self._products = tuple(products)
        self._products_by_id = products_by_id
        self._products_by_sku = products_by_sku
        self._active_products = tuple(product for product in products if product.active)

    def get_all(self) -> list[Product]:
        """Return every catalog record, including inactive products."""
        return _copy_products(self._products)

    def get_active(self) -> list[Product]:
        """Return products from the cached active-products index."""
        return _copy_products(self._active_products)

    def get_by_id(self, product_id: str) -> Product | None:
        """Return an active or inactive product by its case-sensitive ID."""
        product = self._products_by_id.get(product_id.strip())
        return _copy_product(product)

    def get_by_sku(self, sku: str) -> Product | None:
        """Return an active or inactive product by normalized SKU."""
        product = self._products_by_sku.get(_normalize_sku(sku))
        return _copy_product(product)

    def get_by_category(self, category: str) -> list[Product]:
        """Return all products whose stored category matches case-insensitively."""
        normalized_category = _normalize_category(category)
        matches = (
            product
            for product in self._products
            if _normalize_category(product.category) == normalized_category
        )
        return _copy_products(matches)


def _load_products(path: Path) -> list[Product]:
    """Read and validate a product JSON array with safe diagnostics."""
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InvalidStaticDataError(
            f"Unable to read product catalog at '{path}': {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc

    try:
        raw_products = json.loads(document)
    except JSONDecodeError as exc:
        raise InvalidStaticDataError(
            (
                f"Malformed product catalog JSON at '{path}' "
                f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ),
            cause=exc,
        ) from exc

    try:
        return _PRODUCT_CATALOG_ADAPTER.validate_python(raw_products)
    except ValidationError as exc:
        diagnostics = "; ".join(
            f"{_format_location(error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise InvalidStaticDataError(
            f"Invalid product catalog at '{path}': {diagnostics}",
            cause=exc,
        ) from exc


def _normalize_sku(sku: str) -> str:
    return sku.strip().casefold()


def _normalize_category(category: str) -> str:
    return category.strip().casefold()


def _copy_product(product: Product | None) -> Product | None:
    if product is None:
        return None
    return product.model_copy(deep=True)


def _copy_products(products: Iterable[Product]) -> list[Product]:
    return [product.model_copy(deep=True) for product in products]


def _format_location(location: tuple[int | str, ...]) -> str:
    if not location:
        return "$"
    return ".".join(str(part) for part in location)
