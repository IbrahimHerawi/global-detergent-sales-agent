"""Conversation state, message, and Redis-persisted session models."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.schemas.quotation import CustomerInfo, QuoteCartItem
from app.schemas.quotation_rules import QuotationRules

NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]


def utc_now() -> datetime:
    """Return an aware timestamp in UTC for model default factories."""
    return datetime.now(UTC)


def normalize_utc(value: datetime, field_name: str) -> datetime:
    """Reject ambiguous timestamps and normalize aware values to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class SessionModel(BaseModel):
    """Common closed-model behavior for persisted session data."""

    model_config = ConfigDict(extra="forbid")


class ConversationState(StrEnum):
    """Public states in the customer conversation workflow."""

    NEW = "NEW"
    DISCOVERY = "DISCOVERY"
    PRODUCT_DISCUSSION = "PRODUCT_DISCUSSION"
    BUILDING_QUOTE = "BUILDING_QUOTE"
    CUSTOMER_DETAILS = "CUSTOMER_DETAILS"
    QUOTE_REVIEW = "QUOTE_REVIEW"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    QUOTE_GENERATED = "QUOTE_GENERATED"


class ConversationMessage(SessionModel):
    """One bounded-history conversation message."""

    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Store message timestamps unambiguously in UTC."""
        return normalize_utc(value, "timestamp")


class GeneratedQuoteReplayMetadata(SessionModel):
    """Backend-owned information for safely replaying a generated quote.

    This object belongs only to persisted session state. It must not be added
    to any model/tool argument schema.
    """

    quotation_id: str
    quote_fingerprint: str
    confirmation_turn_id: str
    pdf_path: str
    generated_at: datetime = Field(default_factory=utc_now)

    @field_validator("generated_at")
    @classmethod
    def require_utc_generated_at(cls, value: datetime) -> datetime:
        """Store quote-generation timestamps unambiguously in UTC."""
        return normalize_utc(value, "generated_at")


class ConversationSession(SessionModel):
    """Complete temporary conversation and quotation authorization state.

    Revision, fingerprint, delivery, confirmation, and replay fields are
    backend-owned persisted metadata. Agent tool argument models must expose
    only the customer actions they accept, never these authorization fields.
    """

    session_id: UUID = Field(default_factory=uuid4)
    customer_phone: str
    state: ConversationState = ConversationState.NEW
    selected_product_id: str | None = None
    customer: CustomerInfo = Field(default_factory=lambda: CustomerInfo(phone=""))
    cart: list[QuoteCartItem] = Field(default_factory=list)
    quote_confirmed: bool = False
    last_quotation_id: str | None = None
    recent_messages: list[ConversationMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # Backend-owned stale-authorization safeguards.
    quote_revision: NonNegativeStrictInt = 0
    prepared_preview_fingerprint: str | None = None
    preview_delivered: bool = False
    preview_originating_turn_id: str | None = None
    confirmation_turn_id: str | None = None
    last_generated_quote: GeneratedQuoteReplayMetadata | None = None

    @model_validator(mode="before")
    @classmethod
    def initialize_transport_customer(cls, value: object) -> object:
        """Seed new customer data with the transport-owned phone number."""
        if isinstance(value, Mapping) and "customer" not in value and "customer_phone" in value:
            data = dict(value)
            data["customer"] = {"phone": value["customer_phone"]}
            return data
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_utc_session_timestamps(cls, value: datetime, info: ValidationInfo) -> datetime:
        """Store session timestamps unambiguously in UTC."""
        field_name = info.field_name or "timestamp"
        return normalize_utc(value, field_name)

    def quote_fingerprint(
        self,
        *,
        current_prices: Mapping[str, Decimal],
        currency: str,
        quotation_rules: QuotationRules,
    ) -> str:
        """Digest all authoritative inputs that can change a quotation.

        Only prices for products currently in the cart are included. Missing,
        negative, or non-finite prices fail closed instead of yielding a digest
        that could authorize an incomplete preview.
        """
        product_ids = sorted({item.product_id for item in self.cart})
        serialized_prices: dict[str, str] = {}
        for product_id in product_ids:
            try:
                price = current_prices[product_id]
            except KeyError as error:
                raise ValueError(f"missing current price for product {product_id!r}") from error
            if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
                raise ValueError(f"invalid current price for product {product_id!r}")
            serialized_prices[product_id] = str(price)

        fingerprint_input = {
            "customer": self.customer.model_dump(mode="json"),
            "cart": [item.model_dump(mode="json") for item in self.cart],
            "current_prices": serialized_prices,
            "currency": currency,
            "quotation_rules": quotation_rules.model_dump(mode="json"),
        }
        canonical_json = json.dumps(
            fingerprint_input,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


__all__ = [
    "ConversationMessage",
    "ConversationSession",
    "ConversationState",
    "GeneratedQuoteReplayMetadata",
]
