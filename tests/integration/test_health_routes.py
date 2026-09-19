"""HTTP coverage for provider-free liveness and Redis readiness."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest
from app.api.dependencies import ApplicationResources
from app.api.routes.health import get_readiness_resources
from app.main import app
from redis.exceptions import ConnectionError as RedisConnectionError


class StubRedis:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.ping_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        if not self.available:
            raise RedisConnectionError("redis://user:secret@unavailable/0")
        return True


class ForbiddenProviderClient:
    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError("health checks must not access provider clients")


class StubResources:
    def __init__(self, redis: StubRedis, *, closed: bool = False) -> None:
        self.redis = redis
        self._closed = closed
        self.openai_client = ForbiddenProviderClient()
        self.http_client = ForbiddenProviderClient()
        self.company_repository = object()
        self.product_repository = object()
        self.quotation_rules_repository = object()
        self.quotation_storage = object()
        self.company_service = object()
        self.product_service = object()
        self.quotation_service = object()
        self.pdf_service = object()
        self.session_service = object()
        self.tool_executor = object()
        self.conversation_service = object()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    original_overrides = dict(app.dependency_overrides)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as instance:
            yield instance
    finally:
        app.dependency_overrides = original_overrides


async def test_liveness_is_exact_and_does_not_resolve_any_dependency(
    client: httpx.AsyncClient,
) -> None:
    def forbidden_dependency() -> ApplicationResources:
        raise AssertionError("liveness must not resolve application resources")

    app.dependency_overrides[get_readiness_resources] = forbidden_dependency

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "gdf-sales-agent"}


async def test_readiness_checks_only_initialized_local_resources_and_redis(
    client: httpx.AsyncClient,
) -> None:
    redis = StubRedis()
    resources = cast(ApplicationResources, StubResources(redis))
    app.dependency_overrides[get_readiness_resources] = lambda: resources

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": "gdf-sales-agent"}
    assert redis.ping_calls == 1


async def test_readiness_returns_secret_free_503_when_redis_is_unavailable(
    client: httpx.AsyncClient,
) -> None:
    redis = StubRedis(available=False)
    resources = cast(ApplicationResources, StubResources(redis))
    app.dependency_overrides[get_readiness_resources] = lambda: resources

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "service": "gdf-sales-agent"}
    assert redis.ping_calls == 1
    assert "secret" not in response.text
    assert "redis" not in response.text.casefold()


async def test_readiness_returns_503_without_ping_after_resources_close(
    client: httpx.AsyncClient,
) -> None:
    redis = StubRedis()
    resources = cast(ApplicationResources, StubResources(redis, closed=True))
    app.dependency_overrides[get_readiness_resources] = lambda: resources

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "service": "gdf-sales-agent"}
    assert redis.ping_calls == 0


async def test_readiness_returns_503_before_local_resources_are_initialized(
    client: httpx.AsyncClient,
) -> None:
    app.state.resources = None

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "service": "gdf-sales-agent"}
