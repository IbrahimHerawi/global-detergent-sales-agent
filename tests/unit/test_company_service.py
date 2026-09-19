"""Tests for safe company information exposed from repository data."""

import json
from pathlib import Path

from app.repositories.company_repository import CompanyRepository
from app.services.company_service import CompanyService


def company_payload() -> dict[str, object]:
    """Return representative stored company data with unknown contact facts."""
    return {
        "name": "Stored Company Name",
        "short_name": "SCN",
        "description": "Stored company description.",
        "industries_served": ["Hospitality", "Facility management"],
        "product_categories": ["Cleaning Products", "Hand Hygiene"],
        "currency": "QAR",
        "contact": {
            "phone": "+974 1234 5678",
            "email": None,
            "website": None,
            "address": None,
        },
    }


def build_service(tmp_path: Path) -> tuple[CompanyService, CompanyRepository]:
    """Build a service and repository over one temporary company document."""
    path = tmp_path / "company.json"
    path.write_text(json.dumps(company_payload()), encoding="utf-8")
    repository = CompanyRepository(path)
    return CompanyService(repository), repository


def test_get_company_information_matches_stored_data(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    assert service.get_company_information() == {
        "name": "Stored Company Name",
        "short_name": "SCN",
        "description": "Stored company description.",
        "industries_served": ["Hospitality", "Facility management"],
        "product_categories": ["Cleaning Products", "Hand Hygiene"],
        "currency": "QAR",
        "contact": {
            "phone": "+974 1234 5678",
            "email": None,
            "website": None,
            "address": None,
        },
    }


def test_get_company_summary_has_stable_safe_keys(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    assert service.get_company_summary() == {
        "name": "Stored Company Name",
        "short_name": "SCN",
        "description": "Stored company description.",
    }


def test_list_product_categories_has_stable_key_and_stored_values(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    assert service.list_product_categories() == {
        "categories": ["Cleaning Products", "Hand Hygiene"]
    }


def test_service_outputs_are_json_compatible(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    assert json.loads(json.dumps(service.get_company_information())) == (
        service.get_company_information()
    )
    assert json.loads(json.dumps(service.get_company_summary())) == service.get_company_summary()
    assert json.loads(json.dumps(service.list_product_categories())) == (
        service.list_product_categories()
    )


def test_unknown_contact_fields_remain_unknown(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    contact = service.get_company_information()["contact"]

    assert contact == {
        "phone": "+974 1234 5678",
        "email": None,
        "website": None,
        "address": None,
    }


def test_service_does_not_mutate_repository_data(tmp_path: Path) -> None:
    service, repository = build_service(tmp_path)

    information = service.get_company_information()
    industries = information["industries_served"]
    categories = information["product_categories"]
    contact = information["contact"]
    industries.append("Caller-only industry")
    categories.clear()
    contact["address"] = "Caller-only address"

    stored = repository.get_company()
    assert stored.industries_served == ["Hospitality", "Facility management"]
    assert stored.product_categories == ["Cleaning Products", "Hand Hygiene"]
    assert stored.contact.address is None


def test_category_results_are_independent_between_calls(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    first = service.list_product_categories()
    categories = first["categories"]
    assert isinstance(categories, list)
    categories.append("Caller-only category")

    assert service.list_product_categories() == {
        "categories": ["Cleaning Products", "Hand Hygiene"]
    }
