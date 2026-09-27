"""Regression tests for audit findings F27-F30 (zones, energy, calendar).

Verified against home-assistant/core (dev branch):

* F27 ``tools/zones.py`` ``list_persons_in_zone`` — matched a person's state
  string against the zone's ``friendly_name``. That works for custom zones
  (``device_tracker``/``legacy.py`` sets a GPS-tracked device's state to
  ``zone_state.name``, i.e. the zone's friendly_name, when it enters a
  non-home zone) but NOT for ``zone.home``: HA hardcodes the person/
  device_tracker state to ``STATE_HOME`` ("home") for the home zone
  regardless of its ``friendly_name`` (``device_tracker/legacy.py``:
  ``elif zone_state.entity_id == zone.ENTITY_ID_HOME: self._state =
  STATE_HOME``). A `zone.home` whose `friendly_name` is e.g. "Dom" therefore
  always returned `[]`. The zone entity itself already tracks membership
  authoritatively via the `persons` attribute (`components/zone/__init__.py`:
  `ZoneEntityStateAttribute.PERSONS: sorted(self._persons_in_zone)`,
  populated from each person's `in_zones` attribute) — use that instead of
  re-deriving state-string matching.

* F28 ``tools/zones.py`` create/update/delete — called WS
  `config/zone/create` / `config/zone/update` / `config/zone/delete`, which
  do not exist. The zone storage collection is wired up as
  ``collection.DictStorageCollectionWebsocket(storage_collection, DOMAIN,
  DOMAIN, CREATE_FIELDS, UPDATE_FIELDS)`` (``components/zone/__init__.py``),
  and ``StorageCollectionWebsocket`` (``helpers/collection.py``) registers
  its commands as ``f"{api_prefix}/create"`` / ``.../update`` / ``.../delete``
  with ``api_prefix = DOMAIN = "zone"`` — i.e. plain `zone/create`,
  `zone/update`, `zone/delete` (also `zone/list`, `zone/subscribe`).

* F29 ``tools/energy.py`` ``save_energy_prefs`` — sent `currency` and
  `energy_per_unit`, neither of which is a field of the `energy/save_prefs`
  websocket schema (``components/energy/websocket_api.py``: the schema only
  accepts `energy_sources` and `device_consumption`, plus
  `device_consumption_water`, none of which is `currency`/`energy_per_unit`).
  Currency is core config (`homeassistant.currency`), not an Energy Dashboard
  pref.

* F30 ``tools/calendar.py`` ``delete_event`` — always reported
  "not implemented", but HA registers WS `calendar/event/delete`
  (``components/calendar/__init__.py``) with schema
  `{entity_id, uid, recurrence_id?, recurrence_range?}`, calling
  `entity.async_delete_event()` when the calendar platform declares
  `CalendarEntityFeature.DELETE_EVENT`.
"""
import ha_client as ha
from tools import calendar as calendar_tools
from tools import energy as energy_tools
from tools import zones as zones_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


# --- F27: list_persons_in_zone -----------------------------------------------

def _state(entity_id, state, attributes=None):
    return {"entity_id": entity_id, "state": state, "attributes": attributes or {}}


def test_list_persons_in_zone_home_uses_persons_attribute_not_friendly_name(monkeypatch):
    """`zone.home` person state is "home", not its friendly_name ("Dom")."""
    states = [
        _state(
            "zone.home",
            1,
            {"friendly_name": "Dom", "persons": ["person.lukasz"]},
        ),
        _state(
            "person.lukasz",
            "home",
            {"friendly_name": "Lukasz", "latitude": 52.1, "longitude": 21.0},
        ),
    ]
    monkeypatch.setattr(ha, "get_states", lambda: states)

    result = _unwrap(zones_tools.list_persons_in_zone)("zone.home")

    assert [p["entity_id"] for p in result] == ["person.lukasz"]


def test_list_persons_in_zone_custom_zone_still_works(monkeypatch):
    """Custom (non-home) zones: person state is set to the zone's friendly_name."""
    states = [
        _state("zone.work", 1, {"friendly_name": "Biuro", "persons": ["person.lukasz"]}),
        _state("person.lukasz", "Biuro", {"friendly_name": "Lukasz"}),
    ]
    monkeypatch.setattr(ha, "get_states", lambda: states)

    result = _unwrap(zones_tools.list_persons_in_zone)("zone.work")

    assert [p["entity_id"] for p in result] == ["person.lukasz"]


def test_list_persons_in_zone_empty_when_nobody_present(monkeypatch):
    states = [
        _state("zone.home", 0, {"friendly_name": "Dom", "persons": []}),
        _state("person.lukasz", "not_home", {"friendly_name": "Lukasz"}),
    ]
    monkeypatch.setattr(ha, "get_states", lambda: states)

    assert _unwrap(zones_tools.list_persons_in_zone)("zone.home") == []


def test_list_persons_in_zone_falls_back_without_persons_attribute(monkeypatch):
    """Defensive fallback for a zone state with no `persons` attribute at all."""
    states = [
        _state("zone.home", 1, {"friendly_name": "Dom"}),  # no `persons` key
        _state("person.lukasz", "home", {"friendly_name": "Lukasz"}),
    ]
    monkeypatch.setattr(ha, "get_states", lambda: states)

    result = _unwrap(zones_tools.list_persons_in_zone)("zone.home")

    assert [p["entity_id"] for p in result] == ["person.lukasz"]


# --- F28: zone create/update/delete WS commands ------------------------------

def test_create_zone_uses_zone_create_command(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {"zone_id": "work"})

    _unwrap(zones_tools.create_zone)("Work", 52.1, 21.0)

    assert calls[0][0] == "zone/create"


def test_update_zone_uses_zone_update_command(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {})

    _unwrap(zones_tools.update_zone)("zone.work", radius=200)

    assert calls[0][0] == "zone/update"
    assert calls[0][1] == {"zone_id": "work", "radius": 200}


def test_delete_zone_uses_zone_delete_command(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {})

    _unwrap(zones_tools.delete_zone)("zone.work")

    assert calls[0][0] == "zone/delete"
    assert calls[0][1] == {"zone_id": "work"}


# --- F29: save_energy_prefs schema -------------------------------------------

def test_save_energy_prefs_does_not_send_currency_or_energy_per_unit(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {})

    result = _unwrap(energy_tools.save_energy_prefs)(currency="PLN", energy_per_unit=0.8)

    assert calls == []
    assert "error" in result


def test_save_energy_prefs_still_saves_supported_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {"status": "ok"})

    result = _unwrap(energy_tools.save_energy_prefs)(energy_sources=[{"type": "grid"}])

    assert calls[0][0] == "energy/save_prefs"
    assert calls[0][1] == {"energy_sources": [{"type": "grid"}]}
    assert "currency" not in calls[0][1]
    assert "energy_per_unit" not in calls[0][1]
    assert result["status"] == "saved"


def test_save_energy_prefs_rejects_currency_even_with_valid_fields(monkeypatch):
    """currency/energy_per_unit are deprecated no-ops that must not be silently dropped."""
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {})

    result = _unwrap(energy_tools.save_energy_prefs)(energy_sources=[{"type": "grid"}], currency="PLN")

    assert calls == []
    assert "error" in result


# --- F30: calendar delete_event -----------------------------------------------

def test_delete_event_uses_calendar_event_delete_command(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {"success": True})

    result = _unwrap(calendar_tools.delete_event)("calendar.family", "abc123")

    assert calls[0][0] == "calendar/event/delete"
    assert calls[0][1] == {"entity_id": "calendar.family", "uid": "abc123"}
    assert result["ok"] is True


def test_delete_event_passes_recurrence_params_when_given(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {})

    _unwrap(calendar_tools.delete_event)(
        "calendar.family", "abc123", recurrence_id="20260101T000000", recurrence_range="THISANDFUTURE"
    )

    assert calls[0][1] == {
        "entity_id": "calendar.family",
        "uid": "abc123",
        "recurrence_id": "20260101T000000",
        "recurrence_range": "THISANDFUTURE",
    }


def test_delete_event_reports_error_when_calendar_does_not_support_delete(monkeypatch):
    def fake_ws_call(msg_type, **kwargs):
        raise RuntimeError("WS error: {'code': 'not_supported', 'message': 'Calendar does not support deleting events'}")

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(calendar_tools.delete_event)("calendar.family", "abc123")

    assert result["ok"] is False
    assert "not_supported" in result["error"] or "not support" in result["error"]
