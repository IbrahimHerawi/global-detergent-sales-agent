"""Bootstrap checks for the minimal FastAPI entry point."""

from importlib import import_module

import httpx
import pytest


async def test_bootstrap_exposes_framework_docs_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bootstrap app must not initialize integrations or claim business behavior."""
    monkeypatch.setenv("AI_ENABLED", "true")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_VERIFY_TOKEN",
        "META_APP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    module = import_module("app.main")

    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        docs_response = await client.get("/docs")
        schema_response = await client.get("/openapi.json")

    assert docs_response.status_code == 200
    assert docs_response.headers["content-type"].startswith("text/html")
    assert schema_response.status_code == 200

    schema = schema_response.json()
    assert schema["info"] == {
        "title": "Global Detergent Factory AI Sales Agent",
        "version": "0.1.0",
    }
    assert schema["paths"] == {}
