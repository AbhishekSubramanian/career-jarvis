"""CallMeBot WhatsApp notifier backend.

One-time setup:
  1. Add the CallMeBot WhatsApp bot to your contacts: +34 600 83 81 81
     (verify the current number at https://www.callmebot.com/blog/free-api-whatsapp-messages/).
  2. Send it the message:  I allow callmebot to send me messages
  3. It replies with your API key. Set WHATSAPP_PHONE and WHATSAPP_APIKEY.

Tradeoff: routes through a third-party relay (fine for low-volume personal
alerts); can rate-limit. Free.
"""

from __future__ import annotations

import logging
import urllib.parse

import requests

from ..config import Config
from .base import BaseNotifier

log = logging.getLogger(__name__)

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


class WhatsAppNotifier(BaseNotifier):
    # WhatsApp via CallMeBot is happiest with short messages.
    MAX_LENGTH = 3000

    def __init__(self, config: Config):
        if not config.whatsapp_phone:
            raise ValueError("WHATSAPP_PHONE not set")
        if not config.whatsapp_apikey:
            raise ValueError("WHATSAPP_APIKEY not set")
        self.phone = config.whatsapp_phone
        self.apikey = config.whatsapp_apikey

    def send(self, title: str, message: str, *, urgency: str = "low") -> None:
        text = f"*{title}*\n\n{message}"[: self.MAX_LENGTH]
        params = {
            "phone": self.phone,
            "text": text,
            "apikey": self.apikey,
        }
        url = f"{CALLMEBOT_URL}?{urllib.parse.urlencode(params)}"
        resp = requests.get(url, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"CallMeBot WhatsApp failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        # CallMeBot sometimes returns 200 with an error body.
        body = (resp.text or "").lower()
        if "error" in body and "invalid" in body:
            raise RuntimeError(f"CallMeBot error: {resp.text[:200]}")
