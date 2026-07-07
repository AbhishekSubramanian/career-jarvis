"""Base interface for notifier backends.

Every backend implements two methods with the same signature. The dispatcher
in src/notifier.py wraps them so a backend failure is logged, never raised.
"""

from __future__ import annotations

import abc


class BaseNotifier(abc.ABC):
    #: Approx max message length this channel accepts (truncated before send).
    MAX_LENGTH: int = 4000

    @abc.abstractmethod
    def send(self, title: str, message: str, *, urgency: str = "low") -> None:
        """Send a push notification. Raises on failure; dispatcher catches."""
        ...

    def send_opportunity_alert(self, message: str) -> None:
        # ASCII-only titles: some backends (ntfy) put the title in an HTTP
        # header, which must be latin-1 encodable. Avoid em-dashes here.
        self.send("Career Jarvis - opportunity", message, urgency="medium")

    def send_error_alert(self, message: str) -> None:
        self.send("Career Jarvis - error", message, urgency="high")
