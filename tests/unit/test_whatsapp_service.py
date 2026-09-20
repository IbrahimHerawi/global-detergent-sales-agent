"""Mocked Meta text-message client tests; no real provider calls are made."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
import respx
from app.core.config import Settings
from app.services.quotation_storage import (
    LocalQuotationStorage,
    QuotationStorageError,
    StoredQuotationArtifact,
    UnsafeQuotationPathError,
)
from app.services.whatsapp_service import (
    META_DOCUMENT_CAPTION_LIMIT,
    META_TEXT_BODY_LIMIT,
    WhatsAppDocumentSendError,
    WhatsAppDocumentUploadError,
    WhatsAppService,
    WhatsAppTextSendError,
    split_text_message,
)

MESSAGES_URL = "https://graph.facebook.com/v24.0/123456789/messages"
MEDIA_URL = "https://graph.facebook.com/v24.0/123456789/media"
ACCESS_TOKEN = "test-access-token-not-real"
QUOTATION_ID = "GDF-Q-20260920-A82F"
QUOTATION_FILENAME = f"{QUOTATION_ID}.pdf"
PDF_CONTENT = b"%PDF-1.4\nmock quotation\n%%EOF\n"


def settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "whatsapp_access_token": ACCESS_TOKEN,
        "whatsapp_phone_number_id": "123456789",
        "meta_graph_api_version": "v24.0",
        "whatsapp_timeout_seconds": 7.5,
    }
    values.update(updates)
    return Settings.model_validate(values)


def success(message_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": "97450000000", "wa_id": "97450000000"}],
            "messages": [{"id": message_id}],
        },
    )


def stored_quotation(
    root: Path,
) -> tuple[LocalQuotationStorage, StoredQuotationArtifact]:
    storage = LocalQuotationStorage(root=root)
    reservation = storage.reserve(QUOTATION_ID)
    return storage, storage.write(reservation, PDF_CONTENT)


async def test_send_text_uses_configured_endpoint_auth_payload_and_timeout() -> None:
    captured: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return success("wamid.outbound-1")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(side_effect=responder)
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            result = await service.send_text("+97450000000", "Hello from GDF")

    assert route.call_count == 1
    assert result.outbound_message_id == "wamid.outbound-1"
    assert result.outbound_message_ids == ("wamid.outbound-1",)
    request = captured[0]
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "97450000000",
        "type": "text",
        "text": {"preview_url": False, "body": "Hello from GDF"},
    }
    timeout = request.extensions["timeout"]
    assert timeout == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}


async def test_reuses_injected_client_for_multiple_messages() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(
            side_effect=[success("wamid.first"), success("wamid.second")]
        )
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            first = await service.send_text("97450000001", "First")
            second = await service.send_text("97450000002", "Second")
            assert client.is_closed is False

    assert route.call_count == 2
    assert first.outbound_message_id == "wamid.first"
    assert second.outbound_message_id == "wamid.second"


@pytest.mark.parametrize(
    "identity",
    ["dev:browser-user", "974 5000 0000", "974-5000-0000", "https://evil.example"],
)
async def test_rejects_non_whatsapp_or_unvalidated_destinations(identity: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(MESSAGES_URL).mock(return_value=success("wamid.never"))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(ValueError):
                await service.send_text(identity, "Hello")

    assert route.called is False


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_rejects_blank_text_without_a_request(text: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(MESSAGES_URL).mock(return_value=success("wamid.never"))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(ValueError, match="must not be blank"):
                await service.send_text("97450000000", text)

    assert route.called is False


def test_exact_limit_is_one_part_and_hard_boundary_is_deterministic() -> None:
    exact = "a" * META_TEXT_BODY_LIMIT
    over = exact + "b"

    assert split_text_message(exact) == (exact,)
    assert split_text_message(over) == (exact, "b")


def test_split_prefers_latest_safe_whitespace_and_preserves_text_exactly() -> None:
    text = "a" * (META_TEXT_BODY_LIMIT - 8) + " boundary " + "b" * 40

    parts = split_text_message(text)

    assert len(parts) == 2
    assert parts[0].endswith(" ")
    assert len(parts[0]) <= META_TEXT_BODY_LIMIT
    assert all(len(part) <= META_TEXT_BODY_LIMIT for part in parts)
    assert "".join(parts) == text


def test_split_does_not_separate_combining_sequence_at_hard_boundary() -> None:
    text = "a" * (META_TEXT_BODY_LIMIT - 1) + "e\u0301z"

    parts = split_text_message(text)

    assert parts == ("a" * (META_TEXT_BODY_LIMIT - 1), "e\u0301z")
    assert "".join(parts) == text


def test_newline_just_beyond_limit_never_creates_an_oversized_part() -> None:
    text = "a" * META_TEXT_BODY_LIMIT + "\nrest"

    parts = split_text_message(text)

    assert parts == ("a" * META_TEXT_BODY_LIMIT, "\nrest")
    assert all(len(part) <= META_TEXT_BODY_LIMIT for part in parts)


async def test_long_text_sends_parts_sequentially_and_exposes_each_result() -> None:
    text = "a" * META_TEXT_BODY_LIMIT + "b"
    bodies: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content)["text"]["body"])
        return success(f"wamid.part-{len(bodies)}")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(side_effect=responder)
        async with httpx.AsyncClient() as client:
            result = await WhatsAppService(client, settings=settings()).send_text(
                "97450000000", text
            )

    assert route.call_count == 2
    assert bodies == ["a" * META_TEXT_BODY_LIMIT, "b"]
    assert result.outbound_message_id is None
    assert result.outbound_message_ids == ("wamid.part-1", "wamid.part-2")
    assert [(part.part_number, part.part_count, part.character_count) for part in result.parts] == [
        (1, 2, META_TEXT_BODY_LIMIT),
        (2, 2, 1),
    ]


@pytest.mark.parametrize("status_code", [400, 401, 429, 500])
async def test_non_success_status_is_safe_and_never_retried(status_code: int) -> None:
    sensitive_body = {"error": {"message": "customer secret", "token": ACCESS_TOKEN}}
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(
            return_value=httpx.Response(status_code, json=sensitive_body)
        )
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppTextSendError) as caught:
                await service.send_text("97450000000", "Hello")

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is False
    assert caught.value.delivered_parts == ()
    assert caught.value.diagnostic_detail == f"Meta text part 1 returned HTTP {status_code}"
    assert "customer secret" not in caught.value.diagnostic_detail
    assert ACCESS_TOKEN not in caught.value.diagnostic_detail


async def test_timeout_is_uncertain_and_not_retried() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(
            side_effect=httpx.ReadTimeout("provider did not acknowledge")
        )
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppTextSendError) as caught:
                await service.send_text("97450000000", "Hello")

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is True
    assert caught.value.delivered_parts == ()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"messaging_product": "whatsapp", "messages": []}),
        httpx.Response(
            200,
            json={"messaging_product": "whatsapp", "messages": [{"id": "not-wamid"}]},
        ),
    ],
)
async def test_malformed_success_is_uncertain_and_not_retried(response: httpx.Response) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(return_value=response)
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppTextSendError) as caught:
                await service.send_text("97450000000", "Hello")

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is True


async def test_partial_chunk_failure_reports_only_confirmed_prior_parts() -> None:
    text = "a" * META_TEXT_BODY_LIMIT + "b"
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(
            side_effect=[success("wamid.part-1"), httpx.ReadTimeout("ambiguous")]
        )
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppTextSendError) as caught:
                await service.send_text("97450000000", text)

    assert route.call_count == 2
    assert caught.value.outcome_uncertain is True
    assert [part.outbound_message_id for part in caught.value.delivered_parts] == ["wamid.part-1"]


async def test_errors_do_not_log_authorization_or_raw_provider_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_body = "raw-provider-secret"
    with respx.mock(assert_all_called=True) as router:
        router.post(MESSAGES_URL).mock(return_value=httpx.Response(500, text=secret_body))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppTextSendError):
                await service.send_text("97450000000", "Hello")

    logs = caplog.text
    assert ACCESS_TOKEN not in logs
    assert secret_body not in logs


@pytest.mark.parametrize(
    "updates",
    [
        {"whatsapp_access_token": None},
        {"whatsapp_access_token": "<whatsapp-access-token>"},
        {"whatsapp_phone_number_id": "123/path"},
        {"whatsapp_phone_number_id": "https://evil.example"},
    ],
)
async def test_invalid_configuration_fails_before_any_request(updates: dict[str, object]) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            WhatsAppService(client, settings=settings(**updates))


async def test_upload_document_uses_storage_and_official_pdf_multipart_format(
    tmp_path: Path,
) -> None:
    storage, artifact = stored_quotation(tmp_path)
    captured: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "987654321012345"})

    with respx.mock(assert_all_called=True) as router:
        route = router.post(MEDIA_URL).mock(side_effect=responder)
        async with httpx.AsyncClient() as client:
            result = await WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            ).upload_document(str(artifact.path))

    assert route.call_count == 1
    assert result.media_id == "987654321012345"
    assert result.filename == QUOTATION_FILENAME
    assert not hasattr(result, "path")
    request = captured[0]
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="messaging_product"' in body
    assert b"\r\n\r\nwhatsapp\r\n" in body
    assert b'name="type"' in body
    assert b"\r\n\r\napplication/pdf\r\n" in body
    assert f'filename="{QUOTATION_FILENAME}"'.encode() in body
    assert b"Content-Type: application/pdf" in body
    assert PDF_CONTENT in body
    assert request.extensions["timeout"] == {
        "connect": 7.5,
        "read": 7.5,
        "write": 7.5,
        "pool": 7.5,
    }


async def test_upload_document_rejects_missing_pdf_without_request(tmp_path: Path) -> None:
    storage = LocalQuotationStorage(root=tmp_path)
    missing = tmp_path.resolve() / QUOTATION_FILENAME

    with respx.mock(assert_all_called=False) as router:
        route = router.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(QuotationStorageError) as caught:
                await service.upload_document(missing)

    assert route.called is False
    expected_detail = "Quotation PDF does not exist in artifact storage"
    assert caught.value.diagnostic_detail == expected_detail
    assert str(missing) not in (caught.value.diagnostic_detail or "")


async def test_upload_document_requires_quotation_storage_adapter(tmp_path: Path) -> None:
    path = tmp_path / QUOTATION_FILENAME
    path.write_bytes(PDF_CONTENT)

    with respx.mock(assert_all_called=False) as router:
        route = router.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(RuntimeError, match="quotation storage is required"):
                await service.upload_document(path)

    assert route.called is False


async def test_upload_document_rejects_invalid_pdf_without_request(tmp_path: Path) -> None:
    storage = LocalQuotationStorage(root=tmp_path)
    invalid = tmp_path.resolve() / QUOTATION_FILENAME
    invalid.write_bytes(b"not a PDF")

    with respx.mock(assert_all_called=False) as router:
        route = router.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(QuotationStorageError):
                await service.upload_document(invalid)

    assert route.called is False


async def test_upload_document_rejects_path_outside_storage_without_request(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "quotes"
    outside_root = tmp_path / "outside"
    storage = LocalQuotationStorage(root=storage_root)
    outside_root.mkdir()
    forged = outside_root / QUOTATION_FILENAME
    forged.write_bytes(PDF_CONTENT)

    with respx.mock(assert_all_called=False) as router:
        route = router.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(UnsafeQuotationPathError):
                await service.upload_document(forged)

    assert route.called is False


@pytest.mark.parametrize(
    "path",
    [
        Path("customer-name.pdf"),
        Path("GDF-Q-20260920-A82F.txt"),
    ],
)
async def test_upload_document_accepts_only_backend_generated_pdf_names(
    tmp_path: Path,
    path: Path,
) -> None:
    storage = LocalQuotationStorage(root=tmp_path)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(ValueError):
                await service.upload_document(path)

    assert route.called is False


@pytest.mark.parametrize("status_code", [400, 401, 429, 500])
async def test_upload_document_external_rejection_is_safe_and_not_retried(
    tmp_path: Path,
    status_code: int,
) -> None:
    storage, artifact = stored_quotation(tmp_path)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MEDIA_URL).mock(
            return_value=httpx.Response(
                status_code,
                json={"error": {"message": str(artifact.path), "token": ACCESS_TOKEN}},
            )
        )
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(WhatsAppDocumentUploadError) as caught:
                await service.upload_document(artifact.path)

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is False
    expected_detail = f"Meta document upload returned HTTP {status_code}"
    assert caught.value.diagnostic_detail == expected_detail
    assert str(artifact.path) not in (caught.value.diagnostic_detail or "")
    assert ACCESS_TOKEN not in (caught.value.diagnostic_detail or "")


async def test_upload_document_timeout_is_uncertain_and_not_retried(tmp_path: Path) -> None:
    storage, artifact = stored_quotation(tmp_path)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MEDIA_URL).mock(side_effect=httpx.ReadTimeout("ambiguous"))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(WhatsAppDocumentUploadError) as caught:
                await service.upload_document(artifact.path)

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"id": 123}),
        httpx.Response(200, json={"id": "bad media id"}),
    ],
)
async def test_upload_document_malformed_success_is_uncertain(
    tmp_path: Path,
    response: httpx.Response,
) -> None:
    storage, artifact = stored_quotation(tmp_path)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MEDIA_URL).mock(return_value=response)
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(
                client,
                settings=settings(),
                quotation_storage=storage,
            )
            with pytest.raises(WhatsAppDocumentUploadError) as caught:
                await service.upload_document(artifact.path)

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is True


@pytest.mark.parametrize("caption", [None, "Your requested quotation"])
async def test_send_document_uses_media_id_filename_and_optional_caption(
    caption: str | None,
) -> None:
    captured: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return success("wamid.document-1")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(side_effect=responder)
        async with httpx.AsyncClient() as client:
            result = await WhatsAppService(client, settings=settings()).send_document(
                "+97450000000",
                "987654321012345",
                QUOTATION_FILENAME,
                caption,
            )

    assert route.call_count == 1
    assert result.outbound_message_id == "wamid.document-1"
    document = {
        "id": "987654321012345",
        "filename": QUOTATION_FILENAME,
    }
    if caption is not None:
        document["caption"] = caption
    assert json.loads(captured[0].content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "97450000000",
        "type": "document",
        "document": document,
    }
    assert captured[0].extensions["timeout"] == {
        "connect": 7.5,
        "read": 7.5,
        "write": 7.5,
        "pool": 7.5,
    }


@pytest.mark.parametrize(
    ("media_id", "filename", "caption"),
    [
        ("", QUOTATION_FILENAME, None),
        ("bad media id", QUOTATION_FILENAME, None),
        ("123", "customer-controlled.pdf", None),
        ("123", "../GDF-Q-20260920-A82F.pdf", None),
        ("123", QUOTATION_FILENAME, "   "),
        ("123", QUOTATION_FILENAME, "x" * (META_DOCUMENT_CAPTION_LIMIT + 1)),
    ],
)
async def test_send_document_rejects_invalid_fields_without_request(
    media_id: str,
    filename: str,
    caption: str | None,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(MESSAGES_URL).mock(return_value=success("wamid.never"))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(ValueError):
                await service.send_document(
                    "97450000000",
                    media_id,
                    filename,
                    caption,
                )

    assert route.called is False


@pytest.mark.parametrize("status_code", [400, 401, 429, 500])
async def test_send_document_external_rejection_is_safe_and_not_retried(
    status_code: int,
) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(
            return_value=httpx.Response(
                status_code,
                json={"error": {"message": "customer secret", "token": ACCESS_TOKEN}},
            )
        )
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppDocumentSendError) as caught:
                await service.send_document(
                    "97450000000",
                    "987654321012345",
                    QUOTATION_FILENAME,
                )

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is False
    assert caught.value.diagnostic_detail == f"Meta document send returned HTTP {status_code}"
    assert ACCESS_TOKEN not in (caught.value.diagnostic_detail or "")


async def test_send_document_timeout_is_uncertain_and_not_retried() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(side_effect=httpx.ReadTimeout("ambiguous"))
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppDocumentSendError) as caught:
                await service.send_document(
                    "97450000000",
                    "987654321012345",
                    QUOTATION_FILENAME,
                )

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"messaging_product": "whatsapp", "messages": []}),
        httpx.Response(
            200,
            json={"messaging_product": "whatsapp", "messages": [{"id": "not-wamid"}]},
        ),
    ],
)
async def test_send_document_malformed_success_is_uncertain(response: httpx.Response) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(MESSAGES_URL).mock(return_value=response)
        async with httpx.AsyncClient() as client:
            service = WhatsAppService(client, settings=settings())
            with pytest.raises(WhatsAppDocumentSendError) as caught:
                await service.send_document(
                    "97450000000",
                    "987654321012345",
                    QUOTATION_FILENAME,
                )

    assert route.call_count == 1
    assert caught.value.outcome_uncertain is True
