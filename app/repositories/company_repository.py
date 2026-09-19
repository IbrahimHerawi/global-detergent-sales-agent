"""Read-only repository for validated company data."""

from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import InvalidStaticDataError
from app.schemas.company import Company


class CompanyRepository:
    """Load and cache one authoritative company document."""

    __slots__ = ("_company",)

    def __init__(self, path: Path | None = None) -> None:
        configured_path = get_settings().company_data_path if path is None else path
        self._company = _load_company(configured_path)

    def get_company(self) -> Company:
        """Return a deep copy so callers cannot mutate cached company data."""
        return self._company.model_copy(deep=True)


def _load_company(path: Path) -> Company:
    """Read and validate company JSON with safe internal diagnostics."""
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InvalidStaticDataError(
            f"Unable to read company data at '{path}': {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc

    try:
        raw_company = json.loads(document)
    except JSONDecodeError as exc:
        raise InvalidStaticDataError(
            (
                f"Malformed company JSON at '{path}' "
                f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ),
            cause=exc,
        ) from exc

    try:
        return Company.model_validate(raw_company)
    except ValidationError as exc:
        diagnostics = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise InvalidStaticDataError(
            f"Invalid company data at '{path}': {diagnostics}",
            cause=exc,
        ) from exc
