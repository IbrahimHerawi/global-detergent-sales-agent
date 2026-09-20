"""Normalized inbound WhatsApp messages and webhook parsing outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonNegativeIndex = Annotated[int, Field(ge=0)]


class IncomingMessage(BaseModel):
    """Transport-neutral fields extracted from one Meta message event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: NonBlankString
    sender_phone: Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
    message_type: NonBlankString
    text: str | None = None
    timestamp: datetime | None = None

    @field_validator("timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous datetimes and normalize supplied timestamps to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_text_only_for_text_messages(self) -> Self:
        """Keep the normalized representation unambiguous for the controller."""
        if self.message_type == "text":
            if self.text is None or not self.text.strip():
                raise ValueError("text messages require a nonblank text body")
        elif self.text is not None:
            raise ValueError("non-text messages must not contain normalized text")
        return self


class WebhookOutcomeKind(StrEnum):
    """Actions available to the later webhook controller."""

    TEXT_MESSAGE = "text_message"
    UNSUPPORTED_MESSAGE = "unsupported_message"
    IGNORED = "ignored"
    INVALID = "invalid"


class WebhookIssueCode(StrEnum):
    """Stable reasons for ignored or invalid webhook data."""

    INVALID_ENVELOPE = "invalid_envelope"
    INVALID_ENTRY = "invalid_entry"
    INVALID_CHANGE = "invalid_change"
    INVALID_METADATA = "invalid_metadata"
    INVALID_MESSAGE = "invalid_message"
    INVALID_TIMESTAMP = "invalid_timestamp"
    OVERSIZED_TEXT = "oversized_text"
    STATUS_EVENT = "status_event"
    NON_MESSAGE_EVENT = "non_message_event"


class WebhookIssue(BaseModel):
    """Safe, structured diagnostic for one parsing decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: WebhookIssueCode
    detail: NonBlankString


class WebhookParseOutcome(BaseModel):
    """The result of parsing one message or one ignorable/invalid event node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WebhookOutcomeKind
    entry_index: NonNegativeIndex | None = None
    change_index: NonNegativeIndex | None = None
    message_index: NonNegativeIndex | None = None
    message: IncomingMessage | None = None
    issue: WebhookIssue | None = None

    @model_validator(mode="after")
    def require_matching_payload(self) -> Self:
        """Prevent controllers from receiving contradictory outcome data."""
        message_kinds = {
            WebhookOutcomeKind.TEXT_MESSAGE,
            WebhookOutcomeKind.UNSUPPORTED_MESSAGE,
        }
        if self.kind in message_kinds:
            if self.message is None or self.issue is not None:
                raise ValueError("message outcomes require a message and no issue")
            if self.kind is WebhookOutcomeKind.TEXT_MESSAGE:
                if self.message.message_type != "text":
                    raise ValueError("text outcomes require a text message")
            elif self.message.message_type == "text":
                raise ValueError("unsupported outcomes cannot contain text messages")
        elif self.message is not None or self.issue is None:
            raise ValueError("ignored and invalid outcomes require an issue and no message")
        return self


class WebhookParseResult(BaseModel):
    """All outcomes from a webhook, preserving valid siblings of bad nodes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcomes: tuple[WebhookParseOutcome, ...]

    @property
    def messages(self) -> tuple[IncomingMessage, ...]:
        """Return all messages that a controller may process or answer."""
        return tuple(outcome.message for outcome in self.outcomes if outcome.message is not None)

    @property
    def has_invalid_events(self) -> bool:
        """Report whether any envelope node or message failed validation."""
        return any(outcome.kind is WebhookOutcomeKind.INVALID for outcome in self.outcomes)


__all__ = [
    "IncomingMessage",
    "WebhookIssue",
    "WebhookIssueCode",
    "WebhookOutcomeKind",
    "WebhookParseOutcome",
    "WebhookParseResult",
]
