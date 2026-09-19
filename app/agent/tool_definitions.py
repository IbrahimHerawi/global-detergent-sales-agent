"""Strict read-only company and product tools for the Responses API."""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Final, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SearchQuery = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300),
]
ProductId = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100),
]


class ToolInputModel(BaseModel):
    """Closed, strictly typed arguments accepted from a model tool call."""

    model_config = ConfigDict(extra="forbid", strict=True)


class GetCompanyInformationInput(ToolInputModel):
    """Arguments for the property-free company-information lookup."""


class ListProductCategoriesInput(ToolInputModel):
    """Arguments for the property-free product-category lookup."""


class ListProductsInput(ToolInputModel):
    """Arguments for browsing active products, optionally by category."""

    category: str | None = Field(
        description="Exact stored category to filter by, or null to list all active products."
    )


class SearchProductsInput(ToolInputModel):
    """Arguments for bounded natural-language catalog search."""

    query: SearchQuery = Field(
        description="A product name, SKU, application, keyword, or concise customer requirement."
    )


class GetProductDetailsInput(ToolInputModel):
    """Arguments for an exact product lookup by backend catalog ID."""

    product_id: ProductId = Field(description="Exact backend catalog product identifier.")


class FunctionToolDefinition(TypedDict):
    """Responses API function-tool definition in strict mode."""

    type: Literal["function"]
    name: str
    description: str
    parameters: dict[str, object]
    strict: Literal[True]


ToolInputModelType = type[ToolInputModel]


def _strict_parameters(model: ToolInputModelType) -> dict[str, object]:
    """Return Pydantic JSON Schema with an explicit strict required list."""
    schema = cast(dict[str, object], model.model_json_schema())
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("tool input schema must define object properties")
    schema["required"] = list(properties)
    return schema


def _function_tool(
    name: str,
    description: str,
    input_model: ToolInputModelType,
) -> FunctionToolDefinition:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": _strict_parameters(input_model),
        "strict": True,
    }


GET_COMPANY_INFORMATION_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "get_company_information",
    (
        "Read the authoritative company profile when the customer asks about the company, "
        "industries served, approved product categories, currency, or contact information. "
        "Returns {name, short_name, description, industries_served: string[], "
        "product_categories: string[], currency, contact: {phone, email, website, address}}; "
        "unknown contact values are null."
    ),
    GetCompanyInformationInput,
)

LIST_PRODUCT_CATEGORIES_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "list_product_categories",
    (
        "List the authoritative company-level product categories for catalog discovery. "
        "Returns {categories: string[]}."
    ),
    ListProductCategoriesInput,
)

LIST_PRODUCTS_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "list_products",
    (
        "List active catalog products. Pass category as an exact category string to filter, "
        "or null to list every active product. Returns {products: [{product_id, sku, name, "
        "category, short_description}]}."
    ),
    ListProductsInput,
)

SEARCH_PRODUCTS_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "search_products",
    (
        "Search active catalog products using a concise customer requirement, product name, "
        "SKU, application, or keyword. Returns {matches: [{product_id, sku, name, category, "
        "short_description}]}; an empty matches list means no stored product matched."
    ),
    SearchProductsInput,
)

GET_PRODUCT_DETAILS_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "get_product_details",
    (
        "Retrieve authoritative stored details for one active product by product_id. Returns "
        "the product identity and descriptions, applications, packaging, exact catalog price "
        "and currency, instructions, safety and technical fields, ingredients, surfaces, "
        "contact time, dilution ratio, certifications, approved claims, keywords, and active "
        "status. Missing optional stored facts are null or empty lists."
    ),
    GetProductDetailsInput,
)

COMPANY_PRODUCT_TOOL_DEFINITIONS: Final[tuple[FunctionToolDefinition, ...]] = (
    GET_COMPANY_INFORMATION_TOOL,
    LIST_PRODUCT_CATEGORIES_TOOL,
    LIST_PRODUCTS_TOOL,
    SEARCH_PRODUCTS_TOOL,
    GET_PRODUCT_DETAILS_TOOL,
)

COMPANY_PRODUCT_TOOL_INPUT_MODELS: Final[MappingProxyType[str, ToolInputModelType]] = (
    MappingProxyType(
        {
            "get_company_information": GetCompanyInformationInput,
            "list_product_categories": ListProductCategoriesInput,
            "list_products": ListProductsInput,
            "search_products": SearchProductsInput,
            "get_product_details": GetProductDetailsInput,
        }
    )
)

# Stable public collection names for the later executor and Responses loop.
TOOL_DEFINITIONS: Final = COMPANY_PRODUCT_TOOL_DEFINITIONS
TOOL_INPUT_MODELS: Final = COMPANY_PRODUCT_TOOL_INPUT_MODELS

__all__ = [
    "COMPANY_PRODUCT_TOOL_DEFINITIONS",
    "COMPANY_PRODUCT_TOOL_INPUT_MODELS",
    "GET_COMPANY_INFORMATION_TOOL",
    "GET_PRODUCT_DETAILS_TOOL",
    "LIST_PRODUCTS_TOOL",
    "LIST_PRODUCT_CATEGORIES_TOOL",
    "SEARCH_PRODUCTS_TOOL",
    "TOOL_DEFINITIONS",
    "TOOL_INPUT_MODELS",
    "FunctionToolDefinition",
    "GetCompanyInformationInput",
    "GetProductDetailsInput",
    "ListProductCategoriesInput",
    "ListProductsInput",
    "SearchProductsInput",
    "ToolInputModel",
]
