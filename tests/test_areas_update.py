"""`areas_update_area` — partial update of an area registry entry (0.28.0).

Only the fields the caller passes may reach WS `config/area_registry/update`;
anything omitted must keep its current value in Home Assistant. Calling the
tool with no field to change is a caller error and must not reach HA at all.
"""
from __future__ import annotations

import pytest

import ha_client as ha
from tools import areas as areas_tools

update_area = getattr(areas_tools.update_area, "fn", areas_tools.update_area)


@pytest.fixture
def ws_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {"area_id": kwargs.get("area_id"), "name": kwargs.get("name", "unchanged")}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)
    return calls


def test_ha_client_update_area_sends_area_id_and_fields(ws_calls):
    ha.update_area("kitchen", name="Kuchnia")

    assert ws_calls == [("config/area_registry/update", {"area_id": "kitchen", "name": "Kuchnia"})]


def test_rename_sends_only_name(ws_calls):
    result = update_area(area_id="kitchen", name="Kuchnia")

    assert ws_calls == [("config/area_registry/update", {"area_id": "kitchen", "name": "Kuchnia"})]
    assert result["name"] == "Kuchnia"


def test_sends_only_the_fields_given(ws_calls):
    update_area(area_id="kitchen", icon="mdi:stove", floor_id="ground")

    assert ws_calls == [
        ("config/area_registry/update", {"area_id": "kitchen", "icon": "mdi:stove", "floor_id": "ground"})
    ]


def test_all_fields_are_forwarded(ws_calls):
    update_area(
        area_id="kitchen",
        name="Kuchnia",
        icon="mdi:stove",
        floor_id="ground",
        aliases=["kuchnia", "aneks"],
    )

    assert ws_calls == [
        (
            "config/area_registry/update",
            {
                "area_id": "kitchen",
                "name": "Kuchnia",
                "icon": "mdi:stove",
                "floor_id": "ground",
                "aliases": ["kuchnia", "aneks"],
            },
        )
    ]


def test_empty_aliases_list_is_sent_to_clear_aliases(ws_calls):
    update_area(area_id="kitchen", aliases=[])

    assert ws_calls == [("config/area_registry/update", {"area_id": "kitchen", "aliases": []})]


def test_no_fields_raises_value_error_without_calling_ha(ws_calls):
    with pytest.raises(ValueError):
        update_area(area_id="kitchen")

    assert ws_calls == []
