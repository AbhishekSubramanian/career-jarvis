"""SQLite state store for Career Jarvis.

Responsibilities:
- Dedup: each ingested message is processed exactly once across crashes/restarts.
- Incremental Gmail cursor: the last seen historyId is persisted and replayed.
- Classification log: every verdict is recorded for audit/debug.

Design notes:
- One connection per Store instance; SQLite serializes writes. The poll loop
  is single-threaded, so this is fine and avoids locking complexity.
- `mark_processed` is the dedup gate: callers check `is_processed` BEFORE work
  and call `mark_processed` AFTER (including, critically, on per-message
  failure) so a poison message can never wedge the loop forever.
- `WAL` mode for durability across crashes and better concurrent-read behavior.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    source           TEXT NOT NULL,           -- 'email' | 'linkedin'
    message_id       TEXT NOT NULL,           -- gmail msg id or linkedin thread+msg id
    thread_id        TEXT,                    -- gmail thread id / linkedin conversation id
    sender           TEXT,
    subject          TEXT,
    processed_at     INTEGER NOT NULL,        -- epoch seconds
    status           TEXT NOT NULL,           -- 'ok' | 'error' | 'skipped' | 'manual_review'
    verdict_json     TEXT,                    -- full classifier verdict (nullable)
    draft_text       TEXT,                    -- the drafted reply (nullable)
    error            TEXT,                    -- error text on failure (nullable)
    PRIMARY KEY (source, message_id)
);

CREATE TABLE IF NOT EXISTS cursors (
    name             TEXT PRIMARY KEY,        -- 'gmail_history' | 'gmail_start_history' | 'linkedin_threads'
    value            TEXT NOT NULL,
    updated_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_processed_at
    ON processed_messages(processed_at);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            timeout=30,
            isolation_level=None,  # autocommit; we manage txns explicitly
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)
        # SECURITY: the DB stores classification verdicts, sender/subject, and
        # drafted replies - sensitive. Restrict file permissions to the owner.
        _chmod_secret(db_path)
        # WAL mode also creates a -wal sidecar; restrict it too if it appears.
        _chmod_secret(db_path.with_suffix(db_path.suffix + "-wal"))
        log.debug("Store ready at %s", self.db_path)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            log.exception("Error closing store connection")

    @contextmanager
    def _txn(self):
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            yield cur
            cur.execute("COMMIT;")
        except Exception:
            cur.execute("ROLLBACK;")
            raise
        finally:
            cur.close()

    # --- Dedup --------------------------------------------------------------

    def is_processed(self, source: str, message_id: str) -> bool:
        with self._txn() as cur:
            row = cur.execute(
                "SELECT 1 FROM processed_messages "
                "WHERE source=? AND message_id=? LIMIT 1",
                (source, message_id),
            ).fetchone()
        return row is not None

    def mark_processed(
        self,
        source: str,
        message_id: str,
        *,
        thread_id: Optional[str] = None,
        sender: Optional[str] = None,
        subject: Optional[str] = None,
        status: str = "ok",
        verdict: Optional[dict[str, Any]] = None,
        draft_text: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Idempotent: inserts or replaces the row for this message."""
        with self._txn() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO processed_messages
                    (source, message_id, thread_id, sender, subject,
                     processed_at, status, verdict_json, draft_text, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    message_id,
                    thread_id,
                    sender,
                    subject,
                    int(time.time()),
                    status,
                    json.dumps(verdict) if verdict is not None else None,
                    draft_text,
                    error,
                ),
            )

    def record_error(
        self, source: str, message_id: str, error: str,
        thread_id: Optional[str] = None, sender: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> None:
        """Mark a poison message processed-as-error so it can't loop forever."""
        self.mark_processed(
            source, message_id,
            thread_id=thread_id, sender=sender, subject=subject,
            status="error", error=error,
        )

    # --- Cursors ------------------------------------------------------------

    def get_cursor(self, name: str) -> Optional[str]:
        with self._txn() as cur:
            row = cur.execute(
                "SELECT value FROM cursors WHERE name=?", (name,)
            ).fetchone()
        return row["value"] if row else None

    def set_cursor(self, name: str, value: str) -> None:
        with self._txn() as cur:
            cur.execute(
                """
                INSERT INTO cursors (name, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (name, str(value), int(time.time())),
            )

    # --- Query (for tests / debugging) --------------------------------------

    def get_record(self, source: str, message_id: str) -> Optional[dict[str, Any]]:
        with self._txn() as cur:
            row = cur.execute(
                "SELECT * FROM processed_messages WHERE source=? AND message_id=?",
                (source, message_id),
            ).fetchone()
        return dict(row) if row else None


def _chmod_secret(path: Path) -> None:
    """Restrict a sensitive file to owner-only (0o600) on POSIX.

    Best-effort: silently ignored on filesystems that don't support chmod.
    """
    import os

    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        log.debug("Could not chmod %s (continuing): ignored", path)
