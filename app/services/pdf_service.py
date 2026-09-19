"""Asynchronous-safe quotation PDF rendering and storage orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from jinja2 import (
    BaseLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
    Template,
    TemplateNotFound,
    select_autoescape,
)
from weasyprint import HTML  # type: ignore[import-untyped]
from weasyprint.urls import (  # type: ignore[import-untyped]
    FatalURLFetchingError,
    URLFetcher,
)

from app.core.exceptions import QuotationGenerationError
from app.repositories.company_repository import CompanyRepository
from app.schemas.quotation import GeneratedQuotation
from app.services.quotation_storage import (
    LocalQuotationStorage,
    QuotationArtifactStorage,
    QuotationReservation,
)

_DEFAULT_TEMPLATE_NAME: Final = "quotation.html"
_MINIMUM_PDF_LENGTH: Final = len(b"%PDF-1.4\n%%EOF\n")


class _ApprovedTemplateLoader(FileSystemLoader):
    """Reject template reads that resolve outside the approved directory."""

    def __init__(self, template_root: Path) -> None:
        self._template_root = template_root.resolve(strict=True)
        super().__init__(str(self._template_root), followlinks=False)

    def get_source(
        self,
        environment: Environment,
        template: str,
    ) -> tuple[str, str, Any]:
        candidate = self._template_root / template
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._template_root)
        except (OSError, ValueError) as error:
            raise TemplateNotFound(template) from error
        if not resolved.is_file():
            raise TemplateNotFound(template)
        return super().get_source(environment, template)


class _ApprovedAssetFetcher(URLFetcher):  # type: ignore[misc]
    """WeasyPrint fetcher that can open only regular files in one asset root."""

    def __init__(self, asset_root: Path) -> None:
        super().__init__(
            allowed_protocols=("file",),
            allow_redirects=False,
            fail_on_errors=True,
        )
        self._asset_root = asset_root.resolve(strict=False)

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> Any:
        parsed = urlparse(url)
        if parsed.scheme != "file":
            raise ValueError("Only approved local PDF assets are allowed")
        candidate = Path(url2pathname(unquote(parsed.path)))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._asset_root)
        except (OSError, ValueError) as error:
            raise ValueError("PDF asset path is not approved") from error
        if not resolved.is_file():
            raise ValueError("PDF asset is not a regular file")
        return super().fetch(resolved.as_uri(), headers)


class PDFService:
    """Render validated quotation models and persist complete PDFs only."""

    __slots__ = (
        "_asset_root",
        "_company",
        "_environment",
        "_storage",
        "_template_name",
        "_template_root",
    )

    def __init__(
        self,
        storage: QuotationArtifactStorage | None = None,
        *,
        company_repository: CompanyRepository | None = None,
        template_directory: Path | None = None,
        asset_directory: Path | None = None,
        template_name: str = _DEFAULT_TEMPLATE_NAME,
    ) -> None:
        application_root = Path(__file__).resolve().parents[1]
        template_root = (
            application_root / "templates" if template_directory is None else template_directory
        ).resolve(strict=True)
        asset_root = (
            application_root / "assets" if asset_directory is None else asset_directory
        ).resolve(strict=False)
        if not template_root.is_dir():
            raise QuotationGenerationError("PDF template root is not a directory")

        self._storage = storage or LocalQuotationStorage()
        self._company = (company_repository or CompanyRepository()).get_company()
        self._template_root = template_root
        self._asset_root = asset_root
        self._template_name = template_name
        self._environment = Environment(
            loader=_ApprovedTemplateLoader(template_root),
            autoescape=select_autoescape(enabled_extensions=("html",)),
            undefined=StrictUndefined,
        )

    async def generate_quotation_pdf(self, quotation: GeneratedQuotation) -> Path:
        """Render and persist one quotation without blocking the event loop."""
        if not isinstance(quotation, GeneratedQuotation):
            raise TypeError("quotation must be a GeneratedQuotation")

        reservation: QuotationReservation | None = None
        try:
            pdf_content = await asyncio.to_thread(self._render_pdf_bytes, quotation)
            _validate_complete_pdf(pdf_content)
            reservation = await asyncio.to_thread(
                self._storage.reserve,
                quotation.quotation_id,
            )
            artifact = await asyncio.to_thread(
                self._storage.write,
                reservation,
                pdf_content,
            )
            reservation = None
            exists = await asyncio.to_thread(self._storage.exists, artifact)
            if not exists:
                raise QuotationGenerationError("Stored quotation artifact did not pass validation")
            stored_content = await asyncio.to_thread(self._storage.read, artifact)
            _validate_complete_pdf(stored_content)
            return artifact.path
        except asyncio.CancelledError:
            if reservation is not None:
                await asyncio.shield(asyncio.to_thread(self._storage.cancel, reservation))
            raise
        except Exception as error:
            cleanup_error: Exception | None = None
            if reservation is not None:
                try:
                    await asyncio.to_thread(self._storage.cancel, reservation)
                except Exception as caught_cleanup_error:
                    cleanup_error = caught_cleanup_error
            if isinstance(error, QuotationGenerationError) and cleanup_error is None:
                raise
            detail = f"Quotation PDF generation failed: {type(error).__name__}"
            if cleanup_error is not None:
                detail += f"; cleanup failed: {type(cleanup_error).__name__}"
            raise QuotationGenerationError(detail, cause=error) from error

    def _render_pdf_bytes(self, quotation: GeneratedQuotation) -> bytes:
        template = self._load_template()
        html = template.render(
            company=self._company.model_copy(deep=True),
            quotation=quotation,
            logo_data_uri=None,
        )
        try:
            pdf_content = HTML(
                string=html,
                base_url=self._template_root.as_uri(),
                url_fetcher=_ApprovedAssetFetcher(self._asset_root),
            ).write_pdf()
        except FatalURLFetchingError as error:
            raise QuotationGenerationError(
                "Quotation template requested a prohibited or unavailable resource",
                cause=error,
            ) from error
        if not isinstance(pdf_content, bytes):
            raise QuotationGenerationError("WeasyPrint did not return PDF bytes")
        return pdf_content

    def _load_template(self) -> Template:
        loader = self._environment.loader
        if not isinstance(loader, BaseLoader):
            raise QuotationGenerationError("PDF template loader is unavailable")
        try:
            return self._environment.get_template(self._template_name)
        except Exception as error:
            raise QuotationGenerationError(
                f"Unable to load PDF template: {type(error).__name__}",
                cause=error,
            ) from error


async def generate_quotation_pdf(quotation: GeneratedQuotation) -> Path:
    """Generate a quotation PDF using default repositories and local storage."""
    return await PDFService().generate_quotation_pdf(quotation)


def _validate_complete_pdf(content: bytes) -> None:
    if (
        not isinstance(content, bytes)
        or len(content) < _MINIMUM_PDF_LENGTH
        or not content.startswith(b"%PDF-")
        or not content.rstrip().endswith(b"%%EOF")
    ):
        raise QuotationGenerationError("Rendered quotation is not a complete PDF")


__all__ = ["PDFService", "generate_quotation_pdf"]
