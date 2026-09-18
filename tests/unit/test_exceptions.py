"""Tests for safe application exception boundaries."""

import json

import pytest
from app.core.exceptions import (
    TECHNICAL_FALLBACK_MESSAGE,
    AgentExecutionError,
    BusinessError,
    CustomerInformationRequiredError,
    EmptyQuoteError,
    InvalidConversationStateError,
    InvalidQuantityError,
    InvalidStaticDataError,
    ProductNotFoundError,
    QuotationGenerationError,
    QuoteNotConfirmedError,
    SessionStorageUnavailableError,
    StalePreviewError,
    TechnicalError,
    ToolExecutionError,
    ToolNotFoundError,
    WhatsAppAPIError,
)

BUSINESS_ERRORS = (
    (ProductNotFoundError, "product_not_found"),
    (InvalidQuantityError, "invalid_quantity"),
    (EmptyQuoteError, "empty_quote"),
    (CustomerInformationRequiredError, "customer_information_required"),
    (QuoteNotConfirmedError, "quote_not_confirmed"),
    (InvalidConversationStateError, "invalid_conversation_state"),
    (StalePreviewError, "stale_preview"),
)

TECHNICAL_ERRORS = (
    QuotationGenerationError,
    WhatsAppAPIError,
    AgentExecutionError,
    ToolExecutionError,
    ToolNotFoundError,
    InvalidStaticDataError,
    SessionStorageUnavailableError,
)


@pytest.mark.parametrize(("error_type", "expected_code"), BUSINESS_ERRORS)
def test_business_errors_have_stable_serializable_public_payloads(
    error_type: type[BusinessError],
    expected_code: str,
) -> None:
    error = error_type("internal product or conversation context")

    payload = error.to_public_dict()

    assert payload == {"code": expected_code, "message": error.public_message}
    assert json.loads(json.dumps(payload)) == payload
    assert error.code == expected_code
    assert str(error) == error.public_message
    assert "internal product" not in json.dumps(payload)


@pytest.mark.parametrize("error_type", TECHNICAL_ERRORS)
def test_technical_errors_retain_causes_but_publish_only_the_fallback(
    error_type: type[TechnicalError],
) -> None:
    secret_detail = (
        "Bearer access-token; headers={'Authorization': 'secret'}; "
        "conversation=private; path=/app/storage/quotes/private.pdf"
    )
    cause = RuntimeError("upstream response contained sensitive diagnostics")

    error = error_type(secret_detail, cause=cause)
    rendered_payload = json.dumps(error.to_public_dict())

    assert error.diagnostic_detail == secret_detail
    assert error.cause is cause
    assert error.__cause__ is cause
    assert error.to_public_dict() == {
        "code": "technical_error",
        "message": TECHNICAL_FALLBACK_MESSAGE,
    }
    assert str(error) == TECHNICAL_FALLBACK_MESSAGE
    assert secret_detail not in rendered_payload
    assert str(cause) not in rendered_payload


def test_internal_technical_codes_remain_distinct_for_diagnostics() -> None:
    codes = {error_type.code for error_type in TECHNICAL_ERRORS}

    assert len(codes) == len(TECHNICAL_ERRORS)
    assert "technical_error" not in codes
