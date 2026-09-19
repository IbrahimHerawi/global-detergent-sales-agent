"""Tests for validated, cached, read-only company loading."""

import json
from json import JSONDecodeError
from pathlib import Path

import pytest
from app.core.config import Settings, override_settings
from app.core.exceptions import TECHNICAL_FALLBACK_MESSAGE, InvalidStaticDataError
from app.repositories.company_repository import CompanyRepository
from pydantic import ValidationError


def company_payload() -> dict[str, object]:
    """Return a valid company document with nested contact details."""
    return {
        "name": "Global Detergent Factory",
        "short_name": "GDF",
        "description": "Development company profile.",
        "industries_served": ["Facility management"],
        "product_categories": ["Cleaning Products"],
        "currency": "QAR",
        "contact": {
            "phone": None,
            "email": None,
            "website": None,
            "address": None,
        },
    }


def write_company(path: Path, payload: object) -> None:
    """Write a company JSON fixture."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repository_loads_configured_path_once_and_caches(tmp_path: Path) -> None:
    path = tmp_path / "company.json"
    write_company(path, company_payload())
    settings = Settings.model_validate({"company_data_path": path})

    with override_settings(settings):
        repository = CompanyRepository()

    path.write_text("not valid JSON anymore", encoding="utf-8")

    assert repository.get_company().name == "Global Detergent Factory"
    assert repository.get_company().currency == "QAR"


def test_get_company_returns_deep_defensive_copies(tmp_path: Path) -> None:
    path = tmp_path / "company.json"
    write_company(path, company_payload())
    repository = CompanyRepository(path)

    first = repository.get_company()
    first.name = "Changed by caller"
    first.industries_served.append("Caller mutation")
    first.product_categories.clear()
    first.contact.phone = "+974 0000 0000"

    second = repository.get_company()

    assert second is not first
    assert second.name == "Global Detergent Factory"
    assert second.industries_served == ["Facility management"]
    assert second.product_categories == ["Cleaning Products"]
    assert second.contact.phone is None


def test_independent_callers_cannot_mutate_each_others_results(tmp_path: Path) -> None:
    path = tmp_path / "company.json"
    write_company(path, company_payload())
    repository = CompanyRepository(path)

    first = repository.get_company()
    second = repository.get_company()
    first.contact.address = "Caller-only address"

    assert second.contact.address is None
    assert repository.get_company().contact.address is None


def test_repository_has_no_public_write_operations() -> None:
    public_methods = {
        name
        for name in dir(CompanyRepository)
        if not name.startswith("_") and callable(getattr(CompanyRepository, name))
    }

    assert public_methods == {"get_company"}


def test_missing_file_fails_with_predictable_startup_error(tmp_path: Path) -> None:
    path = tmp_path / "missing-company.json"

    with pytest.raises(InvalidStaticDataError) as raised:
        CompanyRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert str(path) in error.diagnostic_detail
    assert "Unable to read company data" in error.diagnostic_detail
    assert isinstance(error.cause, FileNotFoundError)
    assert str(error) == TECHNICAL_FALLBACK_MESSAGE


def test_malformed_json_reports_location_without_document_content(tmp_path: Path) -> None:
    path = tmp_path / "company.json"
    secret_text = "private-token-must-not-leak"
    path.write_text(f'{{"name": "{secret_text}",', encoding="utf-8")

    with pytest.raises(InvalidStaticDataError) as raised:
        CompanyRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "Malformed company JSON" in error.diagnostic_detail
    assert "line 1" in error.diagnostic_detail
    assert "column" in error.diagnostic_detail
    assert secret_text not in error.diagnostic_detail
    assert isinstance(error.cause, JSONDecodeError)


def test_schema_failure_identifies_fields_without_input_values(tmp_path: Path) -> None:
    path = tmp_path / "company.json"
    secret_text = "private-token-must-not-leak"
    payload = company_payload()
    payload["name"] = "   "
    payload["private_api_key"] = secret_text
    write_company(path, payload)

    with pytest.raises(InvalidStaticDataError) as raised:
        CompanyRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "Invalid company data" in error.diagnostic_detail
    assert "name" in error.diagnostic_detail
    assert "private_api_key" in error.diagnostic_detail
    assert secret_text not in error.diagnostic_detail
    assert isinstance(error.cause, ValidationError)
    assert str(error) == TECHNICAL_FALLBACK_MESSAGE


def test_repository_loads_development_company_from_container_path() -> None:
    repository = CompanyRepository()

    company = repository.get_company()

    assert company.name == "Global Detergent Factory"
    assert company.contact.phone is None
