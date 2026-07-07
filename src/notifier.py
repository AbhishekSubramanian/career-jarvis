"""Notifier dispatcher (REQUIREMENT B).

Selects the active backend from NOTIFY_CHANNEL and exposes a uniform API:
``send_opportunity_alert`` and ``send_error_alert``. Every backend call is
wrapped so a notification failure is logged and never crashes the pipeline.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config
from .notifiers import (
    DiscordNotifier,
    NtfyNotifier,
    PushoverNotifier,
    WhatsAppNotifier,
)
from .notifiers.base import BaseNotifier

log = logging.getLogger(__name__)

_BUILDERS = {
    "ntfy": lambda c: NtfyNotifier(c),
    "whatsapp": lambda c: WhatsAppNotifier(c),
    "pushover": lambda c: PushoverNotifier(c),
    "discord": lambda c: DiscordNotifier(c),
}


class Notifier:
    """Wraps a backend so failures are caught + logged, never raised."""

    def __init__(self, backend: Optional[BaseNotifier], channel: str):
        self.backend = backend
        self.channel = channel

    def send_opportunity_alert(self, message: str) -> None:
        self._safe_send("opportunity", message, is_error=False)

    def send_error_alert(self, message: str) -> None:
        self._safe_send("error", message, is_error=True)

    def _safe_send(self, kind: str, message: str, *, is_error: bool) -> None:
        if self.backend is None:
            log.warning("No notifier backend; dropping %s alert: %s", kind, message[:120])
            return
        try:
            if is_error:
                self.backend.send_error_alert(message)
            else:
                self.backend.send_opportunity_alert(message)
        except Exception:
            # NEVER crash the pipeline because of a notification failure.
            log.exception("Notifier (%s) failed to send %s alert", self.channel, kind)


def build_notifier(config: Config) -> Notifier:
    channel = config.notify_channel
    builder = _BUILDERS.get(channel)
    if builder is None:
        log.error("Unknown NOTIFY_CHANNEL=%r; alerts disabled.", channel)
        return Notifier(backend=None, channel=channel)
    try:
        backend = builder(config)
    except Exception:
        log.exception("Failed to build %r notifier; alerts disabled.", channel)
        return Notifier(backend=None, channel=channel)
    return Notifier(backend=backend, channel=channel)
