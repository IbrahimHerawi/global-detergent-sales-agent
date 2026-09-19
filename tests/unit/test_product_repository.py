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
    short_description: str | None = None,
    applications: list[str] | None = None,
    keywords: list[str] | None = None,
    active: bool = True,
) -> dict[str, object]:
    """Return a minimal valid product fixture."""
    return {
        "id": product_id,
        "sku": sku,
        "name": name,
        "category": category,
        "short_description": short_description or f"Development entry for {name}.",
        "description": f"Synthetic development description for {name}.",
        "applications": applications or [],
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "25.00", "currency": "QAR"},
        "keywords": keywords or [name.casefold()],
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
        "search",
    }


def test_search_normalizes_case_unicode_punctuation_and_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(
        path,
        [
            product_payload(
                product_id="P-UNICODE",
                sku="UNI-1L",
                name="Ｃａｆé—Straße Cleaner",
            )
        ],
    )
    repository = ProductRepository(path)

    assert [product.id for product in repository.search("  CAFÉ - STRASSE   cleaner  ")] == [
        "P-UNICODE"
    ]


def test_exact_sku_ranks_before_exact_name_and_other_match_tiers(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(
        path,
        [
            product_payload(product_id="P-CATEGORY", sku="CAT-1", category="Target"),
            product_payload(product_id="P-KEYWORD", sku="KEY-1", name="Other", keywords=["target"]),
            product_payload(product_id="P-NAME-PHRASE", sku="NAM-1", name="Target Cleaner"),
            product_payload(product_id="P-EXACT-NAME", sku="EXN-1", name="Target"),
            product_payload(product_id="P-EXACT-SKU", sku="TARGET", name="SKU Product"),
        ],
    )
    repository = ProductRepository(path)

    assert [product.id for product in repository.search("target")] == [
        "P-EXACT-SKU",
        "P-EXACT-NAME",
        "P-NAME-PHRASE",
        "P-KEYWORD",
        "P-CATEGORY",
    ]


def test_search_uses_applications_and_short_description(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(
        path,
        [
            product_payload(
                product_id="P-APPLICATION",
                sku="APP-1",
                name="First",
                category="Other",
                applications=["food preparation areas"],
            ),
            product_payload(
                product_id="P-DESCRIPTION",
                sku="DESC-1",
                name="Second",
                category="Other",
                short_description="For food preparation areas.",
            ),
        ],
    )
    repository = ProductRepository(path)

    assert [product.id for product in repository.search("food preparation")] == [
        "P-APPLICATION",
        "P-DESCRIPTION",
    ]


def test_multiword_token_search_finds_sample_floor_cleaner() -> None:
    repository = ProductRepository()

    matches = repository.search("office floor cleaning")

    assert matches
    assert matches[0].id == "GDF-FLC-001"


def test_search_ties_are_broken_by_product_id(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(
        path,
        [
            product_payload(product_id="P-003", sku="THREE", name="Third", keywords=["shared"]),
            product_payload(product_id="P-001", sku="ONE", name="First", keywords=["shared"]),
            product_payload(product_id="P-002", sku="TWO", name="Second", keywords=["shared"]),
        ],
    )
    repository = ProductRepository(path)

    assert [product.id for product in repository.search("shared")] == [
        "P-001",
        "P-002",
        "P-003",
    ]


def test_search_excludes_inactive_products(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(
        path,
        [
            product_payload(product_id="P-ACTIVE", sku="ACT", name="Active", keywords=["match"]),
            product_payload(
                product_id="P-INACTIVE",
                sku="INA",
                name="Inactive",
                keywords=["match"],
                active=False,
            ),
        ],
    )
    repository = ProductRepository(path)

    assert [product.id for product in repository.search("match")] == ["P-ACTIVE"]


def test_search_returns_at_most_five_matches(tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(
        path,
        [
            product_payload(
                product_id=f"P-{index:03d}",
                sku=f"SKU-{index}",
                name=f"Product {index}",
                keywords=["shared"],
            )
            for index in range(7)
        ],
    )
    repository = ProductRepository(path)

    assert [product.id for product in repository.search("shared")] == [
        "P-000",
        "P-001",
        "P-002",
        "P-003",
        "P-004",
    ]


@pytest.mark.parametrize("query", ["", "  \t\r\n  ", "no-such-catalog-term"])
def test_search_returns_no_arbitrary_matches(query: str, tmp_path: Path) -> None:
    path = tmp_path / "products.json"
    write_products(path, indexed_catalog())
    repository = ProductRepository(path)

    assert repository.search(query) == []


def test_repository_loads_development_catalog_from_container_path() -> None:
    repository = ProductRepository()

    products = repository.get_all()

    assert len(products) == 10
    assert repository.get_by_id("GDF-FLC-001") is not None
    assert repository.get_by_sku("flc-5l") is not None
