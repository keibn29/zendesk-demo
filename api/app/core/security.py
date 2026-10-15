"""HMAC signature verification for Slack and Zendesk webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime

SLACK_PREFIX = "v0="

# Zendesk sends the timestamp as an ISO-8601 string, not Unix seconds. We still
# require a timezone; a naive value is treated as UTC so hand-rolled senders do
# not silently fail, but the exact original string is always what gets signed.
ZENDESK_FUTURE_SKEW_SECONDS = 60


def slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Compute the expected `v0=` Slack signature for a raw request body."""
    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return SLACK_PREFIX + digest


def verify_slack_signature(
    secret: str,
    timestamp: str | None,
    body: bytes,
    provided_signature: str | None,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """Verify X-Slack-Signature with constant-time compare and replay guard.

    Slack sends Unix epoch seconds.
    """
    if not provided_signature or not timestamp:
        return False
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - request_time) > tolerance_seconds:
        return False
    expected = slack_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, provided_signature)


def zendesk_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Compute the Zendesk webhook signature: base64(HMAC-SHA256(ts + body)).

    `timestamp` must be the *exact* header string Zendesk sent; it is never
    re-formatted, because the sender signed that literal value.
    """
    mac = hmac.new(
        secret.encode(), timestamp.encode() + body, hashlib.sha256
    ).digest()
    return base64.b64encode(mac).decode()


def parse_zendesk_timestamp(timestamp: str) -> float | None:
    """Parse a Zendesk ISO-8601 timestamp header into epoch seconds.

    Returns None when the value is not a usable, timezone-aware ISO-8601
    timestamp. Accepts a trailing 'Z', explicit offsets, and fractional
    seconds. Naive (offset-less) values are rejected because their freshness
    cannot be determined reliably.
    """
    raw = timestamp.strip()
    if not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def verify_zendesk_signature(
    secret: str,
    timestamp: str | None,
    body: bytes,
    provided_signature: str | None,
    tolerance_seconds: int = 300,
    now: float | None = None,
    future_skew_seconds: int = ZENDESK_FUTURE_SKEW_SECONDS,
) -> bool:
    """Verify X-Zendesk-Webhook-Signature-Timestamp / -Signature.

    Freshness uses the parsed ISO-8601 timestamp; the HMAC always uses the
    original header string verbatim.
    """
    if not provided_signature or not timestamp:
        return False
    request_time = parse_zendesk_timestamp(timestamp)
    if request_time is None:
        return False
    current = time.time() if now is None else now
    age = current - request_time
    # Allow a small forward skew for clock drift, but never a far-future stamp.
    if age > tolerance_seconds or age < -future_skew_seconds:
        return False
    expected = zendesk_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, provided_signature)