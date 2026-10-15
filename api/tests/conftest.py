"""Shared pytest fixtures: isolated settings, DB, and fake upstream clients.

These are fixtures only. Test cases themselves live in collected test modules
so pytest actually reports them.

Isolation guarantees:
  - every test gets a fresh in-memory SQLite DB (create_all per test)
  - no real credentials: env defaults below are test-only placeholders
  - no network: upstream clients are fakes or httpx.MockTransport
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Test isolation
#
# These tests must never read a developer's real .env, never inherit real
# credentials, and never open a network connection. We enforce that by:
#   1. pinning every setting the app reads to a test value (not setdefault),
#   2. pointing DATABASE_URL at a throwaway in-memory DB,
#   3. blocking outbound sockets for the whole session (see _no_network below).
# ---------------------------------------------------------------------------

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "SLACK_SIGNING_SECRET": "test-slack-signing-secret",
    "SLACK_BOT_TOKEN": "xoxb-test-token",
    "ZENDESK_SUBDOMAIN": "testsub",
    "ZENDESK_WEBHOOK_SECRET": "test-zendesk-webhook-secret",
    "ZENDESK_OAUTH_TOKEN": "test-zendesk-oauth-token",
    # Never point at the developer's real database file.
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    # Neutralize behavior toggles a developer might have exported.
    "SLACK_CHANNEL_ALLOWLIST": "",
    "WEBHOOK_SHARED_TOKEN": "",
    "WEBHOOK_SHARED_TOKEN_HEADER": "X-Webhook-Token",
    "ZENDESK_EMAIL": "",
    "ZENDESK_API_TOKEN": "",
    "DEV_DISABLE_SIGNATURE_VERIFICATION": "false",
}
# Hard assignment (not setdefault): an inherited value must never win.
os.environ.update(_TEST_ENV)

# Settings caches; drop any cached instance built before this point.
from app.core.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()

# Neutralize the Settings.model_config env_file so a developer's .env on disk
# is never read during tests. Tests that need a .env pass _env_file explicitly.
Settings.model_config["env_file"] = None

import httpx  # noqa: E402
from httpx import AsyncClient, Response  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.clients import SlackClient, ZendeskClient  # noqa: E402
from app.db.base import Base  # noqa: E402

_real_socket_connect = socket.socket.connect


class NetworkAccessDenied(BaseException):
    """Raised when a test opens a real socket.

    Derives from BaseException, NOT Exception, on purpose: application code has
    broad `except Exception` recovery paths that would otherwise swallow this and
    turn a test bug into a silent pass.
    """


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    """Fail loudly if any test tries to open an outbound TCP connection.

    httpx.MockTransport and the ASGI transport never reach this, so any hit is a
    test bug that would otherwise depend on the internet.
    """

    def guard(self, address, *args, **kwargs):
        raise NetworkAccessDenied(
            f"Network access attempted in tests: {address!r}. "
            "Use httpx.MockTransport or a fake client instead."
        )

    socket.socket.connect = guard
    try:
        yield
    finally:
        socket.socket.connect = _real_socket_connect


@pytest.fixture
def settings_kwargs() -> dict[str, Any]:
    """Valid base kwargs for constructing Settings in tests."""
    return {
        "slack_signing_secret": "test-slack-signing-secret",
        "slack_bot_token": "xoxb-test-token",
        "zendesk_subdomain": "testsub",
        "zendesk_webhook_secret": "test-zendesk-webhook-secret",
        "zendesk_oauth_token": "test-zendesk-oauth-token",
        "environment": "test",
    }


@pytest.fixture
def make_settings(settings_kwargs):
    """Factory: build Settings with overrides, with no dependence on real env."""

    def _make(**overrides: Any) -> Settings:
        return Settings(**{**settings_kwargs, **overrides})

    return _make


class FakeZendesk:
    """Records create_ticket calls; returns incrementing ticket ids."""

    def __init__(self, ticket_id: int = 4242) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = ticket_id
        self.error: Exception | None = None

    async def create_ticket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        ticket_id = self._next_id
        self._next_id += 1
        return {"id": ticket_id}

    async def aclose(self) -> None:
        """No-op so application lifespan teardown has nothing real to close."""


class FakeSlack:
    """Records add_reaction calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def add_reaction(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return True

    async def aclose(self) -> None:
        """No-op so application lifespan teardown has nothing real to close."""


@pytest.fixture
def fake_zendesk() -> FakeZendesk:
    return FakeZendesk()


@pytest.fixture
def fake_slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
async def db_factory():
    """A session factory over a shared in-memory-style DB, schema pre-created.

    Uses a shared temp file so that *separate* AsyncSession objects see the same
    data, which is what the lease-ownership tests need to simulate two workers.
    """
    tmpdir = tempfile.mkdtemp(prefix="zendesk-bridge-test-")
    db_path = os.path.join(tmpdir, "test.sqlite3")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
async def session(db_factory) -> AsyncSession:
    """Fresh, isolated DB per test."""
    async with db_factory() as s:
        yield s


@pytest.fixture
def ticket_repo(session: AsyncSession):
    from app.repositories import TicketMappingRepository

    return TicketMappingRepository(session)


@pytest.fixture
def event_repo(session: AsyncSession):
    from app.repositories import ProcessedEventRepository

    return ProcessedEventRepository(session)


def mock_transport(handler) -> httpx.MockTransport:
    """httpx transport that never touches the network."""
    return httpx.MockTransport(handler)


@pytest.fixture
def zendesk_client_factory():
    """Build a ZendeskClient wired to a MockTransport."""

    def _make(handler, **overrides: Any) -> ZendeskClient:
        kwargs: dict[str, Any] = {
            "base_url": "https://testsub.zendesk.com",
            "timeout": 5,
            "oauth_token": "test-zendesk-oauth-token",
            "client": AsyncClient(transport=mock_transport(handler)),
        }
        kwargs.update(overrides)
        return ZendeskClient(**kwargs)

    return _make


@pytest.fixture
def slack_client_factory():
    """Build a SlackClient wired to a MockTransport."""

    def _make(handler, **overrides: Any) -> SlackClient:
        kwargs: dict[str, Any] = {
            "token": "xoxb-test-token",
            "timeout": 5,
            "api_base_url": "https://slack.test/api",
            "client": AsyncClient(transport=mock_transport(handler)),
        }
        kwargs.update(overrides)
        return SlackClient(**kwargs)

    return _make


@pytest.fixture
def json_ok_response():
    """Helper building a JSON Response."""

    def _make(payload: Any, status_code: int = 200) -> Response:
        return Response(status_code, json=payload)

    return _make


@pytest.fixture
def client(db_factory, fake_zendesk, fake_slack, monkeypatch):
    """The single endpoint fixture: fake upstreams, isolated DB, no network.

    The application lifespan is patched so it never constructs a real
    ``httpx.AsyncClient`` (via ZendeskClient/SlackClient) and never touches the
    default ``DATABASE_URL`` engine. Teardown therefore has nothing real to close,
    and no fixture can leak a live connection.
    """
    import app.api.endpoints.slack as slack_endpoint
    import app.api.endpoints.zendesk as zendesk_endpoint
    import app.db.base as db_base
    import app.main as main_module
    from fastapi.testclient import TestClient

    test_engine = db_factory.kw["bind"]

    # Lifespan would otherwise build real HTTP clients and a real engine.
    monkeypatch.setattr(main_module, "ZendeskClient", lambda **kw: fake_zendesk)
    monkeypatch.setattr(main_module, "SlackClient", lambda **kw: fake_slack)
    # init_db() during startup targets this test engine instead of the default.
    monkeypatch.setattr(db_base, "engine", test_engine)
    # Background tasks open their own sessions.
    monkeypatch.setattr(db_base, "SessionFactory", db_factory)
    monkeypatch.setattr(slack_endpoint, "SessionFactory", db_factory)
    monkeypatch.setattr(zendesk_endpoint, "SessionFactory", db_factory)

    from app.main import app

    with TestClient(app) as c:
        assert c.app.state.zendesk_client is fake_zendesk
        assert c.app.state.slack_client is fake_slack
        yield c
