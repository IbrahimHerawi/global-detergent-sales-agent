"""Tests for atomic quotation cart operations and lifecycle invalidation."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from app.core.exceptions import InvalidQuantityError, ProductNotFoundError
from app.repositories.product_repository import ProductRepository
from app.schemas.quotation import QuoteCartItem
from app.schemas.session import (
    ConversationSession,
    ConversationState,
    GeneratedQuoteReplayMetadata,
)
from app.services.quotation_service import QuotationService

ACTIVE_PRODUCT_ID = "GDF-ACTIVE-001"
SECOND_PRODUCT_ID = "GDF-ACTIVE-002"
INACTIVE_PRODUCT_ID = "GDF-INACTIVE-001"


def product_payload(product_id: str, *, active: bool = True) -> dict[str, object]:
    """Build a complete minimal product record for repository-backed tests."""
    return {
        "id": product_id,
        "sku": f"SKU-{product_id}",
        "name": f"Product {product_id}",
        "category": "Development Cleaning",
        "short_description": "Synthetic cart-test product.",
        "description": "Synthetic product used only for cart operation tests.",
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "123.45", "currency": "QAR"},
        "active": active,
    }


@pytest.fixture
def quotation_service(tmp_path: Path) -> QuotationService:
    catalog_path = tmp_path / "products.json"
    catalog_path.write_text(
        json.dumps(
            [
                product_payload(ACTIVE_PRODUCT_ID),
                product_payload(SECOND_PRODUCT_ID),
                product_payload(INACTIVE_PRODUCT_ID, active=False),
            ]
        ),
        encoding="utf-8",
    )
    return QuotationService(ProductRepository(catalog_path))


def session_with_cart(*items: tuple[str, int]) -> ConversationSession:
    return ConversationSession(
        customer_phone="+97450000000",
        cart=[
            QuoteCartItem(product_id=product_id, quantity=quantity)
            for product_id, quantity in items
        ],
    )


def prepared_session(*items: tuple[str, int]) -> ConversationSession:
    """Build state carrying every preview and confirmation binding."""
    fingerprint = "a" * 64
    session = session_with_cart(*items)
    session.state = ConversationState.AWAITING_CONFIRMATION
    session.quote_revision = 7
    session.prepared_preview_fingerprint = fingerprint
    session.preview_delivered = True
    session.preview_originating_turn_id = "preview-turn"
    session.quote_confirmed = True
    session.confirmation_turn_id = "confirmation-turn"
    session.last_quotation_id = "GDF-Q-20260919-001"
    session.last_generated_quote = GeneratedQuoteReplayMetadata(
        quotation_id="GDF-Q-20260919-001",
        quote_fingerprint=fingerprint,
        confirmation_turn_id="confirmation-turn",
        pdf_path="/app/storage/quotes/GDF-Q-20260919-001.pdf",
    )
    return session


def assert_quote_authorization_invalidated(session: ConversationSession) -> None:
    assert session.quote_revision == 8
    assert session.prepared_preview_fingerprint is None
    assert session.preview_delivered is False
    assert session.preview_originating_turn_id is None
    assert session.quote_confirmed is False
    assert session.confirmation_turn_id is None
    assert session.last_generated_quote is None


def test_add_item_stores_only_canonical_product_id_and_quantity(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart()

    quotation_service.add_item(session, f"  {ACTIVE_PRODUCT_ID}  ", 2)

    assert session.cart == [QuoteCartItem(product_id=ACTIVE_PRODUCT_ID, quantity=2)]
    assert session.cart[0].model_dump() == {
        "product_id": ACTIVE_PRODUCT_ID,
        "quantity": 2,
    }
    assert session.state is ConversationState.BUILDING_QUOTE
    assert session.quote_revision == 1


def test_add_existing_item_increments_quantity(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 2))

    quotation_service.add_item(session, ACTIVE_PRODUCT_ID, 3)

    assert session.cart == [QuoteCartItem(product_id=ACTIVE_PRODUCT_ID, quantity=5)]


def test_update_item_quantity_replaces_instead_of_incrementing(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 2))

    quotation_service.update_item_quantity(session, ACTIVE_PRODUCT_ID, 3)

    assert session.cart == [QuoteCartItem(product_id=ACTIVE_PRODUCT_ID, quantity=3)]
    assert session.state is ConversationState.BUILDING_QUOTE


def test_removing_one_item_keeps_nonempty_cart_in_building_quote(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 2), (SECOND_PRODUCT_ID, 1))

    quotation_service.remove_item(session, SECOND_PRODUCT_ID)

    assert session.cart == [QuoteCartItem(product_id=ACTIVE_PRODUCT_ID, quantity=2)]
    assert session.state is ConversationState.BUILDING_QUOTE


def test_removing_last_item_enters_product_discussion(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 2))

    quotation_service.remove_item(session, ACTIVE_PRODUCT_ID)

    assert session.cart == []
    assert session.state is ConversationState.PRODUCT_DISCUSSION


def test_clear_cart_enters_product_discussion(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 2), (SECOND_PRODUCT_ID, 1))

    quotation_service.clear_cart(session)

    assert session.cart == []
    assert session.state is ConversationState.PRODUCT_DISCUSSION


Mutation = Callable[[QuotationService, ConversationSession], None]


@pytest.mark.parametrize(
    ("mutation", "expected_state"),
    [
        (
            lambda service, session: service.add_item(session, ACTIVE_PRODUCT_ID, 1),
            ConversationState.BUILDING_QUOTE,
        ),
        (
            lambda service, session: service.update_item_quantity(
                session, ACTIVE_PRODUCT_ID, 4
            ),
            ConversationState.BUILDING_QUOTE,
        ),
        (
            lambda service, session: service.remove_item(session, SECOND_PRODUCT_ID),
            ConversationState.BUILDING_QUOTE,
        ),
        (
            lambda service, session: service.clear_cart(session),
            ConversationState.PRODUCT_DISCUSSION,
        ),
    ],
)
def test_every_actual_cart_change_invalidates_preview_and_confirmation(
    quotation_service: QuotationService,
    mutation: Mutation,
    expected_state: ConversationState,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 2), (SECOND_PRODUCT_ID, 1))

    mutation(quotation_service, session)

    assert_quote_authorization_invalidated(session)
    assert session.state is expected_state
    assert session.last_quotation_id == "GDF-Q-20260919-001"


@pytest.mark.parametrize("product_id", ["GDF-UNKNOWN-001", INACTIVE_PRODUCT_ID])
@pytest.mark.parametrize("operation", ["add", "update", "remove"])
def test_unknown_or_inactive_product_fails_without_mutating_session(
    quotation_service: QuotationService,
    operation: str,
    product_id: str,
) -> None:
    session = prepared_session((product_id, 2))
    before = session.model_dump_json()

    with pytest.raises(ProductNotFoundError):
        if operation == "add":
            quotation_service.add_item(session, product_id, 1)
        elif operation == "update":
            quotation_service.update_item_quantity(session, product_id, 3)
        else:
            quotation_service.remove_item(session, product_id)

    assert session.model_dump_json() == before


@pytest.mark.parametrize("quantity", [0, -1, 1.0, True, "1", None])
@pytest.mark.parametrize("operation", ["add", "update"])
def test_invalid_quantity_fails_without_mutating_session(
    quotation_service: QuotationService,
    operation: str,
    quantity: object,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 2))
    before = session.model_dump_json()

    with pytest.raises(InvalidQuantityError):
        if operation == "add":
            quotation_service.add_item(session, ACTIVE_PRODUCT_ID, quantity)
        else:
            quotation_service.update_item_quantity(session, ACTIVE_PRODUCT_ID, quantity)

    assert session.model_dump_json() == before


@pytest.mark.parametrize("operation", ["update", "remove"])
def test_update_or_remove_requires_existing_cart_entry_without_mutation(
    quotation_service: QuotationService,
    operation: str,
) -> None:
    session = prepared_session((SECOND_PRODUCT_ID, 2))
    before = session.model_dump_json()

    with pytest.raises(ProductNotFoundError):
        if operation == "update":
            quotation_service.update_item_quantity(session, ACTIVE_PRODUCT_ID, 3)
        else:
            quotation_service.remove_item(session, ACTIVE_PRODUCT_ID)

    assert session.model_dump_json() == before


def test_equal_quantity_update_is_a_no_op(
    quotation_service: QuotationService,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 2))
    before = session.model_dump_json()

    quotation_service.update_item_quantity(session, ACTIVE_PRODUCT_ID, 2)

    assert session.model_dump_json() == before


def test_clearing_empty_cart_is_a_no_op(quotation_service: QuotationService) -> None:
    session = prepared_session()
    before = session.model_dump_json()

    quotation_service.clear_cart(session)

    assert session.model_dump_json() == before
