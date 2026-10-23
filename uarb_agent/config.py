"""Settings from the environment, with .env as a fallback. Nothing here is required to run tests."""

from __future__ import annotations

import datetime as dt
import os
from functools import cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr


class Settings(BaseModel):
    # mail
    agent_email: str | None = None
    agent_app_password: SecretStr | None = None
    owner_email: str | None = None
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    # Gmail API sending (set by scripts/gmail_auth.py); when present it replaces SMTP for outgoing mail.
    gmail_client_id: str | None = None
    gmail_client_secret: SecretStr | None = None
    gmail_refresh_token: SecretStr | None = None
    # When the refresh token was issued. Apps in Google's "Testing" status get tokens that expire after 7 days,
    # so serve emails OWNER_EMAIL a day ahead.
    gmail_token_issued: dt.date | None = None
    # Self-sent mail carrying this value in X-Agent-Canary is answered instead of ignored (for test runs).
    canary_token: SecretStr | None = None

    # llm (optional fallback parser)
    llm_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-haiku-5-5"

    # site
    uarb_url: str = "https://uarb.novascotia.ca/fmi/webd/UARB15"
    uarb_concurrency: int = 2
    uarb_session_gap_s: float = 2.0
    request_timeout_s: float = 120.0

    # output
    data_dir: Path = Path("data")
    max_docs: int = 10
    # Raw bytes per reply. Base64 adds ~37%, so 16 MiB encodes to ~24 MB, under Gmail's 25 MB message limit.
    zip_budget_mb: int = 16

    # ops: healthchecks.io ping URL (optional; pings are skipped when unset)
    healthcheck_url: str | None = None

    @property
    def traces_dir(self) -> Path:
        return self.data_dir / "traces"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def gmail_api_ready(self) -> bool:
        return bool(self.gmail_client_id and self.gmail_client_secret and self.gmail_refresh_token)

    @property
    def llm_active(self) -> bool:
        return self.llm_enabled and self.anthropic_api_key is not None


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from `env` (defaults to os.environ after loading .env). Unset or empty vars keep defaults."""
    if env is None:
        load_dotenv(override=False)
        env = dict(os.environ)
    values = {name: env[name.upper()] for name in Settings.model_fields if env.get(name.upper(), "") != ""}
    return Settings.model_validate(values)


@cache
def get_settings() -> Settings:
    return load_settings()
