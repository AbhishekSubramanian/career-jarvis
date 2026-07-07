"""OPTIONAL direct LinkedIn message ingestion (opt-in, behind LINKEDIN_ENABLED).

⚠️ WARNING — read the warning below before enabling.
Direct browser automation of your logged-in LinkedIn session violates
LinkedIn's User Agreement. LinkedIn actively detects automation; the
realistic risk is account restriction or a permanent ban. The email-
notification baseline (this app's default Gmail ingestion of LinkedIn
notification emails) is the safe, supported path. Enable this only if you
accept that risk.

Safety constraints implemented here:
- Read-only: NO send, NO react, NO typing into any message box. There is no
  code path that types into a LinkedIn message composer or clicks send.
  (Acceptance: grep this file for send/typ... — only comments mention them.)
- Reuses ONE persistent browser context you log into manually ONCE. The code
  NEVER enters your username/password; if not logged in, it raises a clear
  error and stops.
- Slow, jittered polling on its own schedule (LINKEDIN_POLL_HOURS, default 6),
  capped at LINKEDIN_MAX_THREADS recent threads per poll.
- Randomized human-like delays + a small random scroll before reading.

Dependencies (optional): pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .gmail_client import EmailRecord
from .store import Store

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None  # type: ignore
    PWTimeout = TimeoutError  # type: ignore
    _PLAYWRIGHT_AVAILABLE = False


@dataclass
class LinkedInMessage:
    thread_id: str
    sender: str
    sender_title: str
    body: str


class LinkedInNotLoggedIn(RuntimeError):
    pass


class LinkedInClient:
    """Read-only LinkedIn message reader via a persistent Playwright profile."""

    def __init__(self, config: Config, store: Store):
        if not _PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "playwright is not installed. Run: "
                "pip install playwright && playwright install chromium"
            )
        self.config = config
        self.store = store
        self.profile_dir = config.linkedin_profile_absolute
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def _human_delay(self, lo: float = 1.5, hi: float = 5.0) -> None:
        time.sleep(random.uniform(lo, hi))

    def fetch_new_messages(self) -> list[EmailRecord]:
        """Open LinkedIn messaging, read recent threads, return new EmailRecords.

        Each record has source="linkedin". Dedup happens downstream via the
        store on (source, message_id).
        """
        records: list[EmailRecord] = []
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=True,
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
                self._human_delay(2.0, 4.0)

                if self._looks_like_login_wall(page):
                    raise LinkedInNotLoggedIn(
                        "Not logged into LinkedIn. Manually log in once in the "
                        f"persistent profile at {self.profile_dir} (launch with "
                        "headless=False), then re-run. The code never logs in for you."
                    )

                self._human_delay()
                self._small_random_scroll(page)
                self._human_delay()

                threads = self._read_thread_list(page, self.config.linkedin_max_threads)
                seen_cursor = self.store.get_cursor("linkedin_threads") or ""
                new_thread_ids = [t for t in threads if t.thread_id not in (seen_cursor or "")]

                for t in new_thread_ids[: self.config.linkedin_max_threads]:
                    self._human_delay()
                    body_excerpt = t.body
                    msg_id = f"{t.thread_id}::{abs(hash(body_excerpt[:200]))}"
                    if self.store.is_processed("linkedin", msg_id):
                        continue
                    records.append(EmailRecord(
                        id=msg_id,
                        thread_id=t.thread_id,
                        sender=t.sender,
                        subject=f"LinkedIn: {t.sender}",
                        body=body_excerpt,
                        source="linkedin",
                        date="",
                    ))

                if new_thread_ids:
                    self.store.set_cursor(
                        "linkedin_threads",
                        "|".join(t.thread_id for t in new_thread_ids),
                    )
            finally:
                context.close()
        return records

    # --- DOM helpers (selectors may shift; fail soft, never send) ----------

    def _looks_like_login_wall(self, page) -> bool:
        try:
            url = page.url or ""
            if "/login" in url or "/checkpoint" in url or "/uas/login" in url:
                return True
            return page.locator("input[name='session_key']").count() > 0
        except Exception:
            return False

    def _small_random_scroll(self, page) -> None:
        try:
            page.mouse.wheel(0, random.randint(80, 240))
        except Exception:
            pass

    def _read_thread_list(self, page, max_threads: int) -> list[LinkedInMessage]:
        """Read the visible thread list. Selectors are best-effort and kept
        defensive: any failure returns an empty list rather than crashing the
        pipeline. Reads only — never types or clicks send.
        """
        out: list[LinkedInMessage] = []
        try:
            # The messaging sidebar lists conversation items. These selectors
            # target the generic list-item containers LinkedIn renders.
            items = page.locator(
                "section[data-test-id='conversation-list'] li, "
                "ul[data-test-id='conversations-list'] li, "
                "div[class*='conversation-list'] > div"
            ).all()
            for item in items[:max_threads]:
                try:
                    self._human_delay(1.0, 2.5)
                    text = (item.inner_text(timeout=4000) or "").strip()
                    if not text:
                        continue
                    href = item.get_attribute("href") or ""
                    thread_id = href or f"idx:{items.index(item)}"
                    sender, body = self._split_sender_and_body(text)
                    title = ""
                    out.append(LinkedInMessage(
                        thread_id=thread_id, sender=sender,
                        sender_title=title, body=body,
                    ))
                except Exception:
                    continue
        except Exception:
            log.warning("Could not read LinkedIn thread list; returning nothing.")
        return out

    @staticmethod
    def _split_sender_and_body(text: str) -> tuple[str, str]:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        sender = lines[0] if lines else "(unknown sender)"
        body = " ".join(lines[1:]) if len(lines) > 1 else text
        return sender, body[:2000]
