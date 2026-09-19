"""Redis integration coverage for temporary conversation sessions."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import SessionStorageUnavailableError
from app.schemas.session import ConversationMessage, ConversationSession, ConversationState
from app.services.session_service import (
    SessionService,
    normalize_customer_identity,
    session_key,
)
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 15


def isolated_redis_url() -> str:
    """Force integration tests onto the Compose Redis test database."""
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_REDIS_DATABASE}", parts.query, ""))


def make_settings(*, ttl: int = 30, recent_message_limit: int = 20) -> Settings:
    return Settings(
        app_env="test",
        redis_url=isolated_redis_url(),
        session_ttl_seconds=ttl,
        recent_message_limit=recent_message_limit,
    )


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Provide a clean Redis database isolated from development database zero."""
    client = Redis.from_url(isolated_redis_url(), decode_responses=True)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("97450000000", "+97450000000"),
        ("+97450000000", "+97450000000"),
        (" 97450000000 ", "+97450000000"),
        ("0097450000000", "+0097450000000"),
        ("dev:browser-session_1", "dev:browser-session_1"),
    ],
)
def test_transport_identity_normalization(value: str, expected: str) -> None:
    assert normalize_customer_identity(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "+", "974 5000 0000", "974-5000-0000", "whatsapp:+97450000000", "dev:"],
)
def test_invalid_transport_identities_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_customer_identity(value)


async def test_real_redis_serialization_and_normalized_key(redis_client: Redis) -> None:
    service = SessionService(redis_client, settings=make_settings())
    created_at = datetime(2020, 9, 19, 8, 0, tzinfo=UTC)
    session = ConversationSession(
        customer_phone="97450000000",
        state=ConversationState.PRODUCT_DISCUSSION,
        selected_product_id="GDF-FLC-001",
        created_at=created_at,
        updated_at=created_at,
    )

    await service.save_session(session)

    raw = await redis_client.get("gdf:session:+97450000000")
    assert raw is not None
    payload = json.loads(raw)
    assert payload["session_id"] == str(session.session_id)
    assert payload["customer_phone"] == "+97450000000"
    assert payload["customer"]["phone"] == "+97450000000"
    assert payload["created_at"] == created_at.isoformat().replace("+00:00", "Z")
    assert session.customer_phone == "+97450000000"
    assert session.updated_at > created_at

    restored = await service.get_session("+97450000000")
    assert restored == session
    assert restored.created_at == created_at


async def test_save_refreshes_ttl_and_preserves_created_at(redis_client: Redis) -> None:
    service = SessionService(redis_client, settings=make_settings(ttl=4))
    session = ConversationSession(customer_phone="97450000001")
    original_created_at = session.created_at

    await service.save_session(session)
    first_updated_at = session.updated_at
    await asyncio.sleep(1.1)
    reduced_ttl = await redis_client.ttl(session_key(session.customer_phone))

    await service.save_session(session)
    refreshed_ttl = await redis_client.ttl(session_key(session.customer_phone))

    assert reduced_ttl < refreshed_ttl
    assert refreshed_ttl in {3, 4}
    assert session.created_at == original_created_at
    assert session.updated_at > first_updated_at


async def test_expired_or_absent_session_gets_a_new_uuid(redis_client: Redis) -> None:
    service = SessionService(redis_client, settings=make_settings(ttl=1))
    first = await service.get_session("97450000002")
    await service.save_session(first)

    await asyncio.sleep(1.1)
    fresh = await service.get_session("97450000002")

    assert fresh.session_id != first.session_id
    assert fresh.customer_phone == "+97450000002"
    assert await redis_client.exists(session_key("97450000002")) == 0


async def test_delete_and_reset_remove_state(redis_client: Redis) -> None:
    service = SessionService(redis_client, settings=make_settings())
    original = ConversationSession(customer_phone="97450000003")
    await service.save_session(original)

    reset = await service.reset_session("+97450000003")

    assert reset.session_id != original.session_id
    assert reset.customer_phone == "+97450000003"
    assert await redis_client.exists(session_key("97450000003")) == 0
    await service.delete_session("97450000003")


async def test_development_and_whatsapp_identities_are_separate(redis_client: Redis) -> None:
    service = SessionService(redis_client, settings=make_settings())
    phone_session = ConversationSession(customer_phone="123456")
    development_session = ConversationSession(customer_phone="dev:123456")
    await service.save_session(phone_session)
    await service.save_session(development_session)

    assert session_key("123456") == "gdf:session:+123456"
    assert session_key("dev:123456") == "gdf:session:dev:123456"
    assert (await service.get_session("123456")).session_id == phone_session.session_id
    assert (
        await service.get_session("dev:123456")
    ).session_id == development_session.session_id


async def test_save_bounds_recent_message_history(redis_client: Redis) -> None:
    service = SessionService(
        redis_client,
        settings=make_settings(recent_message_limit=2),
    )
    session = ConversationSession(
        customer_phone="97450000004",
        recent_messages=[
            ConversationMessage(role="user", content="one"),
            ConversationMessage(role="assistant", content="two"),
            ConversationMessage(role="user", content="three"),
        ],
    )

    await service.save_session(session)
    restored = await service.get_session("97450000004")

    assert [message.content for message in restored.recent_messages] == ["two", "three"]
    assert restored.recent_messages == session.recent_messages


async def test_malformed_stored_session_fails_closed_without_overwrite(
    redis_client: Redis,
) -> None:
    service = SessionService(redis_client, settings=make_settings())
    key = session_key("97450000005")
    malformed = '{"session_id":"not-a-valid-session"}'
    await redis_client.set(key, malformed, ex=30)

    with pytest.raises(SessionStorageUnavailableError):
        await service.get_session("97450000005")

    assert await redis_client.get(key) == malformed

    replacement = ConversationSession(customer_phone="97450000005")
    with pytest.raises(SessionStorageUnavailableError):
        await service.save_session(replacement)

    assert await redis_client.get(key) == malformed


async def test_wrong_redis_value_type_is_reported_as_storage_failure(
    redis_client: Redis,
) -> None:
    service = SessionService(redis_client, settings=make_settings())
    key = session_key("97450000006")
    await redis_client.lpush(key, "invalid-session-value")

    with pytest.raises(SessionStorageUnavailableError) as caught:
        await service.get_session("97450000006")

    assert caught.value.cause is not None
    assert await redis_client.type(key) == "list"


async def test_unavailable_redis_does_not_advance_updated_at() -> None:
    unavailable = Redis.from_url(
        "redis://redis:1/15",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    service = SessionService(unavailable, settings=make_settings())
    session = ConversationSession(customer_phone="97450000007")
    original_updated_at = session.updated_at

    try:
        with pytest.raises(SessionStorageUnavailableError):
            await service.save_session(session)
    finally:
        await unavailable.aclose()

    assert session.updated_at == original_updated_at


async def test_stored_identity_mismatch_fails_closed(redis_client: Redis) -> None:
    service = SessionService(redis_client, settings=make_settings())
    wrong_identity = ConversationSession(customer_phone="+97450000999")
    key = session_key("97450000008")
    await redis_client.set(key, wrong_identity.model_dump_json(), ex=30)

    with pytest.raises(SessionStorageUnavailableError):
        await service.get_session("97450000008")

    assert await redis_client.get(key) == wrong_identity.model_dump_json()
