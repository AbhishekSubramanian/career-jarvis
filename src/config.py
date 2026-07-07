"""Configuration loading and validation for Career Jarvis.

All secrets come from .env via python-dotenv. We validate required values
at startup and raise a single, clear error listing everything missing so
the user gets one fix-list instead of a trickle of failures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Project root = parent of this file's parent (src/ -> career_jarvis/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_NOTIFY_CHANNELS = {"ntfy", "whatsapp", "pushover", "discord"}


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    # --- Required (no defaults) ---
    poll_minutes: int
    state_db_path: Path
    token_path: Path
    credentials_json_path: Path
    classifier_model: str
    drafter_model: str
    notify_channel: str

    # --- Optional (with defaults; must follow non-default fields) ---
    skip_senders: set = field(default_factory=set)
    llm_base_url: Optional[str] = None
    llm_timeout: int = 60
    drafter_max_tokens: int = 600
    ntfy_topic: Optional[str] = None
    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_token: Optional[str] = None
    whatsapp_phone: Optional[str] = None
    whatsapp_apikey: Optional[str] = None
    pushover_token: Optional[str] = None
    pushover_user: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    linkedin_enabled: bool = False
    linkedin_profile_dir: Path = field(default_factory=lambda: Path(".data/linkedin_profile"))
    linkedin_poll_hours: int = 6
    linkedin_max_threads: int = 20

    @property
    def state_db_absolute(self) -> Path:
        p = self.state_db_path
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def token_absolute(self) -> Path:
        p = self.token_path
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def credentials_absolute(self) -> Path:
        p = self.credentials_json_path
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def linkedin_profile_absolute(self) -> Path:
        p = self.linkedin_profile_dir
        return p if p.is_absolute() else PROJECT_ROOT / p


def _require(name: str, missing: list[str]) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        missing.append(name)
    return val


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(env_path: Optional[Path] = None) -> Config:
    """Load .env, validate, and return a frozen Config.

    Raises ConfigError with a consolidated list of problems.
    """
    if env_path is None:
        env_path = PROJECT_ROOT / ".env"
    # load_dotenv does not raise if the file is missing; it just no-ops.
    # We do NOT require .env to exist (env vars may be set in the shell).
    load_dotenv(dotenv_path=env_path)

    missing: list[str] = []
    problems: list[str] = []

    # --- App ---
    poll_raw = os.getenv("POLL_MINUTES", "5").strip()
    try:
        poll_minutes = int(poll_raw)
        if poll_minutes < 1:
            problems.append("POLL_MINUTES must be >= 1")
    except ValueError:
        problems.append(f"POLL_MINUTES must be an integer, got {poll_raw!r}")
        poll_minutes = 5

    state_db_path = _resolve_path(os.getenv("STATE_DB_PATH", ".data/career_jarvis.db"))
    token_path = _resolve_path(os.getenv("TOKEN_PATH", ".data/token.json"))
    creds_path = _resolve_path(os.getenv("GMAIL_CREDENTIALS_JSON", ".data/credentials.json"))

    skip_raw = os.getenv("SKIP_SENDERS", "").strip()
    skip_senders = {s.strip().lower() for s in skip_raw.split(",") if s.strip()}

    # --- LLM ---
    classifier_model = _require("CLASSIFIER_MODEL", missing)
    drafter_model = _require("DRAFTER_MODEL", missing)
    llm_base_url = os.getenv("LLM_BASE_URL", "").strip() or None
    llm_timeout_raw = os.getenv("LLM_TIMEOUT", "60").strip()
    try:
        llm_timeout = int(llm_timeout_raw)
    except ValueError:
        problems.append(f"LLM_TIMEOUT must be int, got {llm_timeout_raw!r}")
        llm_timeout = 60
    drafter_max_raw = os.getenv("DRAFTER_MAX_TOKENS", "600").strip()
    try:
        drafter_max_tokens = int(drafter_max_raw)
    except ValueError:
        problems.append(f"DRAFTER_MAX_TOKENS must be int, got {drafter_max_raw!r}")
        drafter_max_tokens = 600

    # --- Notifier ---
    notify_channel = os.getenv("NOTIFY_CHANNEL", "ntfy").strip().lower()
    if notify_channel not in VALID_NOTIFY_CHANNELS:
        problems.append(
            f"NOTIFY_CHANNEL={notify_channel!r} invalid; "
            f"must be one of {sorted(VALID_NOTIFY_CHANNELS)}"
        )

    ntfy_topic = os.getenv("NTFY_TOPIC", "").strip() or None
    ntfy_base_url = os.getenv("NTFY_BASE_URL", "https://ntfy.sh").strip() or "https://ntfy.sh"
    ntfy_token = os.getenv("NTFY_TOKEN", "").strip() or None
    whatsapp_phone = os.getenv("WHATSAPP_PHONE", "").strip() or None
    whatsapp_apikey = os.getenv("WHATSAPP_APIKEY", "").strip() or None
    pushover_token = os.getenv("PUSHOVER_TOKEN", "").strip() or None
    pushover_user = os.getenv("PUSHOVER_USER", "").strip() or None
    discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None

    # --- LinkedIn (optional) ---
    linkedin_enabled = os.getenv("LINKEDIN_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    linkedin_profile_dir = _resolve_path(
        os.getenv("LINKEDIN_PROFILE_DIR", ".data/linkedin_profile")
    )
    linkedin_poll_raw = os.getenv("LINKEDIN_POLL_HOURS", "6").strip()
    try:
        linkedin_poll_hours = int(linkedin_poll_raw)
    except ValueError:
        problems.append(f"LINKEDIN_POLL_HOURS must be int, got {linkedin_poll_raw!r}")
        linkedin_poll_hours = 6
    linkedin_max_raw = os.getenv("LINKEDIN_MAX_THREADS", "20").strip()
    try:
        linkedin_max_threads = int(linkedin_max_raw)
    except ValueError:
        problems.append(f"LINKEDIN_MAX_THREADS must be int, got {linkedin_max_raw!r}")
        linkedin_max_threads = 20

    # Channel-specific required vars (only enforce for the active channel).
    channel_missing: List[str] = []
    if notify_channel == "ntfy" and not ntfy_topic:
        channel_missing.append("NTFY_TOPIC")
    elif notify_channel == "whatsapp":
        if not whatsapp_phone:
            channel_missing.append("WHATSAPP_PHONE")
        if not whatsapp_apikey:
            channel_missing.append("WHATSAPP_APIKEY")
    elif notify_channel == "pushover":
        if not pushover_token:
            channel_missing.append("PUSHOVER_TOKEN")
        if not pushover_user:
            channel_missing.append("PUSHOVER_USER")
    elif notify_channel == "discord" and not discord_webhook_url:
        channel_missing.append("DISCORD_WEBHOOK_URL")

    if channel_missing:
        problems.append(
            f"NOTIFY_CHANNEL={notify_channel!r} requires: {', '.join(channel_missing)}"
        )

    if missing:
        problems.append(f"Missing required env vars: {', '.join(missing)}")

    if problems:
        raise ConfigError(
            "Configuration invalid. Fix the following in your .env "
            f"(see .env.example):\n  - " + "\n  - ".join(problems)
        )

    return Config(
        poll_minutes=poll_minutes,
        state_db_path=state_db_path,
        token_path=token_path,
        credentials_json_path=creds_path,
        skip_senders=skip_senders,
        classifier_model=classifier_model,
        drafter_model=drafter_model,
        llm_base_url=llm_base_url,
        llm_timeout=llm_timeout,
        drafter_max_tokens=drafter_max_tokens,
        notify_channel=notify_channel,
        ntfy_topic=ntfy_topic,
        ntfy_base_url=ntfy_base_url,
        ntfy_token=ntfy_token,
        whatsapp_phone=whatsapp_phone,
        whatsapp_apikey=whatsapp_apikey,
        pushover_token=pushover_token,
        pushover_user=pushover_user,
        discord_webhook_url=discord_webhook_url,
        linkedin_enabled=linkedin_enabled,
        linkedin_profile_dir=linkedin_profile_dir,
        linkedin_poll_hours=linkedin_poll_hours,
        linkedin_max_threads=linkedin_max_threads,
    )
