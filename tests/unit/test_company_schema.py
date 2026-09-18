"""Tests for company and nested contact schemas."""

import json

import pytest
from app.schemas.company import Company, CompanyContact
from pydantic import ValidationError


def company_payload() -> dict[str, object]:
    """Return the documented company shape after flat contact normalization."""
    return {
        "name": "Global Detergent Factory",
        "short_name": "GDF",
        "description": "Manufacturer of detergent and cleaning products.",
        "industries_served": ["Hospitality", "Healthcare"],
        "product_categories": ["Laundry", "Household Cleaning"],
        "currency": " qar ",
        "contact": {
            "phone": None,
            "email": None,
            "website": None,
            "address": None,
        },
    }


def test_documented_company_shape_validates_with_nested_contact() -> None:
    company = Company.model_validate(company_payload())

    assert company.name == "Global Detergent Factory"
    assert company.currency == "QAR"
    assert company.contact == CompanyContact()


@pytest.mark.parametrize("field", ["name", "short_name", "description", "currency", "contact"])
def test_required_company_fields_cannot_be_omitted(field: str) -> None:
    payload = company_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        Company.model_validate(payload)


@pytest.mark.parametrize("field", ["name", "short_name", "description"])
def test_required_company_text_must_be_nonblank(field: str) -> None:
    payload = company_payload()
    payload[field] = "   "

    with pytest.raises(ValidationError):
        Company.model_validate(payload)


@pytest.mark.parametrize("currency", ["US", "USDD", "12A", "   "])
def test_currency_must_be_a_three_letter_code(currency: str) -> None:
    payload = company_payload()
    payload["currency"] = currency

    with pytest.raises(ValidationError):
        Company.model_validate(payload)


def test_list_defaults_are_independent() -> None:
    first_payload = company_payload()
    second_payload = company_payload()
    del first_payload["industries_served"]
    del first_payload["product_categories"]
    del second_payload["industries_served"]
    del second_payload["product_categories"]

    first = Company.model_validate(first_payload)
    second = Company.model_validate(second_payload)
    first.industries_served.append("Food service")
    first.product_categories.append("Dishwashing")

    assert second.industries_served == []
    assert second.product_categories == []


@pytest.mark.parametrize(
    "payload",
    [
        {**company_payload(), "unexpected": "value"},
        {**company_payload(), "contact": {"fax": "+974 0000 0000"}},
    ],
)
def test_unknown_fields_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Company.model_validate(payload)


def test_nullable_contact_information_survives_json_round_trip() -> None:
    original = Company.model_validate(company_payload())

    restored = Company.model_validate_json(original.model_dump_json())
    plain_json = json.loads(restored.model_dump_json())

    assert restored == original
    assert plain_json["contact"] == {
        "phone": None,
        "email": None,
        "website": None,
        "address": None,
    }


def test_flat_contact_fields_are_not_accepted_on_company() -> None:
    payload = company_payload()
    payload["phone"] = None

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Company.model_validate(payload)
