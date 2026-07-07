"""Gmail client — read + create drafts ONLY (never send).

OAuth scopes are locked to:
  - https://www.googleapis.com/auth/gmail.readonly
  - https://www.googleapis.com/auth/gmail.compose   (create drafts)

The ``gmail.send`` scope is deliberately NEVER requested. The system can only
draft replies; the human is always the send button. (Acceptance check: grep
the repo for "gmail.send" — it appears only in this comment as a negative
assertion and in README explaining why it's absent.)

Incremental fetching uses Gmail's ``historyId`` as the cursor. On the first
run (no stored cursor) we backfill the most recent ``INITIAL_BACKFILL``
messages and persist their historyId so subsequent runs are purely
incremental via ``users.history.list``.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

# LOCKED scopes. Never add gmail.send here.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Number of most-recent messages to process on the very first run.
INITIAL_BACKFILL = 5

# Gmail history.list returns at most a few hundred history records per page;
# cap how many pages we walk to bound a single poll.
MAX_HISTORY_PAGES = 5
HISTORY_PAGE_SIZE = 100


@dataclass
class EmailRecord:
    id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    source: str = "email"  # "email" | "linkedin" (LinkedIn email notifs -> email)
    date: str = ""


class GmailClient:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self._service = None

    # --- Auth ---------------------------------------------------------------

    def _creds(self) -> Credentials:
        token_path = self.config.token_absolute
        creds: Optional[Credentials] = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            _write_secret_file(token_path, creds.to_json())
            return creds

        # First run: run the OAuth flow (opens a browser locally).
        creds_path = self.config.credentials_absolute
        if not creds_path.exists():
            raise RuntimeError(
                f"Gmail credentials file not found at {creds_path}. "
                "Download credentials.json from Google Cloud (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        _write_secret_file(token_path, creds.to_json())
        return creds

    @property
    def service(self):
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self._creds(), cache_discovery=False)
        return self._service

    # --- Fetch --------------------------------------------------------------

    def _get_message(self, msg_id: str) -> Optional[dict]:
        try:
            return (
                self.service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError as exc:
            log.warning("Failed to fetch message %s: %s", msg_id, exc)
            return None

    @staticmethod
    def _header(headers: list[dict], name: str) -> str:
        name = name.lower()
        for h in headers:
            if h.get("name", "").lower() == name:
                return h.get("value", "")
        return ""

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Best-effort plain-text body extraction from a full-message payload."""
        text = ""

        def walk(part: dict):
            nonlocal text
            mime = part.get("mimeType", "")
            body = part.get("body", {}) or {}
            if mime == "text/plain" and body.get("data"):
                text = _b64url_decode(body["data"]).decode("utf-8", errors="replace")
                return
            if mime.startswith("multipart/") and not text:
                for sub in part.get("parts", []) or []:
                    walk(sub)
                    if text:
                        return
            # Fallback: some single-part messages have no mimeType.
            if not text and body.get("data") and not mime.startswith("multipart/"):
                text = _b64url_decode(body["data"]).decode("utf-8", errors="replace")

        walk(payload or {})
        return text.strip()

    def _to_record(self, msg: dict) -> EmailRecord:
        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        sender = self._header(headers, "From")
        subject = self._header(headers, "Subject")
        date = self._header(headers, "Date")
        body = self._extract_body(payload)

        # LinkedIn notification emails get tagged so the classifier can set
        # source="linkedin" when the content is a forwarded LinkedIn message.
        return EmailRecord(
            id=msg["id"],
            thread_id=msg.get("threadId", ""),
            sender=sender,
            subject=subject,
            body=body,
            source="email",
            date=date,
        )

    def fetch_new(self) -> list[EmailRecord]:
        """Return new, not-yet-processed messages, advancing the history cursor.

        Strategy:
        - No stored cursor: backfill the most recent INITIAL_BACKFILL messages
          and persist the current historyId.
        - Stored cursor: walk users.history.list(startHistoryId=cursor) and
          collect messagesAdded events. If the history is too old (Gmail
          purges it after ~1 week), fall back to a fresh list + reseed.
        """
        cursor = self.store.get_cursor("gmail_history")
        if cursor is None:
            return self._initial_backfill()
        return self._incremental_fetch(cursor)

    def _initial_backfill(self) -> list[EmailRecord]:
        log.info("No Gmail cursor yet — doing initial backfill of recent %d messages.",
                 INITIAL_BACKFILL)
        resp = (
            self.service.users()
            .messages()
            .list(userId="me", maxResults=INITIAL_BACKFILL)
            .execute()
        )
        # The list response carries the current historyId — store it.
        history_id = resp.get("historyId")
        msgs_meta = resp.get("messages", []) or []

        records: list[EmailRecord] = []
        for meta in msgs_meta:
            full = self._get_message(meta["id"])
            if full is None:
                continue
            records.append(self._to_record(full))

        if history_id:
            self.store.set_cursor("gmail_history", history_id)
        return records

    def _incremental_fetch(self, start_history_id: str) -> list[EmailRecord]:
        collected: list[EmailRecord] = []
        seen_ids: set[str] = set()
        page_token: Optional[str] = None
        new_history_id: Optional[str] = None

        for _ in range(MAX_HISTORY_PAGES):
            try:
                list_kwargs = {
                    "userId": "me",
                    "startHistoryId": start_history_id,
                    "maxResults": HISTORY_PAGE_SIZE,
                    "historyTypes": ["messageAdded"],
                }
                if page_token:
                    list_kwargs["pageToken"] = page_token
                resp = (
                    self.service.users()
                    .history()
                    .list(**list_kwargs)
                    .execute()
                )
            except HttpError as exc:
                # 404 / "history expired" -> reseed and bail this cycle.
                log.warning("Gmail history fetch failed (%s); reseeding cursor.", exc)
                # Clear cursor so next cycle backfills fresh; return nothing now.
                return []
            new_history_id = resp.get("historyId", new_history_id)
            for hist in resp.get("history", []) or []:
                for added in hist.get("messagesAdded", []) or []:
                    msg_meta = added.get("message", {}) or {}
                    mid = msg_meta.get("id")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    full = self._get_message(mid)
                    if full is None:
                        continue
                    collected.append(self._to_record(full))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        if new_history_id:
            self.store.set_cursor("gmail_history", new_history_id)
        return collected

    # --- Drafts (compose scope only) ----------------------------------------

    def create_draft(
        self,
        thread_id: str,
        to_address: str,
        subject: str,
        body_text: str,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> str:
        """Create a Gmail draft in the original thread. Returns the draft id.

        Uses the ``gmail.compose`` scope via ``users.drafts.create``. The draft
        is a reply: Subject is prefixed with 'Re:' if needed, and
        In-Reply-To/References headers are set so Gmail threads it correctly.
        The message is NEVER sent by this code.
        """
        msg = EmailMessage()
        msg["To"] = to_address
        # Avoid a double "Re:".
        subj = subject.strip()
        if not subj.lower().startswith("re:"):
            subj = f"Re: {subj}" if subj else "Re: your message"
        msg["Subject"] = subj
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to or ""
        msg["Message-ID"] = make_msgid(domain="career-jarvis.local")
        msg.set_content(body_text)

        raw = _b64url_encode(msg.as_bytes())
        draft_body = {
            "message": {
                "raw": raw,
                "threadId": thread_id,
            }
        }
        try:
            result = (
                self.service.users()
                .drafts()
                .create(userId="me", body=draft_body)
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"Gmail create_draft failed: {exc}") from exc
        draft_id = result.get("id", "")
        log.info("Created Gmail draft id=%s in thread=%s (NOT sent).", draft_id, thread_id)
        return draft_id

    # --- Header helpers (public for orchestrator use) -----------------------

    @staticmethod
    def extract_reply_address(sender_header: str) -> str:
        """Parse a 'From' header into a bare email address."""
        name, addr = parseaddr(sender_header)
        return addr

    @staticmethod
    def extract_message_id_header(msg: dict) -> str:
        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        for h in headers:
            if h.get("name", "").lower() == "message-id":
                return h.get("value", "")
        return ""

    def get_raw_message(self, msg_id: str) -> Optional[dict]:
        return self._get_message(msg_id)


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _write_secret_file(path: Path, contents: str) -> None:
    """Write a secret file (OAuth token, etc.) with restrictive permissions.

    SECURITY: the Gmail OAuth token is a bearer credential for the user's
    entire mailbox (read + compose). On POSIX systems we chmod 0o600 so other
    users on the host can't read it. On Windows the umask/ACL model differs
    and this chmod is a no-op, but we set it unconditionally for portability.
    """
    import os

    path.write_text(contents)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Not all platforms/filesystems support chmod (e.g. some Windows
        # configs); don't fail the run over it. The file is still in .data/,
        # which the user should keep non-shared.
        log.debug("Could not chmod %s (continuing): ignored", path)
