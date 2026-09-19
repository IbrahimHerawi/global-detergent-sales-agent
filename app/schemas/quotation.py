"""Validated customer, cart, and quotation data models."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_serializer,
    field_validator,
)

PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]


class QuotationModel(BaseModel):
    """Common closed-model behavior for quotation data."""

    model_config = ConfigDict(extra="forbid")


class CustomerInfo(QuotationModel):
    """Customer details collected over one or more conversation turns.

    Identity completeness is deliberately not enforced here. The quotation
    service decides when the supplied name/company and transport-owned phone
    are sufficient to prepare or generate a quotation.
    """

    name: str | None = None
    company_name: str | None = None
    phone: str
    email: str | None = None
    address: str | None = None
    contact_person: str | None = None
    notes: str | None = None


class QuoteCartItem(QuotationModel):
    """Minimal, non-authoritative cart state stored in a session."""

    product_id: str
    quantity: PositiveStrictInt


class MoneyModel(QuotationModel):
    """Base model for exact monetary values serialized as JSON strings."""

    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def reject_inexact_money_inputs(cls, value: object, info: ValidationInfo) -> object:
        """Reject floats and booleans only when validating a Decimal field."""
        if info.field_name is None:
            return value
        field = cls.model_fields[info.field_name]
        if field.annotation is Decimal and isinstance(value, (bool, float)):
            raise ValueError(f"{info.field_name} must be an exact decimal value")
        return value

    @field_validator("*", check_fields=False)
    @classmethod
    def require_finite_nonnegative_money(cls, value: object, info: ValidationInfo) -> object:
        """Keep every monetary field finite and nonnegative."""
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError(f"{info.field_name} must be finite")
            if value < 0:
                raise ValueError(f"{info.field_name} must be nonnegative")
        return value

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_decimal_as_string(self, value: object) -> object:
        """Emit exact decimal text instead of JSON numbers."""
        if isinstance(value, Decimal):
            return str(value)
        return value


class QuoteLine(MoneyModel):
    """A priced product line resolved from the authoritative catalog."""

    product_id: str
    sku: str
    description: str
    quantity: PositiveStrictInt
    unit_price: Decimal
    subtotal: Decimal


class QuoteTotals(MoneyModel):
    """Deterministically calculated quotation totals."""

    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal
    currency: str


class QuotePreview(QuotationModel):
    """Customer-facing quotation preview before explicit confirmation."""

    customer: CustomerInfo
    items: list[QuoteLine]
    totals: QuoteTotals
    validity_days: PositiveStrictInt
    terms: list[str]


class GeneratedQuotation(QuotePreview):
    """Final quotation metadata plus its internal PDF storage path.

    ``pdf_path`` is private application state, not untrusted model input and
    not part of JSON payloads. Call :meth:`with_pdf_path` when the PDF storage
    service creates the final quotation.
    """

    quotation_id: str
    issued_at: datetime
    valid_until: date
    _pdf_path: str = PrivateAttr()

    @field_validator("issued_at")
    @classmethod
    def require_timezone_aware_issued_at(cls, value: datetime) -> datetime:
        """Reject ambiguous naive timestamps for final quotations."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issued_at must be timezone-aware")
        return value

    @property
    def pdf_path(self) -> str:
        """Return the internal path assigned by trusted PDF storage code."""
        return self._pdf_path

    @classmethod
    def with_pdf_path(cls, *, pdf_path: str, **quotation_data: object) -> Self:
        """Validate quotation data and attach a trusted internal PDF path."""
        quotation = cls.model_validate(quotation_data)
        quotation._pdf_path = pdf_path
        return quotation


__all__ = [
    "CustomerInfo",
    "GeneratedQuotation",
    "QuoteCartItem",
    "QuoteLine",
    "QuotePreview",
    "QuoteTotals",
]
