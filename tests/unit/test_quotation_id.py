"""Tests for backend-generated UTC quotation identifiers."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.schemas.quotation_rules import QuotationRules
from app.services.quotation_id import QuotationIdGenerator, generate_quotation_id


def test_fixed_clock_id_uses_configured_prefix_utc_date_and_uppercase_token() -> None:
    issued_at = datetime(2026, 9, 19, 23, 45, tzinfo=UTC)

    quotation_id = generate_quotation_id(
        "GDF-Q",
        now=issued_at,
        token_hex_factory=lambda byte_count: "a82f" if byte_count == 2 else "",
    )

    assert quotation_id == "GDF-Q-20260919-A82F"


def test_id_date_is_normalized_to_utc() -> None:
    plus_three = timezone(timedelta(hours=3))

    quotation_id = generate_quotation_id(
        "GDF-Q",
        now=datetime(2026, 9, 20, 1, 30, tzinfo=plus_three),
        token_hex_factory=lambda _: "cafe",
    )

    assert quotation_id == "GDF-Q-20260919-CAFE"


def test_generator_uses_prefix_from_validated_rules() -> None:
    rules = QuotationRules(
        currency="QAR",
        quotation_prefix="FACTORY_QUOTE",
        validity_days=14,
        default_discount_percent=Decimal("0.00"),
        tax_percent=Decimal("0.00"),
        terms=("Subject to availability.",),
    )
    generator = QuotationIdGenerator.from_rules(
        rules,
        clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        token_hex_factory=lambda _: "00ff",
    )

    assert generator() == "FACTORY_QUOTE-20260102-00FF"


@pytest.mark.parametrize(
    ("prefix", "token"),
    [
        ("../escape", "A82F"),
        ("bad/prefix", "A82F"),
        ("GDF-Q", "XYZ1"),
        ("GDF-Q", "ABCDEF"),
    ],
)
def test_unsafe_prefixes_and_invalid_random_tokens_are_rejected(
    prefix: str,
    token: str,
) -> None:
    with pytest.raises(ValueError):
        generate_quotation_id(
            prefix,
            now=datetime(2026, 9, 19, tzinfo=UTC),
            token_hex_factory=lambda _: token,
        )


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_quotation_id(
            "GDF-Q",
            now=datetime(2026, 9, 19),
            token_hex_factory=lambda _: "A82F",
        )
