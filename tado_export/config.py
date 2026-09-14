"""Paths, endpoints and tunable defaults."""

from __future__ import annotations

import os
from pathlib import Path

# Public client id documented by tado for the OAuth2 device code flow.
# https://help.tado.com/en/articles/8565472-how-do-i-authenticate-to-access-the-rest-api
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
SCOPE = "offline_access"

DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API_BASE = "https://my.tado.com/api/v2"

# Two further hosts carry history that outlives the dayReport window: per-zone
# heating running times, and Energy IQ consumption / meter readings / tariffs.
MINDER_BASE = "https://minder.tado.com/v1"
EIQ_BASE = "https://energy-insights.tado.com/api"

# tado X (generation "LINE_X") homes report zero zones from the classic API —
# rooms replaced zones there, and only surface through this newer host.
HOPS_BASE = "https://hops.tado.com"

# Daily REST quota. 100/day without a subscription, 20_000/day with Auto-Assist
# or AI Assist. https://help.tado.com/en/articles/12165739-limitation-for-rest-api-usage
DEFAULT_DAILY_BUDGET = 20_000
FREE_TIER_DAILY_BUDGET = 100

# Access tokens live 10 minutes; renew a little early so long runs never trip.
TOKEN_REFRESH_MARGIN_S = 90

# Days newer than this are considered still in flux and get re-fetched even when
# a copy already exists locally (today is always partial, and tado backfills
# late-arriving measurements for a short while).
DEFAULT_REFRESH_WINDOW_DAYS = 2

USER_AGENT = "tado-export/0.1 (+https://github.com/kritsel/tado-openapispec-v2)"


def config_dir() -> Path:
    """Where the refresh token lives. Honours XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "tado-export"


DEFAULT_PROFILE = "default"


def token_path(profile: str = DEFAULT_PROFILE) -> Path:
    """Refresh token file for one tado account.

    Profiles let several accounts coexist: each keeps its own token, and all of
    them can sync into the same database because rows are keyed by home id.
    """
    override = os.environ.get("TADO_EXPORT_TOKEN")
    if override and profile == DEFAULT_PROFILE:
        return Path(override)
    legacy = config_dir() / "token.json"
    if profile == DEFAULT_PROFILE and legacy.exists():
        return legacy
    return config_dir() / f"token-{profile}.json"


def known_profiles() -> list[str]:
    root = config_dir()
    if not root.is_dir():
        return []
    found = {p.stem.removeprefix("token-") for p in root.glob("token-*.json")}
    if (root / "token.json").exists():
        found.add(DEFAULT_PROFILE)
    return sorted(found)


def default_db_path() -> Path:
    return Path(os.environ.get("TADO_EXPORT_DB", "tado.db")).expanduser()
