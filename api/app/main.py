"""Application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.router import api_router
from app.clients import SlackClient, ZendeskClient
from app.core.config import get_settings
from app.db.base import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # LOG_LEVEL is honored here; logging is configured before settings load,
    # so apply the configured level once it is known.
    logging.getLogger().setLevel(settings.log_level.upper())

    # Ensure the SQLite directory exists before create_all.
    db_path = settings.database_url.split("///")[-1]
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    await init_db()

    app.state.zendesk_client = ZendeskClient(
        base_url=settings.zendesk_base_url,
        timeout=settings.http_timeout_seconds,
        oauth_token=(
            settings.zendesk_oauth_token.get_secret_value()
            if settings.zendesk_oauth_token
            else None
        ),
        email=settings.zendesk_email,
        api_token=(
            settings.zendesk_api_token.get_secret_value()
            if settings.zendesk_api_token
            else None
        ),
    )
    app.state.slack_client = SlackClient(
        token=settings.slack_bot_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
    )

    logger.info("%s started (environment=%s)", settings.app_name, settings.environment)
    try:
        yield
    finally:
        await app.state.zendesk_client.aclose()
        await app.state.slack_client.aclose()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Zendesk <-> Slack Bridge",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(api_router)
    return application


app = create_app()