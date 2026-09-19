"""Read-only repository for validated quotation rules."""

from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import InvalidStaticDataError
from app.schemas.quotation_rules import QuotationRules


class QuotationRulesRepository:
    """Load and cache one immutable quotation-rules document."""

    __slots__ = ("_rules",)

    def __init__(self, path: Path | None = None) -> None:
        configured_path = get_settings().quote_rules_path if path is None else path
        self._rules = _load_rules(configured_path)

    def get_rules(self) -> QuotationRules:
        """Return the cached, validated rules without rereading the source file."""
        return self._rules


def _load_rules(path: Path) -> QuotationRules:
    """Read and validate a quotation-rules JSON document with diagnostics."""
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InvalidStaticDataError(
            f"Unable to read quotation rules at '{path}': {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc

    try:
        raw_rules = json.loads(document)
    except JSONDecodeError as exc:
        raise InvalidStaticDataError(
            (
                f"Malformed quotation rules JSON at '{path}' "
                f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ),
            cause=exc,
        ) from exc

    try:
        return QuotationRules.model_validate(raw_rules)
    except ValidationError as exc:
        diagnostics = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise InvalidStaticDataError(
            f"Invalid quotation rules at '{path}': {diagnostics}",
            cause=exc,
        ) from exc
