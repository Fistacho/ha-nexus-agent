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
