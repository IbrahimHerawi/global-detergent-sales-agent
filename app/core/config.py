"""Typed application configuration loaded from environment variables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnvironment = Literal["development", "test", "production"]
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
GraphApiVersion = Annotated[str, StringConstraints(pattern=r"^v[1-9]\d*\.\d+$")]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class Settings(BaseSettings):
    """Application settings.

    Integration credentials are optional at configuration-load time so unit tests and
    development utilities can run without live accounts. The eventual application
    lifespan must call :meth:`validate_production_integrations` before serving in
    production.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    app_name: NonEmptyString = "Global Detergent Factory AI Sales Agent"
    app_env: AppEnvironment = "development"
    demo_data_mode: bool = False

    ai_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_model: NonEmptyString | None = None
    openai_timeout_seconds: PositiveFloat = 45
    max_tool_iterations: PositiveInt = 8

    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: NonEmptyString | None = None
    whatsapp_verify_token: SecretStr | None = None
    meta_app_secret: SecretStr | None = None
    meta_graph_api_version: GraphApiVersion = "v23.0"
    whatsapp_timeout_seconds: PositiveFloat = 20

    redis_url: NonEmptyString = "redis://redis:6379/0"
    session_ttl_seconds: PositiveInt = 86_400
    message_dedup_ttl_seconds: PositiveInt = 604_800
    recent_message_limit: PositiveInt = 20

    company_data_path: Path = Path("/app/app/data/company.json")
    product_data_path: Path = Path("/app/app/data/products.json")
    quote_rules_path: Path = Path("/app/app/data/quotation_rules.json")
    quote_storage_path: Path = Path("/app/storage/quotes")

    max_inbound_text_length: PositiveInt = 4_096
    max_webhook_body_size: PositiveInt = 1_048_576

    @model_validator(mode="after")
    def require_ai_configuration_when_enabled(self) -> Self:
        """Require an explicit credential and model without inventing a fallback."""
        missing: list[str] = []
        if self.ai_enabled:
            if not _secret_has_value(self.openai_api_key):
                missing.append("OPENAI_API_KEY")
            if not _string_has_value(self.openai_model):
                missing.append("OPENAI_MODEL")
        if missing:
            detail = ", ".join(missing)
            raise ValueError(f"AI is enabled but required settings are missing: {detail}")
        return self

    def validate_production_integrations(self) -> None:
        """Reject incomplete production integration configuration at startup.

        This is intentionally a separate startup check: settings can be constructed by
        isolated and mocked tests without access to real credentials.
        """
        if self.app_env != "production":
            return

        missing: list[str] = []
        if not self.ai_enabled:
            missing.append("AI_ENABLED=true")
        if not _secret_has_value(self.openai_api_key):
            missing.append("OPENAI_API_KEY")
        if not _string_has_value(self.openai_model):
            missing.append("OPENAI_MODEL")
        if not _secret_has_value(self.whatsapp_access_token):
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not _string_has_value(self.whatsapp_phone_number_id):
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        if not _secret_has_value(self.whatsapp_verify_token):
            missing.append("WHATSAPP_VERIFY_TOKEN")
        if not _secret_has_value(self.meta_app_secret):
            missing.append("META_APP_SECRET")

        if missing:
            raise ValueError(
                "Production integration configuration is incomplete: " + ", ".join(missing)
            )


def _secret_has_value(secret: SecretStr | None) -> bool:
    return secret is not None and _string_has_value(secret.get_secret_value())


def _string_has_value(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip()
    return bool(normalized) and not (normalized.startswith("<") and normalized.endswith(">"))


@lru_cache(maxsize=1)
def _load_settings() -> Settings:
    return Settings()


_settings_override: ContextVar[Settings | None] = ContextVar("settings_override", default=None)


def get_settings() -> Settings:
    """Return the context override or the process-cached environment settings."""
    return _settings_override.get() or _load_settings()


def clear_settings_cache() -> None:
    """Clear environment-derived settings, primarily after tests change the environment."""
    _load_settings.cache_clear()


@contextmanager
def override_settings(settings: Settings) -> Iterator[None]:
    """Temporarily override the settings accessor in the current execution context."""
    token = _settings_override.set(settings)
    try:
        yield
    finally:
        _settings_override.reset(token)
