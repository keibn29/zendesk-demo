"""SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TicketMapping(Base):
    """Zendesk ticket id -> originating Slack message coordinates."""

    __tablename__ = "ticket_mappings"
    __table_args__ = (
        UniqueConstraint("zendesk_ticket_id", name="uq_ticket_mappings_zendesk_ticket_id"),
        # One ticket per Slack message, enforced by the database so concurrent
        # deliveries cannot both create one.
        UniqueConstraint(
            "slack_team_id",
            "slack_channel_id",
            "slack_message_ts",
            name="uq_ticket_mappings_slack_message",
        ),
        Index("ix_ticket_mappings_zendesk_ticket_id", "zendesk_ticket_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zendesk_ticket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    slack_team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slack_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slack_message_ts: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ProcessedEvent(Base):
    """Deduplication ledger for Slack message coords and Zendesk invocations.

    status is one of:
      processing - claimed and in flight
      completed  - finished successfully (terminal; suppresses replay)
      failed     - attempt failed; retryable

    owner_token is a per-claim generation token. It changes every time the key
    is (re)claimed, so a worker whose lease expired and was superseded cannot
    write its result over the newer claim's state.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint("source", "dedupe_key", name="uq_processed_events_source_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )