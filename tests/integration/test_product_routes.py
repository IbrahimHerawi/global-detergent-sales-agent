"""HTTP integration coverage for non-production product inspection routes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.api.dependencies import get_product_service
from app.main import app
from app.repositories.product_repository import ProductRepository
from app.services.product_service import ProductService


def product_payload(
    product_id: str,
    *,
    category: str,
    active: bool = True,
) -> dict[str, object]:
    return {
        "id": product_id,
        "sku": f"SKU-{product_id}",
        "name": f"Cleaner {product_id}",
        "category": category,
        "short_description": f"Stored summary for {product_id}.",
        "description": f"Stored full description for {product_id}.",
        "applications": ["Stored application"],
        "packaging": {"size": "5", "unit": "L"},
        "price": {"amount": "25.50", "currency": "QAR"},
        "instructions": None,
        "safety_information": None,
        "technical_details": {"color": None, "fragrance": None, "ph": None},
        "active_ingredients": [],
        "recommended_surfaces": [],
        "contact_time": None,
        "dilution_ratio": None,
        "certifications": [],
        "approved_claims": [],
        "keywords": ["cleaner"],
        "active": active,
    }


@pytest.fixture
def product_service(tmp_path: Path) -> ProductService:
    products = [
        *(product_payload(f"A-OTHER-{index}", category="Other") for index in range(1, 7)),
        *(product_payload(f"Z-TARGET-{index}", category="Target") for index in range(1, 4)),
        product_payload("Z-INACTIVE", category="Target", active=False),
    ]
    path = tmp_path / "products.json"
    path.write_text(json.dumps(products), encoding="utf-8")
    return ProductService(ProductRepository(path))


@pytest.fixture
async def client(product_service: ProductService) -> AsyncIterator[httpx.AsyncClient]:
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_product_service] = lambda: product_service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as instance:
            yield instance
    finally:
        app.dependency_overrides = original_overrides


async def test_list_returns_only_active_safe_summaries(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/products")

    assert response.status_code == 200
    products = response.json()["products"]
    assert len(products) == 9
    assert "Z-INACTIVE" not in {product["product_id"] for product in products}
    assert set(products[0]) == {
        "product_id",
        "sku",
        "name",
        "category",
        "short_description",
    }


async def test_category_filter_is_applied_before_top_five_search_selection(
    client: httpx.AsyncClient,
) -> None:
    global_response = await client.get("/api/v1/products", params={"search": "cleaner"})
    combined_response = await client.get(
        "/api/v1/products",
        params={"category": " target ", "search": "cleaner"},
    )

    assert global_response.status_code == 200
    assert [product["product_id"] for product in global_response.json()["products"]] == [
        "A-OTHER-1",
        "A-OTHER-2",
        "A-OTHER-3",
        "A-OTHER-4",
        "A-OTHER-5",
    ]
    assert combined_response.status_code == 200
    assert [product["product_id"] for product in combined_response.json()["products"]] == [
        "Z-TARGET-1",
        "Z-TARGET-2",
        "Z-TARGET-3",
    ]


async def test_category_only_listing_uses_active_service_filter(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/products", params={"category": "TARGET"})

    assert response.status_code == 200
    assert [product["product_id"] for product in response.json()["products"]] == [
        "Z-TARGET-1",
        "Z-TARGET-2",
        "Z-TARGET-3",
    ]


async def test_detail_returns_full_stored_data_with_exact_decimal_text(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/products/Z-TARGET-1")

    assert response.status_code == 200
    details = response.json()
    assert details["product_id"] == "Z-TARGET-1"
    assert details["description"] == "Stored full description for Z-TARGET-1."
    assert details["price"] == {"amount": "25.50", "currency": "QAR"}
    assert isinstance(details["price"]["amount"], str)
    assert details["instructions"] is None


@pytest.mark.parametrize("product_id", ["UNKNOWN", "Z-INACTIVE"])
async def test_unknown_and_inactive_products_return_safe_404(
    client: httpx.AsyncClient,
    product_id: str,
) -> None:
    response = await client.get(f"/api/v1/products/{product_id}")

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "product_not_found",
        "message": "The requested product was not found.",
    }
    assert product_id not in response.text


async def test_product_routes_are_published_in_container_swagger(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/products" in response.json()["paths"]
    assert "/api/v1/products/{product_id}" in response.json()["paths"]


def test_product_routes_are_not_registered_in_production() -> None:
    environment = dict(os.environ)
    environment["APP_ENV"] = "production"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from app.main import app; "
                "print(json.dumps(sorted(app.openapi()['paths'])))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )
    paths = json.loads(result.stdout)

    assert "/health" in paths
    assert "/api/v1/products" not in paths
    assert "/api/v1/products/{product_id}" not in paths
