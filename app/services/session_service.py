"""Temporary conversation-session persistence backed by Redis."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.core.exceptions import SessionStorageUnavailableError
from app.schemas.session import ConversationSession

SESSION_KEY_PREFIX: Final = "gdf:session:"
DEVELOPMENT_IDENTITY_PREFIX: Final = "dev:"
_DEVELOPMENT_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WHATSAPP_SENDER_ID = re.compile(r"^[0-9]+$")


def normalize_customer_identity(customer_phone: str) -> str:
    """Return a canonical WhatsApp or development transport identity.

    WhatsApp supplies numeric sender identifiers. A leading plus is accepted,
    but no country code is inferred and punctuation is not silently discarded.
    Development identities are deliberately kept outside the phone namespace.
    """
    if not isinstance(customer_phone, str):
        raise TypeError("customer_phone must be a string")

    identity = customer_phone.strip()
    if identity.startswith(DEVELOPMENT_IDENTITY_PREFIX):
        session_id = identity.removeprefix(DEVELOPMENT_IDENTITY_PREFIX)
        if not _DEVELOPMENT_SESSION_ID.fullmatch(session_id):
            raise ValueError("development session identity is invalid")
        return f"{DEVELOPMENT_IDENTITY_PREFIX}{session_id}"

    digits = identity.removeprefix("+")
    if not _WHATSAPP_SENDER_ID.fullmatch(digits):
        raise ValueError("customer_phone must be a WhatsApp numeric sender identifier")
    return f"+{digits}"


def session_key(customer_phone: str) -> str:
    """Build the mandated Redis key for a canonical transport identity."""
    return f"{SESSION_KEY_PREFIX}{normalize_customer_identity(customer_phone)}"


class SessionService:
    """Persist validated temporary sessions with a sliding Redis TTL."""

    __slots__ = ("_recent_message_limit", "_redis", "_session_ttl_seconds")

    def __init__(
        self,
        redis_client: Redis | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        resolved_settings = settings or get_settings()
        self._redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(resolved_settings.redis_url, decode_responses=True)
        )
        self._session_ttl_seconds = resolved_settings.session_ttl_seconds
        self._recent_message_limit = resolved_settings.recent_message_limit

    async def get_session(self, customer_phone: str) -> ConversationSession:
        """Load one session, or create a fresh unsaved session when absent."""
        identity = normalize_customer_identity(customer_phone)
        key = f"{SESSION_KEY_PREFIX}{identity}"
        try:
            stored = await self._redis.get(key)
        except RedisError as error:
            raise _storage_error("read", error) from error

        if stored is None:
            return ConversationSession(customer_phone=identity)
        return _deserialize_session(stored, identity)

    async def save_session(self, session: ConversationSession) -> None:
        """Validate and save one session, refreshing its TTL atomically."""
        identity = normalize_customer_identity(session.customer_phone)
        now = datetime.now(UTC)
        session_data = session.model_dump(mode="python")
        session_data.update(
            {
                "customer_phone": identity,
                "customer": session.customer.model_copy(update={"phone": identity}),
                "recent_messages": session.recent_messages[-self._recent_message_limit :],
                "updated_at": now,
            }
        )

        try:
            candidate = ConversationSession.model_validate(session_data)
            serialized = candidate.model_dump_json()
            # Validate the exact representation that will cross the storage boundary.
            ConversationSession.model_validate_json(serialized)
        except (TypeError, ValueError) as error:
            raise SessionStorageUnavailableError(
                "Session could not be validated for storage", cause=error
            ) from error

        key = f"{SESSION_KEY_PREFIX}{identity}"
        try:
            stored = await self._redis.get(key)
            if stored is not None:
                existing = _deserialize_session(stored, identity)
                if existing.session_id != candidate.session_id:
                    raise SessionStorageUnavailableError(
                        "A different session already exists; reset is required"
                    )
            saved = await self._redis.set(
                key,
                serialized,
                ex=self._session_ttl_seconds,
            )
        except RedisError as error:
            raise _storage_error("write", error) from error
        if not saved:
            raise SessionStorageUnavailableError("Redis did not acknowledge the session write")

        # Only expose the normalized identity and new timestamp after Redis confirms
        # the write. A failed save leaves the caller's in-memory state untouched.
        for field_name in ConversationSession.model_fields:
            setattr(session, field_name, getattr(candidate, field_name))

    async def delete_session(self, customer_phone: str) -> None:
        """Delete a session if it exists; absence is an idempotent success."""
        key = session_key(customer_phone)
        try:
            await self._redis.delete(key)
        except RedisError as error:
            raise _storage_error("delete", error) from error

    async def reset_session(self, customer_phone: str) -> ConversationSession:
        """Delete existing state and return a fresh, unsaved session."""
        identity = normalize_customer_identity(customer_phone)
        await self.delete_session(identity)
        return ConversationSession(customer_phone=identity)


_default_service: SessionService | None = None
_default_service_signature: tuple[str, int, int] | None = None


def _get_default_service() -> SessionService:
    """Return a lazily-created service matching the current settings context."""
    global _default_service, _default_service_signature

    settings = get_settings()
    signature = (
        settings.redis_url,
        settings.session_ttl_seconds,
        settings.recent_message_limit,
    )
    if _default_service is None or _default_service_signature != signature:
        _default_service = SessionService(settings=settings)
        _default_service_signature = signature
    return _default_service


async def get_session(customer_phone: str) -> ConversationSession:
    """Load or create a session using application Redis settings."""
    return await _get_default_service().get_session(customer_phone)


async def save_session(session: ConversationSession) -> None:
    """Validate and save a session using application Redis settings."""
    await _get_default_service().save_session(session)


async def delete_session(customer_phone: str) -> None:
    """Delete a session using application Redis settings."""
    await _get_default_service().delete_session(customer_phone)


async def reset_session(customer_phone: str) -> ConversationSession:
    """Reset a session using application Redis settings."""
    return await _get_default_service().reset_session(customer_phone)


def _storage_error(operation: str, error: BaseException) -> SessionStorageUnavailableError:
    return SessionStorageUnavailableError(
        f"Redis session {operation} failed: {type(error).__name__}",
        cause=error,
    )


def _deserialize_session(stored: object, identity: str) -> ConversationSession:
    """Validate stored JSON and bind it to the Redis-key identity."""
    try:
        if not isinstance(stored, str | bytes | bytearray):
            raise TypeError("Stored session is not JSON text")
        session = ConversationSession.model_validate_json(stored)
    except (TypeError, ValueError) as error:
        raise _storage_error("validate", error) from error

    if session.customer_phone != identity or session.customer.phone != identity:
        raise SessionStorageUnavailableError("Stored session identity does not match its Redis key")
    return session


__all__ = [
    "DEVELOPMENT_IDENTITY_PREFIX",
    "SESSION_KEY_PREFIX",
    "SessionService",
    "delete_session",
    "get_session",
    "normalize_customer_identity",
    "reset_session",
    "save_session",
    "session_key",
]
