"""Validated commercial rules used by the deterministic quotation engine."""

from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
QuotationPrefix = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$",
    ),
]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
NonBlankTerm = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class QuotationRules(BaseModel):
    """Authoritative quotation settings loaded from static JSON.

    Every commercial setting is required. The frozen model and tuple of terms
    ensure the repository can safely share one read-only instance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: CurrencyCode
    quotation_prefix: QuotationPrefix
    validity_days: PositiveStrictInt
    default_discount_percent: Decimal
    tax_percent: Decimal
    terms: tuple[NonBlankTerm, ...]

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        """Normalize a supplied three-letter currency code before validation."""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("default_discount_percent", "tax_percent", mode="before")
    @classmethod
    def reject_inexact_percentage_inputs(cls, value: object, info: ValidationInfo) -> object:
        """Reject binary floats and booleans before Decimal conversion."""
        if isinstance(value, (bool, float)):
            raise ValueError(f"{info.field_name} must be an exact decimal value")
        return value

    @field_validator("default_discount_percent", "tax_percent")
    @classmethod
    def require_finite_nonnegative_percentages(
        cls, value: Decimal, info: ValidationInfo
    ) -> Decimal:
        """Require finite percentages and reject negative policy values."""
        if not value.is_finite():
            raise ValueError(f"{info.field_name} must be finite")
        if value < 0:
            raise ValueError(f"{info.field_name} must be nonnegative")
        return value

    @field_validator("default_discount_percent")
    @classmethod
    def cap_default_discount(cls, value: Decimal) -> Decimal:
        """Prevent a configured default discount above 100 percent."""
        if value > 100:
            raise ValueError("default_discount_percent must not exceed 100")
        return value
