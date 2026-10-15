"""Application settings, loaded from environment / .env."""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# A single safe DNS label: letters/digits/hyphen, not starting or ending with a
# hyphen. Rejects host traversal, URLs, ports and whitespace in the subdomain.
SUBDOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


_STATUS_ALIASES = {"on-hold": "hold", "on hold": "hold", "onhold": "hold", "on_hold": "hold"}


def normalize_status(status: str) -> str:
    """Normalize a Zendesk status label to a reaction-map key.

    Lowercases, strips, and maps the "On-hold" spellings to `hold`. Unknown
    values pass through unchanged (they simply have no reaction).
    """
    key = status.strip().lower()
    return _STATUS_ALIASES.get(key, key)


class Settings(BaseSettings):
    """All configuration for the service. Secrets are never logged."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- App ---
    app_name: str = "zendesk-support-bridge"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/bridge.sqlite3"

    # --- Slack ---
    slack_signing_secret: SecretStr
    slack_bot_token: SecretStr
    # Optional allowlist. Empty == accept any channel the app is in.
    # NoDecode keeps pydantic-settings from trying to JSON-parse the raw env
    # string, so a plain "C1,C2" (or an empty value) works from .env.
    slack_channel_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Zendesk ---
    zendesk_subdomain: str
    zendesk_webhook_secret: SecretStr
    # Auth precedence: client credentials (id + secret) > static OAuth token >
    # legacy email + API token (DEPRECATED: Zendesk is retiring API tokens).
    zendesk_oauth_client_id: str | None = None
    zendesk_oauth_client_secret: SecretStr | None = None
    # Scope names are configurable because they are not verified here.
    zendesk_oauth_scopes: str = "tickets:read tickets:write"
    # Requested access-token lifetime in seconds (clamped to 300..172800).
    zendesk_oauth_token_ttl: int = 3600
    zendesk_oauth_token: SecretStr | None = None
    zendesk_email: str | None = None
    zendesk_api_token: SecretStr | None = None

    # --- Security: defense in depth ---
    # Optional shared token checked in addition to (not instead of) signatures.
    # An empty value disables this layer.
    webhook_shared_token: SecretStr | None = None
    webhook_shared_token_header: str = "X-Webhook-Token"
    # DEV ONLY. When true, signature verification is skipped. Refused in production.
    dev_disable_signature_verification: bool = False
    environment: str = "production"

    # --- Behavior ---
    # Slack requires an ACK within 3s; ticket creation runs in the background.
    signature_tolerance_seconds: int = Field(default=300, ge=0, le=86400)
    # Per-phase HTTP timeout handed to httpx (connect/read/write/pool each).
    http_timeout_seconds: float = 10.0
    # TOTAL wall-clock budget for one upstream service call, enforced with
    # asyncio.timeout around the whole call. httpx's per-phase timeout alone can
    # let a single request run for several times http_timeout_seconds.
    processing_deadline_seconds: float = 60.0
    # A claim left in 'processing' this long is treated as abandoned and becomes
    # claimable again. Must exceed the total processing deadline by a safety
    # margin, otherwise a slow-but-alive upstream call would have its lease
    # stolen mid-flight and two workers would call Zendesk for the same message.
    processing_claim_timeout_seconds: int = Field(default=900, ge=30, le=86400)
    # Safety margin between the total deadline and the lease.
    claim_timeout_margin_seconds: int = Field(default=60, ge=0, le=3600)

    # --- Reaction defaults (env-overridable) ---
    reaction_new: str = "eyes"
    reaction_open: str = "eyes"
    reaction_pending: str = "hourglass_flowing_sand"
    reaction_hold: str = "hourglass_flowing_sand"
    reaction_solved: str = "white_check_mark"
    reaction_closed: str = "white_check_mark"

    @field_validator("slack_channel_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, value: object) -> object:
        """Accept a comma-separated string (the form used in .env)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "slack_signing_secret",
        "slack_bot_token",
        "zendesk_webhook_secret",
        "webhook_shared_token",
        "zendesk_oauth_token",
        "zendesk_api_token",
        "zendesk_oauth_client_secret",
        mode="before",
    )
    @classmethod
    def _blank_secret_to_none(cls, value: object) -> object:
        """An empty env value means 'unset', not an empty secret."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("zendesk_oauth_client_id", mode="before")
    @classmethod
    def _blank_client_id_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("zendesk_oauth_token_ttl")
    @classmethod
    def _clamp_token_ttl(cls, value: int) -> int:
        return max(300, min(172800, value))

    @field_validator("http_timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be a positive, finite number")
        return value

    @field_validator("processing_deadline_seconds")
    @classmethod
    def _positive_deadline(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                "PROCESSING_DEADLINE_SECONDS must be a positive, finite number"
            )
        return value

    @field_validator("webhook_shared_token_header")
    @classmethod
    def _valid_header_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("WEBHOOK_SHARED_TOKEN_HEADER must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        # Required secrets must be present and non-empty.
        for name in ("slack_signing_secret", "slack_bot_token", "zendesk_webhook_secret"):
            secret: SecretStr | None = getattr(self, name)
            if secret is None or not secret.get_secret_value().strip():
                raise ValueError(f"{name.upper()} must be set and non-empty.")

        if not SUBDOMAIN_RE.match(self.zendesk_subdomain):
            raise ValueError(
                "ZENDESK_SUBDOMAIN must be a single safe DNS label "
                "(letters, digits, hyphens; not starting/ending with a hyphen)."
            )

        if self.dev_disable_signature_verification and self.environment != "development":
            raise ValueError(
                "dev_disable_signature_verification is only allowed when "
                "ENVIRONMENT=development. Refusing to start."
            )

        # A lease shorter than the total upstream deadline lets a second worker
        # steal the claim while the first is still calling Zendesk, which would
        # create a duplicate ticket. Enforce rather than trust configuration.
        if (
            self.processing_deadline_seconds + self.claim_timeout_margin_seconds
            > self.processing_claim_timeout_seconds
        ):
            raise ValueError(
                "PROCESSING_DEADLINE_SECONDS "
                f"({self.processing_deadline_seconds:g}) plus "
                "CLAIM_TIMEOUT_MARGIN_SECONDS "
                f"({self.claim_timeout_margin_seconds}) must not exceed "
                "PROCESSING_CLAIM_TIMEOUT_SECONDS "
                f"({self.processing_claim_timeout_seconds})."
            )
        if bool(self.zendesk_oauth_client_id) != bool(self.zendesk_oauth_client_secret):
            raise ValueError(
                "ZENDESK_OAUTH_CLIENT_ID and ZENDESK_OAUTH_CLIENT_SECRET "
                "must be set together."
            )
        if (
            not self.uses_client_credentials
            and not self.zendesk_oauth_token
            and not (self.zendesk_email and self.zendesk_api_token)
        ):
            raise ValueError(
                "Zendesk auth requires ZENDESK_OAUTH_CLIENT_ID + "
                "ZENDESK_OAUTH_CLIENT_SECRET (recommended), or "
                "ZENDESK_OAUTH_TOKEN, or legacy ZENDESK_EMAIL + ZENDESK_API_TOKEN."
            )
        return self

    @property
    def zendesk_base_url(self) -> str:
        return f"https://{self.zendesk_subdomain}.zendesk.com"

    @property
    def uses_client_credentials(self) -> bool:
        return bool(self.zendesk_oauth_client_id and self.zendesk_oauth_client_secret)

    @property
    def min_claim_timeout_seconds(self) -> float:
        """Smallest safe lease: longer than any single upstream operation."""
        return self.processing_deadline_seconds + self.claim_timeout_margin_seconds

    @property
    def shared_token_enabled(self) -> bool:
        """Empty WEBHOOK_SHARED_TOKEN disables the shared-token layer."""
        return bool(
            self.webhook_shared_token
            and self.webhook_shared_token.get_secret_value().strip()
        )

    def reaction_map(self) -> dict[str, str]:
        """Single source of truth for status -> reaction name."""
        return {
            "new": self.reaction_new,
            "open": self.reaction_open,
            "pending": self.reaction_pending,
            "hold": self.reaction_hold,
            "solved": self.reaction_solved,
            "closed": self.reaction_closed,
        }

    def reaction_for_status(self, status: str) -> str:
        """Map a Zendesk status / event action to a Slack reaction name."""
        return self.reaction_map().get(normalize_status(status), "")


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (used as a FastAPI dependency)."""
    return Settings()  # type: ignore[call-arg]