"""Upstream HTTP client behavior: auth headers, request shape, error handling.

Every request goes through httpx.MockTransport, so no external calls are made.
"""

from __future__ import annotations

import httpx
import json

import pytest

from app.clients import UpstreamError, ZendeskClient

# --- Zendesk ---


async def test_zendesk_uses_oauth_bearer_and_idempotency_key(zendesk_client_factory):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        captured["idem"] = request.headers.get("idempotency-key", "")
        return httpx.Response(200, json={"ticket": {"id": 7}})

    client = zendesk_client_factory(handler)
    ticket = await client.create_ticket(
        subject="s", comment="c", external_id="e", idempotency_key="k"
    )
    assert ticket["id"] == 7
    assert captured["auth"] == "Bearer test-zendesk-oauth-token"
    assert captured["idem"] == "k"


async def test_zendesk_falls_back_to_basic_email_token_auth(zendesk_client_factory):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ticket": {"id": 1}})

    client = zendesk_client_factory(
        handler, oauth_token=None, email="agent@test.com", api_token="legacy-token"
    )
    await client.create_ticket(
        subject="s", comment="c", external_id="e", idempotency_key="k"
    )
    assert captured["auth"].startswith("Basic ")


async def test_zendesk_sends_expected_json_body(zendesk_client_factory):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ticket": {"id": 2}})

    client = zendesk_client_factory(handler)
    await client.create_ticket(
        subject="subject here",
        comment="body text",
        external_id="slack:T:C:1",
        idempotency_key="k",
        tags=["slack"],
    )
    assert captured["body"]["ticket"]["subject"] == "subject here"
    assert captured["body"]["ticket"]["comment"]["body"] == "body text"
    assert captured["body"]["ticket"]["external_id"] == "slack:T:C:1"


async def test_zendesk_raises_on_non_json_response(zendesk_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = zendesk_client_factory(handler)
    with pytest.raises(UpstreamError):
        await client.create_ticket(
            subject="s", comment="c", external_id="e", idempotency_key="k"
        )


async def test_zendesk_raises_on_http_error(zendesk_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "RecordInvalid"})

    client = zendesk_client_factory(handler)
    with pytest.raises(UpstreamError) as exc:
        await client.create_ticket(
            subject="s", comment="c", external_id="e", idempotency_key="k"
        )
    assert exc.value.status_code == 422


async def test_zendesk_raises_when_ticket_missing(zendesk_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_a_ticket": {}})

    client = zendesk_client_factory(handler)
    with pytest.raises(UpstreamError):
        await client.create_ticket(
            subject="s", comment="c", external_id="e", idempotency_key="k"
        )


async def test_zendesk_wraps_timeout(zendesk_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = zendesk_client_factory(handler)
    with pytest.raises(UpstreamError, match="timed out"):
        await client.create_ticket(
            subject="s", comment="c", external_id="e", idempotency_key="k"
        )


async def test_zendesk_requires_some_credentials():
    with pytest.raises(ValueError):
        ZendeskClient(base_url="https://x.zendesk.com", timeout=5)


# --- Slack ---


async def test_slack_add_reaction_sends_json_body_and_content_type(slack_client_factory):
    """reactions.add must send a JSON body matching the declared Content-Type."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization", "")
        captured["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    client = slack_client_factory(handler)
    assert await client.add_reaction(
        channel="C123", name="eyes", timestamp="1700000000.000100"
    )

    assert captured["content_type"].startswith("application/json")
    assert captured["body"] == {
        "channel": "C123",
        "name": "eyes",
        "timestamp": "1700000000.000100",
    }
    assert captured["auth"] == "Bearer xoxb-test-token"
    assert captured["path"] == "/api/reactions.add"


async def test_slack_treats_already_reacted_as_success(slack_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "already_reacted"})

    client = slack_client_factory(handler)
    assert await client.add_reaction(channel="C", name="eyes", timestamp="1") is True


async def test_slack_raises_on_other_api_error(slack_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = slack_client_factory(handler)
    with pytest.raises(UpstreamError, match="channel_not_found"):
        await client.add_reaction(channel="C", name="eyes", timestamp="1")


async def test_slack_raises_on_non_json(slack_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    client = slack_client_factory(handler)
    with pytest.raises(UpstreamError):
        await client.add_reaction(channel="C", name="eyes", timestamp="1")


async def test_slack_wraps_timeout(slack_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = slack_client_factory(handler)
    with pytest.raises(UpstreamError, match="timed out"):
        await client.add_reaction(channel="C", name="eyes", timestamp="1")