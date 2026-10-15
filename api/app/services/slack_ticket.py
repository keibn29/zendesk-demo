"""Business logic: Slack -> Zendesk ticket creation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from app.clients import ZendeskClient
from app.models import TicketMapping
from app.repositories import Claim, ProcessedEventRepository, TicketMappingRepository

logger = logging.getLogger(__name__)

SOURCE = "slack"


@dataclass(frozen=True)
class SlackMessageContext:
    """Everything needed to correlate a Slack message with a Zendesk ticket."""

    team_id: str
    channel_id: str
    message_ts: str
    user_id: str | None
    text: str
    event_id: str | None

    @property
    def idempotency_key(self) -> str:
        """Deterministic, secret-free key derived from message coordinates."""
        return f"{self.team_id}:{self.channel_id}:{self.message_ts}"

    @property
    def external_id(self) -> str:
        return f"slack:{self.idempotency_key}"

    @property
    def dedupe_key(self) -> str:
        """Ledger key.

        Uses message coordinates, NOT event_id: Slack assigns a new event_id on
        some redeliveries, so keying on it would let one message create two
        tickets. Coordinates are stable for the life of a message.
        """
        return self.idempotency_key


class TicketCreationService:
    """Creates one Zendesk ticket per Slack message, exactly once."""

    def __init__(
        self,
        *,
        zendesk: ZendeskClient,
        ticket_repo: TicketMappingRepository,
        event_repo: ProcessedEventRepository,
        channel_allowlist: list[str] | None = None,
        tags: list[str] | None = None,
        claim_timeout_seconds: int = 900,
        processing_deadline_seconds: float = 60.0,
    ) -> None:
        self._zendesk = zendesk
        self._tickets = ticket_repo
        self._events = event_repo
        self._allowlist = channel_allowlist or []
        self._tags = tags or ["slack"]
        self._claim_timeout = claim_timeout_seconds
        self._deadline = processing_deadline_seconds

    def channel_allowed(self, channel_id: str) -> bool:
        """Empty allowlist means all channels are accepted."""
        if not self._allowlist:
            return True
        return channel_id in self._allowlist

    async def create_ticket_from_message(
        self, ctx: SlackMessageContext
    ) -> TicketMapping | None:
        """Idempotently create a Zendesk ticket for a Slack message.

        Returns the persisted mapping, or None when the message was skipped as a
        duplicate. Raises the upstream error if Zendesk fails, after marking
        the claim retryable.
        """
        if not self.channel_allowed(ctx.channel_id):
            logger.info(
                "Ignoring Slack message from channel not in allowlist",
                extra={"channel": ctx.channel_id},
            )
            return None

        existing = await self._existing_or_none(ctx)
        if existing is not None:
            # A mapping already exists: the ticket was created. The claim may
            # still be non-terminal if a previous attempt crashed after the
            # Zendesk call, so repair it rather than leaving it retryable.
            await self._repair_completed_claim(ctx)
            return None

        claim = await self._events.claim(
            SOURCE, ctx.dedupe_key, claim_timeout_seconds=self._claim_timeout
        )
        if claim is None:
            logger.info("Slack message already being processed; skipping duplicate")
            return None

        # Recheck after claiming: a concurrent worker may have completed the
        # mapping between our first check and acquiring the lease. Calling
        # Zendesk again here would create a duplicate ticket.
        existing = await self._existing_or_none(ctx)
        if existing is not None:
            await self._events.complete(claim, str(existing.zendesk_ticket_id))
            return None

        try:
            # Total wall-clock bound on the upstream call. httpx's per-phase
            # timeout alone would allow a several-times-longer request, which
            # could outlive our own lease.
            async with asyncio.timeout(self._deadline):
                ticket = await self._zendesk.create_ticket(
                    subject=self._subject(ctx),
                    comment=ctx.text,
                    external_id=ctx.external_id,
                    idempotency_key=ctx.idempotency_key,
                    tags=self._tags,
                )
        except asyncio.TimeoutError:
            # A known, retryable failure: the claim is released back so a later
            # delivery can retry rather than waiting for the lease to expire.
            await self._discard_claim(claim, "processing deadline exceeded")
            raise
        except Exception as exc:
            await self._fail_claim(claim, str(exc)[:200])
            logger.warning(
                "Zendesk ticket creation failed; claim marked retryable",
                extra={"slack_ts": ctx.message_ts},
            )
            raise

        return await self._persist_mapping(ctx, claim, int(ticket["id"]))

    async def _existing_or_none(self, ctx: SlackMessageContext) -> TicketMapping | None:
        return await self._tickets.find_by_slack_message(
            ctx.team_id, ctx.channel_id, ctx.message_ts
        )

    async def _repair_completed_claim(self, ctx: SlackMessageContext) -> None:
        """Best-effort: leave a 'processing' claim terminal once a mapping exists."""
        try:
            claim = await self._events.claim(
                SOURCE, ctx.dedupe_key, claim_timeout_seconds=0
            )
            if claim is not None:
                existing = await self._existing_or_none(ctx)
                detail = str(existing.zendesk_ticket_id) if existing else None
                await self._events.complete(claim, detail)
        except Exception:
            # Never let repair bookkeeping mask the actual outcome; the mapping
            # already exists so the ticket will never be created twice.
            logger.warning("Could not repair Slack claim state", exc_info=True)

    async def _persist_mapping(
        self, ctx: SlackMessageContext, claim: Claim, ticket_id: int
    ) -> TicketMapping | None:
        """Insert the mapping and complete the claim in one transaction."""
        mapping = TicketMapping(
            zendesk_ticket_id=ticket_id,
            slack_team_id=ctx.team_id,
            slack_channel_id=ctx.channel_id,
            slack_message_ts=ctx.message_ts,
            external_id=ctx.external_id,
        )
        try:
            await self._tickets.add(mapping)
            # Commit the mapping and the claim completion together: if the
            # commit fails, neither lands. The completion is owner-checked, so
            # if the lease was lost the row is NOT marked completed.
            owned = await self._events.complete(claim, str(ticket_id), commit=False)
            if not owned:
                # Lease lost while we were upstream. Discard the staged mapping
                # rather than committing a row we no longer own; an authoritative
                # mapping may already exist from the winning worker.
                await self._tickets.rollback()
                existing = await self._existing_or_none(ctx)
                if existing is not None:
                    logger.info(
                        "Lease lost but mapping already exists; keeping it",
                        extra={"zendesk_ticket_id": existing.zendesk_ticket_id},
                    )
                    return None
                logger.warning(
                    "Lease lost and no mapping exists; discarding this attempt",
                    extra={"slack_ts": ctx.message_ts},
                )
                return None
            await self._tickets.commit()
        except IntegrityError:
            # Someone else committed this message's mapping first. Discard the
            # failed transaction before reading or writing anything else.
            await self._tickets.rollback()
            existing = await self._existing_or_none(ctx)
            if existing is not None:
                logger.info("Mapping already present; keeping existing ticket")
                return None
            # A genuine integrity problem with no existing row: retryable.
            await self._fail_claim(claim, "mapping integrity error")
            raise
        except Exception as exc:
            await self._tickets.rollback()
            await self._fail_claim(claim, f"mapping: {exc}"[:200])
            raise

        logger.info(
            "Created Zendesk ticket from Slack message",
            extra={"zendesk_ticket_id": ticket_id, "slack_ts": ctx.message_ts},
        )
        return mapping

    async def _fail_claim(self, claim: Claim, detail: str) -> None:
        """Mark a claim failed without masking an in-flight error."""
        try:
            await self._events.fail(claim, detail)
        except Exception:
            # Recording the failure itself failed. Ownership-conditioned release
            # is a last resort; it can never touch a newer or completed claim.
            logger.warning("Could not mark claim failed; releasing", exc_info=True)
            await self._discard_claim(claim, "failure recording error")

    async def _discard_claim(self, claim: Claim, reason: str) -> None:
        """Release our own claim so the key can be retried. Never touches others."""
        try:
            released = await self._events.release(claim)
            logger.info(
                "Claim discarded",
                extra={"reason": reason, "released": released},
            )
        except Exception:
            logger.error("Could not release claim", exc_info=True)

    @staticmethod
    def _subject(ctx: SlackMessageContext) -> str:
        first_line = ctx.text.strip().splitlines()[0]
        if len(first_line) > 100:
            first_line = first_line[:97] + "..."
        return f"[Slack] {first_line}"