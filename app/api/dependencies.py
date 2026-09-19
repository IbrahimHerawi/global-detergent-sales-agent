"""Application resource construction and FastAPI dependency providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import cast

import httpx
from fastapi import Request
from openai import AsyncOpenAI
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.agent.agent import AIAgent
from app.agent.tool_executor import ToolExecutor
from app.core.config import Settings
from app.core.exceptions import InvalidStaticDataError, SessionStorageUnavailableError
from app.repositories.company_repository import CompanyRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.services.company_service import CompanyService
from app.services.conversation_service import ConversationService
from app.services.pdf_service import PDFService
from app.services.product_service import ProductService
from app.services.quotation_service import QuotationService
from app.services.quotation_storage import LocalQuotationStorage
from app.services.session_service import SessionService

_DEVELOPMENT_MANIFEST_NAME = "development_data_manifest.json"


@dataclass(slots=True)
class ApplicationResources:
    """Reusable infrastructure and service graph owned by one app lifespan."""

    settings: Settings
    redis: Redis
    http_client: httpx.AsyncClient
    openai_client: AsyncOpenAI | None
    company_repository: CompanyRepository
    product_repository: ProductRepository
    quotation_rules_repository: QuotationRulesRepository
    quotation_storage: LocalQuotationStorage
    company_service: CompanyService
    product_service: ProductService
    quotation_service: QuotationService
    pdf_service: PDFService
    session_service: SessionService
    tool_executor: ToolExecutor
    agent: AIAgent | None
    conversation_service: ConversationService
    _closed: bool = False

    async def aclose(self) -> None:
        """Close network clients exactly once at application shutdown."""
        if self._closed:
            return
        try:
            await _close_clients(self.redis, self.http_client, self.openai_client)
        finally:
            self._closed = True


async def build_application_resources(settings: Settings) -> ApplicationResources:
    """Validate configuration/data/infrastructure and build the service graph."""
    settings.validate_production_integrations()

    company_repository = CompanyRepository(settings.company_data_path)
    product_repository = ProductRepository(settings.product_data_path)
    quotation_rules_repository = QuotationRulesRepository(settings.quote_rules_path)
    _validate_business_data(
        settings,
        company_repository,
        product_repository,
        quotation_rules_repository,
    )
    quotation_storage = LocalQuotationStorage(root=settings.quote_storage_path)

    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    http_client = httpx.AsyncClient(timeout=settings.whatsapp_timeout_seconds)
    openai_client: AsyncOpenAI | None = None
    try:
        try:
            connected = await redis_client.ping()
        except RedisError as error:
            raise SessionStorageUnavailableError(
                f"Redis startup connectivity check failed: {type(error).__name__}",
                cause=error,
            ) from error
        if connected is not True:
            raise SessionStorageUnavailableError("Redis startup connectivity check was rejected")

        if settings.ai_enabled:
            api_key = settings.openai_api_key
            if api_key is None:  # guarded by settings validation; retain a fail-closed boundary
                raise ValueError("OPENAI_API_KEY is required when AI is enabled")
            openai_client = AsyncOpenAI(
                api_key=api_key.get_secret_value(),
                timeout=settings.openai_timeout_seconds,
            )

        company_service = CompanyService(company_repository)
        product_service = ProductService(product_repository)
        pdf_service = PDFService(
            quotation_storage,
            company_repository=company_repository,
        )
        quotation_service = QuotationService(
            product_repository,
            quotation_rules_repository,
            pdf_service=pdf_service,
        )
        session_service = SessionService(redis_client, settings=settings)
        tool_executor = ToolExecutor(
            company_service,
            product_service,
            quotation_service,
        )
        agent = (
            AIAgent(
                settings=settings,
                client=openai_client,
                tool_executor=tool_executor,
            )
            if openai_client is not None
            else None
        )
        conversation_service = ConversationService(
            redis_client,
            settings=settings,
            session_service=session_service,
            agent=agent,
            quotation_service=quotation_service,
        )
        return ApplicationResources(
            settings=settings,
            redis=redis_client,
            http_client=http_client,
            openai_client=openai_client,
            company_repository=company_repository,
            product_repository=product_repository,
            quotation_rules_repository=quotation_rules_repository,
            quotation_storage=quotation_storage,
            company_service=company_service,
            product_service=product_service,
            quotation_service=quotation_service,
            pdf_service=pdf_service,
            session_service=session_service,
            tool_executor=tool_executor,
            agent=agent,
            conversation_service=conversation_service,
        )
    except BaseException:
        await _close_clients(redis_client, http_client, openai_client)
        raise


def get_application_resources(request: Request) -> ApplicationResources:
    """Return lifespan-owned resources; FastAPI can override this in tests."""
    resources = getattr(request.app.state, "resources", None)
    if not isinstance(resources, ApplicationResources):
        raise RuntimeError("Application resources are unavailable outside the app lifespan")
    return resources


def get_company_service(request: Request) -> CompanyService:
    return get_application_resources(request).company_service


def get_product_service(request: Request) -> ProductService:
    return get_application_resources(request).product_service


def get_quotation_service(request: Request) -> QuotationService:
    return get_application_resources(request).quotation_service


def get_session_service(request: Request) -> SessionService:
    return get_application_resources(request).session_service


def get_conversation_service(request: Request) -> ConversationService:
    return get_application_resources(request).conversation_service


def get_redis_client(request: Request) -> Redis:
    return get_application_resources(request).redis


def get_http_client(request: Request) -> httpx.AsyncClient:
    return get_application_resources(request).http_client


def get_openai_client(request: Request) -> AsyncOpenAI | None:
    return get_application_resources(request).openai_client


def _validate_business_data(
    settings: Settings,
    company_repository: CompanyRepository,
    product_repository: ProductRepository,
    quotation_rules_repository: QuotationRulesRepository,
) -> None:
    company = company_repository.get_company()
    products = product_repository.get_all()
    rules = quotation_rules_repository.get_rules()

    if not products:
        raise InvalidStaticDataError("Product catalog must contain at least one product")
    if not any(product.active for product in products):
        raise InvalidStaticDataError("Product catalog must contain at least one active product")

    inconsistent_products = sorted(
        product.id for product in products if product.price.currency != rules.currency
    )
    if company.currency != rules.currency or inconsistent_products:
        detail = (
            f"Currency consistency check failed: company={company.currency}, "
            f"quotation_rules={rules.currency}"
        )
        if inconsistent_products:
            detail += f", product_ids={','.join(inconsistent_products)}"
        raise InvalidStaticDataError(detail)

    _enforce_production_data_policy(settings)


def _enforce_production_data_policy(settings: Settings) -> None:
    if settings.app_env != "production":
        return
    if settings.demo_data_mode:
        raise InvalidStaticDataError("Demo data mode is forbidden in production")

    manifest_path = _development_manifest_path(settings)
    if not manifest_path.exists():
        return
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, JSONDecodeError) as error:
        raise InvalidStaticDataError(
            f"Unable to validate data provenance manifest: {type(error).__name__}",
            cause=error,
        ) from error
    if not isinstance(raw_manifest, dict):
        raise InvalidStaticDataError("Data provenance manifest must be a JSON object")
    manifest = cast(dict[str, object], raw_manifest)
    if manifest.get("production_approved") is not True:
        raise InvalidStaticDataError(
            "Unverified development business data is forbidden in production"
        )


def _development_manifest_path(settings: Settings) -> Path:
    configured_paths = {
        settings.company_data_path.resolve(strict=False).parent,
        settings.product_data_path.resolve(strict=False).parent,
        settings.quote_rules_path.resolve(strict=False).parent,
    }
    if len(configured_paths) != 1:
        return Path("/__gdf_no_shared_data_manifest__")
    return next(iter(configured_paths)) / _DEVELOPMENT_MANIFEST_NAME


async def _close_clients(
    redis_client: Redis,
    http_client: httpx.AsyncClient,
    openai_client: AsyncOpenAI | None,
) -> None:
    """Attempt every client close even when an earlier client close fails."""
    try:
        if openai_client is not None:
            await openai_client.close()
    finally:
        try:
            await http_client.aclose()
        finally:
            await redis_client.aclose()


__all__ = [
    "ApplicationResources",
    "build_application_resources",
    "get_application_resources",
    "get_company_service",
    "get_conversation_service",
    "get_http_client",
    "get_openai_client",
    "get_product_service",
    "get_quotation_service",
    "get_redis_client",
    "get_session_service",
]
