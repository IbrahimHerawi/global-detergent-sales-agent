"""Integration tests for Jinja2, WeasyPrint, Poppler, and PDF storage."""

from __future__ import annotations

import re
import struct
import subprocess
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.exceptions import QuotationGenerationError
from app.schemas.quotation import GeneratedQuotation
from app.services.pdf_service import PDFService
from app.services.quotation_storage import (
    LocalQuotationStorage,
    QuotationReservation,
    StoredQuotationArtifact,
)


def generated_quotation(
    quotation_id: str,
    *,
    item_count: int = 1,
    customer_name: str = "Aisha & Sons",
) -> GeneratedQuotation:
    items: list[dict[str, object]] = []
    subtotal = Decimal("0.00")
    for index in range(1, item_count + 1):
        quantity = index
        line_total = Decimal("25.10") * quantity
        subtotal += line_total
        items.append(
            {
                "product_id": f"PRODUCT-{index:03d}",
                "sku": f"SKU-{index:03d}",
                "description": f"Professional floor cleaner {index}",
                "quantity": quantity,
                "unit_price": Decimal("25.10"),
                "subtotal": line_total,
            }
        )

    discount = Decimal("10.25")
    tax = Decimal("3.75")
    return GeneratedQuotation.model_validate(
        {
            "quotation_id": quotation_id,
            "issued_at": datetime(2026, 9, 19, 12, 30, tzinfo=UTC),
            "valid_until": date(2026, 10, 3),
            "customer": {
                "phone": "+97450000000",
                "name": customer_name,
                "company_name": "Example Facilities & Services LLC",
                "contact_person": "Omar Operations",
                "email": "purchasing@example.invalid",
                "address": "Industrial Area, Doha, Qatar",
                "notes": "Reference purchase order PO-100.",
            },
            "items": items,
            "totals": {
                "subtotal": subtotal,
                "discount": discount,
                "tax": tax,
                "total": subtotal - discount + tax,
                "currency": "QAR",
            },
            "validity_days": 14,
            "terms": [
                "Prices remain valid through the valid-until date.",
                "Availability is subject to confirmation.",
            ],
        }
    )


def extracted_text(pdf_path: Path) -> str:
    completed = subprocess.run(
        ["pdftotext", str(pdf_path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def page_count(pdf_path: Path) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(pdf_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"^Pages:\s+(\d+)$", completed.stdout, flags=re.MULTILINE)
    assert match is not None
    return int(match.group(1))


def render_pages(pdf_path: Path, output_directory: Path) -> list[Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    prefix = output_directory / pdf_path.stem
    subprocess.run(
        ["pdftoppm", "-png", "-r", "100", str(pdf_path), str(prefix)],
        check=True,
        capture_output=True,
    )
    pages = sorted(output_directory.glob(f"{pdf_path.stem}-*.png"))
    for page in pages:
        image = page.read_bytes()
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", image[16:24])
        assert width >= 800
        assert height >= 1100
    return pages


@pytest.mark.asyncio
async def test_generates_one_page_pdf_and_extracts_authoritative_values(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "quotes"
    storage = LocalQuotationStorage(root=storage_root)
    service = PDFService(storage)
    quotation = generated_quotation(
        "GDF-Q-20260919-A82F",
        customer_name="Aisha <b>Danger</b> & Sons",
    )

    pdf_path = await service.generate_quotation_pdf(quotation)
    text = extracted_text(pdf_path)
    compact_text = " ".join(text.split())
    rendered_pages = render_pages(pdf_path, tmp_path / "rendered")

    assert pdf_path.parent == storage_root.resolve()
    assert pdf_path.stat().st_size > 0
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    assert quotation.quotation_id in "".join(text.split())
    assert "Aisha <b>Danger</b> & Sons" in compact_text
    assert "Example Facilities & Services LLC" in compact_text
    assert "SKU-001" in compact_text
    assert "QAR 25.10" in compact_text
    assert "QAR 10.25" in compact_text
    assert "QAR 3.75" in compact_text
    assert "QAR 18.60" in compact_text
    assert page_count(pdf_path) == 1
    assert len(rendered_pages) == 1


@pytest.mark.asyncio
async def test_generates_multipage_pdf_with_complete_table_and_totals(
    tmp_path: Path,
) -> None:
    storage = LocalQuotationStorage(root=tmp_path / "quotes")
    service = PDFService(storage)
    quotation = generated_quotation("GDF-Q-20260919-B82F", item_count=70)

    pdf_path = await service.generate_quotation_pdf(quotation)
    text = " ".join(extracted_text(pdf_path).split())
    rendered_pages = render_pages(pdf_path, tmp_path / "rendered")

    assert "SKU-001" in text
    assert "SKU-070" in text
    assert "Professional floor cleaner 70" in text
    assert "QAR 1757.00" in text
    assert "QAR 62367.00" in text
    assert page_count(pdf_path) >= 3
    assert len(rendered_pages) == page_count(pdf_path)


@pytest.mark.asyncio
async def test_blocking_render_runs_outside_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalQuotationStorage(root=tmp_path / "quotes")
    service = PDFService(storage)
    quotation = generated_quotation("GDF-Q-20260919-C82F")
    event_loop_thread = threading.get_ident()
    rendering_threads: list[int] = []
    original_render = PDFService._render_pdf_bytes

    def recording_render(
        current_service: PDFService,
        current_quotation: GeneratedQuotation,
    ) -> bytes:
        rendering_threads.append(threading.get_ident())
        return original_render(current_service, current_quotation)

    monkeypatch.setattr(PDFService, "_render_pdf_bytes", recording_render)

    await service.generate_quotation_pdf(quotation)

    assert rendering_threads
    assert all(thread_id != event_loop_thread for thread_id in rendering_threads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template_source",
    [
        '<html><body><img src="https://example.com/tracker.png"></body></html>',
        '<html><body><img src="file:///etc/passwd"></body></html>',
        "{% include '/etc/passwd' %}",
    ],
)
async def test_prohibited_remote_and_filesystem_resources_are_rejected(
    tmp_path: Path,
    template_source: str,
) -> None:
    template_root = tmp_path / "templates"
    asset_root = tmp_path / "assets"
    storage_root = tmp_path / "quotes"
    template_root.mkdir()
    asset_root.mkdir()
    (template_root / "quotation.html").write_text(template_source, encoding="utf-8")
    service = PDFService(
        LocalQuotationStorage(root=storage_root),
        template_directory=template_root,
        asset_directory=asset_root,
    )

    with pytest.raises(QuotationGenerationError):
        await service.generate_quotation_pdf(generated_quotation("GDF-Q-20260919-D82F"))

    assert not list(storage_root.glob("*"))


@pytest.mark.asyncio
async def test_approved_local_asset_can_render_without_network_access(tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    asset_root = tmp_path / "assets"
    storage_root = tmp_path / "quotes"
    template_root.mkdir()
    asset_root.mkdir()
    logo = asset_root / "logo.svg"
    logo.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        '<rect width="20" height="20" fill="#0f766e"/></svg>',
        encoding="utf-8",
    )
    (template_root / "quotation.html").write_text(
        f'<html><body><img src="{logo.as_uri()}">Approved asset</body></html>',
        encoding="utf-8",
    )
    service = PDFService(
        LocalQuotationStorage(root=storage_root),
        template_directory=template_root,
        asset_directory=asset_root,
    )

    pdf_path = await service.generate_quotation_pdf(generated_quotation("GDF-Q-20260919-E82F"))

    assert pdf_path.is_file()
    assert "Approved asset" in extracted_text(pdf_path)


@pytest.mark.asyncio
async def test_asset_symlink_cannot_escape_approved_directory(tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    asset_root = tmp_path / "assets"
    storage_root = tmp_path / "quotes"
    template_root.mkdir()
    asset_root.mkdir()
    outside_asset = tmp_path / "outside.svg"
    outside_asset.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"></svg>',
        encoding="utf-8",
    )
    escaped_asset = asset_root / "escaped.svg"
    escaped_asset.symlink_to(outside_asset)
    (template_root / "quotation.html").write_text(
        f'<html><body><img src="{escaped_asset.as_uri()}"></body></html>',
        encoding="utf-8",
    )
    service = PDFService(
        LocalQuotationStorage(root=storage_root),
        template_directory=template_root,
        asset_directory=asset_root,
    )

    with pytest.raises(QuotationGenerationError):
        await service.generate_quotation_pdf(generated_quotation("GDF-Q-20260919-9A2F"))

    assert not list(storage_root.glob("*"))


class FailingStorage(LocalQuotationStorage):
    """Fail after reservation so service cleanup can be verified."""

    def write(
        self,
        reservation: QuotationReservation,
        pdf_content: bytes,
    ) -> StoredQuotationArtifact:
        del reservation, pdf_content
        raise OSError("simulated storage failure")


@pytest.mark.asyncio
async def test_storage_failure_becomes_generation_error_and_cleans_reservation(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "quotes"
    service = PDFService(FailingStorage(root=storage_root))

    with pytest.raises(QuotationGenerationError) as raised:
        await service.generate_quotation_pdf(generated_quotation("GDF-Q-20260919-F82F"))

    assert isinstance(raised.value.cause, OSError)
    assert not list(storage_root.glob("*"))


@pytest.mark.asyncio
async def test_invalid_render_output_is_rejected_before_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "quotes"
    service = PDFService(LocalQuotationStorage(root=storage_root))
    monkeypatch.setattr(PDFService, "_render_pdf_bytes", lambda *_: b"%PDF-partial")

    with pytest.raises(QuotationGenerationError):
        await service.generate_quotation_pdf(generated_quotation("GDF-Q-20260919-082F"))

    assert not list(storage_root.glob("*"))
