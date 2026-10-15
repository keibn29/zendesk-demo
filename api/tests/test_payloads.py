"""Payload normalization for authenticated webhook bodies."""

from __future__ import annotations

import pytest

from app.api.payloads import parse_slack_payload, parse_zendesk_payload


# --- Slack ---

NON_OBJECT_BODIES = [[], [1, 2], None, "string", 123, 4.5, True]


@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
def test_slack_non_object_body_rejected(body):
    assert parse_slack_payload(body) is None


def test_slack_url_verification():
    parsed = parse_slack_payload({"type": "url_verification", "challenge": "abc123"})
    assert parsed is not None
    assert parsed.kind == "url_verification"
    assert parsed.challenge == "abc123"


@pytest.mark.parametrize("challenge", [123, None, [], {"a": 1}, "", "   "])
def test_slack_url_verification_bad_challenge_rejected(challenge):
    assert parse_slack_payload({"type": "url_verification", "challenge": challenge}) is None


def test_slack_event_callback_message():
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "hello",
                "ts": "1700000000.000100",
            },
        }
    )
    assert parsed is not None
    assert parsed.kind == "message"
    assert parsed.is_actionable is True
    assert parsed.team_id == "T1"
    assert parsed.event_id == "Ev1"


def test_slack_bot_message_not_actionable():
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "C1",
                "bot_id": "B1",
                "text": "hi",
                "ts": "1.1",
            },
        }
    )
    assert parsed is not None
    assert parsed.is_actionable is False


def test_slack_subtype_not_actionable():
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C1",
                "user": "U1",
                "text": "hi",
                "ts": "1.1",
            },
        }
    )
    assert parsed is not None and not parsed.is_actionable


def test_slack_empty_text_not_actionable():
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "   ",
                "ts": "1.1",
            },
        }
    )
    assert parsed is not None and not parsed.is_actionable


def test_slack_non_string_text_is_coerced_to_empty():
    """A wrong-typed text must not raise; it just is not actionable."""
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": {"nested": 1},
                "ts": "1.1",
            },
        }
    )
    assert parsed is not None
    assert parsed.text == ""
    assert parsed.is_actionable is False


@pytest.mark.parametrize("event", ["oops", [1, 2], 123])
def test_slack_wrong_typed_event_rejected(event):
    """Regression: a nested wrong type produced a 500."""
    assert parse_slack_payload({"type": "event_callback", "event": event}) is None


def test_slack_absent_event_ignored():
    parsed = parse_slack_payload({"type": "event_callback"})
    assert parsed is not None and parsed.kind == "ignored"


def test_slack_non_message_event_ignored():
    parsed = parse_slack_payload(
        {"type": "event_callback", "event": {"type": "reaction_added", "ts": "1"}}
    )
    assert parsed is not None and parsed.kind == "ignored"


def test_slack_missing_coordinates_ignored():
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi"},
        }
    )
    assert parsed is not None and parsed.kind == "ignored"


def test_slack_falls_back_to_event_team():
    parsed = parse_slack_payload(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "team": "T_FALLBACK",
                "text": "hi",
                "ts": "1.1",
            },
        }
    )
    assert parsed is not None and parsed.team_id == "T_FALLBACK"


# --- Zendesk ---


@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
def test_zendesk_non_object_body_rejected(body):
    assert parse_zendesk_payload(body) is None


def test_zendesk_modern_event_shape():
    event = parse_zendesk_payload(
        {
            "id": "01HZZ",
            "type": "zendesk:ticket.status_changed",
            "zendesk_event_id": "zev-1",
            "detail": {"id": 123, "ticket_id": 123, "status": "solved"},
        }
    )
    assert event is not None
    assert (event.ticket_id, event.status, event.invocation_id) == (123, "solved", "zev-1")


def test_zendesk_custom_trigger_shape():
    event = parse_zendesk_payload(
        {"invocation_id": "inv-1", "ticket_id": 123, "action": "pending"}
    )
    assert event is not None
    assert (event.ticket_id, event.status, event.invocation_id) == (123, "pending", "inv-1")


def test_zendesk_falls_back_to_id_for_invocation():
    event = parse_zendesk_payload(
        {"id": "abc", "detail": {"ticket_id": 3, "status": "solved"}}
    )
    assert event is not None and event.invocation_id == "abc"


def test_zendesk_custom_payload_without_invocation_id_rejected():
    """Regression: str(None) collided every delivery into one dedupe key."""
    assert parse_zendesk_payload({"ticket_id": 123, "action": "solved"}) is None


def test_zendesk_distinct_invocation_ids_kept_distinct():
    a = parse_zendesk_payload({"invocation_id": "a", "ticket_id": 1, "action": "solved"})
    b = parse_zendesk_payload({"invocation_id": "b", "ticket_id": 2, "action": "solved"})
    assert a is not None and b is not None
    assert a.invocation_id != b.invocation_id


@pytest.mark.parametrize("ticket_id", ["abc", None, -1, 0, 1.5, True, [1], {"a": 1}])
def test_zendesk_invalid_ticket_id_rejected(ticket_id):
    assert (
        parse_zendesk_payload(
            {"invocation_id": "i", "ticket_id": ticket_id, "action": "solved"}
        )
        is None
    )


@pytest.mark.parametrize("detail", ["oops", [1, 2], 123])
def test_zendesk_wrong_typed_detail_falls_through(detail):
    """A non-mapping detail must not raise; it simply provides nothing."""
    event = parse_zendesk_payload(
        {"id": "abc", "detail": detail, "ticket_id": 5, "action": "solved"}
    )
    assert event is not None and event.ticket_id == 5


@pytest.mark.parametrize("action", [[1, 2], {"a": 1}, "", "   ", 5])
def test_zendesk_invalid_action_rejected(action):
    assert (
        parse_zendesk_payload({"invocation_id": "i", "ticket_id": 1, "action": action})
        is None
    )


def test_zendesk_missing_status_rejected():
    assert parse_zendesk_payload({"invocation_id": "i", "ticket_id": 1}) is None