"""Tests for active-only product service outputs."""

import json
from pathlib import Path

import pytest
from app.core.exceptions import ProductNotFoundError
from app.repositories.product_repository import ProductRepository
from app.services.product_service import ProductService


def product_payload(
    *,
    product_id: str,
    sku: str,
    name: str,
    category: str,
    active: bool = True,
    keywords: list[str] | None = None,
) -> dict[str, object]:
    """Return a complete product fixture with both known and unavailable facts."""
    return {
        "id": product_id,
        "sku": sku,
        "name": name,
        "category": category,
        "short_description": f"Short description for {name}.",
        "description": f"Full description for {name}.",
        "applications": ["Stored application"],
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "25.50", "currency": "QAR"},
        "instructions": None,
        "safety_information": None,
        "technical_details": {"color": None, "fragrance": "Lemon", "ph": None},
        "active_ingredients": [],
        "recommended_surfaces": ["Stored surface"],
        "contact_time": None,
        "dilution_ratio": None,
        "certifications": [],
        "approved_claims": [],
        "keywords": keywords or [name.casefold()],
        "active": active,
    }


def build_service(tmp_path: Path) -> tuple[ProductService, ProductRepository]:
    """Build a service over active and inactive catalog records."""
    products = [
        product_payload(
            product_id="P-FLOOR",
            sku="FLOOR-5L",
            name="Floor Cleaner",
            category="Floor Cleaning",
            keywords=["floor cleaning", "office floor"],
        ),
        product_payload(
            product_id="P-GLASS",
            sku="GLASS-5L",
            name="Glass Cleaner",
            category="Glass Cleaning",
            keywords=["glass cleaning", "windows"],
        ),
        product_payload(
            product_id="P-INACTIVE",
            sku="INACTIVE-5L",
            name="Inactive Floor Cleaner",
            category="Floor Cleaning",
            active=False,
            keywords=["floor cleaning", "office floor"],
        ),
    ]
    path = tmp_path / "products.json"
    path.write_text(json.dumps(products), encoding="utf-8")
    repository = ProductRepository(path)
    return ProductService(repository), repository


def test_list_products_returns_active_summaries_only(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    assert service.list_products() == {
        "products": [
            {
                "product_id": "P-FLOOR",
                "sku": "FLOOR-5L",
                "name": "Floor Cleaner",
                "category": "Floor Cleaning",
                "short_description": "Short description for Floor Cleaner.",
            },
            {
                "product_id": "P-GLASS",
                "sku": "GLASS-5L",
                "name": "Glass Cleaner",
                "category": "Glass Cleaning",
                "short_description": "Short description for Glass Cleaner.",
            },
        ]
    }


def test_list_products_filters_category_without_exposing_inactive_records(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    result = service.list_products("  fLoOr ClEaNiNg ")

    assert [product["product_id"] for product in result["products"]] == ["P-FLOOR"]
    assert service.list_products("Unknown") == {"products": []}


def test_search_products_returns_active_factual_summaries(tmp_path: Path) -> None:
    service, repository = build_service(tmp_path)
    query = "office floor cleaning"

    result = service.search_products(query)

    assert [product["product_id"] for product in result["matches"]] == [
        product.id for product in repository.search(query)
    ]
    assert result["matches"][0]["product_id"] == "P-FLOOR"
    assert all(product["product_id"] != "P-INACTIVE" for product in result["matches"])
    assert set(result["matches"][0]) == {
        "product_id",
        "sku",
        "name",
        "category",
        "short_description",
    }
    assert service.search_products("unknown requirement") == {"matches": []}


def test_product_details_include_all_stored_facts_and_string_price(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)

    details = service.get_product_details("P-FLOOR")

    assert details == {
        "product_id": "P-FLOOR",
        "sku": "FLOOR-5L",
        "name": "Floor Cleaner",
        "category": "Floor Cleaning",
        "short_description": "Short description for Floor Cleaner.",
        "description": "Full description for Floor Cleaner.",
        "applications": ["Stored application"],
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "25.50", "currency": "QAR"},
        "instructions": None,
        "safety_information": None,
        "technical_details": {"color": None, "fragrance": "Lemon", "ph": None},
        "active_ingredients": [],
        "recommended_surfaces": ["Stored surface"],
        "contact_time": None,
        "dilution_ratio": None,
        "certifications": [],
        "approved_claims": [],
        "keywords": ["floor cleaning", "office floor"],
        "active": True,
    }
    assert isinstance(details["price"]["amount"], str)
    assert json.loads(json.dumps(details)) == details


@pytest.mark.parametrize("product_id", ["UNKNOWN", "P-INACTIVE"])
def test_unknown_and_inactive_details_raise_safe_not_found(
    product_id: str,
    tmp_path: Path,
) -> None:
    service, _ = build_service(tmp_path)

    with pytest.raises(ProductNotFoundError) as raised:
        service.get_product_details(product_id)

    assert raised.value.to_public_dict() == {
        "code": "product_not_found",
        "message": "The requested product was not found.",
    }
    assert product_id not in raised.value.to_public_dict()["message"]


def test_recommendations_are_only_deterministic_repository_matches(tmp_path: Path) -> None:
    service, repository = build_service(tmp_path)
    requirement = "office floor cleaning"
    repository_ids = [product.id for product in repository.search(requirement)]

    result = service.recommend_products(requirement)

    recommendation_ids = [product["product_id"] for product in result["recommendations"]]
    assert recommendation_ids == repository_ids
    assert recommendation_ids[0] == "P-FLOOR"
    assert "P-INACTIVE" not in recommendation_ids
    assert set(result["recommendations"][0]) == {
        "product_id",
        "sku",
        "name",
        "category",
        "short_description",
    }
    assert service.recommend_products("no matching use") == {"recommendations": []}


def test_service_outputs_cannot_mutate_repository_products(tmp_path: Path) -> None:
    service, repository = build_service(tmp_path)

    details = service.get_product_details("P-FLOOR")
    details["applications"].append("Caller-only application")
    details["packaging"]["size"] = "999"
    details["price"]["amount"] = "0.00"
    details["technical_details"]["fragrance"] = "Caller-only fragrance"

    stored = repository.get_by_id("P-FLOOR")
    assert stored is not None
    assert stored.applications == ["Stored application"]
    assert stored.packaging.size == "5"
    assert str(stored.price.amount) == "25.50"
    assert stored.technical_details.fragrance == "Lemon"
