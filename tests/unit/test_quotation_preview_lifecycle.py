"""Tests for quotation preview calculation, binding, and delivery lifecycle."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.exceptions import CustomerInformationRequiredError, StalePreviewError
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.quotation import CustomerInfo, QuoteCartItem, QuotePreview
from app.schemas.session import ConversationSession, ConversationState
from app.services.quotation_service import QuotationService


def product_payload(
    product_id: str,
    *,
    name: str,
    price: str,
    size: str,
    unit: str,
) -> dict[str, object]:
    return {
        "id": product_id,
        "sku": f"SKU-{product_id}",
        "name": name,
        "category": "Development Cleaning",
        "short_description": "Synthetic preview-test product.",
        "description": "Synthetic product used only for preview lifecycle tests.",
        "packaging": {"size": size, "unit": unit},
        "price": {"amount": price, "currency": "QAR"},
        "active": True,
    }


@pytest.fixture
def quotation_service(tmp_path: Path) -> QuotationService:
    product_path = tmp_path / "products.json"
    product_path.write_text(
        json.dumps(
            [
                product_payload(
                    "FLOOR",
                    name="Floor Cleaner",
                    price="25.00",
                    size="5",
                    unit="L",
                ),
                product_payload(
                    "GLASS",
                    name="Glass Cleaner",
                    price="10.00",
                    size="750",
                    unit="mL",
                ),
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
                "default_discount_percent": "10.00",
                "tax_percent": "5.00",
                "terms": ["Payment due on receipt.", "Subject to availability."],
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
        customer=CustomerInfo(
            phone="+97450000000",
            name="Aisha",
            company_name="Example LLC",
            email="aisha@example.com",
        ),
        cart=[
            QuoteCartItem(product_id="FLOOR", quantity=2),
            QuoteCartItem(product_id="GLASS", quantity=1),
        ],
        state=ConversationState.BUILDING_QUOTE,
    )


def test_get_quote_summary_is_deterministic_current_calculation_without_mutation(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    before = session.model_dump_json()

    summary = quotation_service.get_quote_summary(session)

    assert summary == "\n".join(
        [
            "Customer:",
            "Name: Aisha",
            "Company: Example LLC",
            "Phone: +97450000000",
            "Email: aisha@example.com",
            "Items:",
            (
                "1. Floor Cleaner 5 L | Quantity: 2 | Unit price: QAR 25.00 | "
                "Line subtotal: QAR 50.00"
            ),
            (
                "2. Glass Cleaner 750 mL | Quantity: 1 | Unit price: QAR 10.00 | "
                "Line subtotal: QAR 10.00"
            ),
            "Subtotal: QAR 60.00",
            "Discount: QAR 6.00",
            "Tax: QAR 2.70",
            "Total: QAR 56.70",
            "Currency: QAR",
            "Validity: 14 days",
            "Terms:",
            "- Payment due on receipt.",
            "- Subject to availability.",
            "Please confirm that I should generate the quotation.",
        ]
    )
    assert session.model_dump_json() == before
    assert session.last_quotation_id is None


def test_prepare_builds_preview_and_binds_it_without_authorizing_confirmation(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    session.quote_confirmed = True
    session.confirmation_turn_id = "old-confirmation"

    preview = quotation_service.prepare_quotation(session, "preview-turn")

    assert isinstance(preview, QuotePreview)
    assert preview.customer == session.customer
    assert preview.customer is not session.customer
    assert [line.description for line in preview.items] == [
        "Floor Cleaner 5 L",
        "Glass Cleaner 750 mL",
    ]
    assert preview.totals.subtotal == Decimal("60.00")
    assert preview.totals.discount == Decimal("6.00")
    assert preview.totals.tax == Decimal("2.70")
    assert preview.totals.total == Decimal("56.70")
    assert preview.validity_days == 14
    assert preview.terms == ["Payment due on receipt.", "Subject to availability."]
    assert session.state is ConversationState.QUOTE_REVIEW
    assert session.prepared_preview_fingerprint is not None
    assert len(session.prepared_preview_fingerprint) == 64
    assert session.preview_originating_turn_id == "preview-turn"
    assert session.preview_delivered is False
    assert session.quote_confirmed is False
    assert session.confirmation_turn_id is None
    assert session.last_quotation_id is None


def test_failed_delivery_leaves_preview_unavailable_for_confirmation(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()

    quotation_service.prepare_quotation(session, "preview-turn")
    # A failed transport does not call mark_ready_for_confirmation.

    assert session.state is ConversationState.QUOTE_REVIEW
    assert session.preview_delivered is False
    assert session.quote_confirmed is False


def test_successful_matching_delivery_enters_awaiting_confirmation(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    quotation_service.prepare_quotation(session, "preview-turn")
    fingerprint = session.prepared_preview_fingerprint
    assert fingerprint is not None

    quotation_service.mark_ready_for_confirmation(session, fingerprint, "preview-turn")

    assert session.state is ConversationState.AWAITING_CONFIRMATION
    assert session.preview_delivered is True
    assert session.quote_confirmed is False
    assert session.confirmation_turn_id is None


@pytest.mark.parametrize(
    ("fingerprint", "turn_id"),
    [("wrong-fingerprint", "preview-turn"), (None, "older-turn")],
)
def test_wrong_fingerprint_or_turn_cannot_authorize_preview(
    quotation_service: QuotationService,
    fingerprint: str | None,
    turn_id: str,
) -> None:
    session = ready_session()
    quotation_service.prepare_quotation(session, "preview-turn")
    supplied_fingerprint = fingerprint or session.prepared_preview_fingerprint
    assert supplied_fingerprint is not None

    with pytest.raises(StalePreviewError):
        quotation_service.mark_ready_for_confirmation(
            session,
            supplied_fingerprint,
            turn_id,
        )

    assert session.state is ConversationState.QUOTE_REVIEW
    assert session.preview_delivered is False


def test_directly_mutated_preview_inputs_fail_recomputed_fingerprint_check(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    quotation_service.prepare_quotation(session, "preview-turn")
    fingerprint = session.prepared_preview_fingerprint
    assert fingerprint is not None
    session.cart[0].quantity = 3

    with pytest.raises(StalePreviewError):
        quotation_service.mark_ready_for_confirmation(session, fingerprint, "preview-turn")

    assert session.state is ConversationState.QUOTE_REVIEW
    assert session.preview_delivered is False


def test_service_cart_mutation_clears_preview_and_stale_delivery_cannot_restore_it(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    quotation_service.prepare_quotation(session, "preview-turn")
    fingerprint = session.prepared_preview_fingerprint
    assert fingerprint is not None
    quotation_service.add_item(session, "FLOOR", 1)

    with pytest.raises(StalePreviewError):
        quotation_service.mark_ready_for_confirmation(session, fingerprint, "preview-turn")

    assert session.state is ConversationState.BUILDING_QUOTE
    assert session.prepared_preview_fingerprint is None
    assert session.preview_delivered is False


def test_older_turn_cannot_authorize_reprepared_identical_preview(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    quotation_service.prepare_quotation(session, "older-turn")
    old_fingerprint = session.prepared_preview_fingerprint
    quotation_service.prepare_quotation(session, "newer-turn")
    assert session.prepared_preview_fingerprint == old_fingerprint
    assert old_fingerprint is not None

    with pytest.raises(StalePreviewError):
        quotation_service.mark_ready_for_confirmation(session, old_fingerprint, "older-turn")

    assert session.state is ConversationState.QUOTE_REVIEW
    assert session.preview_delivered is False


def test_missing_customer_information_raises_and_enters_customer_details(
    quotation_service: QuotationService,
) -> None:
    session = ready_session()
    session.customer = CustomerInfo(phone=session.customer_phone, name="   ")

    with pytest.raises(CustomerInformationRequiredError):
        quotation_service.prepare_quotation(session, "preview-turn")

    assert session.state is ConversationState.CUSTOMER_DETAILS
    assert session.prepared_preview_fingerprint is None
    assert session.preview_delivered is False
    assert session.quote_confirmed is False
    assert session.last_quotation_id is None
