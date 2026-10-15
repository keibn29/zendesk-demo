"""Slack webhook endpoint: ACK immediately, create the ticket in the background."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.api.deps import verify_slack_request
from app.api.payloads import parse_slack_payload
from app.clients import ZendeskClient
from app.core.config import Settings
from app.db.base import SessionFactory
from app.dependencies.providers import (
    build_ticket_creation_service,
    get_settings_dep,
    get_zendesk_client,
)
from app.services.slack_ticket import SlackMessageContext

logger = logging.getLogger(__name__)

router = APIRouter(tags=["slack"])


async def _process_in_background(
    ctx: SlackMessageContext,
    zendesk: ZendeskClient,
    settings: Settings,
) -> None:
    """Runs after the ACK, in its own DB session (the request one is closed)."""
    async with SessionFactory() as session:
        service = build_ticket_creation_service(
            zendesk=zendesk, session=session, settings=settings
        )
        try:
            await service.create_ticket_from_message(ctx)
        except Exception:
            # Never let a background failure crash the worker. The service marks
            # the claim retryable so a replay can pick it up.
            logger.exception(
                "Background ticket creation failed", extra={"slack_ts": ctx.message_ts}
            )


@router.post("/webhooks/slack/events", status_code=200)
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings_dep),
    zendesk: ZendeskClient = Depends(get_zendesk_client),
) -> Response:
    """Receive Slack Events API callbacks."""
    await verify_slack_request(request, settings)

    try:
        raw = await request.json()
    except ValueError:
        return Response(status_code=400)

    parsed = parse_slack_payload(raw)
    if parsed is None:
        # Authenticated but structurally invalid: reject without retry loops.
        logger.warning("Rejecting malformed Slack payload")
        return Response(status_code=400)

    if parsed.kind == "url_verification":
        # Echo the challenge verbatim as plain text.
        return Response(content=parsed.challenge or "", media_type="text/plain")

    if not parsed.is_actionable:
        # Bot messages, subtypes, edits, empty text: acknowledge and ignore.
        return Response(status_code=200)

    assert parsed.team_id and parsed.channel_id and parsed.message_ts  # guaranteed by parser
    ctx = SlackMessageContext(
        team_id=parsed.team_id,
        channel_id=parsed.channel_id,
        message_ts=parsed.message_ts,
        user_id=parsed.user_id,
        text=parsed.text,
        event_id=parsed.event_id,
    )

    # Slack requires an ACK within 3 seconds; do the slow work afterwards.
    background_tasks.add_task(_process_in_background, ctx, zendesk, settings)
    return Response(status_code=200)