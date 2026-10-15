"""Business logic: Zendesk ticket status -> Slack reaction."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.clients import SlackClient
from app.repositories import Claim, ProcessedEventRepository, TicketMappingRepository

logger = logging.getLogger(__name__)

SOURCE = "zendesk"


@dataclass(frozen=True)
class ZendeskTicketEvent:
    """Normalized view over both supported Zendesk webhook shapes."""

    ticket_id: int
    status: str
    invocation_id: str


class ReactionService:
    """Adds a Slack reaction reflecting the current Zendesk ticket status."""

    def __init__(
        self,
        *,
        slack: SlackClient,
        ticket_repo: TicketMappingRepository,
        event_repo: ProcessedEventRepository,
        reactions: dict[str, str],
        claim_timeout_seconds: int = 900,
        processing_deadline_seconds: float = 60.0,
    ) -> None:
        self._slack = slack
        self._tickets = ticket_repo
        self._events = event_repo
        self._reactions = reactions
        self._claim_timeout = claim_timeout_seconds
        self._deadline = processing_deadline_seconds

    def reaction_for(self, status: str) -> str:
        return self._reactions.get(status.strip().lower(), "")

    async def apply_reaction(self, event: ZendeskTicketEvent) -> str:
        """Idempotently add the reaction for the ticket's Slack message.

        Returns one of: "added", "duplicate", "unmapped", "no_reaction".
        """
        claim = await self._events.claim(
            SOURCE, event.invocation_id, claim_timeout_seconds=self._claim_timeout
        )
        if claim is None:
            logger.info("Zendesk invocation already processed; skipping")
            return "duplicate"

        reaction = self.reaction_for(event.status)
        if not reaction:
            # No reaction configured for this status: nothing to do, terminal.
            await self._events.complete(claim, event.status)
            return "no_reaction"

        mapping = await self._tickets.get_by_ticket_id(event.ticket_id)
        if mapping is None:
            # The mapping may not be committed yet if the Slack ACK arrived and
            # ticket creation is still in flight. Treat this as retryable rather
            # than terminal so a later replay can still add the reaction.
            await self._events.fail(claim, "unmapped")
            logger.info(
                "No Slack mapping for Zendesk ticket yet; marked retryable",
                extra={"zendesk_ticket_id": event.ticket_id},
            )
            return "unmapped"

        try:
            # Total wall-clock bound, independent of httpx's per-phase timeout.
            async with asyncio.timeout(self._deadline):
                await self._slack.add_reaction(
                    channel=mapping.slack_channel_id,
                    name=reaction,
                    timestamp=mapping.slack_message_ts,
                )
        except asyncio.TimeoutError:
            await self._discard_claim(claim, "processing deadline exceeded")
            raise
        except Exception as exc:
            # Retryable: do not let one Slack outage permanently drop the event.
            await self._fail_claim(claim, str(exc)[:200])
            raise

        await self._events.complete(claim, reaction)
        return "added"

    async def _fail_claim(self, claim: Claim, detail: str) -> None:
        """Mark a claim failed without masking an in-flight error."""
        try:
            await self._events.fail(claim, detail)
        except Exception:
            logger.warning("Could not mark claim failed; releasing", exc_info=True)
            await self._discard_claim(claim, "failure recording error")

    async def _discard_claim(self, claim: Claim, reason: str) -> None:
        """Release our own claim so the key can be retried. Never touches others."""
        try:
            released = await self._events.release(claim)
            logger.info("Claim discarded", extra={"reason": reason, "released": released})
        except Exception:
            logger.error("Could not release claim", exc_info=True)