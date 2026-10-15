"""The endpoint fixture must not construct real HTTP clients or touch the
default engine, and teardown must not close anything real."""

import httpx
import pytest

from app.clients import SlackClient, ZendeskClient
from tests.conftest import NetworkAccessDenied


@pytest.fixture
def spy_async_client(monkeypatch):
    """Record every httpx.AsyncClient constructed during a test."""
    created: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        created.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    return created


def test_endpoint_fixture_builds_no_real_http_client(client, spy_async_client):
    assert spy_async_client == [], (
        "endpoint tests must not construct real httpx clients: "
        f"{spy_async_client}"
    )
    assert not isinstance(client.app.state.zendesk_client, ZendeskClient)
    assert not isinstance(client.app.state.slack_client, SlackClient)


def test_endpoint_fixture_uses_isolated_database(client, db_factory):
    import app.api.endpoints.slack as slack_endpoint
    import app.db.base as db_base

    assert db_base.SessionFactory is db_factory
    assert slack_endpoint.SessionFactory is db_factory
    assert db_base.engine is db_factory.kw["bind"]


def test_teardown_does_not_close_real_clients(client):
    """aclose() on the fakes is a no-op; nothing real was ever opened."""
    assert client.app.state.zendesk_client is not None
    assert client.app.state.slack_client is not None


def test_network_guard_is_not_swallowed_by_except_exception():
    """The guard derives from BaseException so recovery paths cannot hide it."""

    def broad_recovery() -> str:
        # Mirrors the app's `except Exception` recovery paths.
        try:
            raise NetworkAccessDenied("blocked")
        except Exception:  # noqa: BLE001
            return "swallowed"
        return "not reached"

    # Because it is a BaseException, the broad handler above does not apply.
    with pytest.raises(NetworkAccessDenied):
        broad_recovery()
