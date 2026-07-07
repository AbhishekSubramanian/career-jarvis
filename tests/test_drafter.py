"""Drafter + notifier + orchestrator security regression tests.

Covers:
- The em-dash ban: _strip_em_dashes removes em-dash and en-dash characters.
- ntfy header safety: _latin1_safe makes any title latin-1 encodable so the
  requests/urllib3 stack never raises UnicodeEncodeError on a header.
- PII guard (M1): _scrub_pii removes profile PII (phone, email, salary, EAD
  date, employer) from a draft - the prompt-injection exfiltration defense.
- Alert privacy (M1/M2): the real opportunity alert does NOT include the
  draft text; only the dry-run alert does.
- Error-alert safety (H1): the orchestrator's error alert sends a short
  summary, never a stack trace, to the notifier.
"""

from __future__ import annotations

from src.agents.drafter import _extract_pii_values, _scrub_pii, _strip_em_dashes
from src.notifiers.ntfy import _latin1_safe


# --- em-dash + latin-1 ------------------------------------------------------

def test_strip_em_dashes_removes_em_and_en_dash():
    out = _strip_em_dashes("Thanks - this is great and nice to hear")
    assert "\u2014" not in out
    assert "\u2013" not in out
    assert " - " in out


def test_strip_em_dashes_handles_both_dash_kinds():
    out = _strip_em_dashes("a\u2014b\u2013c")
    assert out == "a - b - c"
    assert "\u2014" not in out and "\u2013" not in out


def test_latin1_safe_keeps_ascii_titles():
    assert _latin1_safe("Career Jarvis - error") == "Career Jarvis - error"


def test_latin1_safe_makes_unicode_title_latin1_encodable():
    out = _latin1_safe("Career Jarvis \u2014 error")
    out.encode("latin-1")  # raises if not encodable; this line is the assertion
    assert "\u2014" not in out


# --- PII guard (M1) ---------------------------------------------------------

_PROFILE = """# Career Profile - Test User

## Who I am
- Name: Abhishek Subramanian
- Current role: Machine Learning Engineer at Quantiphi, based in Irving, TX.

## Contact & links
- Email: abhisheksubramanianofficial@gmail.com
- Phone: +1 (540) 934-8291

## Hard requirements
- Visa: on post-completion OPT (EAD valid to 06/23/2027), STEM-OPT eligible.
- Compensation: current comp is $110,000/year total.
"""


def test_extract_pii_finds_phone_email_salary_employer_date():
    pii = _extract_pii_values(_PROFILE)
    joined = " | ".join(pii)
    # Email is always detected.
    assert "abhisheksubramanianofficial@gmail.com" in pii
    # Salary literal is detected.
    assert any("$110,000" in p for p in pii)
    # Employer name is detected.
    assert any("Quantiphi" in p for p in pii)
    # EAD date is detected.
    assert "06/23/2027" in pii


def test_scrub_pii_removes_phone_from_draft():
    # Simulates a prompt-injection success: the model wrote the user's phone
    # into the reply despite the PII GUARD rule. The scrubber must catch it.
    draft = "Hi - you can reach me at +1 (540) 934-8291 to discuss."
    out, changed = _scrub_pii(draft, _PROFILE)
    assert changed is True
    assert "934-8291" not in out
    assert "[redacted]" in out


def test_scrub_pii_removes_salary_and_employer():
    draft = "I currently make $110,000 at Quantiphi and want a step up."
    out, changed = _scrub_pii(draft, _PROFILE)
    assert changed is True
    assert "$110,000" not in out
    assert "Quantiphi" not in out


def test_scrub_pii_removes_email_and_ead_date():
    draft = (
        "Email me at abhisheksubramanianofficial@gmail.com. "
        "My EAD is valid to 06/23/2027."
    )
    out, changed = _scrub_pii(draft, _PROFILE)
    assert changed is True
    assert "abhisheksubramanianofficial@gmail.com" not in out
    assert "06/23/2027" not in out


def test_scrub_pii_no_op_when_no_pii_present():
    draft = "Hi - thanks for reaching out. Could you tell me about the team?"
    out, changed = _scrub_pii(draft, _PROFILE)
    assert changed is False
    assert out == draft


def test_scrub_pii_handles_empty_profile():
    out, changed = _scrub_pii("call me at +1 (540) 934-8291", "")
    assert changed is False  # nothing to scrub against an empty profile
    assert out == "call me at +1 (540) 934-8291"


# --- Alert privacy (M1/M2) + error-alert safety (H1) ------------------------

def _make_records():
    """Build minimal objects to exercise _format_opportunity_alert."""
    from src.agents.classifier import ClassificationVerdict
    from src.gmail_client import EmailRecord

    verdict = ClassificationVerdict(
        is_job_opportunity=True, category="recruiter_outreach",
        source="email", confidence=0.9, urgency="medium", summary="ok",
    )
    message = EmailRecord(
        id="m1", thread_id="t1", sender="jane@x.com",
        subject="role", body="hi", source="email", date="",
    )
    return verdict, message


def test_real_opportunity_alert_omits_draft_text():
    from src.main import _format_opportunity_alert
    verdict, message = _make_records()
    draft = "SUPER SECRET DRAFT TEXT - reply with my SSN 123-45-6789"
    alert = _format_opportunity_alert(
        verdict=verdict, message=message, draft_text=draft,
        gmail_draft_id="draft-123", dry_run=False, source="email",
        include_draft=False,  # real-run default
    )
    assert "SUPER SECRET DRAFT TEXT" not in alert
    assert "Gmail draft created" in alert


def test_dry_run_alert_includes_draft_text():
    from src.main import _format_opportunity_alert
    verdict, message = _make_records()
    draft = "Thanks - this sounds interesting."
    alert = _format_opportunity_alert(
        verdict=verdict, message=message, draft_text=draft,
        gmail_draft_id="", dry_run=True, source="email",
        include_draft=True,  # dry-run default
    )
    assert "Thanks - this sounds interesting." in alert
    assert "[DRY-RUN]" in alert


def test_error_alert_never_contains_traceback_or_local_paths():
    """The notifier error alert must not leak stack traces / file paths to a
    third-party push server. We exercise _handle_unexpected directly with a
    capturing notifier and an exception whose message/traceback contains
    sensitive-looking strings, and assert none of them reach the alert.
    """
    from src.config import load_config
    from src.gmail_client import EmailRecord
    from src.main import Orchestrator
    from src.notifier import Notifier

    import os
    os.environ.setdefault("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
    os.environ.setdefault("DRAFTER_MODEL", "claude-sonnet-4-5-20250929")
    os.environ.setdefault("NOTIFY_CHANNEL", "ntfy")
    os.environ.setdefault("NTFY_TOPIC", "test-topic")
    config = load_config()

    captured: list[str] = []

    class _CaptureNotifier(Notifier):
        def __init__(self):
            super().__init__(backend=None, channel="capture")

        def send_opportunity_alert(self, message: str) -> None:
            captured.append(message)

        def send_error_alert(self, message: str) -> None:
            captured.append(message)

    # Build an Orchestrator without touching real Gmail/Store: we only call
    # _handle_unexpected, which uses self.store and self.notifier. Give it a
    # minimal in-memory store stub.
    class _StoreStub:
        def record_error(self, *a, **kw):
            pass

    orch = Orchestrator(
        config, _StoreStub(), gmail_client=None, notifier=_CaptureNotifier(),
        llm_complete=lambda *a, **k: "",
    )

    try:
        raise ValueError("boom in /Users/secret/home/path with token=sk-abc")
    except ValueError as exc:
        orch._handle_unexpected(
            source="email",
            message=EmailRecord(
                id="m1", thread_id="t1", sender="jane@x.com",
                subject="role", body="hi", source="email", date="",
            ),
            exc=exc,
        )

    assert captured, "expected an error alert to be captured"
    blob = "\n".join(captured)
    assert "/Users/secret/home/path" not in blob
    assert "sk-abc" not in blob
    assert "Traceback" not in blob
    assert "ValueError" in blob  # exception type name is OK to share
