"""Pushover notifier backend.

Pushover is purpose-built, very reliable, $5 one-time per platform. Needs
PUSHOVER_TOKEN (your app token) and PUSHOVER_USER (your user key).
"""

from __future__ import annotations

import logging

import requests

from ..config import Config
from .base import BaseNotifier

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class PushoverNotifier(BaseNotifier):
    MAX_LENGTH = 1024  # Pushover message limit

    def __init__(self, config: Config):
        if not config.pushover_token:
            raise ValueError("PUSHOVER_TOKEN not set")
        if not config.pushover_user:
            raise ValueError("PUSHOVER_USER not set")
        self.token = config.pushover_token
        self.user = config.pushover_user

    def send(self, title: str, message: str, *, urgency: str = "low") -> None:
        # Pushover priority: -2 lowest, 0 normal, 1 high, 2 emergency
        pri = {"high": 1, "medium": 0, "low": -1}.get(urgency, 0)
        payload = {
            "token": self.token,
            "user": self.user,
            "title": title[:100],
            "message": message[: self.MAX_LENGTH],
            "priority": pri,
        }
        resp = requests.post(PUSHOVER_URL, data=payload, timeout=15)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Pushover failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
