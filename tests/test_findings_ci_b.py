"""Regression tests for two ruff findings investigated during the CI-B lint
cleanup (0.22.0), both flagged as *suspected* real bugs and confirmed, after
investigation, to be false positives -- no behaviour change was made beyond
a clarifying/suppressing edit, and these tests lock in that verified-correct
behaviour so a future edit cannot silently reintroduce either shape of bug.

#1 -- ISC004 in tools/card_builder.py (DESIGN_PATTERNS["cover_panel"]["ux_notes"])
  The first ``ux_notes`` entry was written as three adjacent string literals
  with no comma between them, inside a list -- Python's implicit string
  concatenation silently merges them into ONE string, and ISC004 exists
  precisely because that is ambiguous: it could be a bug (a comma was
  meant to be there, producing 3 list items) or intentional multi-sentence
  prose (matching this same file's "description" field style, and the
  "tile_with_state_color" pattern's own multi-sentence single ux_note two
  entries above it). Comparing against git history (the text has read this
  way since it was introduced in v0.14.0, commit 24e1d53) and against
  ruff's own suggested fix (parenthesize only, no text change) confirms the
  latter: it is ONE intentional two-sentence note. The parentheses were
  added to make that explicit and silence ISC004; the two-item list content
  is unchanged.

#2 -- DTZ007 in tools/statistics.py (_parse_dt)
  ``datetime.datetime.strptime(s, fmt)`` looks naive-by-default, which is
  what DTZ007 flags. But the very next line backfills
  ``tzinfo=UTC`` on any naive result, and a caller-supplied offset (the
  third format, with %z) is preserved as-is -- so the function can never
  actually return a naive datetime, for any of its 3 accepted formats.
  The UTC assumption for input with no explicit offset is a deliberate,
  documented design choice (get_statistics()'s docstring: "start/end parsed
  into UTC timestamps"), consistent with the same convention already used
  by ha_client.get_history() for talking to HA's REST/WS APIs. No code
  change beyond a `# noqa: DTZ007` with this justification was made.
"""
from __future__ import annotations

import datetime

from tools import card_builder as cb
import tools.statistics as st


def _unwrap(tool):
    """FastMCP wraps decorated functions -- get the plain callable back."""
    return getattr(tool, "fn", tool)


# ---------------------------------------------------------------------------
# #1 -- DESIGN_PATTERNS["cover_panel"]["ux_notes"] content is exactly 2 items
# ---------------------------------------------------------------------------

def test_cover_panel_ux_notes_has_two_items_not_three():
    """Guards against a missing comma silently merging/splitting list items.

    If a comma were ever added between the first two source lines by
    mistake, this list would silently grow to 3 items and the first one
    would lose the "For Supla/Netatmo..." sentence -- this test pins the
    verified-intentional shape: 2 items, the first spanning both sentences.
    """
    notes = cb.DESIGN_PATTERNS["cover_panel"]["ux_notes"]
    assert len(notes) == 2
    assert notes[0] == (
        "Slider works only if cover supports SET_POSITION (bit 4 of supported_features). "
        "For Supla/Netatmo without it, the slider reads state (open=100, closed=0) and "
        "tap-toggles open/close instead."
    )
    assert notes[1] == "Show position % in the slider value label."


def test_get_design_pattern_tool_returns_the_same_two_ux_notes():
    """Same assertion through the public MCP tool, not just the raw constant."""
    result = _unwrap(cb.get_design_pattern)("cover_panel")
    assert result["ux_notes"] == cb.DESIGN_PATTERNS["cover_panel"]["ux_notes"]
    assert len(result["ux_notes"]) == 2


# ---------------------------------------------------------------------------
# #2 -- statistics._parse_dt never returns a naive datetime, for every
# accepted input shape (date-only, naive datetime, and 3 aware-offset
# spellings), and preserves a caller-supplied offset instead of overriding it.
# ---------------------------------------------------------------------------

def test_parse_dt_never_returns_naive_datetime_for_date_only():
    dt = st._parse_dt("2024-06-15")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)


def test_parse_dt_never_returns_naive_datetime_for_naive_datetime_string():
    dt = st._parse_dt("2024-06-15T10:30:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)
    assert dt.hour == 10 and dt.minute == 30


def test_parse_dt_preserves_caller_supplied_offset_instead_of_overriding_it():
    dt = st._parse_dt("2024-06-15T10:30:00+02:00")
    assert dt.utcoffset() == datetime.timedelta(hours=2)
    # The wall-clock hour supplied by the caller must survive untouched --
    # this would fail if the naive-datetime UTC backfill ever became an
    # unconditional UTC *conversion* instead of a tzinfo-is-None backfill.
    assert dt.hour == 10


def test_parse_dt_accepts_zulu_suffix_as_utc():
    dt = st._parse_dt("2024-06-15T10:30:00Z")
    assert dt.utcoffset() == datetime.timedelta(0)
    assert dt.hour == 10
