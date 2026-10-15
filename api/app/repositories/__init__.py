"""Repositories: data access only, no business logic.

Transaction policy: methods that write do NOT commit unless documented.
Callers own the transaction boundary so that a mapping insert and the claim
completion that depends on it can commit together (or not at all).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProcessedEvent, TicketMapping

# ProcessedEvent.status values.
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

DEFAULT_CLAIM_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class Claim:
    """An acquired lease.

    ``token`` identifies this specific claim generation. Pass it back to
    complete()/fail() so a superseded worker cannot clobber newer state.
    """

    source: str
    dedupe_key: str
    token: str


class TicketMappingRepository:
    """Persistence for the Slack message <-> Zendesk ticket mapping."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_slack_message(
        self, team_id: str, channel_id: str, message_ts: str
    ) -> TicketMapping | None:
        """Look up a ticket created from a specific Slack message."""
        stmt = select(TicketMapping).where(
            TicketMapping.slack_team_id == team_id,
            TicketMapping.slack_channel_id == channel_id,
            TicketMapping.slack_message_ts == message_ts,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def get_by_ticket_id(self, ticket_id: int) -> TicketMapping | None:
        """Look up a ticket mapping by Zendesk ticket id."""
        stmt = select(TicketMapping).where(TicketMapping.zendesk_ticket_id == ticket_id)
        return (await self._session.execute(stmt)).scalars().first()

    async def add(self, mapping: TicketMapping) -> TicketMapping:
        """Stage a mapping and flush. Does not commit; callers own the boundary."""
        self._session.add(mapping)
        await self._session.flush()
        return mapping

    async def rollback(self) -> None:
        """Discard the current transaction (used before recovery writes)."""
        await self._session.rollback()

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()


class ProcessedEventRepository:
    """Deduplication ledger with an owner-checked claim state machine.

    A claim is granted when the key is unseen, when a previous attempt is
    known-failed, or when a 'processing' claim's lease has expired (the worker
    stalled or crashed).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, source: str, dedupe_key: str) -> ProcessedEvent | None:
        stmt = select(ProcessedEvent).where(
            ProcessedEvent.source == source,
            ProcessedEvent.dedupe_key == dedupe_key,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def claim(
        self,
        source: str,
        dedupe_key: str,
        *,
        claim_timeout_seconds: int = DEFAULT_CLAIM_TIMEOUT_SECONDS,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Claim | None:
        """Atomically acquire a lease on a key.

        Returns None when another attempt holds a live lease or the work already
        completed. Otherwise returns a Claim carrying a fresh owner token.
        """
        moment = now or datetime.now(timezone.utc)
        stale_before = moment - timedelta(seconds=claim_timeout_seconds)
        token = secrets.token_hex(16)

        stmt = (
            sqlite_insert(ProcessedEvent)
            .values(
                source=source,
                dedupe_key=dedupe_key,
                status=STATUS_PROCESSING,
                attempts=1,
                owner_token=token,
                updated_at=moment,
            )
            .on_conflict_do_update(
                index_elements=["source", "dedupe_key"],
                set_={
                    "status": STATUS_PROCESSING,
                    "attempts": ProcessedEvent.attempts + 1,
                    "owner_token": token,
                    "updated_at": moment,
                },
                where=(
                    (ProcessedEvent.status == STATUS_FAILED)
                    | (
                        (ProcessedEvent.status == STATUS_PROCESSING)
                        & (ProcessedEvent.updated_at <= stale_before)
                    )
                ),
            )
        )
        result = await self._session.execute(stmt)
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        if result.rowcount != 1:
            return None
        return Claim(source=source, dedupe_key=dedupe_key, token=token)

    async def seed_processing(
        self, source: str, dedupe_key: str, *, claimed_at: datetime
    ) -> None:
        """Force a row into 'processing' with a chosen timestamp.

        Used to simulate a worker that stalled mid-flight.
        """
        await self._session.execute(
            sqlite_insert(ProcessedEvent).values(
                source=source,
                dedupe_key=dedupe_key,
                status=STATUS_PROCESSING,
                attempts=1,
                owner_token="seed",
                updated_at=claimed_at,
            )
        )
        await self._session.commit()

    async def complete(
        self, claim: Claim, detail: str | None = None, *, commit: bool = True
    ) -> bool:
        """Mark a claim completed, but only if we still own it.

        Returns False when a newer generation holds the claim, in which case the
        caller's late result is discarded rather than overwriting newer state.
        """
        return await self._finish(claim, STATUS_COMPLETED, detail, commit=commit)

    async def fail(
        self, claim: Claim, detail: str | None = None, *, commit: bool = True
    ) -> bool:
        """Mark a claim failed (retryable), but only if we still own it."""
        return await self._finish(claim, STATUS_FAILED, detail, commit=commit)

    async def release(self, claim: Claim) -> bool:
        """Drop a claim entirely so the key is treated as never seen.

        Ownership-conditioned: a worker that has already been superseded must
        not delete the newer claim (nor a completed one), or the next delivery
        would redo work that already succeeded.

        Returns True if this call removed the row.
        """
        stmt = delete(ProcessedEvent).where(
            ProcessedEvent.source == claim.source,
            ProcessedEvent.dedupe_key == claim.dedupe_key,
            ProcessedEvent.owner_token == claim.token,
            ProcessedEvent.status == STATUS_PROCESSING,
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount == 1

    async def _finish(
        self,
        claim: Claim,
        status: str,
        detail: str | None,
        *,
        commit: bool,
    ) -> bool:
        """Owner-checked state transition from 'processing' to a final state."""
        stmt = (
            update(ProcessedEvent)
            .where(
                ProcessedEvent.source == claim.source,
                ProcessedEvent.dedupe_key == claim.dedupe_key,
                ProcessedEvent.owner_token == claim.token,
                ProcessedEvent.status == STATUS_PROCESSING,
            )
            .values(status=status, detail=detail, updated_at=datetime.now(timezone.utc))
        )
        result = await self._session.execute(stmt)
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return result.rowcount == 1