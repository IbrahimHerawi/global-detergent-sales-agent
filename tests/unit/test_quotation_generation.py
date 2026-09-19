"""Tests for authorized, idempotent quotation generation."""

import inspect
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.exceptions import QuotationGenerationError, StalePreviewError
from app.repositories.product_repository import ProductRepository
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.quotation import CustomerInfo, GeneratedQuotation, QuoteCartItem
from app.schemas.session import ConversationSession, ConversationState
from app.services.quotation_service import CurrentTurnContext, QuotationService

PRODUCT_ID = "FLOOR"
ISSUED_AT = datetime(2026, 9, 19, 20, 30, tzinfo=UTC)
QUOTATION_ID = "GDF-Q-20260919-A82F"


class RecordingPDFService:
    """Small PDF boundary fake that records immutable service inputs."""

    def __init__(self, path: Path, *, fail: bool = False) -> None:
        self.path = path
        self.fail = fail
        self.quotations: list[GeneratedQuotation] = []

    async def generate_quotation_pdf(self, quotation: GeneratedQuotation) -> Path:
        self.quotations.append(quotation.model_copy(deep=True))
        if self.fail:
            raise QuotationGenerationError("injected PDF failure")
        return self.path


@pytest.fixture
def generation_dependencies(
    tmp_path: Path,
) -> tuple[QuotationService, ProductRepository, RecordingPDFService]:
    product_path = tmp_path / "products.json"
    product_path.write_text(
        json.dumps(
            [
                {
                    "id": PRODUCT_ID,
                    "sku": "SKU-FLOOR",
                    "name": "Floor Cleaner",
                    "category": "Development Cleaning",
                    "short_description": "Synthetic generation-test product.",
                    "description": "Synthetic product for generation tests.",
                    "packaging": {"size": "5", "unit": "L"},
                    "price": {"amount": "25.00", "currency": "QAR"},
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    rules_path = tmp_path / "quotation_rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "currency": "QAR",
                "quotation_prefix": "GDF-Q",
                "validity_days": 14,
                "default_discount_percent": "10.00",
                "tax_percent": "5.00",
                "terms": ["Subject to availability."],
            }
        ),
        encoding="utf-8",
    )
    repository = ProductRepository(product_path)
    pdf_service = RecordingPDFService(tmp_path / f"{QUOTATION_ID}.pdf")
    service = QuotationService(
        repository,
        QuotationRulesRepository(rules_path),
        pdf_service=pdf_service,
        clock=lambda: ISSUED_AT,
        quotation_id_factory=lambda prefix, issued_at: QUOTATION_ID,
    )
    return service, repository, pdf_service


def confirmed_session(service: QuotationService) -> ConversationSession:
    session = ConversationSession(
        customer_phone="+97450000000",
        customer=CustomerInfo(
            phone="+97450000000",
            name="Aisha",
            company_name="Example LLC",
        ),
        cart=[QuoteCartItem(product_id=PRODUCT_ID, quantity=2)],
        state=ConversationState.BUILDING_QUOTE,
    )
    service.prepare_quotation(session, "preview-turn")
    fingerprint = session.prepared_preview_fingerprint
    assert fingerprint is not None
    service.mark_ready_for_confirmation(session, fingerprint, "preview-turn")
    service.confirm(
        session,
        CurrentTurnContext(
            session_id=session.session_id,
            turn_id="confirmation-turn",
            customer_message="Confirmed",
            preceding_turn_ids=("preview-turn",),
        ),
    )
    return session


@pytest.mark.asyncio
async def test_authorized_generation_creates_one_backend_calculated_pdf(
    generation_dependencies: tuple[QuotationService, ProductRepository, RecordingPDFService],
) -> None:
    service, _, pdf_service = generation_dependencies
    session = confirmed_session(service)
    confirmed_fingerprint = session.prepared_preview_fingerprint

    generated = await service.generate(session)

    assert generated.quotation_id == QUOTATION_ID
    assert generated.issued_at == ISSUED_AT
    assert generated.valid_until == date(2026, 10, 3)
    assert generated.customer == session.customer
    assert generated.customer is not session.customer
    assert generated.items[0].unit_price == Decimal("25.00")
    assert generated.items[0].subtotal == Decimal("50.00")
    assert generated.totals.subtotal == Decimal("50.00")
    assert generated.totals.discount == Decimal("5.00")
    assert generated.totals.tax == Decimal("2.25")
    assert generated.totals.total == Decimal("47.25")
    assert generated.pdf_path == str(pdf_service.path)
    assert len(pdf_service.quotations) == 1
    assert pdf_service.quotations[0].model_dump() == generated.model_dump()
    assert "pdf_path" not in generated.model_dump(mode="json")
    assert session.state is ConversationState.QUOTE_GENERATED
    assert session.last_quotation_id == QUOTATION_ID
    assert session.last_generated_quote is not None
    assert session.last_generated_quote.quote_fingerprint == confirmed_fingerprint
    assert session.last_generated_quote.confirmation_turn_id == "confirmation-turn"


@pytest.mark.asyncio
async def test_repeated_generation_reuses_successful_revision_without_new_id_or_pdf(
    generation_dependencies: tuple[QuotationService, ProductRepository, RecordingPDFService],
) -> None:
    service, _, pdf_service = generation_dependencies
    session = confirmed_session(service)
    first = await service.generate(session)

    second = await service.generate(session)

    assert second.model_dump() == first.model_dump()
    assert second.pdf_path == first.pdf_path
    assert len(pdf_service.quotations) == 1


@pytest.mark.asyncio
async def test_stale_confirmation_is_rejected_before_pdf_generation(
    generation_dependencies: tuple[QuotationService, ProductRepository, RecordingPDFService],
) -> None:
    service, repository, pdf_service = generation_dependencies
    session = confirmed_session(service)
    repository._products_by_id[PRODUCT_ID].price.amount = Decimal("999.00")

    with pytest.raises(StalePreviewError):
        await service.generate(session)

    assert pdf_service.quotations == []
    assert session.state is ConversationState.AWAITING_CONFIRMATION
    assert session.last_quotation_id is None
    assert session.last_generated_quote is None


@pytest.mark.asyncio
async def test_generation_accepts_no_caller_supplied_price_or_artifact_metadata(
    generation_dependencies: tuple[QuotationService, ProductRepository, RecordingPDFService],
) -> None:
    service, _, pdf_service = generation_dependencies
    session = confirmed_session(service)
    assert tuple(inspect.signature(service.generate).parameters) == ("session",)

    with pytest.raises(TypeError):
        await service.generate(  # type: ignore[call-arg]
            session,
            unit_price=Decimal("0.01"),
            quotation_id="ATTACKER-ID",
            pdf_path="/tmp/attacker.pdf",
        )

    assert pdf_service.quotations == []
    assert session.state is ConversationState.AWAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_pdf_failure_keeps_confirmed_preview_recoverable(
    generation_dependencies: tuple[QuotationService, ProductRepository, RecordingPDFService],
) -> None:
    service, _, pdf_service = generation_dependencies
    session = confirmed_session(service)
    pdf_service.fail = True

    with pytest.raises(QuotationGenerationError):
        await service.generate(session)

    assert len(pdf_service.quotations) == 1
    assert session.state is ConversationState.AWAITING_CONFIRMATION
    assert session.quote_confirmed is True
    assert session.last_quotation_id is None
    assert session.last_generated_quote is None

    pdf_service.fail = False
    recovered = await service.generate(session)

    assert recovered.quotation_id == QUOTATION_ID
    assert len(pdf_service.quotations) == 2
    assert session.state.value == ConversationState.QUOTE_GENERATED.value
