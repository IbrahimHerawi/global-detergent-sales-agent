"""Durable, resumable orchestration for authenticated WhatsApp messages."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.core.exceptions import TechnicalError
from app.schemas.agent import PreviewDeliveryMetadata
from app.schemas.whatsapp import WebhookOutcomeKind, WebhookParseOutcome, WebhookParseResult
from app.services.conversation_service import ConversationResult
from app.services.message_deduplication_service import (
    ClaimDecision,
    MessageDeduplicationService,
    MessageProcessingRecord,
    ProcessingStage,
    SafeReplayMetadata,
)
from app.services.quotation_storage import QuotationStorageError
from app.services.whatsapp_service import (
    DocumentSendResult,
    DocumentUploadResult,
    TextSendResult,
    WhatsAppDocumentSendError,
    WhatsAppDocumentUploadError,
    WhatsAppTextSendError,
)

UNSUPPORTED_MEDIA_RESPONSE: Final = (
    "At the moment I can assist through text messages. Please send your request as text."
)


class ConversationProcessor(Protocol):
    """Conversation operations required by the WhatsApp transport."""

    async def process_message(self, sender: str, message: str) -> ConversationResult: ...

    async def acknowledge_successful_delivery(
        self,
        sender: str,
        preview: PreviewDeliveryMetadata,
    ) -> object: ...


class WhatsAppTransport(Protocol):
    """Outbound operations required by the processing pipeline."""

    async def send_text(self, to: str, text: str) -> TextSendResult: ...

    async def upload_document(self, path: str | Path) -> DocumentUploadResult: ...

    async def send_document(
        self,
        to: str,
        media_id: str,
        filename: str,
        caption: str | None = None,
    ) -> DocumentSendResult: ...


class ProcessingDisposition(StrEnum):
    """Safe summary of one inbound message processing attempt."""

    COMPLETED = "completed"
    COMPLETED_DUPLICATE = "completed_duplicate"


class WhatsAppProcessingRetryableError(TechnicalError):
    """Failure that must produce a non-success webhook response for Meta retry."""

    code = "whatsapp_processing_retryable"


class WhatsAppProcessingCoordinator:
    """Advance each message through durable checkpoints without repeating stages."""

    __slots__ = (
        "_claim_renew_interval_seconds",
        "_conversation",
        "_deduplication",
        "_owner_token_factory",
        "_quotation_storage_root",
        "_transport",
    )

    def __init__(
        self,
        *,
        conversation: ConversationProcessor,
        deduplication: MessageDeduplicationService,
        transport: WhatsAppTransport,
        settings: Settings | None = None,
        owner_token_factory: Callable[[], str] | None = None,
        claim_renew_interval_seconds: float = 15.0,
    ) -> None:
        if (
            isinstance(claim_renew_interval_seconds, bool)
            or not isinstance(claim_renew_interval_seconds, int | float)
            or claim_renew_interval_seconds <= 0
        ):
            raise ValueError("claim_renew_interval_seconds must be positive")
        resolved_settings = settings or get_settings()
        self._conversation = conversation
        self._deduplication = deduplication
        self._transport = transport
        self._quotation_storage_root = resolved_settings.quote_storage_path
        self._owner_token_factory = owner_token_factory or (lambda: uuid4().hex)
        self._claim_renew_interval_seconds = float(claim_renew_interval_seconds)

    async def process_webhook(
        self,
        parsed: WebhookParseResult,
    ) -> tuple[ProcessingDisposition, ...]:
        """Process actionable outcomes in order and ignore status/invalid events."""
        dispositions: list[ProcessingDisposition] = []
        for outcome in parsed.outcomes:
            if outcome.kind in {
                WebhookOutcomeKind.TEXT_MESSAGE,
                WebhookOutcomeKind.UNSUPPORTED_MESSAGE,
            }:
                dispositions.append(await self.process_outcome(outcome))
        return tuple(dispositions)

    async def process_outcome(self, outcome: WebhookParseOutcome) -> ProcessingDisposition:
        """Claim and resume one text or unsupported-media message."""
        if outcome.kind not in {
            WebhookOutcomeKind.TEXT_MESSAGE,
            WebhookOutcomeKind.UNSUPPORTED_MESSAGE,
        } or outcome.message is None:
            raise TypeError("outcome must contain an actionable WhatsApp message")

        message = outcome.message
        owner_token = self._new_owner_token()
        claim = await self._deduplication.claim_message(message.message_id, owner_token)
        if claim.decision is ClaimDecision.COMPLETE:
            return ProcessingDisposition.COMPLETED_DUPLICATE
        if not claim.owns_processing:
            raise WhatsAppProcessingRetryableError("Message is being processed by another owner")
        if claim.record.has_uncertain_external_action:
            raise WhatsAppProcessingRetryableError(
                "Message has an unreconciled outbound provider action"
            )

        record = claim.record
        if record.stage is ProcessingStage.CLAIMED:
            async with self._claim_heartbeat(message.message_id, owner_token):
                created_replay = await self._create_replay(outcome)
            record = await self._deduplication.save_result(
                message.message_id,
                owner_token,
                created_replay,
            )

        replay = record.replay
        if replay is None:
            raise WhatsAppProcessingRetryableError("Persisted replay metadata is unavailable")

        if record.stage is ProcessingStage.RESULT_SAVED:
            record = await self._deliver_text(
                message.message_id,
                owner_token,
                message.sender_phone,
                replay,
            )

        if record.stage is ProcessingStage.TEXT_SENT:
            if replay.generated_quotation_id is None:
                await self._deduplication.mark_message_processed(
                    message.message_id,
                    owner_token,
                )
                return ProcessingDisposition.COMPLETED
            record = await self._upload_document(
                message.message_id,
                owner_token,
                replay,
            )

        if record.stage is ProcessingStage.DOCUMENT_UPLOADED:
            record = await self._deliver_document(
                message.message_id,
                owner_token,
                message.sender_phone,
                record,
                replay,
            )

        if record.stage is ProcessingStage.DOCUMENT_SENT:
            await self._deduplication.mark_message_processed(
                message.message_id,
                owner_token,
            )
            return ProcessingDisposition.COMPLETED

        raise WhatsAppProcessingRetryableError(
            f"Message stopped at unsupported processing stage {record.stage.value}"
        )

    async def _create_replay(self, outcome: WebhookParseOutcome) -> SafeReplayMetadata:
        message = outcome.message
        assert message is not None
        if outcome.kind is WebhookOutcomeKind.TEXT_MESSAGE:
            assert message.text is not None
            result = await self._conversation.process_message(
                message.sender_phone,
                message.text,
            )
            generated = result.generated_quote
            preview = result.prepared_preview
            filename = Path(generated.pdf_path).name if generated is not None else None
            return SafeReplayMetadata(
                response_text=result.response_text,
                generated_quotation_id=(
                    generated.quotation_id if generated is not None else None
                ),
                document_filename=filename,
                preview_fingerprint=preview.fingerprint if preview is not None else None,
                preview_originating_turn_id=(
                    preview.originating_turn_id if preview is not None else None
                ),
            )
        return SafeReplayMetadata(response_text=UNSUPPORTED_MEDIA_RESPONSE)

    async def _deliver_text(
        self,
        message_id: str,
        owner_token: str,
        sender_phone: str,
        replay: SafeReplayMetadata,
    ) -> MessageProcessingRecord:
        await self._deduplication.begin_text_send(message_id, owner_token)
        try:
            async with self._claim_heartbeat(message_id, owner_token):
                result = await self._transport.send_text(sender_phone, replay.response_text)
        except WhatsAppTextSendError as error:
            if not error.outcome_uncertain and not error.delivered_parts:
                await self._deduplication.mark_text_send_not_accepted(message_id, owner_token)
            raise WhatsAppProcessingRetryableError(
                "WhatsApp text response was not durably accepted",
                cause=error,
            ) from error
        except (TypeError, ValueError) as error:
            await self._deduplication.mark_text_send_not_accepted(message_id, owner_token)
            raise WhatsAppProcessingRetryableError(
                "WhatsApp text response failed validation before delivery",
                cause=error,
            ) from error

        if replay.preview_fingerprint is not None:
            assert replay.preview_originating_turn_id is not None
            try:
                async with self._claim_heartbeat(message_id, owner_token):
                    await self._conversation.acknowledge_successful_delivery(
                        sender_phone,
                        PreviewDeliveryMetadata(
                            fingerprint=replay.preview_fingerprint,
                            originating_turn_id=replay.preview_originating_turn_id,
                        ),
                    )
            except Exception as error:
                raise WhatsAppProcessingRetryableError(
                    "Preview delivery acknowledgement failed after provider acceptance",
                    cause=error,
                ) from error

        return await self._deduplication.mark_text_sent(
            message_id,
            owner_token,
            result.outbound_message_id,
        )

    async def _upload_document(
        self,
        message_id: str,
        owner_token: str,
        replay: SafeReplayMetadata,
    ) -> MessageProcessingRecord:
        filename = replay.document_filename
        if filename is None:
            raise WhatsAppProcessingRetryableError(
                "Generated quotation replay is missing its backend filename"
            )
        await self._deduplication.begin_document_upload(message_id, owner_token)
        try:
            async with self._claim_heartbeat(message_id, owner_token):
                uploaded = await self._transport.upload_document(
                    self._quotation_storage_root / filename
                )
        except WhatsAppDocumentUploadError as error:
            if not error.outcome_uncertain:
                await self._deduplication.mark_document_upload_not_accepted(
                    message_id,
                    owner_token,
                )
            raise WhatsAppProcessingRetryableError(
                "WhatsApp quotation upload was not durably accepted",
                cause=error,
            ) from error
        except (QuotationStorageError, TypeError, ValueError) as error:
            await self._deduplication.mark_document_upload_not_accepted(
                message_id,
                owner_token,
            )
            raise WhatsAppProcessingRetryableError(
                "Quotation file was rejected before a Meta upload was attempted",
                cause=error,
            ) from error
        if uploaded.filename != filename:
            raise WhatsAppProcessingRetryableError(
                "Quotation storage returned a filename different from the durable checkpoint"
            )
        return await self._deduplication.mark_document_uploaded(
            message_id,
            owner_token,
            uploaded.media_id,
        )

    async def _deliver_document(
        self,
        message_id: str,
        owner_token: str,
        sender_phone: str,
        record: MessageProcessingRecord,
        replay: SafeReplayMetadata,
    ) -> MessageProcessingRecord:
        filename = replay.document_filename
        media_id = record.document_media_id
        if filename is None or media_id is None:
            raise WhatsAppProcessingRetryableError(
                "Document delivery checkpoint is missing trusted metadata"
            )
        await self._deduplication.begin_document_send(message_id, owner_token)
        try:
            async with self._claim_heartbeat(message_id, owner_token):
                delivered = await self._transport.send_document(
                    sender_phone,
                    media_id,
                    filename,
                )
        except WhatsAppDocumentSendError as error:
            if not error.outcome_uncertain:
                await self._deduplication.mark_document_send_not_accepted(
                    message_id,
                    owner_token,
                )
            raise WhatsAppProcessingRetryableError(
                "WhatsApp quotation delivery was not durably accepted",
                cause=error,
            ) from error
        except (TypeError, ValueError) as error:
            await self._deduplication.mark_document_send_not_accepted(
                message_id,
                owner_token,
            )
            raise WhatsAppProcessingRetryableError(
                "WhatsApp quotation delivery failed validation before sending",
                cause=error,
            ) from error
        return await self._deduplication.mark_document_sent(
            message_id,
            owner_token,
            delivered.outbound_message_id,
        )

    def _new_owner_token(self) -> str:
        token = self._owner_token_factory()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("owner token factory must return a nonblank string")
        return token.strip()

    def _claim_heartbeat(self, message_id: str, owner_token: str) -> _ClaimHeartbeat:
        return _ClaimHeartbeat(
            self._deduplication,
            message_id=message_id,
            owner_token=owner_token,
            interval_seconds=self._claim_renew_interval_seconds,
        )


class _ClaimHeartbeat:
    """Renew ownership only while a slow dependency call is in progress."""

    __slots__ = (
        "_deduplication",
        "_interval_seconds",
        "_message_id",
        "_owner_task",
        "_owner_token",
        "_renew_task",
        "lost_error",
    )

    def __init__(
        self,
        deduplication: MessageDeduplicationService,
        *,
        message_id: str,
        owner_token: str,
        interval_seconds: float,
    ) -> None:
        self._deduplication = deduplication
        self._message_id = message_id
        self._owner_token = owner_token
        self._interval_seconds = interval_seconds
        self._owner_task: asyncio.Task[object] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self.lost_error: WhatsAppProcessingRetryableError | None = None

    async def __aenter__(self) -> _ClaimHeartbeat:
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - every running coroutine has an owner
            raise RuntimeError("claim renewal requires an asyncio task")
        self._owner_task = owner
        self._renew_task = asyncio.create_task(self._renew_loop())
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        renew_task = self._renew_task
        self._renew_task = None
        if renew_task is not None:
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task
        if self.lost_error is not None:
            raise self.lost_error from self.lost_error.cause

    async def _renew_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                try:
                    await self._deduplication.renew_claim(
                        self._message_id,
                        self._owner_token,
                    )
                except Exception as error:
                    self.lost_error = WhatsAppProcessingRetryableError(
                        "Message claim ownership was lost during processing",
                        cause=error,
                    )
                    owner = self._owner_task
                    if owner is not None and not owner.done():
                        owner.cancel()
                    return
        except asyncio.CancelledError:
            raise


__all__ = [
    "UNSUPPORTED_MEDIA_RESPONSE",
    "ProcessingDisposition",
    "WhatsAppProcessingCoordinator",
    "WhatsAppProcessingRetryableError",
]
