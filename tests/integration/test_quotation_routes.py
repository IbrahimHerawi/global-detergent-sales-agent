"""Integration coverage for internal quotation testing endpoints."""

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
from app.api.dependencies import (
    get_conversation_service,
    get_quotation_service,
    get_session_service,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.agent import AgentResult, PreviewDeliveryMetadata
from app.schemas.quotation import CustomerInfo, GeneratedQuotation, QuoteCartItem
from app.schemas.session import ConversationMessage, ConversationSession, ConversationState
from app.services.conversation_service import ConversationService
from app.services.quotation_service import CurrentTurnContext, QuotationService
from app.services.session_service import SessionService
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 15
PRODUCT_ID = "GDF-SDI-001"
QUOTATION_ID = "GDF-Q-20260920-A82F"
SESSION_ID = "existing-development-session"


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


class ConfirmationOnlyAgent:
    """Reach confirmation only through the ordinary development-chat flow."""

    def __init__(self, quotation_service: QuotationService) -> None:
        self._quotation_service = quotation_service

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
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
            response_text = "Confirmation recorded."
        else:
            response_text = "No action."

        session.recent_messages.extend(
            [
                ConversationMessage(role="user", content=user_message),
                ConversationMessage(role="assistant", content=response_text),
            ]
        )
        return AgentResult(
            response_text=response_text,
            updated_session=session,
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
def quotation_dependencies(
    redis_client: Redis,
    tmp_path: Path,
) -> tuple[QuotationService, SessionService, ConversationService, RecordingPDFService]:
    settings = make_settings()
    sessions = SessionService(redis_client, settings=settings)
    pdf = RecordingPDFService(tmp_path / "private" / "quotation.pdf")
    quotations = QuotationService(
        pdf_service=pdf,
        clock=lambda: datetime(2026, 9, 20, 12, tzinfo=UTC),
        quotation_id_factory=lambda prefix, issued_at: QUOTATION_ID,
    )
    conversations = ConversationService(
        redis_client,
        settings=settings,
        session_service=sessions,
        agent=ConfirmationOnlyAgent(quotations),
        quotation_service=quotations,
    )
    return quotations, sessions, conversations, pdf


@pytest.fixture
async def client(
    quotation_dependencies: tuple[
        QuotationService,
        SessionService,
        ConversationService,
        RecordingPDFService,
    ],
) -> AsyncIterator[httpx.AsyncClient]:
    quotations, sessions, conversations, _ = quotation_dependencies
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_quotation_service] = lambda: quotations
    app.dependency_overrides[get_session_service] = lambda: sessions
    app.dependency_overrides[get_conversation_service] = lambda: conversations
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as instance:
            yield instance
    finally:
        app.dependency_overrides = original_overrides


async def test_preview_matches_service_without_persistence_or_pdf(
    client: httpx.AsyncClient,
    redis_client: Redis,
    quotation_dependencies: tuple[
        QuotationService,
        SessionService,
        ConversationService,
        RecordingPDFService,
    ],
) -> None:
    quotations, _, _, pdf = quotation_dependencies
    customer = CustomerInfo(
        name="Preview Customer",
        phone="+97450000000",
        email="preview@example.com",
    )
    cart = [QuoteCartItem(product_id=PRODUCT_ID, quantity=3)]
    expected_session = ConversationSession(
        customer_phone=customer.phone,
        customer=customer,
        cart=cart,
        state=ConversationState.BUILDING_QUOTE,
    )
    expected = quotations.prepare_quotation(expected_session, "expected-preview")

    response = await client.post(
        "/api/v1/quotes/preview",
        json={
            "customer": customer.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in cart],
        },
    )

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")
    assert isinstance(response.json()["totals"]["total"], str)
    assert await redis_client.dbsize() == 0
    assert pdf.calls == 0


async def test_unconfirmed_session_cannot_generate(
    client: httpx.AsyncClient,
    quotation_dependencies: tuple[
        QuotationService,
        SessionService,
        ConversationService,
        RecordingPDFService,
    ],
) -> None:
    _, _, _, pdf = quotation_dependencies
    preview = await _development_chat(client, "Prepare quote")

    response = await client.post(
        "/api/v1/quotes/generate",
        json={"session_id": SESSION_ID},
    )

    assert preview.status_code == 200
    assert preview.json()["state"] == ConversationState.AWAITING_CONFIRMATION
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "quote_not_confirmed"
    assert pdf.calls == 0


async def test_confirmed_generation_persists_and_reuses_safe_metadata(
    client: httpx.AsyncClient,
    quotation_dependencies: tuple[
        QuotationService,
        SessionService,
        ConversationService,
        RecordingPDFService,
    ],
) -> None:
    _, sessions, _, pdf = quotation_dependencies
    await _development_chat(client, "Prepare quote")
    confirmation = await _development_chat(client, "Confirmed")

    first = await client.post(
        "/api/v1/quotes/generate",
        json={"session_id": SESSION_ID},
    )
    second = await client.post(
        "/api/v1/quotes/generate",
        json={"session_id": SESSION_ID},
    )
    stored = await sessions.get_session(f"dev:{SESSION_ID}")

    assert confirmation.status_code == 200
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["quotation_id"] == QUOTATION_ID
    assert first.json()["totals"]["total"] == second.json()["totals"]["total"]
    assert "pdf_path" not in first.json()
    assert "private" not in first.text
    assert "quotation.pdf" not in first.text
    assert pdf.calls == 1
    assert stored.state is ConversationState.QUOTE_GENERATED
    assert stored.last_quotation_id == QUOTATION_ID
    assert stored.last_generated_quote is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": SESSION_ID, "confirmed": True},
        {"session_id": SESSION_ID, "state": "AWAITING_CONFIRMATION"},
        {"session_id": SESSION_ID, "prices": {PRODUCT_ID: "1.00"}},
        {"session_id": SESSION_ID, "totals": {"total": "1.00"}},
        {"session_id": "invalid.session"},
    ],
)
async def test_generation_accepts_only_a_valid_development_session_id(
    client: httpx.AsyncClient,
    redis_client: Redis,
    payload: dict[str, object],
) -> None:
    response = await client.post("/api/v1/quotes/generate", json=payload)

    assert response.status_code == 422
    assert await redis_client.dbsize() == 0


async def test_quotation_routes_are_published_in_nonproduction_swagger(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/quotes/preview" in response.json()["paths"]
    assert "/api/v1/quotes/generate" in response.json()["paths"]


def test_quotation_routes_are_not_registered_in_production() -> None:
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
    assert "/api/v1/quotes/preview" not in paths
    assert "/api/v1/quotes/generate" not in paths


async def _development_chat(client: httpx.AsyncClient, message: str) -> httpx.Response:
    return await client.post(
        "/api/v1/dev/chat",
        json={"session_id": SESSION_ID, "message": message},
    )
