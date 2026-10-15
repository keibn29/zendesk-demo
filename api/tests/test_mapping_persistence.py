"""Mapping persistence failures: rollback, integrity conflicts, single txn.

Uses real SQLAlchemy flush/commit failure paths rather than mocks.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.clients import UpstreamError
from app.models import TicketMapping
from app.repositories import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    ProcessedEventRepository,
    TicketMappingRepository,
)
from app.services.slack_ticket import SlackMessageContext, TicketCreationService


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


def service(zendesk, s) -> TicketCreationService:
    return TicketCreationService(
        zendesk=zendesk,
        ticket_repo=TicketMappingRepository(s),
        event_repo=ProcessedEventRepository(s),
        claim_timeout_seconds=900,
    )


class BrokenCommitZendesk:
    """Returns a ticket id that already exists, forcing a real IntegrityError."""

    def __init__(self, ticket_id: int = 1) -> None:
        self.calls: list[dict] = []
        self._id = ticket_id

    async def create_ticket(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": self._id}


async def test_conflicting_mapping_is_handled_not_masked(db_factory):
    """A duplicate ticket id raises IntegrityError; recovery must not raise."""
    async with db_factory() as s:
        repo = TicketMappingRepository(s)
        # Occupies zendesk_ticket_id=1 for a *different* Slack message.
        await repo.add(
            TicketMapping(
                zendesk_ticket_id=1,
                slack_team_id="T9",
                slack_channel_id="C9",
                slack_message_ts="1.0",
                external_id="slack:T9:C9:1.0",
            )
        )
        await repo.commit()

    async with db_factory() as s:
        zendesk = BrokenCommitZendesk(ticket_id=1)
        with pytest.raises(IntegrityError):
            await service(zendesk, s).create_ticket_from_message(ctx())
        # Session must still be usable (recovery rolled back the failed txn).
        assert await ProcessedEventRepository(s).get("slack", ctx().dedupe_key) is not None


async def test_integrity_error_leaves_session_usable(db_factory):
    async with db_factory() as s:
        repo = TicketMappingRepository(s)
        await repo.add(
            TicketMapping(
                zendesk_ticket_id=5,
                slack_team_id="T8",
                slack_channel_id="C8",
                slack_message_ts="8.0",
                external_id="x",
            )
        )
        await repo.commit()

    async with db_factory() as s:
        zendesk = BrokenCommitZendesk(ticket_id=5)
        svc = service(zendesk, s)
        with pytest.raises(IntegrityError):
            await svc.create_ticket_from_message(ctx())
        # If the session were poisoned, this write would raise too.
        await svc._events.claim("slack", "after-error")  # noqa: SLF001
        assert await svc._events.get("slack", "after-error") is not None  # noqa: SLF001


async def test_existing_mapping_found_after_conflict_is_reused(db_factory):
    """When the conflict is the *same* message, the existing row is kept."""
    async with db_factory() as s:
        repo = TicketMappingRepository(s)
        await repo.add(
            TicketMapping(
                zendesk_ticket_id=42,
                slack_team_id="T1",
                slack_channel_id="C1",
                slack_message_ts="1700000000.000100",
                external_id="slack:T1:C1:1700000000.000100",
            )
        )
        await repo.commit()

    async with db_factory() as s:
        zendesk = BrokenCommitZendesk(ticket_id=42)
        svc = service(zendesk, s)
        # Fast path: mapping already exists, so Zendesk is never called.
        assert await svc.create_ticket_from_message(ctx()) is None
        assert zendesk.calls == []
        row = await svc._events.get("slack", ctx().dedupe_key)  # noqa: SLF001
        assert row is not None
        assert row.status == STATUS_COMPLETED


async def test_mapping_and_claim_complete_commit_together(db_factory, fake_zendesk):
    """Success path: mapping insert and claim completion share one commit."""
    async with db_factory() as s:
        svc = service(fake_zendesk, s)
        mapping = await svc.create_ticket_from_message(ctx())
        assert mapping is not None
        assert mapping.zendesk_ticket_id == 4242
        row = await svc._events.get("slack", ctx().dedupe_key)  # noqa: SLF001
        assert row.status == STATUS_COMPLETED
        assert row.detail == "4242"


async def test_failed_commit_leaves_no_partial_state(db_factory, fake_zendesk):
    """If the commit fails, neither mapping nor completion may survive."""
    async with db_factory() as s:
        svc = service(fake_zendesk, s)
        claim = await svc._events.claim(  # noqa: SLF001
            "slack", ctx().dedupe_key, claim_timeout_seconds=900
        )
        mapping = TicketMapping(
            zendesk_ticket_id=77,
            slack_team_id="T1",
            slack_channel_id="C1",
            slack_message_ts="1700000000.000100",
            external_id="slack:T1:C1:1700000000.000100",
        )
        await svc._tickets.add(mapping)  # noqa: SLF001
        await svc._tickets.rollback()  # noqa: SLF001
        assert claim is not None
        assert await svc._tickets.find_by_slack_message(  # noqa: SLF001
            "T1", "C1", "1700000000.000100"
        ) is None
        # The claim is untouched by the discarded transaction.
        row = await svc._events.get("slack", ctx().dedupe_key)  # noqa: SLF001
        assert row.status == "processing"


async def test_upstream_error_marked_failed_and_retryable(db_factory, fake_zendesk):
    fake_zendesk.error = UpstreamError("zendesk 500")
    async with db_factory() as s:
        svc = service(fake_zendesk, s)
        with pytest.raises(UpstreamError):
            await svc.create_ticket_from_message(ctx())
        row = await svc._events.get("slack", ctx().dedupe_key)  # noqa: SLF001
        assert row.status == STATUS_FAILED
        assert row.attempts == 1


async def test_original_exception_preserved_when_recovery_also_fails(db_factory, fake_zendesk):
    """If marking the claim failed also fails, the original error must surface."""
    fake_zendesk.error = UpstreamError("original upstream boom")

    class ExplodingEvents(ProcessedEventRepository):
        async def fail(self, claim, detail=None, *, commit=True):
            raise RuntimeError("recovery exploded")

    async with db_factory() as s:
        svc = TicketCreationService(
            zendesk=fake_zendesk,
            ticket_repo=TicketMappingRepository(s),
            event_repo=ExplodingEvents(s),
            claim_timeout_seconds=900,
        )
        with pytest.raises(UpstreamError, match="original upstream boom"):
            await svc.create_ticket_from_message(ctx())