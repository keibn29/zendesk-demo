"""Zendesk webhook endpoint: normalize payload and react in Slack."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.api.deps import verify_zendesk_request
from app.api.payloads import parse_zendesk_payload
from app.clients import SlackClient
from app.core.config import Settings
from app.db.base import SessionFactory
from app.dependencies.providers import (
    build_reaction_service,
    get_settings_dep,
    get_slack_client,
)
from app.services.zendesk_reaction import ZendeskTicketEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["zendesk"])


async def _process_in_background(
    event: ZendeskTicketEvent,
    slack: SlackClient,
    settings: Settings,
) -> None:
    async with SessionFactory() as session:
        service = build_reaction_service(slack=slack, session=session, settings=settings)
        try:
            await service.apply_reaction(event)
        except Exception:
            # Failure is recorded as retryable so a replay can recover.
            logger.exception(
                "Background reaction failed", extra={"zendesk_ticket_id": event.ticket_id}
            )


@router.post("/webhooks/zendesk", status_code=200)
async def zendesk_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings_dep),
    slack: SlackClient = Depends(get_slack_client),
) -> Response:
    """Receive Zendesk ticket webhook events."""
    await verify_zendesk_request(request, settings)

    try:
        raw = await request.json()
    except ValueError:
        return Response(status_code=400)

    event = parse_zendesk_payload(raw)
    if event is None:
        # Authenticated but unusable: reject rather than ACK a bad shape.
        logger.warning("Rejecting malformed Zendesk payload")
        return Response(status_code=400)

    background_tasks.add_task(_process_in_background, event, slack, settings)
    return Response(status_code=200)