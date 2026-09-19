"""Closed request models for internal development-only API routes."""

from typing import Annotated

from pydantic import StringConstraints

from app.schemas.quotation import CustomerInfo, QuotationModel, QuoteCartItem

DevelopmentSessionId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$"),
]


class QuotationPreviewRequest(QuotationModel):
    """Unpriced inputs for an isolated quotation preview."""

    customer: CustomerInfo
    items: list[QuoteCartItem]


class QuotationGenerationRequest(QuotationModel):
    """Reference an existing development session by transport identity only."""

    session_id: DevelopmentSessionId


__all__ = [
    "DevelopmentSessionId",
    "QuotationGenerationRequest",
    "QuotationPreviewRequest",
]
