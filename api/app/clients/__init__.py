"""Thin async HTTP clients for the Zendesk and Slack Web APIs."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from typing import Any

import httpx

# Refresh a client-credentials token when less than this many seconds remain.
TOKEN_REFRESH_LEEWAY_SECONDS = 60


def _basic_auth(email: str, api_token: str) -> str:
    """Build the base64 payload for legacy Zendesk email/token auth."""
    encoded = base64.b64encode(f"{email}/token:{api_token}".encode()).decode()
    return encoded


class UpstreamError(Exception):
    """An upstream API call failed or returned an unexpected payload."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ZendeskTokenProvider:
    """Fetches and caches OAuth access tokens via the client_credentials grant.

    Tokens expire and come without a refresh token, so we simply request a new
    one shortly before expiry. A lock prevents concurrent callers from each
    requesting a token. Neither the client secret nor a token is ever logged or
    included in an error message.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        scopes: str,
        ttl_seconds: int,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/oauth/tokens"
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._ttl = ttl_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"ZendeskTokenProvider(client_id={self._client_id!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _fresh(self) -> bool:
        return (
            self._token is not None
            and self._clock() < self._expires_at - TOKEN_REFRESH_LEEWAY_SECONDS
        )

    async def get_token(self) -> str:
        """Return a valid access token, fetching a new one when needed."""
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            # Re-check: another task may have refreshed while we waited.
            if not self._fresh():
                await self._fetch()
            assert self._token is not None
            return self._token

    def invalidate(self, stale_token: str) -> None:
        """Drop the cache if it still holds the token Zendesk rejected."""
        if self._token == stale_token:
            self._token = None
            self._expires_at = 0.0

    async def _fetch(self) -> None:
        body = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scopes,
            "expires_in": self._ttl,
        }
        try:
            response = await self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Zendesk token request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise UpstreamError(
                f"Zendesk token request rejected ({response.status_code})",
                status_code=response.status_code,
            )
        try:
            data = response.json()
            token = data["access_token"]
            expires_in = float(data.get("expires_in", self._ttl))
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("Zendesk token response malformed") from None
        if not isinstance(token, str) or not token:
            raise UpstreamError("Zendesk token response malformed")
        self._token = token
        self._expires_at = self._clock() + expires_in


class ZendeskClient:
    """Creates Zendesk tickets via the v2 API."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        oauth_token: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        token_provider: ZendeskTokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        if token_provider:
            # Client credentials: the bearer header is set per request.
            self._client.headers["Content-Type"] = "application/json"
        elif oauth_token:
            # Static OAuth access token.
            self._client.headers.update(
                {
                    "Authorization": f"Bearer {oauth_token}",
                    "Content-Type": "application/json",
                }
            )
        elif email and api_token:
            # DEPRECATED legacy API token auth (Zendesk is retiring API tokens).
            self._client.headers.update(
                {
                    "Authorization": f"Basic {_basic_auth(email, api_token)}",
                    "Content-Type": "application/json",
                }
            )
        else:  # pragma: no cover - guarded by settings validation
            raise ValueError("Zendesk auth requires OAuth credentials or email + API token")

    async def aclose(self) -> None:
        if self._token_provider is not None:
            await self._token_provider.aclose()
        if self._owns_client:
            await self._client.aclose()

    async def _post(
        self, url: str, payload: dict[str, Any], idempotency_key: str
    ) -> httpx.Response:
        """POST with auth; on 401 with a token provider, refresh and retry once."""
        headers = {"Idempotency-Key": idempotency_key}
        provider = self._token_provider
        if provider is None:
            return await self._client.post(url, json=payload, headers=headers)
        token = await provider.get_token()
        response = await self._client.post(
            url, json=payload, headers={**headers, "Authorization": f"Bearer {token}"}
        )
        if response.status_code == 401:
            provider.invalidate(token)
            token = await provider.get_token()
            response = await self._client.post(
                url,
                json=payload,
                headers={**headers, "Authorization": f"Bearer {token}"},
            )
        return response

    async def create_ticket(
        self,
        *,
        subject: str,
        comment: str,
        external_id: str,
        idempotency_key: str,
        tags: list[str] | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v2/tickets.json. Zendesk external_id is not unique, so we
        also send an Idempotency-Key and rely on our own dedupe ledger."""
        payload: dict[str, Any] = {
            "ticket": {
                "subject": subject,
                "comment": {"body": comment, "public": False},
                "external_id": external_id,
            }
        }
        if tags:
            payload["ticket"]["tags"] = tags
        if priority:
            payload["ticket"]["priority"] = priority

        url = f"{self._base_url}/api/v2/tickets.json"
        try:
            response = await self._post(url, payload, idempotency_key)
        except httpx.TimeoutException as exc:
            raise UpstreamError(f"Zendesk request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Zendesk request failed: {exc}") from exc

        return self._parse(response)

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            snippet = response.text[:200]
            raise UpstreamError(
                f"Zendesk returned non-JSON response: {snippet!r}",
                status_code=response.status_code,
            ) from None
        if response.status_code >= 400:
            raise UpstreamError(
                f"Zendesk API error {response.status_code}: {data}",
                status_code=response.status_code,
            )
        ticket = data.get("ticket") if isinstance(data, dict) else None
        if not isinstance(ticket, dict) or "id" not in ticket:
            raise UpstreamError("Zendesk response missing ticket payload")
        return ticket


class SlackClient:
    """Minimal Slack Web API wrapper (reactions only)."""

    def __init__(
        self,
        *,
        token: str,
        timeout: float,
        api_base_url: str = "https://slack.com/api",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._client.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def add_reaction(self, *, channel: str, name: str, timestamp: str) -> bool:
        """Call reactions.add. Already-reacted is treated as success.

        The request body is real JSON, matching the Content-Type header.

        Returns True when the reaction is present after the call.
        """
        try:
            response = await self._client.post(
                f"{self._api_base_url}/reactions.add",
                json={"channel": channel, "name": name, "timestamp": timestamp},
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError(f"Slack request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Slack request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError(
                f"Slack returned non-JSON response: {response.text[:200]!r}",
                status_code=response.status_code,
            ) from None

        if payload.get("ok"):
            return True
        error = payload.get("error", "unknown_error")
        if error == "already_reacted":
            return True
        raise UpstreamError(f"Slack reactions.add failed: {error}")