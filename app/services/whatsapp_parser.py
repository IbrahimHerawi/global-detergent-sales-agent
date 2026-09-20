"""Pure normalization of Meta WhatsApp webhook payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError

from app.schemas.whatsapp import (
    IncomingMessage,
    WebhookIssue,
    WebhookIssueCode,
    WebhookOutcomeKind,
    WebhookParseOutcome,
    WebhookParseResult,
)

META_WHATSAPP_OBJECT: Final = "whatsapp_business_account"
META_MESSAGES_FIELD: Final = "messages"
META_MESSAGING_PRODUCT: Final = "whatsapp"
KNOWN_UNSUPPORTED_MESSAGE_TYPES: Final = frozenset(
    {"audio", "document", "image", "location", "sticker", "video"}
)


def parse_whatsapp_webhook(
    payload: object,
    *,
    max_text_length: int,
) -> WebhookParseResult:
    """Parse every entry/change/message without side effects or fail-fast loss.

    A malformed sibling becomes an ``INVALID`` outcome and does not prevent
    later entries, changes, or messages from being normalized. Status updates
    and other subscription fields become explicit ``IGNORED`` outcomes so a
    controller can acknowledge them without producing a reply.
    """
    if (
        not isinstance(max_text_length, int)
        or isinstance(max_text_length, bool)
        or max_text_length <= 0
    ):
        raise ValueError("max_text_length must be a positive integer")

    if not isinstance(payload, Mapping):
        return _single_invalid(
            WebhookIssueCode.INVALID_ENVELOPE,
            "Webhook payload must be a JSON object",
        )
    if payload.get("object") != META_WHATSAPP_OBJECT:
        return _single_invalid(
            WebhookIssueCode.INVALID_ENVELOPE,
            "Webhook object must be whatsapp_business_account",
        )

    entries = payload.get("entry")
    if not _is_list(entries) or not entries:
        return _single_invalid(
            WebhookIssueCode.INVALID_ENVELOPE,
            "Webhook entry must be a nonempty array",
        )

    outcomes: list[WebhookParseOutcome] = []
    for entry_index, entry in enumerate(entries):
        outcomes.extend(_parse_entry(entry, entry_index, max_text_length))
    return WebhookParseResult(outcomes=tuple(outcomes))


def _parse_entry(
    entry: object,
    entry_index: int,
    max_text_length: int,
) -> list[WebhookParseOutcome]:
    if not isinstance(entry, Mapping):
        return [
            _invalid(
                WebhookIssueCode.INVALID_ENTRY,
                "Entry must be an object",
                entry_index=entry_index,
            )
        ]
    if not _is_nonblank_string(entry.get("id")):
        return [
            _invalid(
                WebhookIssueCode.INVALID_ENTRY,
                "Entry id is required",
                entry_index=entry_index,
            )
        ]

    changes = entry.get("changes")
    if not _is_list(changes) or not changes:
        return [
            _invalid(
                WebhookIssueCode.INVALID_ENTRY,
                "Entry changes must be a nonempty array",
                entry_index=entry_index,
            )
        ]

    outcomes: list[WebhookParseOutcome] = []
    for change_index, change in enumerate(changes):
        outcomes.extend(_parse_change(change, entry_index, change_index, max_text_length))
    return outcomes


def _parse_change(
    change: object,
    entry_index: int,
    change_index: int,
    max_text_length: int,
) -> list[WebhookParseOutcome]:
    location = {"entry_index": entry_index, "change_index": change_index}
    if not isinstance(change, Mapping):
        return [_invalid(WebhookIssueCode.INVALID_CHANGE, "Change must be an object", **location)]

    field = change.get("field")
    if not _is_nonblank_string(field):
        return [_invalid(WebhookIssueCode.INVALID_CHANGE, "Change field is required", **location)]
    if field != META_MESSAGES_FIELD:
        return [
            _ignored(
                WebhookIssueCode.NON_MESSAGE_EVENT,
                f"Webhook field {field!r} is not a message event",
                **location,
            )
        ]

    value = change.get("value")
    if not isinstance(value, Mapping):
        return [
            _invalid(
                WebhookIssueCode.INVALID_CHANGE,
                "Message change value must be an object",
                **location,
            )
        ]
    if value.get("messaging_product") != META_MESSAGING_PRODUCT:
        return [
            _invalid(
                WebhookIssueCode.INVALID_METADATA,
                "Message change requires messaging_product whatsapp",
                **location,
            )
        ]
    metadata_error = _metadata_error(value.get("metadata"))
    if metadata_error is not None:
        return [_invalid(WebhookIssueCode.INVALID_METADATA, metadata_error, **location)]

    messages = value.get("messages")
    statuses = value.get("statuses")
    outcomes: list[WebhookParseOutcome] = []
    if messages is not None:
        if not _is_list(messages):
            outcomes.append(
                _invalid(
                    WebhookIssueCode.INVALID_CHANGE,
                    "Messages must be an array",
                    **location,
                )
            )
        else:
            for message_index, message in enumerate(messages):
                outcomes.append(
                    _parse_message(
                        message,
                        entry_index,
                        change_index,
                        message_index,
                        max_text_length,
                    )
                )

    if statuses is not None:
        if not _is_list(statuses):
            outcomes.append(
                _invalid(
                    WebhookIssueCode.INVALID_CHANGE,
                    "Statuses must be an array",
                    **location,
                )
            )
        elif statuses:
            outcomes.append(
                _ignored(
                    WebhookIssueCode.STATUS_EVENT,
                    "Delivery status event does not require a reply",
                    **location,
                )
            )

    if not outcomes:
        outcomes.append(
            _ignored(
                WebhookIssueCode.NON_MESSAGE_EVENT,
                "Message change contains no incoming messages",
                **location,
            )
        )
    return outcomes


def _parse_message(
    raw_message: object,
    entry_index: int,
    change_index: int,
    message_index: int,
    max_text_length: int,
) -> WebhookParseOutcome:
    location = {
        "entry_index": entry_index,
        "change_index": change_index,
        "message_index": message_index,
    }
    if not isinstance(raw_message, Mapping):
        return _invalid(
            WebhookIssueCode.INVALID_MESSAGE,
            "Message must be an object",
            **location,
        )

    message_id = raw_message.get("id")
    sender_phone = raw_message.get("from")
    message_type = raw_message.get("type")
    if not _is_nonblank_string(message_id):
        return _invalid(
            WebhookIssueCode.INVALID_MESSAGE,
            "Message id is required",
            **location,
        )
    if not _is_digit_string(sender_phone):
        return _invalid(
            WebhookIssueCode.INVALID_MESSAGE,
            "Message sender must be a numeric WhatsApp identifier",
            **location,
        )
    if not _is_nonblank_string(message_type):
        return _invalid(
            WebhookIssueCode.INVALID_MESSAGE,
            "Message type is required",
            **location,
        )
    assert isinstance(message_id, str)
    assert isinstance(sender_phone, str)
    assert isinstance(message_type, str)

    try:
        timestamp = _parse_timestamp(raw_message)
    except ValueError as error:
        return _invalid(WebhookIssueCode.INVALID_TIMESTAMP, str(error), **location)

    text: str | None = None
    if message_type == "text":
        text_container = raw_message.get("text")
        if not isinstance(text_container, Mapping):
            return _invalid(
                WebhookIssueCode.INVALID_MESSAGE,
                "Text message requires a text object",
                **location,
            )
        body = text_container.get("body")
        if not isinstance(body, str) or not body.strip():
            return _invalid(
                WebhookIssueCode.INVALID_MESSAGE,
                "Text message body must be a nonblank string",
                **location,
            )
        if len(body) > max_text_length:
            return _invalid(
                WebhookIssueCode.OVERSIZED_TEXT,
                "Text message exceeds the configured length limit",
                **location,
            )
        text = body

    try:
        message = IncomingMessage(
            message_id=message_id,
            sender_phone=sender_phone,
            message_type=message_type,
            text=text,
            timestamp=timestamp,
        )
    except ValidationError:
        return _invalid(
            WebhookIssueCode.INVALID_MESSAGE,
            "Message fields are invalid",
            **location,
        )

    outcome_kind = (
        WebhookOutcomeKind.TEXT_MESSAGE
        if message_type == "text"
        else WebhookOutcomeKind.UNSUPPORTED_MESSAGE
    )
    return WebhookParseOutcome(
        kind=outcome_kind,
        entry_index=entry_index,
        change_index=change_index,
        message_index=message_index,
        message=message,
    )


def _parse_timestamp(raw_message: Mapping[object, object]) -> datetime | None:
    if "timestamp" not in raw_message or raw_message["timestamp"] is None:
        return None

    raw_timestamp = raw_message["timestamp"]
    if isinstance(raw_timestamp, bool):
        raise ValueError("Message timestamp must be Unix seconds")
    if isinstance(raw_timestamp, int):
        seconds = raw_timestamp
    elif isinstance(raw_timestamp, str) and raw_timestamp.isascii() and raw_timestamp.isdigit():
        seconds = int(raw_timestamp)
    else:
        raise ValueError("Message timestamp must be Unix seconds")
    if seconds < 0:
        raise ValueError("Message timestamp must be Unix seconds")
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError("Message timestamp is outside the supported range") from error


def _metadata_error(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return "Message metadata must be an object"
    if not _is_nonblank_string(metadata.get("display_phone_number")):
        return "Message metadata display_phone_number is required"
    if not _is_nonblank_string(metadata.get("phone_number_id")):
        return "Message metadata phone_number_id is required"
    return None


def _single_invalid(code: WebhookIssueCode, detail: str) -> WebhookParseResult:
    return WebhookParseResult(outcomes=(_invalid(code, detail),))


def _invalid(
    code: WebhookIssueCode,
    detail: str,
    *,
    entry_index: int | None = None,
    change_index: int | None = None,
    message_index: int | None = None,
) -> WebhookParseOutcome:
    return WebhookParseOutcome(
        kind=WebhookOutcomeKind.INVALID,
        entry_index=entry_index,
        change_index=change_index,
        message_index=message_index,
        issue=WebhookIssue(code=code, detail=detail),
    )


def _ignored(
    code: WebhookIssueCode,
    detail: str,
    *,
    entry_index: int | None = None,
    change_index: int | None = None,
    message_index: int | None = None,
) -> WebhookParseOutcome:
    return WebhookParseOutcome(
        kind=WebhookOutcomeKind.IGNORED,
        entry_index=entry_index,
        change_index=change_index,
        message_index=message_index,
        issue=WebhookIssue(code=code, detail=detail),
    )


def _is_list(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_digit_string(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


__all__ = [
    "KNOWN_UNSUPPORTED_MESSAGE_TYPES",
    "parse_whatsapp_webhook",
]
