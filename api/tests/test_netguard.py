"""The session-wide network guard must actually block outbound connections.

This is a self-check: if the guard in conftest.py ever stops working, tests that
depend on it would silently start reaching the internet.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import NetworkAccessDenied


def _contains_guard(exc: BaseException) -> bool:
    """anyio wraps the guard error in an ExceptionGroup."""
    if isinstance(exc, NetworkAccessDenied):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_guard(inner) for inner in exc.exceptions)
    return False


@pytest.mark.parametrize(
    "target", ["http://127.0.0.1:1/x", "https://api.slack.com/api/reactions.add"]
)
async def test_outbound_connection_is_blocked(target):
    with pytest.raises(BaseException) as excinfo:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.get(target)
    assert _contains_guard(excinfo.value), (
        f"expected NetworkAccessDenied, got {type(excinfo.value).__name__}"
    )


async def test_mock_transport_is_unaffected():
    """Sanity check: the guard must not break httpx.MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await client.get("https://slack.test/api/test")
    assert response.json() == {"ok": True}

def test_developer_dotenv_is_ignored():
    """Regression: a real .env on disk used to leak into test settings."""
    from app.core.config import Settings, get_settings

    s = Settings()
    assert s.slack_channel_allowlist == []
    assert s.shared_token_enabled is False
    assert s.database_url == "sqlite+aiosqlite:///:memory:"
    assert get_settings().database_url == "sqlite+aiosqlite:///:memory:"
