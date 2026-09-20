"""Unit coverage for side-effect-free Meta webhook normalization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.schemas.whatsapp import (
    IncomingMessage,
    WebhookIssueCode,
    WebhookOutcomeKind,
)
from app.services.whatsapp_parser import (
    KNOWN_UNSUPPORTED_MESSAGE_TYPES,
    parse_whatsapp_webhook,
)
from pydantic import ValidationError


def message_change(*messages: object, statuses: list[object] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "97440000000",
            "phone_number_id": "phone-number-id",
        },
    }
    if messages:
        value["messages"] = list(messages)
    if statuses is not None:
        value["statuses"] = statuses
    return {"field": "messages", "value": value}


def text_message(
    message_id: str,
    body: str,
    *,
    sender: str = "97450000000",
    timestamp: object = "1603059201",
) -> dict[str, object]:
    return {
        "from": sender,
        "id": message_id,
        "timestamp": timestamp,
        "type": "text",
        "text": {"body": body},
    }


def envelope(*entries: object) -> dict[str, object]:
    return {"object": "whatsapp_business_account", "entry": list(entries)}


def entry(entry_id: str, *changes: object) -> dict[str, object]:
    return {"id": entry_id, "changes": list(changes)}


def test_incoming_message_requires_aware_timestamp_and_normalizes_to_utc() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        IncomingMessage(
            message_id="wamid.1",
            sender_phone="97450000000",
            message_type="text",
            text="Hello",
            timestamp=datetime(2026, 9, 20, 12),
        )

    message = IncomingMessage(
        message_id="wamid.1",
        sender_phone="97450000000",
        message_type="text",
        text="Hello",
        timestamp=datetime.fromtimestamp(1603059201, tz=UTC),
    )

    assert message.timestamp is not None
    assert message.timestamp.tzinfo is UTC


def test_batched_text_traverses_all_entries_changes_and_messages() -> None:
    payload = envelope(
        entry(
            "waba-1",
            message_change(
                text_message("wamid.1", "First"),
                text_message("wamid.2", "Second", timestamp=None),
            ),
            message_change(text_message("wamid.3", "Third")),
        ),
        entry("waba-2", message_change(text_message("wamid.4", "Fourth"))),
    )

    result = parse_whatsapp_webhook(payload, max_text_length=4_096)

    assert [outcome.kind for outcome in result.outcomes] == [
        WebhookOutcomeKind.TEXT_MESSAGE,
        WebhookOutcomeKind.TEXT_MESSAGE,
        WebhookOutcomeKind.TEXT_MESSAGE,
        WebhookOutcomeKind.TEXT_MESSAGE,
    ]
    assert [message.message_id for message in result.messages] == [
        "wamid.1",
        "wamid.2",
        "wamid.3",
        "wamid.4",
    ]
    assert [message.text for message in result.messages] == ["First", "Second", "Third", "Fourth"]
    assert result.messages[0].timestamp == datetime.fromtimestamp(1603059201, tz=UTC)
    assert result.messages[1].timestamp is None
    assert result.has_invalid_events is False


def test_status_only_event_is_explicitly_ignored_without_a_message() -> None:
    payload = envelope(
        entry(
            "waba-1",
            message_change(
                statuses=[
                    {
                        "id": "wamid.outbound",
                        "status": "delivered",
                        "timestamp": "1603086313",
                        "recipient_id": "97450000000",
                    }
                ]
            ),
        )
    )

    result = parse_whatsapp_webhook(payload, max_text_length=4_096)

    assert result.messages == ()
    assert len(result.outcomes) == 1
    assert result.outcomes[0].kind is WebhookOutcomeKind.IGNORED
    assert result.outcomes[0].issue is not None
    assert result.outcomes[0].issue.code is WebhookIssueCode.STATUS_EVENT


@pytest.mark.parametrize("message_type", sorted(KNOWN_UNSUPPORTED_MESSAGE_TYPES))
def test_unsupported_media_is_normalized_for_a_safe_controller_reply(message_type: str) -> None:
    raw_message = {
        "from": "97450000000",
        "id": f"wamid.{message_type}",
        "timestamp": "1603059201",
        "type": message_type,
        message_type: {"id": "media-id"},
    }

    result = parse_whatsapp_webhook(
        envelope(entry("waba-1", message_change(raw_message))),
        max_text_length=4_096,
    )

    assert len(result.messages) == 1
    assert result.outcomes[0].kind is WebhookOutcomeKind.UNSUPPORTED_MESSAGE
    assert result.messages[0].message_type == message_type
    assert result.messages[0].text is None


def test_unknown_message_type_is_forward_compatible_and_unsupported() -> None:
    raw_message = {
        "from": "97450000000",
        "id": "wamid.future",
        "timestamp": "1603059201",
        "type": "future_meta_type",
    }

    result = parse_whatsapp_webhook(
        envelope(entry("waba-1", message_change(raw_message))),
        max_text_length=4_096,
    )

    assert result.outcomes[0].kind is WebhookOutcomeKind.UNSUPPORTED_MESSAGE
    assert result.messages[0].message_type == "future_meta_type"


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        (
            {
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "97440000000"},
                    "messages": [text_message("wamid.1", "Hello")],
                },
            },
            WebhookIssueCode.INVALID_METADATA,
        ),
        (
            message_change({"from": "97450000000", "type": "text", "text": {"body": "Hi"}}),
            WebhookIssueCode.INVALID_MESSAGE,
        ),
        (
            message_change({"id": "wamid.1", "type": "text", "text": {"body": "Hi"}}),
            WebhookIssueCode.INVALID_MESSAGE,
        ),
        (
            message_change({"from": "97450000000", "id": "wamid.1", "text": {"body": "Hi"}}),
            WebhookIssueCode.INVALID_MESSAGE,
        ),
        (
            message_change(
                {
                    "from": "97450000000",
                    "id": "wamid.1",
                    "type": "text",
                    "text": {},
                }
            ),
            WebhookIssueCode.INVALID_MESSAGE,
        ),
    ],
)
def test_required_metadata_and_message_fields_are_validated(
    change: dict[str, Any], expected_code: WebhookIssueCode
) -> None:
    result = parse_whatsapp_webhook(
        envelope(entry("waba-1", change)),
        max_text_length=4_096,
    )

    assert result.messages == ()
    assert result.has_invalid_events is True
    assert result.outcomes[0].issue is not None
    assert result.outcomes[0].issue.code is expected_code


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "1.5", -1, True, 10**30])
def test_invalid_timestamp_is_a_structured_error(timestamp: object) -> None:
    payload = envelope(
        entry("waba-1", message_change(text_message("wamid.bad", "Hello", timestamp=timestamp)))
    )

    result = parse_whatsapp_webhook(payload, max_text_length=4_096)

    assert result.messages == ()
    assert result.outcomes[0].kind is WebhookOutcomeKind.INVALID
    assert result.outcomes[0].issue is not None
    assert result.outcomes[0].issue.code is WebhookIssueCode.INVALID_TIMESTAMP


def test_oversized_text_uses_the_configured_limit() -> None:
    payload = envelope(entry("waba-1", message_change(text_message("wamid.long", "123456"))))

    result = parse_whatsapp_webhook(payload, max_text_length=5)

    assert result.messages == ()
    assert result.outcomes[0].issue is not None
    assert result.outcomes[0].issue.code is WebhookIssueCode.OVERSIZED_TEXT


def test_malformed_siblings_do_not_hide_valid_entries_changes_or_messages() -> None:
    payload = envelope(
        "bad-entry",
        entry(
            "waba-1",
            "bad-change",
            message_change(
                {"from": "97450000000", "id": "wamid.bad", "type": "text"},
                text_message("wamid.valid-1", "Still parsed"),
            ),
        ),
        entry("waba-2", message_change(text_message("wamid.valid-2", "Also parsed"))),
    )

    result = parse_whatsapp_webhook(payload, max_text_length=4_096)

    assert [message.message_id for message in result.messages] == [
        "wamid.valid-1",
        "wamid.valid-2",
    ]
    assert [outcome.kind for outcome in result.outcomes] == [
        WebhookOutcomeKind.INVALID,
        WebhookOutcomeKind.INVALID,
        WebhookOutcomeKind.INVALID,
        WebhookOutcomeKind.TEXT_MESSAGE,
        WebhookOutcomeKind.TEXT_MESSAGE,
    ]
    assert result.outcomes[0].entry_index == 0
    assert result.outcomes[2].message_index == 0


def test_non_message_subscription_change_is_ignored_without_meta_metadata() -> None:
    payload = envelope(entry("waba-1", {"field": "account_update", "value": {"event": "changed"}}))

    result = parse_whatsapp_webhook(payload, max_text_length=4_096)

    assert result.messages == ()
    assert result.outcomes[0].kind is WebhookOutcomeKind.IGNORED
    assert result.outcomes[0].issue is not None
    assert result.outcomes[0].issue.code is WebhookIssueCode.NON_MESSAGE_EVENT


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"object": "page", "entry": []},
        {"object": "whatsapp_business_account", "entry": "not-an-array"},
    ],
)
def test_invalid_envelopes_return_structured_outcomes(payload: object) -> None:
    result = parse_whatsapp_webhook(payload, max_text_length=4_096)

    assert result.messages == ()
    assert result.outcomes[0].kind is WebhookOutcomeKind.INVALID
    assert result.outcomes[0].issue is not None
    assert result.outcomes[0].issue.code is WebhookIssueCode.INVALID_ENVELOPE


def test_parser_rejects_invalid_limit_configuration() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        parse_whatsapp_webhook(envelope(entry("waba-1")), max_text_length=0)
