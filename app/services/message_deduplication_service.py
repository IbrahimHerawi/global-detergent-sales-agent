"""Atomic Redis records for resumable inbound-message processing.

The record prevents concurrent webhook deliveries from running the agent twice,
while retaining enough safe output metadata to resume transport delivery after a
worker failure. Provider delivery is deliberately not described as exactly once:
an external call is marked uncertain *before* it starts and must be reconciled or
explicitly classified as not accepted before it can be attempted again.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Final, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.core.exceptions import TechnicalError

PROCESSED_MESSAGE_KEY_PREFIX: Final = "gdf:processed-message:"
COMPLETED_MESSAGE_TTL_SECONDS: Final = 604_800
DEFAULT_INCOMPLETE_TTL_SECONDS: Final = 86_400
DEFAULT_OWNERSHIP_LEASE_SECONDS: Final = 60.0

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]

_CLAIM_SCRIPT: Final = r"""
local time = redis.call('TIME')
local now_ms = (tonumber(time[1]) * 1000) + math.floor(tonumber(time[2]) / 1000)
local raw = redis.call('GET', KEYS[1])

if not raw then
    local record = cjson.decode(ARGV[4])
    record.owner_token = ARGV[1]
    record.lease_expires_at_ms = now_ms + tonumber(ARGV[2])
    record.created_at_ms = now_ms
    record.updated_at_ms = now_ms
    record.revision = 1
    local encoded = cjson.encode(record)
    local stored = redis.call('SET', KEYS[1], encoded, 'NX', 'EX', ARGV[3])
    if stored then
        return {'acquired', encoded}
    end
    raw = redis.call('GET', KEYS[1])
end

local record = cjson.decode(raw)
if record.stage == 'complete' then
    return {'complete', raw}
end

if record.owner_token == ARGV[1] or tonumber(record.lease_expires_at_ms) <= now_ms then
    local decision = 'resumed'
    if record.owner_token ~= ARGV[1] then
        decision = 'acquired'
    end
    record.owner_token = ARGV[1]
    record.lease_expires_at_ms = now_ms + tonumber(ARGV[2])
    record.updated_at_ms = now_ms
    record.revision = tonumber(record.revision) + 1
    local encoded = cjson.encode(record)
    redis.call('SET', KEYS[1], encoded, 'XX', 'EX', ARGV[3])
    return {decision, encoded}
end

return {'busy', raw}
"""

_RENEW_SCRIPT: Final = r"""
local time = redis.call('TIME')
local now_ms = (tonumber(time[1]) * 1000) + math.floor(tonumber(time[2]) / 1000)
local raw = redis.call('GET', KEYS[1])
if not raw then
    return {'missing'}
end

local record = cjson.decode(raw)
if record.stage == 'complete' then
    return {'complete', raw}
end
if record.owner_token ~= ARGV[1] then
    return {'not_owner', raw}
end
if tonumber(record.lease_expires_at_ms) <= now_ms then
    return {'expired', raw}
end

record.lease_expires_at_ms = now_ms + tonumber(ARGV[2])
record.updated_at_ms = now_ms
record.revision = tonumber(record.revision) + 1
local encoded = cjson.encode(record)
redis.call('SET', KEYS[1], encoded, 'XX', 'EX', ARGV[3])
return {'ok', encoded}
"""

_OWNED_UPDATE_SCRIPT: Final = r"""
local time = redis.call('TIME')
local now_ms = (tonumber(time[1]) * 1000) + math.floor(tonumber(time[2]) / 1000)
local raw = redis.call('GET', KEYS[1])
if not raw then
    return {'missing'}
end

local current = cjson.decode(raw)
if current.stage == 'complete' then
    return {'complete', raw}
end
if current.owner_token ~= ARGV[1] then
    return {'not_owner', raw}
end
if tonumber(current.lease_expires_at_ms) <= now_ms then
    return {'expired', raw}
end
if tonumber(current.revision) ~= tonumber(ARGV[2]) then
    return {'changed', raw}
end

local replacement = cjson.decode(ARGV[5])
replacement.created_at_ms = current.created_at_ms
replacement.updated_at_ms = now_ms
replacement.revision = tonumber(current.revision) + 1

local ttl = ARGV[4]
if replacement.stage == 'complete' then
    replacement.owner_token = cjson.null
    replacement.lease_expires_at_ms = cjson.null
    replacement.completed_at_ms = now_ms
    ttl = ARGV[6]
else
    replacement.owner_token = ARGV[1]
    replacement.lease_expires_at_ms = now_ms + tonumber(ARGV[3])
end

local encoded = cjson.encode(replacement)
redis.call('SET', KEYS[1], encoded, 'XX', 'EX', ttl)
return {'ok', encoded}
"""


class ProcessingStage(StrEnum):
    """Durable boundaries in message processing and outbound delivery."""

    CLAIMED = "claimed"
    RESULT_SAVED = "result_saved"
    TEXT_SENT = "text_sent"
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_SENT = "document_sent"
    COMPLETE = "complete"


class ExternalActionState(StrEnum):
    """Conservative state for provider calls with uncertain outcomes."""

    NOT_STARTED = "not_started"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"


class ClaimDecision(StrEnum):
    """Atomic claim result returned to webhook orchestration."""

    ACQUIRED = "acquired"
    RESUMED = "resumed"
    BUSY = "busy"
    COMPLETE = "complete"


class MessageProcessingError(TechnicalError):
    """Redis processing-record failure with a safe external representation."""

    code = "message_processing_storage_error"


class MessageOwnershipError(MessageProcessingError):
    """The caller no longer owns the processing lease."""

    code = "message_processing_ownership_lost"


class MessageStageError(MessageProcessingError):
    """A requested stage transition is unsafe or out of order."""

    code = "message_processing_stage_error"


class ProcessingModel(BaseModel):
    """Closed immutable models persisted in the deduplication record."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SafeReplayMetadata(ProcessingModel):
    """Backend-created output needed to resume without rerunning the agent.

    It intentionally excludes inbound text, customer/session data, provider
    credentials, and arbitrary filesystem paths.
    """

    response_text: Annotated[str, StringConstraints(min_length=1, max_length=16_384)]
    generated_quotation_id: NonBlankString | None = None
    document_filename: NonBlankString | None = None
    preview_fingerprint: NonBlankString | None = None
    preview_originating_turn_id: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_safe_pairs_and_filename(self) -> Self:
        if (self.preview_fingerprint is None) != (self.preview_originating_turn_id is None):
            raise ValueError("preview replay fields must be supplied together")
        if self.document_filename is not None and (
            self.document_filename in {".", ".."}
            or "/" in self.document_filename
            or "\\" in self.document_filename
        ):
            raise ValueError("document_filename must be a safe basename")
        return self


class MessageProcessingRecord(ProcessingModel):
    """One ownership, replay, delivery, and completion record."""

    message_id: NonBlankString
    stage: ProcessingStage
    owner_token: NonBlankString | None
    lease_expires_at_ms: PositiveStrictInt | None
    revision: NonNegativeStrictInt
    created_at_ms: NonNegativeStrictInt
    updated_at_ms: NonNegativeStrictInt
    completed_at_ms: PositiveStrictInt | None = None
    replay: SafeReplayMetadata | None = None
    text_delivery: ExternalActionState = ExternalActionState.NOT_STARTED
    document_upload: ExternalActionState = ExternalActionState.NOT_STARTED
    document_delivery: ExternalActionState = ExternalActionState.NOT_STARTED
    text_outbound_id: NonBlankString | None = None
    document_media_id: NonBlankString | None = None
    document_outbound_id: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_stage_invariants(self) -> Self:
        if self.stage is ProcessingStage.COMPLETE:
            if self.owner_token is not None or self.lease_expires_at_ms is not None:
                raise ValueError("complete records must not retain ownership")
            if self.completed_at_ms is None:
                raise ValueError("complete records require a completion timestamp")
        else:
            if self.owner_token is None or self.lease_expires_at_ms is None:
                raise ValueError("incomplete records require ownership")
            if self.completed_at_ms is not None:
                raise ValueError("incomplete records cannot have a completion timestamp")

        if self.stage is ProcessingStage.CLAIMED:
            if self.replay is not None:
                raise ValueError("claimed records cannot contain a persisted result")
        elif self.replay is None:
            raise ValueError("post-agent stages require persisted replay metadata")

        if self.text_delivery is ExternalActionState.SUCCEEDED:
            pass
        elif self.text_outbound_id is not None:
            raise ValueError("text outbound id requires confirmed text delivery")

        if self.document_upload is ExternalActionState.SUCCEEDED:
            if self.document_media_id is None:
                raise ValueError("confirmed document upload requires a media id")
        elif self.document_media_id is not None:
            raise ValueError("document media id requires confirmed upload")

        if self.document_delivery is ExternalActionState.SUCCEEDED:
            pass
        elif self.document_outbound_id is not None:
            raise ValueError("document outbound id requires confirmed document delivery")

        if (
            self.stage
            in {
                ProcessingStage.TEXT_SENT,
                ProcessingStage.DOCUMENT_UPLOADED,
                ProcessingStage.DOCUMENT_SENT,
                ProcessingStage.COMPLETE,
            }
            and self.text_delivery is not ExternalActionState.SUCCEEDED
        ):
            raise ValueError("text_sent and later stages require confirmed text delivery")
        if (
            self.stage
            in {
                ProcessingStage.DOCUMENT_UPLOADED,
                ProcessingStage.DOCUMENT_SENT,
            }
            and self.document_upload is not ExternalActionState.SUCCEEDED
        ):
            raise ValueError("document_uploaded and later stages require confirmed upload")
        if (
            self.stage is ProcessingStage.DOCUMENT_SENT
            and self.document_delivery is not ExternalActionState.SUCCEEDED
        ):
            raise ValueError("document_sent requires confirmed document delivery")
        if (
            self.stage is ProcessingStage.COMPLETE
            and self.replay is not None
            and self.replay.generated_quotation_id is not None
            and (
                self.document_upload is not ExternalActionState.SUCCEEDED
                or self.document_delivery is not ExternalActionState.SUCCEEDED
            )
        ):
            raise ValueError("quotation completion requires confirmed upload and delivery")
        return self

    @property
    def has_uncertain_external_action(self) -> bool:
        """Tell recovery logic to stop rather than automatically resend."""
        return ExternalActionState.UNCERTAIN in {
            self.text_delivery,
            self.document_upload,
            self.document_delivery,
        }


class MessageClaim(ProcessingModel):
    """Atomic acquisition decision plus the current durable record."""

    decision: ClaimDecision
    record: MessageProcessingRecord

    @property
    def owns_processing(self) -> bool:
        return self.decision in {ClaimDecision.ACQUIRED, ClaimDecision.RESUMED}

    @property
    def should_run_agent(self) -> bool:
        """Only a claimed record without a saved result may execute the agent."""
        return self.owns_processing and self.record.stage is ProcessingStage.CLAIMED

    @property
    def should_resume_delivery(self) -> bool:
        return (
            self.owns_processing
            and self.record.stage not in {ProcessingStage.CLAIMED, ProcessingStage.COMPLETE}
            and not self.record.has_uncertain_external_action
        )


class MessageDeduplicationService:
    """Maintain atomic message ownership and resumable processing stages."""

    __slots__ = (
        "_completed_ttl_seconds",
        "_incomplete_ttl_seconds",
        "_lease_ms",
        "_redis",
    )

    def __init__(
        self,
        redis_client: Redis | None = None,
        *,
        settings: Settings | None = None,
        ownership_lease_seconds: float = DEFAULT_OWNERSHIP_LEASE_SECONDS,
        incomplete_ttl_seconds: int = DEFAULT_INCOMPLETE_TTL_SECONDS,
        completed_ttl_seconds: int | None = None,
    ) -> None:
        resolved_settings = settings or get_settings()
        if (
            isinstance(ownership_lease_seconds, bool)
            or not isinstance(ownership_lease_seconds, int | float)
            or ownership_lease_seconds <= 0
        ):
            raise ValueError("ownership_lease_seconds must be positive")
        if (
            isinstance(incomplete_ttl_seconds, bool)
            or not isinstance(incomplete_ttl_seconds, int)
            or incomplete_ttl_seconds <= ownership_lease_seconds
        ):
            raise ValueError("incomplete_ttl_seconds must exceed the ownership lease")
        resolved_completed_ttl = (
            resolved_settings.message_dedup_ttl_seconds
            if completed_ttl_seconds is None
            else completed_ttl_seconds
        )
        if (
            isinstance(resolved_completed_ttl, bool)
            or not isinstance(resolved_completed_ttl, int)
            or resolved_completed_ttl <= 0
        ):
            raise ValueError("completed_ttl_seconds must be a positive integer")

        self._redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(resolved_settings.redis_url, decode_responses=True)
        )
        self._lease_ms = max(1, int(ownership_lease_seconds * 1_000))
        self._incomplete_ttl_seconds = incomplete_ttl_seconds
        self._completed_ttl_seconds = resolved_completed_ttl

    async def is_message_processed(self, message_id: str) -> bool:
        """Return whether processing completed; never use this as a claim gate."""
        record = await self.get_record(message_id)
        return record is not None and record.stage is ProcessingStage.COMPLETE

    async def claim_message(
        self,
        message_id: str,
        owner_token: str | None = None,
    ) -> MessageClaim:
        """Atomically create, resume, reject, or take over a processing claim."""
        normalized_message_id = _validate_identifier(message_id, "message_id", 512)
        normalized_owner = _validate_identifier(
            owner_token or uuid4().hex,
            "owner_token",
            128,
        )
        initial = MessageProcessingRecord(
            message_id=normalized_message_id,
            stage=ProcessingStage.CLAIMED,
            owner_token=normalized_owner,
            lease_expires_at_ms=1,
            revision=0,
            created_at_ms=0,
            updated_at_ms=0,
        )
        raw_result = await self._eval(
            "claim",
            _CLAIM_SCRIPT,
            1,
            processed_message_key(normalized_message_id),
            normalized_owner,
            self._lease_ms,
            self._incomplete_ttl_seconds,
            initial.model_dump_json(),
        )
        status, raw_record = _script_pair(raw_result, "claim")
        try:
            decision = ClaimDecision(status)
        except ValueError as error:
            raise MessageProcessingError("Redis returned an unknown claim decision") from error
        return MessageClaim(
            decision=decision,
            record=_deserialize_record(raw_record, normalized_message_id),
        )

    async def renew_claim(self, message_id: str, owner_token: str) -> MessageProcessingRecord:
        """Refresh a live ownership lease using a compare-by-owner script."""
        normalized_message_id, normalized_owner = _validate_identity_pair(message_id, owner_token)
        raw_result = await self._eval(
            "renew",
            _RENEW_SCRIPT,
            1,
            processed_message_key(normalized_message_id),
            normalized_owner,
            self._lease_ms,
            self._incomplete_ttl_seconds,
        )
        status, raw_record = _script_status_with_optional_record(raw_result, "renew")
        if status != "ok":
            raise _ownership_error(status)
        assert raw_record is not None
        return _deserialize_record(raw_record, normalized_message_id)

    async def save_result(
        self,
        message_id: str,
        owner_token: str,
        replay: SafeReplayMetadata,
    ) -> MessageProcessingRecord:
        """Persist agent output before any provider delivery is attempted."""
        if not isinstance(replay, SafeReplayMetadata):
            raise TypeError("replay must be SafeReplayMetadata")

        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            _require_stage(record, ProcessingStage.CLAIMED)
            return _updated_record(
                record,
                stage=ProcessingStage.RESULT_SAVED,
                replay=replay,
            )

        return await self._mutate_owned(message_id, owner_token, transition)

    async def begin_text_send(self, message_id: str, owner_token: str) -> MessageProcessingRecord:
        """Record uncertainty before issuing the external text-send request."""
        return await self._begin_external_action(
            message_id,
            owner_token,
            expected_stage=ProcessingStage.RESULT_SAVED,
            field_name="text_delivery",
        )

    async def mark_text_send_not_accepted(
        self, message_id: str, owner_token: str
    ) -> MessageProcessingRecord:
        """Allow retry only after a known response proves no send was accepted."""
        return await self._reset_external_action(
            message_id,
            owner_token,
            expected_stage=ProcessingStage.RESULT_SAVED,
            field_name="text_delivery",
        )

    async def mark_text_sent(
        self,
        message_id: str,
        owner_token: str,
        outbound_id: str | None = None,
    ) -> MessageProcessingRecord:
        """Confirm provider acceptance and store its outbound ID when returned."""
        normalized_outbound = _optional_identifier(outbound_id, "outbound_id", 512)

        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            _require_stage(record, ProcessingStage.RESULT_SAVED)
            _require_action_state(record.text_delivery, ExternalActionState.UNCERTAIN)
            return _updated_record(
                record,
                stage=ProcessingStage.TEXT_SENT,
                text_delivery=ExternalActionState.SUCCEEDED,
                text_outbound_id=normalized_outbound,
            )

        return await self._mutate_owned(message_id, owner_token, transition)

    async def begin_document_upload(
        self, message_id: str, owner_token: str
    ) -> MessageProcessingRecord:
        """Record an in-flight document upload before calling Meta."""
        return await self._begin_external_action(
            message_id,
            owner_token,
            expected_stage=ProcessingStage.TEXT_SENT,
            field_name="document_upload",
            require_quotation=True,
        )

    async def mark_document_upload_not_accepted(
        self, message_id: str, owner_token: str
    ) -> MessageProcessingRecord:
        """Reset upload only when the provider definitely rejected it."""
        return await self._reset_external_action(
            message_id,
            owner_token,
            expected_stage=ProcessingStage.TEXT_SENT,
            field_name="document_upload",
        )

    async def mark_document_uploaded(
        self,
        message_id: str,
        owner_token: str,
        media_id: str,
    ) -> MessageProcessingRecord:
        """Persist the confirmed Meta media ID before attempting document send."""
        normalized_media_id = _validate_identifier(media_id, "media_id", 512)

        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            _require_stage(record, ProcessingStage.TEXT_SENT)
            _require_action_state(record.document_upload, ExternalActionState.UNCERTAIN)
            return _updated_record(
                record,
                stage=ProcessingStage.DOCUMENT_UPLOADED,
                document_upload=ExternalActionState.SUCCEEDED,
                document_media_id=normalized_media_id,
            )

        return await self._mutate_owned(message_id, owner_token, transition)

    async def begin_document_send(
        self, message_id: str, owner_token: str
    ) -> MessageProcessingRecord:
        """Record uncertainty before issuing the external document-send request."""
        return await self._begin_external_action(
            message_id,
            owner_token,
            expected_stage=ProcessingStage.DOCUMENT_UPLOADED,
            field_name="document_delivery",
        )

    async def mark_document_send_not_accepted(
        self, message_id: str, owner_token: str
    ) -> MessageProcessingRecord:
        """Reset document send only after a definitive provider rejection."""
        return await self._reset_external_action(
            message_id,
            owner_token,
            expected_stage=ProcessingStage.DOCUMENT_UPLOADED,
            field_name="document_delivery",
        )

    async def mark_document_sent(
        self,
        message_id: str,
        owner_token: str,
        outbound_id: str | None = None,
    ) -> MessageProcessingRecord:
        """Confirm document acceptance and retain its provider ID when available."""
        normalized_outbound = _optional_identifier(outbound_id, "outbound_id", 512)

        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            _require_stage(record, ProcessingStage.DOCUMENT_UPLOADED)
            _require_action_state(record.document_delivery, ExternalActionState.UNCERTAIN)
            return _updated_record(
                record,
                stage=ProcessingStage.DOCUMENT_SENT,
                document_delivery=ExternalActionState.SUCCEEDED,
                document_outbound_id=normalized_outbound,
            )

        return await self._mutate_owned(message_id, owner_token, transition)

    async def mark_message_processed(
        self,
        message_id: str,
        owner_token: str,
    ) -> MessageProcessingRecord:
        """Complete an owned, fully delivered record with the seven-day TTL."""

        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            replay = record.replay
            if replay is None:
                raise MessageStageError("A persisted result is required before completion")
            expected = (
                ProcessingStage.DOCUMENT_SENT
                if replay.generated_quotation_id is not None
                else ProcessingStage.TEXT_SENT
            )
            _require_stage(record, expected)
            return _updated_record(
                record,
                stage=ProcessingStage.COMPLETE,
                owner_token=None,
                lease_expires_at_ms=None,
                completed_at_ms=1,
            )

        return await self._mutate_owned(message_id, owner_token, transition)

    async def get_record(self, message_id: str) -> MessageProcessingRecord | None:
        """Read and validate a record for duplicate/recovery inspection."""
        normalized_message_id = _validate_identifier(message_id, "message_id", 512)
        try:
            raw = await self._redis.get(processed_message_key(normalized_message_id))
        except RedisError as error:
            raise _storage_error("read", error) from error
        if raw is None:
            return None
        return _deserialize_record(_as_text(raw), normalized_message_id)

    async def _begin_external_action(
        self,
        message_id: str,
        owner_token: str,
        *,
        expected_stage: ProcessingStage,
        field_name: str,
        require_quotation: bool = False,
    ) -> MessageProcessingRecord:
        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            _require_stage(record, expected_stage)
            current = getattr(record, field_name)
            _require_action_state(current, ExternalActionState.NOT_STARTED)
            if require_quotation:
                replay = record.replay
                if replay is None or replay.generated_quotation_id is None:
                    raise MessageStageError("Document upload requires a generated quotation")
            return _updated_record(record, **{field_name: ExternalActionState.UNCERTAIN})

        return await self._mutate_owned(message_id, owner_token, transition)

    async def _reset_external_action(
        self,
        message_id: str,
        owner_token: str,
        *,
        expected_stage: ProcessingStage,
        field_name: str,
    ) -> MessageProcessingRecord:
        def transition(record: MessageProcessingRecord) -> MessageProcessingRecord:
            _require_stage(record, expected_stage)
            current = getattr(record, field_name)
            _require_action_state(current, ExternalActionState.UNCERTAIN)
            return _updated_record(record, **{field_name: ExternalActionState.NOT_STARTED})

        return await self._mutate_owned(message_id, owner_token, transition)

    async def _mutate_owned(
        self,
        message_id: str,
        owner_token: str,
        transition: Callable[[MessageProcessingRecord], MessageProcessingRecord],
    ) -> MessageProcessingRecord:
        normalized_message_id, normalized_owner = _validate_identity_pair(message_id, owner_token)
        current = await self.get_record(normalized_message_id)
        if current is None:
            raise MessageOwnershipError("Message processing record no longer exists")
        if current.owner_token != normalized_owner:
            raise MessageOwnershipError("Message processing ownership belongs to another worker")
        replacement = transition(current)
        raw_result = await self._eval(
            "update",
            _OWNED_UPDATE_SCRIPT,
            1,
            processed_message_key(normalized_message_id),
            normalized_owner,
            current.revision,
            self._lease_ms,
            self._incomplete_ttl_seconds,
            replacement.model_dump_json(),
            self._completed_ttl_seconds,
        )
        status, raw_record = _script_status_with_optional_record(raw_result, "update")
        if status != "ok":
            raise _ownership_error(status)
        assert raw_record is not None
        return _deserialize_record(raw_record, normalized_message_id)

    async def _eval(
        self,
        operation: str,
        script: str,
        numkeys: int,
        *keys_and_args: str | int | float,
    ) -> object:
        try:
            return await self._redis.eval(script, numkeys, *keys_and_args)
        except RedisError as error:
            raise _storage_error(operation, error) from error


def processed_message_key(message_id: str) -> str:
    """Build the contract-mandated, hyphenated Redis key."""
    return f"{PROCESSED_MESSAGE_KEY_PREFIX}{_validate_identifier(message_id, 'message_id', 512)}"


@lru_cache(maxsize=1)
def _default_service() -> MessageDeduplicationService:
    return MessageDeduplicationService()


async def is_message_processed(message_id: str) -> bool:
    """Check the default Redis processing record for completion."""
    return await _default_service().is_message_processed(message_id)


async def claim_message(
    message_id: str,
    owner_token: str | None = None,
) -> MessageClaim:
    """Atomically claim a message through the default Redis service."""
    return await _default_service().claim_message(message_id, owner_token)


async def mark_message_processed(
    message_id: str,
    owner_token: str,
) -> MessageProcessingRecord:
    """Complete an owned default-service record."""
    return await _default_service().mark_message_processed(message_id, owner_token)


def _updated_record(
    record: MessageProcessingRecord,
    **updates: object,
) -> MessageProcessingRecord:
    values = record.model_dump(mode="python")
    values.update(updates)
    return MessageProcessingRecord.model_validate(values)


def _require_stage(record: MessageProcessingRecord, expected: ProcessingStage) -> None:
    if record.stage is not expected:
        raise MessageStageError(
            f"Expected message stage {expected.value}, found {record.stage.value}"
        )


def _require_action_state(current: object, expected: ExternalActionState) -> None:
    if current is not expected:
        rendered = current.value if isinstance(current, ExternalActionState) else repr(current)
        raise MessageStageError(
            f"Expected external action state {expected.value}, found {rendered}"
        )


def _validate_identity_pair(message_id: str, owner_token: str) -> tuple[str, str]:
    return (
        _validate_identifier(message_id, "message_id", 512),
        _validate_identifier(owner_token, "owner_token", 128),
    )


def _optional_identifier(value: str | None, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _validate_identifier(value, label, maximum)


def _validate_identifier(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable():
        raise ValueError(f"{label} must be a printable nonblank string up to {maximum} characters")
    return normalized


def _deserialize_record(raw: str, expected_message_id: str) -> MessageProcessingRecord:
    try:
        record = MessageProcessingRecord.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError) as error:
        raise MessageProcessingError(
            "Stored message processing record is invalid",
            cause=error,
        ) from error
    if record.message_id != expected_message_id:
        raise MessageProcessingError("Stored message ID does not match its Redis key")
    return record


def _script_pair(result: object, operation: str) -> tuple[str, str]:
    if not isinstance(result, list) or len(result) != 2:
        raise MessageProcessingError(f"Redis {operation} script returned an invalid result")
    return _as_text(result[0]), _as_text(result[1])


def _script_status_with_optional_record(
    result: object,
    operation: str,
) -> tuple[str, str | None]:
    if not isinstance(result, list) or len(result) not in {1, 2}:
        raise MessageProcessingError(f"Redis {operation} script returned an invalid result")
    status = _as_text(result[0])
    raw_record = _as_text(result[1]) if len(result) == 2 else None
    return status, raw_record


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise MessageProcessingError("Redis script returned a non-text value")


def _ownership_error(status: str) -> MessageOwnershipError:
    details = {
        "missing": "Message processing record no longer exists",
        "complete": "Message processing record is already complete",
        "not_owner": "Message processing ownership belongs to another worker",
        "expired": "Message processing ownership lease expired",
        "changed": "Message processing record changed concurrently",
    }
    return MessageOwnershipError(details.get(status, "Message processing ownership was lost"))


def _storage_error(operation: str, error: BaseException) -> MessageProcessingError:
    return MessageProcessingError(
        f"Redis message processing {operation} failed: {type(error).__name__}",
        cause=error,
    )


__all__ = [
    "COMPLETED_MESSAGE_TTL_SECONDS",
    "DEFAULT_INCOMPLETE_TTL_SECONDS",
    "DEFAULT_OWNERSHIP_LEASE_SECONDS",
    "PROCESSED_MESSAGE_KEY_PREFIX",
    "ClaimDecision",
    "ExternalActionState",
    "MessageClaim",
    "MessageDeduplicationService",
    "MessageOwnershipError",
    "MessageProcessingError",
    "MessageProcessingRecord",
    "MessageStageError",
    "ProcessingStage",
    "SafeReplayMetadata",
    "claim_message",
    "is_message_processed",
    "mark_message_processed",
    "processed_message_key",
]
