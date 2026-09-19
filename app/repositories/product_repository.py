"""Read-only repository, indexes, and deterministic catalog search.

Search scoring uses mutually exclusive primary tiers so matches in lower-priority
fields cannot overtake a higher-priority match:

* exact normalized SKU: 600
* exact normalized name: 500
* normalized query phrase in the name: 400
* normalized query phrase in a keyword or application: 300
* normalized query phrase in the category or short description: 200
* token-only fallback: 1--199

For the fallback, each distinct query token earns the best applicable field weight:
name/SKU 4, keyword/application 3, and category/short description 2. The sum is
capped at 199. Equal scores are ordered by product ID.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable
from json import JSONDecodeError
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.core.config import get_settings
from app.core.exceptions import InvalidStaticDataError
from app.schemas.product import Product

_PRODUCT_CATALOG_ADAPTER = TypeAdapter(list[Product])

_EXACT_SKU_SCORE = 600
_EXACT_NAME_SCORE = 500
_NAME_PHRASE_SCORE = 400
_KEYWORD_APPLICATION_PHRASE_SCORE = 300
_CATEGORY_DESCRIPTION_PHRASE_SCORE = 200
_TOKEN_SCORE_CAP = 199
_NAME_SKU_TOKEN_SCORE = 4
_KEYWORD_APPLICATION_TOKEN_SCORE = 3
_CATEGORY_DESCRIPTION_TOKEN_SCORE = 2
_MAX_SEARCH_RESULTS = 5


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

    def search(self, query: str, category: str | None = None) -> list[Product]:
        """Return up to five ranked active matches, optionally within a category."""
        normalized_query = _normalize_search_text(query)
        if not normalized_query:
            return []

        if category is None:
            candidates = self._active_products
        else:
            normalized_category = _normalize_category(category)
            candidates = tuple(
                product
                for product in self._active_products
                if _normalize_category(product.category) == normalized_category
            )

        ranked_matches = (
            (score, product.id, product)
            for product in candidates
            if (score := _search_score(product, normalized_query)) > 0
        )
        ordered_matches = sorted(ranked_matches, key=lambda match: (-match[0], match[1]))
        return _copy_products(product for _, _, product in ordered_matches[:_MAX_SEARCH_RESULTS])


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


def _normalize_search_text(value: str) -> str:
    """Normalize compatibility forms, case, punctuation, and whitespace for search."""
    compatibility_normalized = unicodedata.normalize("NFKC", value).casefold()
    punctuation_normalized = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in compatibility_normalized
    )
    return " ".join(punctuation_normalized.split())


def _search_score(product: Product, normalized_query: str) -> int:
    """Score one active product using the documented, mutually exclusive tiers."""
    normalized_sku = _normalize_search_text(product.sku)
    normalized_name = _normalize_search_text(product.name)
    normalized_keywords = tuple(_normalize_search_text(value) for value in product.keywords)
    normalized_applications = tuple(_normalize_search_text(value) for value in product.applications)
    normalized_category = _normalize_search_text(product.category)
    normalized_short_description = _normalize_search_text(product.short_description)

    if normalized_query == normalized_sku:
        return _EXACT_SKU_SCORE
    if normalized_query == normalized_name:
        return _EXACT_NAME_SCORE
    if normalized_query in normalized_name:
        return _NAME_PHRASE_SCORE
    if _phrase_in_any(normalized_query, (*normalized_keywords, *normalized_applications)):
        return _KEYWORD_APPLICATION_PHRASE_SCORE
    if _phrase_in_any(
        normalized_query,
        (normalized_category, normalized_short_description),
    ):
        return _CATEGORY_DESCRIPTION_PHRASE_SCORE

    query_tokens = set(normalized_query.split())
    name_sku_tokens = set(normalized_name.split()) | set(normalized_sku.split())
    keyword_application_tokens = _tokens_from((*normalized_keywords, *normalized_applications))
    category_description_tokens = _tokens_from((normalized_category, normalized_short_description))

    token_score = sum(
        _best_token_score(
            token,
            name_sku_tokens,
            keyword_application_tokens,
            category_description_tokens,
        )
        for token in query_tokens
    )
    return min(token_score, _TOKEN_SCORE_CAP)


def _phrase_in_any(phrase: str, values: Iterable[str]) -> bool:
    return any(phrase in value for value in values)


def _tokens_from(values: Iterable[str]) -> set[str]:
    return {token for value in values for token in value.split()}


def _best_token_score(
    token: str,
    name_sku_tokens: set[str],
    keyword_application_tokens: set[str],
    category_description_tokens: set[str],
) -> int:
    if token in name_sku_tokens:
        return _NAME_SKU_TOKEN_SCORE
    if token in keyword_application_tokens:
        return _KEYWORD_APPLICATION_TOKEN_SCORE
    if token in category_description_tokens:
        return _CATEGORY_DESCRIPTION_TOKEN_SCORE
    return 0


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
