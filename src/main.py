"""Orchestrator — the poll loop and per-message processing pipeline.

Modes:
  python -m src.main --once          # one cycle then exit
  python -m src.main --once --dry-run # full loop against MOCKED gmail+LLM, no creds
  python -m src.main                 # daemon: poll every POLL_MINUTES

Reliability guarantees (per BUILD_PROMPT.md):
- SQLite dedup: every message id processed exactly once across restarts.
- Per-message try/except: one poison message never wedges the loop. On
  per-message failure we send an error alert and mark the message processed
  (status='error' or 'manual_review') so it can't loop forever.
- The notifier is wrapped so it can never crash the pipeline.
- Structured logging throughout.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .agents.classifier import (
    ClassificationVerdict,
    ManualReviewNeededError,
    classify,
)
from .agents.drafter import draft_reply
from .config import Config, ConfigError, load_config
from .gmail_client import EmailRecord, GmailClient
from .notifier import build_notifier
from .notifier import Notifier
from .store import Store

log = logging.getLogger(__name__)

# Type alias for the LLM complete callable injected into agents.
LLMComplete = Callable[..., str]


@dataclass
class CycleStats:
    fetched: int = 0
    opportunities: int = 0
    drafted: int = 0
    errors: int = 0
    skipped: int = 0
    manual_review: int = 0


class Orchestrator:
    def __init__(
        self,
        config: Config,
        store: Store,
        gmail_client: Optional[GmailClient],
        notifier: Notifier,
        *,
        llm_complete: LLMComplete,
        linkedin_client=None,
        dry_run: bool = False,
    ):
        self.config = config
        self.store = store
        self.gmail_client = gmail_client
        self.notifier = notifier
        self.llm_complete = llm_complete
        self.linkedin_client = linkedin_client
        self.dry_run = dry_run

    # --- Core per-message processing ----------------------------------------

    def process_message(self, message: EmailRecord, source: str) -> None:
        """Process a single message with full isolation.

        Dedup is checked here; the message is marked processed on every exit
        path (success, skip, error, manual review) so it can never loop.
        """
        if self.store.is_processed(source, message.id):
            log.debug("Skip already-processed %s/%s", source, message.id)
            return

        # Hard-skip by sender (e.g. own address, known noise).
        if _matches_skip(self.config, message.sender):
            self.store.mark_processed(
                source, message.id, thread_id=message.thread_id,
                sender=message.sender, subject=message.subject, status="skipped",
            )
            log.debug("Skip by sender rule: %s", message.sender)
            return

        try:
            verdict = classify(
                self.config, message, llm_complete=self.llm_complete
            )
        except ManualReviewNeededError as exc:
            # Fail-safe: record + alert, but DO NOT drop the message.
            log.error("Manual review needed for %s/%s: %s", source, message.id, exc.reason)
            self.store.mark_processed(
                source, message.id, thread_id=message.thread_id,
                sender=message.sender, subject=message.subject,
                status="manual_review", error=exc.reason,
            )
            self.notifier.send_error_alert(
                f"Classifier couldn't parse a message and flagged it for "
                f"manual review.\nFrom: {message.sender}\nSubject: "
                f"{message.subject}\nReason: {exc.reason}"
            )
            return
        except Exception as exc:
            self._handle_unexpected(source, message, exc)
            return

        # Not an opportunity -> record verdict, no draft, no alert.
        if not verdict.is_job_opportunity:
            self.store.mark_processed(
                source, message.id, thread_id=message.thread_id,
                sender=message.sender, subject=message.subject,
                status="ok", verdict=verdict.model_dump(),
            )
            log.info("Not an opportunity (%s): %s", verdict.category, message.subject[:80])
            return

        # Genuine opportunity -> draft + (maybe) Gmail draft + notify.
        try:
            draft_text = draft_reply(
                self.config, message, verdict, llm_complete=self.llm_complete
            )
        except Exception as exc:
            # Drafting failed: still record the classification so the verdict
            # isn't lost, and alert the user so they can act manually.
            log.exception("Drafting failed for %s/%s", source, message.id)
            self.store.mark_processed(
                source, message.id, thread_id=message.thread_id,
                sender=message.sender, subject=message.subject,
                status="error", verdict=verdict.model_dump(),
                error=f"drafter: {exc}",
            )
            self.notifier.send_error_alert(
                f"Drafting failed for an opportunity.\nFrom: {message.sender}\n"
                f"Subject: {message.subject}\nSummary: {verdict.summary}\n"
                f"Error: {exc}"
            )
            return

        gmail_draft_id = ""
        if verdict.source == "email" and self.gmail_client is not None and not self.dry_run:
            try:
                to_addr = GmailClient.extract_reply_address(message.sender)
                gmail_draft_id = self.gmail_client.create_draft(
                    thread_id=message.thread_id,
                    to_address=to_addr,
                    subject=message.subject,
                    body_text=draft_text,
                )
            except Exception as exc:
                # Draft creation failed but we still have the text -> notify
                # the user with the draft in the alert so they can paste it.
                log.warning("Gmail draft creation failed; alerting with text inline: %s", exc)
                gmail_draft_id = ""

        self.store.mark_processed(
            source, message.id, thread_id=message.thread_id,
            sender=message.sender, subject=message.subject,
            status="ok", verdict=verdict.model_dump(),
            draft_text=draft_text,
        )

        alert = _format_opportunity_alert(
            verdict=verdict,
            message=message,
            draft_text=draft_text,
            gmail_draft_id=gmail_draft_id,
            dry_run=self.dry_run,
            source=source,
            # SECURITY: never send draft text through a real push channel
            # (ntfy topics are public-by-default). Dry-run uses a local
            # printer, so it's safe to show the draft there for visibility.
            include_draft=self.dry_run,
        )
        self.notifier.send_opportunity_alert(alert)
        log.info("Opportunity (%s, %s): %s", verdict.category, verdict.urgency, message.subject[:80])

    def _handle_unexpected(self, source: str, message: EmailRecord, exc: Exception) -> None:
        # SECURITY: log the full traceback locally, but NEVER send it to the
        # notifier. Tracebacks can contain file paths, variable values, and
        # message content; the notifier may POST to a third-party server
        # (ntfy.sh). Send only a short, safe summary to the phone.
        log.exception("Unexpected error processing %s/%s", source, message.id)
        self.store.record_error(
            source, message.id, error=f"{type(exc).__name__}: {exc}",
            thread_id=message.thread_id, sender=message.sender, subject=message.subject,
        )
        self.notifier.send_error_alert(
            f"Pipeline error on a message.\nFrom: {message.sender}\n"
            f"Subject: {message.subject}\nError: {type(exc).__name__}. "
            f"See logs for details."
        )

    # --- Cycle --------------------------------------------------------------

    def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        messages: list[tuple[EmailRecord, str]] = []

        if self.gmail_client is not None:
            try:
                fetched = self.gmail_client.fetch_new()
                for m in fetched:
                    messages.append((m, "email"))
                stats.fetched += len(fetched)
            except Exception:
                log.exception("Gmail fetch failed; skipping Gmail this cycle.")
                self.notifier.send_error_alert("Gmail fetch failed this cycle (see logs).")

        if self.linkedin_client is not None and self.config.linkedin_enabled:
            try:
                fetched = self.linkedin_client.fetch_new_messages()
                for m in fetched:
                    messages.append((m, "linkedin"))
                stats.fetched += len(fetched)
            except Exception:
                log.exception("LinkedIn fetch failed; skipping LinkedIn this cycle.")
                self.notifier.send_error_alert("LinkedIn fetch failed this cycle (see logs).")

        for message, source in messages:
            try:
                self.process_message(message, source)
            except Exception:
                # Ultimate safety net: process_message already handles its own
                # errors, but if something escapes (e.g. a bug in dedup), we
                # must not let it kill the whole cycle.
                log.exception("Top-level error on %s/%s — isolating.", source, message.id)
                try:
                    self.store.record_error(source, message.id, error="top-level isolation catch")
                except Exception:
                    log.exception("Could not record error for %s/%s", source, message.id)

        return stats

    def run_loop(self) -> None:
        log.info("Starting daemon loop (poll every %d min).", self.config.poll_minutes)
        while True:
            try:
                stats = self.run_cycle()
                log.info("Cycle done: fetched=%d", stats.fetched)
            except Exception:
                log.exception("Cycle crashed; continuing after sleep.")
            time.sleep(self.config.poll_minutes * 60)


# --- Helpers ----------------------------------------------------------------

def _matches_skip(config: Config, sender: str) -> bool:
    if not config.skip_senders:
        return False
    s = (sender or "").lower()
    return any(skip in s for skip in config.skip_senders)


def _format_opportunity_alert(
    *,
    verdict: ClassificationVerdict,
    message: EmailRecord,
    draft_text: str,
    gmail_draft_id: str,
    dry_run: bool,
    source: str,
    include_draft: bool = True,
) -> str:
    """Format the phone alert for an opportunity.

    SECURITY: by default `include_draft` is True ONLY in --dry-run (where the
    notifier is a local printer). In real runs we pass include_draft=False so
    the draft text never transits a third-party push server (ntfy.sh topics are
    public-by-default). The user reads the draft in Gmail/LinkedIn, not in the
    push notification.
    """
    icon = {"high": "🚨", "medium": "📨", "low": "ℹ️"}.get(verdict.urgency, "📨")
    src_label = "LinkedIn" if verdict.source == "linkedin" else "Email"
    lines = [
        f"{icon} [{src_label}] {verdict.category} (urgency={verdict.urgency}, conf={verdict.confidence:.2f})",
        f"From: {message.sender}",
        f"Subject: {message.subject}",
        f"Summary: {verdict.summary}",
    ]
    if include_draft:
        lines += ["", "Draft:", draft_text]
    if verdict.source == "email":
        if dry_run or not gmail_draft_id:
            lines.append("")
            lines.append("[dry-run] No Gmail draft created (dry-run).")
        else:
            lines.append("")
            lines.append(
                "Gmail draft created - open Gmail Drafts on your phone to "
                "review and send. (Draft text is NOT included here for privacy.)"
            )
    else:  # linkedin
        lines.append("")
        lines.append(
            "Open the LinkedIn thread to review the draft and paste to send - "
            "the system will NOT send for you. (Draft text is NOT included "
            "here for privacy.)"
        )
    if dry_run:
        lines.insert(0, "[DRY-RUN] " + lines[0])
    return "\n".join(lines)


# --- Entry point ------------------------------------------------------------

def _build_real_clients(config: Config, store: Store) -> tuple[Optional[GmailClient], Notifier, Optional[object]]:
    notifier = build_notifier(config)
    gmail = GmailClient(config, store)
    linkedin = None
    if config.linkedin_enabled:
        try:
            from . import linkedin_client  # local import: playwright optional
            linkedin = linkedin_client.LinkedInClient(config, store)
        except ImportError:
            log.error("LINKEDIN_ENABLED=true but playwright is not installed. "
                      "Run: pip install playwright && playwright install chromium")
        except Exception:
            log.exception("Failed to init LinkedIn client; continuing without it.")
    return gmail, notifier, linkedin


def _run(args: argparse.Namespace) -> int:
    if args.dry_run:
        # Dry-run uses mocked Gmail + mocked LLM + a printing notifier, so it
        # must NOT require real credentials. Seed the minimum env defaults
        # needed to pass config validation.
        import os
        os.environ.setdefault("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
        os.environ.setdefault("DRAFTER_MODEL", "claude-sonnet-4-5-20250929")
        os.environ.setdefault("NOTIFY_CHANNEL", "ntfy")
        os.environ.setdefault("NTFY_TOPIC", "dry-run-topic")
        os.environ.setdefault("STATE_DB_PATH", ".data/career_jarvis_dryrun.db")

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        # Fresh DB each dry-run so the cycle always demonstrates the full
        # pipeline (otherwise dedup skips everything on a 2nd run).
        try:
            config.state_db_absolute.unlink()
        except FileNotFoundError:
            pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # On Windows the default console codec (cp1252) can't encode emoji/unicode
    # used in alerts and logs; reconfigure to UTF-8 with safe replacement so
    # the dry-run print notifier and structured logging never crash on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    store = Store(config.state_db_absolute)
    try:
        if args.dry_run:
            return _run_dry_run(config, store)
        gmail, notifier, linkedin = _build_real_clients(config, store)
        from .llm import complete as real_complete
        orch = Orchestrator(
            config, store, gmail, notifier,
            llm_complete=real_complete, linkedin_client=linkedin,
        )
        if args.once:
            stats = orch.run_cycle()
            log.info("Once-cycle done: fetched=%d", stats.fetched)
        else:
            orch.run_loop()
        return 0
    finally:
        store.close()


def _run_dry_run(config: Config, store: Store) -> int:
    """Run the full loop against MOCKED Gmail + MOCKED LLM, no live creds.

    Uses the sample email fixtures and a deterministic fake LLM so the
    pipeline exercises classifier -> drafter -> notifier end to end. The
    notifier is replaced with a printing stub so nothing is actually pushed.
    """
    from .notifier import Notifier
    from tests.sample_emails import SAMPLE_EMAILS

    class _FakeGmail:
        def __init__(self, records):
            self._records = records
            self._i = 0

        def fetch_new(self):
            out = self._records[self._i:]
            self._i = len(self._records)
            return out

        def create_draft(self, thread_id, to_address, subject, body_text, **kw):
            log.info("[dry-run] would create Gmail draft in thread=%s to=%s", thread_id, to_address)
            return "fake-draft-id"

    class _PrintNotifier(Notifier):
        def __init__(self):
            super().__init__(backend=None, channel="dry-run-print")

        def send_opportunity_alert(self, message: str) -> None:
            print("\n===== OPPORTUNITY ALERT =====")
            print(message)
            print("=============================\n")

        def send_error_alert(self, message: str) -> None:
            print("\n===== ERROR ALERT =====")
            print(message)
            print("=======================\n")

    fake_gmail = _FakeGmail(SAMPLE_EMAILS)
    notifier = _PrintNotifier()
    fake_llm = _FakeLLM()

    orch = Orchestrator(
        config, store, fake_gmail, notifier,
        llm_complete=fake_llm.complete, dry_run=True,
    )
    stats = orch.run_cycle()
    print(
        f"\n[dry-run] cycle complete: fetched={stats.fetched} "
        f"(no live Gmail/LLM/notifier credentials used)"
    )
    return 0


class _FakeLLM:
    """Deterministic stand-in for the LLM in --dry-run mode.

    Returns canned classifier JSON and a canned draft, keyed off the user
    prompt content so different fixtures get sensible verdicts. This lets the
    full pipeline run with zero network and zero credentials.
    """

    REC      = {"is_job_opportunity": True,  "category": "recruiter_outreach",  "source": "email",    "confidence": 0.9, "urgency": "medium", "summary": "Recruiter with a named ML role"}
    INTER    = {"is_job_opportunity": True,  "category": "interview_invite",    "source": "email",    "confidence": 0.95,"urgency": "high",   "summary": "Interview invite for tomorrow"}
    LINKEDIN = {"is_job_opportunity": True,  "category": "recruiter_outreach",  "source": "linkedin", "confidence": 0.85,"urgency": "medium", "summary": "LinkedIn InMail about an AI role"}
    NETWORK  = {"is_job_opportunity": True,  "category": "networking",          "source": "email",    "confidence": 0.8, "urgency": "low",    "summary": "Warm intro from a fellow Hokie"}
    SPAM     = {"is_job_opportunity": False, "category": "job_alert_digest",    "source": "email",    "confidence": 0.9, "urgency": "low",    "summary": "Mass agency blast, no role specifics"}
    DIGEST   = {"is_job_opportunity": False, "category": "job_alert_digest",    "source": "email",    "confidence": 0.95,"urgency": "low",    "summary": "LinkedIn job alert digest"}
    REJECT   = {"is_job_opportunity": False, "category": "rejection",           "source": "email",    "confidence": 0.95,"urgency": "low",    "summary": "Automated rejection, no reply needed"}
    NONJOB   = {"is_job_opportunity": False, "category": "not_job_related",     "source": "email",    "confidence": 0.9, "urgency": "low",    "summary": "Banking statement — not job related"}

    def complete(self, config, role, system, user, json_mode=False, max_tokens=None):
        import json as _json
        if role == "classifier":
            body = user.lower()
            # Order matters: check the negative/specific signals first so a
            # rejection (which mentions "interview") and a digest (which
            # mentions "linkedin") aren't false-positive'd into opportunities.
            if "account summary" in body or "bank statement" in body:
                v = self.NONJOB
            elif "not moving forward" in body or "unfortunately" in body or "regret" in body:
                v = self.REJECT
            elif "jobs you may be interested" in body or "job alert" in body or "jobs matching" in body:
                v = self.DIGEST
            elif "shortlist" in body or "respond promptly" in body or "premium client" in body:
                v = self.SPAM
            elif "inmail" in body or "you have a new message" in body or "new message from" in body:
                v = self.LINKEDIN
            elif "hokie" in body or "virginia tech" in body:
                v = self.NETWORK
            elif "interview confirmed" in body or "onsite" in body or "interview is confirmed" in body:
                v = self.INTER
            else:
                v = self.REC
            return _json.dumps(v)
        # drafter
        return ("Hi there,\n\nThanks for reaching out - this sounds like an "
                "interesting role. Could you tell me a bit about the team and "
                "what it owns? Happy to dig into comp range and sponsorship "
                "details as we talk, and glad to set up a quick call.\n\n"
                "Best, Abhishek")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="career-jarvis", description="Career Jarvis opportunity copilot")
    parser.add_argument("--once", action="store_true", help="run a single poll cycle then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the full loop against MOCKED Gmail + LLM; no live credentials")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
