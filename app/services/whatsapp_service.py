"""Direct Meta WhatsApp Cloud API client.

The service borrows one reusable ``httpx.AsyncClient`` from the application
lifespan and never accepts an endpoint URL from a caller. Quotation documents
are read only through the configured artifact-storage boundary.
"""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import WhatsAppAPIError
from app.services.quotation_storage import (
    QuotationArtifactStorage,
    QuotationStorageError,
    StoredQuotationArtifact,
)
from app.services.session_service import normalize_customer_identity

META_GRAPH_API_BASE_URL: Final = "https://graph.facebook.com"
META_TEXT_BODY_LIMIT: Final = 4_096
META_DOCUMENT_CAPTION_LIMIT: Final = 1_024
_QUOTATION_PDF_FILENAME_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?-[0-9]{8}-[0-9A-F]{4}\.pdf$"
)


@dataclass(frozen=True, slots=True)
class TextPartDelivery:
    """Provider acceptance result for one deterministic text part."""

    part_number: int
    part_count: int
    character_count: int
    outbound_message_id: str


@dataclass(frozen=True, slots=True)
class TextSendResult:
    """All provider IDs created by one logical text send."""

    parts: tuple[TextPartDelivery, ...]

    @property
    def outbound_message_id(self) -> str | None:
        """Return the identifier directly when the logical send had one part."""
        if len(self.parts) == 1:
            return self.parts[0].outbound_message_id
        return None

    @property
    def outbound_message_ids(self) -> tuple[str, ...]:
        """Return every identifier in deterministic delivery order."""
        return tuple(part.outbound_message_id for part in self.parts)


@dataclass(frozen=True, slots=True)
class DocumentUploadResult:
    """Checkpoint-safe result of a quotation PDF upload."""

    media_id: str
    filename: str


@dataclass(frozen=True, slots=True)
class DocumentSendResult:
    """Checkpoint-safe result of a document-message send."""

    outbound_message_id: str


class WhatsAppTextSendError(WhatsAppAPIError):
    """Safe text-send failure with partial progress and ambiguity metadata."""

    code = "whatsapp_text_send_failed"

    def __init__(
        self,
        detail: str,
        *,
        delivered_parts: tuple[TextPartDelivery, ...] = (),
        outcome_uncertain: bool,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(detail, cause=cause)
        self.delivered_parts = delivered_parts
        self.outcome_uncertain = outcome_uncertain


class WhatsAppDocumentUploadError(WhatsAppAPIError):
    """Safe document-upload failure with retry ambiguity metadata."""

    code = "whatsapp_document_upload_failed"

    def __init__(
        self,
        detail: str,
        *,
        outcome_uncertain: bool,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(detail, cause=cause)
        self.outcome_uncertain = outcome_uncertain


class WhatsAppDocumentSendError(WhatsAppAPIError):
    """Safe document-send failure with retry ambiguity metadata."""

    code = "whatsapp_document_send_failed"

    def __init__(
        self,
        detail: str,
        *,
        outcome_uncertain: bool,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(detail, cause=cause)
        self.outcome_uncertain = outcome_uncertain


class WhatsAppService:
    """Send outbound content through a fixed, configured Meta endpoint."""

    __slots__ = (
        "_access_token",
        "_client",
        "_media_url",
        "_messages_url",
        "_quotation_storage",
        "_timeout_seconds",
    )

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        settings: Settings | None = None,
        quotation_storage: QuotationArtifactStorage | None = None,
    ) -> None:
        if not isinstance(http_client, httpx.AsyncClient):
            raise TypeError("http_client must be an httpx.AsyncClient")
        resolved_settings = settings or get_settings()
        access_token = resolved_settings.whatsapp_access_token
        if access_token is None:
            raise ValueError("WHATSAPP_ACCESS_TOKEN is required for WhatsApp sending")
        token_value = access_token.get_secret_value().strip()
        if not token_value or _is_placeholder(token_value):
            raise ValueError("WHATSAPP_ACCESS_TOKEN must be configured for WhatsApp sending")

        phone_number_id = resolved_settings.whatsapp_phone_number_id
        if (
            phone_number_id is None
            or not phone_number_id.isascii()
            or not phone_number_id.isdigit()
        ):
            raise ValueError("WHATSAPP_PHONE_NUMBER_ID must be a numeric Meta phone-number ID")

        self._client = http_client
        self._access_token = token_value
        self._quotation_storage = quotation_storage
        self._timeout_seconds = resolved_settings.whatsapp_timeout_seconds
        phone_number_url = (
            f"{META_GRAPH_API_BASE_URL}/{resolved_settings.meta_graph_api_version}/"
            f"{phone_number_id}"
        )
        self._media_url = f"{phone_number_url}/media"
        self._messages_url = f"{phone_number_url}/messages"

    async def send_text(self, to: str, text: str) -> TextSendResult:
        """Send text parts sequentially and return each outbound Meta ID.

        No automatic retry is performed. A timeout, transport failure, or
        malformed success is marked uncertain because Meta may have accepted
        the request without the client receiving a usable acknowledgement.
        """
        destination = _meta_destination(to)
        parts = split_text_message(text)
        delivered: list[TextPartDelivery] = []
        part_count = len(parts)

        for part_number, part in enumerate(parts, start=1):
            try:
                response = await self._client.post(
                    self._messages_url,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    json={
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": destination,
                        "type": "text",
                        "text": {"preview_url": False, "body": part},
                    },
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as error:
                raise WhatsAppTextSendError(
                    f"Meta text part {part_number} timed out; delivery outcome is unknown",
                    delivered_parts=tuple(delivered),
                    outcome_uncertain=True,
                    cause=error,
                ) from error
            except httpx.RequestError as error:
                raise WhatsAppTextSendError(
                    f"Meta text part {part_number} failed at the network boundary; "
                    "delivery outcome is unknown",
                    delivered_parts=tuple(delivered),
                    outcome_uncertain=True,
                    cause=error,
                ) from error

            if not response.is_success:
                raise WhatsAppTextSendError(
                    f"Meta text part {part_number} returned HTTP {response.status_code}",
                    delivered_parts=tuple(delivered),
                    outcome_uncertain=False,
                )

            outbound_message_id = _outbound_message_id(response, part_number, delivered)
            delivered.append(
                TextPartDelivery(
                    part_number=part_number,
                    part_count=part_count,
                    character_count=len(part),
                    outbound_message_id=outbound_message_id,
                )
            )

        return TextSendResult(parts=tuple(delivered))

    async def upload_document(self, path: str | Path) -> DocumentUploadResult:
        """Upload one storage-validated quotation PDF and return its Meta ID.

        The path is converted to the storage adapter's opaque artifact reference;
        the adapter must validate both the backend-generated filename and its
        location before any bytes can reach Meta. No automatic retry or quotation
        regeneration is performed.
        """
        storage = self._quotation_storage
        if storage is None:
            raise RuntimeError("quotation storage is required for document uploads")
        artifact = _quotation_artifact(path)

        exists = await asyncio.to_thread(storage.exists, artifact)
        if not exists:
            raise QuotationStorageError("Quotation PDF does not exist in artifact storage")
        pdf_content = await asyncio.to_thread(storage.read, artifact)

        try:
            # httpx consumes the multipart stream before ``post`` returns. Keep
            # the handle scoped to that operation and close it on every outcome.
            with io.BytesIO(pdf_content) as document:
                response = await self._client.post(
                    self._media_url,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    data={
                        "messaging_product": "whatsapp",
                        "type": "application/pdf",
                    },
                    files={
                        "file": (artifact.path.name, document, "application/pdf"),
                    },
                    timeout=self._timeout_seconds,
                )
        except httpx.TimeoutException as error:
            raise WhatsAppDocumentUploadError(
                "Meta document upload timed out; upload outcome is unknown",
                outcome_uncertain=True,
                cause=error,
            ) from error
        except httpx.RequestError as error:
            raise WhatsAppDocumentUploadError(
                "Meta document upload failed at the network boundary; upload outcome is unknown",
                outcome_uncertain=True,
                cause=error,
            ) from error

        if not response.is_success:
            raise WhatsAppDocumentUploadError(
                f"Meta document upload returned HTTP {response.status_code}",
                outcome_uncertain=False,
            )

        return DocumentUploadResult(
            media_id=_uploaded_media_id(response),
            filename=artifact.path.name,
        )

    async def send_document(
        self,
        to: str,
        media_id: str,
        filename: str,
        caption: str | None = None,
    ) -> DocumentSendResult:
        """Send a previously uploaded quotation PDF by Meta media ID."""
        destination = _meta_destination(to)
        normalized_media_id = _provider_identifier(media_id, "media_id")
        normalized_filename = _quotation_pdf_filename(filename)
        normalized_caption = _document_caption(caption)
        document: dict[str, str] = {
            "id": normalized_media_id,
            "filename": normalized_filename,
        }
        if normalized_caption is not None:
            document["caption"] = normalized_caption

        try:
            response = await self._client.post(
                self._messages_url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": destination,
                    "type": "document",
                    "document": document,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise WhatsAppDocumentSendError(
                "Meta document send timed out; delivery outcome is unknown",
                outcome_uncertain=True,
                cause=error,
            ) from error
        except httpx.RequestError as error:
            raise WhatsAppDocumentSendError(
                "Meta document send failed at the network boundary; delivery outcome is unknown",
                outcome_uncertain=True,
                cause=error,
            ) from error

        if not response.is_success:
            raise WhatsAppDocumentSendError(
                f"Meta document send returned HTTP {response.status_code}",
                outcome_uncertain=False,
            )

        return DocumentSendResult(outbound_message_id=_document_message_id(response))


def split_text_message(text: str, *, limit: int = META_TEXT_BODY_LIMIT) -> tuple[str, ...]:
    """Split text without data loss, preferring line and whitespace boundaries."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text.strip():
        raise ValueError("text must not be blank")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")

    parts: list[str] = []
    offset = 0
    while len(text) - offset > limit:
        maximum = offset + limit
        split_at = _preferred_boundary(text, offset, maximum)
        if split_at is None:
            split_at = _unicode_safe_boundary(text, offset, maximum)
        if split_at <= offset:
            raise ValueError("text contains a Unicode sequence longer than Meta's text limit")
        parts.append(text[offset:split_at])
        offset = split_at
    parts.append(text[offset:])
    return tuple(parts)


def _preferred_boundary(text: str, start: int, maximum: int) -> int | None:
    # ``maximum`` is the first forbidden index for this part. A newline at
    # exactly that index belongs to the next part and must not create a
    # 4,097-character request body.
    newline = text.rfind("\n", start, maximum)
    if newline >= start:
        candidate = newline + 1
        if candidate > start and _is_unicode_boundary(text, candidate):
            return candidate

    for index in range(maximum - 1, start - 1, -1):
        if text[index].isspace():
            candidate = index + 1
            if _is_unicode_boundary(text, candidate):
                return candidate
    return None


def _unicode_safe_boundary(text: str, start: int, maximum: int) -> int:
    candidate = maximum
    while candidate > start and not _is_unicode_boundary(text, candidate):
        candidate -= 1
    return candidate


def _is_unicode_boundary(text: str, index: int) -> bool:
    if index <= 0 or index >= len(text):
        return True
    previous = text[index - 1]
    following = text[index]
    if previous == "\r" and following == "\n":
        return False
    if previous == "\u200d" or following == "\u200d":
        return False
    if unicodedata.combining(following) != 0 or _is_variation_or_modifier(following):
        return False
    if _is_regional_indicator(previous) and _is_regional_indicator(following):
        run_length = 0
        cursor = index - 1
        while cursor >= 0 and _is_regional_indicator(text[cursor]):
            run_length += 1
            cursor -= 1
        return run_length % 2 == 0
    return True


def _is_variation_or_modifier(character: str) -> bool:
    codepoint = ord(character)
    return 0xFE00 <= codepoint <= 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _meta_destination(identity: str) -> str:
    try:
        normalized = normalize_customer_identity(identity)
    except (TypeError, ValueError) as error:
        raise ValueError("to must be a validated WhatsApp transport identity") from error
    if normalized.startswith("dev:"):
        raise ValueError("development identities cannot receive WhatsApp messages")
    destination = normalized.removeprefix("+")
    if not destination.isascii() or not destination.isdigit():  # defensive after normalization
        raise ValueError("to must be a numeric WhatsApp transport identity")
    return destination


def _quotation_artifact(path: str | Path) -> StoredQuotationArtifact:
    if not isinstance(path, str | Path):
        raise TypeError("path must be a string or pathlib.Path from quotation storage")
    candidate = Path(path)
    filename = _quotation_pdf_filename(candidate.name)
    return StoredQuotationArtifact(
        quotation_id=filename.removesuffix(".pdf"),
        path=candidate,
    )


def _quotation_pdf_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise TypeError("filename must be a string")
    if not _QUOTATION_PDF_FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("filename must be a backend-generated quotation PDF filename")
    return filename


def _document_caption(caption: str | None) -> str | None:
    if caption is None:
        return None
    if not isinstance(caption, str):
        raise TypeError("caption must be a string or None")
    if not caption.strip():
        raise ValueError("caption must not be blank")
    if len(caption) > META_DOCUMENT_CAPTION_LIMIT:
        raise ValueError(f"caption must not exceed {META_DOCUMENT_CAPTION_LIMIT} characters")
    return caption


def _provider_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(character.isspace() or ord(character) < 0x20 for character in normalized)
    ):
        raise ValueError(f"{field_name} is not a valid provider identifier")
    return normalized


def _uploaded_media_id(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as error:
        raise WhatsAppDocumentUploadError(
            "Meta document upload returned malformed success JSON",
            outcome_uncertain=True,
            cause=error,
        ) from error
    if not isinstance(payload, Mapping):
        raise _malformed_document_upload()
    media_id = payload.get("id")
    if not isinstance(media_id, str):
        raise _malformed_document_upload()
    try:
        return _provider_identifier(media_id, "media_id")
    except ValueError as error:
        raise _malformed_document_upload(error) from error


def _malformed_document_upload(
    cause: BaseException | None = None,
) -> WhatsAppDocumentUploadError:
    return WhatsAppDocumentUploadError(
        "Meta document upload returned a malformed success response",
        outcome_uncertain=True,
        cause=cause,
    )


def _document_message_id(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as error:
        raise WhatsAppDocumentSendError(
            "Meta document send returned malformed success JSON",
            outcome_uncertain=True,
            cause=error,
        ) from error
    if not isinstance(payload, Mapping) or payload.get("messaging_product") != "whatsapp":
        raise _malformed_document_send()
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], Mapping):
        raise _malformed_document_send()
    message_id = messages[0].get("id")
    if not isinstance(message_id, str) or not message_id.strip().startswith("wamid."):
        raise _malformed_document_send()
    return message_id.strip()


def _malformed_document_send() -> WhatsAppDocumentSendError:
    return WhatsAppDocumentSendError(
        "Meta document send returned a malformed success response",
        outcome_uncertain=True,
    )


def _outbound_message_id(
    response: httpx.Response,
    part_number: int,
    delivered: Sequence[TextPartDelivery],
) -> str:
    try:
        payload = response.json()
    except ValueError as error:
        raise WhatsAppTextSendError(
            f"Meta text part {part_number} returned malformed success JSON",
            delivered_parts=tuple(delivered),
            outcome_uncertain=True,
            cause=error,
        ) from error
    if not isinstance(payload, Mapping) or payload.get("messaging_product") != "whatsapp":
        raise _malformed_success(part_number, delivered)
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], Mapping):
        raise _malformed_success(part_number, delivered)
    message_id = messages[0].get("id")
    if not isinstance(message_id, str) or not message_id.strip().startswith("wamid."):
        raise _malformed_success(part_number, delivered)
    return message_id.strip()


def _malformed_success(
    part_number: int,
    delivered: Sequence[TextPartDelivery],
) -> WhatsAppTextSendError:
    return WhatsAppTextSendError(
        f"Meta text part {part_number} returned a malformed success response",
        delivered_parts=tuple(delivered),
        outcome_uncertain=True,
    )


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


__all__ = [
    "META_DOCUMENT_CAPTION_LIMIT",
    "META_GRAPH_API_BASE_URL",
    "META_TEXT_BODY_LIMIT",
    "DocumentSendResult",
    "DocumentUploadResult",
    "TextPartDelivery",
    "TextSendResult",
    "WhatsAppDocumentSendError",
    "WhatsAppDocumentUploadError",
    "WhatsAppService",
    "WhatsAppTextSendError",
    "split_text_message",
]
