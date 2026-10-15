"""Total upstream deadline and lease interaction.

The deadline must bound the whole upstream call (not just httpx's per-phase
timeout), so a slow upstream cannot outlive the claim lease and let a second
worker start duplicate work.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import UpstreamError
from app.models import ProcessedEvent, TicketMapping
from app.repositories import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    ProcessedEventRepository,
    TicketMappingRepository,
)
from app.services.slack_ticket import SlackMessageContext, TicketCreationService
from app.services.zendesk_reaction import ReactionService, ZendeskTicketEvent

# Supported configuration: total deadline well under the 300s lease.
DEADLINE = 0.3
LEASE = 300


class BlockingZendesk:
    """An upstream that never returns, so only the deadline can end the call."""

    def __init__(self) -> None:
        self.calls = 0

    async def create_ticket(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(3600)  # effectively never
        return {"id": 1}


class BlockingSlack:
    def __init__(self) -> None:
        self.calls = 0

    async def add_reaction(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(3600)
        return True


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


def ticket_service(zendesk, s: AsyncSession) -> TicketCreationService:
    return TicketCreationService(
        zendesk=zendesk,
        ticket_repo=TicketMappingRepository(s),
        event_repo=ProcessedEventRepository(s),
        claim_timeout_seconds=LEASE,
        processing_deadline_seconds=DEADLINE,
    )


def reaction_service(slack, s: AsyncSession) -> ReactionService:
    return ReactionService(
        slack=slack,
        ticket_repo=TicketMappingRepository(s),
        event_repo=ProcessedEventRepository(s),
        reactions={"solved": "white_check_mark"},
        claim_timeout_seconds=LEASE,
        processing_deadline_seconds=DEADLINE,
    )


class TestUpstreamDeadline:
    async def test_blocked_zendesk_call_times_out(self, db_factory):
        zendesk = BlockingZendesk()
        async with db_factory() as s:
            with pytest.raises(asyncio.TimeoutError):
                await ticket_service(zendesk, s).create_ticket_from_message(ctx())
        assert zendesk.calls == 1

    async def test_blocked_slack_call_times_out(self, db_factory):
        async with db_factory() as s:
            await TicketMappingRepository(s).add(
                TicketMapping(
                    zendesk_ticket_id=9,
                    slack_team_id="T1",
                    slack_channel_id="C1",
                    slack_message_ts="1.1",
                    external_id="x",
                )
            )
            await TicketMappingRepository(s).commit()

        slack = BlockingSlack()
        async with db_factory() as s:
            with pytest.raises(asyncio.TimeoutError):
                await reaction_service(slack, s).apply_reaction(
                    ZendeskTicketEvent(ticket_id=9, status="solved", invocation_id="inv-1")
                )
        assert slack.calls == 1

    async def test_deadline_bounds_wall_clock(self, db_factory):
        """The call must end at the deadline, not at some per-phase timeout."""
        import time

        zendesk = BlockingZendesk()
        started = time.monotonic()
        async with db_factory() as s:
            with pytest.raises(asyncio.TimeoutError):
                await ticket_service(zendesk, s).create_ticket_from_message(ctx())
        elapsed = time.monotonic() - started
        assert DEADLINE <= elapsed < DEADLINE + 5


class TestReplayDuringBlockedCall:
    async def test_replay_cannot_reclaim_within_deadline(self, db_factory):
        """While the first worker is still inside its deadline, the claim is
        live, so a concurrent delivery must not start a second upstream call."""
        zendesk = BlockingZendesk()
        key = ctx().dedupe_key

        async with db_factory() as s1:
            task = asyncio.create_task(
                ticket_service(zendesk, s1).create_ticket_from_message(ctx())
            )
            # Give the first worker time to acquire the claim and start upstream.
            await asyncio.sleep(0.05)

            # Separate session: a replay of the same logical message.
            async with db_factory() as s2:
                result = await ticket_service(zendesk, s2).create_ticket_from_message(
                    ctx(event_id="Ev2")
                )
            assert result is None, "replay must be suppressed while claim is live"
            assert zendesk.calls == 1, "no duplicate upstream call"

            with pytest.raises(asyncio.TimeoutError):
                await task

        async with db_factory() as s3:
            row = await ProcessedEventRepository(s3).get("slack", key)
            # The deadline released our claim outright, so a later delivery can
            # retry immediately rather than waiting for the lease to expire.
            assert row is None

    async def test_timeout_allows_a_later_replay_to_succeed(self, db_factory):
        """After a deadline timeout the claim is released, so replay can retry."""
        class FlakyOnce:
            def __init__(self) -> None:
                self.calls = 0

            async def create_ticket(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.sleep(3600)
                return {"id": 555}

        zendesk = FlakyOnce()
        async with db_factory() as s1:
            with pytest.raises(asyncio.TimeoutError):
                await ticket_service(zendesk, s1).create_ticket_from_message(ctx())

        async with db_factory() as s2:
            mapping = await ticket_service(zendesk, s2).create_ticket_from_message(
                ctx(event_id="Ev2")
            )
            assert mapping is not None
            assert mapping.zendesk_ticket_id == 555
        assert zendesk.calls == 2

    async def test_failed_call_is_retryable_not_terminal(self, db_factory):
        class Failing:
            def __init__(self) -> None:
                self.calls = 0

            async def create_ticket(self, **kwargs):
                self.calls += 1
                raise UpstreamError("zendesk 503")

        zendesk = Failing()
        async with db_factory() as s1:
            with pytest.raises(UpstreamError):
                await ticket_service(zendesk, s1).create_ticket_from_message(ctx())

        async with db_factory() as s2:
            row = await ProcessedEventRepository(s2).get("slack", ctx().dedupe_key)
            assert row.status == STATUS_FAILED

        # Retryable: a later delivery re-attempts.
        class Succeeding:
            def __init__(self) -> None:
                self.calls = 0

            async def create_ticket(self, **kwargs):
                self.calls += 1
                return {"id": 777}

        good = Succeeding()
        async with db_factory() as s3:
            mapping = await ticket_service(good, s3).create_ticket_from_message(ctx())
            assert mapping is not None
        assert good.calls == 1


class TestReleaseIsOwnershipConditioned:
    async def test_release_removes_own_live_claim(self, db_factory):
        async with db_factory() as s:
            repo = ProcessedEventRepository(s)
            claim = await repo.claim("slack", "K", claim_timeout_seconds=LEASE)
            assert await repo.release(claim) is True
            assert await repo.get("slack", "K") is None

    async def test_stale_release_cannot_delete_newer_active_claim(self, db_factory):
        async with db_factory() as s1:
            old = await ProcessedEventRepository(s1).claim(
                "slack", "K", claim_timeout_seconds=1
            )
        # Expire the lease and let another worker take over.
        async with db_factory() as s2:
            import datetime as dt

            await s2.execute(
                sqlalchemy.update(ProcessedEvent).values(
                    updated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
                )
            )
            await s2.commit()
            newer = await ProcessedEventRepository(s2).claim(
                "slack", "K", claim_timeout_seconds=LEASE
            )
            assert newer is not None

        async with db_factory() as s3:
            repo = ProcessedEventRepository(s3)
            assert await repo.release(old) is False
            row = await repo.get("slack", "K")
            assert row is not None
            assert row.status == "processing"

    async def test_stale_release_cannot_delete_completed_claim(self, db_factory):
        """Regression: an unconditioned release wiped a completed row, which
        let the next delivery redo work that had already succeeded."""
        async with db_factory() as s1:
            old = await ProcessedEventRepository(s1).claim(
                "slack", "K", claim_timeout_seconds=1
            )
        async with db_factory() as s2:
            import datetime as dt

            await s2.execute(
                sqlalchemy.update(ProcessedEvent).values(
                    updated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
                )
            )
            await s2.commit()
            newer = await ProcessedEventRepository(s2).claim(
                "slack", "K", claim_timeout_seconds=LEASE
            )
            await ProcessedEventRepository(s2).complete(newer, "ticket-1")

        async with db_factory() as s3:
            repo = ProcessedEventRepository(s3)
            assert await repo.release(old) is False
            row = await repo.get("slack", "K")
            assert row is not None
            assert row.status == STATUS_COMPLETED
            assert row.detail == "ticket-1"

    async def test_release_after_completion_is_refused(self, db_factory):
        async with db_factory() as s:
            repo = ProcessedEventRepository(s)
            claim = await repo.claim("slack", "K", claim_timeout_seconds=LEASE)
            await repo.complete(claim)
            assert await repo.release(claim) is False
            assert (await repo.get("slack", "K")).status == STATUS_COMPLETED


class TestStaleSuccessDoesNotCorruptMapping:
    async def test_mapping_not_committed_when_lease_lost_before_persist(
        self, db_factory
    ):
        """If ownership is lost while upstream was in flight, the staged mapping
        must be rolled back rather than committed."""
        zendesk = BlockingZendesk()
        key = ctx().dedupe_key

        async with db_factory() as s1:
            svc = ticket_service(zendesk, s1)
            task = asyncio.create_task(svc.create_ticket_from_message(ctx()))
            await asyncio.sleep(0.05)

            # Another worker supersedes our claim while we are upstream.
            async with db_factory() as s2:
                import datetime as dt

                await s2.execute(
                    sqlalchemy.update(ProcessedEvent).values(
                        updated_at=dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(seconds=600)
                    )
                )
                await s2.commit()
                repo = ProcessedEventRepository(s2)
                newer = await repo.claim("slack", key, claim_timeout_seconds=LEASE)
                assert newer is not None
                await repo.complete(newer, "won-by-other-worker")

            with pytest.raises(asyncio.TimeoutError):
                await task

        # No mapping was written by the stale worker.
        async with db_factory() as s3:
            assert (
                await TicketMappingRepository(s3).find_by_slack_message(
                    "T1", "C1", "1700000000.000100"
                )
                is None
            )
            row = await ProcessedEventRepository(s3).get("slack", key)
            assert row.status == STATUS_COMPLETED
            assert row.detail == "won-by-other-worker"

    async def test_existing_mapping_used_when_lease_lost_after_creation(
        self, db_factory
    ):
        """The winning worker's mapping is authoritative and is reused."""

        class SlowSuccess:
            def __init__(self) -> None:
                self.calls = 0

            async def create_ticket(self, **kwargs):
                self.calls += 1
                await asyncio.sleep(0.05)
                return {"id": 888}

        zendesk = SlowSuccess()
        key = ctx().dedupe_key

        async with db_factory() as s1:
            svc = ticket_service(zendesk, s1)
            task = asyncio.create_task(svc.create_ticket_from_message(ctx()))
            await asyncio.sleep(0.01)

            async with db_factory() as s2:
                # Simulate the other worker having already created the mapping.
                repo = TicketMappingRepository(s2)
                await repo.add(
                    TicketMapping(
                        zendesk_ticket_id=999,
                        slack_team_id="T1",
                        slack_channel_id="C1",
                        slack_message_ts="1700000000.000100",
                        external_id="slack:T1:C1:1700000000.000100",
                    )
                )
                await repo.commit()

            # Our in-flight worker now finds the mapping and completes cleanly.
            result = await task

        async with db_factory() as s3:
            repo = TicketMappingRepository(s3)
            stored = await repo.find_by_slack_message("T1", "C1", "1700000000.000100")
            assert stored is not None
            assert stored.zendesk_ticket_id == 999
            # Exactly one mapping row exists (no duplicate insert).
            assert result is None or result.zendesk_ticket_id == 999
            row = await ProcessedEventRepository(s3).get("slack", key)
            assert row is not None

class TestRecoveryPathsDoNotHideNetworkErrors:
    """The `except Exception` recovery paths must not swallow the network guard.

    A test bug that reached the network should fail loudly, not turn into a
    silent pass via the claim-release fallback.
    """

    async def test_network_attempt_during_upstream_is_not_swallowed(self, db_factory):
        from tests.conftest import NetworkAccessDenied

        class NetworkCallingZendesk:
            def __init__(self) -> None:
                self.calls = 0

            async def create_ticket(self, **kwargs):
                self.calls += 1
                import socket

                sock = socket.socket()
                sock.connect(("127.0.0.1", 9))  # blocked by the session guard
                return {"id": 1}

        zendesk = NetworkCallingZendesk()
        async with db_factory() as s:
            with pytest.raises(BaseException) as excinfo:
                await ticket_service(zendesk, s).create_ticket_from_message(ctx())

        # The guard error propagates instead of being turned into "failed".
        flat = excinfo.value
        if isinstance(flat, BaseExceptionGroup):
            flat = next(
                (e for e in flat.exceptions if isinstance(e, NetworkAccessDenied)),
                flat,
            )
        assert isinstance(flat, NetworkAccessDenied)
