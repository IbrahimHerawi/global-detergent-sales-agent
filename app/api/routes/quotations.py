"""Internal quotation testing endpoints backed by application services."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_quotation_service, get_session_service
from app.schemas.development import QuotationGenerationRequest, QuotationPreviewRequest
from app.schemas.quotation import GeneratedQuotation, QuotePreview
from app.schemas.session import ConversationSession, ConversationState
from app.services.quotation_service import QuotationService
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/v1/quotes", tags=["development-quotations"])


@router.post("/preview", response_model=QuotePreview)
def preview_quotation(
    request: QuotationPreviewRequest,
    quotation_service: Annotated[QuotationService, Depends(get_quotation_service)],
) -> QuotePreview:
    """Calculate a preview in memory without confirmation, storage, or PDF work."""
    session = ConversationSession(
        customer_phone=request.customer.phone,
        customer=request.customer.model_copy(deep=True),
        cart=[item.model_copy(deep=True) for item in request.items],
        state=ConversationState.BUILDING_QUOTE,
    )
    return quotation_service.prepare_quotation(session, "internal-preview")


@router.post("/generate", response_model=GeneratedQuotation)
async def generate_quotation(
    request: QuotationGenerationRequest,
    quotation_service: Annotated[QuotationService, Depends(get_quotation_service)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> GeneratedQuotation:
    """Generate or replay the guarded quote for one persisted development session."""
    session = await session_service.get_session(f"dev:{request.session_id}")
    generated = await quotation_service.generate(session)
    await session_service.save_session(session)
    return generated


__all__ = ["router"]
