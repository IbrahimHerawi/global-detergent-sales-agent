"""Backend-only quotation identifier generation."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.schemas.quotation_rules import QuotationRules

_PREFIX_PATTERN: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")
_TOKEN_PATTERN: Final = re.compile(r"^[0-9A-F]{4}$")
_MAX_PREFIX_LENGTH: Final = 64

Clock = Callable[[], datetime]
TokenHexFactory = Callable[[int], str]


def generate_quotation_id(
    prefix: str,
    *,
    now: datetime | None = None,
    token_hex_factory: TokenHexFactory | None = None,
) -> str:
    """Return ``PREFIX-UTC_YYYYMMDD-XXXX`` using backend randomness."""
    validated_prefix = _validate_prefix(prefix)
    issued_at = datetime.now(UTC) if now is None else _normalize_utc(now)
    token_factory = token_hex_factory or secrets.token_hex
    token = token_factory(2).upper()
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("token_hex_factory must return exactly four hexadecimal characters")
    return f"{validated_prefix}-{issued_at:%Y%m%d}-{token}"


@dataclass(frozen=True, slots=True)
class QuotationIdGenerator:
    """Callable ID generator configured from authoritative quotation rules."""

    prefix: str
    clock: Clock = lambda: datetime.now(UTC)
    token_hex_factory: TokenHexFactory = secrets.token_hex

    def __post_init__(self) -> None:
        _validate_prefix(self.prefix)

    @classmethod
    def from_rules(
        cls,
        rules: QuotationRules,
        *,
        clock: Clock | None = None,
        token_hex_factory: TokenHexFactory | None = None,
    ) -> QuotationIdGenerator:
        """Build a generator from the configured quotation prefix."""
        return cls(
            prefix=rules.quotation_prefix,
            clock=clock or (lambda: datetime.now(UTC)),
            token_hex_factory=token_hex_factory or secrets.token_hex,
        )

    def __call__(self) -> str:
        return generate_quotation_id(
            self.prefix,
            now=self.clock(),
            token_hex_factory=self.token_hex_factory,
        )


def _validate_prefix(prefix: str) -> str:
    if (
        not isinstance(prefix, str)
        or len(prefix) > _MAX_PREFIX_LENGTH
        or not _PREFIX_PATTERN.fullmatch(prefix)
    ):
        raise ValueError("quotation prefix is not safe for identifiers")
    return prefix


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quotation ID time must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["QuotationIdGenerator", "generate_quotation_id"]
