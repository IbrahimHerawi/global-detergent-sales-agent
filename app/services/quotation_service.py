"""Deterministic quotation cart operations and lifecycle invalidation."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from app.core.exceptions import (
    CustomerInformationRequiredError,
    InvalidConversationStateError,
    InvalidQuantityError,
    ProductNotFoundError,
    QuoteNotConfirmedError,
    StalePreviewError,
)
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.product import Product
from app.schemas.quotation import CustomerInfo, GeneratedQuotation, QuoteCartItem, QuotePreview
from app.schemas.session import (
    ConversationSession,
    ConversationState,
    GeneratedQuoteReplayMetadata,
)
from app.services.pdf_service import PDFService
from app.services.quotation_calculator import QuotationCalculator
from app.services.quotation_id import generate_quotation_id

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


class ConfirmationInterpretation(StrEnum):
    """Semantic result produced from the current customer message only."""

    EXPLICIT = "EXPLICIT"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class CurrentTurnContext:
    """Backend-owned evidence for the customer turn invoking confirmation.

    This context is constructed by conversation orchestration, not from tool
    arguments. In particular, it deliberately contains no state, preview,
    fingerprint, or authorization flag that a model could provide.
    """

    session_id: UUID
    turn_id: str
    customer_message: str
    preceding_turn_ids: tuple[str, ...]


ConfirmationInterpreter = Callable[[str], ConfirmationInterpretation]
Clock = Callable[[], datetime]
QuotationIdFactory = Callable[[str, datetime], str]


class QuotationPDFGenerator(Protocol):
    """Trusted boundary that creates the final quotation artifact."""

    async def generate_quotation_pdf(self, quotation: GeneratedQuotation) -> Path:
        """Create one PDF and return its internal storage path."""
        ...


class QuotationService:
    """Manage quotation state using authoritative catalog product identities."""

    __slots__ = (
        "_calculator",
        "_clock",
        "_confirmation_interpreter",
        "_pdf_service",
        "_product_repository",
        "_quotation_id_factory",
        "_rules_repository",
    )

    def __init__(
        self,
        product_repository: ProductRepository | None = None,
        rules_repository: QuotationRulesRepository | None = None,
        confirmation_interpreter: ConfirmationInterpreter | None = None,
        pdf_service: QuotationPDFGenerator | None = None,
        clock: Clock | None = None,
        quotation_id_factory: QuotationIdFactory | None = None,
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
        self._confirmation_interpreter = (
            confirmation_interpreter
            if confirmation_interpreter is not None
            else _interpret_conservative_confirmation
        )
        self._pdf_service = pdf_service
        self._clock = clock if clock is not None else _utc_now
        self._quotation_id_factory = (
            quotation_id_factory
            if quotation_id_factory is not None
            else _generate_backend_quotation_id
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
        session.confirmation_message = None
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
        session.confirmation_message = None
        session.state = ConversationState.AWAITING_CONFIRMATION

    def confirm(
        self,
        session: ConversationSession,
        current_turn_context: CurrentTurnContext,
    ) -> None:
        """Confirm the delivered current preview from a later customer turn.

        Calling this method expresses the model's request to interpret the
        current message as confirmation. Authorization still depends entirely
        on backend-owned session and preview state, plus semantic evaluation of
        the preserved message. No caller-supplied boolean can authorize it.
        """
        turn_id, customer_message = _validate_current_turn_context(
            session,
            current_turn_context,
        )
        self._validate_current_delivered_preview(session)

        preview_turn_id = session.preview_originating_turn_id
        if (
            preview_turn_id is None
            or turn_id == preview_turn_id
            or preview_turn_id not in current_turn_context.preceding_turn_ids
        ):
            raise InvalidConversationStateError(
                "Confirmation must come from a customer turn after preview delivery"
            )

        interpretation = self._confirmation_interpreter(customer_message)
        if interpretation is not ConfirmationInterpretation.EXPLICIT:
            raise QuoteNotConfirmedError(
                "The current customer message is not an explicit confirmation"
            )

        session.quote_confirmed = True
        session.confirmation_turn_id = turn_id
        session.confirmation_message = customer_message

    def validate_generation_authorization(self, session: ConversationSession) -> None:
        """Fail unless the current delivered preview has explicit authorization."""
        self._validate_current_delivered_preview(session)
        if (
            not session.quote_confirmed
            or not _is_nonblank(session.confirmation_turn_id)
            or not _is_nonblank(session.confirmation_message)
        ):
            raise QuoteNotConfirmedError(
                "The current quotation has no preserved explicit confirmation evidence"
            )

    async def generate(self, session: ConversationSession) -> GeneratedQuotation:
        """Generate or replay the quotation bound to the confirmed preview.

        All commercial values are recalculated from backend repositories. The
        session is committed only after the PDF service returns successfully,
        so a rendering or storage failure leaves the confirmed preview
        recoverable for a later retry.
        """
        replay = self._replay_generated_quotation(session)
        if replay is not None:
            return replay

        # Revalidate every authorization and commercial input immediately
        # before freezing the object passed to the PDF boundary.
        self.validate_generation_authorization(session)
        preview = self._calculate_preview(session)
        quote_fingerprint = self._current_quote_fingerprint(session)
        if quote_fingerprint != session.prepared_preview_fingerprint:
            raise StalePreviewError("Quotation inputs changed before generation")

        rules = self._rules_repository.get_rules()
        issued_at = _normalize_generation_time(self._clock())
        quotation_id = self._quotation_id_factory(rules.quotation_prefix, issued_at)
        quotation = _build_generated_quotation(
            preview,
            quotation_id=quotation_id,
            issued_at=issued_at,
        )

        pdf_service = self._pdf_service
        if pdf_service is None:
            pdf_service = PDFService()
        pdf_path = await pdf_service.generate_quotation_pdf(quotation)
        generated = _build_generated_quotation(
            preview,
            quotation_id=quotation_id,
            issued_at=issued_at,
            pdf_path=str(pdf_path),
        )
        confirmation_turn_id = session.confirmation_turn_id
        if confirmation_turn_id is None:  # guarded above; fail closed if state was mutated
            raise QuoteNotConfirmedError("Confirmation evidence disappeared during generation")
        replay_metadata = GeneratedQuoteReplayMetadata(
            quotation_id=quotation_id,
            quote_fingerprint=quote_fingerprint,
            confirmation_turn_id=confirmation_turn_id,
            pdf_path=str(pdf_path),
            generated_at=issued_at,
        )

        # These are the only success mutations and occur after PDF creation.
        session.last_generated_quote = replay_metadata
        session.last_quotation_id = quotation_id
        session.state = ConversationState.QUOTE_GENERATED
        return generated

    def _replay_generated_quotation(
        self,
        session: ConversationSession,
    ) -> GeneratedQuotation | None:
        """Recreate the immutable successful snapshot without another PDF."""
        metadata = session.last_generated_quote
        if metadata is None:
            return None

        preview = self._calculate_preview(session)
        self.validate_customer_information(session)
        current_fingerprint = self._current_quote_fingerprint(session)
        if (
            session.state is not ConversationState.QUOTE_GENERATED
            or session.last_quotation_id != metadata.quotation_id
            or session.prepared_preview_fingerprint != metadata.quote_fingerprint
            or current_fingerprint != metadata.quote_fingerprint
            or session.confirmation_turn_id != metadata.confirmation_turn_id
            or not session.quote_confirmed
        ):
            raise StalePreviewError("Generated quotation replay binding is stale")

        return _build_generated_quotation(
            preview,
            quotation_id=metadata.quotation_id,
            issued_at=metadata.generated_at,
            pdf_path=metadata.pdf_path,
        )

    def _validate_current_delivered_preview(
        self,
        session: ConversationSession,
    ) -> None:
        """Validate all backend-owned facts shared by confirm and generation."""
        self._calculate_preview(session)
        self.validate_customer_information(session)

        if session.state is not ConversationState.AWAITING_CONFIRMATION:
            raise InvalidConversationStateError("Quotation must be awaiting confirmation")
        if not session.preview_delivered:
            raise InvalidConversationStateError(
                "The current quotation preview was not successfully delivered"
            )

        prepared_fingerprint = session.prepared_preview_fingerprint
        if prepared_fingerprint is None:
            raise StalePreviewError("No delivered quotation preview is bound to the session")
        if self._current_quote_fingerprint(session) != prepared_fingerprint:
            raise StalePreviewError("Quotation inputs changed after preview delivery")

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


def _validate_current_turn_context(
    session: ConversationSession,
    context: CurrentTurnContext,
) -> tuple[str, str]:
    if not isinstance(context, CurrentTurnContext):
        raise TypeError("current_turn_context must be backend-owned CurrentTurnContext")
    if context.session_id != session.session_id:
        raise InvalidConversationStateError(
            "Confirmation turn belongs to a different conversation session"
        )
    turn_id = _validate_binding_value(context.turn_id, "turn_id")
    if not isinstance(context.preceding_turn_ids, tuple) or any(
        not isinstance(prior_turn_id, str) or not prior_turn_id.strip()
        for prior_turn_id in context.preceding_turn_ids
    ):
        raise TypeError("preceding_turn_ids must contain backend-owned turn IDs")
    if not isinstance(context.customer_message, str) or not context.customer_message.strip():
        raise QuoteNotConfirmedError("A current customer message is required for confirmation")
    return turn_id, context.customer_message


_EXPLICIT_CONFIRMATION_MESSAGES: Final = frozenset(
    {
        "yes",
        "confirmed",
        "correct",
        "proceed",
        "generate it",
        "generate the quotation",
        "please generate it",
        "please generate the quotation",
    }
)


def _interpret_conservative_confirmation(message: str) -> ConfirmationInterpretation:
    """Recognize only canonical confirmations when no model evaluator is wired.

    Broader natural-language meaning requires model evaluation. This fallback
    intentionally treats uncertainty, proposed changes, and all unrecognized
    wording as ambiguous rather than granting quotation authorization.
    """
    normalized = re.sub(r"[^\w\s]", "", message.casefold()).strip()
    normalized = " ".join(normalized.split())
    if normalized in _EXPLICIT_CONFIRMATION_MESSAGES:
        return ConfirmationInterpretation.EXPLICIT
    return ConfirmationInterpretation.AMBIGUOUS


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_generation_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quotation generation clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _generate_backend_quotation_id(prefix: str, issued_at: datetime) -> str:
    return generate_quotation_id(prefix, now=issued_at)


def _build_generated_quotation(
    preview: QuotePreview,
    *,
    quotation_id: str,
    issued_at: datetime,
    pdf_path: str | None = None,
) -> GeneratedQuotation:
    quotation_data = {
        **preview.model_dump(mode="python"),
        "quotation_id": quotation_id,
        "issued_at": issued_at,
        "valid_until": issued_at.date() + timedelta(days=preview.validity_days),
    }
    if pdf_path is None:
        return GeneratedQuotation.model_validate(quotation_data)
    return GeneratedQuotation.with_pdf_path(pdf_path=pdf_path, **quotation_data)


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
    session.confirmation_message = None
    session.last_generated_quote = None


__all__ = [
    "ADDRESS_MAX_LENGTH",
    "EMAIL_MAX_LENGTH",
    "NAME_MAX_LENGTH",
    "NOTES_MAX_LENGTH",
    "ConfirmationInterpretation",
    "CurrentTurnContext",
    "QuotationPDFGenerator",
    "QuotationService",
]
