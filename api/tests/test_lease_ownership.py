"""Claim ownership under lease expiry, using separate DB sessions.

These tests simulate two workers: worker A claims, stalls past its lease,
worker B reclaims and succeeds, then A finishes late. A's late write must not
corrupt B's result or trigger duplicate upstream work.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import UpstreamError
from app.models import TicketMapping
from app.repositories import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    ProcessedEventRepository,
    TicketMappingRepository,
)
from app.services.slack_ticket import SlackMessageContext, TicketCreationService
from app.services.zendesk_reaction import ReactionService, ZendeskTicketEvent

LEASE_SECONDS = 60


def ctx(**overrides) -> SlackMessageContext:
    base = dict(
        team_id="T1",
        channel_id="C1",
        message_ts="1700000000.000100",
        user_id="U1",
        text="laptop broken",
        event_id="Ev1",
    )
    base.update(overrides)
    return SlackMessageContext(**base)  # type: ignore[arg-type]


async def expire_lease(db_factory, source: str, key: str, *, seconds: int) -> None:
    """Backdate a claim so a competing worker can treat the lease as expired."""
    from app.models import ProcessedEvent

    stale = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    async with db_factory() as s:
        await s.execute(
            sqlalchemy.update(ProcessedEvent)
            .values(updated_at=stale)
            .where(
                ProcessedEvent.source == source,
                ProcessedEvent.dedupe_key == key,
            )
        )
        await s.commit()


def ticket_service(zendesk, s: AsyncSession) -> TicketCreationService:
    return TicketCreationService(
        zendesk=zendesk,
        ticket_repo=TicketMappingRepository(s),
        event_repo=ProcessedEventRepository(s),
        claim_timeout_seconds=LEASE_SECONDS,
    )


def reaction_service(slack, s: AsyncSession) -> ReactionService:
    return ReactionService(
        slack=slack,
        ticket_repo=TicketMappingRepository(s),
        event_repo=ProcessedEventRepository(s),
        reactions={"solved": "white_check_mark"},
        claim_timeout_seconds=LEASE_SECONDS,
    )


class TestStaleWorkerCannotOverwriteNewerState:
    async def test_stale_fail_cannot_flip_newer_completion(self, db_factory):
        async with db_factory() as s1:
            a_repo = ProcessedEventRepository(s1)
            claim_a = await a_repo.claim("slack", "K", claim_timeout_seconds=LEASE_SECONDS)

        await expire_lease(db_factory, "slack", "K", seconds=600)

        async with db_factory() as s2:
            b_repo = ProcessedEventRepository(s2)
            claim_b = await b_repo.claim("slack", "K", claim_timeout_seconds=LEASE_SECONDS)
            assert claim_b is not None
            await b_repo.complete(claim_b, "ticket-1")

        # Worker A finally fails, late.
        async with db_factory() as s3:
            a_repo = ProcessedEventRepository(s3)
            assert await a_repo.fail(claim_a, "late-error") is False
            row = await a_repo.get("slack", "K")
            assert row.status == STATUS_COMPLETED
            assert row.detail == "ticket-1"

    async def test_stale_complete_cannot_overwrite_newer_failure(self, db_factory):
        async with db_factory() as s1:
            claim_a = await ProcessedEventRepository(s1).claim(
                "zendesk", "K", claim_timeout_seconds=LEASE_SECONDS
            )
        await expire_lease(db_factory, "zendesk", "K", seconds=600)

        async with db_factory() as s2:
            b_repo = ProcessedEventRepository(s2)
            claim_b = await b_repo.claim("zendesk", "K", claim_timeout_seconds=LEASE_SECONDS)
            await b_repo.fail(claim_b, "slack down")

        async with db_factory() as s3:
            a_repo = ProcessedEventRepository(s3)
            assert await a_repo.complete(claim_a, "too-late") is False
            row = await a_repo.get("zendesk", "K")
            assert row.status == STATUS_FAILED
            assert row.detail == "slack down"

    async def test_reclaim_issues_a_new_owner_token(self, db_factory):
        async with db_factory() as s1:
            first = await ProcessedEventRepository(s1).claim(
                "slack", "K", claim_timeout_seconds=LEASE_SECONDS
            )
        await expire_lease(db_factory, "slack", "K", seconds=600)
        async with db_factory() as s2:
            second = await ProcessedEventRepository(s2).claim(
                "slack", "K", claim_timeout_seconds=LEASE_SECONDS
            )
        assert first is not None and second is not None
        assert first.token != second.token

    async def test_live_lease_is_not_stolen(self, db_factory):
        """Under supported config the lease outlives an upstream call, so no
        second worker can take over while the first is still in flight."""
        async with db_factory() as s1:
            assert (
                await ProcessedEventRepository(s1).claim(
                    "slack", "K", claim_timeout_seconds=LEASE_SECONDS
                )
                is not None
            )
        async with db_factory() as s2:
            assert (
                await ProcessedEventRepository(s2).claim(
                    "slack", "K", claim_timeout_seconds=LEASE_SECONDS
                )
                is None
            )


class TestNoDuplicateUpstreamWork:
    async def test_second_worker_after_expiry_does_not_create_second_ticket(
        self, db_factory, fake_zendesk
    ):
        """Worker B reclaims and finds the mapping, so it must not call Zendesk."""
        async with db_factory() as s1:
            svc = ticket_service(fake_zendesk, s1)
            mapping = await svc.create_ticket_from_message(ctx())
            assert mapping is not None
        assert len(fake_zendesk.calls) == 1

        # A later delivery (new event id) must not create another ticket.
        async with db_factory() as s2:
            svc = ticket_service(fake_zendesk, s2)
            assert await svc.create_ticket_from_message(ctx(event_id="Ev2")) is None
        assert len(fake_zendesk.calls) == 1

    async def test_stale_worker_failure_keeps_newer_mapping_intact(
        self, db_factory, fake_zendesk
    ):
        """A stalled worker A whose call failed late must not mark the key
        failed after B already completed it."""
        async with db_factory() as s1:
            svc = ticket_service(fake_zendesk, s1)
            claim_a = await svc._events.claim(  # noqa: SLF001
                "slack", ctx().dedupe_key, claim_timeout_seconds=LEASE_SECONDS
            )
        await expire_lease(db_factory, "slack", ctx().dedupe_key, seconds=600)

        async with db_factory() as s2:
            svc = ticket_service(fake_zendesk, s2)
            assert await svc.create_ticket_from_message(ctx()) is not None

        # A finishes late with an error.
        async with db_factory() as s3:
            svc = ticket_service(fake_zendesk, s3)
            await svc._fail_claim(claim_a, "late upstream error")  # noqa: SLF001

        async with db_factory() as s4:
            repo = ProcessedEventRepository(s4)
            row = await repo.get("slack", ctx().dedupe_key)
            assert row.status == STATUS_COMPLETED
            assert (
                await TicketMappingRepository(s4).find_by_slack_message("T1", "C1", "1700000000.000100")
                is not None
            )

        # And a further redelivery still must not create a second ticket.
        async with db_factory() as s5:
            svc = ticket_service(fake_zendesk, s5)
            assert await svc.create_ticket_from_message(ctx(event_id="Ev3")) is None
        assert len(fake_zendesk.calls) == 1

    async def test_failed_attempt_remains_retryable_and_creates_one_ticket(
        self, db_factory, fake_zendesk
    ):
        fake_zendesk.error = UpstreamError("zendesk down")
        async with db_factory() as s1:
            svc = ticket_service(fake_zendesk, s1)
            with pytest.raises(UpstreamError):
                await svc.create_ticket_from_message(ctx())
            row = await svc._events.get("slack", ctx().dedupe_key)  # noqa: SLF001
            assert row.status == STATUS_FAILED

        fake_zendesk.error = None
        async with db_factory() as s2:
            svc = ticket_service(fake_zendesk, s2)
            assert await svc.create_ticket_from_message(ctx()) is not None
        assert len(fake_zendesk.calls) == 2

        async with db_factory() as s3:
            svc = ticket_service(fake_zendesk, s3)
            assert await svc.create_ticket_from_message(ctx(event_id="Ev9")) is None
        assert len(fake_zendesk.calls) == 2

    async def test_reaction_not_reapplied_after_newer_completion(self, db_factory, fake_slack):
        async with db_factory() as s1:
            repo = TicketMappingRepository(s1)
            await repo.add(
                TicketMapping(
                    zendesk_ticket_id=7,
                    slack_team_id="T1",
                    slack_channel_id="C1",
                    slack_message_ts="1700000000.000100",
                    external_id="slack:T1:C1:1700000000.000100",
                )
            )
            await repo.commit()

        event = ZendeskTicketEvent(ticket_id=7, status="solved", invocation_id="inv-1")
        async with db_factory() as s1:
            assert await reaction_service(fake_slack, s1).apply_reaction(event) == "added"
        assert len(fake_slack.calls) == 1

        await expire_lease(db_factory, "zendesk", "inv-1", seconds=600)
        async with db_factory() as s2:
            # Lease expired, but the work is completed: must be suppressed.
            assert await reaction_service(fake_slack, s2).apply_reaction(event) == "duplicate"
        assert len(fake_slack.calls) == 1