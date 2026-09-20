"""Deterministic conversation regression tests with scripted OpenAI choices.

Only the model response is mocked.  Tool validation, repositories, quotation
state/calculation, confirmation binding, PDF rendering, and artifact storage are
the production implementations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.config import Settings
from app.schemas.agent import AgentResult
from app.schemas.quotation import QuoteCartItem
from app.schemas.session import ConversationState
from app.services.session_service import SessionService
from redis.asyncio import Redis

from tests.conversations.conftest import ConversationHarness
from tests.conversations.scenarios import (
    FIXED_QUOTATION_ID,
    FLOOR_PRODUCT_ID,
    GLASS_PRODUCT_ID,
    SCENARIOS,
    SCENARIOS_BY_ID,
    SPECIFICATION_JOURNEY,
    UNKNOWN_PRODUCT_QUERY,
    ConversationScenario,
    call,
)


def tool_names(harness: ConversationHarness) -> list[str]:
    return [record.name for record in harness.recorder.calls]


def tool_output(harness: ConversationHarness, name: str) -> dict[str, object]:
    matches = [
        record.result.model_output for record in harness.recorder.calls if record.name == name
    ]
    assert matches, f"tool {name!r} was not recorded"
    return matches[-1]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.scenario_id)
async def test_scripted_scenario_pipeline(
    scenario: ConversationScenario,
    conversation_harness_factory: Callable[[ConversationScenario], ConversationHarness],
) -> None:
    """Assert tools and authoritative outcomes without coupling to model prose."""
    harness = conversation_harness_factory(scenario)
    result = await harness.send(
        scenario.message,
        scripted_calls=scenario.scripted_calls,
    )

    expected_names = [call.name for call in scenario.scripted_calls]
    expected_arguments = [call.arguments for call in scenario.scripted_calls]
    if "prepare_quotation" in expected_names:
        # The production agent obtains service-owned deterministic preview text
        # after preparation rather than asking the model to reproduce totals.
        expected_names.append("get_quote_summary")
        expected_arguments.append("{}")
    assert tool_names(harness) == expected_names
    assert [record.arguments for record in harness.recorder.calls] == expected_arguments
    assert result.updated_session is harness.session
    assert harness.scripted_responses is not None
    assert harness.scripted_responses.outcomes == []
    _assert_scenario_outcome(scenario, harness, result)


def _assert_scenario_outcome(
    scenario: ConversationScenario,
    harness: ConversationHarness,
    result: AgentResult,
) -> None:
    session = harness.session
    scenario_id = scenario.scenario_id

    if scenario_id == "TP66-A":
        company = tool_output(harness, "get_company_information")
        assert company["name"] == "Global Detergent Factory"
        assert company["contact"] == {
            "phone": None,
            "email": None,
            "website": None,
            "address": None,
        }
    elif scenario_id == "TP66-B":
        matches = tool_output(harness, "search_products")["matches"]
        assert isinstance(matches, list)
        assert any(item["product_id"] == FLOOR_PRODUCT_ID for item in matches)
        assert session.selected_product_id == FLOOR_PRODUCT_ID
    elif scenario_id in {"SPEC-DETAILS", "TP66-C"}:
        details = tool_output(harness, "get_product_details")
        assert details["product_id"] == FLOOR_PRODUCT_ID
        assert details["packaging"] == {"size": "5", "unit": "L"}
        assert details["price"] == {"amount": "25.00", "currency": "QAR"}
    elif scenario_id == "TP66-D":
        assert [(item.product_id, item.quantity) for item in session.cart] == [
            (FLOOR_PRODUCT_ID, 20)
        ]
    elif scenario_id == "TP66-E":
        assert [(item.product_id, item.quantity) for item in session.cart] == [
            (FLOOR_PRODUCT_ID, 20),
            (GLASS_PRODUCT_ID, 10),
        ]
        summary = tool_output(harness, "get_quote_summary")["summary"]
        assert isinstance(summary, str)
        assert "QAR 500.00" in summary
        assert "QAR 220.00" in summary
        assert "Total: QAR 720.00" in summary
    elif scenario_id == "TP66-F":
        assert [(item.product_id, item.quantity) for item in session.cart] == [
            (FLOOR_PRODUCT_ID, 25)
        ]
        assert session.state is ConversationState.BUILDING_QUOTE
        assert session.quote_confirmed is False
    elif scenario_id == "SPEC-REMOVE":
        assert [(item.product_id, item.quantity) for item in session.cart] == [
            (FLOOR_PRODUCT_ID, 20)
        ]
        assert session.state is ConversationState.BUILDING_QUOTE
    elif scenario_id == "SPEC-CUSTOMER":
        assert session.customer.company_name == "ABC Facility Management"
        assert session.customer.phone == "+97450000000"
        assert session.state is ConversationState.BUILDING_QUOTE
    elif scenario_id == "SPEC-PREVIEW":
        assert result.prepared_preview is not None
        assert session.state is ConversationState.AWAITING_CONFIRMATION
        assert session.preview_delivered is True
        assert session.quote_confirmed is False
        preview = tool_output(harness, "prepare_quotation")
        assert preview["quote_confirmed"] is False
        assert preview["totals"] == {
            "subtotal": "500.00",
            "discount": "0.00",
            "tax": "0.00",
            "total": "500.00",
            "currency": "QAR",
        }
    elif scenario_id == "TP66-G":
        details = tool_output(harness, "get_product_details")
        assert details["certifications"] == []
        assert details["approved_claims"] == []
    elif scenario_id == "SPEC-MISSING-TECHNICAL":
        details = tool_output(harness, "get_product_details")
        assert details["dilution_ratio"] is None
        assert details["contact_time"] is None
    elif scenario_id == "SPEC-UNKNOWN-PRODUCT":
        search = tool_output(harness, "search_products")
        assert search == {"matches": []}
        assert UNKNOWN_PRODUCT_QUERY not in {item.product_id for item in session.cart}
    elif scenario_id == "SPEC-AMBIGUOUS-SELECTION":
        search = tool_output(harness, "search_products")
        matches = search["matches"]
        assert isinstance(matches, list)
        assert len(matches) > 1
        assert session.cart == []
        assert session.selected_product_id is None
    elif scenario_id == "SPEC-TENTATIVE-QUANTITY":
        assert session.cart == []
        assert session.selected_product_id == FLOOR_PRODUCT_ID
    elif scenario_id == "TP66-H":
        assert session.state is ConversationState.BUILDING_QUOTE
        assert session.quote_confirmed is False
        assert session.last_quotation_id is None
        assert result.generated_quote is None
    elif scenario_id == "SPEC-MODIFY-AFTER-PREVIEW":
        assert [(item.product_id, item.quantity) for item in session.cart] == [
            (FLOOR_PRODUCT_ID, 25)
        ]
        assert session.state is ConversationState.BUILDING_QUOTE
        assert session.quote_confirmed is False
        assert session.preview_delivered is False
        assert session.prepared_preview_fingerprint is None
        assert session.confirmation_turn_id is None
    elif scenario_id == "TP66-I":
        generated = result.generated_quote
        assert generated is not None
        assert generated.quotation_id == FIXED_QUOTATION_ID
        assert generated.totals.subtotal == Decimal("500.00")
        assert generated.totals.total == Decimal("500.00")
        assert session.state is ConversationState.QUOTE_GENERATED
        assert session.quote_confirmed is True
        assert session.last_quotation_id == FIXED_QUOTATION_ID
        artifact = Path(generated.pdf_path)
        assert artifact.is_file()
        assert artifact.stat().st_size > 0
        assert artifact.read_bytes().startswith(b"%PDF-")
        create_output = tool_output(harness, "create_quotation")
        assert create_output == {
            "quotation_id": FIXED_QUOTATION_ID,
            "total": "500.00",
            "currency": "QAR",
            "pdf_generated": True,
        }
        assert "pdf_path" not in create_output
    else:  # pragma: no cover - forces additions to define their regression oracle
        raise AssertionError(f"missing assertions for {scenario_id}")


async def test_complete_specification_journey_preserves_context_and_generates_pdf(
    conversation_harness_factory: Callable[[ConversationScenario], ConversationHarness],
) -> None:
    """Run the source Specification journey as one continuous session."""
    harness = conversation_harness_factory(SCENARIOS_BY_ID["TP66-A"])

    for scenario_id in SPECIFICATION_JOURNEY:
        scenario = SCENARIOS_BY_ID[scenario_id]
        await harness.send(
            scenario.message,
            scripted_calls=scenario.scripted_calls,
        )

    session = harness.session
    generated = harness.results[-1].generated_quote
    assert generated is not None
    assert [(item.product_id, item.quantity) for item in session.cart] == [(FLOOR_PRODUCT_ID, 25)]
    assert session.customer.company_name == "ABC Facility Management"
    assert generated.totals.subtotal == Decimal("625.00")
    assert generated.totals.discount == Decimal("0.00")
    assert generated.totals.tax == Decimal("0.00")
    assert generated.totals.total == Decimal("625.00")
    assert generated.quotation_id == FIXED_QUOTATION_ID
    assert Path(generated.pdf_path).is_file()
    assert session.state is ConversationState.QUOTE_GENERATED
    assert session.quote_confirmed is True


async def test_backend_rejects_model_attempt_to_generate_without_confirmation(
    conversation_harness_factory: Callable[[ConversationScenario], ConversationHarness],
) -> None:
    """TP-66-H is enforced by Python even when a mocked model chooses create."""
    scenario = SCENARIOS_BY_ID["TP66-H"]
    harness = conversation_harness_factory(scenario)

    result = await harness.send(
        scenario.message,
        scripted_calls=(call("create_quotation"),),
    )

    output = tool_output(harness, "create_quotation")
    assert output["status"] == "error"
    assert output["error"] == {
        "code": "customer_information_required",
        "message": "Customer information is required before creating a quotation.",
    }
    assert result.generated_quote is None
    assert harness.session.last_quotation_id is None
    assert harness.session.state is ConversationState.CUSTOMER_DETAILS


async def test_expired_session_loses_selected_product_cart_and_confirmation(
    conversation_redis: Redis,
) -> None:
    """S-12: an expired Redis session starts fresh instead of reviving quote state."""
    service = SessionService(
        conversation_redis,
        settings=Settings(
            app_env="test",
            redis_url="redis://redis:6379/15",
            session_ttl_seconds=1,
        ),
    )
    original = await service.get_session("+97450000091")
    original.selected_product_id = FLOOR_PRODUCT_ID
    original.state = ConversationState.BUILDING_QUOTE
    original.cart = [QuoteCartItem(product_id=FLOOR_PRODUCT_ID, quantity=20)]
    await service.save_session(original)

    await asyncio.sleep(1.1)
    fresh = await service.get_session("+97450000091")

    assert fresh.session_id != original.session_id
    assert fresh.state is ConversationState.NEW
    assert fresh.selected_product_id is None
    assert fresh.cart == []
    assert fresh.quote_confirmed is False
    assert fresh.prepared_preview_fingerprint is None


def test_scenario_catalog_is_complete_and_source_traceable() -> None:
    """Make missing §66 cases or untraced scenarios a visible suite failure."""
    scenario_ids = [scenario.scenario_id for scenario in SCENARIOS]
    assert len(scenario_ids) == len(set(scenario_ids))
    assert {f"TP66-{letter}" for letter in "ABCDEFGHI"} <= set(scenario_ids)
    assert set(SPECIFICATION_JOURNEY) <= set(scenario_ids)
    assert all(scenario.sources for scenario in SCENARIOS)
    assert all(
        source.startswith(("S-", "TP-", "D-", "SG-"))
        for scenario in SCENARIOS
        for source in scenario.sources
    )
