"""Drafter + notifier regression tests.

Covers:
- The em-dash ban: _strip_em_dashes removes em-dash and en-dash characters.
- ntfy header safety: _latin1_safe makes any title latin-1 encodable so the
  requests/urllib3 stack never raises UnicodeEncodeError on a header.
"""

from __future__ import annotations

from src.agents.drafter import _strip_em_dashes
from src.notifiers.ntfy import _latin1_safe


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
    # An em-dash in a title used to crash the ntfy header send.
    out = _latin1_safe("Career Jarvis \u2014 error")
    # Must be latin-1 encodable (the whole point) and contain no em-dash.
    out.encode("latin-1")  # raises if not encodable; this line is the assertion
    assert "\u2014" not in out
