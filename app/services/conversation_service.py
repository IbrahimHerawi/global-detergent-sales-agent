"""Shared, Redis-serialized conversation processing for every text transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.agent.agent import handle_message as handle_agent_message
from app.core.config import Settings, get_settings
from app.core.exceptions import AgentExecutionError, SessionStorageUnavailableError
from app.schemas.agent import AgentResult, PreviewDeliveryMetadata
from app.schemas.quotation import GeneratedQuotation, QuoteCartItem
from app.schemas.session import ConversationSession, ConversationState
from app.services.quotation_service import CurrentTurnContext, QuotationService
from app.services.session_service import (
    SessionService,
    normalize_customer_identity,
)

CONVERSATION_LOCK_KEY_PREFIX: Final = "gdf:conversation-lock:"
DEFAULT_LOCK_LEASE_SECONDS: Final = 60.0
DEFAULT_LOCK_WAIT_SECONDS: Final = 180.0
DEFAULT_LOCK_POLL_SECONDS: Final = 0.05

_RENEW_LOCK_SCRIPT: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LOCK_SCRIPT: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class ConversationAgent(Protocol):
    """Agent boundary consumed by the common application service."""

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        """Process one message and return the updated backend-owned state."""
        ...


class _ConfiguredAgent:
    """Defer default OpenAI client construction until a turn is processed."""

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        return await handle_agent_message(session, user_message, turn_context)


@dataclass(frozen=True, slots=True)
class ConversationResult:
    """Transport-neutral output plus trusted internal delivery metadata."""

    response_text: str
    session_id: UUID
    state: ConversationState
    selected_product_id: str | None
    cart: tuple[QuoteCartItem, ...]
    generated_quote: GeneratedQuotation | None = None
    prepared_preview: PreviewDeliveryMetadata | None = None

    @property
    def generated_quotation(self) -> GeneratedQuotation | None:
        """Compatibility alias naming the generated artifact explicitly."""
        return self.generated_quote

    @property
    def preview_delivery_metadata(self) -> PreviewDeliveryMetadata | None:
        """Return the binding accepted by successful-delivery acknowledgement."""
        return self.prepared_preview


class ConversationService:
    """Execute complete turns while holding a renewable per-sender Redis lock."""

    __slots__ = (
        "_agent",
        "_lock_lease_seconds",
        "_lock_poll_seconds",
        "_lock_wait_seconds",
        "_max_inbound_text_length",
        "_quotation_service",
        "_redis",
        "_session_service",
        "_token_factory",
        "_turn_id_factory",
    )

    def __init__(
        self,
        redis_client: Redis | None = None,
        *,
        settings: Settings | None = None,
        session_service: SessionService | None = None,
        agent: ConversationAgent | None = None,
        quotation_service: QuotationService | None = None,
        lock_lease_seconds: float = DEFAULT_LOCK_LEASE_SECONDS,
        lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
        lock_poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
        token_factory: Callable[[], str] | None = None,
        turn_id_factory: Callable[[], str] | None = None,
    ) -> None:
        resolved_settings = settings or get_settings()
        _validate_positive_duration(lock_lease_seconds, "lock_lease_seconds")
        _validate_positive_duration(lock_wait_seconds, "lock_wait_seconds")
        _validate_positive_duration(lock_poll_seconds, "lock_poll_seconds")

        self._redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(resolved_settings.redis_url, decode_responses=True)
        )
        self._session_service = session_service or SessionService(
            self._redis,
            settings=resolved_settings,
        )
        self._agent = agent or _ConfiguredAgent()
        self._quotation_service = quotation_service or QuotationService()
        self._max_inbound_text_length = resolved_settings.max_inbound_text_length
        self._lock_lease_seconds = lock_lease_seconds
        self._lock_wait_seconds = lock_wait_seconds
        self._lock_poll_seconds = lock_poll_seconds
        self._token_factory = token_factory or (lambda: uuid4().hex)
        self._turn_id_factory = turn_id_factory or (lambda: uuid4().hex)

    async def process_message(
        self,
        sender: str,
        message: str,
    ) -> ConversationResult:
        """Validate and process one development-chat or WhatsApp text turn.

        Persistence completes before the result is returned to a transport. In
        particular, generated quotation replay metadata is durable before an
        outbound text or document delivery can be attempted.
        """
        identity = normalize_customer_identity(sender)
        inbound_text = self._validate_message(message)
        lock = self._sender_lock(identity)

        try:
            async with lock:
                session = await self._session_service.get_session(identity)
                turn_context = _build_turn_context(
                    session,
                    inbound_text,
                    self._new_identifier(self._turn_id_factory, "turn ID"),
                )
                agent_result = await self._agent.handle_message(
                    session,
                    inbound_text,
                    turn_context,
                )
                _validate_agent_result(agent_result, session, identity)
                await lock.ensure_owned()
                await self._session_service.save_session(agent_result.updated_session)
                await lock.ensure_owned()
                return _conversation_result(agent_result)
        except asyncio.CancelledError:
            if lock.lost_error is not None:
                raise lock.lost_error from lock.lost_error.cause
            raise

    async def process(
        self,
        sender: str,
        message: str,
    ) -> ConversationResult:
        """Concise alias for the common processing entry point."""
        return await self.process_message(sender, message)

    async def acknowledge_successful_delivery(
        self,
        sender: str,
        preview: PreviewDeliveryMetadata,
    ) -> ConversationState:
        """Mark exactly the matching preview ready after transport success.

        Failed deliveries must not call this method. Loading and transition are
        intentionally separate from processing because calculation is not proof
        that the response reached its transport.
        """
        identity = normalize_customer_identity(sender)
        if not isinstance(preview, PreviewDeliveryMetadata):
            raise TypeError("preview must be backend-created delivery metadata")
        lock = self._sender_lock(identity)

        try:
            async with lock:
                session = await self._session_service.get_session(identity)
                self._quotation_service.mark_ready_for_confirmation(
                    session,
                    preview.fingerprint,
                    preview.originating_turn_id,
                )
                await lock.ensure_owned()
                await self._session_service.save_session(session)
                await lock.ensure_owned()
                return session.state
        except asyncio.CancelledError:
            if lock.lost_error is not None:
                raise lock.lost_error from lock.lost_error.cause
            raise

    async def acknowledge_delivery(
        self,
        sender: str,
        preview: PreviewDeliveryMetadata,
    ) -> ConversationState:
        """Alias retained for transport adapters with shorter terminology."""
        return await self.acknowledge_successful_delivery(sender, preview)

    def _validate_message(self, message: str) -> str:
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        if not message.strip():
            raise ValueError("message must not be blank")
        if len(message) > self._max_inbound_text_length:
            raise ValueError(f"message must not exceed {self._max_inbound_text_length} characters")
        return message

    def _sender_lock(self, identity: str) -> _RedisOwnershipLock:
        return _RedisOwnershipLock(
            self._redis,
            key=f"{CONVERSATION_LOCK_KEY_PREFIX}{identity}",
            token=self._new_identifier(self._token_factory, "ownership token"),
            lease_seconds=self._lock_lease_seconds,
            wait_seconds=self._lock_wait_seconds,
            poll_seconds=self._lock_poll_seconds,
        )

    @staticmethod
    def _new_identifier(factory: Callable[[], str], label: str) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} factory must return a nonblank string")
        return value.strip()


class _RedisOwnershipLock:
    """Renewable compare-by-token Redis lease for one conversation owner."""

    __slots__ = (
        "_key",
        "_lease_ms",
        "_owner_task",
        "_poll_seconds",
        "_redis",
        "_renew_task",
        "_token",
        "_wait_seconds",
        "lost_error",
    )

    def __init__(
        self,
        redis_client: Redis,
        *,
        key: str,
        token: str,
        lease_seconds: float,
        wait_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._redis = redis_client
        self._key = key
        self._token = token
        self._lease_ms = max(1, int(lease_seconds * 1_000))
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._renew_task: asyncio.Task[None] | None = None
        self._owner_task: asyncio.Task[object] | None = None
        self.lost_error: SessionStorageUnavailableError | None = None

    async def __aenter__(self) -> _RedisOwnershipLock:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait_seconds
        while True:
            try:
                acquired = await self._redis.set(
                    self._key,
                    self._token,
                    nx=True,
                    px=self._lease_ms,
                )
            except RedisError as error:
                raise _lock_storage_error("acquire", error) from error
            if acquired:
                owner = asyncio.current_task()
                if owner is None:  # pragma: no cover - asyncio always owns a running coroutine
                    raise RuntimeError("conversation lock requires an asyncio task")
                self._owner_task = owner
                self._renew_task = asyncio.create_task(self._renew_loop())
                return self
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SessionStorageUnavailableError(
                    "Timed out waiting for the sender conversation lock"
                )
            await asyncio.sleep(min(self._poll_seconds, remaining))

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        renew_task = self._renew_task
        self._renew_task = None
        if renew_task is not None:
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task

        try:
            await self._redis.eval(
                _RELEASE_LOCK_SCRIPT,
                1,
                self._key,
                self._token,
            )
        except RedisError as error:
            if exc is None and self.lost_error is None:
                raise _lock_storage_error("release", error) from error

        if exc is None and self.lost_error is not None:
            raise self.lost_error from self.lost_error.cause

    async def ensure_owned(self) -> None:
        """Atomically prove ownership and refresh the lease before a boundary."""
        if self.lost_error is not None:
            raise self.lost_error from self.lost_error.cause
        try:
            renewed = await self._redis.eval(
                _RENEW_LOCK_SCRIPT,
                1,
                self._key,
                self._token,
                self._lease_ms,
            )
        except RedisError as error:
            failure = _lock_storage_error("verify", error)
            self.lost_error = failure
            raise failure from error
        if renewed != 1:
            failure = SessionStorageUnavailableError(
                "Conversation lock ownership was lost before persistence"
            )
            self.lost_error = failure
            raise failure

    async def _renew_loop(self) -> None:
        interval = max(0.001, self._lease_ms / 3_000)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self._redis.eval(
                        _RENEW_LOCK_SCRIPT,
                        1,
                        self._key,
                        self._token,
                        self._lease_ms,
                    )
                except RedisError as error:
                    self.lost_error = _lock_storage_error("renew", error)
                    self._cancel_owner()
                    return
                if renewed != 1:
                    self.lost_error = SessionStorageUnavailableError(
                        "Conversation lock ownership was lost during processing"
                    )
                    self._cancel_owner()
                    return
        except asyncio.CancelledError:
            raise

    def _cancel_owner(self) -> None:
        owner = self._owner_task
        if owner is not None and not owner.done():
            owner.cancel()


def _build_turn_context(
    session: ConversationSession,
    message: str,
    turn_id: str,
) -> CurrentTurnContext:
    preceding_turn_ids = (
        (session.preview_originating_turn_id,)
        if session.preview_originating_turn_id is not None
        else ()
    )
    return CurrentTurnContext(
        session_id=session.session_id,
        turn_id=turn_id,
        customer_message=message,
        preceding_turn_ids=preceding_turn_ids,
    )


def _validate_agent_result(
    result: AgentResult,
    loaded_session: ConversationSession,
    identity: str,
) -> None:
    if not isinstance(result, AgentResult):
        raise AgentExecutionError("Agent returned an invalid conversation result")
    updated_session = result.updated_session
    if (
        updated_session.session_id != loaded_session.session_id
        or updated_session.customer_phone != identity
        or updated_session.customer.phone != identity
    ):
        raise AgentExecutionError("Agent returned state for a different conversation")


def _conversation_result(result: AgentResult) -> ConversationResult:
    session = result.updated_session
    return ConversationResult(
        response_text=result.response_text,
        session_id=session.session_id,
        state=session.state,
        selected_product_id=session.selected_product_id,
        cart=tuple(item.model_copy(deep=True) for item in session.cart),
        generated_quote=result.generated_quote,
        prepared_preview=result.prepared_preview,
    )


def _validate_positive_duration(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _lock_storage_error(operation: str, error: BaseException) -> SessionStorageUnavailableError:
    return SessionStorageUnavailableError(
        f"Redis conversation lock {operation} failed: {type(error).__name__}",
        cause=error,
    )


__all__ = [
    "CONVERSATION_LOCK_KEY_PREFIX",
    "ConversationAgent",
    "ConversationResult",
    "ConversationService",
]
