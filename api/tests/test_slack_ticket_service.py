"""TicketCreationService: dedup by message coordinates, retry, allowlist."""

from __future__ import annotations

import pytest

from app.clients import UpstreamError
from app.models import TicketMapping
from app.repositories import STATUS_COMPLETED, STATUS_FAILED
from app.services.slack_ticket import SlackMessageContext, TicketCreationService


def ctx(**overrides) -> SlackMessageContext:
    base = dict(
        team_id="T1",
        channel_id="C1",
        message_ts="1700000000.000100",
        user_id="U1",
        text="my laptop is broken",
        event_id="Ev1",
    )
    base.update(overrides)
    return SlackMessageContext(**base)  # type: ignore[arg-type]


def build(zendesk, ticket_repo, event_repo, allowlist=None) -> TicketCreationService:
    return TicketCreationService(
        zendesk=zendesk,
        ticket_repo=ticket_repo,
        event_repo=event_repo,
        channel_allowlist=allowlist or [],
    )


class TestDerivedKeys:
    def test_keys_are_deterministic_and_secret_free(self):
        c = ctx()
        assert c.idempotency_key == "T1:C1:1700000000.000100"
        assert c.external_id == "slack:T1:C1:1700000000.000100"
        assert c.dedupe_key == c.idempotency_key

    def test_dedupe_key_does_not_depend_on_event_id(self):
        a = ctx(event_id="Ev1")
        b = ctx(event_id="Ev2")
        assert a.dedupe_key == b.dedupe_key


class TestCreateTicket:
    async def test_creates_and_persists_mapping(self, ticket_repo, event_repo, fake_zendesk):
        svc = build(fake_zendesk, ticket_repo, event_repo)

        mapping = await svc.create_ticket_from_message(ctx())

        assert mapping is not None
        assert mapping.zendesk_ticket_id == 4242
        assert len(fake_zendesk.calls) == 1
        stored = await ticket_repo.find_by_slack_message("T1", "C1", "1700000000.000100")
        assert stored is not None
        assert stored.external_id == "slack:T1:C1:1700000000.000100"
        assert fake_zendesk.calls[0]["idempotency_key"] == "T1:C1:1700000000.000100"
        assert fake_zendesk.calls[0]["comment"] == "my laptop is broken"

    async def test_marked_completed_after_success(self, ticket_repo, event_repo, fake_zendesk):
        svc = build(fake_zendesk, ticket_repo, event_repo)
        await svc.create_ticket_from_message(ctx())

        record = await event_repo.get("slack", "T1:C1:1700000000.000100")
        assert record is not None
        assert record.status == STATUS_COMPLETED
        assert record.attempts == 1

    async def test_identical_retry_does_not_duplicate(self, ticket_repo, event_repo, fake_zendesk):
        svc = build(fake_zendesk, ticket_repo, event_repo)

        first = await svc.create_ticket_from_message(ctx())
        second = await svc.create_ticket_from_message(ctx())

        assert first is not None
        assert second is None
        assert len(fake_zendesk.calls) == 1

    async def test_different_event_id_same_message_does_not_duplicate(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        """Regression: dedup used event_id, so a new id created a second ticket."""
        svc = build(fake_zendesk, ticket_repo, event_repo)

        await svc.create_ticket_from_message(ctx(event_id="Ev1"))
        await svc.create_ticket_from_message(ctx(event_id="Ev2"))
        await svc.create_ticket_from_message(ctx(event_id="Ev3"))

        assert len(fake_zendesk.calls) == 1

    async def test_different_message_creates_second_ticket(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        svc = build(fake_zendesk, ticket_repo, event_repo)

        await svc.create_ticket_from_message(ctx())
        await svc.create_ticket_from_message(ctx(message_ts="1700000000.000200"))

        assert len(fake_zendesk.calls) == 2

    async def test_db_constraint_blocks_duplicate_mapping(self, session, event_repo):
        """Belt-and-braces: the unique constraint enforces one ticket per message."""
        for _ in range(2):
            session.add(
                TicketMapping(
                    zendesk_ticket_id=1,
                    slack_team_id="T1",
                    slack_channel_id="C1",
                    slack_message_ts="1700000000.1",
                    external_id="slack:T1:C1:1700000000.1",
                )
            )
        with pytest.raises(Exception):
            await session.commit()
        await session.rollback()

    async def test_mapping_not_persisted_when_zendesk_fails(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        fake_zendesk.error = UpstreamError("zendesk down")
        svc = build(fake_zendesk, ticket_repo, event_repo)

        with pytest.raises(UpstreamError):
            await svc.create_ticket_from_message(ctx())

        assert (
            await ticket_repo.find_by_slack_message("T1", "C1", "1700000000.000100")
            is None
        )

    async def test_failure_leaves_claim_retryable(self, ticket_repo, event_repo, fake_zendesk):
        fake_zendesk.error = UpstreamError("zendesk down")
        svc = build(fake_zendesk, ticket_repo, event_repo)

        with pytest.raises(UpstreamError):
            await svc.create_ticket_from_message(ctx())

        record = await event_repo.get("slack", "T1:C1:1700000000.000100")
        assert record is not None
        assert record.status == STATUS_FAILED

    async def test_retry_after_failure_succeeds(self, ticket_repo, event_repo, fake_zendesk):
        fake_zendesk.error = UpstreamError("zendesk down")
        svc = build(fake_zendesk, ticket_repo, event_repo)

        with pytest.raises(UpstreamError):
            await svc.create_ticket_from_message(ctx())

        fake_zendesk.error = None
        mapping = await svc.create_ticket_from_message(ctx())

        assert mapping is not None
        assert len(fake_zendesk.calls) == 2
        record = await event_repo.get("slack", "T1:C1:1700000000.000100")
        assert record is not None
        assert record.status == STATUS_COMPLETED
        assert record.attempts == 2

    async def test_retry_with_new_event_id_after_failure(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        fake_zendesk.error = UpstreamError("zendesk down")
        svc = build(fake_zendesk, ticket_repo, event_repo)

        with pytest.raises(UpstreamError):
            await svc.create_ticket_from_message(ctx(event_id="Ev1"))

        fake_zendesk.error = None
        mapping = await svc.create_ticket_from_message(ctx(event_id="Ev2"))

        assert mapping is not None

    async def test_channel_allowlist_blocks_other_channels(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        svc = build(fake_zendesk, ticket_repo, event_repo, allowlist=["C_OTHER"])

        assert await svc.create_ticket_from_message(ctx()) is None
        assert fake_zendesk.calls == []

    async def test_channel_allowlist_allows_listed_channel(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        svc = build(fake_zendesk, ticket_repo, event_repo, allowlist=["C1"])

        assert await svc.create_ticket_from_message(ctx()) is not None

    async def test_empty_allowlist_allows_any_channel(
        self, ticket_repo, event_repo, fake_zendesk
    ):
        svc = build(fake_zendesk, ticket_repo, event_repo, allowlist=[])

        assert await svc.create_ticket_from_message(ctx(channel_id="C_ANY")) is not None

    async def test_subject_truncated(self, ticket_repo, event_repo, fake_zendesk):
        svc = build(fake_zendesk, ticket_repo, event_repo)

        await svc.create_ticket_from_message(ctx(text="x" * 500))
        assert len(fake_zendesk.calls[0]["subject"]) <= 108

    async def test_subject_uses_first_line(self, ticket_repo, event_repo, fake_zendesk):
        svc = build(fake_zendesk, ticket_repo, event_repo)

        await svc.create_ticket_from_message(ctx(text="first\nsecond"))
        assert fake_zendesk.calls[0]["subject"] == "[Slack] first"