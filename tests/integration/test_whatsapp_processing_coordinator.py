"""Durable WhatsApp POST processing tests against real Redis checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from app.core.config import Settings, get_settings
from app.schemas.agent import PreviewDeliveryMetadata
from app.schemas.quotation import (
    CustomerInfo,
    GeneratedQuotation,
    QuoteLine,
    QuoteTotals,
)
from app.schemas.session import ConversationState
from app.schemas.whatsapp import (
    IncomingMessage,
    WebhookOutcomeKind,
    WebhookParseOutcome,
    WebhookParseResult,
)
from app.services.conversation_service import ConversationResult
from app.services.message_deduplication_service import (
    ClaimDecision,
    ExternalActionState,
    MessageDeduplicationService,
    ProcessingStage,
    SafeReplayMetadata,
)
from app.services.whatsapp_processing_coordinator import (
    UNSUPPORTED_MEDIA_RESPONSE,
    ProcessingDisposition,
    WhatsAppProcessingCoordinator,
    WhatsAppProcessingRetryableError,
)
from app.services.whatsapp_service import (
    DocumentSendResult,
    DocumentUploadResult,
    TextPartDelivery,
    TextSendResult,
    WhatsAppTextSendError,
)
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 13
PHONE = "97450000000"
QUOTE_ID = "GDF-Q-20260920-ABCD"
QUOTE_FILENAME = f"{QUOTE_ID}.pdf"


def isolated_redis_url() -> str:
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_REDIS_DATABASE}", parts.query, ""))


def settings() -> Settings:
    return Settings(
        app_env="test",
        redis_url=isolated_redis_url(),
        quote_storage_path=Path("/app/data/quotes"),
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


def deduplication(
    redis_client: Redis,
    *,
    lease_seconds: float = 1,
) -> MessageDeduplicationService:
    return MessageDeduplicationService(
        redis_client,
        settings=settings(),
        ownership_lease_seconds=lease_seconds,
        incomplete_ttl_seconds=60,
    )


def text_outcome(message_id: str = "wamid.text", text: str = "Hello") -> WebhookParseOutcome:
    return WebhookParseOutcome(
        kind=WebhookOutcomeKind.TEXT_MESSAGE,
        message=IncomingMessage(
            message_id=message_id,
            sender_phone=PHONE,
            message_type="text",
            text=text,
        ),
    )


def unsupported_outcome(message_id: str = "wamid.image") -> WebhookParseOutcome:
    return WebhookParseOutcome(
        kind=WebhookOutcomeKind.UNSUPPORTED_MESSAGE,
        message=IncomingMessage(
            message_id=message_id,
            sender_phone=PHONE,
            message_type="image",
        ),
    )


def generated_quote() -> GeneratedQuotation:
    return GeneratedQuotation.with_pdf_path(
        pdf_path=f"/app/data/quotes/{QUOTE_FILENAME}",
        quotation_id=QUOTE_ID,
        issued_at=datetime(2026, 9, 20, tzinfo=UTC),
        valid_until=date(2026, 10, 4),
        customer=CustomerInfo(phone=f"+{PHONE}", name="Aisha"),
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


def conversation_result(
    *,
    response_text: str = "How can I help?",
    quote: GeneratedQuotation | None = None,
    preview: PreviewDeliveryMetadata | None = None,
) -> ConversationResult:
    return ConversationResult(
        response_text=response_text,
        session_id=uuid4(),
        state=ConversationState.NEW,
        selected_product_id=None,
        cart=(),
        generated_quote=quote,
        prepared_preview=preview,
    )


class FakeConversation:
    def __init__(self, result: ConversationResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []
        self.acknowledgements: list[tuple[str, PreviewDeliveryMetadata]] = []

    async def process_message(self, sender: str, message: str) -> ConversationResult:
        self.calls.append((sender, message))
        return self.result

    async def acknowledge_successful_delivery(
        self,
        sender: str,
        preview: PreviewDeliveryMetadata,
    ) -> object:
        self.acknowledgements.append((sender, preview))
        return ConversationState.AWAITING_CONFIRMATION


class BlockingConversation(FakeConversation):
    def __init__(self, result: ConversationResult) -> None:
        super().__init__(result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process_message(self, sender: str, message: str) -> ConversationResult:
        self.calls.append((sender, message))
        self.started.set()
        await self.release.wait()
        return self.result


class FakeTransport:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.uploads: list[Path] = []
        self.documents: list[tuple[str, str, str, str | None]] = []
        self.text_error: WhatsAppTextSendError | None = None

    async def send_text(self, to: str, text: str) -> TextSendResult:
        self.texts.append((to, text))
        if self.text_error is not None:
            raise self.text_error
        return TextSendResult(
            parts=(
                TextPartDelivery(
                    part_number=1,
                    part_count=1,
                    character_count=len(text),
                    outbound_message_id="wamid.outbound-text",
                ),
            )
        )

    async def upload_document(self, path: str | Path) -> DocumentUploadResult:
        normalized = Path(path)
        self.uploads.append(normalized)
        return DocumentUploadResult(media_id="meta-media-id", filename=normalized.name)

    async def send_document(
        self,
        to: str,
        media_id: str,
        filename: str,
        caption: str | None = None,
    ) -> DocumentSendResult:
        self.documents.append((to, media_id, filename, caption))
        return DocumentSendResult(outbound_message_id="wamid.outbound-document")


def coordinator(
    redis_client: Redis,
    conversation: FakeConversation,
    transport: FakeTransport,
    *,
    lease_seconds: float = 1,
    owner: str = "worker",
) -> WhatsAppProcessingCoordinator:
    return WhatsAppProcessingCoordinator(
        conversation=conversation,
        deduplication=deduplication(redis_client, lease_seconds=lease_seconds),
        transport=transport,
        settings=settings(),
        owner_token_factory=lambda: owner,
    )


async def test_text_message_completes_and_duplicate_does_not_repeat_side_effects(
    redis_client: Redis,
) -> None:
    conversation = FakeConversation(conversation_result())
    transport = FakeTransport()
    pipeline = coordinator(redis_client, conversation, transport)
    outcome = text_outcome()

    first = await pipeline.process_outcome(outcome)
    duplicate = await pipeline.process_outcome(outcome)
    record = await deduplication(redis_client).get_record("wamid.text")

    assert first is ProcessingDisposition.COMPLETED
    assert duplicate is ProcessingDisposition.COMPLETED_DUPLICATE
    assert conversation.calls == [(PHONE, "Hello")]
    assert transport.texts == [(PHONE, "How can I help?")]
    assert record is not None
    assert record.stage is ProcessingStage.COMPLETE
    assert record.text_outbound_id == "wamid.outbound-text"


async def test_unsupported_media_gets_exact_text_without_calling_conversation(
    redis_client: Redis,
) -> None:
    conversation = FakeConversation(conversation_result())
    transport = FakeTransport()

    result = await coordinator(redis_client, conversation, transport).process_outcome(
        unsupported_outcome()
    )

    assert result is ProcessingDisposition.COMPLETED
    assert conversation.calls == []
    assert transport.texts == [(PHONE, UNSUPPORTED_MEDIA_RESPONSE)]


async def test_webhook_batch_processes_each_message_in_order(redis_client: Redis) -> None:
    conversation = FakeConversation(conversation_result())
    transport = FakeTransport()
    pipeline = coordinator(redis_client, conversation, transport)

    results = await pipeline.process_webhook(
        WebhookParseResult(
            outcomes=(
                text_outcome("wamid.batch-1", "First"),
                text_outcome("wamid.batch-2", "Second"),
            )
        )
    )

    assert results == (
        ProcessingDisposition.COMPLETED,
        ProcessingDisposition.COMPLETED,
    )
    assert conversation.calls == [(PHONE, "First"), (PHONE, "Second")]
    assert transport.texts == [(PHONE, "How can I help?"), (PHONE, "How can I help?")]


async def test_generated_pdf_upload_and_delivery_are_checkpointed(
    redis_client: Redis,
) -> None:
    conversation = FakeConversation(
        conversation_result(response_text="Your quotation is ready.", quote=generated_quote())
    )
    transport = FakeTransport()
    pipeline = coordinator(redis_client, conversation, transport)

    result = await pipeline.process_outcome(text_outcome("wamid.pdf", "Create my quotation"))
    record = await deduplication(redis_client).get_record("wamid.pdf")

    assert result is ProcessingDisposition.COMPLETED
    assert transport.uploads == [Path("/app/data/quotes") / QUOTE_FILENAME]
    assert transport.documents == [(PHONE, "meta-media-id", QUOTE_FILENAME, None)]
    assert conversation.calls == [(PHONE, "Create my quotation")]
    assert record is not None
    assert record.stage is ProcessingStage.COMPLETE
    assert record.document_media_id == "meta-media-id"
    assert record.document_outbound_id == "wamid.outbound-document"
    assert record.replay is not None
    assert record.replay.document_filename == QUOTE_FILENAME


async def test_failed_preview_send_is_not_acknowledged_and_can_retry_safely(
    redis_client: Redis,
) -> None:
    preview = PreviewDeliveryMetadata(
        fingerprint="f" * 64,
        originating_turn_id="turn-preview",
    )
    conversation = FakeConversation(conversation_result(preview=preview))
    transport = FakeTransport()
    transport.text_error = WhatsAppTextSendError(
        "definitive rejection",
        outcome_uncertain=False,
    )
    pipeline = coordinator(redis_client, conversation, transport)

    with pytest.raises(WhatsAppProcessingRetryableError):
        await pipeline.process_outcome(text_outcome("wamid.preview", "Show preview"))

    failed_record = await deduplication(redis_client).get_record("wamid.preview")
    assert failed_record is not None
    assert failed_record.stage is ProcessingStage.RESULT_SAVED
    assert failed_record.text_delivery is ExternalActionState.NOT_STARTED
    assert conversation.acknowledgements == []

    transport.text_error = None
    completed = await pipeline.process_outcome(text_outcome("wamid.preview", "Show preview"))

    assert completed is ProcessingDisposition.COMPLETED
    assert conversation.calls == [(PHONE, "Show preview")]
    assert conversation.acknowledgements == [(PHONE, preview)]


async def test_persisted_result_replay_skips_conversation_service(
    redis_client: Redis,
) -> None:
    store = deduplication(redis_client, lease_seconds=0.08)
    await store.claim_message("wamid.replay", "old-worker")
    await store.save_result(
        "wamid.replay",
        "old-worker",
        SafeReplayMetadata(response_text="Previously persisted result."),
    )
    await asyncio.sleep(0.11)
    conversation = FakeConversation(conversation_result(response_text="must not be used"))
    transport = FakeTransport()
    pipeline = WhatsAppProcessingCoordinator(
        conversation=conversation,
        deduplication=store,
        transport=transport,
        settings=settings(),
        owner_token_factory=lambda: "new-worker",
    )

    result = await pipeline.process_outcome(text_outcome("wamid.replay", "Original request"))

    assert result is ProcessingDisposition.COMPLETED
    assert conversation.calls == []
    assert transport.texts == [(PHONE, "Previously persisted result.")]


async def test_slow_conversation_renews_claim_and_prevents_second_agent_owner(
    redis_client: Redis,
) -> None:
    store = deduplication(redis_client, lease_seconds=0.08)
    conversation = BlockingConversation(conversation_result())
    transport = FakeTransport()
    pipeline = WhatsAppProcessingCoordinator(
        conversation=conversation,
        deduplication=store,
        transport=transport,
        settings=settings(),
        owner_token_factory=lambda: "original-worker",
        claim_renew_interval_seconds=0.02,
    )

    processing = asyncio.create_task(
        pipeline.process_outcome(text_outcome("wamid.slow", "Slow request"))
    )
    await conversation.started.wait()
    await asyncio.sleep(0.12)
    competing_claim = await store.claim_message("wamid.slow", "competing-worker")
    conversation.release.set()
    result = await processing

    assert competing_claim.decision is ClaimDecision.BUSY
    assert result is ProcessingDisposition.COMPLETED
    assert conversation.calls == [(PHONE, "Slow request")]


async def test_document_checkpoint_replay_skips_text_and_upload(
    redis_client: Redis,
) -> None:
    store = deduplication(redis_client, lease_seconds=0.08)
    message_id = "wamid.document-replay"
    await store.claim_message(message_id, "old-worker")
    await store.save_result(
        message_id,
        "old-worker",
        SafeReplayMetadata(
            response_text="Quotation ready.",
            generated_quotation_id=QUOTE_ID,
            document_filename=QUOTE_FILENAME,
        ),
    )
    await store.begin_text_send(message_id, "old-worker")
    await store.mark_text_sent(message_id, "old-worker", "wamid.existing-text")
    await store.begin_document_upload(message_id, "old-worker")
    await store.mark_document_uploaded(message_id, "old-worker", "existing-media-id")
    await asyncio.sleep(0.11)
    conversation = FakeConversation(conversation_result(response_text="must not be used"))
    transport = FakeTransport()
    pipeline = WhatsAppProcessingCoordinator(
        conversation=conversation,
        deduplication=store,
        transport=transport,
        settings=settings(),
        owner_token_factory=lambda: "new-worker",
    )

    result = await pipeline.process_outcome(text_outcome(message_id, "Original request"))

    assert result is ProcessingDisposition.COMPLETED
    assert conversation.calls == []
    assert transport.texts == []
    assert transport.uploads == []
    assert transport.documents == [(PHONE, "existing-media-id", QUOTE_FILENAME, None)]
