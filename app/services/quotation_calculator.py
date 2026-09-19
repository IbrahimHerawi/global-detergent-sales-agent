"""Deterministic quotation calculations from authoritative catalog data."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from app.core.exceptions import (
    CurrencyMismatchError,
    EmptyQuoteError,
    InvalidQuantityError,
    ProductNotFoundError,
)
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.product import Product
from app.schemas.quotation import QuoteCartItem, QuoteLine, QuoteTotals
from app.schemas.quotation_rules import QuotationRules

_CENT = Decimal("0.01")
_PERCENT = Decimal("100")
_ZERO = Decimal("0.00")


def money(value: Decimal) -> Decimal:
    """Round one monetary value to cents using commercial half-up rounding."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


class QuotationCalculator:
    """Resolve current products and rules, then calculate exact quote values."""

    __slots__ = ("_product_repository", "_rules_repository")

    def __init__(
        self,
        product_repository: ProductRepository | None = None,
        rules_repository: QuotationRulesRepository | None = None,
    ) -> None:
        self._product_repository = (
            product_repository if product_repository is not None else ProductRepository()
        )
        self._rules_repository = (
            rules_repository if rules_repository is not None else QuotationRulesRepository()
        )

    def calculate(
        self,
        cart: Sequence[QuoteCartItem],
    ) -> tuple[list[QuoteLine], QuoteTotals]:
        """Calculate lines and totals without accepting any commercial overrides."""
        if not cart:
            raise EmptyQuoteError("Cannot calculate an empty quotation cart")

        rules = self._rules_repository.get_rules()
        lines = [self._calculate_line(item, rules) for item in cart]
        subtotal = sum((line.subtotal for line in lines), start=_ZERO)
        discount = money(subtotal * rules.default_discount_percent / _PERCENT)
        taxable_amount = subtotal - discount
        tax = money(taxable_amount * rules.tax_percent / _PERCENT)
        total = money(taxable_amount + tax)

        return lines, QuoteTotals(
            subtotal=money(subtotal),
            discount=discount,
            tax=tax,
            total=total,
            currency=rules.currency,
        )

    def _calculate_line(self, item: QuoteCartItem, rules: QuotationRules) -> QuoteLine:
        quantity = item.quantity
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise InvalidQuantityError("Quantity must be a strict positive integer")

        product = self._current_active_product(item.product_id)
        if product.price.currency != rules.currency:
            raise CurrencyMismatchError(
                f"Product {product.id!r} uses {product.price.currency}; "
                f"quotation rules require {rules.currency}"
            )

        return QuoteLine(
            product_id=product.id,
            sku=product.sku,
            description=_product_description(product),
            quantity=quantity,
            unit_price=money(product.price.amount),
            subtotal=money(product.price.amount * Decimal(quantity)),
        )

    def _current_active_product(self, product_id: str) -> Product:
        if not isinstance(product_id, str) or not product_id.strip():
            raise ProductNotFoundError("Missing or invalid product ID")
        product = self._product_repository.get_by_id(product_id)
        if product is None or not product.active:
            raise ProductNotFoundError("Unknown or inactive product requested")
        return product


def _product_description(product: Product) -> str:
    """Build line text exclusively from the stored product and packaging."""
    return f"{product.name} {product.packaging.size} {product.packaging.unit}"


__all__ = ["QuotationCalculator", "money"]
