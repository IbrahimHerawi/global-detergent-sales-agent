"""Typed application exceptions with explicitly safe public representations."""

from __future__ import annotations

from typing import ClassVar

TECHNICAL_FALLBACK_MESSAGE = (
    "Sorry, I'm temporarily unable to process your request. Please try again."
)


class ApplicationError(Exception):
    """Base exception that keeps public output separate from diagnostics."""

    code: ClassVar[str] = "application_error"
    public_message: ClassVar[str] = TECHNICAL_FALLBACK_MESSAGE
    _public_code: ClassVar[str | None] = "technical_error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(self.public_message)
        self.diagnostic_detail = detail
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    @property
    def public_code(self) -> str:
        """Return the stable code that may be shown outside the application."""
        return self._public_code or self.code

    def to_public_dict(self) -> dict[str, str]:
        """Return the complete allowlisted, JSON-serializable public payload."""
        return {"code": self.public_code, "message": self.public_message}


class BusinessError(ApplicationError):
    """Expected business rejection whose code and message are customer-safe."""

    _public_code = None


class TechnicalError(ApplicationError):
    """Operational failure that always uses the common public fallback."""


class ProductNotFoundError(BusinessError):
    code = "product_not_found"
    public_message = "The requested product was not found."


class InvalidQuantityError(BusinessError):
    code = "invalid_quantity"
    public_message = "Quantity must be a positive whole number."


class EmptyQuoteError(BusinessError):
    code = "empty_quote"
    public_message = "The quotation must contain at least one item."


class CustomerInformationRequiredError(BusinessError):
    code = "customer_information_required"
    public_message = "Customer information is required before creating a quotation."


class QuoteNotConfirmedError(BusinessError):
    code = "quote_not_confirmed"
    public_message = "Please confirm the current quotation before it is generated."


class InvalidConversationStateError(BusinessError):
    code = "invalid_conversation_state"
    public_message = "That action is not available at this stage of the conversation."


class StalePreviewError(BusinessError):
    code = "stale_preview"
    public_message = "The quotation has changed. Please review and confirm the latest preview."


class QuotationGenerationError(TechnicalError):
    code = "quotation_generation_failed"


class WhatsAppAPIError(TechnicalError):
    code = "whatsapp_api_error"


class AgentExecutionError(TechnicalError):
    code = "agent_execution_failed"


class ToolExecutionError(TechnicalError):
    code = "tool_execution_failed"


class ToolNotFoundError(TechnicalError):
    code = "tool_not_found"


class InvalidStaticDataError(TechnicalError):
    code = "invalid_static_data"


class SessionStorageUnavailableError(TechnicalError):
    code = "session_storage_unavailable"


__all__ = [
    "TECHNICAL_FALLBACK_MESSAGE",
    "AgentExecutionError",
    "ApplicationError",
    "BusinessError",
    "CustomerInformationRequiredError",
    "EmptyQuoteError",
    "InvalidConversationStateError",
    "InvalidQuantityError",
    "InvalidStaticDataError",
    "ProductNotFoundError",
    "QuotationGenerationError",
    "QuoteNotConfirmedError",
    "SessionStorageUnavailableError",
    "StalePreviewError",
    "TechnicalError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "WhatsAppAPIError",
]
