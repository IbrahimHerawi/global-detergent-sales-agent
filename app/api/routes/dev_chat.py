"""Alternate development transport for the shared conversation service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.api.dependencies import get_conversation_service
from app.core.config import get_settings
from app.schemas.quotation import QuoteCartItem
from app.schemas.session import ConversationState
from app.services.conversation_service import ConversationService

DevelopmentSessionId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$"),
]

router = APIRouter(prefix="/api/v1/dev", tags=["development-chat"])


class DevChatRequest(BaseModel):
    """Only transport identity and customer text are accepted from callers."""

    model_config = ConfigDict(extra="forbid")

    session_id: DevelopmentSessionId
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class DevChatResponse(BaseModel):
    """Technical Plan development-chat response without internal artifact data."""

    model_config = ConfigDict(extra="forbid")

    response: str
    state: ConversationState
    selected_product_id: str | None
    cart: list[QuoteCartItem]
    generated_quotation_id: str | None


def get_inbound_text_limit() -> int:
    """Expose the configured transport limit as an overridable dependency."""
    return get_settings().max_inbound_text_length


@router.post("/chat", response_model=DevChatResponse)
async def chat(
    request: DevChatRequest,
    conversation_service: Annotated[ConversationService, Depends(get_conversation_service)],
    max_inbound_text_length: Annotated[int, Depends(get_inbound_text_limit)],
) -> DevChatResponse:
    """Process one development turn through the common conversation boundary."""
    if len(request.message) > max_inbound_text_length:
        raise HTTPException(status_code=422, detail="Message exceeds the configured length limit")

    identity = f"dev:{request.session_id}"
    result = await conversation_service.process_message(identity, request.message)
    state = result.state
    preview = result.preview_delivery_metadata
    if preview is not None:
        state = await conversation_service.acknowledge_successful_delivery(identity, preview)

    generated_quote = result.generated_quotation
    return DevChatResponse(
        response=result.response_text,
        state=state,
        selected_product_id=result.selected_product_id,
        cart=list(result.cart),
        generated_quotation_id=(
            generated_quote.quotation_id if generated_quote is not None else None
        ),
    )


__all__ = [
    "DevChatRequest",
    "DevChatResponse",
    "get_inbound_text_limit",
    "router",
]
