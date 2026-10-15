"""Thin async HTTP clients for the Zendesk and Slack Web APIs."""

from __future__ import annotations

import base64
from typing import Any

import httpx

def _basic_auth(email: str, api_token: str) -> str:
    """Build the base64 payload for legacy Zendesk email/token auth."""
    encoded = base64.b64encode(f"{email}/token:{api_token}".encode()).decode()
    return encoded


class UpstreamError(Exception):
    """An upstream API call failed or returned an unexpected payload."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        if oauth_token:
            # OAuth access token (preferred).
            self._client.headers.update(
                {
                    "Authorization": f"Bearer {oauth_token}",
                    "Content-Type": "application/json",
                }
            )
        elif email and api_token:
            # Legacy API token auth.
            self._client.headers.update(
                {
                    "Authorization": f"Basic {_basic_auth(email, api_token)}",
                    "Content-Type": "application/json",
                }
            )
        else:  # pragma: no cover - guarded by settings validation
            raise ValueError("Zendesk auth requires OAuth token or email + API token")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

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
            response = await self._client.post(
                url, json=payload, headers={"Idempotency-Key": idempotency_key}
            )
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