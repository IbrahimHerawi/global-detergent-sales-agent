"""Tests for validated, indexed, read-only product loading."""

import json
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path

import pytest
from app.core.config import Settings, override_settings
from app.core.exceptions import TECHNICAL_FALLBACK_MESSAGE, InvalidStaticDataError
from app.repositories.product_repository import ProductRepository
from pydantic import ValidationError


def product_payload(
    *,
    product_id: str = "GDF-ACT-001",
    sku: str = "ACT-5L",
    name: str = "Active Product",
    category: str = "Floor Cleaning",
    active: bool = True,
) -> dict[str, object]:
    """Return a minimal valid product fixture."""
    return {
        "id": product_id,
        "sku": sku,
        "name": name,
        "category": category,
        "short_description": f"Development entry for {name}.",
        "description": f"Synthetic development description for {name}.",
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "25.00", "currency": "QAR"},
        "keywords": [name.casefold()],
        "active": active,
    }


def write_products(path: Path, payload: object) -> None:
    """Write a product catalog fixture."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def indexed_catalog() -> list[dict[str, object]]:
    """Return active and inactive records sharing one category."""
    return [
        product_payload(),
        product_payload(
            product_id="GDF-INACTIVE-001",
            sku="INA-1L",
            name="Inactive Product",
            category="Floor Cleaning",
            active=False,
        ),
        product_payload(
            product_id="GDF-GLASS-001",
            sku="GLA-1L",
            name="Glass Product",
            category="Glass Cleaning",
        ),
    ]


def test_repository_loads_configured_json_array_once(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(path, indexed_catalog())
    settings = Settings.model_validate({"product_data_path": path})

    with override_settings(settings):
        repository = ProductRepository()

    path.write_text("not valid JSON anymore", encoding="utf-8")

    assert [product.id for product in repository.get_all()] == [
        "GDF-ACT-001",
        "GDF-INACTIVE-001",
        "GDF-GLASS-001",
    ]


def test_id_and_normalized_sku_indexes_include_inactive_products(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(path, indexed_catalog())
    repository = ProductRepository(path)

    by_id = repository.get_by_id(" GDF-INACTIVE-001 ")
    by_sku = repository.get_by_sku("  ina-1l ")

    assert by_id is not None
    assert by_id.active is False
    assert by_sku is not None
    assert by_sku.id == "GDF-INACTIVE-001"
    assert repository.get_by_id("gdf-inactive-001") is None
    assert repository.get_by_id("missing") is None
    assert repository.get_by_sku("missing") is None


def test_active_index_excludes_inactive_products(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(path, indexed_catalog())
    repository = ProductRepository(path)

    assert [product.id for product in repository.get_active()] == [
        "GDF-ACT-001",
        "GDF-GLASS-001",
    ]
    assert len(repository.get_all()) == 3


def test_category_matching_is_case_insensitive_and_preserves_labels(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(path, indexed_catalog())
    repository = ProductRepository(path)

    matches = repository.get_by_category("  fLoOr ClEaNiNg  ")

    assert [product.id for product in matches] == ["GDF-ACT-001", "GDF-INACTIVE-001"]
    assert [product.category for product in matches] == ["Floor Cleaning", "Floor Cleaning"]
    assert repository.get_by_category("unknown") == []


def test_all_lookup_results_are_deep_defensive_copies(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(path, indexed_catalog())
    repository = ProductRepository(path)

    first = repository.get_by_id("GDF-ACT-001")
    assert first is not None
    first.name = "Caller mutation"
    first.keywords.append("caller-only")
    first.packaging.size = "999"
    first.price.amount = Decimal("0.00")
    repository.get_all().clear()

    unchanged = repository.get_by_id("GDF-ACT-001")
    assert unchanged is not None
    assert unchanged.name == "Active Product"
    assert unchanged.keywords == ["active product"]
    assert unchanged.packaging.size == "5"
    assert unchanged.price.amount == Decimal("25.00")
    assert len(repository.get_all()) == 3


def test_duplicate_ids_fail_at_initialization(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    products = indexed_catalog()
    products[1]["id"] = products[0]["id"]
    write_products(path, products)

    with pytest.raises(InvalidStaticDataError) as raised:
        ProductRepository(path)

    assert raised.value.diagnostic_detail is not None
    assert "Duplicate product ID" in raised.value.diagnostic_detail
    assert "GDF-ACT-001" in raised.value.diagnostic_detail


def test_duplicate_normalized_skus_fail_at_initialization(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    products = indexed_catalog()
    products[1]["sku"] = "  act-5l  "
    write_products(path, products)

    with pytest.raises(InvalidStaticDataError) as raised:
        ProductRepository(path)

    assert raised.value.diagnostic_detail is not None
    assert "Duplicate normalized product SKU" in raised.value.diagnostic_detail
    assert "act-5l" in raised.value.diagnostic_detail


def test_missing_catalog_fails_predictably(tmp_path: Path) -> None:
    path = tmp_path / "missing-products.json"

    with pytest.raises(InvalidStaticDataError) as raised:
        ProductRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert str(path) in error.diagnostic_detail
    assert "Unable to read product catalog" in error.diagnostic_detail
    assert isinstance(error.cause, FileNotFoundError)
    assert str(error) == TECHNICAL_FALLBACK_MESSAGE


def test_malformed_json_reports_location_without_document_content(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    secret_text = "private-token-must-not-leak"
    path.write_text(f'[{{"id": "{secret_text}",', encoding="utf-8")

    with pytest.raises(InvalidStaticDataError) as raised:
        ProductRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "Malformed product catalog JSON" in error.diagnostic_detail
    assert "line 1" in error.diagnostic_detail
    assert secret_text not in error.diagnostic_detail
    assert isinstance(error.cause, JSONDecodeError)


@pytest.mark.parametrize("catalog", [{"products": []}, None, "not-an-array"])
def test_catalog_root_must_be_a_json_array(tmp_path: Path, catalog: object) -> None:
    path = tmp_path / "products.json"
    write_products(path, catalog)

    with pytest.raises(InvalidStaticDataError) as raised:
        ProductRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "Invalid product catalog" in error.diagnostic_detail
    assert isinstance(error.cause, ValidationError)


def test_schema_failure_identifies_product_and_field_without_input_value(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    secret_text = "private-token-must-not-leak"
    product = product_payload()
    product["private_api_key"] = secret_text
    product["price"] = {"amount": "not-money", "currency": "QAR"}
    write_products(path, [product])

    with pytest.raises(InvalidStaticDataError) as raised:
        ProductRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "0.price.amount" in error.diagnostic_detail
    assert "0.private_api_key" in error.diagnostic_detail
    assert secret_text not in error.diagnostic_detail
    assert isinstance(error.cause, ValidationError)


def test_repository_exposes_only_required_read_operations() -> None:
    public_methods = {
        name
        for name in dir(ProductRepository)
        if not name.startswith("_") and callable(getattr(ProductRepository, name))
    }

    assert public_methods == {
        "get_active",
        "get_all",
        "get_by_category",
        "get_by_id",
        "get_by_sku",
    }
    assert "search" not in public_methods


def test_repository_loads_development_catalog_from_container_path() -> None:
    repository = ProductRepository()

    products = repository.get_all()

    assert len(products) == 10
    assert repository.get_by_id("GDF-FLC-001") is not None
    assert repository.get_by_sku("flc-5l") is not None
