"""Discord webhook notifier backend.

Free; good mobile push. Create a webhook in a Discord server channel's
integration settings and set DISCORD_WEBHOOK_URL.
"""

from __future__ import annotations

import logging

import requests

from ..config import Config
from .base import BaseNotifier

log = logging.getLogger(__name__)

# Discord webhook body limit is 2000 chars.
class DiscordNotifier(BaseNotifier):
    MAX_LENGTH = 1900

    def __init__(self, config: Config):
        if not config.discord_webhook_url:
            raise ValueError("DISCORD_WEBHOOK_URL not set")
        self.webhook_url = config.discord_webhook_url

    def send(self, title: str, message: str, *, urgency: str = "low") -> None:
        icon = {"high": "🚨", "medium": "📨", "low": "ℹ️"}.get(urgency, "ℹ️")
        content = f"{icon} **{title}**\n{message}"[: self.MAX_LENGTH]
        resp = requests.post(
            self.webhook_url,
            json={"content": content},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Discord webhook failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
