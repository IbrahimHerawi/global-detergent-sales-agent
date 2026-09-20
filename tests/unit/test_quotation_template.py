"""Render and inspect the print-oriented quotation template."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.schemas.company import Company
from app.schemas.quotation import GeneratedQuotation
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from weasyprint import HTML  # type: ignore[import-untyped]

TEMPLATE_DIRECTORY = Path("/app/app/templates")
TEMPLATE_PATH = TEMPLATE_DIRECTORY / "quotation.html"


def company(*, with_contacts: bool = True) -> Company:
    contact: dict[str, str | None]
    if with_contacts:
        contact = {
            "phone": "+974 4400 0000",
            "email": "sales@example.invalid",
            "website": "www.example.invalid",
            "address": "Industrial Area, Doha, Qatar",
        }
    else:
        contact = {"phone": None, "email": None, "website": None, "address": None}
    return Company.model_validate(
        {
            "name": "Global Detergent Factory",
            "short_name": "GDF",
            "description": "Synthetic template-test company.",
            "industries_served": [],
            "product_categories": [],
            "currency": "QAR",
            "contact": contact,
        }
    )


def quotation(
    *,
    item_count: int = 1,
    long_content: bool = False,
) -> GeneratedQuotation:
    long_word = "ULTRALONGUNBROKENTEXT" * 14
    customer_name = (
        f"Aisha & Sons <script>alert('x')</script> {long_word}" if long_content else "Aisha"
    )
    address = (
        f"Building 123, Floor 45, Industrial District, Doha, Qatar {long_word}"
        if long_content
        else "Doha, Qatar"
    )
    items: list[dict[str, object]] = []
    subtotal = Decimal("0.00")
    for index in range(1, item_count + 1):
        description = (
            f"Heavy-duty professional cleaner {long_word} with a detailed supplied description"
            if long_content
            else f"Professional floor cleaner {index}"
        )
        line_subtotal = Decimal("25.10") * index
        subtotal += line_subtotal
        items.append(
            {
                "product_id": f"PRODUCT-{index:03d}",
                "sku": f"SKU-{index:03d}",
                "description": description,
                "quantity": index,
                "unit_price": Decimal("25.10"),
                "subtotal": line_subtotal,
            }
        )

    discount = Decimal("10.25")
    tax = Decimal("3.75")
    total = subtotal - discount + tax
    return GeneratedQuotation.model_validate(
        {
            "quotation_id": "GDF-Q-20260919-A82F",
            "issued_at": datetime(2026, 9, 19, 12, 30, tzinfo=UTC),
            "valid_until": date(2026, 10, 3),
            "customer": {
                "phone": "+97450000000",
                "name": customer_name,
                "company_name": "Example Facilities & Services LLC",
                "contact_person": "Omar <Operations>",
                "email": "purchasing@example.invalid",
                "address": address,
                "notes": "Please reference PO <pending> on delivery.",
            },
            "items": items,
            "totals": {
                "subtotal": subtotal,
                "discount": discount,
                "tax": tax,
                "total": total,
                "currency": "QAR",
            },
            "validity_days": 14,
            "terms": [
                "Prices remain valid through the valid-until date.",
                "Availability is subject to confirmation.",
            ],
        }
    )


@pytest.fixture(scope="module")
def template_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIRECTORY),
        autoescape=select_autoescape(enabled_extensions=("html",)),
        undefined=StrictUndefined,
    )


def render_html(
    template_environment: Environment,
    generated_quote: GeneratedQuotation,
    *,
    supplied_company: Company | None = None,
    logo_data_uri: str | None = None,
) -> str:
    template = template_environment.get_template("quotation.html")
    return template.render(
        company=supplied_company or company(),
        quotation=generated_quote,
        logo_data_uri=logo_data_uri,
    )


def render_pdf(html: str) -> tuple[bytes, int]:
    document = HTML(string=html, base_url=str(TEMPLATE_DIRECTORY)).render()
    pdf = document.write_pdf()
    return pdf, len(document.pages)


def test_one_item_html_contains_every_required_field_and_supplied_values(
    template_environment: Environment,
) -> None:
    generated_quote = quotation()
    logo = "data:image/svg+xml;base64,PHN2Zy8+"

    html = render_html(template_environment, generated_quote, logo_data_uri=logo)
    pdf, page_count = render_pdf(html)

    expected_text = (
        "Global Detergent Factory",
        "QUOTATION",
        "GDF-Q-20260919-A82F",
        "2026-09-19",
        "2026-10-03",
        "Aisha",
        "Example Facilities &amp; Services LLC",
        "Omar &lt;Operations&gt;",
        "+97450000000",
        "purchasing@example.invalid",
        "Doha, Qatar",
        "Professional floor cleaner 1",
        "SKU-001",
        "QAR 25.10",
        "QAR 10.25",
        "QAR 3.75",
        "QAR 18.60",
        "All amounts are in QAR.",
        "Prices remain valid through the valid-until date.",
    )
    assert all(value in html for value in expected_text)
    assert f'src="{logo}"' in html
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert page_count == 1


def test_unavailable_company_and_customer_contacts_are_omitted(
    template_environment: Environment,
) -> None:
    generated_quote = quotation().model_copy(deep=True)
    generated_quote.customer.email = None
    generated_quote.customer.address = None
    generated_quote.customer.contact_person = None
    generated_quote.customer.notes = None

    html = render_html(
        template_environment,
        generated_quote,
        supplied_company=company(with_contacts=False),
    )

    assert '<img class="brand-logo"' not in html
    assert "sales@example.invalid" not in html
    assert "Industrial Area" not in html
    assert "purchasing@example.invalid" not in html
    assert "Contact person" not in html
    assert "Notes" not in html


def test_customer_text_is_escaped_and_long_content_renders_readably(
    template_environment: Environment,
) -> None:
    html = render_html(template_environment, quotation(long_content=True))
    pdf, page_count = render_pdf(html)

    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html
    assert "&lt;pending&gt;" in html
    assert "overflow-wrap: anywhere" in html
    assert "word-break: break-word" not in html
    assert "table-layout: fixed" in html
    assert pdf.startswith(b"%PDF-")
    assert page_count >= 1


def test_many_items_render_across_pages_with_repeating_table_header(
    template_environment: Environment,
) -> None:
    html = render_html(template_environment, quotation(item_count=70))
    pdf, page_count = render_pdf(html)

    assert html.count("<tr>") >= 70
    assert "display: table-header-group" in html
    assert "break-inside: avoid" in html
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert page_count >= 3


def test_template_has_no_external_assets_or_business_arithmetic() -> None:
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    jinja_expressions = re.findall(r"{{(.*?)}}", source, flags=re.DOTALL)

    assert "http://" not in source.casefold()
    assert "https://" not in source.casefold()
    assert "@import" not in source.casefold()
    assert "url(" not in source.casefold()
    assert all(
        not re.search(r"\b(subtotal|discount|tax|total)\b\s*[+*/-]", expression)
        for expression in jinja_expressions
    )
