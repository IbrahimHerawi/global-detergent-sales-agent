"""Tests for customer, cart, preview, and generated-quotation schemas."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.quotation import (
    CustomerInfo,
    GeneratedQuotation,
    QuoteCartItem,
    QuoteLine,
    QuotePreview,
    QuoteTotals,
)


def preview_payload() -> dict[str, object]:
    """Return a complete preview using exact decimal strings."""
    return {
        "customer": {"phone": "+97450000000", "company_name": "Example LLC"},
        "items": [
            {
                "product_id": "GDF-FLC-001",
                "sku": "FLC-5L",
                "description": "Floor cleaner, 5 L",
                "quantity": 3,
                "unit_price": "25.10",
                "subtotal": "75.30",
            }
        ],
        "totals": {
            "subtotal": "75.30",
            "discount": "0.00",
            "tax": "3.765",
            "total": "79.065",
            "currency": "QAR",
        },
        "validity_days": 14,
        "terms": ["Payment due on receipt."],
    }


def test_incomplete_customer_is_valid_before_readiness_check() -> None:
    customer = CustomerInfo(phone="+97450000000")

    assert customer.name is None
    assert customer.company_name is None
    assert customer.contact_person is None
    assert customer.notes is None


def test_customer_supports_all_optional_contact_fields() -> None:
    customer = CustomerInfo(
        phone="+97450000000",
        name="Aisha",
        company_name="Example LLC",
        email="aisha@example.com",
        address="Doha",
        contact_person="Omar",
        notes="Deliver in the morning.",
    )

    assert customer.contact_person == "Omar"
    assert customer.notes == "Deliver in the morning."


def test_customer_phone_is_required() -> None:
    with pytest.raises(ValidationError):
        CustomerInfo.model_validate({})


@pytest.mark.parametrize("quantity", [True, False, 1.0, 1.5, "1", Decimal("1")])
def test_cart_quantity_rejects_non_strict_integers(quantity: object) -> None:
    with pytest.raises(ValidationError):
        QuoteCartItem(product_id="GDF-FLC-001", quantity=quantity)  # type: ignore[arg-type]


@pytest.mark.parametrize("quantity", [0, -1])
def test_cart_quantity_must_be_positive(quantity: int) -> None:
    with pytest.raises(ValidationError):
        QuoteCartItem(product_id="GDF-FLC-001", quantity=quantity)


def test_cart_item_contains_only_product_id_and_quantity() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QuoteCartItem.model_validate(
            {"product_id": "GDF-FLC-001", "quantity": 1, "unit_price": "25.00"}
        )


def test_preview_json_round_trip_preserves_decimal_precision() -> None:
    preview = QuotePreview.model_validate(preview_payload())
    encoded = preview.model_dump_json()
    decoded = json.loads(encoded)
    restored = QuotePreview.model_validate_json(encoded)

    assert decoded["items"][0]["unit_price"] == "25.10"
    assert decoded["totals"]["tax"] == "3.765"
    assert restored == preview
    assert restored.items[0].unit_price == Decimal("25.10")
    assert restored.totals.total == Decimal("79.065")


@pytest.mark.parametrize("amount", [True, False, 1.5, float("nan"), "NaN", "Infinity", "-0.01"])
def test_invalid_money_values_are_rejected(amount: object) -> None:
    payload = preview_payload()
    totals = payload["totals"]
    assert isinstance(totals, dict)
    totals["total"] = amount

    with pytest.raises(ValidationError):
        QuotePreview.model_validate(payload)


def test_quote_line_quantity_is_also_strict() -> None:
    with pytest.raises(ValidationError):
        QuoteLine.model_validate(
            {
                "product_id": "GDF-FLC-001",
                "sku": "FLC-5L",
                "description": "Floor cleaner, 5 L",
                "quantity": "3",
                "unit_price": "25.10",
                "subtotal": "75.30",
            }
        )


def test_generated_quotation_requires_aware_issued_at() -> None:
    payload = preview_payload()
    payload.update(
        {
            "quotation_id": "GDF-Q-20260919-001",
            "issued_at": datetime(2026, 9, 19, 12, 0),
            "valid_until": date(2026, 10, 3),
        }
    )

    with pytest.raises(ValidationError, match="timezone-aware"):
        GeneratedQuotation.model_validate(payload)


def test_generated_quotation_has_internal_non_input_pdf_path() -> None:
    payload = preview_payload()
    payload.update(
        {
            "quotation_id": "GDF-Q-20260919-001",
            "issued_at": datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            "valid_until": date(2026, 10, 3),
        }
    )
    quotation = GeneratedQuotation.with_pdf_path(pdf_path="/quotes/q-001.pdf", **payload)

    assert quotation.pdf_path == "/quotes/q-001.pdf"
    assert "pdf_path" not in quotation.model_dump()
    assert "pdf_path" not in json.loads(quotation.model_dump_json())

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GeneratedQuotation.model_validate({**payload, "pdf_path": "/untrusted/path.pdf"})


def test_documented_models_construct_with_expected_types() -> None:
    preview = QuotePreview.model_validate(preview_payload())

    assert preview.customer == CustomerInfo(phone="+97450000000", company_name="Example LLC")
    assert preview.items[0] == QuoteLine(
        product_id="GDF-FLC-001",
        sku="FLC-5L",
        description="Floor cleaner, 5 L",
        quantity=3,
        unit_price=Decimal("25.10"),
        subtotal=Decimal("75.30"),
    )
    assert preview.totals == QuoteTotals(
        subtotal=Decimal("75.30"),
        discount=Decimal("0.00"),
        tax=Decimal("3.765"),
        total=Decimal("79.065"),
        currency="QAR",
    )
