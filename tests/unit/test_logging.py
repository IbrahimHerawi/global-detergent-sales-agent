"""Tests for structured logging, context isolation, and redaction."""

import asyncio
import json
import logging
from io import StringIO
from typing import Any

import pytest
from app.core.exceptions import WhatsAppAPIError
from app.core.logging import JsonFormatter, log_context, log_tool_result, mask_phone_number


@pytest.fixture
def captured_logger() -> tuple[logging.Logger, StringIO]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.structured")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def _records(stream: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_concurrent_contexts_do_not_leak_identifiers(
    captured_logger: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured_logger
    barrier = asyncio.Event()

    async def worker(request_id: str, session_id: str) -> None:
        with log_context(request_id=request_id, session_id=session_id):
            await barrier.wait()
            logger.info("request handled")
            await asyncio.sleep(0)

    first = asyncio.create_task(worker("request-a", "session-a"))
    second = asyncio.create_task(worker("request-b", "session-b"))
    await asyncio.sleep(0)
    barrier.set()
    await asyncio.gather(first, second)
    logger.info("outside context")

    records = _records(stream)
    paired_contexts = {(item["request_id"], item["session_id"]) for item in records[:2]}
    assert paired_contexts == {("request-a", "session-a"), ("request-b", "session-b")}
    assert "request_id" not in records[2]
    assert "session_id" not in records[2]


def test_nested_secrets_urls_phones_and_customer_content_are_sanitized(
    captured_logger: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured_logger
    logger.info(
        "received Authorization: Bearer visible-in-input",
        extra={
            "payload": {
                "headers": {"Authorization": "Bearer header-secret"},
                "api_key": "sk-super-secret-key",
                "callback": "https://user:password@example.com/send?access_token=url-secret&ok=yes",
                "contact": {"phone_number": "+97455512345"},
                "customer_record": {"name": "Full Customer", "phone": "+97455599999"},
                "conversation": ["complete private conversation"],
            }
        },
    )

    raw = stream.getvalue()
    record = _records(stream)[0]
    payload = record["data"]["payload"]

    for secret in (
        "visible-in-input",
        "header-secret",
        "sk-super-secret-key",
        "url-secret",
        "password",
        "Full Customer",
        "complete private conversation",
    ):
        assert secret not in raw
    assert payload["headers"]["Authorization"] == "[REDACTED]"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["contact"]["phone_number"] == "+974*****345"
    assert payload["customer_record"] == "[OMITTED]"
    assert payload["conversation"] == "[OMITTED]"
    assert "ok=yes" in payload["callback"]


def test_phone_masking_retains_only_limited_correlation_digits() -> None:
    assert mask_phone_number("+974 5551 2345") == "+974*****345"
    assert mask_phone_number("5551234567") == "55*****567"


def test_exception_logging_is_structured_and_redacted(
    captured_logger: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured_logger
    cause = RuntimeError("authorization=Bearer upstream-secret")

    try:
        raise WhatsAppAPIError(
            "access_token=diagnostic-secret",
            cause=cause,
        ) from cause
    except WhatsAppAPIError:
        logger.exception("WhatsApp request failed api_key=message-secret")

    raw = stream.getvalue()
    record = _records(stream)[0]

    assert record["exception"]["type"] == "WhatsAppAPIError"
    assert "temporarily unable" in record["exception"]["message"]
    assert "Traceback" in record["exception"]["traceback"]
    assert "diagnostic-secret" not in raw
    assert "upstream-secret" not in raw
    assert "message-secret" not in raw


def test_tool_result_logging_contains_summary_only(
    captured_logger: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured_logger

    with log_context(
        request_id="request-1",
        whatsapp_message_id="wamid-1",
        session_id="session-1",
        quotation_id="quote-1",
        previous_state="DISCOVERY",
        new_state="BUILDING_QUOTE",
    ):
        log_tool_result(
            logger,
            tool_name="search_products",
            status="success",
            result_count=3,
            duration=0.125,
        )

    record = _records(stream)[0]
    assert record["message"] == "tool_result"
    assert record["tool_name"] == "search_products"
    assert record["status"] == "success"
    assert record["result_count"] == 3
    assert record["duration"] == 0.125
    assert record["request_id"] == "request-1"
    assert record["whatsapp_message_id"] == "wamid-1"
    assert record["session_id"] == "session-1"
    assert record["quotation_id"] == "quote-1"
    assert record["previous_state"] == "DISCOVERY"
    assert record["new_state"] == "BUILDING_QUOTE"
    assert "result" not in record.get("data", {})

    log_tool_result(
        logger,
        tool_name="search_products",
        status="error",
        error_code="product_search_failed",
    )
    failed_record = _records(stream)[1]
    assert failed_record["status"] == "error"
    assert failed_record["error_code"] == "product_search_failed"
