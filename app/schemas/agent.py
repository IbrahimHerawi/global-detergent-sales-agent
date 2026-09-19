"""Internal result schemas for one asynchronous AI-agent turn."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.quotation import GeneratedQuotation
from app.schemas.session import ConversationSession


class AgentModel(BaseModel):
    """Closed model behavior for agent orchestration results."""

    model_config = ConfigDict(extra="forbid")


class PreviewDeliveryMetadata(AgentModel):
    """Internal binding needed to mark a returned preview as delivered."""

    fingerprint: str
    originating_turn_id: str


class AgentResult(AgentModel):
    """Complete result of one customer turn.

    Generated quotation objects and preview bindings are application state.
    The model sees only explicit tool outputs produced during orchestration.
    """

    response_text: str
    updated_session: ConversationSession
    generated_quote: GeneratedQuotation | None = None
    prepared_preview: PreviewDeliveryMetadata | None = Field(default=None, exclude=True)

    @property
    def preview_delivery_metadata(self) -> PreviewDeliveryMetadata | None:
        """Descriptive alias for transport orchestration."""
        return self.prepared_preview


__all__ = ["AgentResult", "PreviewDeliveryMetadata"]
