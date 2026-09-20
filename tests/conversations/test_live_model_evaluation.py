"""Opt-in behavioral evaluation against the configured live OpenAI model.

These tests are intentionally excluded from the offline regression command.
They reuse the scripted scenario catalog because mocked tool choices cannot
demonstrate how a real model interprets customer language.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from app.core.config import Settings
from app.schemas.quotation import GeneratedQuotation
from app.schemas.session import ConversationState

from tests.conversations.conftest import (
    ConversationHarness,
    build_conversation_harness,
)
from tests.conversations.scenarios import (
    FIXED_QUOTATION_ID,
    FLOOR_PRODUCT_ID,
    SCENARIOS,
    ConversationScenario,
)

pytestmark = pytest.mark.live_model

KNOWN_PRODUCT_NAMES = (
    "GDF Multi-Purpose Cleaner",
    "GDF Floor Cleaner",
    "GDF Glass Cleaner",
    "GDF Surface Disinfectant",
    "GDF Hand Sanitizer",
    "GDF Hand Wash",
    "GDF Dishwashing Liquid",
    "GDF Toilet & Bathroom Cleaner",
    "GDF Degreaser",
    "GDF Laundry Detergent",
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.scenario_id)
async def test_live_model_scenario_behavior(
    scenario: ConversationScenario,
    live_model_settings: Settings,
    tmp_path: Path,
) -> None:
    """Evaluate real tool choice, facts, state, and confirmation interpretation."""
    harness = build_conversation_harness(
        scenario,
        tmp_path / scenario.scenario_id / "quotes",
        live_settings=live_model_settings,
    )

    try:
        result = await harness.send(scenario.message)
    finally:
        await harness.aclose()
    names = [record.name for record in harness.recorder.calls]

    for required_tool in scenario.required_live_tools:
        assert required_tool in names, (
            f"{scenario.scenario_id} did not call required tool {required_tool!r}; called {names!r}"
        )
    for prohibited_tool in scenario.prohibited_live_tools:
        assert prohibited_tool not in names, (
            f"{scenario.scenario_id} called prohibited tool {prohibited_tool!r}; called {names!r}"
        )

    _assert_factual_support(scenario, harness, result.response_text)
    _assert_confirmation_interpretation(scenario, harness, result.generated_quote)


def _assert_factual_support(
    scenario: ConversationScenario,
    harness: ConversationHarness,
    response_text: str,
) -> None:
    """Check model claims against facts actually returned during this turn."""
    assert response_text.strip()
    lower_response = response_text.casefold()
    outputs = [record.result.model_output for record in harness.recorder.calls]
    serialized_outputs = " ".join(str(output) for output in outputs).casefold()

    # Any explicit catalog product name in the answer must be supported by a
    # current tool result, rather than model memory or another fixture case.
    mentioned_products = {
        product_name
        for product_name in KNOWN_PRODUCT_NAMES
        if product_name.casefold() in lower_response
    }
    for product_name in mentioned_products:
        assert product_name.casefold() in serialized_outputs

    if scenario.scenario_id == "TP66-A":
        assert "global detergent factory" in lower_response or "gdf" in lower_response
        assert "global detergent factory" in serialized_outputs
    elif scenario.scenario_id == "TP66-C":
        assert "25" in response_text
        assert "qar" in lower_response
        assert "'amount': '25.00'" in serialized_outputs
    elif scenario.scenario_id == "TP66-G":
        assert "'certifications': []" in serialized_outputs
        assert not re.search(
            r"\b(?:is|has|holds)\s+(?:an?\s+)?iso\s*9001",
            lower_response,
        )
        assert not re.search(
            r"\b(?:isn't|is\s+not|doesn't\s+have|does\s+not\s+have)\b"
            r"[^.]*\biso\s*9001\b",
            lower_response,
        )
    elif scenario.scenario_id == "SPEC-MISSING-TECHNICAL":
        assert "'contact_time': none" in serialized_outputs
        assert "'dilution_ratio': none" in serialized_outputs
        assert re.search(r"\b\d+\s*:\s*\d+\b", lower_response) is None
        assert (
            re.search(
                r"\b\d+(?:\.\d+)?\s*(?:seconds?|minutes?|hours?)\b",
                lower_response,
            )
            is None
        )
    elif scenario.scenario_id == "SPEC-UNKNOWN-PRODUCT":
        assert outputs and outputs[-1] == {"matches": []}
        assert not re.search(
            r"\b(?:we|gdf)\s+(?:sell|stock|carry|offer)(?:s)?\b",
            lower_response,
        )


def _assert_confirmation_interpretation(
    scenario: ConversationScenario,
    harness: ConversationHarness,
    generated_quote: GeneratedQuotation | None,
) -> None:
    """Explicitly distinguish confirmation from tentative or premature intent."""
    session = harness.session
    scenario_id = scenario.scenario_id

    if scenario_id == "SPEC-TENTATIVE-QUANTITY":
        assert session.cart == []
        assert session.quote_confirmed is False
    elif scenario_id == "TP66-H":
        assert session.quote_confirmed is False
        assert session.last_quotation_id is None
        assert session.state is not ConversationState.QUOTE_GENERATED
        assert generated_quote is None
    elif scenario_id == "SPEC-MODIFY-AFTER-PREVIEW":
        assert [(item.product_id, item.quantity) for item in session.cart] == [
            (FLOOR_PRODUCT_ID, 25)
        ]
        assert session.quote_confirmed is False
        assert session.preview_delivered is False
        assert session.prepared_preview_fingerprint is None
        assert session.state is ConversationState.BUILDING_QUOTE
    elif scenario_id == "TP66-I":
        assert generated_quote is not None
        assert session.quote_confirmed is True
        assert session.state is ConversationState.QUOTE_GENERATED
        assert session.last_quotation_id == FIXED_QUOTATION_ID
        assert Path(generated_quote.pdf_path).is_file()


def test_live_evaluation_reports_effective_configuration(
    live_model_settings: Settings,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    """Put model/config values in console header and machine-readable JUnit output."""
    record_testsuite_property("live_model", str(live_model_settings.openai_model))
    record_testsuite_property(
        "openai_timeout_seconds",
        live_model_settings.openai_timeout_seconds,
    )
    record_testsuite_property(
        "max_tool_iterations",
        live_model_settings.max_tool_iterations,
    )
    assert live_model_settings.openai_model
