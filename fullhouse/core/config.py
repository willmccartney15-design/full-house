"""Runtime settings, read from the environment (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = PACKAGE_DIR / "sample"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("FULLHOUSE_DATA_DIR", ".fullhouse")))
    claude_model: str = field(default_factory=lambda: os.getenv("FULLHOUSE_CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: os.getenv("FULLHOUSE_CLAUDE_EFFORT", "medium"))
    claude_fallbacks: bool = field(default_factory=lambda: _bool("FULLHOUSE_CLAUDE_FALLBACKS", True))
    claude_max_retries: int = field(default_factory=lambda: int(os.getenv("FULLHOUSE_CLAUDE_MAX_RETRIES", "4")))
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "FULLHOUSE_USER_AGENT", "FullHouseAuditBot/0.1 (+revenue audit; respects robots.txt)"
        )
    )
    min_seconds_between_requests: float = field(
        default_factory=lambda: float(os.getenv("FULLHOUSE_MIN_SECONDS_BETWEEN_REQUESTS", "5"))
    )
    cache_ttl_hours: float = field(default_factory=lambda: float(os.getenv("FULLHOUSE_CACHE_TTL_HOURS", "24")))
    chromium_path: str | None = field(default_factory=lambda: os.getenv("FULLHOUSE_CHROMIUM_PATH") or None)
    timezone: str = field(default_factory=lambda: os.getenv("FULLHOUSE_TIMEZONE", "Australia/Sydney"))
    currency: str = field(default_factory=lambda: os.getenv("FULLHOUSE_CURRENCY", "AUD"))
    direct_rate_buffer_pct: float = field(
        default_factory=lambda: float(os.getenv("FULLHOUSE_DIRECT_RATE_BUFFER_PCT", "5"))
    )
    google_places_api_key: str | None = field(default_factory=lambda: os.getenv("GOOGLE_PLACES_API_KEY") or None)
    gbp_access_token: str | None = field(default_factory=lambda: os.getenv("GBP_ACCESS_TOKEN") or None)
    # Email (SMTP). Without SMTP_HOST, messages are written to the outbox folder instead of sent.
    smtp_host: str | None = field(default_factory=lambda: os.getenv("SMTP_HOST") or None)
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str | None = field(default_factory=lambda: os.getenv("SMTP_USER") or None)
    smtp_password: str | None = field(default_factory=lambda: os.getenv("SMTP_PASSWORD") or None)
    smtp_ssl: bool = field(default_factory=lambda: _bool("SMTP_SSL", False))  # True = port 465 SSL, else STARTTLS
    mail_from: str = field(default_factory=lambda: os.getenv("MAIL_FROM", "reports@fullhouse.local"))
    mail_from_name: str = field(default_factory=lambda: os.getenv("MAIL_FROM_NAME", "Full House"))
    # Sender identity printed in every outreach email (the Spam Act requires who is sending and how to reach them).
    sender_name: str = field(default_factory=lambda: os.getenv("FULLHOUSE_SENDER_NAME", "Will"))
    sender_business: str = field(default_factory=lambda: os.getenv("FULLHOUSE_SENDER_BUSINESS", "Full House"))
    sender_contact: str = field(default_factory=lambda: os.getenv("FULLHOUSE_SENDER_CONTACT", ""))
    # Public address of the approvals web app (`fullhouse approvals serve`), used in email links.
    approval_base_url: str = field(default_factory=lambda: os.getenv("FULLHOUSE_APPROVAL_BASE_URL",
                                                                     "http://localhost:8000").rstrip("/"))
    proposal_ttl_days: int = field(default_factory=lambda: int(os.getenv("FULLHOUSE_PROPOSAL_TTL_DAYS", "14")))

    @property
    def db_url(self) -> str:
        return os.getenv("FULLHOUSE_DB_URL", f"sqlite:///{self.data_dir / 'fullhouse.db'}")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cache_dir, self.output_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides) -> Settings:
    load_dotenv()
    settings = Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.ensure_dirs()
    return settings
