"""Deterministic quotation cart operations and lifecycle invalidation."""

from __future__ import annotations

import re
from typing import Final

from app.core.exceptions import (
    CustomerInformationRequiredError,
    InvalidConversationStateError,
    InvalidQuantityError,
    ProductNotFoundError,
    StalePreviewError,
)
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.product import Product
from app.schemas.quotation import CustomerInfo, QuoteCartItem, QuotePreview
from app.schemas.session import ConversationSession, ConversationState
from app.services.quotation_calculator import QuotationCalculator

NAME_MAX_LENGTH: Final = 200
EMAIL_MAX_LENGTH: Final = 254
ADDRESS_MAX_LENGTH: Final = 1_000
NOTES_MAX_LENGTH: Final = 2_000
_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


class _UnsetType:
    """Sentinel type distinguishing omission from an explicit null update."""

    __slots__ = ()


_UNSET: Final = _UnsetType()
CustomerUpdateValue = str | None | _UnsetType


class QuotationService:
    """Manage quotation state using authoritative catalog product identities."""

    __slots__ = ("_calculator", "_product_repository", "_rules_repository")

    def __init__(
        self,
        product_repository: ProductRepository | None = None,
        rules_repository: QuotationRulesRepository | None = None,
    ) -> None:
        self._product_repository = (
            product_repository if product_repository is not None else ProductRepository()
        )
        self._rules_repository = (
            rules_repository if rules_repository is not None else QuotationRulesRepository()
        )
        self._calculator = QuotationCalculator(
            self._product_repository,
            self._rules_repository,
        )

    def add_item(
        self,
        session: ConversationSession,
        product_id: str,
        quantity: object,
    ) -> None:
        """Add an active product, incrementing an existing cart quantity."""
        product = self._require_active_product(product_id)
        valid_quantity = _validate_quantity(quantity)
        new_cart = [item.model_copy(deep=True) for item in session.cart]

        existing = _find_cart_item(new_cart, product.id)
        if existing is None:
            new_cart.append(QuoteCartItem(product_id=product.id, quantity=valid_quantity))
        else:
            existing.quantity += valid_quantity

        _commit_cart_change(session, new_cart)

    def update_item_quantity(
        self,
        session: ConversationSession,
        product_id: str,
        quantity: object,
    ) -> None:
        """Replace the quantity of an active product already in the cart."""
        product = self._require_active_product(product_id)
        valid_quantity = _validate_quantity(quantity)
        existing = _find_cart_item(session.cart, product.id)
        if existing is None:
            raise ProductNotFoundError("Product is not present in the quotation cart")
        if existing.quantity == valid_quantity:
            return

        new_cart = [
            QuoteCartItem(
                product_id=item.product_id,
                quantity=valid_quantity if item.product_id == product.id else item.quantity,
            )
            for item in session.cart
        ]
        _commit_cart_change(session, new_cart)

    def remove_item(self, session: ConversationSession, product_id: str) -> None:
        """Remove an active product that is currently in the cart."""
        product = self._require_active_product(product_id)
        if _find_cart_item(session.cart, product.id) is None:
            raise ProductNotFoundError("Product is not present in the quotation cart")

        new_cart = [
            item.model_copy(deep=True) for item in session.cart if item.product_id != product.id
        ]
        _commit_cart_change(session, new_cart)

    def clear_cart(self, session: ConversationSession) -> None:
        """Remove every cart item, leaving an already-empty cart unchanged."""
        if not session.cart:
            return
        _commit_cart_change(session, [])

    def set_customer_information(
        self,
        session: ConversationSession,
        *,
        name: CustomerUpdateValue = _UNSET,
        company_name: CustomerUpdateValue = _UNSET,
        contact_person: CustomerUpdateValue = _UNSET,
        email: CustomerUpdateValue = _UNSET,
        address: CustomerUpdateValue = _UNSET,
        notes: CustomerUpdateValue = _UNSET,
    ) -> None:
        """Apply a validated partial customer update without exposing phone."""
        current = session.customer
        customer_data = current.model_dump(mode="python")
        customer_data["phone"] = session.customer_phone

        updates = {
            "name": _normalize_optional_string(name, "name", NAME_MAX_LENGTH),
            "company_name": _normalize_optional_string(
                company_name, "company_name", NAME_MAX_LENGTH
            ),
            "contact_person": _normalize_optional_string(
                contact_person, "contact_person", NAME_MAX_LENGTH
            ),
            "email": _normalize_email(email),
            "address": _normalize_optional_string(address, "address", ADDRESS_MAX_LENGTH),
            "notes": _normalize_optional_string(notes, "notes", NOTES_MAX_LENGTH),
        }
        customer_data.update(
            {field_name: value for field_name, value in updates.items() if value is not _UNSET}
        )
        candidate = CustomerInfo.model_validate(customer_data)
        if candidate == current:
            return

        session.customer = candidate
        _invalidate_quote_authorization(session)
        session.state = _customer_update_state(session)

    def validate_customer_information(self, session: ConversationSession) -> None:
        """Require a transport phone and at least one nonblank identity name."""
        if not _customer_information_is_ready(session):
            session.state = ConversationState.CUSTOMER_DETAILS
            raise CustomerInformationRequiredError(
                "A nonblank name or company name and transport phone are required"
            )

    def get_quote_summary(self, session: ConversationSession) -> str:
        """Return a deterministic current-value summary without changing state."""
        preview = self._calculate_preview(session)
        return _preview_text(preview)

    def prepare_quotation(
        self,
        session: ConversationSession,
        turn_id: str,
    ) -> QuotePreview:
        """Validate and bind a preview without authorizing confirmation."""
        valid_turn_id = _validate_binding_value(turn_id, "turn_id")
        preview = self._calculate_preview(session)
        self.validate_customer_information(session)
        fingerprint = self._current_quote_fingerprint(session)

        session.prepared_preview_fingerprint = fingerprint
        session.preview_delivered = False
        session.preview_originating_turn_id = valid_turn_id
        session.quote_confirmed = False
        session.confirmation_turn_id = None
        session.state = ConversationState.QUOTE_REVIEW
        return preview

    def mark_ready_for_confirmation(
        self,
        session: ConversationSession,
        preview_fingerprint: str,
        turn_id: str,
    ) -> None:
        """Record successful preview delivery when its binding is still current."""
        supplied_fingerprint = _validate_binding_value(
            preview_fingerprint,
            "preview_fingerprint",
        )
        supplied_turn_id = _validate_binding_value(turn_id, "turn_id")
        prepared_fingerprint = session.prepared_preview_fingerprint
        originating_turn_id = session.preview_originating_turn_id

        if (
            prepared_fingerprint is None
            or supplied_fingerprint != prepared_fingerprint
            or originating_turn_id is None
            or supplied_turn_id != originating_turn_id
        ):
            raise StalePreviewError("Preview delivery binding does not match preparation")

        current_fingerprint = self._current_quote_fingerprint(session)
        if current_fingerprint != prepared_fingerprint:
            raise StalePreviewError("Quotation inputs changed after preview preparation")
        if session.state is not ConversationState.QUOTE_REVIEW:
            raise InvalidConversationStateError(
                "Only a prepared quotation preview can become confirmable"
            )

        session.preview_delivered = True
        session.quote_confirmed = False
        session.confirmation_turn_id = None
        session.state = ConversationState.AWAITING_CONFIRMATION

    def _calculate_preview(self, session: ConversationSession) -> QuotePreview:
        lines, totals = self._calculator.calculate(session.cart)
        rules = self._rules_repository.get_rules()
        return QuotePreview(
            customer=session.customer.model_copy(deep=True),
            items=lines,
            totals=totals,
            validity_days=rules.validity_days,
            terms=list(rules.terms),
        )

    def _current_quote_fingerprint(self, session: ConversationSession) -> str:
        rules = self._rules_repository.get_rules()
        current_prices = {
            item.product_id: self._require_active_product(item.product_id).price.amount
            for item in session.cart
        }
        return session.quote_fingerprint(
            current_prices=current_prices,
            currency=rules.currency,
            quotation_rules=rules,
        )

    def _require_active_product(self, product_id: str) -> Product:
        """Resolve a catalog product and reject unknown or inactive records."""
        if not isinstance(product_id, str) or not product_id.strip():
            raise ProductNotFoundError("Missing or invalid product ID")
        product = self._product_repository.get_by_id(product_id)
        if product is None or not product.active:
            raise ProductNotFoundError("Unknown or inactive product requested")
        return product


def _validate_quantity(quantity: object) -> int:
    """Accept positive integers while rejecting booleans and coercion."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise InvalidQuantityError("Quantity must be a strict positive integer")
    return quantity


def _find_cart_item(cart: list[QuoteCartItem], product_id: str) -> QuoteCartItem | None:
    return next((item for item in cart if item.product_id == product_id), None)


def _normalize_optional_string(
    value: CustomerUpdateValue,
    field_name: str,
    max_length: int,
) -> str | _UnsetType | None:
    if value is _UNSET or value is None:
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return normalized


def _normalize_email(value: CustomerUpdateValue) -> str | _UnsetType | None:
    normalized = _normalize_optional_string(value, "email", EMAIL_MAX_LENGTH)
    if isinstance(normalized, _UnsetType) or normalized is None:
        return normalized
    if not normalized or not _EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("email must be a valid address")
    local_part, domain = normalized.rsplit("@", maxsplit=1)
    if (
        len(local_part) > 64
        or len(domain) > 253
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
    ):
        raise ValueError("email must be a valid address")
    return normalized


def _customer_information_is_ready(session: ConversationSession) -> bool:
    customer = session.customer
    has_identity = _is_nonblank(customer.name) or _is_nonblank(customer.company_name)
    has_transport_phone = _is_nonblank(session.customer_phone)
    phone_is_authoritative = customer.phone == session.customer_phone
    return has_identity and has_transport_phone and phone_is_authoritative


def _is_nonblank(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _validate_binding_value(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")
    return value.strip()


def _preview_text(preview: QuotePreview) -> str:
    """Render all preview facts in a stable, transport-independent format."""
    customer = preview.customer
    customer_lines = ["Customer:"]
    customer_fields = (
        ("Name", customer.name),
        ("Company", customer.company_name),
        ("Contact person", customer.contact_person),
        ("Phone", customer.phone),
        ("Email", customer.email),
        ("Address", customer.address),
        ("Notes", customer.notes),
    )
    customer_lines.extend(
        f"{label}: {value}" for label, value in customer_fields if _is_nonblank(value)
    )
    if len(customer_lines) == 1:
        customer_lines.append("Not provided")

    currency = preview.totals.currency
    item_lines = ["Items:"]
    item_lines.extend(
        (
            f"{index}. {item.description} | Quantity: {item.quantity} | "
            f"Unit price: {currency} {item.unit_price:.2f} | "
            f"Line subtotal: {currency} {item.subtotal:.2f}"
        )
        for index, item in enumerate(preview.items, start=1)
    )
    total_lines = [
        f"Subtotal: {currency} {preview.totals.subtotal:.2f}",
        f"Discount: {currency} {preview.totals.discount:.2f}",
        f"Tax: {currency} {preview.totals.tax:.2f}",
        f"Total: {currency} {preview.totals.total:.2f}",
        f"Currency: {currency}",
        f"Validity: {preview.validity_days} days",
        "Terms:",
        *(f"- {term}" for term in preview.terms),
        "Please confirm that I should generate the quotation.",
    ]
    return "\n".join((*customer_lines, *item_lines, *total_lines))


def _customer_update_state(session: ConversationSession) -> ConversationState:
    if not _customer_information_is_ready(session):
        return ConversationState.CUSTOMER_DETAILS
    if session.cart:
        return ConversationState.BUILDING_QUOTE
    return ConversationState.PRODUCT_DISCUSSION


def _commit_cart_change(
    session: ConversationSession,
    new_cart: list[QuoteCartItem],
) -> None:
    """Commit one validated change and revoke stale quote authorization."""
    session.cart = new_cart
    _invalidate_quote_authorization(session)
    session.state = (
        ConversationState.BUILDING_QUOTE if new_cart else ConversationState.PRODUCT_DISCUSSION
    )


def _invalidate_quote_authorization(session: ConversationSession) -> None:
    """Revoke preview, confirmation, and replay bindings after quote input changes."""
    session.quote_revision += 1
    session.prepared_preview_fingerprint = None
    session.preview_delivered = False
    session.preview_originating_turn_id = None
    session.quote_confirmed = False
    session.confirmation_turn_id = None
    session.last_generated_quote = None


__all__ = [
    "ADDRESS_MAX_LENGTH",
    "EMAIL_MAX_LENGTH",
    "NAME_MAX_LENGTH",
    "NOTES_MAX_LENGTH",
    "QuotationService",
]
