"""Tests for explicit quotation confirmation and generation authorization."""

import json
from pathlib import Path

import pytest

from app.core.exceptions import (
    CustomerInformationRequiredError,
    EmptyQuoteError,
    InvalidConversationStateError,
    QuoteNotConfirmedError,
    StalePreviewError,
)
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.quotation import CustomerInfo, QuoteCartItem
from app.schemas.session import ConversationSession, ConversationState
from app.services.quotation_service import (
    ConfirmationInterpretation,
    CurrentTurnContext,
    QuotationService,
)

PRODUCT_ID = "FLOOR"


@pytest.fixture
def quotation_service(tmp_path: Path) -> QuotationService:
    product_path = tmp_path / "products.json"
    product_path.write_text(
        json.dumps(
            [
                {
                    "id": PRODUCT_ID,
                    "sku": "SKU-FLOOR",
                    "name": "Floor Cleaner",
                    "category": "Development Cleaning",
                    "short_description": "Synthetic confirmation-test product.",
                    "description": "Synthetic product for confirmation tests.",
                    "packaging": {"size": "5", "unit": "L"},
                    "price": {"amount": "25.00", "currency": "QAR"},
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    rules_path = tmp_path / "quotation_rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "currency": "QAR",
                "quotation_prefix": "GDF-Q",
                "validity_days": 14,
                "default_discount_percent": "0.00",
                "tax_percent": "0.00",
                "terms": ["Subject to availability."],
            }
        ),
        encoding="utf-8",
    )
    return QuotationService(
        ProductRepository(product_path),
        QuotationRulesRepository(rules_path),
    )


def ready_session() -> ConversationSession:
    return ConversationSession(
        customer_phone="+97450000000",
        customer=CustomerInfo(phone="+97450000000", name="Aisha"),
        cart=[QuoteCartItem(product_id=PRODUCT_ID, quantity=2)],
        state=ConversationState.BUILDING_QUOTE,
    )


def deliver_preview(
    service: QuotationService,
    session: ConversationSession,
    *,
    turn_id: str = "preview-turn",
) -> None:
    service.prepare_quotation(session, turn_id)
    fingerprint = session.prepared_preview_fingerprint
    assert fingerprint is not None
    service.mark_ready_for_confirmation(session, fingerprint, turn_id)


def confirmation_context(
    session: ConversationSession,
    message: str,
    *,
    turn_id: str = "confirmation-turn",
) -> CurrentTurnContext:
    return CurrentTurnContext(
        session_id=session.session_id,
        turn_id=turn_id,
        customer_message=message,
        preceding_turn_ids=("preview-turn",),
    )


@pytest.mark.parametrize(
    "message",
    [
        "Yes",
        "Confirmed",
        "Correct",
        "Proceed",
        "Generate it",
        "Please generate the quotation",
        "  Yes!  ",
    ],
)
def test_positive_confirmation_concepts_preserve_current_turn_evidence(
    quotation_service: QuotationService,
    message: str,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)

    quotation_service.confirm(session, confirmation_context(session, message))

    assert session.quote_confirmed is True
    assert session.state is ConversationState.AWAITING_CONFIRMATION
    assert session.confirmation_turn_id == "confirmation-turn"
    assert session.confirmation_message == message
    quotation_service.validate_generation_authorization(session)


def test_ambiguous_quantity_language_requests_clarification_without_authorizing(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)

    with pytest.raises(QuoteNotConfirmedError) as raised:
        quotation_service.confirm(
            session,
            confirmation_context(session, "I might need 20."),
        )

    assert "confirm" in raised.value.public_message.casefold()
    assert session.quote_confirmed is False
    assert session.confirmation_turn_id is None
    assert session.confirmation_message is None


def test_confirmation_before_preview_is_rejected(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()

    with pytest.raises(InvalidConversationStateError):
        quotation_service.confirm(session, confirmation_context(session, "Yes"))

    assert session.quote_confirmed is False


def test_confirmation_before_successful_preview_delivery_is_rejected(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    quotation_service.prepare_quotation(session, "preview-turn")

    with pytest.raises(InvalidConversationStateError):
        quotation_service.confirm(session, confirmation_context(session, "Yes"))

    assert session.quote_confirmed is False


def test_confirmation_on_preview_originating_turn_is_rejected(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)

    with pytest.raises(InvalidConversationStateError):
        quotation_service.confirm(
            session,
            confirmation_context(session, "Yes", turn_id="preview-turn"),
        )

    assert session.quote_confirmed is False


def test_confirmation_from_a_different_but_not_later_turn_is_rejected(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)
    context = CurrentTurnContext(
        session_id=session.session_id,
        turn_id="older-turn",
        customer_message="Yes",
        preceding_turn_ids=(),
    )

    with pytest.raises(InvalidConversationStateError):
        quotation_service.confirm(session, context)

    assert session.quote_confirmed is False


def test_confirmation_after_direct_quote_change_is_rejected_as_stale(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)
    session.cart[0].quantity = 20

    with pytest.raises(StalePreviewError):
        quotation_service.confirm(session, confirmation_context(session, "Proceed"))

    assert session.quote_confirmed is False


def test_confirmation_context_from_another_session_is_rejected(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    other_session = ready_session()
    deliver_preview(quotation_service, session)

    with pytest.raises(InvalidConversationStateError):
        quotation_service.confirm(
            session,
            confirmation_context(other_session, "Confirmed"),
        )

    assert session.quote_confirmed is False


def test_generation_guard_rejects_delivered_but_unconfirmed_preview(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)

    with pytest.raises(QuoteNotConfirmedError):
        quotation_service.validate_generation_authorization(session)


def test_generation_guard_rejects_forged_flag_without_confirmation_evidence(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)
    session.quote_confirmed = True

    with pytest.raises(QuoteNotConfirmedError):
        quotation_service.validate_generation_authorization(session)


def test_generation_guard_rechecks_current_fingerprint(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)
    quotation_service.confirm(session, confirmation_context(session, "Yes"))
    session.customer.name = "Changed Customer"

    with pytest.raises(StalePreviewError):
        quotation_service.validate_generation_authorization(session)


def test_confirmation_requires_nonempty_cart(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)
    session.cart = []

    with pytest.raises(EmptyQuoteError):
        quotation_service.confirm(session, confirmation_context(session, "Yes"))


def test_confirmation_requires_current_customer_information(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    deliver_preview(quotation_service, session)
    session.customer.name = " "

    with pytest.raises(CustomerInformationRequiredError):
        quotation_service.confirm(session, confirmation_context(session, "Yes"))


def test_model_evaluator_receives_only_the_preserved_customer_message(
    quotation_service: QuotationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_messages: list[str] = []

    def evaluator(message: str) -> ConfirmationInterpretation:
        seen_messages.append(message)
        return ConfirmationInterpretation.EXPLICIT

    monkeypatch.setattr(quotation_service, "_confirmation_interpreter", evaluator)
    session = ready_session()
    deliver_preview(quotation_service, session)

    quotation_service.confirm(
        session,
        confirmation_context(session, "Certainly, go ahead."),
    )

    assert seen_messages == ["Certainly, go ahead."]
    assert session.quote_confirmed is True
