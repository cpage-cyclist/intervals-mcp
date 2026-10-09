"""Settings, read from environment variables.

Nothing secret lives in the code: the Intervals.icu key, the connector
password and the signing secret are all set in the host's environment
(Render dashboard -> Environment).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    intervals_api_key: str
    athlete_id: str
    connector_password: str
    token_secret: str
    public_url: str
    intervals_base_url: str = "https://intervals.icu/api/v1"
    # Hosts allowed as OAuth redirect targets (Claude's callback lives on these).
    allowed_redirect_hosts: tuple[str, ...] = field(
        default=("claude.ai", "claude.com", "localhost", "127.0.0.1")
    )
    access_token_ttl: int = 24 * 3600
    refresh_token_ttl: int = 180 * 24 * 3600

    @classmethod
    def from_env(cls) -> "Settings":
        public_url = (
            os.environ.get("PUBLIC_URL")
            or os.environ.get("RENDER_EXTERNAL_URL")  # set automatically by Render
            or "http://localhost:8000"
        ).rstrip("/")
        hosts = os.environ.get("ALLOWED_REDIRECT_HOSTS")
        extra = {}
        if hosts:
            extra["allowed_redirect_hosts"] = tuple(h.strip() for h in hosts.split(",") if h.strip())
        secret = _require("TOKEN_SECRET")
        if len(secret) < 32:
            raise RuntimeError("TOKEN_SECRET must be at least 32 characters")
        password = _require("CONNECTOR_PASSWORD")
        if len(password) < 12:
            raise RuntimeError("CONNECTOR_PASSWORD must be at least 12 characters")
        return cls(
            intervals_api_key=_require("INTERVALS_API_KEY"),
            athlete_id=_require("INTERVALS_ATHLETE_ID"),
            connector_password=password,
            token_secret=secret,
            public_url=public_url,
            **extra,
        )
