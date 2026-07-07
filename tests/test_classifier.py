"""Classifier tests — replay sample emails, assert verdicts, mock the LLM.

Acceptance criteria:
- recruiter -> opportunity
- agency-spam -> NOT opportunity
- interview -> HIGH urgency
- LinkedIn fixture -> source="linkedin"
- malformed model output is handled without dropping the message
  (retry once, then fail safe to manual review).
"""

from __future__ import annotations

import json

import pytest

from src.agents.classifier import (
    ClassificationVerdict,
    ManualReviewNeededError,
    _parse_verdict,
    classify,
)
from src.config import Config, load_config
from tests.sample_emails import by_id


# --- Test config helper -----------------------------------------------------

def make_test_config() -> Config:
    """Construct a Config without relying on a .env file.

    Sets the minimum required env vars then calls load_config, which tolerates
    a missing .env (it no-ops), so this works in CI.
    """
    import os
    os.environ.setdefault("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
    os.environ.setdefault("DRAFTER_MODEL", "claude-sonnet-4-5-20250929")
    os.environ.setdefault("NOTIFY_CHANNEL", "ntfy")
    os.environ.setdefault("NTFY_TOPIC", "test-topic")
    return load_config()


# --- Fake LLM ---------------------------------------------------------------

class FakeLLM:
    """Returns canned classifier JSON keyed off the user prompt content."""

    REC      = {"is_job_opportunity": True,  "category": "recruiter_outreach",  "source": "email",    "confidence": 0.9, "urgency": "medium", "summary": "Recruiter with a named ML role"}
    INTER    = {"is_job_opportunity": True,  "category": "interview_invite",    "source": "email",    "confidence": 0.95,"urgency": "high",   "summary": "Interview invite"}
    LINKEDIN = {"is_job_opportunity": True,  "category": "recruiter_outreach",  "source": "linkedin", "confidence": 0.85,"urgency": "medium", "summary": "LinkedIn InMail"}
    NETWORK  = {"is_job_opportunity": True,  "category": "networking",          "source": "email",    "confidence": 0.8, "urgency": "low",    "summary": "Warm intro"}
    SPAM     = {"is_job_opportunity": False, "category": "job_alert_digest",    "source": "email",    "confidence": 0.9, "urgency": "low",    "summary": "Mass agency blast"}
    DIGEST   = {"is_job_opportunity": False, "category": "job_alert_digest",    "source": "email",    "confidence": 0.95,"urgency": "low",    "summary": "Job alert digest"}
    REJECT   = {"is_job_opportunity": False, "category": "rejection",           "source": "email",    "confidence": 0.95,"urgency": "low",    "summary": "Rejection"}
    NONJOB   = {"is_job_opportunity": False, "category": "not_job_related",     "source": "email",    "confidence": 0.9, "urgency": "low",    "summary": "Not job related"}

    def __init__(self):
        self.calls = 0

    def complete(self, config, role, system, user, json_mode=False, max_tokens=None):
        self.calls += 1
        assert role == "classifier"
        body = user.lower()
        # Order matters: check negative/specific signals first so a rejection
        # (mentions "interview") and a digest (mentions "linkedin") aren't
        # false-positive'd into opportunities.
        if "account summary" in body or "bank" in body:
            v = self.NONJOB
        elif "not moving forward" in body or "regret" in body or "unfortunately" in body:
            v = self.REJECT
        elif "jobs you may be interested" in body or "job alert" in body:
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
        return json.dumps(v)


# --- Acceptance tests -------------------------------------------------------

def test_recruiter_is_opportunity():
    v = classify(make_test_config(), by_id("msg-recruiter-001"), llm_complete=FakeLLM().complete)
    assert isinstance(v, ClassificationVerdict)
    assert v.is_job_opportunity is True
    assert v.category == "recruiter_outreach"


def test_agency_spam_not_opportunity():
    v = classify(make_test_config(), by_id("msg-agency-spam-001"), llm_complete=FakeLLM().complete)
    assert v.is_job_opportunity is False


def test_interview_is_high_urgency():
    v = classify(make_test_config(), by_id("msg-interview-001"), llm_complete=FakeLLM().complete)
    assert v.is_job_opportunity is True
    assert v.urgency == "high"


def test_linkedin_fixture_source_is_linkedin():
    v = classify(make_test_config(), by_id("msg-linkedin-inmail-001"), llm_complete=FakeLLM().complete)
    assert v.source == "linkedin"


def test_job_digest_not_opportunity():
    v = classify(make_test_config(), by_id("msg-job-digest-001"), llm_complete=FakeLLM().complete)
    assert v.is_job_opportunity is False
    assert v.category == "job_alert_digest"


def test_rejection_not_opportunity():
    v = classify(make_test_config(), by_id("msg-rejection-001"), llm_complete=FakeLLM().complete)
    assert v.is_job_opportunity is False
    assert v.category == "rejection"


def test_networking_is_opportunity():
    v = classify(make_test_config(), by_id("msg-networking-001"), llm_complete=FakeLLM().complete)
    assert v.is_job_opportunity is True
    assert v.category == "networking"


# --- Malformed-output handling (fail safe, never drop) ----------------------

class _AlwaysMalformed:
    def __init__(self):
        self.calls = 0

    def complete(self, config, role, system, user, json_mode=False, max_tokens=None):
        self.calls += 1
        return "I think this is a recruiter email about a job."  # prose, no JSON


class _MalformedThenValid:
    def __init__(self):
        self.calls = 0

    def complete(self, config, role, system, user, json_mode=False, max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            return "Sorry, here's my answer: ```json\n{not valid}\n```"
        return json.dumps(FakeLLM.REC)


def test_malformed_output_retries_then_fails_safe():
    llm = _AlwaysMalformed()
    with pytest.raises(ManualReviewNeededError):
        classify(make_test_config(), by_id("msg-recruiter-001"), llm_complete=llm.complete)
    # Exactly two attempts: initial + one retry.
    assert llm.calls == 2


def test_malformed_then_valid_recovers():
    llm = _MalformedThenValid()
    v = classify(make_test_config(), by_id("msg-recruiter-001"), llm_complete=llm.complete)
    assert v.is_job_opportunity is True
    assert llm.calls == 2


def test_fenced_json_is_parsed():
    """Some models wrap JSON in ```json fences; we must still parse it."""
    raw = (
        '```json\n{"is_job_opportunity": true, "category": "recruiter_outreach", '
        '"source": "email", "confidence": 0.9, "urgency": "medium", "summary": "ok"}\n```'
    )
    v = _parse_verdict(raw)
    assert v.is_job_opportunity is True


def test_pydantic_rejects_extra_fields():
    from pydantic import ValidationError

    raw = (
        '{"is_job_opportunity": true, "category": "recruiter_outreach", "source": "email", '
        '"confidence": 0.9, "urgency": "medium", "summary": "ok", "extra": "boom"}'
    )
    with pytest.raises(ValidationError):
        _parse_verdict(raw)
