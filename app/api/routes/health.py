"""Provider-free liveness and Redis-backed readiness routes."""

from __future__ import annotations

from typing import Annotated, Final, TypedDict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.api.dependencies import ApplicationResources

SERVICE_NAME: Final = "gdf-sales-agent"

router = APIRouter(tags=["operations"])


class HealthResponse(TypedDict):
    """Stable, secret-free health response."""

    status: str
    service: str


@router.get("/health")
async def liveness() -> HealthResponse:
    """Report process liveness without accessing any external dependency."""
    return {"status": "ok", "service": SERVICE_NAME}


@router.get(
    "/health/ready",
    responses={503: {"description": "Required resources are unavailable"}},
)
async def readiness(
    resources: Annotated[ApplicationResources | None, Depends(get_readiness_resources)],
) -> JSONResponse:
    """Report readiness after local initialization and one Redis ping."""
    if resources is None or not _local_resources_ready(resources):
        return _readiness_response(ready=False)
    try:
        connected = await resources.redis.ping()
    except (RedisError, TimeoutError):
        return _readiness_response(ready=False)
    return _readiness_response(ready=connected is True)


def get_readiness_resources(request: Request) -> ApplicationResources | None:
    """Return initialized resources, or ``None`` while the app is not ready."""
    resources = getattr(request.app.state, "resources", None)
    return resources if isinstance(resources, ApplicationResources) else None


def _local_resources_ready(resources: ApplicationResources | None) -> bool:
    """Verify that every required local resource survived initialization."""
    return resources is not None and not resources._closed and all(
        resource is not None
        for resource in (
            resources.company_repository,
            resources.product_repository,
            resources.quotation_rules_repository,
            resources.quotation_storage,
            resources.company_service,
            resources.product_service,
            resources.quotation_service,
            resources.pdf_service,
            resources.session_service,
            resources.tool_executor,
            resources.conversation_service,
        )
    )


def _readiness_response(*, ready: bool) -> JSONResponse:
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "service": SERVICE_NAME,
        },
    )


__all__ = ["SERVICE_NAME", "get_readiness_resources", "router"]
