"""Tests for atomic quotation cart operations and lifecycle invalidation."""

import inspect
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from app.core.exceptions import (
    CustomerInformationRequiredError,
    InvalidQuantityError,
    ProductNotFoundError,
)
from app.repositories.product_repository import ProductRepository
from app.schemas.quotation import CustomerInfo, QuoteCartItem
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


def complete_customer() -> CustomerInfo:
    return CustomerInfo(
        phone="+97450000000",
        name="Original Name",
        company_name="Original Company",
        contact_person="Original Contact",
        email="original@example.com",
        address="Original Address",
        notes="Original Notes",
    )


def test_customer_partial_update_trims_values_and_preserves_omitted_fields(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 1))
    session.customer = complete_customer()

    quotation_service.set_customer_information(
        session,
        name="  Updated Name  ",
        email="  updated@example.com  ",
    )

    assert session.customer == CustomerInfo(
        phone="+97450000000",
        name="Updated Name",
        company_name="Original Company",
        contact_person="Original Contact",
        email="updated@example.com",
        address="Original Address",
        notes="Original Notes",
    )
    assert session.state is ConversationState.BUILDING_QUOTE


def test_explicit_null_clears_optional_customer_fields(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 1))
    session.customer = complete_customer()

    quotation_service.set_customer_information(
        session,
        company_name=None,
        contact_person=None,
        email=None,
        address=None,
        notes=None,
    )

    assert session.customer == CustomerInfo(phone="+97450000000", name="Original Name")
    assert session.state is ConversationState.BUILDING_QUOTE


def test_whitespace_is_trimmed_and_whitespace_identity_is_not_ready(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 1))

    quotation_service.set_customer_information(
        session,
        name="   ",
        company_name="  ",
        contact_person="  Contact Person  ",
        address="  Doha  ",
        notes="  Call first.  ",
    )

    assert session.customer.name == ""
    assert session.customer.company_name == ""
    assert session.customer.contact_person == "Contact Person"
    assert session.customer.address == "Doha"
    assert session.customer.notes == "Call first."
    assert session.state is ConversationState.CUSTOMER_DETAILS
    with pytest.raises(CustomerInformationRequiredError):
        quotation_service.validate_customer_information(session)


def test_all_bounded_customer_fields_accept_their_limits(
    quotation_service: QuotationService,
) -> None:
    session = session_with_cart()
    email = f"{'a' * 64}@{'b' * 61}.{'c' * 61}.{'d' * 61}.com"
    assert len(email) == 254

    quotation_service.set_customer_information(
        session,
        name="n" * 200,
        company_name="c" * 200,
        contact_person="p" * 200,
        email=email,
        address="a" * 1_000,
        notes="x" * 2_000,
    )

    assert session.customer.name == "n" * 200
    assert session.customer.company_name == "c" * 200
    assert session.customer.contact_person == "p" * 200
    assert session.customer.email == email
    assert session.customer.address == "a" * 1_000
    assert session.customer.notes == "x" * 2_000


InvalidCustomerUpdate = Callable[[QuotationService, ConversationSession], None]


@pytest.mark.parametrize(
    "update",
    [
        lambda service, session: service.set_customer_information(
            session, name="n" * 201
        ),
        lambda service, session: service.set_customer_information(
            session, company_name="c" * 201
        ),
        lambda service, session: service.set_customer_information(
            session, contact_person="p" * 201
        ),
        lambda service, session: service.set_customer_information(
            session, email=f"{'a' * 243}@example.com"
        ),
        lambda service, session: service.set_customer_information(
            session, address="a" * 1_001
        ),
        lambda service, session: service.set_customer_information(
            session, notes="n" * 2_001
        ),
    ],
)
def test_overlong_customer_fields_fail_atomically(
    quotation_service: QuotationService,
    update: InvalidCustomerUpdate,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 1))
    session.customer = complete_customer()
    before = session.model_dump_json()

    with pytest.raises(ValueError):
        update(quotation_service, session)

    assert session.model_dump_json() == before


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "missing-at.example.com",
        "two@@example.com",
        "spaces are@example.com",
        "missing-domain@",
        "missing-dot@example",
        ".invalid@example.com",
    ],
)
def test_malformed_email_fails_without_mutating_session(
    quotation_service: QuotationService,
    email: str,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 1))
    session.customer = complete_customer()
    before = session.model_dump_json()

    with pytest.raises(ValueError, match="valid address"):
        quotation_service.set_customer_information(session, email=email)

    assert session.model_dump_json() == before


def test_customer_phone_is_not_an_update_parameter_and_remains_transport_owned(
    quotation_service: QuotationService,
) -> None:
    parameters = inspect.signature(QuotationService.set_customer_information).parameters
    session = session_with_cart()
    session.customer = CustomerInfo(phone="+111111", name="Original")

    quotation_service.set_customer_information(session, name="Updated")

    assert "phone" not in parameters
    assert session.customer.phone == session.customer_phone == "+97450000000"


@pytest.mark.parametrize(
    "customer",
    [
        CustomerInfo(phone="+97450000000", name="Customer Name"),
        CustomerInfo(phone="+97450000000", company_name="Company Name"),
    ],
)
def test_name_or_company_name_with_transport_phone_is_ready(
    quotation_service: QuotationService,
    customer: CustomerInfo,
) -> None:
    session = session_with_cart((ACTIVE_PRODUCT_ID, 1))
    session.customer = customer
    session.state = ConversationState.BUILDING_QUOTE

    quotation_service.validate_customer_information(session)

    assert session.state is ConversationState.BUILDING_QUOTE


@pytest.mark.parametrize(
    ("customer_phone", "customer"),
    [
        ("+97450000000", CustomerInfo(phone="+97450000000")),
        ("+97450000000", CustomerInfo(phone="+97450000000", name="   ")),
        ("", CustomerInfo(phone="", name="Customer Name")),
        ("+97450000000", CustomerInfo(phone="+111111", name="Customer Name")),
    ],
)
def test_missing_required_identity_enters_customer_details(
    quotation_service: QuotationService,
    customer_phone: str,
    customer: CustomerInfo,
) -> None:
    session = ConversationSession(customer_phone=customer_phone, customer=customer)
    session.state = ConversationState.QUOTE_REVIEW

    with pytest.raises(CustomerInformationRequiredError):
        quotation_service.validate_customer_information(session)

    assert session.state is ConversationState.CUSTOMER_DETAILS


def test_clearing_required_identity_enters_customer_details_and_invalidates(
    quotation_service: QuotationService,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 1))
    session.customer = CustomerInfo(phone=session.customer_phone, name="Customer Name")

    quotation_service.set_customer_information(session, name=None, company_name=None)

    assert session.customer.name is None
    assert session.state is ConversationState.CUSTOMER_DETAILS
    assert_quote_authorization_invalidated(session)


def test_actual_customer_change_with_cart_invalidates_confirmation(
    quotation_service: QuotationService,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 1))

    quotation_service.set_customer_information(session, company_name="Company Name")

    assert session.customer.company_name == "Company Name"
    assert session.state is ConversationState.BUILDING_QUOTE
    assert_quote_authorization_invalidated(session)


def test_ready_customer_without_cart_enters_product_discussion(
    quotation_service: QuotationService,
) -> None:
    session = prepared_session()

    quotation_service.set_customer_information(session, name="Customer Name")

    assert session.state is ConversationState.PRODUCT_DISCUSSION
    assert_quote_authorization_invalidated(session)


def test_equivalent_trimmed_customer_update_is_a_no_op(
    quotation_service: QuotationService,
) -> None:
    session = prepared_session((ACTIVE_PRODUCT_ID, 1))
    session.customer = CustomerInfo(phone=session.customer_phone, name="Customer Name")
    before = session.model_dump_json()

    quotation_service.set_customer_information(session, name="  Customer Name  ")

    assert session.model_dump_json() == before
