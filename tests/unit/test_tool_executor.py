"""Tests for allowlisted, validated tool dispatch and internal result boundaries."""

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from app.agent.tool_executor import (
    MAX_TOOL_ARGUMENT_BYTES,
    TOOL_HANDLERS,
    TOOL_INPUT_MODELS,
    ToolExecutor,
)
from app.core.exceptions import TECHNICAL_FALLBACK_MESSAGE, ToolExecutionError, ToolNotFoundError
from app.schemas.quotation import CustomerInfo, GeneratedQuotation, QuoteLine, QuoteTotals
from app.schemas.session import ConversationSession
from app.services.quotation_service import CurrentTurnContext, QuotationService
from pydantic import ValidationError

PRODUCT_ID = "GDF-FLC-001"
EXPECTED_TOOL_NAMES = (
    "get_company_information",
    "list_product_categories",
    "list_products",
    "search_products",
    "get_product_details",
    "add_quote_item",
    "update_quote_item_quantity",
    "remove_quote_item",
    "get_quote_summary",
    "set_customer_information",
    "prepare_quotation",
    "confirm_quotation",
    "create_quotation",
)


class FakePDFService:
    """Return a deliberately sensitive internal path without touching storage."""

    path = Path("/private/storage/customer-secret/GDF-Q-TEST.pdf")

    async def generate_quotation_pdf(self, quotation: GeneratedQuotation) -> Path:
        del quotation
        return self.path


def turn(
    session: ConversationSession,
    turn_id: str = "turn-1",
    message: str = "I need floor cleaner",
    preceding: tuple[str, ...] = (),
) -> CurrentTurnContext:
    return CurrentTurnContext(
        session_id=session.session_id,
        turn_id=turn_id,
        customer_message=message,
        preceding_turn_ids=preceding,
    )


def customer_arguments() -> str:
    return json.dumps(
        {
            "fields_to_update": ["name"],
            "name": "Aisha",
            "company_name": None,
            "contact_person": None,
            "email": None,
            "address": None,
            "notes": None,
        }
    )


@pytest.fixture
def session() -> ConversationSession:
    return ConversationSession(customer_phone="+97450000000")


@pytest.fixture
def executor() -> ToolExecutor:
    return ToolExecutor(
        quotation_service=QuotationService(
            pdf_service=FakePDFService(),
            clock=lambda: datetime(2026, 9, 19, 12, tzinfo=UTC),
            quotation_id_factory=lambda prefix, issued_at: "GDF-Q-20260919-TEST",
        )
    )


def test_dispatch_and_validation_tables_explicitly_register_exactly_thirteen_tools() -> None:
    assert tuple(TOOL_HANDLERS) == EXPECTED_TOOL_NAMES
    assert tuple(TOOL_INPUT_MODELS) == EXPECTED_TOOL_NAMES
    assert len(TOOL_HANDLERS) == 13


@pytest.mark.asyncio
async def test_all_thirteen_tools_map_to_existing_services_and_safe_results(
    executor: ToolExecutor,
    session: ConversationSession,
) -> None:
    context = turn(session)

    company = await executor.execute("get_company_information", "{}", session, context)
    categories = await executor.execute("list_product_categories", "{}", session, context)
    products = await executor.execute("list_products", '{"category":null}', session, context)
    matches = await executor.execute(
        "search_products", '{"query":"floor cleaner"}', session, context
    )
    details = await executor.execute(
        "get_product_details", f'{{"product_id":"{PRODUCT_ID}"}}', session, context
    )
    added = await executor.execute(
        "add_quote_item",
        f'{{"product_id":"{PRODUCT_ID}","quantity":1}}',
        session,
        context,
    )
    updated = await executor.execute(
        "update_quote_item_quantity",
        f'{{"product_id":"{PRODUCT_ID}","quantity":2}}',
        session,
        context,
    )
    summary = await executor.execute("get_quote_summary", "{}", session, context)
    customer = await executor.execute(
        "set_customer_information", customer_arguments(), session, context
    )
    prepared = await executor.execute("prepare_quotation", "{}", session, context)

    assert company.model_output["name"] == "Global Detergent Factory"
    assert categories.model_output["categories"]
    assert len(cast(list[object], products.model_output["products"])) == 10
    assert (
        cast(list[dict[str, object]], matches.model_output["matches"])[0]["product_id"]
        == PRODUCT_ID
    )
    assert details.model_output["product_id"] == PRODUCT_ID
    assert added.model_output["status"] == "success"
    assert updated.model_output["cart"] == [{"product_id": PRODUCT_ID, "quantity": 2}]
    assert "Total:" in cast(str, summary.model_output["summary"])
    assert customer.model_output == {"status": "success", "updated_fields": ["name"]}
    assert prepared.model_output["quote_confirmed"] is False
    assert prepared.prepared_preview is not None
    assert prepared.prepared_preview.originating_turn_id == "turn-1"
    assert "fingerprint" not in prepared.model_output

    executor._quotation_service.mark_ready_for_confirmation(
        session,
        prepared.prepared_preview.fingerprint,
        prepared.prepared_preview.originating_turn_id,
    )
    confirmation_context = turn(
        session,
        turn_id="turn-2",
        message="Confirmed",
        preceding=("turn-1",),
    )
    confirmed = await executor.execute("confirm_quotation", "{}", session, confirmation_context)
    created = await executor.execute("create_quotation", "{}", session, confirmation_context)
    removed = await executor.execute(
        "remove_quote_item",
        f'{{"product_id":"{PRODUCT_ID}"}}',
        session,
        confirmation_context,
    )

    assert confirmed.model_output == {"confirmed": True}
    assert set(created.model_output) == {
        "quotation_id",
        "total",
        "currency",
        "pdf_generated",
    }
    assert created.model_output["pdf_generated"] is True
    assert created.generated_quote is not None
    assert created.generated_quote.pdf_path == str(FakePDFService.path)
    assert "private" not in created.model_output_json()
    assert "storage" not in created.model_output_json()
    assert removed.model_output == {"status": "success", "cart": []}


@pytest.mark.parametrize(
    "arguments",
    [
        "{",
        "[]",
        '{"query":"floor","query":"glass"}',
        '{"query":NaN}',
        json.dumps({"query": "x" * MAX_TOOL_ARGUMENT_BYTES}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_unbounded_json_arguments_raise_safe_technical_error(
    executor: ToolExecutor,
    session: ConversationSession,
    arguments: str,
) -> None:
    with pytest.raises(ToolExecutionError) as raised:
        await executor.execute("search_products", arguments, session, turn(session))

    assert raised.value.to_public_dict() == {
        "code": "technical_error",
        "message": TECHNICAL_FALLBACK_MESSAGE,
    }
    assert arguments not in str(raised.value)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_company_information", '{"url":"https://attacker.invalid"}'),
        ("get_product_details", '{"product_id":"GDF-FLC-001","active":true}'),
        ("add_quote_item", '{"product_id":"GDF-FLC-001","quantity":1,"price":"0"}'),
        ("prepare_quotation", '{"preview_fingerprint":"forged"}'),
        ("confirm_quotation", '{"quote_confirmed":true}'),
        ("create_quotation", '{"pdf_path":"/tmp/attacker.pdf"}'),
    ],
)
@pytest.mark.asyncio
async def test_unauthorized_fields_never_reach_handlers(
    executor: ToolExecutor,
    session: ConversationSession,
    tool_name: str,
    arguments: str,
) -> None:
    before = session.model_dump(mode="json")

    with pytest.raises(ToolExecutionError) as raised:
        await executor.execute(tool_name, arguments, session, turn(session))

    assert isinstance(raised.value.cause, ValidationError)
    assert session.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_unknown_tool_raises_tool_not_found_before_parsing_or_dispatch(
    executor: ToolExecutor,
    session: ConversationSession,
) -> None:
    with pytest.raises(ToolNotFoundError):
        await executor.execute("__import__", "not even json", session, turn(session))


@pytest.mark.asyncio
async def test_missing_product_returns_only_safe_structured_business_error(
    executor: ToolExecutor,
    session: ConversationSession,
) -> None:
    result = await executor.execute(
        "get_product_details",
        '{"product_id":"SECRET-MISSING-ID"}',
        session,
        turn(session),
    )

    assert result.model_output == {
        "status": "error",
        "error": {
            "code": "product_not_found",
            "message": "The requested product was not found.",
        },
    }
    assert "SECRET-MISSING-ID" not in result.model_output_json()
    assert result.prepared_preview is None
    assert result.generated_quote is None


@pytest.mark.asyncio
async def test_selection_changes_only_after_explicit_active_product_resolution(
    executor: ToolExecutor,
    session: ConversationSession,
) -> None:
    session.selected_product_id = "existing-selection"
    context = turn(session)

    await executor.execute("list_products", '{"category":null}', session, context)
    await executor.execute("search_products", '{"query":"cleaner"}', session, context)
    missing = await executor.execute(
        "get_product_details", '{"product_id":"missing"}', session, context
    )

    assert missing.model_output["status"] == "error"
    assert session.selected_product_id == "existing-selection"

    await executor.execute(
        "get_product_details", f'{{"product_id":"{PRODUCT_ID}"}}', session, context
    )
    assert session.selected_product_id == PRODUCT_ID


@pytest.mark.asyncio
async def test_backend_turn_context_is_never_accepted_from_tool_arguments(
    executor: ToolExecutor,
    session: ConversationSession,
) -> None:
    other_session = ConversationSession(customer_phone="+97451111111")
    mismatched = turn(other_session)

    with pytest.raises(ToolExecutionError):
        await executor.execute("get_company_information", "{}", session, mismatched)


def generated_quote() -> GeneratedQuotation:
    return GeneratedQuotation.with_pdf_path(
        pdf_path="/private/sequential.pdf",
        quotation_id="GDF-Q-SEQUENTIAL",
        issued_at=datetime(2026, 9, 19, tzinfo=UTC),
        valid_until=date(2026, 10, 3),
        customer=CustomerInfo(phone="+97450000000", name="Aisha"),
        items=[
            QuoteLine(
                product_id=PRODUCT_ID,
                sku="FLC-5L",
                description="Floor cleaner",
                quantity=1,
                unit_price=Decimal("25.00"),
                subtotal=Decimal("25.00"),
            )
        ],
        totals=QuoteTotals(
            subtotal=Decimal("25.00"),
            discount=Decimal("0.00"),
            tax=Decimal("0.00"),
            total=Decimal("25.00"),
            currency="QAR",
        ),
        validity_days=14,
        terms=["Test term"],
    )


class ConcurrentGenerationService:
    """Detect whether the executor overlaps mutating service calls."""

    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    async def generate(self, session: ConversationSession) -> GeneratedQuotation:
        del session
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return generated_quote()


@pytest.mark.asyncio
async def test_mutation_tools_execute_sequentially_even_when_calls_overlap(
    session: ConversationSession,
) -> None:
    service = ConcurrentGenerationService()
    executor = ToolExecutor(quotation_service=cast(QuotationService, service))
    context = turn(session)

    await asyncio.gather(
        executor.execute("create_quotation", "{}", session, context),
        executor.execute("create_quotation", "{}", session, context),
    )

    assert service.maximum_active == 1
