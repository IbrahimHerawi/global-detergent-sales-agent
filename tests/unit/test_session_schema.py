"""Tests for conversation state and persisted session schemas."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas.quotation import CustomerInfo, QuoteCartItem
from app.schemas.quotation_rules import QuotationRules
from app.schemas.session import (
    ConversationMessage,
    ConversationSession,
    ConversationState,
    GeneratedQuoteReplayMetadata,
)


def quotation_rules(**changes: object) -> QuotationRules:
    """Build authoritative rules for fingerprint tests."""
    values: dict[str, object] = {
        "currency": "QAR",
        "quotation_prefix": "GDF-Q",
        "validity_days": 14,
        "default_discount_percent": "0.00",
        "tax_percent": "5.00",
        "terms": ["Payment due on receipt."],
    }
    values.update(changes)
    return QuotationRules.model_validate(values)


def test_all_eight_conversation_states_are_defined() -> None:
    assert [state.value for state in ConversationState] == [
        "NEW",
        "DISCOVERY",
        "PRODUCT_DISCUSSION",
        "BUILDING_QUOTE",
        "CUSTOMER_DETAILS",
        "QUOTE_REVIEW",
        "AWAITING_CONFIRMATION",
        "QUOTE_GENERATED",
    ]


def test_new_session_has_safe_defaults_and_transport_customer() -> None:
    session = ConversationSession(customer_phone="+97450000000")

    assert isinstance(session.session_id, UUID)
    assert session.state is ConversationState.NEW
    assert session.selected_product_id is None
    assert session.customer == CustomerInfo(phone="+97450000000")
    assert session.cart == []
    assert session.recent_messages == []
    assert session.quote_confirmed is False
    assert session.last_quotation_id is None
    assert session.quote_revision == 0
    assert session.prepared_preview_fingerprint is None
    assert session.preview_delivered is False
    assert session.preview_originating_turn_id is None
    assert session.confirmation_turn_id is None
    assert session.confirmation_message is None
    assert session.last_generated_quote is None
    assert session.created_at.tzinfo is UTC
    assert session.updated_at.tzinfo is UTC


def test_default_collections_are_independent() -> None:
    first = ConversationSession(customer_phone="+97450000001")
    second = ConversationSession(customer_phone="+97450000002")

    first.cart.append(QuoteCartItem(product_id="GDF-FLC-001", quantity=1))
    first.recent_messages.append(ConversationMessage(role="user", content="Hello"))

    assert second.cart == []
    assert second.recent_messages == []


def test_complete_session_serializes_and_deserializes() -> None:
    session_id = uuid4()
    created_at = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    fingerprint = "a" * 64
    session = ConversationSession(
        session_id=session_id,
        customer_phone="+97450000000",
        state=ConversationState.QUOTE_GENERATED,
        selected_product_id="GDF-FLC-001",
        customer=CustomerInfo(
            phone="+97450000000",
            name="Aisha",
            company_name="Example LLC",
            contact_person="Omar",
            notes="Morning delivery.",
        ),
        cart=[QuoteCartItem(product_id="GDF-FLC-001", quantity=3)],
        quote_confirmed=True,
        last_quotation_id="GDF-Q-20260919-001",
        recent_messages=[
            ConversationMessage(role="user", content="Confirmed", timestamp=created_at)
        ],
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=1),
        quote_revision=4,
        prepared_preview_fingerprint=fingerprint,
        preview_delivered=True,
        preview_originating_turn_id="turn-preview-1",
        confirmation_turn_id="turn-confirm-1",
        confirmation_message="Confirmed",
        last_generated_quote=GeneratedQuoteReplayMetadata(
            quotation_id="GDF-Q-20260919-001",
            quote_fingerprint=fingerprint,
            confirmation_turn_id="turn-confirm-1",
            pdf_path="/app/storage/quotes/GDF-Q-20260919-001.pdf",
            generated_at=created_at + timedelta(minutes=1),
        ),
    )

    restored = ConversationSession.model_validate_json(session.model_dump_json())

    assert restored == session
    assert restored.session_id == session_id
    assert restored.last_generated_quote is not None
    assert restored.last_generated_quote.pdf_path.endswith("GDF-Q-20260919-001.pdf")
    assert restored.created_at.tzinfo is UTC


def test_aware_timestamps_are_normalized_to_utc() -> None:
    plus_three = timezone(timedelta(hours=3))
    local_time = datetime(2026, 9, 19, 13, 0, tzinfo=plus_three)

    message = ConversationMessage(role="assistant", content="Hello", timestamp=local_time)
    session = ConversationSession(
        customer_phone="+97450000000", created_at=local_time, updated_at=local_time
    )

    assert message.timestamp == datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    assert session.created_at == datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    assert session.updated_at.tzinfo is UTC


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_naive_session_timestamps_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ConversationSession.model_validate(
            {"customer_phone": "+97450000000", field: datetime(2026, 9, 19, 10, 0)}
        )


def test_naive_message_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ConversationMessage(role="user", content="Hello", timestamp=datetime(2026, 9, 19))


def test_lifecycle_metadata_distinguishes_internal_quote_stages() -> None:
    fingerprint = "b" * 64
    new = ConversationSession(customer_phone="+97450000000")
    prepared = new.model_copy(
        update={
            "state": ConversationState.QUOTE_REVIEW,
            "quote_revision": 1,
            "prepared_preview_fingerprint": fingerprint,
            "preview_originating_turn_id": "turn-preview-1",
        }
    )
    delivered = prepared.model_copy(
        update={
            "state": ConversationState.AWAITING_CONFIRMATION,
            "preview_delivered": True,
        }
    )
    confirmed = delivered.model_copy(
        update={
            "quote_confirmed": True,
            "confirmation_turn_id": "turn-confirm-1",
            "confirmation_message": "Confirmed",
        }
    )
    generated = confirmed.model_copy(
        update={
            "state": ConversationState.QUOTE_GENERATED,
            "last_quotation_id": "GDF-Q-20260919-001",
            "last_generated_quote": GeneratedQuoteReplayMetadata(
                quotation_id="GDF-Q-20260919-001",
                quote_fingerprint=fingerprint,
                confirmation_turn_id="turn-confirm-1",
                pdf_path="/app/storage/quotes/GDF-Q-20260919-001.pdf",
            ),
        }
    )

    assert prepared.prepared_preview_fingerprint == fingerprint
    assert prepared.preview_delivered is False
    assert delivered.preview_delivered is True
    assert delivered.quote_confirmed is False
    assert confirmed.quote_confirmed is True
    assert confirmed.confirmation_turn_id == "turn-confirm-1"
    assert confirmed.confirmation_message == "Confirmed"
    assert generated.last_generated_quote is not None
    assert generated.last_generated_quote.quote_fingerprint == fingerprint


def test_fingerprint_is_stable_and_covers_all_quote_inputs() -> None:
    session = ConversationSession(
        customer_phone="+97450000000",
        customer=CustomerInfo(phone="+97450000000", company_name="Example LLC"),
        cart=[QuoteCartItem(product_id="GDF-FLC-001", quantity=2)],
    )
    prices = {"GDF-FLC-001": Decimal("25.00")}
    rules = quotation_rules()
    original = session.quote_fingerprint(
        current_prices=prices, currency="QAR", quotation_rules=rules
    )

    assert len(original) == 64
    assert original == session.quote_fingerprint(
        current_prices=prices, currency="QAR", quotation_rules=rules
    )
    assert original != session.model_copy(
        update={"customer": CustomerInfo(phone="+97450000000", company_name="Changed LLC")}
    ).quote_fingerprint(current_prices=prices, currency="QAR", quotation_rules=rules)
    assert original != session.model_copy(
        update={"cart": [QuoteCartItem(product_id="GDF-FLC-001", quantity=3)]}
    ).quote_fingerprint(current_prices=prices, currency="QAR", quotation_rules=rules)
    assert original != session.quote_fingerprint(
        current_prices={"GDF-FLC-001": Decimal("25.01")},
        currency="QAR",
        quotation_rules=rules,
    )
    assert original != session.quote_fingerprint(
        current_prices=prices, currency="USD", quotation_rules=rules
    )
    assert original != session.quote_fingerprint(
        current_prices=prices,
        currency="QAR",
        quotation_rules=quotation_rules(tax_percent="10.00"),
    )


@pytest.mark.parametrize(
    "prices",
    [{}, {"GDF-FLC-001": Decimal("NaN")}, {"GDF-FLC-001": Decimal("-0.01")}],
)
def test_fingerprint_fails_closed_for_missing_or_invalid_prices(
    prices: dict[str, Decimal],
) -> None:
    session = ConversationSession(
        customer_phone="+97450000000",
        cart=[QuoteCartItem(product_id="GDF-FLC-001", quantity=1)],
    )

    with pytest.raises(ValueError):
        session.quote_fingerprint(
            current_prices=prices,
            currency="QAR",
            quotation_rules=quotation_rules(),
        )


def test_session_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConversationSession.model_validate(
            {"customer_phone": "+97450000000", "model_can_confirm": True}
        )
