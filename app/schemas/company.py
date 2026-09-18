"""Validated company and company-contact data models."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class CompanyContact(BaseModel):
    """Contact details supplied by the company data source.

    Unknown values deliberately remain ``None``; the model does not derive or
    synthesize contact information.
    """

    model_config = ConfigDict(extra="forbid")

    phone: NonBlankString | None = None
    email: NonBlankString | None = None
    website: NonBlankString | None = None
    address: NonBlankString | None = None


class Company(BaseModel):
    """Authoritative company profile loaded from static company data."""

    model_config = ConfigDict(extra="forbid")

    name: NonBlankString
    short_name: NonBlankString
    description: NonBlankString
    industries_served: list[NonBlankString] = Field(default_factory=list)
    product_categories: list[NonBlankString] = Field(default_factory=list)
    currency: CurrencyCode
    contact: CompanyContact

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        """Normalize a supplied three-letter currency code before validation."""
        if isinstance(value, str):
            return value.strip().upper()
        return value
