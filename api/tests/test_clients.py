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

# --- Zendesk client_credentials token provider ---

from app.clients import ZendeskTokenProvider  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _provider(handler, clock=None, **kw) -> ZendeskTokenProvider:
    return ZendeskTokenProvider(
        base_url="https://testsub.zendesk.com",
        client_id="slack_bridge",
        client_secret="s3cr3t-value",
        scopes="tickets:read tickets:write",
        ttl_seconds=3600,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock or _Clock(),
        **kw,
    )


def _token_handler(calls: list, tokens: list[str], expires_in: int = 1800):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/tokens":
            calls.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"access_token": tokens[len(calls) - 1], "expires_in": expires_in},
            )
        return httpx.Response(200, json={"ticket": {"id": 5}})

    return handler


async def test_token_fetched_once_and_reused():
    calls: list = []
    provider = _provider(_token_handler(calls, ["t1", "t2"]))
    assert await provider.get_token() == "t1"
    assert await provider.get_token() == "t1"
    assert len(calls) == 1
    assert calls[0] == {
        "grant_type": "client_credentials",
        "client_id": "slack_bridge",
        "client_secret": "s3cr3t-value",
        "scope": "tickets:read tickets:write",
        "expires_in": 3600,
    }


async def test_token_refreshed_near_expiry():
    calls: list = []
    clock = _Clock()
    provider = _provider(_token_handler(calls, ["t1", "t2"], expires_in=1800), clock)
    assert await provider.get_token() == "t1"
    clock.now += 1800 - 59  # inside the 60s refresh leeway
    assert await provider.get_token() == "t2"
    assert len(calls) == 2


async def test_concurrent_callers_share_one_fetch():
    import asyncio

    calls: list = []
    provider = _provider(_token_handler(calls, ["t1", "t2"]))
    results = await asyncio.gather(*(provider.get_token() for _ in range(5)))
    assert results == ["t1"] * 5
    assert len(calls) == 1


async def test_client_refreshes_and_retries_once_on_401():
    token_calls: list = []
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/tokens":
            token_calls.append(1)
            return httpx.Response(
                200, json={"access_token": f"t{len(token_calls)}", "expires_in": 1800}
            )
        seen_auth.append(request.headers["authorization"])
        if len(seen_auth) == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"ticket": {"id": 9}})

    provider = _provider(handler)
    client = ZendeskClient(
        base_url="https://testsub.zendesk.com",
        timeout=5,
        token_provider=provider,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    ticket = await client.create_ticket(
        subject="s", comment="c", external_id="e", idempotency_key="k"
    )
    assert ticket["id"] == 9
    assert seen_auth == ["Bearer t1", "Bearer t2"]
    assert len(token_calls) == 2


async def test_client_gives_up_after_single_retry():
    posts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/tokens":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
        posts.append(1)
        return httpx.Response(401, json={"error": "unauthorized"})

    client = ZendeskClient(
        base_url="https://testsub.zendesk.com",
        timeout=5,
        token_provider=_provider(handler),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(UpstreamError):
        await client.create_ticket(
            subject="s", comment="c", external_id="e", idempotency_key="k"
        )
    assert len(posts) == 2


async def test_token_error_does_not_leak_secret(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    provider = _provider(handler)
    with pytest.raises(UpstreamError) as exc:
        await provider.get_token()
    assert "s3cr3t-value" not in str(exc.value)
    assert "s3cr3t-value" not in repr(provider)
    assert "s3cr3t-value" not in caplog.text
