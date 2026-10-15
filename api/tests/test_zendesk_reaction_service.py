"""ReactionService: mapping lookup, idempotency, retryability."""

from __future__ import annotations

import pytest

from app.models import TicketMapping
from app.repositories import STATUS_COMPLETED, STATUS_FAILED
from app.services.zendesk_reaction import ReactionService, ZendeskTicketEvent

REACTIONS = {
    "new": "eyes",
    "open": "eyes",
    "pending": "hourglass_flowing_sand",
    "hold": "hourglass_flowing_sand",
    "solved": "white_check_mark",
    "closed": "white_check_mark",
}


async def seed(ticket_repo, ticket_id: int = 99) -> TicketMapping:
    mapping = TicketMapping(
        zendesk_ticket_id=ticket_id,
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_message_ts="1700000000.000100",
        external_id="slack:T1:C1:1700000000.000100",
    )
    await ticket_repo.add(mapping)
    await ticket_repo.commit()
    return mapping


def build(slack, ticket_repo, event_repo) -> ReactionService:
    return ReactionService(
        slack=slack, ticket_repo=ticket_repo, event_repo=event_repo, reactions=REACTIONS
    )


class TestReactionFor:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("new", "eyes"),
            ("open", "eyes"),
            ("pending", "hourglass_flowing_sand"),
            ("hold", "hourglass_flowing_sand"),
            ("solved", "white_check_mark"),
            ("closed", "white_check_mark"),
            ("SOLVED", "white_check_mark"),
            (" SOLVED ", "white_check_mark"),
            ("Open", "eyes"),
            ("New", "eyes"),
            ("Pending", "hourglass_flowing_sand"),
            ("On-hold", "hourglass_flowing_sand"),
            ("on hold", "hourglass_flowing_sand"),
            ("OnHold", "hourglass_flowing_sand"),
            ("on_hold", "hourglass_flowing_sand"),
            ("Closed", "white_check_mark"),
        ],
    )
    def test_status_mapping(self, ticket_repo, event_repo, status, expected):
        svc = build(object(), ticket_repo, event_repo)
        assert svc.reaction_for(status) == expected

    def test_unknown_status_maps_to_empty(self, ticket_repo, event_repo):
        svc = build(object(), ticket_repo, event_repo)
        assert svc.reaction_for("weird") == ""
        assert svc.reaction_for("Đang xử lý") == ""


class TestApplyReaction:
    async def test_adds_reaction_to_original_message(
        self, ticket_repo, event_repo, fake_slack
    ):
        await seed(ticket_repo)
        svc = build(fake_slack, ticket_repo, event_repo)

        result = await svc.apply_reaction(
            ZendeskTicketEvent(ticket_id=99, status="solved", invocation_id="inv-1")
        )

        assert result == "added"
        assert fake_slack.calls == [
            {
                "channel": "C1",
                "name": "white_check_mark",
                "timestamp": "1700000000.000100",
            }
        ]

    async def test_marked_completed_after_success(
        self, ticket_repo, event_repo, fake_slack
    ):
        await seed(ticket_repo)
        svc = build(fake_slack, ticket_repo, event_repo)

        await svc.apply_reaction(
            ZendeskTicketEvent(ticket_id=99, status="solved", invocation_id="inv-1")
        )

        record = await event_repo.get("zendesk", "inv-1")
        assert record is not None
        assert record.status == STATUS_COMPLETED

    async def test_duplicate_invocation_id_is_idempotent(
        self, ticket_repo, event_repo, fake_slack
    ):
        await seed(ticket_repo)
        svc = build(fake_slack, ticket_repo, event_repo)

        event = ZendeskTicketEvent(ticket_id=99, status="solved", invocation_id="inv-1")
        assert await svc.apply_reaction(event) == "added"
        assert await svc.apply_reaction(event) == "duplicate"
        assert len(fake_slack.calls) == 1

    async def test_missing_mapping_is_safe(self, ticket_repo, event_repo, fake_slack):
        svc = build(fake_slack, ticket_repo, event_repo)

        result = await svc.apply_reaction(
            ZendeskTicketEvent(ticket_id=404, status="solved", invocation_id="inv-2")
        )

        assert result == "unmapped"
        assert fake_slack.calls == []

    async def test_missing_mapping_leaves_invocation_retryable(
        self, ticket_repo, event_repo, fake_slack
    ):
        """Regression: unmapped was terminal, so a later replay was suppressed.

        The mapping may not exist yet when Slack delivery races ahead of the
        background ticket-creation task.
        """
        svc = build(fake_slack, ticket_repo, event_repo)
        event = ZendeskTicketEvent(ticket_id=404, status="solved", invocation_id="inv-2")

        assert await svc.apply_reaction(event) == "unmapped"

        record = await event_repo.get("zendesk", "inv-2")
        assert record is not None
        assert record.status == STATUS_FAILED

        # Once the mapping exists, a replay applies the reaction.
        await seed(ticket_repo, ticket_id=404)
        assert await svc.apply_reaction(event) == "added"
        assert len(fake_slack.calls) == 1

    async def test_unknown_status_no_reaction_sent(
        self, ticket_repo, event_repo, fake_slack
    ):
        await seed(ticket_repo)
        svc = build(fake_slack, ticket_repo, event_repo)

        result = await svc.apply_reaction(
            ZendeskTicketEvent(ticket_id=99, status="unknown", invocation_id="inv-3")
        )

        assert result == "no_reaction"
        assert fake_slack.calls == []

    async def test_slack_failure_leaves_claim_retryable(
        self, ticket_repo, event_repo, fake_slack
    ):
        """Regression: a Slack outage must not permanently drop the event."""
        await seed(ticket_repo)
        fake_slack.error = RuntimeError("slack down")
        svc = build(fake_slack, ticket_repo, event_repo)
        event = ZendeskTicketEvent(ticket_id=99, status="solved", invocation_id="inv-4")

        with pytest.raises(RuntimeError):
            await svc.apply_reaction(event)

        record = await event_repo.get("zendesk", "inv-4")
        assert record is not None
        assert record.status == STATUS_FAILED

        fake_slack.error = None
        assert await svc.apply_reaction(event) == "added"
        # Two attempts total: the failed one and the successful retry.
        assert len(fake_slack.calls) == 2

    async def test_fresh_processing_claim_blocks_retry(self, ticket_repo, event_repo):
        """A live in-flight claim must not be stolen."""
        assert await event_repo.claim("zendesk", "live", claim_timeout_seconds=900)
        assert await event_repo.claim("zendesk", "live", claim_timeout_seconds=900) is None

    async def test_completed_claim_blocks_retry(self, ticket_repo, event_repo):
        claim = await event_repo.claim("zendesk", "done")
        await event_repo.complete(claim)
        assert await event_repo.claim("zendesk", "done", claim_timeout_seconds=0) is None

    async def test_failed_claim_is_reclaimable(self, ticket_repo, event_repo):
        claim = await event_repo.claim("zendesk", "flaky")
        await event_repo.fail(claim, "boom")
        assert await event_repo.claim("zendesk", "flaky", claim_timeout_seconds=900) is not None

    @pytest.mark.parametrize(
        "stored_age,claim_timeout,expected",
        [
            (10, 900, False),     # fresh: blocks
            (500, 900, False),    # not yet stale: blocks
            (3600, 60, True),     # stale: recovered
            (900, 900, True),     # borderline: recovered
        ],
    )
    async def test_stale_processing_claim_recovered(
        self, event_repo, stored_age, claim_timeout, expected
    ):
        """A crash mid-flight leaves 'processing'; after the timeout it retries."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        await event_repo.seed_processing(
            "zendesk", "crashed", claimed_at=now - timedelta(seconds=stored_age)
        )

        claimed = await event_repo.claim(
            "zendesk", "crashed", claim_timeout_seconds=claim_timeout
        )
        assert (claimed is not None) is expected

    async def test_release_allows_fresh_claim(self, ticket_repo, event_repo):
        claim = await event_repo.claim("zendesk", "rel")
        assert await event_repo.release(claim) is True
        assert await event_repo.claim("zendesk", "rel", claim_timeout_seconds=900)