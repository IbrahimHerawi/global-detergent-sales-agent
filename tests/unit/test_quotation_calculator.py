"""Tests for repository-authoritative Decimal quotation calculations."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.exceptions import (
    CurrencyMismatchError,
    EmptyQuoteError,
    InvalidQuantityError,
    ProductNotFoundError,
)
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.quotation import QuoteCartItem
from app.services.quotation_calculator import QuotationCalculator, money


def product_payload(
    product_id: str,
    *,
    name: str,
    amount: str,
    currency: str = "QAR",
    size: str = "5",
    unit: str = "L",
    active: bool = True,
) -> dict[str, object]:
    return {
        "id": product_id,
        "sku": f"SKU-{product_id}",
        "name": name,
        "category": "Development Cleaning",
        "short_description": "Synthetic calculator-test product.",
        "description": "Synthetic product used only for calculator tests.",
        "packaging": {"size": size, "unit": unit},
        "price": {"amount": amount, "currency": currency},
        "active": active,
    }


def calculator(
    tmp_path: Path,
    products: list[dict[str, object]],
    *,
    discount: str = "0.00",
    tax: str = "0.00",
    currency: str = "QAR",
) -> QuotationCalculator:
    product_path = tmp_path / "products.json"
    product_path.write_text(json.dumps(products), encoding="utf-8")
    rules_path = tmp_path / "quotation_rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "currency": currency,
                "quotation_prefix": "GDF-Q",
                "validity_days": 30,
                "default_discount_percent": discount,
                "tax_percent": tax,
                "terms": ["Development-only terms."],
            }
        ),
        encoding="utf-8",
    )
    return QuotationCalculator(
        ProductRepository(product_path),
        QuotationRulesRepository(rules_path),
    )


def test_twenty_units_at_twenty_five_qar_yields_five_hundred(tmp_path: Path) -> None:
    quote_calculator = calculator(
        tmp_path,
        [product_payload("FLOOR", name="Floor Cleaner", amount="25.00")],
    )

    lines, totals = quote_calculator.calculate([QuoteCartItem(product_id="FLOOR", quantity=20)])

    assert lines[0].unit_price == Decimal("25.00")
    assert lines[0].subtotal == Decimal("500.00")
    assert totals.subtotal == Decimal("500.00")
    assert totals.discount == totals.tax == Decimal("0.00")
    assert totals.total == Decimal("500.00")
    assert totals.currency == "QAR"


def test_multiple_fractional_lines_are_rounded_before_subtotal(tmp_path: Path) -> None:
    quote_calculator = calculator(
        tmp_path,
        [
            product_payload("A", name="Cleaner A", amount="1.005"),
            product_payload("B", name="Cleaner B", amount="2.335", size="500", unit="mL"),
        ],
    )

    lines, totals = quote_calculator.calculate(
        [
            QuoteCartItem(product_id="A", quantity=1),
            QuoteCartItem(product_id="B", quantity=3),
        ]
    )

    assert [line.subtotal for line in lines] == [Decimal("1.01"), Decimal("7.01")]
    assert totals.subtotal == Decimal("8.02")
    assert totals.total == Decimal("8.02")


def test_half_cent_values_round_up(tmp_path: Path) -> None:
    assert money(Decimal("10.005")) == Decimal("10.01")
    assert money(Decimal("10.004")) == Decimal("10.00")

    quote_calculator = calculator(
        tmp_path,
        [product_payload("HALF", name="Half Cent", amount="0.005")],
    )
    lines, _ = quote_calculator.calculate([QuoteCartItem(product_id="HALF", quantity=1)])
    assert lines[0].subtotal == Decimal("0.01")


def test_discount_is_applied_before_tax_and_each_boundary_is_rounded(tmp_path: Path) -> None:
    quote_calculator = calculator(
        tmp_path,
        [product_payload("A", name="Cleaner A", amount="33.35")],
        discount="10",
        tax="5",
    )

    _, totals = quote_calculator.calculate([QuoteCartItem(product_id="A", quantity=3)])

    assert totals.subtotal == Decimal("100.05")
    assert totals.discount == Decimal("10.01")
    assert totals.tax == Decimal("4.50")
    assert totals.total == Decimal("94.54")


def test_zero_priced_product_is_valid_and_description_uses_stored_fields(
    tmp_path: Path,
) -> None:
    quote_calculator = calculator(
        tmp_path,
        [product_payload("ZERO", name="Stored Product Name", amount="0", size="750", unit="mL")],
    )

    lines, totals = quote_calculator.calculate([QuoteCartItem(product_id="ZERO", quantity=2)])

    assert lines[0].description == "Stored Product Name 750 mL"
    assert lines[0].unit_price == Decimal("0.00")
    assert lines[0].subtotal == Decimal("0.00")
    assert totals.total == Decimal("0.00")


def test_currency_mismatch_is_rejected(tmp_path: Path) -> None:
    quote_calculator = calculator(
        tmp_path,
        [product_payload("USD", name="Wrong Currency", amount="1.00", currency="USD")],
    )

    with pytest.raises(CurrencyMismatchError):
        quote_calculator.calculate([QuoteCartItem(product_id="USD", quantity=1)])


def test_empty_cart_is_rejected(tmp_path: Path) -> None:
    quote_calculator = calculator(
        tmp_path,
        [product_payload("A", name="Cleaner A", amount="1.00")],
    )

    with pytest.raises(EmptyQuoteError):
        quote_calculator.calculate([])


@pytest.mark.parametrize("product_id", ["UNKNOWN", "INACTIVE"])
def test_unknown_or_inactive_products_are_rejected(tmp_path: Path, product_id: str) -> None:
    quote_calculator = calculator(
        tmp_path,
        [
            product_payload("ACTIVE", name="Active", amount="1.00"),
            product_payload("INACTIVE", name="Inactive", amount="1.00", active=False),
        ],
    )

    with pytest.raises(ProductNotFoundError):
        quote_calculator.calculate([QuoteCartItem(product_id=product_id, quantity=1)])


def test_invalid_constructed_quantity_is_rejected(tmp_path: Path) -> None:
    quote_calculator = calculator(
        tmp_path,
        [product_payload("A", name="Cleaner A", amount="1.00")],
    )
    invalid_item = QuoteCartItem.model_construct(product_id="A", quantity=0)

    with pytest.raises(InvalidQuantityError):
        quote_calculator.calculate([invalid_item])


def test_calculator_api_has_no_price_or_rule_override_arguments() -> None:
    argument_names = QuotationCalculator.calculate.__annotations__

    assert set(argument_names) == {"cart", "return"}
