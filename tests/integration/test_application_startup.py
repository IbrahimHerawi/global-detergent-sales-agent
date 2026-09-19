"""Composition-root startup, shutdown, dependency, and HTTP mapping checks."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from app.api.dependencies import (
    ApplicationResources,
    get_application_resources,
    get_company_service,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    InvalidConversationStateError,
    InvalidQuantityError,
    InvalidStaticDataError,
    ProductNotFoundError,
    QuotationGenerationError,
    SessionStorageUnavailableError,
)
from app.main import APP_TITLE, APP_VERSION, REQUEST_ID_HEADER, app
from app.services.company_service import CompanyService
from fastapi import Depends
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 15
DATA_ROOT = Path(__file__).resolve().parents[2] / "app" / "data"


def isolated_redis_url() -> str:
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_REDIS_DATABASE}", parts.query, ""))


@pytest.fixture
async def isolated_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(isolated_redis_url(), decode_responses=True)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def data_directory(tmp_path: Path) -> Path:
    destination = tmp_path / "data"
    destination.mkdir()
    for name in (
        "company.json",
        "products.json",
        "quotation_rules.json",
        "development_data_manifest.json",
    ):
        shutil.copyfile(DATA_ROOT / name, destination / name)
    return destination


def startup_settings(
    tmp_path: Path,
    data_directory: Path,
    **updates: object,
) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "redis_url": isolated_redis_url(),
        "company_data_path": data_directory / "company.json",
        "product_data_path": data_directory / "products.json",
        "quote_rules_path": data_directory / "quotation_rules.json",
        "quote_storage_path": tmp_path / "quotes",
    }
    values.update(updates)
    return Settings.model_validate(values)


@pytest.fixture
def configured_app() -> Iterator[None]:
    original_settings_provider = app.state.settings_provider
    original_resource_factory = app.state.resource_factory
    original_overrides = dict(app.dependency_overrides)
    original_routes = list(app.router.routes)
    original_openapi = app.openapi_schema
    try:
        yield
    finally:
        app.state.settings_provider = original_settings_provider
        app.state.resource_factory = original_resource_factory
        app.state.resources = None
        app.dependency_overrides = original_overrides
        app.router.routes[:] = original_routes
        app.openapi_schema = original_openapi


async def test_valid_startup_exposes_docs_health_and_reusable_resources_then_closes_clients(
    isolated_redis: Redis,
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    settings = startup_settings(tmp_path, data_directory)
    app.state.settings_provider = lambda: settings

    async with app.router.lifespan_context(app):
        resources = app.state.resources
        assert isinstance(resources, ApplicationResources)
        assert resources.redis is not isolated_redis
        assert resources.openai_client is None
        assert resources.conversation_service is not None
        assert resources.quotation_storage is not None
        assert await resources.redis.ping() is True

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            docs_response = await client.get("/docs")
            schema_response = await client.get("/openapi.json")
            health_response = await client.get("/health", headers={REQUEST_ID_HEADER: "test-42"})

        assert docs_response.status_code == 200
        assert docs_response.headers["content-type"].startswith("text/html")
        assert schema_response.json()["info"] == {"title": APP_TITLE, "version": APP_VERSION}
        assert "/health" in schema_response.json()["paths"]
        assert health_response.json() == {"status": "ok"}
        assert health_response.headers[REQUEST_ID_HEADER] == "test-42"
        http_client = resources.http_client

    assert app.state.resources is None
    assert http_client.is_closed is True


async def test_ai_client_is_initialized_without_provider_call_and_closed_on_shutdown(
    isolated_redis: Redis,
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    settings = startup_settings(
        tmp_path,
        data_directory,
        ai_enabled=True,
        openai_api_key="sk-test-not-real",
        openai_model="test-model",
    )
    app.state.settings_provider = lambda: settings

    async with app.router.lifespan_context(app):
        resources = app.state.resources
        openai_client = resources.openai_client
        assert openai_client is not None
        assert openai_client.is_closed() is False
        assert resources.agent is not None

    assert openai_client.is_closed() is True


async def test_invalid_static_data_fails_startup_before_resources_are_published(
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    (data_directory / "company.json").write_text('{"name":', encoding="utf-8")
    app.state.settings_provider = lambda: startup_settings(tmp_path, data_directory)

    with pytest.raises(InvalidStaticDataError):
        async with app.router.lifespan_context(app):
            pytest.fail("invalid business data must not enter the lifespan")

    assert getattr(app.state, "resources", None) is None


async def test_currency_inconsistency_fails_startup(
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    products_path = data_directory / "products.json"
    products = json.loads(products_path.read_text(encoding="utf-8"))
    products[0]["price"]["currency"] = "USD"
    products_path.write_text(json.dumps(products), encoding="utf-8")
    app.state.settings_provider = lambda: startup_settings(tmp_path, data_directory)

    with pytest.raises(InvalidStaticDataError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("currency mismatch must not enter the lifespan")

    assert caught.value.diagnostic_detail is not None
    assert "Currency consistency check failed" in caught.value.diagnostic_detail


async def test_unverified_demo_data_is_forbidden_in_production(
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    settings = startup_settings(
        tmp_path,
        data_directory,
        app_env="production",
        ai_enabled=True,
        openai_api_key="sk-test-not-real",
        openai_model="test-model",
        whatsapp_access_token="test-access-token",
        whatsapp_phone_number_id="123456789",
        whatsapp_verify_token="test-verify-token",
        meta_app_secret="test-meta-secret",
    )
    app.state.settings_provider = lambda: settings

    with pytest.raises(InvalidStaticDataError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("unverified production data must not enter the lifespan")

    assert caught.value.diagnostic_detail is not None
    assert "forbidden in production" in caught.value.diagnostic_detail


async def test_unavailable_redis_fails_startup(
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    settings = startup_settings(
        tmp_path,
        data_directory,
        redis_url="redis://redis:1/15",
    )
    app.state.settings_provider = lambda: settings

    with pytest.raises(SessionStorageUnavailableError):
        async with app.router.lifespan_context(app):
            pytest.fail("unavailable Redis must not enter the lifespan")


async def test_unavailable_quotation_storage_fails_startup(
    configured_app: None,
    tmp_path: Path,
    data_directory: Path,
) -> None:
    storage_file = tmp_path / "not-a-directory"
    storage_file.write_text("occupied", encoding="utf-8")
    settings = startup_settings(
        tmp_path,
        data_directory,
        quote_storage_path=storage_file,
    )
    app.state.settings_provider = lambda: settings

    with pytest.raises(QuotationGenerationError):
        async with app.router.lifespan_context(app):
            pytest.fail("unavailable quotation storage must not enter the lifespan")


async def _mapped_error_endpoint(kind: str) -> None:
    errors = {
        "product": ProductNotFoundError("test"),
        "state": InvalidConversationStateError("test"),
        "business": InvalidQuantityError("test"),
        "dependency": SessionStorageUnavailableError("test"),
        "unexpected": RuntimeError("sensitive internal detail"),
    }
    raise errors[kind]


async def _validation_endpoint(quantity: int) -> dict[str, int]:
    return {"quantity": quantity}


async def _dependency_endpoint(
    company_service: Annotated[CompanyService, Depends(get_company_service)],
) -> object:
    return company_service.get_company_summary()


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_code"),
    [
        ("product", 404, "product_not_found"),
        ("state", 409, "invalid_conversation_state"),
        ("business", 422, "invalid_quantity"),
        ("dependency", 503, "technical_error"),
        ("unexpected", 500, "internal_error"),
    ],
)
async def test_safe_http_error_mapping_and_request_ids(
    configured_app: None,
    kind: str,
    expected_status: int,
    expected_code: str,
) -> None:
    app.add_api_route("/_test/error/{kind}", _mapped_error_endpoint)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/_test/error/{kind}",
            headers={REQUEST_ID_HEADER: "correlation-123"},
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert response.json()["request_id"] == "correlation-123"
    assert response.headers[REQUEST_ID_HEADER] == "correlation-123"
    assert "sensitive internal detail" not in response.text


async def test_request_validation_is_422_and_unsafe_request_id_is_not_reflected(
    configured_app: None,
) -> None:
    app.add_api_route("/_test/validation", _validation_endpoint)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/_test/validation?quantity=not-an-integer",
            headers={REQUEST_ID_HEADER: "bad request id"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.headers[REQUEST_ID_HEADER] != "bad request id"
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_fastapi_dependency_override_replaces_lifespan_resource_lookup(
    configured_app: None,
) -> None:
    class StubCompanyService:
        def get_company_summary(self) -> dict[str, str]:
            return {"name": "Override", "short_name": "TEST", "description": "Mocked"}

    app.add_api_route("/_test/dependency", _dependency_endpoint)
    app.dependency_overrides[get_company_service] = StubCompanyService
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/dependency")

    assert response.status_code == 200
    assert response.json()["short_name"] == "TEST"


def test_application_resource_dependency_rejects_use_outside_lifespan() -> None:
    class StubRequest:
        def __init__(self) -> None:
            self.app = app

    app.state.resources = None
    with pytest.raises(RuntimeError, match="outside the app lifespan"):
        get_application_resources(StubRequest())  # type: ignore[arg-type]
