"""Real-Redis tests for atomic and resumable message processing records."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from app.core.config import Settings, get_settings
from app.services.message_deduplication_service import (
    COMPLETED_MESSAGE_TTL_SECONDS,
    ClaimDecision,
    ExternalActionState,
    MessageDeduplicationService,
    MessageOwnershipError,
    MessageStageError,
    ProcessingStage,
    SafeReplayMetadata,
    processed_message_key,
)
from redis.asyncio import Redis

TEST_REDIS_DATABASE = 14


def isolated_redis_url() -> str:
    """Keep processing-record tests out of application and other test state."""
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_REDIS_DATABASE}", parts.query, ""))


def make_settings() -> Settings:
    return Settings(
        app_env="test",
        redis_url=isolated_redis_url(),
        message_dedup_ttl_seconds=COMPLETED_MESSAGE_TTL_SECONDS,
    )


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Flush only the dedicated Redis database before and after each test."""
    client = Redis.from_url(isolated_redis_url(), decode_responses=True)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def service(
    redis_client: Redis,
    *,
    lease_seconds: float = 1,
    incomplete_ttl_seconds: int = 60,
) -> MessageDeduplicationService:
    return MessageDeduplicationService(
        redis_client,
        settings=make_settings(),
        ownership_lease_seconds=lease_seconds,
        incomplete_ttl_seconds=incomplete_ttl_seconds,
    )


def text_result() -> SafeReplayMetadata:
    return SafeReplayMetadata(response_text="Your requested information is ready.")


async def complete_text_message(
    deduplication: MessageDeduplicationService,
    message_id: str,
    owner: str,
) -> None:
    claim = await deduplication.claim_message(message_id, owner)
    assert claim.owns_processing
    await deduplication.save_result(message_id, owner, text_result())
    await deduplication.begin_text_send(message_id, owner)
    await deduplication.mark_text_sent(message_id, owner, "wamid.outbound-text")
    await deduplication.mark_message_processed(message_id, owner)


async def test_simultaneous_claims_have_exactly_one_agent_owner(redis_client: Redis) -> None:
    deduplication = service(redis_client)
    message_id = "wamid.concurrent"

    claims = await asyncio.gather(
        *(deduplication.claim_message(message_id, f"worker-{index}") for index in range(20))
    )

    owners = [claim for claim in claims if claim.owns_processing]
    assert len(owners) == 1
    assert owners[0].decision is ClaimDecision.ACQUIRED
    assert owners[0].should_run_agent is True
    assert all(claim.should_run_agent is False for claim in claims if claim is not owners[0])
    assert {claim.decision for claim in claims if claim is not owners[0]} == {ClaimDecision.BUSY}
    assert await redis_client.dbsize() == 1


async def test_completed_duplicate_never_reacquires_and_has_seven_day_ttl(
    redis_client: Redis,
) -> None:
    deduplication = service(redis_client)
    message_id = "wamid.complete"
    await complete_text_message(deduplication, message_id, "worker-original")

    duplicate = await deduplication.claim_message(message_id, "worker-duplicate")
    ttl = await redis_client.ttl(processed_message_key(message_id))

    assert duplicate.decision is ClaimDecision.COMPLETE
    assert duplicate.owns_processing is False
    assert duplicate.should_run_agent is False
    assert duplicate.record.stage is ProcessingStage.COMPLETE
    assert duplicate.record.owner_token is None
    assert await deduplication.is_message_processed(message_id) is True
    assert ttl in {COMPLETED_MESSAGE_TTL_SECONDS - 1, COMPLETED_MESSAGE_TTL_SECONDS}


async def test_incomplete_records_have_temporary_ttl(redis_client: Redis) -> None:
    deduplication = service(redis_client, incomplete_ttl_seconds=45)
    message_id = "wamid.temporary"

    await deduplication.claim_message(message_id, "worker-1")
    await deduplication.save_result(message_id, "worker-1", text_result())
    ttl = await redis_client.ttl(processed_message_key(message_id))

    assert ttl in {44, 45}
    assert ttl < COMPLETED_MESSAGE_TTL_SECONDS
    assert await deduplication.is_message_processed(message_id) is False


async def test_expired_ownership_can_be_taken_over_but_old_owner_cannot_write(
    redis_client: Redis,
) -> None:
    deduplication = service(redis_client, lease_seconds=0.08)
    message_id = "wamid.expired"
    first = await deduplication.claim_message(message_id, "worker-old")
    assert first.should_run_agent is True

    await asyncio.sleep(0.11)
    takeover = await deduplication.claim_message(message_id, "worker-new")

    assert takeover.decision is ClaimDecision.ACQUIRED
    assert takeover.record.owner_token == "worker-new"
    assert takeover.should_run_agent is True
    with pytest.raises(MessageOwnershipError):
        await deduplication.save_result(message_id, "worker-old", text_result())


async def test_persisted_result_takeover_resumes_delivery_without_agent(
    redis_client: Redis,
) -> None:
    deduplication = service(redis_client, lease_seconds=0.08)
    message_id = "wamid.replay"
    replay = SafeReplayMetadata(
        response_text="Quotation ready.",
        generated_quotation_id="GDF-Q-20260920-001",
        document_filename="GDF-Q-20260920-001.pdf",
        preview_fingerprint="f" * 64,
        preview_originating_turn_id="turn-preview",
    )
    await deduplication.claim_message(message_id, "worker-old")
    await deduplication.save_result(message_id, "worker-old", replay)

    await asyncio.sleep(0.11)
    takeover = await deduplication.claim_message(message_id, "worker-new")

    assert takeover.decision is ClaimDecision.ACQUIRED
    assert takeover.record.stage is ProcessingStage.RESULT_SAVED
    assert takeover.record.replay == replay
    assert takeover.should_run_agent is False
    assert takeover.should_resume_delivery is True


async def test_uncertain_text_send_is_not_automatically_retried_after_takeover(
    redis_client: Redis,
) -> None:
    deduplication = service(redis_client, lease_seconds=0.08)
    message_id = "wamid.uncertain"
    await deduplication.claim_message(message_id, "worker-old")
    await deduplication.save_result(message_id, "worker-old", text_result())
    uncertain = await deduplication.begin_text_send(message_id, "worker-old")
    assert uncertain.text_delivery is ExternalActionState.UNCERTAIN

    await asyncio.sleep(0.11)
    takeover = await deduplication.claim_message(message_id, "worker-new")

    assert takeover.record.has_uncertain_external_action is True
    assert takeover.should_run_agent is False
    assert takeover.should_resume_delivery is False
    with pytest.raises(MessageStageError):
        await deduplication.begin_text_send(message_id, "worker-new")

    # Only an explicit known-not-accepted result makes another attempt safe.
    reset = await deduplication.mark_text_send_not_accepted(message_id, "worker-new")
    assert reset.text_delivery is ExternalActionState.NOT_STARTED
    retry = await deduplication.begin_text_send(message_id, "worker-new")
    assert retry.text_delivery is ExternalActionState.UNCERTAIN


async def test_full_document_progress_tracks_replay_and_provider_ids(redis_client: Redis) -> None:
    deduplication = service(redis_client)
    message_id = "wamid.document"
    owner = "worker-document"
    replay = SafeReplayMetadata(
        response_text="Quotation ready.",
        generated_quotation_id="GDF-Q-20260920-002",
        document_filename="GDF-Q-20260920-002.pdf",
    )
    await deduplication.claim_message(message_id, owner)
    await deduplication.save_result(message_id, owner, replay)
    await deduplication.begin_text_send(message_id, owner)
    text_sent = await deduplication.mark_text_sent(message_id, owner, "wamid.outbound-text")
    await deduplication.begin_document_upload(message_id, owner)
    uploaded = await deduplication.mark_document_uploaded(message_id, owner, "meta-media-id")
    await deduplication.begin_document_send(message_id, owner)
    document_sent = await deduplication.mark_document_sent(
        message_id, owner, "wamid.outbound-document"
    )
    completed = await deduplication.mark_message_processed(message_id, owner)

    assert text_sent.stage is ProcessingStage.TEXT_SENT
    assert uploaded.stage is ProcessingStage.DOCUMENT_UPLOADED
    assert uploaded.document_media_id == "meta-media-id"
    assert document_sent.stage is ProcessingStage.DOCUMENT_SENT
    assert document_sent.text_outbound_id == "wamid.outbound-text"
    assert document_sent.document_outbound_id == "wamid.outbound-document"
    assert completed.stage is ProcessingStage.COMPLETE
    assert completed.replay == replay
    assert completed.document_media_id == "meta-media-id"


async def test_renewal_prevents_takeover_and_completion_checks_owner(redis_client: Redis) -> None:
    deduplication = service(redis_client, lease_seconds=0.5)
    message_id = "wamid.renewed"
    await deduplication.claim_message(message_id, "worker-owner")
    await asyncio.sleep(0.05)
    renewed = await deduplication.renew_claim(message_id, "worker-owner")
    duplicate = await deduplication.claim_message(message_id, "worker-other")

    assert renewed.owner_token == "worker-owner"
    assert duplicate.decision is ClaimDecision.BUSY
    with pytest.raises(MessageOwnershipError):
        await deduplication.mark_message_processed(message_id, "worker-other")


async def test_key_uses_contract_hyphenated_prefix(redis_client: Redis) -> None:
    deduplication = service(redis_client)
    await deduplication.claim_message("wamid.key", "worker-key")

    assert await redis_client.exists("gdf:processed-message:wamid.key") == 1
    assert await redis_client.exists("gdf:processed_message:wamid.key") == 0
