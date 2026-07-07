"""Notifier backends for Career Jarvis.

Concrete backends live here and are selected via NOTIFY_CHANNEL in config.
Each backend exposes:

    send_opportunity_alert(message: str) -> None
    send_error_alert(message: str) -> None

The dispatcher in src/notifier.py wraps each call so a backend failure
can never crash the pipeline.
"""

from .whatsapp import WhatsAppNotifier
from .ntfy import NtfyNotifier
from .pushover import PushoverNotifier
from .discord import DiscordNotifier

__all__ = [
    "WhatsAppNotifier",
    "NtfyNotifier",
    "PushoverNotifier",
    "DiscordNotifier",
]
