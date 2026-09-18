"""Validated product catalog data models."""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TrimmedString = Annotated[str, StringConstraints(strip_whitespace=True)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Packaging(BaseModel):
    """A product's catalog packaging description."""

    model_config = ConfigDict(extra="forbid")

    size: NonBlankString
    unit: NonBlankString


class Price(BaseModel):
    """An authoritative catalog price represented without binary floats."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    currency: CurrencyCode

    @field_validator("amount", mode="before")
    @classmethod
    def reject_binary_floats_and_booleans(cls, value: object) -> object:
        """Keep monetary input on an exact decimal path."""
        if isinstance(value, (bool, float)):
            raise ValueError("price amount must be an exact decimal value, not a float or boolean")
        return value

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        """Require a finite, nonnegative catalog amount."""
        if not value.is_finite():
            raise ValueError("price amount must be finite")
        if value < 0:
            raise ValueError("price amount must be nonnegative")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        """Normalize a supplied ISO-style currency code before validation."""
        if isinstance(value, str):
            return value.strip().upper()
        return value


class ProductTechnicalDetails(BaseModel):
    """Optional technical facts supplied by the product catalog."""

    model_config = ConfigDict(extra="forbid")

    color: NonBlankString | None = None
    fragrance: NonBlankString | None = None
    ph: NonBlankString | None = None


class Product(BaseModel):
    """Complete authoritative product record loaded from the catalog.

    Optional facts are never inferred from product identity or descriptive
    text. Missing facts retain their explicit ``None`` or empty-list values.
    """

    model_config = ConfigDict(extra="forbid")

    id: NonBlankString
    sku: NonBlankString
    name: NonBlankString
    category: NonBlankString
    short_description: TrimmedString
    description: TrimmedString
    applications: list[NonBlankString] = Field(default_factory=list)
    packaging: Packaging
    price: Price
    instructions: NonBlankString | None = None
    safety_information: NonBlankString | None = None
    technical_details: ProductTechnicalDetails = Field(default_factory=ProductTechnicalDetails)
    active_ingredients: list[NonBlankString] = Field(default_factory=list)
    recommended_surfaces: list[NonBlankString] = Field(default_factory=list)
    contact_time: NonBlankString | None = None
    dilution_ratio: NonBlankString | None = None
    certifications: list[NonBlankString] = Field(default_factory=list)
    approved_claims: list[NonBlankString] = Field(default_factory=list)
    keywords: list[NonBlankString] = Field(default_factory=list)
    active: bool = True

    @model_validator(mode="after")
    def require_active_product_descriptions(self) -> Self:
        """Active products must have both customer-facing descriptions."""
        if self.active and (not self.short_description or not self.description):
            raise ValueError("active products require nonblank short and full descriptions")
        return self
