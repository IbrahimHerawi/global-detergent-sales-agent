"""Strict company, product, and quotation tools for the Responses API."""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Final, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SearchQuery = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300),
]
ProductId = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100),
]
PositiveQuantity = Annotated[int, Field(strict=True, gt=0)]
CustomerName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200),
]
CustomerEmail = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]
CustomerAddress = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1_000),
]
CustomerNotes = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2_000),
]
CustomerUpdateField = Literal[
    "name",
    "company_name",
    "contact_person",
    "email",
    "address",
    "notes",
]
_CUSTOMER_UPDATE_FIELDS: Final[tuple[CustomerUpdateField, ...]] = (
    "name",
    "company_name",
    "contact_person",
    "email",
    "address",
    "notes",
)


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


class AddQuoteItemInput(ToolInputModel):
    """Arguments for adding a product quantity to the quotation cart."""

    product_id: ProductId = Field(description="Exact backend catalog product identifier.")
    quantity: PositiveQuantity = Field(
        description="Positive whole-number quantity to add to the existing cart quantity."
    )


class UpdateQuoteItemQuantityInput(ToolInputModel):
    """Arguments for replacing one quotation-cart item quantity."""

    product_id: ProductId = Field(description="Exact backend catalog product identifier.")
    quantity: PositiveQuantity = Field(
        description="Positive whole-number quantity that replaces the current cart quantity."
    )


class RemoveQuoteItemInput(ToolInputModel):
    """Arguments for removing one product from the quotation cart."""

    product_id: ProductId = Field(description="Exact backend catalog product identifier.")


class GetQuoteSummaryInput(ToolInputModel):
    """Arguments for the property-free current quotation summary."""


class SetCustomerInformationInput(ToolInputModel):
    """Explicit partial customer update without transport-owned phone data."""

    fields_to_update: list[CustomerUpdateField] = Field(
        min_length=1,
        max_length=len(_CUSTOMER_UPDATE_FIELDS),
        description=(
            "One or more customer fields to change. Listed null fields are cleared; fields "
            "not listed must be null and remain unchanged."
        ),
    )
    name: CustomerName | None = Field(
        description="Customer name to set, null to clear when listed, or null when unchanged."
    )
    company_name: CustomerName | None = Field(
        description="Company name to set, null to clear when listed, or null when unchanged."
    )
    contact_person: CustomerName | None = Field(
        description="Contact person to set, null to clear when listed, or null when unchanged."
    )
    email: CustomerEmail | None = Field(
        description="Email address to set, null to clear when listed, or null when unchanged."
    )
    address: CustomerAddress | None = Field(
        description="Address to set, null to clear when listed, or null when unchanged."
    )
    notes: CustomerNotes | None = Field(
        description="Customer notes to set, null to clear when listed, or null when unchanged."
    )

    @model_validator(mode="after")
    def require_unambiguous_update_intent(self) -> SetCustomerInformationInput:
        """Reject duplicates and non-null values for fields that stay unchanged."""
        selected_fields = set(self.fields_to_update)
        if len(selected_fields) != len(self.fields_to_update):
            raise ValueError("fields_to_update must not contain duplicates")
        contradictory_fields = [
            field_name
            for field_name in _CUSTOMER_UPDATE_FIELDS
            if field_name not in selected_fields and getattr(self, field_name) is not None
        ]
        if contradictory_fields:
            raise ValueError("fields not selected for update must be null")
        return self


class PrepareQuotationInput(ToolInputModel):
    """Arguments for the property-free quotation-preview operation."""


class ConfirmQuotationInput(ToolInputModel):
    """Arguments for backend-bound confirmation of the current user turn."""


class CreateQuotationInput(ToolInputModel):
    """Arguments for the property-free authorized PDF generation operation."""


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

ADD_QUOTE_ITEM_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "add_quote_item",
    (
        "Add an active catalog product to the quotation cart; when already present, add the "
        "requested quantity to its current quantity. The backend resolves the product and "
        "revokes any prior preview or confirmation. Returns {status: 'success', cart: "
        "[{product_id, quantity}]}."
    ),
    AddQuoteItemInput,
)

UPDATE_QUOTE_ITEM_QUANTITY_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "update_quote_item_quantity",
    (
        "Replace, rather than increment, the quantity of a product already in the quotation "
        "cart. The backend validates the cart entry and revokes any prior preview or "
        "confirmation. Returns {status: 'success', cart: [{product_id, quantity}]}."
    ),
    UpdateQuoteItemQuantityInput,
)

REMOVE_QUOTE_ITEM_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "remove_quote_item",
    (
        "Remove one existing product from the quotation cart. The backend revokes any prior "
        "preview or confirmation. Returns {status: 'success', cart: [{product_id, quantity}]}."
    ),
    RemoveQuoteItemInput,
)

GET_QUOTE_SUMMARY_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "get_quote_summary",
    (
        "Calculate the current quotation summary from backend catalog prices and quotation "
        "rules without preparing a preview or generating a PDF. Returns {summary: string}."
    ),
    GetQuoteSummaryInput,
)

SET_CUSTOMER_INFORMATION_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "set_customer_information",
    (
        "Update only the customer fields named in fields_to_update. A listed field with null "
        "is cleared; an unlisted field remains unchanged and must be null in the call. Phone "
        "is transport-owned and cannot be supplied. Returns {status: 'success', "
        "updated_fields: string[]} with no backend authorization metadata."
    ),
    SetCustomerInformationInput,
)

PREPARE_QUOTATION_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "prepare_quotation",
    (
        "Validate the cart and required customer information, calculate a backend-priced "
        "quotation preview, and bind it for delivery. This does not generate a PDF. Returns "
        "{customer, items: [{product_id, sku, description, quantity, unit_price, subtotal}], "
        "totals: {subtotal, discount, tax, total, currency}, validity_days, terms, "
        "quote_confirmed: false}."
    ),
    PrepareQuotationInput,
)

CONFIRM_QUOTATION_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "confirm_quotation",
    (
        "Use only when the current user's message is an explicit confirmation of the "
        "previously delivered, still-current quotation preview. Never use it for ambiguous "
        "language, an earlier message, or the preview-delivery turn. The backend binds the "
        "current turn and revalidates the preview. Returns {confirmed: true}."
    ),
    ConfirmQuotationInput,
)

CREATE_QUOTATION_TOOL: Final[FunctionToolDefinition] = _function_tool(
    "create_quotation",
    (
        "Generate the final quotation and PDF only after backend authorization confirms the "
        "delivered preview and explicit customer confirmation are still current. The backend "
        "calculates prices, totals, discounts, dates, and the quotation ID. Returns "
        "{quotation_id, total, currency, pdf_generated: true}; internal filesystem and "
        "artifact metadata are not returned."
    ),
    CreateQuotationInput,
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

QUOTATION_TOOL_DEFINITIONS: Final[tuple[FunctionToolDefinition, ...]] = (
    ADD_QUOTE_ITEM_TOOL,
    UPDATE_QUOTE_ITEM_QUANTITY_TOOL,
    REMOVE_QUOTE_ITEM_TOOL,
    GET_QUOTE_SUMMARY_TOOL,
    SET_CUSTOMER_INFORMATION_TOOL,
    PREPARE_QUOTATION_TOOL,
    CONFIRM_QUOTATION_TOOL,
    CREATE_QUOTATION_TOOL,
)

QUOTATION_TOOL_INPUT_MODELS: Final[MappingProxyType[str, ToolInputModelType]] = MappingProxyType(
    {
        "add_quote_item": AddQuoteItemInput,
        "update_quote_item_quantity": UpdateQuoteItemQuantityInput,
        "remove_quote_item": RemoveQuoteItemInput,
        "get_quote_summary": GetQuoteSummaryInput,
        "set_customer_information": SetCustomerInformationInput,
        "prepare_quotation": PrepareQuotationInput,
        "confirm_quotation": ConfirmQuotationInput,
        "create_quotation": CreateQuotationInput,
    }
)

# Stable public collection names for the later executor and Responses loop.
TOOL_DEFINITIONS: Final = COMPANY_PRODUCT_TOOL_DEFINITIONS + QUOTATION_TOOL_DEFINITIONS
TOOL_INPUT_MODELS: Final[MappingProxyType[str, ToolInputModelType]] = MappingProxyType(
    {**COMPANY_PRODUCT_TOOL_INPUT_MODELS, **QUOTATION_TOOL_INPUT_MODELS}
)

__all__ = [
    "ADD_QUOTE_ITEM_TOOL",
    "COMPANY_PRODUCT_TOOL_DEFINITIONS",
    "COMPANY_PRODUCT_TOOL_INPUT_MODELS",
    "CONFIRM_QUOTATION_TOOL",
    "CREATE_QUOTATION_TOOL",
    "GET_COMPANY_INFORMATION_TOOL",
    "GET_PRODUCT_DETAILS_TOOL",
    "GET_QUOTE_SUMMARY_TOOL",
    "LIST_PRODUCTS_TOOL",
    "LIST_PRODUCT_CATEGORIES_TOOL",
    "PREPARE_QUOTATION_TOOL",
    "QUOTATION_TOOL_DEFINITIONS",
    "QUOTATION_TOOL_INPUT_MODELS",
    "REMOVE_QUOTE_ITEM_TOOL",
    "SEARCH_PRODUCTS_TOOL",
    "SET_CUSTOMER_INFORMATION_TOOL",
    "TOOL_DEFINITIONS",
    "TOOL_INPUT_MODELS",
    "UPDATE_QUOTE_ITEM_QUANTITY_TOOL",
    "AddQuoteItemInput",
    "ConfirmQuotationInput",
    "CreateQuotationInput",
    "FunctionToolDefinition",
    "GetCompanyInformationInput",
    "GetProductDetailsInput",
    "GetQuoteSummaryInput",
    "ListProductCategoriesInput",
    "ListProductsInput",
    "PrepareQuotationInput",
    "RemoveQuoteItemInput",
    "SearchProductsInput",
    "SetCustomerInformationInput",
    "ToolInputModel",
    "UpdateQuoteItemQuantityInput",
]
