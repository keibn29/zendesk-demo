"""Settings validation and env/dotenv loading."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


# --- Loading from the real environment (not just constructor kwargs) ---


def test_allowlist_loads_comma_separated_from_env(monkeypatch, settings_kwargs):
    monkeypatch.setenv("SLACK_CHANNEL_ALLOWLIST", "C1,C2,C3")
    assert Settings(**settings_kwargs).slack_channel_allowlist == ["C1", "C2", "C3"]


def test_allowlist_loads_empty_from_env(monkeypatch, settings_kwargs):
    """Regression: an empty value (as shipped in .env.example) crashed startup."""
    monkeypatch.setenv("SLACK_CHANNEL_ALLOWLIST", "")
    assert Settings(**settings_kwargs).slack_channel_allowlist == []


def test_allowlist_trims_whitespace(monkeypatch, settings_kwargs):
    monkeypatch.setenv("SLACK_CHANNEL_ALLOWLIST", " C1 , C2 ")
    assert Settings(**settings_kwargs).slack_channel_allowlist == ["C1", "C2"]


def test_allowlist_loads_from_dotenv(tmp_path, monkeypatch, settings_kwargs):
    """Verify the .env file path, not only process env."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in settings_kwargs.items() if isinstance(v, str))
        + "\nSLACK_CHANNEL_ALLOWLIST=C9,C8\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SLACK_CHANNEL_ALLOWLIST", raising=False)
    loaded = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert loaded.slack_channel_allowlist == ["C9", "C8"]


def test_allowlist_empty_in_dotenv(tmp_path, monkeypatch, settings_kwargs):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in settings_kwargs.items() if isinstance(v, str))
        + "\nSLACK_CHANNEL_ALLOWLIST=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SLACK_CHANNEL_ALLOWLIST", raising=False)
    loaded = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert loaded.slack_channel_allowlist == []


# --- Shared token: empty disables, header name is honored ---


def test_empty_shared_token_disables_layer(monkeypatch, settings_kwargs):
    """Regression: empty used to become SecretStr(''), rejecting everything."""
    monkeypatch.setenv("WEBHOOK_SHARED_TOKEN", "")
    s = Settings(**settings_kwargs)
    assert s.webhook_shared_token is None
    assert s.shared_token_enabled is False


def test_set_shared_token_enables_layer(make_settings):
    s = make_settings(webhook_shared_token="s3cret")
    assert s.shared_token_enabled is True


def test_shared_token_header_configurable(monkeypatch, settings_kwargs):
    monkeypatch.setenv("WEBHOOK_SHARED_TOKEN_HEADER", "X-Custom-Guard")
    assert Settings(**settings_kwargs).webhook_shared_token_header == "X-Custom-Guard"


def test_empty_header_name_rejected(make_settings):
    with pytest.raises(ValidationError):
        make_settings(webhook_shared_token_header="   ")


# --- Required secrets / credentials ---


@pytest.mark.parametrize(
    "field",
    ["slack_signing_secret", "slack_bot_token", "zendesk_webhook_secret"],
)
def test_required_secret_rejects_empty(make_settings, field):
    with pytest.raises(ValidationError):
        make_settings(**{field: ""})


@pytest.mark.parametrize(
    "field",
    ["slack_signing_secret", "slack_bot_token", "zendesk_webhook_secret"],
)
def test_required_secret_rejects_whitespace(make_settings, field):
    with pytest.raises(ValidationError):
        make_settings(**{field: "   "})


def test_missing_zendesk_auth_rejected(make_settings):
    with pytest.raises(ValidationError, match="Zendesk auth"):
        make_settings(zendesk_oauth_token=None, zendesk_email=None, zendesk_api_token=None)


def test_legacy_email_token_is_valid(make_settings):
    s = make_settings(zendesk_oauth_token=None, zendesk_email="a@b.c", zendesk_api_token="tok")
    assert s.zendesk_api_token is not None


@pytest.mark.parametrize(
    "subdomain",
    [
        "https://evil.com/x",
        "acme.zendesk.com",
        "acme/sub",
        "-acme",
        "acme-",
        "ac me",
        "",
        "acme:443",
        "ac\nme",
    ],
)
def test_unsafe_subdomain_rejected(make_settings, subdomain):
    with pytest.raises(ValidationError, match="SUBDOMAIN|DNS"):
        make_settings(zendesk_subdomain=subdomain)


@pytest.mark.parametrize("subdomain", ["acme", "a", "acme-co", "ACME123", "a1"])
def test_valid_subdomain_accepted(make_settings, subdomain):
    assert make_settings(zendesk_subdomain=subdomain).zendesk_subdomain == subdomain


# --- Numeric bounds ---


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_non_positive_or_non_finite_timeout_rejected(make_settings, value):
    with pytest.raises(ValidationError):
        make_settings(http_timeout_seconds=value)


def test_positive_timeout_accepted(make_settings):
    assert make_settings(http_timeout_seconds=2.5).http_timeout_seconds == 2.5


@pytest.mark.parametrize("value", [-1, -300])
def test_negative_signature_tolerance_rejected(make_settings, value):
    with pytest.raises(ValidationError):
        make_settings(signature_tolerance_seconds=value)


def test_signature_tolerance_upper_bound_enforced(make_settings):
    with pytest.raises(ValidationError):
        make_settings(signature_tolerance_seconds=999_999)


def test_zero_signature_tolerance_allowed(make_settings):
    assert make_settings(signature_tolerance_seconds=0).signature_tolerance_seconds == 0


# --- Dev-only bypass ---


def test_dev_bypass_refused_in_production(make_settings):
    with pytest.raises(ValidationError, match="only allowed when"):
        make_settings(
            zendesk_oauth_token="oauth",
            environment="production",
            dev_disable_signature_verification=True,
        )


def test_dev_bypass_allowed_in_development(make_settings):
    s = make_settings(
        zendesk_oauth_token="oauth",
        environment="development",
        dev_disable_signature_verification=True,
    )
    assert s.dev_disable_signature_verification is True


def test_dev_bypass_refused_in_test_environment(make_settings):
    with pytest.raises(ValidationError):
        make_settings(
            zendesk_oauth_token="oauth",
            environment="test",
            dev_disable_signature_verification=True,
        )


# --- Reaction map ---


def test_reaction_map_defaults(make_settings):
    s = make_settings()
    assert s.reaction_for_status("new") == "eyes"
    assert s.reaction_for_status("open") == "eyes"
    assert s.reaction_for_status("pending") == "hourglass_flowing_sand"
    assert s.reaction_for_status("hold") == "hourglass_flowing_sand"
    assert s.reaction_for_status("solved") == "white_check_mark"
    assert s.reaction_for_status("closed") == "white_check_mark"
    assert s.reaction_for_status("SOLVED") == "white_check_mark"


def test_reaction_map_is_single_source(make_settings):
    s = make_settings(reaction_solved="tada")
    assert s.reaction_map()["solved"] == "tada"
    assert s.reaction_for_status("solved") == "tada"


def test_secrets_masked_in_repr(make_settings):
    s = make_settings(zendesk_oauth_token="super-secret-token")
    assert "super-secret-token" not in repr(s)
    assert "super-secret-token" not in str(s.zendesk_oauth_token)

# --- Lease must outlive any single upstream call ---


def test_default_lease_exceeds_total_deadline(make_settings):
    s = make_settings()
    assert s.processing_claim_timeout_seconds > s.processing_deadline_seconds
    assert s.processing_claim_timeout_seconds >= s.min_claim_timeout_seconds


def test_lease_shorter_than_deadline_rejected(make_settings):
    """A lease shorter than the total deadline lets a second worker steal it."""
    with pytest.raises(ValidationError, match="PROCESSING_DEADLINE_SECONDS"):
        make_settings(processing_deadline_seconds=120.0, processing_claim_timeout_seconds=120)


def test_lease_below_deadline_plus_margin_rejected(make_settings):
    with pytest.raises(ValidationError, match="PROCESSING_DEADLINE_SECONDS"):
        make_settings(
            processing_deadline_seconds=120.0,
            claim_timeout_margin_seconds=60,
            processing_claim_timeout_seconds=179,
        )


def test_lease_exactly_deadline_plus_margin_allowed(make_settings):
    """Boundary is inclusive: the margin is exactly the required cushion."""
    s = make_settings(
        processing_deadline_seconds=120.0,
        claim_timeout_margin_seconds=60,
        processing_claim_timeout_seconds=180,
    )
    assert s.processing_claim_timeout_seconds == 180


def test_lease_above_deadline_plus_margin_accepted(make_settings):
    s = make_settings(
        processing_deadline_seconds=120.0,
        claim_timeout_margin_seconds=60,
        processing_claim_timeout_seconds=200,
    )
    assert s.min_claim_timeout_seconds == 180.0


def test_min_claim_timeout_property(make_settings):
    s = make_settings(processing_deadline_seconds=30.0, claim_timeout_margin_seconds=60)
    assert s.min_claim_timeout_seconds == 90.0


def test_default_deadline_is_below_lease(make_settings):
    s = make_settings()
    assert s.processing_deadline_seconds < s.processing_claim_timeout_seconds


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_non_positive_or_non_finite_deadline_rejected(make_settings, value):
    with pytest.raises(ValidationError):
        make_settings(processing_deadline_seconds=value)
