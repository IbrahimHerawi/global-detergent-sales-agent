"""Tests for strict read-only company and product tool schemas."""

from collections.abc import Mapping
from typing import Any, cast

import pytest
from app.agent.tool_definitions import (
    COMPANY_PRODUCT_TOOL_DEFINITIONS,
    COMPANY_PRODUCT_TOOL_INPUT_MODELS,
    GetCompanyInformationInput,
    GetProductDetailsInput,
    ListProductCategoriesInput,
    ListProductsInput,
    SearchProductsInput,
    ToolInputModel,
)
from pydantic import ValidationError

EXPECTED_TOOL_NAMES = (
    "get_company_information",
    "list_product_categories",
    "list_products",
    "search_products",
    "get_product_details",
)
DISALLOWED_INPUT_FIELDS = {
    "url",
    "urls",
    "filename",
    "file_path",
    "code",
    "price",
    "unit_price",
    "state",
    "session_id",
    "quotation_id",
}


def _tool_by_name(name: str) -> Mapping[str, object]:
    return next(tool for tool in COMPANY_PRODUCT_TOOL_DEFINITIONS if tool["name"] == name)


def _assert_strict_object_schema(schema: Mapping[str, Any]) -> None:
    """Check the official strict-mode invariants recursively."""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        assert set(schema.get("required", [])) == set(properties)
        for property_schema in properties.values():
            assert isinstance(property_schema, dict)
            _assert_strict_object_schema(property_schema)

    for alternative_keyword in ("anyOf", "oneOf", "allOf"):
        alternatives = schema.get(alternative_keyword, [])
        assert isinstance(alternatives, list)
        for alternative in alternatives:
            assert isinstance(alternative, dict)
            _assert_strict_object_schema(alternative)


def test_exact_responses_api_function_tools_are_defined_in_stable_order() -> None:
    assert tuple(tool["name"] for tool in COMPANY_PRODUCT_TOOL_DEFINITIONS) == (EXPECTED_TOOL_NAMES)
    assert tuple(COMPANY_PRODUCT_TOOL_INPUT_MODELS) == EXPECTED_TOOL_NAMES

    for tool in COMPANY_PRODUCT_TOOL_DEFINITIONS:
        assert set(tool) == {"type", "name", "description", "parameters", "strict"}
        assert tool["type"] == "function"
        assert tool["strict"] is True
        assert len(tool["description"]) >= 80
        assert "Returns" in tool["description"]


def test_every_parameter_schema_is_strict_mode_compatible() -> None:
    for tool in COMPANY_PRODUCT_TOOL_DEFINITIONS:
        parameters = tool["parameters"]
        assert parameters["type"] == "object"
        _assert_strict_object_schema(parameters)
        properties = cast(dict[str, dict[str, object]], parameters["properties"])
        for property_schema in properties.values():
            assert property_schema["description"]


def test_nullable_category_is_still_required_by_strict_schema() -> None:
    parameters = _tool_by_name("list_products")["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["required"] == ["category"]
    category = parameters["properties"]["category"]
    assert category["anyOf"] == [{"type": "string"}, {"type": "null"}]

    assert ListProductsInput.model_validate({"category": None}).category is None
    assert ListProductsInput.model_validate({"category": "Floor Care"}).category == "Floor Care"

    with pytest.raises(ValidationError):
        ListProductsInput.model_validate({})
    with pytest.raises(ValidationError):
        ListProductsInput.model_validate({"category": 123})


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        (ListProductsInput, "category"),
        (SearchProductsInput, "query"),
        (GetProductDetailsInput, "product_id"),
    ],
)
def test_strict_schema_properties_are_required_by_pydantic_validation(
    model: type[ToolInputModel],
    field_name: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        model.model_validate({})

    assert raised.value.errors()[0]["loc"] == (field_name,)


@pytest.mark.parametrize(
    "model",
    [GetCompanyInformationInput, ListProductCategoriesInput],
)
def test_empty_input_tools_accept_only_an_empty_object(model: type[ToolInputModel]) -> None:
    assert model.model_validate({}).model_dump() == {}

    with pytest.raises(ValidationError):
        model.model_validate({"query": "detergent"})


@pytest.mark.parametrize(
    ("raw_query", "normalized_query"),
    [
        ("x", "x"),
        ("  floor cleaner  ", "floor cleaner"),
        ("x" * 300, "x" * 300),
    ],
)
def test_search_query_accepts_and_trims_values_within_bounds(
    raw_query: str,
    normalized_query: str,
) -> None:
    assert SearchProductsInput.model_validate({"query": raw_query}).query == normalized_query


@pytest.mark.parametrize("query", ["", "   ", "x" * 301, 1, True, None])
def test_search_query_rejects_empty_overlong_or_nonstrict_values(query: object) -> None:
    with pytest.raises(ValidationError):
        SearchProductsInput.model_validate({"query": query})


@pytest.mark.parametrize(
    ("raw_product_id", "normalized_product_id"),
    [
        ("P", "P"),
        ("  GDF-FLOOR-001  ", "GDF-FLOOR-001"),
        ("P" * 100, "P" * 100),
    ],
)
def test_product_id_accepts_trimmed_values_within_bounds(
    raw_product_id: str,
    normalized_product_id: str,
) -> None:
    validated = GetProductDetailsInput.model_validate({"product_id": raw_product_id})
    assert validated.product_id == normalized_product_id


@pytest.mark.parametrize("product_id", ["", "   ", "P" * 101, 1, True, None])
def test_product_id_rejects_empty_overlong_or_nonstrict_values(product_id: object) -> None:
    with pytest.raises(ValidationError):
        GetProductDetailsInput.model_validate({"product_id": product_id})


@pytest.mark.parametrize(
    ("model", "valid_arguments"),
    [
        (ListProductsInput, {"category": None}),
        (SearchProductsInput, {"query": "floor"}),
        (GetProductDetailsInput, {"product_id": "GDF-FLOOR-001"}),
    ],
)
def test_nonempty_input_tools_reject_additional_properties(
    model: type[ToolInputModel],
    valid_arguments: dict[str, object],
) -> None:
    for field_name in DISALLOWED_INPUT_FIELDS:
        with pytest.raises(ValidationError):
            model.model_validate({**valid_arguments, field_name: "caller-controlled"})


def test_schemas_expose_only_the_five_allowlisted_input_properties() -> None:
    exposed_fields: set[str] = set()
    for tool in COMPANY_PRODUCT_TOOL_DEFINITIONS:
        parameters = cast(dict[str, Any], tool["parameters"])
        properties = cast(dict[str, object], parameters["properties"])
        exposed_fields.update(properties)

    assert exposed_fields == {"category", "query", "product_id"}
    assert exposed_fields.isdisjoint(DISALLOWED_INPUT_FIELDS)
