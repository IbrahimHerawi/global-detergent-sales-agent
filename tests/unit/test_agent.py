"""Mocked tests for the bounded asynchronous Responses API agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import patch

import httpx2
import pytest
from app.agent.agent import MAX_TOOL_ITERATIONS, AIAgent
from app.agent.tool_definitions import TOOL_DEFINITIONS
from app.agent.tool_executor import (
    PreparedPreviewMetadata,
    ToolExecutionResult,
    ToolExecutor,
)
from app.core.config import Settings
from app.core.exceptions import TECHNICAL_FALLBACK_MESSAGE, ToolExecutionError
from app.schemas.agent import AgentResult, PreviewDeliveryMetadata
from app.schemas.quotation import CustomerInfo, GeneratedQuotation, QuoteLine, QuoteTotals
from app.schemas.session import ConversationMessage, ConversationSession
from app.services.quotation_service import CurrentTurnContext
from openai import APIError, APITimeoutError, AsyncOpenAI, InternalServerError, RateLimitError
from openai.types.responses import Response
from pydantic import SecretStr


class FakeResponses:
    """Record create arguments and return or raise queued outcomes."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected Responses API call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.responses = FakeResponses(outcomes)


class RecordingToolExecutor:
    """Return configured tool results while recording sequential calls."""

    def __init__(
        self,
        results: Mapping[str, ToolExecutionResult | BaseException] | None = None,
    ) -> None:
        self.results = dict(results or {})
        self.calls: list[tuple[str, str]] = []

    async def execute(
        self,
        tool_name: str,
        arguments: str,
        session: ConversationSession,
        current_turn_context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        del session, current_turn_context
        self.calls.append((tool_name, arguments))
        configured = self.results.get(tool_name)
        if isinstance(configured, BaseException):
            raise configured
        if configured is not None:
            return configured
        return ToolExecutionResult(model_output={"tool": tool_name})


def settings(
    *,
    recent_message_limit: int = 20,
    max_tool_iterations: int = MAX_TOOL_ITERATIONS,
) -> Settings:
    return Settings(
        openai_model="gpt-test",
        recent_message_limit=recent_message_limit,
        max_tool_iterations=max_tool_iterations,
    )


def session() -> ConversationSession:
    return ConversationSession(customer_phone="+97450000000")


def turn(
    current_session: ConversationSession,
    message: str,
    *,
    turn_id: str = "turn-current",
) -> CurrentTurnContext:
    return CurrentTurnContext(
        session_id=current_session.session_id,
        turn_id=turn_id,
        customer_message=message,
        preceding_turn_ids=(),
    )


def response_with_text(text: str, *, response_id: str = "resp-final") -> Response:
    return Response.model_validate(
        {
            "id": response_id,
            "created_at": 1.0,
            "model": "gpt-test",
            "object": "response",
            "output": [
                {
                    "type": "message",
                    "id": f"msg-{response_id}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
        }
    )


def response_with_calls(
    *calls: tuple[str, str, str],
    response_id: str = "resp-tools",
) -> Response:
    return Response.model_validate(
        {
            "id": response_id,
            "created_at": 1.0,
            "model": "gpt-test",
            "object": "response",
            "output": [
                {
                    "type": "function_call",
                    "id": f"fc-{index}",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "status": "completed",
                }
                for index, (call_id, name, arguments) in enumerate(calls)
            ],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
        }
    )


def generated_quote() -> GeneratedQuotation:
    return GeneratedQuotation.with_pdf_path(
        pdf_path="/private/quotes/GDF-Q-TEST.pdf",
        quotation_id="GDF-Q-TEST",
        issued_at=datetime(2026, 9, 19, tzinfo=UTC),
        valid_until=date(2026, 10, 3),
        customer=CustomerInfo(phone="+97450000000", name="Aisha"),
        items=[
            QuoteLine(
                product_id="GDF-FLC-001",
                sku="FLC-5L",
                description="Floor cleaner",
                quantity=2,
                unit_price=Decimal("25.00"),
                subtotal=Decimal("50.00"),
            )
        ],
        totals=QuoteTotals(
            subtotal=Decimal("50.00"),
            discount=Decimal("0.00"),
            tax=Decimal("0.00"),
            total=Decimal("50.00"),
            currency="QAR",
        ),
        validity_days=14,
        terms=["Subject to availability."],
    )


def agent(
    outcomes: list[object],
    executor: RecordingToolExecutor | None = None,
    *,
    configured_settings: Settings | None = None,
) -> tuple[AIAgent, FakeClient, RecordingToolExecutor]:
    fake_client = FakeClient(outcomes)
    fake_executor = executor or RecordingToolExecutor()
    instance = AIAgent(
        settings=configured_settings or settings(),
        client=cast(AsyncOpenAI, fake_client),
        tool_executor=cast(ToolExecutor, fake_executor),
    )
    return instance, fake_client, fake_executor


def function_outputs(call: Mapping[str, object]) -> list[dict[str, object]]:
    input_items = call["input"]
    assert isinstance(input_items, list)
    return [
        item
        for item in input_items
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def test_default_client_uses_configured_key_and_timeout_without_making_a_call() -> None:
    fake_client = FakeClient([])
    fake_executor = RecordingToolExecutor()
    configured = Settings(
        openai_model="gpt-configured",
        openai_api_key=SecretStr("sk-configured-secret"),
        openai_timeout_seconds=12.5,
    )

    with patch("app.agent.agent.AsyncOpenAI", return_value=fake_client) as constructor:
        AIAgent(
            settings=configured,
            tool_executor=cast(ToolExecutor, fake_executor),
        )

    constructor.assert_called_once_with(
        api_key="sk-configured-secret",
        timeout=12.5,
    )
    assert fake_client.responses.calls == []


@pytest.mark.asyncio
async def test_single_round_sends_instructions_tools_and_appends_history_once() -> None:
    current_session = session()
    current_session.recent_messages.append(
        ConversationMessage(role="assistant", content="Earlier reply")
    )
    instance, fake_client, executor = agent([response_with_text("How can I help?")])

    result = await instance.handle_message(
        current_session,
        "Hello",
        turn(current_session, "Hello"),
    )

    assert result.response_text == "How can I help?"
    assert result.updated_session is current_session
    assert result.generated_quote is None
    assert result.prepared_preview is None
    assert executor.calls == []
    assert [(message.role, message.content) for message in current_session.recent_messages] == [
        ("assistant", "Earlier reply"),
        ("user", "Hello"),
        ("assistant", "How can I help?"),
    ]

    assert len(fake_client.responses.calls) == 1
    request = fake_client.responses.calls[0]
    assert request["model"] == "gpt-test"
    assert request["parallel_tool_calls"] is False
    assert request["stream"] is False
    assert request["tools"] == TOOL_DEFINITIONS
    assert isinstance(request["instructions"], str)
    assert "## 8. Current session context" in request["instructions"]
    assert request["input"] == [{"role": "user", "content": "Hello"}]


@pytest.mark.asyncio
async def test_multiple_rounds_preserve_response_items_and_match_call_ids() -> None:
    current_session = session()
    instance, fake_client, executor = agent(
        [
            response_with_calls(
                ("call-company", "get_company_information", "{}"),
                response_id="resp-1",
            ),
            response_with_calls(
                ("call-search", "search_products", '{"query":"floor"}'),
                response_id="resp-2",
            ),
            response_with_text("I found the floor cleaner.", response_id="resp-3"),
        ],
        RecordingToolExecutor(
            {
                "get_company_information": ToolExecutionResult(
                    model_output={"name": "Global Detergent Factory"}
                ),
                "search_products": ToolExecutionResult(
                    model_output={"matches": [{"product_id": "GDF-FLC-001"}]}
                ),
            }
        ),
    )

    result = await instance.handle_message(
        current_session,
        "Find floor cleaner",
        turn(current_session, "Find floor cleaner"),
    )

    assert result.response_text == "I found the floor cleaner."
    assert executor.calls == [
        ("get_company_information", "{}"),
        ("search_products", '{"query":"floor"}'),
    ]
    assert len(fake_client.responses.calls) == 3
    second_input = fake_client.responses.calls[1]["input"]
    assert isinstance(second_input, list)
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == "call-company"
        for item in second_input
    )
    assert function_outputs(fake_client.responses.calls[1]) == [
        {
            "type": "function_call_output",
            "call_id": "call-company",
            "output": '{"name":"Global Detergent Factory"}',
        }
    ]
    assert [item["call_id"] for item in function_outputs(fake_client.responses.calls[2])] == [
        "call-company",
        "call-search",
    ]


@pytest.mark.asyncio
async def test_multiple_calls_in_one_response_execute_sequentially_with_each_output() -> None:
    current_session = session()
    instance, fake_client, executor = agent(
        [
            response_with_calls(
                ("call-categories", "list_product_categories", "{}"),
                ("call-products", "list_products", '{"category":null}'),
            ),
            response_with_text("Here are the products."),
        ]
    )

    result = await instance.handle_message(
        current_session,
        "Show products",
        turn(current_session, "Show products"),
    )

    assert result.response_text == "Here are the products."
    assert executor.calls == [
        ("list_product_categories", "{}"),
        ("list_products", '{"category":null}'),
    ]
    outputs = function_outputs(fake_client.responses.calls[1])
    assert [output["call_id"] for output in outputs] == [
        "call-categories",
        "call-products",
    ]


@pytest.mark.asyncio
async def test_prepared_preview_returns_deterministic_text_and_stops_before_confirmation() -> None:
    current_session = session()
    preview_metadata = PreparedPreviewMetadata(
        fingerprint="a" * 64,
        originating_turn_id="turn-current",
    )
    executor = RecordingToolExecutor(
        {
            "prepare_quotation": ToolExecutionResult(
                model_output={"quote_confirmed": False},
                prepared_preview=preview_metadata,
            ),
            "get_quote_summary": ToolExecutionResult(
                model_output={"summary": "Backend-calculated quotation preview"}
            ),
            "confirm_quotation": ToolExecutionResult(model_output={"confirmed": True}),
        }
    )
    instance, fake_client, _ = agent(
        [
            response_with_calls(
                ("call-preview", "prepare_quotation", "{}"),
                ("call-confirm", "confirm_quotation", "{}"),
            )
        ],
        executor,
    )

    result = await instance.handle_message(
        current_session,
        "Prepare the quote",
        turn(current_session, "Prepare the quote"),
    )

    assert result.response_text == "Backend-calculated quotation preview"
    assert result.prepared_preview == PreviewDeliveryMetadata(
        fingerprint="a" * 64,
        originating_turn_id="turn-current",
    )
    assert executor.calls == [
        ("prepare_quotation", "{}"),
        ("get_quote_summary", "{}"),
    ]
    assert len(fake_client.responses.calls) == 1
    assert current_session.recent_messages[-1].content == result.response_text


@pytest.mark.asyncio
async def test_generated_artifact_survives_a_later_model_timeout() -> None:
    current_session = session()
    generated = generated_quote()
    timeout = APITimeoutError(httpx2.Request("POST", "https://api.openai.test/responses"))
    instance, _, _ = agent(
        [
            response_with_calls(("call-create", "create_quotation", "{}")),
            timeout,
        ],
        RecordingToolExecutor(
            {
                "create_quotation": ToolExecutionResult(
                    model_output={
                        "quotation_id": generated.quotation_id,
                        "total": "50.00",
                        "currency": "QAR",
                        "pdf_generated": True,
                    },
                    generated_quote=generated,
                )
            }
        ),
    )

    result = await instance.handle_message(
        current_session,
        "Create it",
        turn(current_session, "Create it"),
    )

    assert result.response_text == TECHNICAL_FALLBACK_MESSAGE
    assert result.generated_quote is generated
    assert result.generated_quote.pdf_path == "/private/quotes/GDF-Q-TEST.pdf"
    assert current_session.recent_messages[-1].content == TECHNICAL_FALLBACK_MESSAGE


def api_failures() -> list[APIError]:
    request = httpx2.Request("POST", "https://api.openai.test/responses")
    return [
        APITimeoutError(request),
        RateLimitError(
            "rate limited",
            response=httpx2.Response(429, request=request),
            body=None,
        ),
        InternalServerError(
            "server failed",
            response=httpx2.Response(500, request=request),
            body=None,
        ),
    ]


@pytest.mark.parametrize("failure", api_failures())
@pytest.mark.asyncio
async def test_openai_timeout_rate_limit_and_server_failures_use_fallback(
    failure: APIError,
) -> None:
    current_session = session()
    instance, _, _ = agent([failure])

    result = await instance.handle_message(
        current_session,
        "Hello",
        turn(current_session, "Hello"),
    )

    assert result.response_text == TECHNICAL_FALLBACK_MESSAGE
    assert [message.role for message in current_session.recent_messages] == [
        "user",
        "assistant",
    ]


def malformed_responses() -> list[object]:
    empty_output = response_with_text("placeholder")
    empty_output.output = []
    incomplete = response_with_text("partial")
    incomplete.status = "incomplete"
    return [
        object(),
        empty_output,
        incomplete,
        response_with_text("   "),
        response_with_calls(("", "search_products", '{"query":"floor"}')),
        response_with_calls(
            ("duplicate", "list_product_categories", "{}"),
            ("duplicate", "list_products", '{"category":null}'),
        ),
    ]


@pytest.mark.parametrize("malformed", malformed_responses())
@pytest.mark.asyncio
async def test_invalid_or_malformed_responses_use_fallback(malformed: object) -> None:
    current_session = session()
    instance, _, executor = agent([malformed])

    result = await instance.handle_message(
        current_session,
        "Hello",
        turn(current_session, "Hello"),
    )

    assert result.response_text == TECHNICAL_FALLBACK_MESSAGE
    assert executor.calls == []


@pytest.mark.asyncio
async def test_malformed_tool_arguments_from_executor_use_fallback() -> None:
    current_session = session()
    instance, _, executor = agent(
        [response_with_calls(("call-search", "search_products", "not-json"))],
        RecordingToolExecutor({"search_products": ToolExecutionError("malformed model arguments")}),
    )

    result = await instance.handle_message(
        current_session,
        "Find a product",
        turn(current_session, "Find a product"),
    )

    assert result.response_text == TECHNICAL_FALLBACK_MESSAGE
    assert executor.calls == [("search_products", "not-json")]


@pytest.mark.asyncio
async def test_tool_iteration_overflow_stops_at_eight_rounds() -> None:
    current_session = session()
    outcomes: list[object] = [
        response_with_calls(
            (f"call-{index}", "list_product_categories", "{}"),
            response_id=f"resp-{index}",
        )
        for index in range(MAX_TOOL_ITERATIONS + 1)
    ]
    instance, fake_client, executor = agent(outcomes)

    result = await instance.handle_message(
        current_session,
        "Keep looking",
        turn(current_session, "Keep looking"),
    )

    assert result.response_text == TECHNICAL_FALLBACK_MESSAGE
    assert len(fake_client.responses.calls) == MAX_TOOL_ITERATIONS
    assert len(executor.calls) == MAX_TOOL_ITERATIONS
    assert fake_client.responses.outcomes


@pytest.mark.asyncio
async def test_history_is_trimmed_after_one_user_and_one_assistant_append() -> None:
    current_session = session()
    current_session.recent_messages = [
        ConversationMessage(role="user", content=f"old-{index}") for index in range(5)
    ]
    instance, _, _ = agent(
        [response_with_text("Final")],
        configured_settings=settings(recent_message_limit=3),
    )

    await instance.handle_message(
        current_session,
        "Current",
        turn(current_session, "Current"),
    )

    assert [(message.role, message.content) for message in current_session.recent_messages] == [
        ("user", "old-4"),
        ("user", "Current"),
        ("assistant", "Final"),
    ]


def test_agent_result_keeps_preview_metadata_internal() -> None:
    current_session = session()
    result = AgentResult(
        response_text="Preview",
        updated_session=current_session,
        prepared_preview=PreviewDeliveryMetadata(
            fingerprint="a" * 64,
            originating_turn_id="turn-preview",
        ),
    )

    assert result.preview_delivery_metadata is result.prepared_preview
    assert "prepared_preview" not in result.model_dump(mode="json")
