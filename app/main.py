"""ASGI composition root for the Global Detergent Factory AI Sales Agent."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Final, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.api.dependencies import ApplicationResources, build_application_resources
from app.api.routes import health_router
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    TECHNICAL_FALLBACK_MESSAGE,
    ApplicationError,
    BusinessError,
    CustomerInformationRequiredError,
    EmptyQuoteError,
    InvalidConversationStateError,
    ProductNotFoundError,
    QuoteNotConfirmedError,
    StalePreviewError,
    TechnicalError,
)
from app.core.logging import bind_log_context, configure_logging, reset_log_context

APP_TITLE: Final = "Global Detergent Factory AI Sales Agent"
APP_VERSION: Final = "0.1.0"
REQUEST_ID_HEADER: Final = "X-Request-ID"
_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_logger = logging.getLogger(__name__)

SettingsProvider = Callable[[], Settings]
ResourceFactory = Callable[[Settings], Awaitable[ApplicationResources]]


@asynccontextmanager
async def application_lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate startup inputs, publish resources, and close them on shutdown."""
    configure_logging()
    settings_provider = cast(
        SettingsProvider,
        getattr(application.state, "settings_provider", get_settings),
    )
    resource_factory = cast(
        ResourceFactory,
        getattr(application.state, "resource_factory", build_application_resources),
    )
    settings = settings_provider()
    resources = await resource_factory(settings)
    application.state.resources = resources
    _logger.info("application_started")
    try:
        yield
    finally:
        application.state.resources = None
        await resources.aclose()
        _logger.info("application_stopped")


app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    lifespan=application_lifespan,
)
app.state.settings_provider = get_settings
app.state.resource_factory = build_application_resources
app.include_router(health_router)


@app.middleware("http")
async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind one safe correlation ID and include it in every HTTP response."""
    request_id = _request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    token = bind_log_context(request_id=request_id)
    started = perf_counter()
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        _logger.info(
            "request_completed",
            extra={
                "status": str(response.status_code),
                "duration": perf_counter() - started,
            },
        )
        return response
    finally:
        reset_log_context(token)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request,
    _error: RequestValidationError,
) -> JSONResponse:
    return _error_response(
        request,
        status_code=422,
        code="invalid_request",
        message="Request validation failed.",
    )


@app.exception_handler(ProductNotFoundError)
async def product_not_found_error_handler(
    request: Request,
    error: ProductNotFoundError,
) -> JSONResponse:
    return _application_error_response(request, error, status_code=404)


@app.exception_handler(InvalidConversationStateError)
@app.exception_handler(QuoteNotConfirmedError)
@app.exception_handler(StalePreviewError)
@app.exception_handler(EmptyQuoteError)
@app.exception_handler(CustomerInformationRequiredError)
async def quotation_state_error_handler(
    request: Request,
    error: BusinessError,
) -> JSONResponse:
    return _application_error_response(request, error, status_code=409)


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, error: BusinessError) -> JSONResponse:
    return _application_error_response(request, error, status_code=422)


@app.exception_handler(TechnicalError)
async def temporary_dependency_error_handler(
    request: Request,
    error: TechnicalError,
) -> JSONResponse:
    _logger.error(
        "temporary_dependency_failure",
        extra={"error_code": error.code},
        exc_info=error,
    )
    return _application_error_response(request, error, status_code=503)


@app.exception_handler(ApplicationError)
async def application_error_handler(request: Request, error: ApplicationError) -> JSONResponse:
    _logger.error(
        "unmapped_application_failure",
        extra={"error_code": error.code},
        exc_info=error,
    )
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        message=TECHNICAL_FALLBACK_MESSAGE,
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, error: HTTPException) -> JSONResponse:
    message = (
        "The requested resource was not found." if error.status_code == 404 else "Request failed."
    )
    return _error_response(
        request,
        status_code=error.status_code,
        code="http_error",
        message=message,
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
    _logger.error(
        "unexpected_application_failure",
        extra={"error_code": type(error).__name__},
    )
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        message=TECHNICAL_FALLBACK_MESSAGE,
    )


def _application_error_response(
    request: Request,
    error: ApplicationError,
    *,
    status_code: int,
) -> JSONResponse:
    public = error.to_public_dict()
    return _error_response(
        request,
        status_code=status_code,
        code=public["code"],
        message=public["message"],
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message},
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id} if isinstance(request_id, str) else None,
    )


def _request_id(candidate: str | None) -> str:
    if candidate is not None:
        normalized = candidate.strip()
        if _REQUEST_ID_PATTERN.fullmatch(normalized):
            return normalized
    return uuid4().hex


__all__ = [
    "APP_TITLE",
    "APP_VERSION",
    "REQUEST_ID_HEADER",
    "app",
    "application_lifespan",
]
