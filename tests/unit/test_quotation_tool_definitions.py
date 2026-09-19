"""Tests for strict quotation tool schemas and backend-authority boundaries."""

from collections.abc import Mapping
from typing import Any, cast

import pytest
from app.agent.tool_definitions import (
    COMPANY_PRODUCT_TOOL_DEFINITIONS,
    QUOTATION_TOOL_DEFINITIONS,
    QUOTATION_TOOL_INPUT_MODELS,
    TOOL_DEFINITIONS,
    TOOL_INPUT_MODELS,
    AddQuoteItemInput,
    ConfirmQuotationInput,
    CreateQuotationInput,
    GetQuoteSummaryInput,
    PrepareQuotationInput,
    RemoveQuoteItemInput,
    SetCustomerInformationInput,
    ToolInputModel,
    UpdateQuoteItemQuantityInput,
)
from pydantic import ValidationError

EXPECTED_QUOTATION_TOOL_NAMES = (
    "add_quote_item",
    "update_quote_item_quantity",
    "remove_quote_item",
    "get_quote_summary",
    "set_customer_information",
    "prepare_quotation",
    "confirm_quotation",
    "create_quotation",
)
CUSTOMER_FIELDS = (
    "name",
    "company_name",
    "contact_person",
    "email",
    "address",
    "notes",
)
BACKEND_AUTHORITY_FIELDS = (
    "phone",
    "price",
    "unit_price",
    "subtotal",
    "total",
    "discount",
    "discount_rate",
    "tax",
    "state",
    "quote_confirmed",
    "confirmation_turn_id",
    "preview_fingerprint",
    "quotation_id",
    "pdf_path",
    "file_path",
    "filename",
)


def _tool_by_name(name: str) -> Mapping[str, object]:
    return next(tool for tool in QUOTATION_TOOL_DEFINITIONS if tool["name"] == name)


def _assert_strict_object_schema(schema: Mapping[str, Any]) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        assert set(schema.get("required", [])) == set(properties)
        for property_schema in properties.values():
            assert isinstance(property_schema, dict)
            _assert_strict_object_schema(property_schema)

    alternatives = schema.get("anyOf", [])
    assert isinstance(alternatives, list)
    for alternative in alternatives:
        assert isinstance(alternative, dict)
        _assert_strict_object_schema(alternative)


def _customer_payload(
    fields_to_update: list[str],
    **updates: str | None,
) -> dict[str, object]:
    return {
        "fields_to_update": fields_to_update,
        **{field_name: updates.get(field_name) for field_name in CUSTOMER_FIELDS},
    }


def test_exact_quotation_tools_extend_the_existing_tool_collections() -> None:
    assert tuple(tool["name"] for tool in QUOTATION_TOOL_DEFINITIONS) == (
        EXPECTED_QUOTATION_TOOL_NAMES
    )
    assert tuple(QUOTATION_TOOL_INPUT_MODELS) == EXPECTED_QUOTATION_TOOL_NAMES
    assert TOOL_DEFINITIONS == COMPANY_PRODUCT_TOOL_DEFINITIONS + QUOTATION_TOOL_DEFINITIONS
    assert tuple(TOOL_INPUT_MODELS) == tuple(tool["name"] for tool in TOOL_DEFINITIONS)


def test_every_quotation_tool_has_strict_parameters_and_documented_output() -> None:
    for tool in QUOTATION_TOOL_DEFINITIONS:
        assert set(tool) == {"type", "name", "description", "parameters", "strict"}
        assert tool["type"] == "function"
        assert tool["strict"] is True
        assert "Returns" in tool["description"]
        parameters = tool["parameters"]
        assert parameters["type"] == "object"
        _assert_strict_object_schema(parameters)


@pytest.mark.parametrize("model", [AddQuoteItemInput, UpdateQuoteItemQuantityInput])
def test_cart_quantity_tools_accept_strict_positive_integers(
    model: type[ToolInputModel],
) -> None:
    validated = model.model_validate({"product_id": "  GDF-FLOOR-001  ", "quantity": 3})

    assert validated.model_dump() == {"product_id": "GDF-FLOOR-001", "quantity": 3}


@pytest.mark.parametrize("quantity", [0, -1, 1.0, "1", True, None])
@pytest.mark.parametrize("model", [AddQuoteItemInput, UpdateQuoteItemQuantityInput])
def test_cart_quantity_tools_reject_nonpositive_or_nonstrict_values(
    model: type[ToolInputModel],
    quantity: object,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"product_id": "GDF-FLOOR-001", "quantity": quantity})


def test_remove_quote_item_accepts_only_a_bounded_product_id() -> None:
    assert (
        RemoveQuoteItemInput.model_validate({"product_id": "  GDF-FLOOR-001  "}).product_id
        == "GDF-FLOOR-001"
    )

    for product_id in ("", "   ", "x" * 101, 1, True, None):
        with pytest.raises(ValidationError):
            RemoveQuoteItemInput.model_validate({"product_id": product_id})


def test_customer_update_uses_required_nullable_fields_and_explicit_intent() -> None:
    parameters = cast(dict[str, Any], _tool_by_name("set_customer_information")["parameters"])
    properties = cast(dict[str, dict[str, Any]], parameters["properties"])

    assert parameters["required"] == ["fields_to_update", *CUSTOMER_FIELDS]
    assert set(properties) == {"fields_to_update", *CUSTOMER_FIELDS}
    for field_name in CUSTOMER_FIELDS:
        assert properties[field_name]["anyOf"][-1] == {"type": "null"}

    validated = SetCustomerInformationInput.model_validate(
        _customer_payload(
            ["name", "email"],
            name="  Updated Customer  ",
            email=None,
        )
    )
    assert validated.name == "Updated Customer"
    assert validated.email is None
    assert validated.fields_to_update == ["name", "email"]


@pytest.mark.parametrize(
    "payload",
    [
        _customer_payload([]),
        _customer_payload(["name", "name"], name="Customer"),
        _customer_payload(["name"], company_name="Contradictory Company"),
        _customer_payload(["unknown"]),
        _customer_payload(["name"], name="   "),
        _customer_payload(["name"], name="n" * 201),
        _customer_payload(["email"], email="not-an-email"),
        _customer_payload(["address"], address="a" * 1_001),
        _customer_payload(["notes"], notes="n" * 2_001),
    ],
)
def test_customer_update_rejects_ambiguous_or_invalid_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SetCustomerInformationInput.model_validate(payload)


def test_customer_update_requires_every_strict_nullable_property() -> None:
    payload = _customer_payload(["name"], name="Customer")
    payload.pop("notes")

    with pytest.raises(ValidationError) as raised:
        SetCustomerInformationInput.model_validate(payload)

    assert raised.value.errors()[0]["loc"] == ("notes",)


@pytest.mark.parametrize(
    "model",
    [GetQuoteSummaryInput, PrepareQuotationInput, ConfirmQuotationInput, CreateQuotationInput],
)
def test_property_free_tools_reject_every_extra_argument(model: type[ToolInputModel]) -> None:
    assert model.model_validate({}).model_dump() == {}

    for field_name in BACKEND_AUTHORITY_FIELDS:
        with pytest.raises(ValidationError):
            model.model_validate({field_name: "caller-controlled"})


@pytest.mark.parametrize(
    ("model", "valid_payload"),
    [
        (AddQuoteItemInput, {"product_id": "GDF-FLOOR-001", "quantity": 1}),
        (
            UpdateQuoteItemQuantityInput,
            {"product_id": "GDF-FLOOR-001", "quantity": 2},
        ),
        (RemoveQuoteItemInput, {"product_id": "GDF-FLOOR-001"}),
        (
            SetCustomerInformationInput,
            _customer_payload(["company_name"], company_name="Example LLC"),
        ),
    ],
)
def test_mutating_tools_reject_unknown_and_backend_authority_overrides(
    model: type[ToolInputModel],
    valid_payload: dict[str, object],
) -> None:
    for field_name in ("unexpected", *BACKEND_AUTHORITY_FIELDS):
        with pytest.raises(ValidationError):
            model.model_validate({**valid_payload, field_name: "caller-controlled"})


def test_no_quotation_schema_exposes_backend_authority_inputs() -> None:
    exposed_fields = {
        field_name
        for tool in QUOTATION_TOOL_DEFINITIONS
        for field_name in cast(dict[str, object], tool["parameters"]["properties"])
    }

    assert exposed_fields == {
        "product_id",
        "quantity",
        "fields_to_update",
        *CUSTOMER_FIELDS,
    }
    assert exposed_fields.isdisjoint(BACKEND_AUTHORITY_FIELDS)


def test_confirmation_tool_is_limited_to_current_explicit_preview_confirmation() -> None:
    description = _tool_by_name("confirm_quotation")["description"]
    assert isinstance(description, str)
    assert "current user's message" in description
    assert "explicit confirmation" in description
    assert "previously delivered" in description
    parameters = cast(dict[str, Any], _tool_by_name("confirm_quotation")["parameters"])
    assert parameters["properties"] == {}
