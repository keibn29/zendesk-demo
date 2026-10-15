"""Signature verification tests: Slack (epoch) and Zendesk (ISO-8601)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone

import pytest

from app.core.security import (
    parse_zendesk_timestamp,
    verify_slack_signature,
    verify_zendesk_signature,
    zendesk_signature,
)

SLACK_SECRET = "test-slack-signing-secret"
ZD_SECRET = "test-zendesk-webhook-secret"
BODY = b'{"type":"event_callback","event_id":"Ev1"}'
NOW = 1_700_000_000


def _slack_sig(secret: str, ts: str, body: bytes) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def iso(offset_seconds: float = 0.0, fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    moment = datetime.fromtimestamp(NOW + offset_seconds, tz=timezone.utc)
    return moment.strftime(fmt)


# --- Slack (Unix epoch seconds) ---


class TestSlackSignature:
    def test_valid_signature_accepted(self):
        ts = str(NOW)
        assert verify_slack_signature(
            SLACK_SECRET, ts, BODY, _slack_sig(SLACK_SECRET, ts, BODY), 300, now=NOW
        )

    def test_wrong_secret_rejected(self):
        ts = str(NOW)
        assert not verify_slack_signature(
            SLACK_SECRET, ts, BODY, _slack_sig("other", ts, BODY), 300, now=NOW
        )

    def test_tampered_body_rejected(self):
        ts = str(NOW)
        sig = _slack_sig(SLACK_SECRET, ts, BODY)
        assert not verify_slack_signature(
            SLACK_SECRET, ts, BODY + b" ", sig, 300, now=NOW
        )

    def test_replay_outside_tolerance_rejected(self):
        ts = str(NOW - 301)
        sig = _slack_sig(SLACK_SECRET, ts, BODY)
        assert not verify_slack_signature(SLACK_SECRET, ts, BODY, sig, 300, now=NOW)

    def test_replay_inside_tolerance_accepted(self):
        ts = str(NOW - 299)
        sig = _slack_sig(SLACK_SECRET, ts, BODY)
        assert verify_slack_signature(SLACK_SECRET, ts, BODY, sig, 300, now=NOW)

    def test_missing_signature_rejected(self):
        assert not verify_slack_signature(SLACK_SECRET, str(NOW), BODY, None, 300, now=NOW)

    def test_missing_timestamp_rejected(self):
        sig = _slack_sig(SLACK_SECRET, str(NOW), BODY)
        assert not verify_slack_signature(SLACK_SECRET, None, BODY, sig, 300, now=NOW)

    def test_non_numeric_timestamp_rejected(self):
        assert not verify_slack_signature(
            SLACK_SECRET, "abc", BODY, "v0=deadbeef", 300, now=NOW
        )

    @pytest.mark.parametrize("sig", ["", "v0=", "not-hex", "v0=zz"])
    def test_garbage_signatures_rejected(self, sig: str):
        assert not verify_slack_signature(
            SLACK_SECRET, str(NOW), BODY, sig, 300, now=NOW
        )

    def test_uses_current_time_by_default(self):
        ts = str(int(time.time()))
        assert verify_slack_signature(SLACK_SECRET, ts, BODY, _slack_sig(SLACK_SECRET, ts, BODY))


# --- Zendesk timestamp parsing ---


class TestZendeskTimestampParsing:
    def test_trailing_z(self):
        assert parse_zendesk_timestamp("2023-01-01T00:00:00Z") is not None

    def test_explicit_offset(self):
        assert parse_zendesk_timestamp("2023-01-01T02:00:00+02:00") is not None

    def test_fractional_seconds(self):
        assert parse_zendesk_timestamp("2023-01-01T00:00:00.123Z") is not None

    def test_lowercase_z_accepted(self):
        assert parse_zendesk_timestamp("2023-01-01T00:00:00z") is not None

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not-a-date",
            "1672531199",  # unix seconds are NOT the Zendesk format
            "2023-01-01T00:00:00",  # naive: no timezone
            "2023-13-45T99:99:99Z",
        ],
    )
    def test_invalid_values_return_none(self, value: str):
        assert parse_zendesk_timestamp(value) is None


# --- Zendesk signature ---


class TestZendeskSignature:
    def test_known_vector(self):
        ts = "1595373913"
        body = b'{"ticket_id":1}'
        mac = hmac.new(ZD_SECRET.encode(), ts.encode() + body, hashlib.sha256).digest()
        assert zendesk_signature(ZD_SECRET, ts, body) == base64.b64encode(mac).decode()

    def test_real_world_iso_timestamp_accepted(self):
        """A genuine Zendesk ISO-8601 header must verify."""
        ts = iso()
        assert verify_zendesk_signature(
            ZD_SECRET, ts, BODY, zendesk_signature(ZD_SECRET, ts, BODY), 300, now=NOW
        )

    def test_exact_header_string_is_signed(self):
        """Signature is computed over the literal header, not a reformatted one."""
        ts = iso(fmt="%Y-%m-%dT%H:%M:%S.%fZ")
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert verify_zendesk_signature(ZD_SECRET, ts, BODY, sig, 300, now=NOW)

    def test_wrong_secret_rejected(self):
        ts = iso()
        assert not verify_zendesk_signature(
            ZD_SECRET, ts, BODY, zendesk_signature("nope", ts, BODY), 300, now=NOW
        )

    def test_body_must_match_exactly(self):
        ts = iso()
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert not verify_zendesk_signature(
            ZD_SECRET, ts, BODY.replace(b"Ev1", b"Ev2"), sig, 300, now=NOW
        )

    def test_stale_timestamp_rejected(self):
        ts = iso(-400)
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert not verify_zendesk_signature(ZD_SECRET, ts, BODY, sig, 300, now=NOW)

    def test_small_future_skew_tolerated(self):
        """Modest forward clock drift is tolerated."""
        ts = iso(30)
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert verify_zendesk_signature(ZD_SECRET, ts, BODY, sig, 300, now=NOW)

    def test_far_future_timestamp_rejected(self):
        ts = iso(86_400)
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert not verify_zendesk_signature(ZD_SECRET, ts, BODY, sig, 300, now=NOW)

    def test_future_skew_is_bounded(self):
        ts = iso(120)
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert not verify_zendesk_signature(ZD_SECRET, ts, BODY, sig, 300, now=NOW)

    @pytest.mark.parametrize("bad", ["", "garbage", "1672531199", "2023-01-01T00:00:00"])
    def test_unusable_timestamp_rejected(self, bad: str):
        assert not verify_zendesk_signature(ZD_SECRET, bad, BODY, "sig", 300, now=NOW)

    def test_missing_signature_rejected(self):
        assert not verify_zendesk_signature(
            ZD_SECRET, iso(), BODY, None, 300, now=NOW
        )

    def test_uses_current_time_by_default(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert verify_zendesk_signature(
            ZD_SECRET, ts, BODY, zendesk_signature(ZD_SECRET, ts, BODY)
        )

    def test_rejects_unix_epoch_header(self):
        """Regression: the old int() parsing silently failed on real payloads."""
        ts = str(NOW)
        sig = zendesk_signature(ZD_SECRET, ts, BODY)
        assert not verify_zendesk_signature(ZD_SECRET, ts, BODY, sig, 300, now=NOW)