"""Safe, JSON-compatible company information derived from the repository."""

from __future__ import annotations

from typing import TypedDict

from app.repositories.company_repository import CompanyRepository
from app.schemas.company import CompanyContact


class CompanyContactOutput(TypedDict):
    """Stable JSON-compatible contact output."""

    phone: str | None
    email: str | None
    website: str | None
    address: str | None


class CompanyInformationOutput(TypedDict):
    """Stable JSON-compatible full company output."""

    name: str
    short_name: str
    description: str
    industries_served: list[str]
    product_categories: list[str]
    currency: str
    contact: CompanyContactOutput


class CompanySummaryOutput(TypedDict):
    """Stable JSON-compatible company summary output."""

    name: str
    short_name: str
    description: str


class ProductCategoriesOutput(TypedDict):
    """Stable JSON-compatible product-category output."""

    categories: list[str]


class CompanyService:
    """Expose stable customer-facing company data without deriving missing facts."""

    __slots__ = ("_repository",)

    def __init__(self, repository: CompanyRepository | None = None) -> None:
        self._repository = repository if repository is not None else CompanyRepository()

    def get_company_information(self) -> CompanyInformationOutput:
        """Return every approved company-information field with stable keys."""
        company = self._repository.get_company()
        return {
            "name": company.name,
            "short_name": company.short_name,
            "description": company.description,
            "industries_served": list(company.industries_served),
            "product_categories": list(company.product_categories),
            "currency": company.currency,
            "contact": _contact_information(company.contact),
        }

    def get_company_summary(self) -> CompanySummaryOutput:
        """Return the stored company identity and description only."""
        company = self._repository.get_company()
        return {
            "name": company.name,
            "short_name": company.short_name,
            "description": company.description,
        }

    def list_product_categories(self) -> ProductCategoriesOutput:
        """Return stored company-level product categories under a stable key."""
        company = self._repository.get_company()
        return {"categories": list(company.product_categories)}


def _contact_information(contact: CompanyContact) -> CompanyContactOutput:
    """Preserve every stored contact value, including explicit unknowns."""
    return {
        "phone": contact.phone,
        "email": contact.email,
        "website": contact.website,
        "address": contact.address,
    }
