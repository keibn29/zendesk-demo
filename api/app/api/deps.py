"""Webhook signature verification dependencies."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from app.core.config import Settings
from app.core.security import verify_slack_signature, verify_zendesk_signature


async def _require_shared_token(request: Request, settings: Settings) -> None:
    """Optional defense-in-depth layer. The signature is still mandatory.

    The header name is read from configuration at request time, so a custom
    WEBHOOK_SHARED_TOKEN_HEADER is honored without redeploying code.
    """
    if not settings.shared_token_enabled:
        return
    expected = settings.webhook_shared_token
    assert expected is not None  # guaranteed by shared_token_enabled
    provided = request.headers.get(settings.webhook_shared_token_header)
    if provided is None or not secrets.compare_digest(
        expected.get_secret_value(), provided
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token"
        )


async def verify_slack_request(request: Request, settings: Settings) -> None:
    """Validate the Slack request signature against the exact raw body."""
    await _require_shared_token(request, settings)
    if settings.dev_disable_signature_verification:
        return
    body = await request.body()
    signature = request.headers.get("X-Slack-Signature")
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    if not verify_slack_signature(
        settings.slack_signing_secret.get_secret_value(),
        timestamp,
        body,
        signature,
        settings.signature_tolerance_seconds,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Slack signature"
        )


async def verify_zendesk_request(request: Request, settings: Settings) -> None:
    """Validate the Zendesk signature against the exact raw body."""
    await _require_shared_token(request, settings)
    if settings.dev_disable_signature_verification:
        return
    body = await request.body()
    signature = request.headers.get("X-Zendesk-Webhook-Signature")
    timestamp = request.headers.get("X-Zendesk-Webhook-Signature-Timestamp")
    if not verify_zendesk_signature(
        settings.zendesk_webhook_secret.get_secret_value(),
        timestamp,
        body,
        signature,
        settings.signature_tolerance_seconds,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Zendesk signature"
        )