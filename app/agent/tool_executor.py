"""Allowlisted dispatch from validated model tool calls to business services."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from types import MappingProxyType
from typing import Final, cast

from pydantic import ValidationError

from app.agent.tool_definitions import (
    AddQuoteItemInput,
    ConfirmQuotationInput,
    CreateQuotationInput,
    GetCompanyInformationInput,
    GetProductDetailsInput,
    GetQuoteSummaryInput,
    ListProductCategoriesInput,
    ListProductsInput,
    PrepareQuotationInput,
    RemoveQuoteItemInput,
    SearchProductsInput,
    SetCustomerInformationInput,
    ToolInputModel,
    UpdateQuoteItemQuantityInput,
)
from app.core.exceptions import BusinessError, ToolExecutionError, ToolNotFoundError
from app.schemas.quotation import GeneratedQuotation
from app.schemas.session import ConversationSession
from app.services.company_service import CompanyService
from app.services.product_service import ProductService
from app.services.quotation_service import CurrentTurnContext, QuotationService

MAX_TOOL_ARGUMENT_BYTES: Final = 16_384


@dataclass(frozen=True, slots=True)
class PreparedPreviewMetadata:
    """Backend-only binding needed after a preview is successfully delivered."""

    fingerprint: str
    originating_turn_id: str


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Separate model-visible output from trusted per-turn application state."""

    model_output: dict[str, object]
    prepared_preview: PreparedPreviewMetadata | None = None
    generated_quote: GeneratedQuotation | None = None

    @property
    def output(self) -> dict[str, object]:
        """Compatibility alias for the structured model-visible result."""
        return self.model_output

    @property
    def generated_quotation(self) -> GeneratedQuotation | None:
        """Expose the generated artifact to backend orchestration only."""
        return self.generated_quote

    def model_output_json(self) -> str:
        """Serialize only the explicitly model-visible portion of the result."""
        return json.dumps(self.model_output, ensure_ascii=False, separators=(",", ":"))


ToolHandler = Callable[
    ["ToolExecutor", ToolInputModel, ConversationSession, CurrentTurnContext],
    Awaitable[ToolExecutionResult],
]


class ToolExecutor:
    """Validate and dispatch exactly the thirteen registered business tools."""

    __slots__ = (
        "_company_service",
        "_max_argument_bytes",
        "_mutation_lock",
        "_product_service",
        "_quotation_service",
    )

    def __init__(
        self,
        company_service: CompanyService | None = None,
        product_service: ProductService | None = None,
        quotation_service: QuotationService | None = None,
        *,
        max_argument_bytes: int = MAX_TOOL_ARGUMENT_BYTES,
    ) -> None:
        if (
            not isinstance(max_argument_bytes, int)
            or isinstance(max_argument_bytes, bool)
            or max_argument_bytes <= 0
        ):
            raise ValueError("max_argument_bytes must be a positive integer")
        self._company_service = company_service if company_service is not None else CompanyService()
        self._product_service = product_service if product_service is not None else ProductService()
        self._quotation_service = (
            quotation_service if quotation_service is not None else QuotationService()
        )
        self._max_argument_bytes = max_argument_bytes
        self._mutation_lock = asyncio.Lock()

    async def execute(
        self,
        tool_name: str,
        arguments: str,
        session: ConversationSession,
        current_turn_context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        """Execute one allowlisted tool call using backend-owned conversation context."""
        if not isinstance(tool_name, str):
            raise ToolNotFoundError("Tool name must be a string")
        handler = TOOL_HANDLERS.get(tool_name)
        input_model = TOOL_INPUT_MODELS.get(tool_name)
        if handler is None or input_model is None:
            raise ToolNotFoundError(f"Unknown tool name: {tool_name!r}")

        self._validate_backend_context(session, current_turn_context)
        validated_arguments = self._parse_and_validate_arguments(arguments, input_model)

        try:
            if tool_name in MUTATION_TOOL_NAMES:
                async with self._mutation_lock:
                    return await handler(
                        self,
                        validated_arguments,
                        session,
                        current_turn_context,
                    )
            return await handler(self, validated_arguments, session, current_turn_context)
        except BusinessError as exc:
            return ToolExecutionResult(
                model_output={"status": "error", "error": exc.to_public_dict()}
            )

    async def execute_tool(
        self,
        tool_name: str,
        arguments: str,
        session: ConversationSession,
        current_turn_context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        """Named alias used by the later Responses API orchestration loop."""
        return await self.execute(tool_name, arguments, session, current_turn_context)

    def _parse_and_validate_arguments(
        self,
        arguments: str,
        input_model: type[ToolInputModel],
    ) -> ToolInputModel:
        if not isinstance(arguments, str):
            raise ToolExecutionError("Tool arguments must be a JSON string")
        try:
            argument_size = len(arguments.encode("utf-8"))
        except UnicodeError as exc:
            raise ToolExecutionError("Tool arguments are not valid UTF-8", cause=exc) from exc
        if argument_size > self._max_argument_bytes:
            raise ToolExecutionError(
                f"Tool arguments exceed the {self._max_argument_bytes}-byte limit"
            )

        try:
            raw_arguments = json.loads(
                arguments,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (JSONDecodeError, UnicodeError, RecursionError, ValueError) as exc:
            raise ToolExecutionError("Malformed tool arguments", cause=exc) from exc
        if not isinstance(raw_arguments, dict):
            raise ToolExecutionError("Tool arguments must be a JSON object")

        try:
            return input_model.model_validate(raw_arguments)
        except ValidationError as exc:
            raise ToolExecutionError("Tool arguments failed schema validation", cause=exc) from exc

    @staticmethod
    def _validate_backend_context(
        session: ConversationSession,
        current_turn_context: CurrentTurnContext,
    ) -> None:
        if not isinstance(session, ConversationSession):
            raise ToolExecutionError("session must be backend-owned conversation state")
        if not isinstance(current_turn_context, CurrentTurnContext):
            raise ToolExecutionError("current turn context must be backend-owned")
        if current_turn_context.session_id != session.session_id:
            raise ToolExecutionError("current turn context belongs to another session")
        if (
            not isinstance(current_turn_context.turn_id, str)
            or not current_turn_context.turn_id.strip()
        ):
            raise ToolExecutionError("current turn ID must be nonblank")

    async def _get_company_information(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del arguments, session, context
        return _result(self._company_service.get_company_information())

    async def _list_product_categories(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del arguments, session, context
        return _result(self._company_service.list_product_categories())

    async def _list_products(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del context
        validated = cast(ListProductsInput, arguments)
        output = self._product_service.list_products(validated.category)
        # Browsing a list does not express a choice, even when results are ranked.
        del session
        return _result(output)

    async def _search_products(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del session, context
        validated = cast(SearchProductsInput, arguments)
        return _result(self._product_service.search_products(validated.query))

    async def _get_product_details(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del context
        validated = cast(GetProductDetailsInput, arguments)
        output = self._product_service.get_product_details(validated.product_id)
        session.selected_product_id = output["product_id"]
        return _result(output)

    async def _add_quote_item(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del context
        validated = cast(AddQuoteItemInput, arguments)
        self._quotation_service.add_item(session, validated.product_id, validated.quantity)
        session.selected_product_id = validated.product_id
        return _cart_result(session)

    async def _update_quote_item_quantity(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del context
        validated = cast(UpdateQuoteItemQuantityInput, arguments)
        self._quotation_service.update_item_quantity(
            session,
            validated.product_id,
            validated.quantity,
        )
        session.selected_product_id = validated.product_id
        return _cart_result(session)

    async def _remove_quote_item(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del context
        validated = cast(RemoveQuoteItemInput, arguments)
        self._quotation_service.remove_item(session, validated.product_id)
        session.selected_product_id = validated.product_id
        return _cart_result(session)

    async def _get_quote_summary(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del arguments, context
        return ToolExecutionResult(
            model_output={"summary": self._quotation_service.get_quote_summary(session)}
        )

    async def _set_customer_information(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del context
        validated = cast(SetCustomerInformationInput, arguments)
        candidate_updates = {
            "name": validated.name,
            "company_name": validated.company_name,
            "contact_person": validated.contact_person,
            "email": validated.email,
            "address": validated.address,
            "notes": validated.notes,
        }
        updates = {
            field_name: candidate_updates[field_name] for field_name in validated.fields_to_update
        }
        self._quotation_service.set_customer_information(session, **updates)
        return ToolExecutionResult(
            model_output={
                "status": "success",
                "updated_fields": list(validated.fields_to_update),
            }
        )

    async def _prepare_quotation(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del arguments
        preview = self._quotation_service.prepare_quotation(session, context.turn_id)
        fingerprint = session.prepared_preview_fingerprint
        originating_turn_id = session.preview_originating_turn_id
        if fingerprint is None or originating_turn_id is None:
            raise ToolExecutionError("Quotation service did not preserve preview binding metadata")
        model_output = cast(dict[str, object], preview.model_dump(mode="json"))
        model_output["quote_confirmed"] = False
        return ToolExecutionResult(
            model_output=model_output,
            prepared_preview=PreparedPreviewMetadata(
                fingerprint=fingerprint,
                originating_turn_id=originating_turn_id,
            ),
        )

    async def _confirm_quotation(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del arguments
        self._quotation_service.confirm(session, context)
        return ToolExecutionResult(model_output={"confirmed": True})

    async def _create_quotation(
        self,
        arguments: ToolInputModel,
        session: ConversationSession,
        context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del arguments, context
        generated = await self._quotation_service.generate(session)
        return ToolExecutionResult(
            model_output={
                "quotation_id": generated.quotation_id,
                "total": str(generated.totals.total),
                "currency": generated.totals.currency,
                "pdf_generated": True,
            },
            generated_quote=generated,
        )


TOOL_INPUT_MODELS: Final[Mapping[str, type[ToolInputModel]]] = MappingProxyType(
    {
        "get_company_information": GetCompanyInformationInput,
        "list_product_categories": ListProductCategoriesInput,
        "list_products": ListProductsInput,
        "search_products": SearchProductsInput,
        "get_product_details": GetProductDetailsInput,
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

# This literal is the sole dispatch table. A tool name can reach only one of
# these pre-bound implementation methods; no attribute lookup, import, eval,
# or caller-provided executable name participates in dispatch.
TOOL_HANDLERS: Final[Mapping[str, ToolHandler]] = MappingProxyType(
    {
        "get_company_information": ToolExecutor._get_company_information,
        "list_product_categories": ToolExecutor._list_product_categories,
        "list_products": ToolExecutor._list_products,
        "search_products": ToolExecutor._search_products,
        "get_product_details": ToolExecutor._get_product_details,
        "add_quote_item": ToolExecutor._add_quote_item,
        "update_quote_item_quantity": ToolExecutor._update_quote_item_quantity,
        "remove_quote_item": ToolExecutor._remove_quote_item,
        "get_quote_summary": ToolExecutor._get_quote_summary,
        "set_customer_information": ToolExecutor._set_customer_information,
        "prepare_quotation": ToolExecutor._prepare_quotation,
        "confirm_quotation": ToolExecutor._confirm_quotation,
        "create_quotation": ToolExecutor._create_quotation,
    }
)

MUTATION_TOOL_NAMES: Final = frozenset(
    {
        "get_product_details",
        "add_quote_item",
        "update_quote_item_quantity",
        "remove_quote_item",
        "set_customer_information",
        "prepare_quotation",
        "confirm_quotation",
        "create_quotation",
    }
)


def _result(output: Mapping[str, object]) -> ToolExecutionResult:
    return ToolExecutionResult(model_output=dict(output))


def _cart_result(session: ConversationSession) -> ToolExecutionResult:
    return ToolExecutionResult(
        model_output={
            "status": "success",
            "cart": [item.model_dump(mode="json") for item in session.cart],
        }
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        parsed[key] = value
    return parsed


def _reject_nonstandard_json_constant(value: str) -> object:
    raise ValueError(f"nonstandard JSON constant: {value}")


__all__ = [
    "MAX_TOOL_ARGUMENT_BYTES",
    "MUTATION_TOOL_NAMES",
    "TOOL_HANDLERS",
    "TOOL_INPUT_MODELS",
    "PreparedPreviewMetadata",
    "ToolExecutionResult",
    "ToolExecutor",
]
