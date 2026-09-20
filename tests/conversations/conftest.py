"""Reusable fixtures for deterministic and live conversation evaluations."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from app.agent.agent import AIAgent
from app.agent.tool_executor import ToolExecutionResult, ToolExecutor
from app.core.config import Settings, get_settings
from app.repositories.company_repository import CompanyRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.agent import AgentResult
from app.schemas.session import ConversationSession, ConversationState
from app.services.company_service import CompanyService
from app.services.pdf_service import PDFService
from app.services.product_service import ProductService
from app.services.quotation_service import CurrentTurnContext, QuotationService
from app.services.quotation_storage import LocalQuotationStorage
from openai import AsyncOpenAI
from openai.types.responses import Response
from pydantic import SecretStr
from redis.asyncio import Redis

from tests.conversations.scenarios import (
    DISINFECTANT_PRODUCT_ID,
    FIXED_QUOTATION_ID,
    FLOOR_PRODUCT_ID,
    GLASS_PRODUCT_ID,
    ConversationScenario,
    ExpectedToolCall,
    SetupName,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_REDIS_DATABASE = 15


def response_with_text(text: str, *, response_id: str) -> Response:
    """Create a validated Responses API assistant-message fixture."""
    return Response.model_validate(
        {
            "id": response_id,
            "created_at": 1.0,
            "model": "gpt-scripted",
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
    calls: tuple[ExpectedToolCall, ...],
    *,
    response_id: str,
) -> Response:
    """Create one validated Responses API function-call fixture."""
    return Response.model_validate(
        {
            "id": response_id,
            "created_at": 1.0,
            "model": "gpt-scripted",
            "object": "response",
            "output": [
                {
                    "type": "function_call",
                    "id": f"fc-{response_id}-{index}",
                    "call_id": f"call-{response_id}-{index}",
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "status": "completed",
                }
                for index, tool_call in enumerate(calls)
            ],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
        }
    )


class ScriptedResponses:
    """A queue-backed Responses API fake that records complete request payloads."""

    def __init__(self) -> None:
        self.outcomes: list[Response] = []
        self.calls: list[dict[str, object]] = []

    def queue_turn(
        self,
        calls: tuple[ExpectedToolCall, ...],
        *,
        response_text: str,
        turn_number: int,
    ) -> None:
        if calls:
            self.outcomes.append(
                response_with_calls(calls, response_id=f"script-{turn_number}-tools")
            )
            # prepare_quotation intentionally ends the agent turn at the
            # deterministic backend preview instead of returning to the model.
            if any(tool_call.name == "prepare_quotation" for tool_call in calls):
                return
        self.outcomes.append(
            response_with_text(response_text, response_id=f"script-{turn_number}-text")
        )

    async def create(self, **kwargs: object) -> Response:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("scripted Responses API queue is empty")
        return self.outcomes.pop(0)


class ScriptedClient:
    """Minimal AsyncOpenAI-compatible client surface used by :class:`AIAgent`."""

    def __init__(self, responses: ScriptedResponses) -> None:
        self.responses = responses


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    """One real executor invocation and its model-visible backend result."""

    name: str
    arguments: str
    result: ToolExecutionResult


class RecordingToolExecutor:
    """Record tool choices while delegating every call to the real executor."""

    def __init__(self, delegate: ToolExecutor) -> None:
        self._delegate = delegate
        self.calls: list[RecordedToolCall] = []

    async def execute(
        self,
        tool_name: str,
        arguments: str,
        session: ConversationSession,
        current_turn_context: CurrentTurnContext,
    ) -> ToolExecutionResult:
        result = await self._delegate.execute(
            tool_name,
            arguments,
            session,
            current_turn_context,
        )
        self.calls.append(RecordedToolCall(tool_name, arguments, result))
        return result


class ConversationHarness:
    """Run multi-turn conversations through a real agent/tool/business pipeline."""

    def __init__(
        self,
        *,
        agent: AIAgent,
        session: ConversationSession,
        quotation_service: QuotationService,
        recorder: RecordingToolExecutor,
        scripted_responses: ScriptedResponses | None,
        live_client: AsyncOpenAI | None = None,
        preceding_turn_ids: tuple[str, ...] = (),
    ) -> None:
        self.agent = agent
        self.session = session
        self.quotation_service = quotation_service
        self.recorder = recorder
        self.scripted_responses = scripted_responses
        self.live_client = live_client
        self.preceding_turn_ids = list(preceding_turn_ids)
        self.results: list[AgentResult] = []

    async def send(
        self,
        message: str,
        *,
        scripted_calls: tuple[ExpectedToolCall, ...] | None = None,
        deliver_preview: bool = True,
        response_text: str = "Scripted assistant response.",
    ) -> AgentResult:
        """Process one turn and optionally acknowledge a returned preview."""
        turn_number = len(self.results) + 1
        turn_id = f"turn-{turn_number}"
        if scripted_calls is not None:
            if self.scripted_responses is None:
                raise AssertionError("live harness cannot accept scripted calls")
            self.scripted_responses.queue_turn(
                scripted_calls,
                response_text=response_text,
                turn_number=turn_number,
            )

        context = CurrentTurnContext(
            session_id=self.session.session_id,
            turn_id=turn_id,
            customer_message=message,
            preceding_turn_ids=tuple(self.preceding_turn_ids),
        )
        result = await self.agent.handle_message(self.session, message, context)
        assert result.updated_session is self.session
        if deliver_preview and result.prepared_preview is not None:
            self.quotation_service.mark_ready_for_confirmation(
                self.session,
                result.prepared_preview.fingerprint,
                result.prepared_preview.originating_turn_id,
            )
        self.preceding_turn_ids.append(turn_id)
        self.results.append(result)
        return result

    async def aclose(self) -> None:
        """Close a live OpenAI transport on the same event loop that used it."""
        if self.live_client is not None:
            await self.live_client.close()
            self.live_client = None


def _build_business_pipeline(
    artifact_root: Path,
) -> tuple[QuotationService, ToolExecutor]:
    company_repository = CompanyRepository(REPOSITORY_ROOT / "app/data/company.json")
    product_repository = ProductRepository(REPOSITORY_ROOT / "app/data/products.json")
    rules_repository = QuotationRulesRepository(REPOSITORY_ROOT / "app/data/quotation_rules.json")
    pdf_service = PDFService(
        LocalQuotationStorage(root=artifact_root),
        company_repository=company_repository,
    )
    quotation_service = QuotationService(
        product_repository=product_repository,
        rules_repository=rules_repository,
        pdf_service=pdf_service,
        clock=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        quotation_id_factory=lambda prefix, issued_at: FIXED_QUOTATION_ID,
    )
    executor = ToolExecutor(
        company_service=CompanyService(company_repository),
        product_service=ProductService(product_repository),
        quotation_service=quotation_service,
    )
    return quotation_service, executor


def _session_for_setup(
    setup: SetupName,
    quotation_service: QuotationService,
) -> tuple[ConversationSession, tuple[str, ...]]:
    session = ConversationSession(customer_phone="+97450000000")
    preceding_turn_ids: tuple[str, ...] = ()

    if setup == "new":
        return session, preceding_turn_ids
    if setup == "selected_floor":
        session.selected_product_id = FLOOR_PRODUCT_ID
        session.state = ConversationState.PRODUCT_DISCUSSION
        return session, preceding_turn_ids
    if setup == "selected_disinfectant":
        session.selected_product_id = DISINFECTANT_PRODUCT_ID
        session.state = ConversationState.PRODUCT_DISCUSSION
        return session, preceding_turn_ids

    quotation_service.add_item(session, FLOOR_PRODUCT_ID, 20)
    session.selected_product_id = FLOOR_PRODUCT_ID
    if setup == "floor_cart":
        return session, preceding_turn_ids
    if setup == "two_item_cart":
        quotation_service.add_item(session, GLASS_PRODUCT_ID, 10)
        return session, preceding_turn_ids

    quotation_service.set_customer_information(
        session,
        company_name="ABC Facility Management",
    )
    if setup == "named_floor_cart":
        return session, preceding_turn_ids
    if setup == "confirmable_floor_cart":
        preview_turn_id = "setup-preview-turn"
        quotation_service.prepare_quotation(session, preview_turn_id)
        fingerprint = session.prepared_preview_fingerprint
        assert fingerprint is not None
        quotation_service.mark_ready_for_confirmation(
            session,
            fingerprint,
            preview_turn_id,
        )
        return session, (preview_turn_id,)
    raise AssertionError(f"unhandled scenario setup: {setup}")


def build_conversation_harness(
    scenario: ConversationScenario,
    artifact_root: Path,
    *,
    live_settings: Settings | None = None,
) -> ConversationHarness:
    """Build a scenario harness with either scripted or real OpenAI responses."""
    quotation_service, executor = _build_business_pipeline(artifact_root)
    recorder = RecordingToolExecutor(executor)
    session, preceding_turn_ids = _session_for_setup(scenario.setup, quotation_service)

    scripted_responses: ScriptedResponses | None = None
    live_client: AsyncOpenAI | None = None
    if live_settings is None:
        scripted_responses = ScriptedResponses()
        client = cast(AsyncOpenAI, ScriptedClient(scripted_responses))
        agent_settings = Settings(openai_model="gpt-scripted")
        agent = AIAgent(
            settings=agent_settings,
            client=client,
            tool_executor=cast(ToolExecutor, recorder),
        )
    else:
        api_key = live_settings.openai_api_key
        if api_key is None:  # guarded by the live settings fixture
            raise ValueError("live OpenAI API key is required")
        live_client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            timeout=live_settings.openai_timeout_seconds,
        )
        agent = AIAgent(
            settings=live_settings,
            client=live_client,
            tool_executor=cast(ToolExecutor, recorder),
        )

    return ConversationHarness(
        agent=agent,
        session=session,
        quotation_service=quotation_service,
        recorder=recorder,
        scripted_responses=scripted_responses,
        live_client=live_client,
        preceding_turn_ids=preceding_turn_ids,
    )


@pytest.fixture
def conversation_harness_factory(
    tmp_path: Path,
) -> Callable[[ConversationScenario], ConversationHarness]:
    """Return isolated scripted harnesses with real per-case PDF storage."""
    counter = 0

    def factory(scenario: ConversationScenario) -> ConversationHarness:
        nonlocal counter
        counter += 1
        return build_conversation_harness(
            scenario,
            tmp_path / f"case-{counter}" / "quotes",
        )

    return factory


def _test_redis_url() -> str:
    configured = get_settings().redis_url.rsplit("/", maxsplit=1)[0]
    return f"{configured}/{TEST_REDIS_DATABASE}"


@pytest.fixture
async def conversation_redis() -> AsyncIterator[Redis]:
    """Provide clean Compose Redis storage for session-expiry acceptance."""
    client = Redis.from_url(_test_redis_url(), decode_responses=True)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture(scope="session")
def live_model_settings() -> Settings:
    """Load explicit live-evaluation configuration without ever inventing it."""
    if os.getenv("RUN_LIVE_MODEL_EVAL") != "1":
        pytest.skip("set RUN_LIVE_MODEL_EVAL=1 to run paid live-model evaluations")

    configured = Settings()
    api_key = configured.openai_api_key
    model = configured.openai_model
    if api_key is None or not api_key.get_secret_value().strip() or model is None:
        pytest.fail("live evaluation requires OPENAI_API_KEY and OPENAI_MODEL")
    if api_key.get_secret_value().strip().startswith("<"):
        pytest.fail("OPENAI_API_KEY is still a placeholder")
    if str(model).strip().startswith("<"):
        pytest.fail("OPENAI_MODEL is still a placeholder")
    return Settings(
        app_env="test",
        ai_enabled=True,
        openai_api_key=SecretStr(api_key.get_secret_value()),
        openai_model=model,
        openai_timeout_seconds=configured.openai_timeout_seconds,
        max_tool_iterations=configured.max_tool_iterations,
        recent_message_limit=configured.recent_message_limit,
    )


def pytest_report_header(config: pytest.Config) -> str:
    """Report the exact opt-in live model configuration in Docker test output."""
    del config
    if os.getenv("RUN_LIVE_MODEL_EVAL") != "1":
        return "live-model evaluation: disabled (offline suite uses scripted OpenAI responses)"
    settings = Settings()
    return (
        "live-model evaluation: enabled; "
        f"model={settings.openai_model!s}; timeout={settings.openai_timeout_seconds}s; "
        f"max_tool_iterations={settings.max_tool_iterations}; "
        f"recent_message_limit={settings.recent_message_limit}"
    )


__all__ = [
    "ConversationHarness",
    "RecordedToolCall",
    "RecordingToolExecutor",
    "build_conversation_harness",
]
