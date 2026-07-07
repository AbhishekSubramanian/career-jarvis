"""ntfy.sh notifier backend (recommended default).

ntfy is free, open-source, account-less push. You install the ntfy phone app,
subscribe to a secret topic, and this app POSTs to https://ntfy.sh/<topic>
(or NTFY_BASE_URL if self-hosted). No charges, no signup.

Self-hosting: set NTFY_BASE_URL to your own ntfy server. For protected
topics, set NTFY_TOKEN.
"""

from __future__ import annotations

import logging

import requests

from ..config import Config
from .base import BaseNotifier

log = logging.getLogger(__name__)


class NtfyNotifier(BaseNotifier):
    MAX_LENGTH = 4096

    def __init__(self, config: Config):
        if not config.ntfy_topic:
            raise ValueError("NTFY_TOPIC not set")
        self.topic = config.ntfy_topic
        self.base_url = config.ntfy_base_url.rstrip("/")
        self.token = config.ntfy_token

    def send(self, title: str, message: str, *, urgency: str = "low") -> None:
        url = f"{self.base_url}/{self.topic}"
        # HTTP headers must be latin-1 encodable. ntfy titles can contain
        # unicode (em-dashes, emoji, etc.); sanitize to latin-1 with replacement
        # so a non-ASCII title never crashes the send (defensive: the dispatcher
        # catches errors anyway, but we'd rather deliver the alert).
        headers = {"Title": _latin1_safe(title)[:200]}
        # ntfy priority: 1=min,2=low,3=default,4=high,5=max
        pri = {"high": "5", "medium": "4", "low": "3"}.get(urgency, "3")
        headers["Priority"] = pri
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = message[: self.MAX_LENGTH]
        resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=15)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"ntfy POST failed: HTTP {resp.status_code} {resp.text[:200]}"
            )


def _latin1_safe(value: str) -> str:
    """Encode a header value to latin-1, replacing chars that don't fit."""
    return value.encode("latin-1", errors="replace").decode("latin-1")
