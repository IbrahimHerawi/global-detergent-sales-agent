"""Tests for typed application settings."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import (
    Settings,
    clear_settings_cache,
    get_settings,
    override_settings,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's environment and settings cache out of these tests."""
    names = Settings.model_fields
    for name in names:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(Path(__file__).parent)
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_defaults_use_container_safe_values() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.session_ttl_seconds == 86_400
    assert settings.openai_timeout_seconds == 45
    assert settings.whatsapp_timeout_seconds == 20
    assert settings.recent_message_limit == 20
    assert settings.max_tool_iterations == 8
    assert settings.message_dedup_ttl_seconds == 604_800
    assert settings.max_inbound_text_length == 4_096
    assert settings.max_webhook_body_size == 1_048_576
    assert settings.company_data_path == Path("/app/app/data/company.json")
    assert settings.product_data_path == Path("/app/app/data/products.json")
    assert settings.quote_rules_path == Path("/app/app/data/quotation_rules.json")
    assert settings.quote_storage_path == Path("/app/storage/quotes")
    assert settings.openai_model is None


def test_environment_overrides_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMO_DATA_MODE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6380/2")
    monkeypatch.setenv("RECENT_MESSAGE_LIMIT", "7")
    monkeypatch.setenv("MAX_INBOUND_TEXT_LENGTH", "1024")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v24.0")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.demo_data_mode is True
    assert settings.redis_url == "redis://cache:6380/2"
    assert settings.recent_message_limit == 7
    assert settings.max_inbound_text_length == 1_024
    assert settings.meta_graph_api_version == "v24.0"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_ttl_seconds", 0),
        ("message_dedup_ttl_seconds", -1),
        ("openai_timeout_seconds", 0),
        ("whatsapp_timeout_seconds", -0.1),
        ("recent_message_limit", 0),
        ("max_tool_iterations", -1),
        ("max_inbound_text_length", 0),
        ("max_webhook_body_size", -1),
    ],
)
def test_positive_values_are_enforced(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


@pytest.mark.parametrize("app_env", ["staging", "prod", "DEVELOPMENT"])
def test_unknown_environment_is_rejected(app_env: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"app_env": app_env})


@pytest.mark.parametrize("version", ["24.0", "v24", "latest", "v0.1"])
def test_invalid_meta_graph_version_is_rejected(version: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"meta_graph_api_version": version})


def test_ai_requires_explicit_model_and_api_key_when_enabled() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        Settings.model_validate({"ai_enabled": True})

    settings = Settings.model_validate(
        {
            "ai_enabled": True,
            "openai_api_key": "mock-key",
            "openai_model": "mock-model",
        }
    )
    assert settings.openai_model == "mock-model"


def test_ai_rejects_example_placeholders_when_enabled() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        Settings.model_validate(
            {
                "ai_enabled": True,
                "openai_api_key": "<openai-api-key>",
                "openai_model": "<model-available-to-your-openai-project>",
            }
        )


def test_secret_values_are_masked_in_representations() -> None:
    secrets = {
        "openai_api_key": "openai-super-secret",
        "whatsapp_access_token": "meta-access-super-secret",
        "whatsapp_verify_token": "verify-super-secret",
        "meta_app_secret": "signature-super-secret",
    }
    settings = Settings.model_validate(secrets)

    rendered = f"{settings!r}\n{settings.model_dump_json()}"
    assert all(secret not in rendered for secret in secrets.values())
    assert isinstance(settings.openai_api_key, SecretStr)


def test_env_example_uses_placeholders_for_credentials_and_model() -> None:
    example_path = Path(__file__).parents[2] / ".env.example"
    values = dict(
        line.split("=", maxsplit=1)
        for line in example_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )

    placeholder_names = {
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_VERIFY_TOKEN",
        "META_APP_SECRET",
    }
    for name in placeholder_names:
        assert values[name].startswith("<") and values[name].endswith(">")


def test_production_startup_validation_rejects_missing_integrations() -> None:
    settings = Settings(_env_file=None, app_env="production")

    with pytest.raises(ValueError, match="Production integration configuration is incomplete"):
        settings.validate_production_integrations()


def test_production_startup_validation_accepts_mocked_credentials() -> None:
    settings = Settings.model_validate(
        {
            "app_env": "production",
            "ai_enabled": True,
            "openai_api_key": "mock-openai-key",
            "openai_model": "mock-model",
            "whatsapp_access_token": "mock-access-token",
            "whatsapp_phone_number_id": "mock-phone-number-id",
            "whatsapp_verify_token": "mock-verify-token",
            "meta_app_secret": "mock-app-secret",
        }
    )

    settings.validate_production_integrations()


def test_settings_accessor_is_cached_and_context_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = get_settings()
    monkeypatch.setenv("APP_NAME", "Changed after caching")

    assert get_settings() is first

    replacement = Settings.model_validate({"app_name": "Test override"})
    with override_settings(replacement):
        assert get_settings() is replacement

    assert get_settings() is first
    clear_settings_cache()
    assert get_settings().app_name == "Changed after caching"
