"""Endpoint tests: /health and both signed webhooks."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.security import slack_signature, zendesk_signature
from app.dependencies.providers import get_settings_dep

SLACK_SECRET = "test-slack-signing-secret"
ZD_SECRET = "test-zendesk-webhook-secret"


def _zendesk_ts(offset: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def post_slack(client: TestClient, body: Any, *, ts: str | None = None, secret: str = SLACK_SECRET):
    raw = json.dumps(body).encode()
    stamp = ts if ts is not None else str(int(time.time()))
    return client.post(
        "/webhooks/slack/events",
        content=raw,
        headers={
            "content-type": "application/json",
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": slack_signature(secret, stamp, raw),
        },
    )


def post_zendesk(client: TestClient, body: Any, *, ts: str | None = None, secret: str = ZD_SECRET):
    raw = json.dumps(body).encode()
    stamp = ts if ts is not None else _zendesk_ts()
    return client.post(
        "/webhooks/zendesk",
        content=raw,
        headers={
            "content-type": "application/json",
            "X-Zendesk-Webhook-Signature-Timestamp": stamp,
            "X-Zendesk-Webhook-Signature": zendesk_signature(secret, stamp, raw),
        },
    )


class TestHealth:
    def test_health_ok(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestSlackSignatureRejection:
    def test_bad_signature_rejected(self, client: TestClient):
        response = client.post(
            "/webhooks/slack/events",
            json={"type": "url_verification", "challenge": "x"},
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=bad",
            },
        )
        assert response.status_code == 401

    def test_missing_signature_headers_rejected(self, client: TestClient):
        response = client.post("/webhooks/slack/events", json={"type": "url_verification"})
        assert response.status_code == 401

    def test_stale_timestamp_rejected(self, client: TestClient):
        response = post_slack(
            client, {"type": "url_verification", "challenge": "x"},
            ts=str(int(time.time()) - 4000),
        )
        assert response.status_code == 401

    def test_wrong_secret_rejected(self, client: TestClient):
        response = post_slack(
            client, {"type": "url_verification", "challenge": "x"}, secret="wrong-secret"
        )
        assert response.status_code == 401


class TestSlackEndpoint:
    def test_url_verification_echoes_challenge(self, client: TestClient):
        response = post_slack(
            client, {"type": "url_verification", "challenge": "abc123"}
        )
        assert response.status_code == 200
        assert response.text == "abc123"

    def test_event_callback_acked(self, client: TestClient):
        response = post_slack(
            client,
            {
                "type": "event_callback",
                "team_id": "T1",
                "event_id": "Ev-test-1",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "user": "U1",
                    "text": "hello",
                    "ts": "1700000000.000200",
                },
            },
        )
        assert response.status_code == 200

    def test_bot_message_acked(self, client: TestClient):
        response = post_slack(
            client,
            {
                "type": "event_callback",
                "team_id": "T1",
                "event_id": "Ev-bot",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "bot_id": "B1",
                    "text": "hello",
                    "ts": "1700000000.000300",
                },
            },
        )
        assert response.status_code == 200

    def test_non_message_event_acked(self, client: TestClient):
        response = post_slack(
            client,
            {
                "type": "event_callback",
                "event_id": "Ev-reaction",
                "event": {"type": "reaction_added", "ts": "1", "channel": "C1"},
            },
        )
        assert response.status_code == 200

    def test_unrelated_type_acked(self, client: TestClient):
        response = post_slack(client, {"type": "something_else"})
        assert response.status_code == 200

    @pytest.mark.parametrize("body", [[1, 2], None, "string", 123, True])
    def test_non_object_payload_returns_400(self, client: TestClient, body):
        """Regression: these produced 500s."""
        response = post_slack(client, body)
        assert response.status_code == 400

    def test_nested_wrong_type_returns_400(self, client: TestClient):
        response = post_slack(client, {"type": "event_callback", "event": "oops"})
        assert response.status_code == 400

    def test_url_verification_bad_challenge_returns_400(self, client: TestClient):
        response = post_slack(client, {"type": "url_verification", "challenge": 123})
        assert response.status_code == 400

    def test_invalid_json_returns_400(self, client: TestClient):
        raw = b"{not json"
        stamp = str(int(time.time()))
        response = client.post(
            "/webhooks/slack/events",
            content=raw,
            headers={
                "content-type": "application/json",
                "X-Slack-Request-Timestamp": stamp,
                "X-Slack-Signature": slack_signature(SLACK_SECRET, stamp, raw),
            },
        )
        assert response.status_code == 400


class TestZendeskSignatureRejection:
    def test_bad_signature_rejected(self, client: TestClient):
        response = client.post(
            "/webhooks/zendesk",
            json={"invocation_id": "i1", "ticket_id": 1, "action": "solved"},
            headers={
                "X-Zendesk-Webhook-Signature-Timestamp": _zendesk_ts(),
                "X-Zendesk-Webhook-Signature": "bogus",
            },
        )
        assert response.status_code == 401

    def test_missing_signature_headers_rejected(self, client: TestClient):
        response = client.post(
            "/webhooks/zendesk",
            json={"invocation_id": "i1", "ticket_id": 1, "action": "solved"},
        )
        assert response.status_code == 401

    def test_stale_iso_timestamp_rejected(self, client: TestClient):
        response = post_zendesk(
            client,
            {"invocation_id": "i1", "ticket_id": 1, "action": "solved"},
            ts=_zendesk_ts(-400),
        )
        assert response.status_code == 401

    def test_unix_epoch_timestamp_rejected(self, client: TestClient):
        """Regression: real Zendesk payloads use ISO-8601, not epoch."""
        stamp = str(int(time.time()))
        response = post_zendesk(
            client,
            {"invocation_id": "i1", "ticket_id": 1, "action": "solved"},
            ts=stamp,
        )
        assert response.status_code == 401


class TestZendeskEndpoint:
    def test_valid_custom_payload_acked(self, client: TestClient):
        response = post_zendesk(
            client, {"invocation_id": "i1", "ticket_id": 1, "action": "solved"}
        )
        assert response.status_code == 200

    def test_valid_event_payload_acked(self, client: TestClient):
        response = post_zendesk(
            client,
            {
                "id": "abc",
                "type": "zendesk:ticket.status_changed",
                "zendesk_event_id": "zev-1",
                "detail": {"id": 5, "ticket_id": 5, "status": "solved"},
            },
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("body", [[], None, "x", 5])
    def test_non_object_payload_returns_400(self, client: TestClient, body):
        """Regression: these produced 500s."""
        response = post_zendesk(client, body)
        assert response.status_code == 400

    @pytest.mark.parametrize("ticket_id", ["abc", -1, 0, 1.5, None])
    def test_invalid_ticket_id_returns_400(self, client: TestClient, ticket_id):
        response = post_zendesk(
            client,
            {"invocation_id": "i1", "ticket_id": ticket_id, "action": "solved"},
        )
        assert response.status_code == 400

    def test_missing_invocation_id_returns_400(self, client: TestClient):
        response = post_zendesk(client, {"ticket_id": 1, "action": "solved"})
        assert response.status_code == 400

    def test_unrecognised_payload_returns_400(self, client: TestClient):
        response = post_zendesk(client, {"hello": "world"})
        assert response.status_code == 400

    def test_invalid_json_returns_400(self, client: TestClient):
        raw = b"{not json"
        stamp = _zendesk_ts()
        response = client.post(
            "/webhooks/zendesk",
            content=raw,
            headers={
                "content-type": "application/json",
                "X-Zendesk-Webhook-Signature-Timestamp": stamp,
                "X-Zendesk-Webhook-Signature": zendesk_signature(ZD_SECRET, stamp, raw),
            },
        )
        assert response.status_code == 400


class TestSharedTokenLayer:
    """The optional shared token is checked in addition to the signature."""

    def _override(self, app_, **overrides):
        from app.core.config import Settings

        kwargs = {
            "slack_signing_secret": SLACK_SECRET,
            "slack_bot_token": "xoxb-test-token",
            "zendesk_subdomain": "testsub",
            "zendesk_webhook_secret": ZD_SECRET,
            "zendesk_oauth_token": "test-zendesk-oauth-token",
            "environment": "test",
        }
        kwargs.update(overrides)
        return Settings(**kwargs)

    def test_empty_token_means_disabled(self, client: TestClient):
        settings = self._override(client.app, webhook_shared_token="")
        assert settings.shared_token_enabled is False
        response = post_slack(client, {"type": "url_verification", "challenge": "ok"})
        assert response.status_code == 200

    def test_token_enforced_when_set(self, client: TestClient):
        client.app.dependency_overrides[get_settings_dep] = lambda: self._override(
            client.app, webhook_shared_token="expected-token"
        )
        try:
            # No token header supplied -> rejected even with a valid signature.
            response = post_slack(client, {"type": "url_verification", "challenge": "ok"})
            assert response.status_code == 401

            # Wrong token -> rejected.
            raw = json.dumps({"type": "url_verification", "challenge": "ok"}).encode()
            stamp = str(int(time.time()))
            response = client.post(
                "/webhooks/slack/events",
                content=raw,
                headers={
                    "content-type": "application/json",
                    "X-Slack-Request-Timestamp": stamp,
                    "X-Slack-Signature": slack_signature(SLACK_SECRET, stamp, raw),
                    "X-Webhook-Token": "wrong",
                },
            )
            assert response.status_code == 401

            # Correct token -> accepted.
            response = client.post(
                "/webhooks/slack/events",
                content=raw,
                headers={
                    "content-type": "application/json",
                    "X-Slack-Request-Timestamp": stamp,
                    "X-Slack-Signature": slack_signature(SLACK_SECRET, stamp, raw),
                    "X-Webhook-Token": "expected-token",
                },
            )
            assert response.status_code == 200
        finally:
            client.app.dependency_overrides.clear()

    def test_custom_header_name_is_honored(self, client: TestClient):
        """Regression: the header name was hardcoded instead of read from config."""
        client.app.dependency_overrides[get_settings_dep] = lambda: self._override(
            client.app,
            webhook_shared_token="expected-token",
            webhook_shared_token_header="X-Custom-Guard",
        )
        try:
            raw = json.dumps({"type": "url_verification", "challenge": "ok"}).encode()
            stamp = str(int(time.time()))
            base_headers = {
                "content-type": "application/json",
                "X-Slack-Request-Timestamp": stamp,
                "X-Slack-Signature": slack_signature(SLACK_SECRET, stamp, raw),
            }

            # Default header name no longer works.
            assert (
                client.post(
                    "/webhooks/slack/events",
                    content=raw,
                    headers={**base_headers, "X-Webhook-Token": "expected-token"},
                ).status_code
                == 401
            )

            # Configured name works.
            assert (
                client.post(
                    "/webhooks/slack/events",
                    content=raw,
                    headers={**base_headers, "X-Custom-Guard": "expected-token"},
                ).status_code
                == 200
            )
        finally:
            client.app.dependency_overrides.clear()

class TestBackgroundWorkUsesFakes:
    """Endpoint tests must never reach a real upstream API."""

    def test_slack_message_creates_ticket_via_fake(
        self, client, fake_zendesk, fake_slack
    ):
        response = post_slack(
            client,
            {
                "type": "event_callback",
                "team_id": "T1",
                "event_id": "Ev-bg-1",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "user": "U1",
                    "text": "please help",
                    "ts": "1700000000.009999",
                },
            },
        )
        assert response.status_code == 200
        # BackgroundTasks run synchronously before TestClient returns.
        assert len(fake_zendesk.calls) == 1
        assert fake_zendesk.calls[0]["comment"] == "please help"
        assert fake_slack.calls == []

    def test_zendesk_status_event_reacts_via_fake(
        self, client, fake_zendesk, fake_slack
    ):
        # First create a mapping via a Slack message.
        post_slack(
            client,
            {
                "type": "event_callback",
                "team_id": "T1",
                "event_id": "Ev-bg-2",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "user": "U1",
                    "text": "another issue",
                    "ts": "1700000000.008888",
                },
            },
        )
        assert len(fake_zendesk.calls) == 1
        ticket_id = 4242

        response = post_zendesk(
            client,
            {"invocation_id": "inv-bg-1", "ticket_id": ticket_id, "action": "solved"},
        )
        assert response.status_code == 200
        assert len(fake_slack.calls) == 1
        assert fake_slack.calls[0]["name"] == "white_check_mark"

    def test_bot_message_does_no_background_work(self, client, fake_zendesk):
        response = post_slack(
            client,
            {
                "type": "event_callback",
                "team_id": "T1",
                "event_id": "Ev-bg-3",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "bot_id": "B1",
                    "text": "ignored",
                    "ts": "1700000000.007777",
                },
            },
        )
        assert response.status_code == 200
        assert fake_zendesk.calls == []

    def test_duplicate_slack_delivery_creates_one_ticket(
        self, client, fake_zendesk
    ):
        payload = {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev-bg-4",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "duplicate me",
                "ts": "1700000000.006666",
            },
        }
        assert post_slack(client, payload).status_code == 200
        assert post_slack(client, payload).status_code == 200
        assert len(fake_zendesk.calls) == 1
