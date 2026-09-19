"""Redis-backed integration coverage for the alternate development transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from app.api.dependencies import get_conversation_service
from app.api.routes.dev_chat import get_inbound_text_limit
from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.agent import AgentResult, PreviewDeliveryMetadata
from app.schemas.quotation import GeneratedQuotation
from app.schemas.session import ConversationMessage, ConversationSession, ConversationState
from app.services.conversation_service import ConversationService
from app.services.quotation_service import CurrentTurnContext, QuotationService
from app.services.session_service import SessionService
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 15
PRODUCT_ID = "GDF-SDI-001"
QUOTATION_ID = "GDF-Q-20260919-A82F"


def isolated_redis_url() -> str:
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_REDIS_DATABASE}", parts.query, ""))


def make_settings() -> Settings:
    return Settings(
        app_env="test",
        redis_url=isolated_redis_url(),
        session_ttl_seconds=30,
        recent_message_limit=20,
        max_inbound_text_length=1_000,
    )


class RecordingPDFService:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    async def generate_quotation_pdf(self, quotation: GeneratedQuotation) -> Path:
        self.calls += 1
        return self.path


class ScriptedAgent:
    """Drive real session/quotation services without external provider calls."""

    def __init__(self, quotation_service: QuotationService) -> None:
        self._quotation_service = quotation_service

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        generated: GeneratedQuotation | None = None
        preview: PreviewDeliveryMetadata | None = None

        if user_message == "Prepare quote":
            self._quotation_service.add_item(session, PRODUCT_ID, 2)
            self._quotation_service.set_customer_information(session, name="Development User")
            self._quotation_service.prepare_quotation(session, turn_context.turn_id)
            fingerprint = session.prepared_preview_fingerprint
            assert fingerprint is not None
            preview = PreviewDeliveryMetadata(
                fingerprint=fingerprint,
                originating_turn_id=turn_context.turn_id,
            )
            response_text = "Preview ready."
        elif user_message == "Confirmed":
            self._quotation_service.confirm(session, turn_context)
            generated = await self._quotation_service.generate(session)
            response_text = "Quotation generated."
        else:
            prior_turns = sum(message.role == "user" for message in session.recent_messages)
            response_text = f"{session.customer_phone}:turn-{prior_turns + 1}"

        session.recent_messages.extend(
            [
                ConversationMessage(role="user", content=user_message),
                ConversationMessage(role="assistant", content=response_text),
            ]
        )
        return AgentResult(
            response_text=response_text,
            updated_session=session,
            generated_quote=generated,
            prepared_preview=preview,
        )


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(isolated_redis_url(), decode_responses=True)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def conversation_dependencies(
    redis_client: Redis,
    tmp_path: Path,
) -> tuple[ConversationService, SessionService, RecordingPDFService]:
    settings = make_settings()
    sessions = SessionService(redis_client, settings=settings)
    pdf = RecordingPDFService(tmp_path / "internal" / "secret-quotation.pdf")
    quotations = QuotationService(
        pdf_service=pdf,
        clock=lambda: datetime(2026, 9, 19, 12, tzinfo=UTC),
        quotation_id_factory=lambda prefix, issued_at: QUOTATION_ID,
    )
    service = ConversationService(
        redis_client,
        settings=settings,
        session_service=sessions,
        agent=ScriptedAgent(quotations),
        quotation_service=quotations,
    )
    return service, sessions, pdf


@pytest.fixture
async def client(
    conversation_dependencies: tuple[ConversationService, SessionService, RecordingPDFService],
) -> AsyncIterator[httpx.AsyncClient]:
    service, _, _ = conversation_dependencies
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_inbound_text_limit] = lambda: 1_000
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as instance:
            yield instance
    finally:
        app.dependency_overrides = original_overrides


async def test_same_development_identity_preserves_multi_turn_context(
    client: httpx.AsyncClient,
    conversation_dependencies: tuple[ConversationService, SessionService, RecordingPDFService],
) -> None:
    _, sessions, _ = conversation_dependencies

    first = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "test-user-001", "message": "First"},
    )
    second = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "test-user-001", "message": "Second"},
    )
    stored = await sessions.get_session("dev:test-user-001")

    assert first.status_code == 200
    assert first.json()["response"] == "dev:test-user-001:turn-1"
    assert second.status_code == 200
    assert second.json()["response"] == "dev:test-user-001:turn-2"
    assert [message.content for message in stored.recent_messages] == [
        "First",
        "dev:test-user-001:turn-1",
        "Second",
        "dev:test-user-001:turn-2",
    ]


async def test_development_identities_remain_independent(client: httpx.AsyncClient) -> None:
    first, second = await _post_independent_turns(client)

    assert first.json()["response"] == "dev:user-A:turn-1"
    assert second.json()["response"] == "dev:user-B:turn-1"
    assert first.json()["cart"] == second.json()["cart"] == []


async def _post_independent_turns(
    client: httpx.AsyncClient,
) -> tuple[httpx.Response, httpx.Response]:
    first = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "user-A", "message": "Hello"},
    )
    second = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "user-B", "message": "Hello"},
    )
    return first, second


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "", "message": "Hello"},
        {"session_id": "invalid.session", "message": "Hello"},
        {"session_id": "space user", "message": "Hello"},
        {"session_id": "x" * 101, "message": "Hello"},
        {"session_id": "valid-user", "message": "   "},
        {"session_id": "valid-user", "message": "Hello", "state": "NEW"},
    ],
)
async def test_invalid_requests_are_rejected_without_creating_sessions(
    client: httpx.AsyncClient,
    redis_client: Redis,
    payload: dict[str, object],
) -> None:
    response = await client.post("/api/v1/dev/chat", json=payload)

    assert response.status_code == 422
    assert await redis_client.dbsize() == 0


async def test_configured_message_length_limit_is_enforced_before_processing(
    client: httpx.AsyncClient,
    redis_client: Redis,
) -> None:
    app.dependency_overrides[get_inbound_text_limit] = lambda: 5

    response = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "valid-user", "message": "123456"},
    )

    assert response.status_code == 422
    assert await redis_client.dbsize() == 0


async def test_preview_is_acknowledged_and_generated_id_has_no_internal_path(
    client: httpx.AsyncClient,
    conversation_dependencies: tuple[ConversationService, SessionService, RecordingPDFService],
) -> None:
    _, sessions, pdf = conversation_dependencies

    preview = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "quote-user", "message": "Prepare quote"},
    )
    after_preview = await sessions.get_session("dev:quote-user")
    generated = await client.post(
        "/api/v1/dev/chat",
        json={"session_id": "quote-user", "message": "Confirmed"},
    )

    assert preview.status_code == 200
    assert preview.json()["state"] == ConversationState.AWAITING_CONFIRMATION
    assert preview.json()["generated_quotation_id"] is None
    assert preview.json()["cart"] == [{"product_id": PRODUCT_ID, "quantity": 2}]
    assert after_preview.preview_delivered is True
    assert after_preview.state is ConversationState.AWAITING_CONFIRMATION

    assert generated.status_code == 200
    assert generated.json()["state"] == ConversationState.QUOTE_GENERATED
    assert generated.json()["generated_quotation_id"] == QUOTATION_ID
    assert "secret-quotation.pdf" not in generated.text
    assert "pdf_path" not in generated.text
    assert pdf.calls == 1


async def test_dev_chat_is_published_in_nonproduction_swagger(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/dev/chat" in response.json()["paths"]


def test_dev_chat_is_not_registered_in_production() -> None:
    environment = dict(os.environ)
    environment["APP_ENV"] = "production"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from app.main import app; "
                "print(json.dumps(sorted(app.openapi()['paths'])))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )
    paths = json.loads(result.stdout)

    assert "/health" in paths
    assert "/api/v1/dev/chat" not in paths
