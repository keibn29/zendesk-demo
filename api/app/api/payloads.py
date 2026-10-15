"""Parsers for authenticated webhook payloads.

These run only after the request signature has been verified, so they assume a
trusted sender but must never raise: a malformed body must produce a safe,
explicit result rather than a 500.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.zendesk_reaction import ZendeskTicketEvent

# Statuses/keys we accept as a reaction trigger.
_ACTION_MAX_LEN = 64
_INVOCATION_MAX_LEN = 255


def _as_mapping(value: object) -> dict | None:
    """Return value if it is a usable JSON object, else None."""
    return value if isinstance(value, dict) else None


def _positive_int(value: object) -> int | None:
    """Accept a positive integral ID; reject strings, floats, bools, <= 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _clean_str(value: object, *, max_len: int) -> str | None:
    """Return a non-empty stripped string within max_len, else None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > max_len:
        return None
    return stripped


def parse_slack_payload(payload: object) -> "SlackPayload | None":
    """Normalize a verified Slack Events API body.

    Returns None for any shape we cannot safely handle (non-object body,
    wrong-typed nested fields). Callers should ACK such payloads with 400 rather
    than attempting work.
    """
    body = _as_mapping(payload)
    if body is None:
        return None

    body_type = _clean_str(body.get("type"), max_len=64)
    if body_type == "url_verification":
        challenge = _clean_str(body.get("challenge"), max_len=2048)
        if challenge is None:
            return None
        return SlackPayload(kind="url_verification", challenge=challenge)

    if body_type != "event_callback":
        return SlackPayload(kind="ignored")

    event = body.get("event")
    if event is None:
        return SlackPayload(kind="ignored")
    # Present but the wrong type (string/list/scalar) is malformed, not ignorable.
    event = _as_mapping(event)
    if event is None:
        return None

    event_type = _clean_str(event.get("type"), max_len=64)
    if event_type != "message":
        return SlackPayload(kind="ignored")

    team_id = _clean_str(body.get("team_id"), max_len=64) or _clean_str(
        event.get("team"), max_len=64
    )
    channel_id = _clean_str(event.get("channel"), max_len=64)
    message_ts = _clean_str(event.get("ts"), max_len=64)
    text = event.get("text")
    text = text if isinstance(text, str) else ""
    user_id = _clean_str(event.get("user"), max_len=64)
    event_id = _clean_str(body.get("event_id"), max_len=255)

    if not team_id or not channel_id or not message_ts:
        return SlackPayload(kind="ignored")

    return SlackPayload(
        kind="message",
        team_id=team_id,
        channel_id=channel_id,
        message_ts=message_ts,
        user_id=user_id,
        text=text,
        event_id=event_id,
        bot_id=_clean_str(event.get("bot_id"), max_len=64),
        subtype=_clean_str(event.get("subtype"), max_len=64),
    )


@dataclass(frozen=True)
class SlackPayload:
    """Normalized view over a verified Slack payload."""

    kind: str  # "message" | "url_verification" | "ignored"
    challenge: str | None = None
    team_id: str | None = None
    channel_id: str | None = None
    message_ts: str | None = None
    user_id: str | None = None
    text: str = ""
    event_id: str | None = None
    bot_id: str | None = None
    subtype: str | None = None

    @property
    def is_actionable(self) -> bool:
        """Plain, non-empty, human-authored message."""
        return (
            self.kind == "message"
            and self.bot_id is None
            and self.subtype is None
            and bool(self.text.strip())
            and self.user_id is not None
        )


def parse_zendesk_payload(payload: object) -> ZendeskTicketEvent | None:
    """Normalize a verified Zendesk webhook body.

    Supports the modern `zendesk:ticket.status_changed` event shape and the
    documented custom trigger shape. The invocation id is required in both
    cases: without it every delivery would share one dedupe key and all but
    the first would be silently skipped.
    """
    body = _as_mapping(payload)
    if body is None:
        return None

    detail = _as_mapping(body.get("detail")) or {}

    ticket_id = (
        _positive_int(body.get("ticket_id"))
        or _positive_int(detail.get("ticket_id"))
        or _positive_int(detail.get("id"))
    )
    if ticket_id is None:
        return None

    invocation_id = (
        _clean_str(body.get("invocation_id"), max_len=_INVOCATION_MAX_LEN)
        or _clean_str(body.get("zendesk_event_id"), max_len=_INVOCATION_MAX_LEN)
        or _clean_str(body.get("id"), max_len=_INVOCATION_MAX_LEN)
    )
    if invocation_id is None:
        return None

    status = (
        _clean_str(detail.get("status"), max_len=_ACTION_MAX_LEN)
        or _clean_str(detail.get("action"), max_len=_ACTION_MAX_LEN)
        or _clean_str(body.get("status"), max_len=_ACTION_MAX_LEN)
        or _clean_str(body.get("action"), max_len=_ACTION_MAX_LEN)
    )
    if status is None:
        return None

    return ZendeskTicketEvent(
        ticket_id=ticket_id, status=status, invocation_id=invocation_id
    )