"""Structured logging with asynchronous context and defensive redaction."""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from io import TextIOBase
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LogValue = str | int | float
LogContext = dict[str, LogValue]

CONTEXT_FIELDS: Final = frozenset(
    {
        "request_id",
        "whatsapp_message_id",
        "session_id",
        "tool_name",
        "quotation_id",
        "previous_state",
        "new_state",
        "duration",
        "error_code",
    }
)
SUMMARY_FIELDS: Final = frozenset({"status", "result_count"})
REDACTED: Final = "[REDACTED]"
OMITTED: Final = "[OMITTED]"

_log_context: ContextVar[LogContext | None] = ContextVar("log_context", default=None)
_record_defaults = logging.makeLogRecord({}).__dict__.keys()
_RESERVED_RECORD_FIELDS: Final = frozenset({*_record_defaults, "asctime", "message"})
_SENSITIVE_KEY_PATTERN: Final = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|auth[_-]?header|"
    r"password|passwd|secret|credential|signature)$",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_PATTERN: Final = re.compile(
    r"\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|password|"
    r"secret|credential)\b(\s*[:=]\s*)(?:Bearer\s+)?([^\s,;&]+)",
    re.IGNORECASE,
)
_BEARER_PATTERN: Final = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_KEY_PATTERN: Final = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_URL_PATTERN: Final = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PHONE_PATTERN: Final = re.compile(r"(?<!\w)\+\d[\d\s().-]{6,}\d(?!\w)|\b\d{10,15}\b")
_PHONE_KEYS: Final = frozenset({"phone", "phone_number", "sender", "wa_id", "mobile"})
_CONTENT_KEYS: Final = frozenset(
    {
        "chat_history",
        "conversation",
        "conversation_contents",
        "customer",
        "customer_data",
        "customer_record",
        "message_history",
        "messages",
    }
)


def mask_phone_number(value: str) -> str:
    """Mask a phone number while retaining limited correlation characters."""
    has_plus = value.strip().startswith("+")
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        return REDACTED
    if len(digits) <= 4:
        return "*" * max(len(digits) - 2, 1) + digits[-2:]

    prefix_length = 3 if has_plus else 2
    suffix_length = 3
    hidden_length = max(len(digits) - prefix_length - suffix_length, 1)
    prefix = digits[:prefix_length]
    suffix = digits[-suffix_length:]
    return f"{'+' if has_plus else ''}{prefix}{'*' * hidden_length}{suffix}"


def get_log_context() -> LogContext:
    """Return a copy of the current task-local logging context."""
    return dict(_log_context.get() or {})


def bind_log_context(**fields: LogValue | None) -> Token[LogContext | None]:
    """Add supported fields to the current task-local context."""
    unknown = fields.keys() - CONTEXT_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unsupported logging context fields: {names}")

    updated = get_log_context()
    for name, value in fields.items():
        if value is None:
            updated.pop(name, None)
        else:
            updated[name] = value
    return _log_context.set(updated)


def reset_log_context(token: Token[LogContext | None]) -> None:
    """Restore the context that existed before :func:`bind_log_context`."""
    _log_context.reset(token)


def clear_log_context() -> None:
    """Clear all context fields in the current execution context."""
    _log_context.set({})


@contextmanager
def log_context(**fields: LogValue | None) -> Iterator[None]:
    """Temporarily bind identifiers without leaking them across async tasks."""
    token = bind_log_context(**fields)
    try:
        yield
    finally:
        reset_log_context(token)


class JsonFormatter(logging.Formatter):
    """Render standard log records as one sanitized JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _sanitize_text(record.getMessage()),
        }

        context = get_log_context()
        for name in CONTEXT_FIELDS | SUMMARY_FIELDS:
            value = record.__dict__.get(name, context.get(name))
            if value is not None:
                payload[name] = _sanitize(value, key=name)

        extra = {
            name: value
            for name, value in record.__dict__.items()
            if name not in _RESERVED_RECORD_FIELDS
            and name not in CONTEXT_FIELDS
            and name not in SUMMARY_FIELDS
        }
        if extra:
            payload["data"] = _sanitize(extra)

        if (
            record.exc_info is not None
            and record.exc_info[0] is not None
            and record.exc_info[1] is not None
        ):
            exception_type, exception, _ = record.exc_info
            payload["exception"] = {
                "type": exception_type.__name__,
                "message": _sanitize_text(str(exception)),
                "traceback": _sanitize_text(
                    "".join(traceback.format_exception(*record.exc_info)).strip()
                ),
            }

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIOBase | None = None,
) -> None:
    """Configure the root logger with a single structured stream handler."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def log_tool_result(
    logger: logging.Logger,
    *,
    tool_name: str,
    status: str,
    result_count: int | None = None,
    error_code: str | None = None,
    duration: float | None = None,
) -> None:
    """Log only allowlisted tool-result metadata, never the complete result."""
    extra: dict[str, LogValue] = {"tool_name": tool_name, "status": status}
    if result_count is not None:
        extra["result_count"] = result_count
    if error_code is not None:
        extra["error_code"] = error_code
    if duration is not None:
        extra["duration"] = duration
    logger.info("tool_result", extra=extra)


def _sanitize(value: object, *, key: str | None = None, depth: int = 0) -> Any:
    if depth >= 8:
        return OMITTED

    normalized_key = key.casefold() if key is not None else None
    if normalized_key is not None:
        if _SENSITIVE_KEY_PATTERN.search(normalized_key):
            return REDACTED
        if normalized_key in _CONTENT_KEYS:
            return OMITTED
        if normalized_key in _PHONE_KEYS and isinstance(value, str):
            return mask_phone_number(value)

    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            str(nested_key): _sanitize(
                nested_value,
                key=str(nested_key),
                depth=depth + 1,
            )
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": _sanitize_text(str(value))}
    return f"<{type(value).__name__}>"


def _sanitize_text(value: str) -> str:
    sanitized = _URL_PATTERN.sub(lambda match: _sanitize_url(match.group(0)), value)
    sanitized = _SENSITIVE_TEXT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        sanitized,
    )
    sanitized = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", sanitized)
    sanitized = _API_KEY_PATTERN.sub(REDACTED, sanitized)
    return _PHONE_PATTERN.sub(lambda match: mask_phone_number(match.group(0)), sanitized)


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        query = [
            (
                name,
                REDACTED
                if _SENSITIVE_KEY_PATTERN.search(name)
                else mask_phone_number(item)
                if name.casefold() in _PHONE_KEYS
                else item,
            )
            for name, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]

        netloc = parsed.netloc
        if parsed.password is not None and parsed.hostname is not None:
            username = f"{parsed.username}:" if parsed.username is not None else ""
            port = f":{parsed.port}" if parsed.port is not None else ""
            netloc = f"{username}{REDACTED}@{parsed.hostname}{port}"

        return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        return REDACTED


__all__ = [
    "CONTEXT_FIELDS",
    "JsonFormatter",
    "bind_log_context",
    "clear_log_context",
    "configure_logging",
    "get_log_context",
    "log_context",
    "log_tool_result",
    "mask_phone_number",
    "reset_log_context",
]
