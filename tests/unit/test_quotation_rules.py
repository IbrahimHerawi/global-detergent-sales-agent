"""Tests for quotation-rule validation and cached loading."""

import json
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path

import pytest
from app.core.config import Settings, override_settings
from app.core.exceptions import TECHNICAL_FALLBACK_MESSAGE, InvalidStaticDataError
from app.repositories.quotation_rules_repository import QuotationRulesRepository
from app.schemas.quotation_rules import QuotationRules
from pydantic import ValidationError

TERMS = (
    "Prices are valid for the quotation validity period.",
    "Availability is subject to confirmation.",
)


def rules_payload() -> dict[str, object]:
    """Return the documented development quotation rules."""
    return {
        "currency": "QAR",
        "quotation_prefix": "GDF-Q",
        "validity_days": 30,
        "default_discount_percent": "0.00",
        "tax_percent": "0.00",
        "terms": list(TERMS),
    }


def write_rules(path: Path, payload: object) -> None:
    """Write a JSON rules fixture."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_documented_rules_validate_and_serialize_decimal_strings() -> None:
    rules = QuotationRules.model_validate(rules_payload())
    serialized = json.loads(rules.model_dump_json())

    assert rules.currency == "QAR"
    assert rules.quotation_prefix == "GDF-Q"
    assert rules.validity_days == 30
    assert rules.default_discount_percent == Decimal("0.00")
    assert rules.tax_percent == Decimal("0.00")
    assert rules.terms == TERMS
    assert serialized == rules_payload()


def test_currency_is_normalized_to_an_uppercase_three_letter_code() -> None:
    payload = rules_payload()
    payload["currency"] = " qar "

    assert QuotationRules.model_validate(payload).currency == "QAR"


@pytest.mark.parametrize("currency", ["", "US", "USDD", "12A", True])
def test_malformed_currency_is_rejected(currency: object) -> None:
    payload = rules_payload()
    payload["currency"] = currency

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


@pytest.mark.parametrize(
    "prefix",
    ["", "   ", "../GDF-Q", "GDF/Q", "GDF\\Q", ".GDF-Q", "GDF-Q.", "GDF Q", "GDF:Q"],
)
def test_quotation_prefix_rejects_filename_unsafe_values(prefix: str) -> None:
    payload = rules_payload()
    payload["quotation_prefix"] = prefix

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


def test_quotation_prefix_has_a_bounded_length() -> None:
    payload = rules_payload()
    payload["quotation_prefix"] = "A" * 65

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


@pytest.mark.parametrize("validity_days", [0, -1, True, False, "30", 30.0])
def test_validity_days_must_be_a_positive_strict_integer(validity_days: object) -> None:
    payload = rules_payload()
    payload["validity_days"] = validity_days

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_discount_percent", "-0.01"),
        ("default_discount_percent", "100.01"),
        ("default_discount_percent", "NaN"),
        ("default_discount_percent", "Infinity"),
        ("default_discount_percent", "not-a-number"),
        ("tax_percent", "-0.01"),
        ("tax_percent", "NaN"),
        ("tax_percent", "Infinity"),
        ("tax_percent", "not-a-number"),
    ],
)
def test_invalid_percentage_values_are_rejected(field: str, value: str) -> None:
    payload = rules_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


@pytest.mark.parametrize("field", ["default_discount_percent", "tax_percent"])
@pytest.mark.parametrize("value", [0.0, float("nan"), float("inf"), True, False])
def test_float_and_boolean_percentages_are_rejected(field: str, value: object) -> None:
    payload = rules_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


def test_percentage_boundaries_are_accepted() -> None:
    payload = rules_payload()
    payload["default_discount_percent"] = "100"
    payload["tax_percent"] = "125.50"

    rules = QuotationRules.model_validate(payload)

    assert rules.default_discount_percent == Decimal("100")
    assert rules.tax_percent == Decimal("125.50")


def test_terms_are_explicit_and_nonblank() -> None:
    payload = rules_payload()
    payload["terms"] = ["   "]

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


@pytest.mark.parametrize("field", list(rules_payload()))
def test_every_commercial_rule_is_required(field: str) -> None:
    payload = rules_payload()
    del payload[field]

    with pytest.raises(ValidationError):
        QuotationRules.model_validate(payload)


def test_unknown_fields_are_rejected() -> None:
    payload = rules_payload()
    payload["automatic_discount_authority"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QuotationRules.model_validate(payload)


def test_rules_are_deeply_read_only() -> None:
    rules = QuotationRules.model_validate(rules_payload())

    with pytest.raises(ValidationError, match="Instance is frozen"):
        rules.tax_percent = Decimal("5.00")

    assert isinstance(rules.terms, tuple)


def test_repository_loads_configured_container_path_once_and_caches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quotation_rules.json"
    write_rules(path, rules_payload())
    settings = Settings.model_validate({"quote_rules_path": path})

    with override_settings(settings):
        repository = QuotationRulesRepository()

    cached = repository.get_rules()
    path.write_text("not valid JSON anymore", encoding="utf-8")

    assert cached is repository.get_rules()
    assert cached.currency == "QAR"
    assert cached.terms == TERMS


def test_repository_reports_missing_file_with_internal_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(InvalidStaticDataError) as raised:
        QuotationRulesRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert str(path) in error.diagnostic_detail
    assert "Unable to read quotation rules" in error.diagnostic_detail
    assert isinstance(error.cause, FileNotFoundError)
    assert str(error) == TECHNICAL_FALLBACK_MESSAGE


def test_repository_reports_malformed_json_location(tmp_path: Path) -> None:
    path = tmp_path / "quotation_rules.json"
    path.write_text('{"currency":', encoding="utf-8")

    with pytest.raises(InvalidStaticDataError) as raised:
        QuotationRulesRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "Malformed quotation rules JSON" in error.diagnostic_detail
    assert "line 1" in error.diagnostic_detail
    assert "column" in error.diagnostic_detail
    assert isinstance(error.cause, JSONDecodeError)


def test_repository_reports_validation_fields_without_raw_input(tmp_path: Path) -> None:
    path = tmp_path / "quotation_rules.json"
    payload = rules_payload()
    payload["validity_days"] = False
    payload["invented_tax_policy"] = "unknown"
    write_rules(path, payload)

    with pytest.raises(InvalidStaticDataError) as raised:
        QuotationRulesRepository(path)

    error = raised.value
    assert error.diagnostic_detail is not None
    assert "Invalid quotation rules" in error.diagnostic_detail
    assert "validity_days" in error.diagnostic_detail
    assert "invented_tax_policy" in error.diagnostic_detail
    assert isinstance(error.cause, ValidationError)
