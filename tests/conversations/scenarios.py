"""Reusable, source-traceable conversation acceptance scenarios.

The scripted suite and opt-in live-model evaluation both consume these cases.  A
case describes customer intent and observable behavior; it deliberately does not
prescribe exact assistant wording.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

SetupName = Literal[
    "new",
    "selected_floor",
    "selected_disinfectant",
    "floor_cart",
    "two_item_cart",
    "named_floor_cart",
    "confirmable_floor_cart",
]


@dataclass(frozen=True, slots=True)
class ExpectedToolCall:
    """One deterministic tool call returned by the scripted model."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ConversationScenario:
    """One acceptance case shared by offline and live-model tests."""

    scenario_id: str
    title: str
    sources: tuple[str, ...]
    setup: SetupName
    message: str
    scripted_calls: tuple[ExpectedToolCall, ...]
    required_live_tools: tuple[str, ...] = ()
    prohibited_live_tools: tuple[str, ...] = ()


def call(tool_name: str, **arguments: object) -> ExpectedToolCall:
    """Build stable compact JSON arguments for a strict function tool."""
    return ExpectedToolCall(
        name=tool_name,
        arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )


EMPTY_CALL = "{}"
FLOOR_PRODUCT_ID: Final = "GDF-FLC-001"
GLASS_PRODUCT_ID: Final = "GDF-GLC-001"
DISINFECTANT_PRODUCT_ID: Final = "GDF-SDI-001"
UNKNOWN_PRODUCT_QUERY: Final = "industrial carpet shampoo"
FIXED_QUOTATION_ID: Final = "GDF-Q-20260920-A47F"


SCENARIOS: Final[tuple[ConversationScenario, ...]] = (
    ConversationScenario(
        "TP66-A",
        "Company introduction",
        ("TP-66-A", "S-1", "S-8", "S-21"),
        "new",
        "What is Global Detergent Factory?",
        (call("get_company_information"),),
        ("get_company_information",),
    ),
    ConversationScenario(
        "TP66-B",
        "Office-floor discovery",
        ("TP-66-B", "S-1", "S-21", "S-22"),
        "new",
        "I need something for cleaning office floors.",
        (
            call("search_products", query="office floors"),
            call("get_product_details", product_id=FLOOR_PRODUCT_ID),
        ),
        ("search_products",),
        ("add_quote_item", "create_quotation"),
    ),
    ConversationScenario(
        "SPEC-DETAILS",
        "Selected product details",
        ("S-1", "S-11", "S-21", "S-39"),
        "selected_floor",
        "Please give me the specifications and package details.",
        (call("get_product_details", product_id=FLOOR_PRODUCT_ID),),
        ("get_product_details",),
    ),
    ConversationScenario(
        "TP66-C",
        "Correct catalog price",
        ("TP-66-C", "S-15", "S-21", "S-39"),
        "selected_floor",
        "How much is the 5 L floor cleaner?",
        (call("get_product_details", product_id=FLOOR_PRODUCT_ID),),
        ("get_product_details",),
    ),
    ConversationScenario(
        "TP66-D",
        "Quantity for selected product",
        ("TP-66-D", "S-14", "S-39", "D-03"),
        "selected_floor",
        "I need 20.",
        (call("add_quote_item", product_id=FLOOR_PRODUCT_ID, quantity=20),),
        ("add_quote_item",),
        ("update_quote_item_quantity",),
    ),
    ConversationScenario(
        "TP66-E",
        "Multiple products",
        ("TP-66-E", "S-14", "S-15", "S-39"),
        "floor_cart",
        "Add 10 units of the 5 L glass cleaner too.",
        (
            call("get_product_details", product_id=GLASS_PRODUCT_ID),
            call("add_quote_item", product_id=GLASS_PRODUCT_ID, quantity=10),
            call("get_quote_summary"),
        ),
        ("add_quote_item",),
    ),
    ConversationScenario(
        "TP66-F",
        "Quantity replacement",
        ("TP-66-F", "TP-24", "D-03", "SG-01"),
        "floor_cart",
        "Change the floor cleaner quantity from 20 to 25.",
        (call("update_quote_item_quantity", product_id=FLOOR_PRODUCT_ID, quantity=25),),
        ("update_quote_item_quantity",),
        ("add_quote_item",),
    ),
    ConversationScenario(
        "SPEC-REMOVE",
        "Remove a cart item",
        ("S-14", "S-20", "S-39", "TP-42"),
        "two_item_cart",
        "Remove the glass cleaner from the quotation.",
        (call("remove_quote_item", product_id=GLASS_PRODUCT_ID),),
        ("remove_quote_item",),
    ),
    ConversationScenario(
        "SPEC-CUSTOMER",
        "Collect customer company name",
        ("S-18", "S-39", "D-04"),
        "floor_cart",
        "The company name is ABC Facility Management.",
        (
            call(
                "set_customer_information",
                fields_to_update=["company_name"],
                name=None,
                company_name="ABC Facility Management",
                contact_person=None,
                email=None,
                address=None,
                notes=None,
            ),
        ),
        ("set_customer_information",),
    ),
    ConversationScenario(
        "SPEC-PREVIEW",
        "Backend quotation preview",
        ("S-19", "S-39", "D-05", "SG-03"),
        "named_floor_cart",
        "Please prepare the quotation preview.",
        (call("prepare_quotation"),),
        ("prepare_quotation",),
        ("confirm_quotation", "create_quotation"),
    ),
    ConversationScenario(
        "TP66-G",
        "Unknown certification",
        ("TP-66-G", "S-11", "S-32"),
        "selected_disinfectant",
        "Is this surface disinfectant ISO 9001 certified?",
        (call("get_product_details", product_id=DISINFECTANT_PRODUCT_ID),),
        ("get_product_details",),
        ("add_quote_item",),
    ),
    ConversationScenario(
        "SPEC-MISSING-TECHNICAL",
        "Unavailable dilution and contact time",
        ("S-11", "S-32", "TP-39"),
        "selected_disinfectant",
        "What dilution ratio and contact time should I use?",
        (call("get_product_details", product_id=DISINFECTANT_PRODUCT_ID),),
        ("get_product_details",),
    ),
    ConversationScenario(
        "SPEC-UNKNOWN-PRODUCT",
        "Unknown product",
        ("S-1", "S-2", "S-32"),
        "new",
        f"Do you sell {UNKNOWN_PRODUCT_QUERY}?",
        (call("search_products", query=UNKNOWN_PRODUCT_QUERY),),
        ("search_products",),
        ("add_quote_item",),
    ),
    ConversationScenario(
        "SPEC-AMBIGUOUS-SELECTION",
        "Ambiguous product selection",
        ("S-19", "S-23", "TP-34"),
        "new",
        "Add 5 of the cleaner.",
        (call("search_products", query="cleaner"),),
        (),
        ("add_quote_item", "update_quote_item_quantity"),
    ),
    ConversationScenario(
        "SPEC-TENTATIVE-QUANTITY",
        "Tentative quantity is not a cart instruction",
        ("S-19", "S-23", "TP-34"),
        "selected_floor",
        "I might need 20.",
        (),
        (),
        ("add_quote_item", "update_quote_item_quantity", "confirm_quotation"),
    ),
    ConversationScenario(
        "TP66-H",
        "Premature yes cannot generate",
        ("TP-66-H", "S-19", "TP-21", "D-05"),
        "floor_cart",
        "Yes.",
        (),
        (),
        ("confirm_quotation", "create_quotation"),
    ),
    ConversationScenario(
        "SPEC-MODIFY-AFTER-PREVIEW",
        "Modification after preview invalidates authorization",
        ("S-19", "TP-24", "SG-01"),
        "confirmable_floor_cart",
        "Actually change the floor cleaner quantity to 25.",
        (call("update_quote_item_quantity", product_id=FLOOR_PRODUCT_ID, quantity=25),),
        ("update_quote_item_quantity",),
        ("confirm_quotation", "create_quotation"),
    ),
    ConversationScenario(
        "TP66-I",
        "Explicit confirmation and PDF generation",
        ("TP-66-I", "S-17", "S-19", "S-30", "S-39"),
        "confirmable_floor_cart",
        "Confirmed.",
        (call("confirm_quotation"), call("create_quotation")),
        ("confirm_quotation", "create_quotation"),
    ),
)

SCENARIOS_BY_ID: Final = {scenario.scenario_id: scenario for scenario in SCENARIOS}

SPECIFICATION_JOURNEY: Final[tuple[str, ...]] = (
    "TP66-A",
    "TP66-B",
    "SPEC-DETAILS",
    "TP66-C",
    "TP66-D",
    "TP66-E",
    "TP66-F",
    "SPEC-REMOVE",
    "SPEC-CUSTOMER",
    "SPEC-PREVIEW",
    "TP66-I",
)


__all__ = [
    "DISINFECTANT_PRODUCT_ID",
    "FIXED_QUOTATION_ID",
    "FLOOR_PRODUCT_ID",
    "GLASS_PRODUCT_ID",
    "SCENARIOS",
    "SCENARIOS_BY_ID",
    "SPECIFICATION_JOURNEY",
    "UNKNOWN_PRODUCT_QUERY",
    "ConversationScenario",
    "ExpectedToolCall",
]
