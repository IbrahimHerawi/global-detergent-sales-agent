"""Tests for complete product catalog schemas."""

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.product import Packaging, Price, Product, ProductTechnicalDetails


def product_payload() -> dict[str, object]:
    """Return a fully populated Technical Plan section 8 product."""
    return {
        "id": "GDF-FLC-001",
        "sku": "FLC-5L",
        "name": "GDF Floor Cleaner",
        "category": "Floor Cleaning",
        "short_description": "Floor cleaning solution.",
        "description": "Floor cleaning solution for general cleaning applications.",
        "applications": ["Office floors", "Commercial facilities"],
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "25.00", "currency": "QAR"},
        "instructions": "Follow the supplied label instructions.",
        "safety_information": "Use according to the supplied safety information.",
        "technical_details": {
            "color": "Blue",
            "fragrance": "Fresh",
            "ph": "7",
        },
        "active_ingredients": ["Catalog-supplied ingredient"],
        "recommended_surfaces": ["Catalog-supplied surface"],
        "contact_time": "Catalog-supplied contact time",
        "dilution_ratio": "Catalog-supplied dilution ratio",
        "certifications": ["Catalog-supplied certification"],
        "approved_claims": ["Catalog-supplied claim"],
        "keywords": ["floor", "cleaner"],
        "active": True,
    }


def test_complete_documented_product_serializes_and_validates() -> None:
    product = Product.model_validate(product_payload())
    serialized = json.loads(product.model_dump_json())

    assert product.packaging == Packaging(size="5", unit="L")
    assert product.price == Price(amount=Decimal("25.00"), currency="QAR")
    assert product.technical_details == ProductTechnicalDetails(
        color="Blue", fragrance="Fresh", ph="7"
    )
    assert serialized == product_payload()
    assert serialized["price"]["amount"] == "25.00"


@pytest.mark.parametrize("amount", ["-0.01", "NaN", "Infinity", "-Infinity", "not-money"])
def test_invalid_decimal_price_strings_are_rejected(amount: str) -> None:
    with pytest.raises(ValidationError):
        Price.model_validate({"amount": amount, "currency": "QAR"})


@pytest.mark.parametrize("amount", [25.0, float("nan"), float("inf"), True, False])
def test_float_and_boolean_prices_are_rejected(amount: object) -> None:
    with pytest.raises(ValidationError):
        Price.model_validate({"amount": amount, "currency": "QAR"})


def test_decimal_and_decimal_string_prices_are_accepted() -> None:
    assert Price.model_validate({"amount": "0", "currency": "QAR"}).amount == Decimal("0")
    assert Price(amount=Decimal("25.00"), currency="QAR").amount == Decimal("25.00")


@pytest.mark.parametrize("currency", ["US", "USDD", "12A", "", True])
def test_currency_must_be_a_three_letter_code(currency: object) -> None:
    with pytest.raises(ValidationError):
        Price.model_validate({"amount": "1.00", "currency": currency})


def test_currency_is_serialized_in_uppercase() -> None:
    price = Price.model_validate({"amount": "1.00", "currency": " qar "})

    assert price.currency == "QAR"


@pytest.mark.parametrize("field", ["id", "sku", "name", "category"])
def test_required_product_identity_fields_must_be_nonblank(field: str) -> None:
    payload = product_payload()
    payload[field] = "   "

    with pytest.raises(ValidationError):
        Product.model_validate(payload)


@pytest.mark.parametrize("field", ["size", "unit"])
def test_packaging_fields_must_be_nonblank(field: str) -> None:
    payload = product_payload()
    original_packaging = payload["packaging"]
    assert isinstance(original_packaging, dict)
    packaging = dict(original_packaging)
    packaging[field] = "   "
    payload["packaging"] = packaging

    with pytest.raises(ValidationError):
        Product.model_validate(payload)


@pytest.mark.parametrize("field", ["short_description", "description"])
def test_active_product_descriptions_must_be_nonblank(field: str) -> None:
    payload = product_payload()
    payload[field] = "   "

    with pytest.raises(ValidationError):
        Product.model_validate(payload)


def test_inactive_product_can_retain_blank_descriptions() -> None:
    payload = product_payload()
    payload["short_description"] = "   "
    payload["description"] = ""
    payload["active"] = False

    product = Product.model_validate(payload)

    assert product.short_description == ""
    assert product.description == ""


def test_unknown_technical_information_defaults_without_inference() -> None:
    payload = product_payload()
    product_name = "Certified Lemon Disinfectant 30-second Concentrate"
    payload["name"] = product_name
    for field in (
        "applications",
        "instructions",
        "safety_information",
        "technical_details",
        "active_ingredients",
        "recommended_surfaces",
        "contact_time",
        "dilution_ratio",
        "certifications",
        "approved_claims",
        "keywords",
        "active",
    ):
        del payload[field]

    product = Product.model_validate(payload)

    assert product.name == product_name
    assert product.instructions is None
    assert product.safety_information is None
    assert product.technical_details == ProductTechnicalDetails()
    assert product.active_ingredients == []
    assert product.recommended_surfaces == []
    assert product.contact_time is None
    assert product.dilution_ratio is None
    assert product.certifications == []
    assert product.approved_claims == []
    assert product.applications == []
    assert product.keywords == []
    assert product.active is True


def test_default_lists_are_independent() -> None:
    first_payload = product_payload()
    second_payload = product_payload()
    for payload in (first_payload, second_payload):
        for field in (
            "applications",
            "active_ingredients",
            "recommended_surfaces",
            "certifications",
            "approved_claims",
            "keywords",
        ):
            del payload[field]

    first = Product.model_validate(first_payload)
    second = Product.model_validate(second_payload)
    first.keywords.append("new")

    assert second.keywords == []


@pytest.mark.parametrize(
    "payload",
    [
        {**product_payload(), "unexpected": "value"},
        {**product_payload(), "packaging": {"size": "5", "unit": "L", "count": 1}},
        {**product_payload(), "price": {"amount": "25.00", "currency": "QAR", "tax": 0}},
        {
            **product_payload(),
            "technical_details": {
                "color": None,
                "fragrance": None,
                "ph": None,
                "viscosity": None,
            },
        },
    ],
)
def test_unexpected_fields_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Product.model_validate(payload)
