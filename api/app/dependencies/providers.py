"""Dependency injection wiring: settings -> clients -> repositories -> services.

Endpoints only ever build a service through one of the `build_*` factories, so
service construction (including the reaction map) lives in exactly one place.
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import SlackClient, ZendeskClient
from app.core.config import Settings, get_settings
from app.db.base import get_db
from app.repositories import ProcessedEventRepository, TicketMappingRepository
from app.services.slack_ticket import TicketCreationService
from app.services.zendesk_reaction import ReactionService


def get_settings_dep() -> Settings:
    return get_settings()


def get_zendesk_client(request: Request) -> ZendeskClient:
    """Long-lived client stored on app.state (created at startup)."""
    client = getattr(request.app.state, "zendesk_client", None)
    if client is None:  # pragma: no cover - startup always sets it
        raise RuntimeError("Zendesk client not initialised")
    return client


def get_slack_client(request: Request) -> SlackClient:
    client = getattr(request.app.state, "slack_client", None)
    if client is None:  # pragma: no cover - startup always sets it
        raise RuntimeError("Slack client not initialised")
    return client


def get_ticket_repo(session: AsyncSession = Depends(get_db)) -> TicketMappingRepository:
    return TicketMappingRepository(session)


def get_event_repo(session: AsyncSession = Depends(get_db)) -> ProcessedEventRepository:
    return ProcessedEventRepository(session)


def build_ticket_creation_service(
    *, zendesk: ZendeskClient, session: AsyncSession, settings: Settings
) -> TicketCreationService:
    """Single construction site for the ticket-creation service."""
    return TicketCreationService(
        zendesk=zendesk,
        ticket_repo=TicketMappingRepository(session),
        event_repo=ProcessedEventRepository(session),
        channel_allowlist=settings.slack_channel_allowlist,
        claim_timeout_seconds=settings.processing_claim_timeout_seconds,
        processing_deadline_seconds=settings.processing_deadline_seconds,
    )


def build_reaction_service(
    *, slack: SlackClient, session: AsyncSession, settings: Settings
) -> ReactionService:
    """Single construction site for the reaction service."""
    return ReactionService(
        slack=slack,
        ticket_repo=TicketMappingRepository(session),
        event_repo=ProcessedEventRepository(session),
        reactions=settings.reaction_map(),
        claim_timeout_seconds=settings.processing_claim_timeout_seconds,
        processing_deadline_seconds=settings.processing_deadline_seconds,
    )


# --- FastAPI dependency providers (used by tests / override points) ---

def get_ticket_creation_service(
    zendesk: ZendeskClient = Depends(get_zendesk_client),
    ticket_repo: TicketMappingRepository = Depends(get_ticket_repo),
    event_repo: ProcessedEventRepository = Depends(get_event_repo),
    settings: Settings = Depends(get_settings_dep),
) -> TicketCreationService:
    return TicketCreationService(
        zendesk=zendesk,
        ticket_repo=ticket_repo,
        event_repo=event_repo,
        channel_allowlist=settings.slack_channel_allowlist,
        claim_timeout_seconds=settings.processing_claim_timeout_seconds,
    )


def get_reaction_service(
    slack: SlackClient = Depends(get_slack_client),
    ticket_repo: TicketMappingRepository = Depends(get_ticket_repo),
    event_repo: ProcessedEventRepository = Depends(get_event_repo),
    settings: Settings = Depends(get_settings_dep),
) -> ReactionService:
    return ReactionService(
        slack=slack,
        ticket_repo=ticket_repo,
        event_repo=event_repo,
        reactions=settings.reaction_map(),
        claim_timeout_seconds=settings.processing_claim_timeout_seconds,
        processing_deadline_seconds=settings.processing_deadline_seconds,
    )