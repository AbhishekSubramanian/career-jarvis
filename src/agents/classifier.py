"""Classifier agent — triage every ingested message into a validated verdict.

Contract:
  - Schema-validated JSON via Pydantic v2, temperature=0, strict system prompt.
  - On malformed output: retry once, then fail safe by flagging for manual
    review (NEVER silently drop).
  - Conservative: automated job-board digests and mass newsletters are NOT
    opportunities; a named human writing about a specific role IS.
  - Apply scam/spam heuristics from career_profile.md.

The verdict is the single source of truth the orchestrator uses to decide
whether to draft + notify.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, Field

from ..config import Config
from ..gmail_client import EmailRecord
from ..llm import LLMError, complete

log = logging.getLogger(__name__)

Category = Literal[
    "recruiter_outreach",
    "interview_invite",
    "application_update",
    "networking",
    "job_alert_digest",
    "rejection",
    "not_job_related",
]
Source = Literal["email", "linkedin"]
Urgency = Literal["high", "medium", "low"]


class ClassificationVerdict(BaseModel):
    is_job_opportunity: bool
    category: Category
    source: Source
    confidence: float = Field(ge=0.0, le=1.0)
    urgency: Urgency
    summary: str

    model_config = {"extra": "forbid"}


class ManualReviewNeeded(BaseModel):
    """Fail-safe verdict used when the model output can't be parsed.

    The message is marked as needing manual review and still recorded so it
    can't loop forever, but no draft is produced and no opportunity alert
    fires (an error alert is sent so the user knows to look).
    """
    reason: str


# --- Prompts ---------------------------------------------------------------

SYSTEM_PROMPT = """You are Career Jarvis, an email/LinkedIn triage classifier for a passive job seeker.

Your job: classify an incoming message into EXACTLY this JSON schema:

{{
  "is_job_opportunity": boolean,
  "category": "recruiter_outreach" | "interview_invite" | "application_update" | "networking" | "job_alert_digest" | "rejection" | "not_job_related",
  "source": "email" | "linkedin",
  "confidence": float (0.0..1.0),
  "urgency": "high" | "medium" | "low",
  "summary": string (one line, <=120 chars)
}}

Rules (be CONSERVATIVE — the user is employed and selective):
- is_job_opportunity=true ONLY for messages worth a personal reply:
  a named human writing about a SPECIFIC role (recruiter outreach),
  an interview invite/schedule, an actionable application update that needs
  a response, or a genuine networking intro. Networking from a real named
  person (e.g. a fellow alum) counts.
- is_job_opportunity=false for: automated job-board digests/newsletters,
  mass agency blasts with no specific role, rejections, application
  confirmations that need no reply, and anything not job-related.
- source="linkedin" if the message is a LinkedIn notification/InMail email
  OR clearly a LinkedIn DM. Otherwise source="email".
- urgency="high" for interview invites and time-sensitive offers;
  "medium" for recruiter outreach about a real role; "low" otherwise.
- Spam/scam heuristics (set is_job_opportunity=false, category="not_job_related"
  or "job_alert_digest"): vague agency outreach with pressure tactics
  ("shortlists close soon", "respond promptly"), no company name, no comp,
  no JD, requests to fill out details before sharing role specifics, requests
  for SSN/banking/fees/documents.
- Output ONLY the JSON object. No prose, no markdown fences.

Confidence reflects how sure you are of the category, not how good the
opportunity is."""

USER_PROMPT_TEMPLATE = """Classify this message.

Sender: {sender}
Subject: {subject}
Date: {date}
Body (truncated to 4000 chars):
---
{body}
---

Return the JSON object only."""


def _extract_json(raw: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from a model response.

    Handles: pure JSON, JSON wrapped in ```json fences, and JSON with
    leading/trailing prose (we grab the first {...} block).
    """
    if not raw:
        return None
    raw = raw.strip()
    # Strip code fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Fall back to first balanced {...} block.
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _parse_verdict(raw: str) -> ClassificationVerdict:
    data = _extract_json(raw)
    if data is None:
        raise ValueError("no JSON object found in model output")
    return ClassificationVerdict.model_validate(data)


def classify(
    config: Config,
    message: EmailRecord,
    *,
    llm_complete=complete,
) -> ClassificationVerdict:
    """Classify a message with one retry, then fail safe to manual review.

    The ``llm_complete`` param is for tests (inject a mock). In production it
    defaults to src.llm.complete.
    """
    user_prompt = USER_PROMPT_TEMPLATE.format(
        sender=message.sender or "(unknown)",
        subject=message.subject or "(no subject)",
        date=message.date or "",
        body=(message.body or "")[:4000],
    )

    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            raw = llm_complete(
                config,
                role="classifier",
                system=SYSTEM_PROMPT,
                user=user_prompt,
                json_mode=True,
                max_tokens=300,
            )
        except LLMError as exc:
            last_err = exc
            log.warning("Classifier LLM call failed (attempt %d): %s", attempt, exc)
            continue

        try:
            return _parse_verdict(raw)
        except (ValidationError, ValueError) as exc:
            last_err = exc
            log.warning(
                "Classifier output unparseable (attempt %d): %s | raw=%r",
                attempt, exc, raw[:300],
            )
            continue

    # Fail safe: never silently drop. Raise ManualReviewNeeded so the
    # orchestrator records the message, sends an error alert, and stops.
    reason = f"Classifier could not produce a valid verdict: {last_err}"
    raise ManualReviewNeededError(reason, last_err)


class ManualReviewNeededError(Exception):
    """Raised when the classifier cannot produce a valid verdict after retry.

    Carries the reason; the orchestrator marks the message
    status='manual_review' and emits an error alert.
    """

    def __init__(self, reason: str, cause: Optional[Exception] = None):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause
