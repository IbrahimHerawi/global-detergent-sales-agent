"""Redis integration coverage for shared conversation application orchestration."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import SessionStorageUnavailableError, StalePreviewError
from app.schemas.agent import AgentResult, PreviewDeliveryMetadata
from app.schemas.quotation import CustomerInfo, GeneratedQuotation, QuoteCartItem
from app.schemas.session import ConversationMessage, ConversationSession, ConversationState
from app.services.conversation_service import (
    CONVERSATION_LOCK_KEY_PREFIX,
    ConversationService,
)
from app.services.quotation_service import CurrentTurnContext, QuotationService
from app.services.session_service import SessionService, normalize_customer_identity
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 15
PRODUCT_ID = "GDF-FLC-001"


def isolated_redis_url() -> str:
    """Force integration tests onto the Compose Redis test database."""
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_REDIS_DATABASE}", parts.query, ""))


def make_settings(*, max_length: int = 4_096) -> Settings:
    return Settings(
        app_env="test",
        redis_url=isolated_redis_url(),
        session_ttl_seconds=30,
        recent_message_limit=20,
        max_inbound_text_length=max_length,
    )


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Provide clean Compose Redis state isolated from development database zero."""
    client = Redis.from_url(isolated_redis_url(), decode_responses=True)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


class CartAgent:
    """Exercise real cart mutations while recording concurrency."""

    def __init__(self, quotation_service: QuotationService, *, delay: float = 0.0) -> None:
        self._quotation_service = quotation_service
        self._delay = delay
        self.active_by_sender: defaultdict[str, int] = defaultdict(int)
        self.maximum_by_sender: defaultdict[str, int] = defaultdict(int)
        self.total_active = 0
        self.maximum_total = 0

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        sender = session.customer_phone
        self.active_by_sender[sender] += 1
        self.maximum_by_sender[sender] = max(
            self.maximum_by_sender[sender],
            self.active_by_sender[sender],
        )
        self.total_active += 1
        self.maximum_total = max(self.maximum_total, self.total_active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            self._quotation_service.add_item(session, PRODUCT_ID, 1)
            session.recent_messages.extend(
                [
                    ConversationMessage(role="user", content=user_message),
                    ConversationMessage(role="assistant", content="Added."),
                ]
            )
            return AgentResult(response_text="Added.", updated_session=session)
        finally:
            self.active_by_sender[sender] -= 1
            self.total_active -= 1


class PreviewAgent:
    """Prepare previews through the quotation service used by production tools."""

    def __init__(self, quotation_service: QuotationService) -> None:
        self._quotation_service = quotation_service

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        self._quotation_service.add_item(session, PRODUCT_ID, 1)
        self._quotation_service.set_customer_information(session, name="Aisha")
        self._quotation_service.prepare_quotation(session, turn_context.turn_id)
        fingerprint = session.prepared_preview_fingerprint
        assert fingerprint is not None
        session.recent_messages.extend(
            [
                ConversationMessage(role="user", content=user_message),
                ConversationMessage(role="assistant", content="Preview"),
            ]
        )
        return AgentResult(
            response_text="Preview",
            updated_session=session,
            prepared_preview=PreviewDeliveryMetadata(
                fingerprint=fingerprint,
                originating_turn_id=turn_context.turn_id,
            ),
        )


class RecordingPDFService:
    """Record real quotation-service generation without rendering a PDF."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    async def generate_quotation_pdf(self, quotation: GeneratedQuotation) -> Path:
        self.calls += 1
        return self.path


class GenerationAgent:
    def __init__(self, quotation_service: QuotationService) -> None:
        self._quotation_service = quotation_service

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        generated = await self._quotation_service.generate(session)
        session.recent_messages.extend(
            [
                ConversationMessage(role="user", content=user_message),
                ConversationMessage(role="assistant", content="Quotation ready."),
            ]
        )
        return AgentResult(
            response_text="Quotation ready.",
            updated_session=session,
            generated_quote=generated,
        )


async def test_complete_turn_persists_history_and_returns_transport_neutral_result(
    redis_client: Redis,
) -> None:
    settings = make_settings()
    sessions = SessionService(redis_client, settings=settings)
    agent = CartAgent(QuotationService())
    service = ConversationService(
        redis_client,
        settings=settings,
        session_service=sessions,
        agent=agent,
    )

    result = await service.process_message("dev:browser-1", "Add floor cleaner")
    restored = await sessions.get_session("dev:browser-1")

    assert result.response_text == "Added."
    assert result.session_id == restored.session_id
    assert result.cart == (QuoteCartItem(product_id=PRODUCT_ID, quantity=1),)
    assert [message.content for message in restored.recent_messages] == [
        "Add floor cleaner",
        "Added.",
    ]


async def test_same_sender_turns_are_serialized_without_lost_cart_updates(
    redis_client: Redis,
) -> None:
    settings = make_settings()
    sessions = SessionService(redis_client, settings=settings)
    agent = CartAgent(QuotationService(), delay=0.15)
    service = ConversationService(
        redis_client,
        settings=settings,
        session_service=sessions,
        agent=agent,
        lock_lease_seconds=0.12,
        lock_wait_seconds=2,
        lock_poll_seconds=0.01,
    )

    first, second = await asyncio.gather(
        service.process_message("97450000001", "Add one"),
        service.process_message("+97450000001", "Add another"),
    )
    restored = await sessions.get_session("97450000001")

    assert {first.cart[0].quantity, second.cart[0].quantity} == {1, 2}
    assert restored.cart == [QuoteCartItem(product_id=PRODUCT_ID, quantity=2)]
    assert agent.maximum_by_sender["+97450000001"] == 1
    assert len(restored.recent_messages) == 4


async def test_different_senders_process_independently(redis_client: Redis) -> None:
    settings = make_settings()
    agent = CartAgent(QuotationService(), delay=0.15)
    service = ConversationService(redis_client, settings=settings, agent=agent)

    await asyncio.gather(
        service.process_message("97450000002", "First sender"),
        service.process_message("97450000003", "Second sender"),
    )

    assert agent.maximum_total == 2


async def test_inbound_validation_happens_before_state_or_agent_work(
    redis_client: Redis,
) -> None:
    settings = make_settings(max_length=4)
    agent = CartAgent(QuotationService())
    service = ConversationService(redis_client, settings=settings, agent=agent)

    with pytest.raises(ValueError, match="must not exceed"):
        await service.process_message("97450000004", "12345")

    assert agent.maximum_total == 0
    assert await redis_client.dbsize() == 0


async def test_redis_failure_prevents_agent_state_changes() -> None:
    unavailable = Redis.from_url(
        "redis://redis:1/15",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    agent = CartAgent(QuotationService())
    service = ConversationService(unavailable, settings=make_settings(), agent=agent)

    try:
        with pytest.raises(SessionStorageUnavailableError):
            await service.process_message("97450000005", "Add one")
    finally:
        await unavailable.aclose()

    assert agent.maximum_total == 0


async def test_lost_owner_never_releases_or_persists_over_new_owner(
    redis_client: Redis,
) -> None:
    settings = make_settings()
    identity = normalize_customer_identity("97450000006")
    lock_key = f"{CONVERSATION_LOCK_KEY_PREFIX}{identity}"

    class ReplacingAgent:
        async def handle_message(
            self,
            session: ConversationSession,
            user_message: str,
            turn_context: CurrentTurnContext,
        ) -> AgentResult:
            session.recent_messages.append(ConversationMessage(role="user", content=user_message))
            await redis_client.set(lock_key, "new-worker-token", px=5_000)
            return AgentResult(response_text="unsafe", updated_session=session)

    service = ConversationService(
        redis_client,
        settings=settings,
        agent=ReplacingAgent(),
        lock_lease_seconds=5,
    )

    with pytest.raises(SessionStorageUnavailableError) as caught:
        await service.process_message(identity, "message")

    assert caught.value.diagnostic_detail is not None
    assert "ownership was lost" in caught.value.diagnostic_detail
    assert await redis_client.get(lock_key) == "new-worker-token"
    assert await redis_client.get(f"gdf:session:{identity}") is None


async def test_preview_only_becomes_confirmable_after_matching_success_ack(
    redis_client: Redis,
) -> None:
    settings = make_settings()
    sessions = SessionService(redis_client, settings=settings)
    quotations = QuotationService()
    service = ConversationService(
        redis_client,
        settings=settings,
        session_service=sessions,
        agent=PreviewAgent(quotations),
        quotation_service=quotations,
    )

    result = await service.process_message("97450000007", "Prepare my quote")
    preview = result.preview_delivery_metadata
    assert preview is not None

    # A failed transport simply does not acknowledge. Preparation remains durable
    # but cannot authorize a confirmation.
    undelivered = await sessions.get_session("97450000007")
    assert undelivered.state is ConversationState.QUOTE_REVIEW
    assert undelivered.preview_delivered is False

    wrong = preview.model_copy(update={"fingerprint": "0" * 64})
    with pytest.raises(StalePreviewError):
        await service.acknowledge_successful_delivery("97450000007", wrong)
    assert (await sessions.get_session("97450000007")).preview_delivered is False

    state = await service.acknowledge_successful_delivery("97450000007", preview)
    delivered = await sessions.get_session("97450000007")
    assert state is ConversationState.AWAITING_CONFIRMATION
    assert delivered.state is ConversationState.AWAITING_CONFIRMATION
    assert delivered.preview_delivered is True


async def test_generated_artifact_metadata_is_persisted_and_reused_before_delivery(
    redis_client: Redis,
    tmp_path: Path,
) -> None:
    settings = make_settings()
    sessions = SessionService(redis_client, settings=settings)
    pdf = RecordingPDFService(tmp_path / "generated.pdf")
    quotations = QuotationService(pdf_service=pdf)
    session = ConversationSession(
        customer_phone="+97450000008",
        customer=CustomerInfo(phone="+97450000008", name="Aisha"),
        cart=[QuoteCartItem(product_id=PRODUCT_ID, quantity=1)],
        state=ConversationState.BUILDING_QUOTE,
    )
    quotations.prepare_quotation(session, "preview-turn")
    fingerprint = session.prepared_preview_fingerprint
    assert fingerprint is not None
    quotations.mark_ready_for_confirmation(session, fingerprint, "preview-turn")
    quotations.confirm(
        session,
        CurrentTurnContext(
            session_id=session.session_id,
            turn_id="confirmation-turn",
            customer_message="Confirmed",
            preceding_turn_ids=("preview-turn",),
        ),
    )
    await sessions.save_session(session)
    service = ConversationService(
        redis_client,
        settings=settings,
        session_service=sessions,
        agent=GenerationAgent(quotations),
        quotation_service=quotations,
    )

    first = await service.process_message("97450000008", "Generate it")
    persisted_before_delivery = await sessions.get_session("97450000008")
    second = await service.process_message("97450000008", "Retry delivery")

    assert first.generated_quotation is not None
    assert second.generated_quotation is not None
    assert persisted_before_delivery.last_generated_quote is not None
    assert (
        persisted_before_delivery.last_generated_quote.quotation_id
        == first.generated_quotation.quotation_id
        == second.generated_quotation.quotation_id
    )
    assert pdf.calls == 1
