"""Dedup regression test: the same message must NOT be re-processed across
runs. This guards against the bug where the dry-run wiped the SQLite DB each
run, causing every fixture to be re-classified and re-drafted repeatedly.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from src.config import load_config
from src.gmail_client import EmailRecord
from src.main import Orchestrator
from src.notifier import Notifier
from src.store import Store


class _RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(backend=None, channel="recording")
        self.opps: list[str] = []
        self.errors: list[str] = []

    def send_opportunity_alert(self, message: str) -> None:
        self.opps.append(message)

    def send_error_alert(self, message: str) -> None:
        self.errors.append(message)


class _StubGmail:
    """Returns the same fixed record on every fetch_new() call, mimicking
    Gmail re-returning a message in history across runs."""

    def __init__(self, record: EmailRecord):
        self.record = record

    def fetch_new(self):
        return [self.record]

    def create_draft(self, *a, **kw):
        return "draft-id"


class _StubLLM:
    """Always-classify-as-opportunity so the drafter+notifier always fire."""

    def __call__(self, config, role, system, user, json_mode=False, max_tokens=None):
        import json
        if role == "classifier":
            return json.dumps({
                "is_job_opportunity": True,
                "category": "recruiter_outreach",
                "source": "email",
                "confidence": 0.9,
                "urgency": "medium",
                "summary": "test opportunity",
            })
        return "Hi - thanks for reaching out."


def _config(tmp_db: Path):
    os.environ.setdefault("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
    os.environ.setdefault("DRAFTER_MODEL", "claude-sonnet-4-5-20250929")
    os.environ.setdefault("NOTIFY_CHANNEL", "ntfy")
    os.environ.setdefault("NTFY_TOPIC", "test-topic")
    os.environ["STATE_DB_PATH"] = str(tmp_db)
    return load_config()


def test_dedup_persists_across_runs_same_message_not_reprocessed():
    """Two cycles against the SAME persisted DB and the SAME message: the
    first cycle drafts+notifies; the second must skip it entirely."""
    tmp_dir = Path(tempfile.mkdtemp())
    db = tmp_dir / "state.db"
    config = _config(db)

    record = EmailRecord(
        id="msg-test-001", thread_id="thread-001",
        sender="recruiter@x.com", subject="ML role",
        body="Hi, interested in your profile.", source="email", date="",
    )
    gmail = _StubGmail(record)
    llm = _StubLLM()

    # Cycle 1: fresh DB -> should process and notify.
    store1 = Store(db)
    notif1 = _RecordingNotifier()
    orch1 = Orchestrator(config, store1, gmail, notif1, llm_complete=llm)
    orch1.run_cycle()
    store1.close()
    assert len(notif1.opps) == 1, "cycle 1 should have produced 1 opportunity alert"

    # Cycle 2: SAME DB file (persisted), SAME message id returned by gmail.
    # Dedup must skip it -> zero new alerts.
    store2 = Store(db)
    notif2 = _RecordingNotifier()
    orch2 = Orchestrator(config, store2, gmail, notif2, llm_complete=llm)
    orch2.run_cycle()
    store2.close()
    assert len(notif2.opps) == 0, (
        f"cycle 2 must NOT re-process the same message (got {len(notif2.opps)} "
        "alerts) - dedup state must persist across runs."
    )
    # And the DB should have exactly one processed record.
    store3 = Store(db)
    row = store3.get_record("email", "msg-test-001")
    store3.close()
    assert row is not None
    assert row["status"] == "ok"


def test_reset_db_flag_deletes_state():
    """--reset-db semantics: when the DB is deleted before a cycle, a
    previously-processed message IS re-processed (this is the explicit
    fresh-start path, and confirms the fix is the removal of the per-run wipe
    rather than weakening dedup)."""
    import shutil

    tmp_dir = Path(tempfile.mkdtemp())
    db = tmp_dir / "state.db"
    config = _config(db)

    record = EmailRecord(
        id="msg-test-002", thread_id="thread-002",
        sender="recruiter@y.com", subject="AI role",
        body="Hi", source="email", date="",
    )
    gmail = _StubGmail(record)
    llm = _StubLLM()

    # Process once.
    s1 = Store(db); n1 = _RecordingNotifier()
    Orchestrator(config, s1, gmail, n1, llm_complete=llm).run_cycle()
    s1.close()
    assert len(n1.opps) == 1

    # Simulate the --reset-db path: delete the DB file, then re-run.
    db.unlink()
    s2 = Store(db); n2 = _RecordingNotifier()
    Orchestrator(config, s2, gmail, n2, llm_complete=llm).run_cycle()
    s2.close()
    assert len(n2.opps) == 1, "after explicit DB reset, the message is processed again"
