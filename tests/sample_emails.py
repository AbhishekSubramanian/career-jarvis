"""Sample email fixtures for testing the classifier and the dry-run pipeline.

Each fixture is an EmailRecord mimicking what gmail_client would produce.
Covers: real recruiter, interview invite, LinkedIn InMail notification,
agency spam, job-board digest, rejection, networking intro, non-job mail.

Bodies are realistic but synthetic. Sender addresses are fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `src` importable when tests are run as a module from the project root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.gmail_client import EmailRecord  # noqa: E402


SAMPLE_EMAILS: list[EmailRecord] = [
    # 1. Real recruiter outreach — opportunity, medium urgency
    EmailRecord(
        id="msg-recruiter-001",
        thread_id="thread-recruiter-001",
        sender="Jane Whitfield <jane.whitfield@northstar-ai.com>",
        subject="ML Engineer role — Agentic AI platform (NYC / remote)",
        body=(
            "Hi Abhishek,\n\n"
            "I'm Jane, a recruiter at NorthStar AI. We're building a multi-agent "
            "orchestration platform and your LangGraph + evaluation-flywheel "
            "background looks like a great fit for a Senior ML Engineer role on "
            "the team that owns it. The role is NYC-based with remote flexibility.\n\n"
            "Would you be open to a quick call this week to hear more?\n\n"
            "Best,\n"
            "Jane Whitfield | NorthStar AI"
        ),
        source="email",
        date="Mon, 6 Jul 2026 14:22:00 -0500",
    ),

    # 2. Interview invite — opportunity, HIGH urgency
    EmailRecord(
        id="msg-interview-001",
        thread_id="thread-interview-001",
        sender="Interview Scheduling <scheduling@northstar-ai.com>",
        subject="Interview confirmed: ML Engineer onsite — Wednesday 2pm ET",
        body=(
            "Hi Abhishek,\n\n"
            "Your onsite interview for the Senior ML Engineer role is confirmed "
            "for Wednesday at 2:00pm ET (4 loops, 45 min each). Please confirm "
            "this slot still works for you; reply if you need to reschedule.\n\n"
            "We'll send a calendar invite shortly.\n\n"
            "— Northstar AI Recruiting"
        ),
        source="email",
        date="Tue, 7 Jul 2026 09:10:00 -0500",
    ),

    # 3. LinkedIn InMail notification email — opportunity, source=linkedin
    EmailRecord(
        id="msg-linkedin-inmail-001",
        thread_id="thread-linkedin-001",
        sender="LinkedIn <messages-noreply@linkedin.com>",
        subject="You have a new message from Dev Patel on LinkedIn",
        body=(
            "View this message on LinkedIn:\n\n"
            "From: Dev Patel, Head of AI at Cobalt Labs\n\n"
            "Hi Abhishek — saw your work on agent evaluation flywheels. We're "
            "hiring a founding engineer for our agentic-AI team (SF, with "
            "equity). Would love to chat if you're open.\n\n"
            "Go to LinkedIn to reply: https://www.linkedin.com/messaging/thread/abc123"
        ),
        source="email",
        date="Tue, 7 Jul 2026 11:30:00 -0500",
    ),

    # 4. Agency spam — NOT an opportunity
    EmailRecord(
        id="msg-agency-spam-001",
        thread_id="thread-spam-001",
        sender="Talent Desk <sourcing@talentdesk-agency.com>",
        subject="Urgent: matching candidates for premium client — shortlists close soon",
        body=(
            "Hi Candidate,\n\n"
            "We have a premium client looking to fill roles ASAP. Shortlists "
            "close soon and we're prioritizing candidates who respond promptly. "
            "Please reply with your updated resume, current compensation, and "
            "availability, and we'll share role specifics after we review.\n\n"
            "Talent Desk — your trusted staffing partner"
        ),
        source="email",
        date="Tue, 7 Jul 2026 06:02:00 -0500",
    ),

    # 5. LinkedIn job alert digest — NOT an opportunity
    EmailRecord(
        id="msg-job-digest-001",
        thread_id="thread-digest-001",
        sender="LinkedIn Jobs <jobs-noreply@linkedin.com>",
        subject="Jobs you may be interested in — 12 new ML Engineer roles",
        body=(
            "Based on your profile, here are jobs you may be interested in:\n\n"
            "- ML Engineer at Acme Corp (Austin)\n"
            "- Senior ML Engineer at Beta Inc (Remote)\n"
            "- Applied Scientist at Gamma Labs (Seattle)\n"
            "... 9 more\n\n"
            "View all on LinkedIn."
        ),
        source="email",
        date="Tue, 7 Jul 2026 05:00:00 -0500",
    ),

    # 6. Rejection — NOT an opportunity
    EmailRecord(
        id="msg-rejection-001",
        thread_id="thread-rejection-001",
        sender="Careers <careers@somecompany.com>",
        subject="Update on your application — Senior ML Engineer",
        body=(
            "Hi Abhishek,\n\n"
            "Thank you for taking the time to interview with us. Unfortunately "
            "we're not moving forward with your application at this time. We "
            "appreciate your interest and wish you the best.\n\n"
            "— Some Company Recruiting"
        ),
        source="email",
        date="Mon, 6 Jul 2026 17:45:00 -0500",
    ),

    # 7. Networking intro from a fellow Hokie — opportunity (networking)
    EmailRecord(
        id="msg-networking-001",
        thread_id="thread-networking-001",
        sender="Rahul Mehta <rahul.mehta@alumni.vt.edu>",
        subject="Fellow Hokie — intro + quick chat?",
        body=(
            "Hi Abhishek,\n\n"
            "Fellow Hokie here (VT MS CS '23). A mutual connection pointed me "
            "your way — I'm now at a small agentic-AI startup and would love a "
            "20-min chat to compare notes. No pressure, just a warm intro.\n\n"
            "Rahul"
        ),
        source="email",
        date="Tue, 7 Jul 2026 12:40:00 -0500",
    ),

    # 8. Non-job mail — NOT job related
    EmailRecord(
        id="msg-nonjob-001",
        thread_id="thread-nonjob-001",
        sender="Chase Bank <no-reply@chase.com>",
        subject="Your July account summary is available",
        body=(
            "Your account summary for July 2026 is now available. "
            "Log in to view your statement and recent transactions."
        ),
        source="email",
        date="Tue, 7 Jul 2026 03:00:00 -0500",
    ),
]


def by_id(mid: str) -> EmailRecord:
    for e in SAMPLE_EMAILS:
        if e.id == mid:
            return e
    raise KeyError(mid)
