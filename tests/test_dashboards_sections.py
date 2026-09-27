"""Tests for section-aware Lovelace editing.

Background: a view with ``type: sections`` renders only ``view.sections``.
Cards placed in ``view.cards`` show up exclusively in edit mode, inside an
"imported cards" block — verified in home-assistant/frontend
``src/panels/lovelace/views/hui-sections-view.ts``. So ``add_card_to_view``
cannot put a card on a sections view, and ``save_dashboard_config`` overwrites
the whole dashboard, which makes hand-editing a 16-view config unsafe.

These tools do the read-modify-write server-side, preserving every other view.
"""
import copy

import pytest

import ha_client as ha
from tools import dashboards as dash


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


def _dashboard() -> dict:
    return {
        "title": "Dom",
        "views": [
            {
                "title": "Łukasz 2",
                "path": "lukasz2",
                "type": "sections",
                "max_columns": 2,
                "sections": [
                    {"cards": [{"type": "heading", "heading": "Wejście"}]},
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Nowa sekcja"},
                            {"type": "tile", "entity": "switch.bieznia_socket"},
                        ],
                    },
                ],
                "cards": [],
            },
            {
                "title": "Rolety2",
                "path": "rolety2",
                "type": "masonry",
                "cards": [{"type": "tile", "entity": "cover.x"}],
            },
        ],
    }


@pytest.fixture
def ws(monkeypatch):
    """Fake WS transport. Returns a recorder with .calls and .saved."""

    class Recorder:
        def __init__(self):
            self.config = _dashboard()
            self.calls = []
            self.saved = None
            self.save_kwargs = None

        def __call__(self, msg_type, **kwargs):
            self.calls.append((msg_type, kwargs))
            if msg_type == "lovelace/config":
                if kwargs.get("url_path"):
                    # Mirrors HA: a view path is not a dashboard.
                    raise RuntimeError("config_not_found: unknown dashboard")
                return copy.deepcopy(self.config)
            if msg_type == "lovelace/config/save":
                self.saved = kwargs["config"]
                self.save_kwargs = kwargs
                # Mirrors HA: subsequent reads see what was just persisted —
                # required so `_save_and_hash`'s post-save re-read (ADR-0003
                # D2) reflects the mutation instead of the fixture's original
                # snapshot.
                self.config = copy.deepcopy(kwargs["config"])
                return None
            raise AssertionError(f"unexpected WS command: {msg_type}")

    rec = Recorder()
    monkeypatch.setattr(ha, "_ws_call", rec)
    return rec


# --- add_card_to_section -----------------------------------------------------

def test_add_card_to_section_appends_to_the_targeted_section(ws):
    card = {"type": "tile", "entity": "sensor.grzalka_moc"}
    result = _unwrap(dash.add_card_to_section)(None, 1, card, view_index=0)

    section = ws.saved["views"][0]["sections"][1]
    assert section["cards"][-1] == card
    assert result["card_index"] == 2
    assert result["cards_in_section"] == 3


def test_add_card_to_section_preserves_every_other_view_and_section(ws):
    before = _dashboard()
    _unwrap(dash.add_card_to_section)(None, 1, {"type": "tile"}, view_index=0)

    assert len(ws.saved["views"]) == 2
    assert ws.saved["views"][1] == before["views"][1]
    assert ws.saved["views"][0]["sections"][0] == before["views"][0]["sections"][0]
    assert ws.saved["title"] == "Dom"
    assert ws.saved["views"][0]["max_columns"] == 2


def test_add_card_to_section_accepts_a_view_path_as_url_path(ws):
    """'lukasz2' is a view inside the default dashboard, not a dashboard."""
    _unwrap(dash.add_card_to_section)("lukasz2", 1, {"type": "tile"})

    # Must save the default dashboard — passing url_path='lukasz2' to
    # lovelace/config/save would create a stray dashboard.
    assert "url_path" not in ws.save_kwargs
    assert len(ws.saved["views"][0]["sections"][1]["cards"]) == 3


def test_add_card_to_section_rejects_a_view_that_is_not_sections(ws):
    with pytest.raises(ValueError, match="not a 'sections' view"):
        _unwrap(dash.add_card_to_section)(None, 0, {"type": "tile"}, view_index=1)

    assert ws.saved is None


def test_add_card_to_section_rejects_an_out_of_range_section(ws):
    with pytest.raises(ValueError, match="has 2 sections"):
        _unwrap(dash.add_card_to_section)(None, 5, {"type": "tile"}, view_index=0)

    assert ws.saved is None


def test_add_card_to_section_honours_position(ws):
    card = {"type": "tile", "entity": "sensor.grzalka_moc"}
    _unwrap(dash.add_card_to_section)(None, 1, card, view_index=0, position=0)

    assert ws.saved["views"][0]["sections"][1]["cards"][0] == card


# --- update / remove ---------------------------------------------------------

def test_update_card_in_section_merges_by_default(ws):
    _unwrap(dash.update_card_in_section)(None, 1, 1, {"name": "Grzałka"}, view_index=0)

    card = ws.saved["views"][0]["sections"][1]["cards"][1]
    assert card == {"type": "tile", "entity": "switch.bieznia_socket", "name": "Grzałka"}


def test_update_card_in_section_can_replace(ws):
    _unwrap(dash.update_card_in_section)(
        None, 1, 1, {"type": "gauge", "entity": "sensor.grzalka_moc"}, view_index=0, merge=False
    )

    assert ws.saved["views"][0]["sections"][1]["cards"][1] == {
        "type": "gauge",
        "entity": "sensor.grzalka_moc",
    }


def test_remove_card_from_section_returns_the_removed_card(ws):
    result = _unwrap(dash.remove_card_from_section)(None, 1, 1, view_index=0)

    assert result["removed_card"] == {"type": "tile", "entity": "switch.bieznia_socket"}
    assert len(ws.saved["views"][0]["sections"][1]["cards"]) == 1


# --- sections themselves -----------------------------------------------------

def test_add_section_to_view_defaults_to_a_grid_with_no_cards(ws):
    result = _unwrap(dash.add_section_to_view)(None, {}, view_index=0)

    sections = ws.saved["views"][0]["sections"]
    assert len(sections) == 3
    assert sections[2] == {"type": "grid", "cards": []}
    assert result["section_index"] == 2


def test_add_section_to_view_keeps_supplied_cards(ws):
    section = {"cards": [{"type": "heading", "heading": "Grzałka"}]}
    _unwrap(dash.add_section_to_view)(None, section, view_index=0)

    assert ws.saved["views"][0]["sections"][2]["cards"][0]["heading"] == "Grzałka"
    assert ws.saved["views"][0]["sections"][2]["type"] == "grid"


def test_get_view_sections_reports_indexes_and_headings(ws):
    result = _unwrap(dash.get_view_sections)("lukasz2")

    assert result["view_index"] == 0
    assert result["view_title"] == "Łukasz 2"
    assert result["sections"] == [
        {"section_index": 0, "heading": "Wejście", "card_count": 1},
        {"section_index": 1, "heading": "Nowa sekcja", "card_count": 2},
    ]
    assert ws.saved is None


# --- config_hash / expected_config_hash (ADR-0003 D2) ------------------------


def test_config_hash_is_stable_regardless_of_key_order():
    """Canonical JSON (sort_keys=True) means dict key order never changes the hash."""
    a = {"views": [{"b": 1, "a": 2}]}
    b = {"views": [{"a": 2, "b": 1}]}
    assert dash._config_hash(a) == dash._config_hash(b)


def test_config_hash_changes_when_content_changes():
    assert dash._config_hash({"a": 1}) != dash._config_hash({"a": 2})


def test_get_view_sections_returns_a_config_hash(ws):
    result = _unwrap(dash.get_view_sections)("lukasz2")

    assert result["config_hash"] == dash._config_hash(ws.config)
    assert ws.saved is None


def test_add_card_to_section_accepts_a_matching_expected_config_hash(ws):
    current_hash = _unwrap(dash.get_view_sections)(None, view_index=0)["config_hash"]

    result = _unwrap(dash.add_card_to_section)(
        None, 1, {"type": "tile"}, view_index=0, expected_config_hash=current_hash
    )

    assert result["status"] == "added"
    assert ws.saved is not None
    assert result["config_hash"] != current_hash


def test_add_card_to_section_rejects_a_mismatched_expected_config_hash(ws):
    result = _unwrap(dash.add_card_to_section)(
        None, 1, {"type": "tile"}, view_index=0, expected_config_hash="0" * 16
    )

    assert result == {
        "error": "config_changed",
        "message": (
            "The dashboard changed since expected_config_hash was read; "
            "section_index/card_index positions may now point at different cards."
        ),
        "expected_config_hash": "0" * 16,
        "current_config_hash": dash._config_hash(ws.config),
        "action": "Call dashboards_get_view_sections again and re-derive indexes.",
    }
    # No save was attempted — the fixture's config is untouched.
    assert ws.saved is None


def test_update_card_in_section_rejects_a_mismatched_expected_config_hash(ws):
    result = _unwrap(dash.update_card_in_section)(
        None, 1, 1, {"name": "Grzałka"}, view_index=0, expected_config_hash="0" * 16
    )

    assert result["error"] == "config_changed"
    assert ws.saved is None


def test_add_section_to_view_rejects_a_mismatched_expected_config_hash(ws):
    result = _unwrap(dash.add_section_to_view)(
        None, {}, view_index=0, expected_config_hash="0" * 16
    )

    assert result["error"] == "config_changed"
    assert ws.saved is None


def test_remove_card_from_section_rejects_a_repeat_with_the_same_stale_hash(ws, monkeypatch):
    """ADR-0003 D2 regression: get_view_sections -> remove_card_from_section
    (expected_config_hash=h) -> repeating with the SAME h must be rejected,
    not silently remove whatever card shifted into that position next."""
    stale_hash = _unwrap(dash.get_view_sections)("lukasz2")["config_hash"]

    first = _unwrap(dash.remove_card_from_section)(
        None, 1, 1, view_index=0, expected_config_hash=stale_hash
    )
    assert first["status"] == "removed"
    assert first["config_hash"] != stale_hash
    assert len(ws.saved["views"][0]["sections"][1]["cards"]) == 1

    def _no_write(msg_type, **kwargs):
        if msg_type == "lovelace/config/save":
            raise AssertionError("must not save when expected_config_hash is stale")
        return ws(msg_type, **kwargs)

    monkeypatch.setattr(ha, "_ws_call", _no_write)

    second = _unwrap(dash.remove_card_from_section)(
        None, 1, 1, view_index=0, expected_config_hash=stale_hash
    )

    assert second == {
        "error": "config_changed",
        "message": (
            "The dashboard changed since expected_config_hash was read; "
            "section_index/card_index positions may now point at different cards."
        ),
        "expected_config_hash": stale_hash,
        "current_config_hash": first["config_hash"],
        "action": "Call dashboards_get_view_sections again and re-derive indexes.",
    }
    # The repeat touched nothing — still just the one card the first call left.
    assert len(ws.saved["views"][0]["sections"][1]["cards"]) == 1


def test_add_card_to_section_hash_reflects_a_fresh_read_not_the_local_copy(monkeypatch):
    """If HA normalizes what it persists (e.g. adds/reorders a field), the
    returned config_hash must come from re-reading lovelace/config after the
    save, not from hashing the dict this process built locally (ADR-0003 D2,
    the "HA may normalize the saved config" acceptance criterion)."""
    state = {"config": _dashboard()}

    def fake_ws_call(msg_type, **kwargs):
        if msg_type == "lovelace/config":
            if kwargs.get("url_path"):
                raise RuntimeError("config_not_found: unknown dashboard")
            return copy.deepcopy(state["config"])
        if msg_type == "lovelace/config/save":
            normalized = copy.deepcopy(kwargs["config"])
            normalized["_ha_normalized"] = True  # simulates HA-side normalization
            state["config"] = normalized
            return None
        raise AssertionError(f"unexpected WS command: {msg_type}")

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(dash.add_card_to_section)(None, 1, {"type": "tile"}, view_index=0)

    assert result["config_hash"] == dash._config_hash(state["config"])
    local_only = copy.deepcopy(state["config"])
    del local_only["_ha_normalized"]
    assert result["config_hash"] != dash._config_hash(local_only)
