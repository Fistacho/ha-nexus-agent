"""Regression tests requested by the Security review follow-up to ADR-0006
(2026-09-28, "MUST z tej samej klasy co F3"): a malformed `entity_id`/
`automation_id`/`script_id` must be rejected with zero HTTP requests, at
both the `ha_client` layer and the MCP tools that call it, and the tools
with a broad `except Exception` (`entities_bulk_set_state`) must still
surface a readable `{"error": ...}` per item instead of an unhandled
exception escaping the batch.
"""
from __future__ import annotations

import pytest

import ha_client as ha
from tools import automations as automations_tools
from tools import entities as entities_tools

get_entity = getattr(entities_tools.get_entity, "fn", entities_tools.get_entity)
bulk_set_state = getattr(entities_tools.bulk_set_state, "fn", entities_tools.bulk_set_state)
set_automation_config = getattr(
    automations_tools.set_automation_config, "fn", automations_tools.set_automation_config
)
delete_automation = getattr(automations_tools.delete_automation, "fn", automations_tools.delete_automation)


class _BoomClient:
    def __enter__(self):
        raise AssertionError("no HTTP client should be constructed for a malformed path segment")

    def __exit__(self, *exc):
        return False


_MALICIOUS_ENTITY_IDS = [
    "../services/hassio/host_shutdown",
    "light.kitchen/../../hassio/addon_stop",
    "light.kitchen?x=1",
    "light.kitchen#frag",
    "%2e%2e/states/light.kitchen",
]

_MALICIOUS_IDENTIFIERS = [
    "../services/hassio/host_shutdown",
    "1?x=1",
    "1#frag",
    "%2e%2e",
    "%2e%2e%2fhassio%2faddon_stop",
]


# ---------------------------------------------------------------------------
# ha_client layer — direct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity_id", _MALICIOUS_ENTITY_IDS)
def test_ha_client_get_state_rejects_malicious_entity_id_without_io(monkeypatch, entity_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        ha.get_state(entity_id)


@pytest.mark.parametrize("entity_id", _MALICIOUS_ENTITY_IDS)
def test_ha_client_set_state_rejects_malicious_entity_id_without_io(monkeypatch, entity_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        ha.set_state(entity_id, "on")


@pytest.mark.parametrize("automation_id", _MALICIOUS_IDENTIFIERS)
def test_ha_client_automation_config_crud_rejects_malicious_id_without_io(monkeypatch, automation_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        ha.get_automation_config(automation_id)
    with pytest.raises(ValueError):
        ha.set_automation_config(automation_id, {"alias": "x", "trigger": []})
    with pytest.raises(ValueError):
        ha.delete_automation_config(automation_id)


@pytest.mark.parametrize("script_id", _MALICIOUS_IDENTIFIERS)
def test_ha_client_script_config_crud_rejects_malicious_id_without_io(monkeypatch, script_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        ha.get_script_config(script_id)
    with pytest.raises(ValueError):
        ha.set_script_config(script_id, {"sequence": []})
    with pytest.raises(ValueError):
        ha.delete_script_config(script_id)


@pytest.mark.parametrize("event_type", _MALICIOUS_IDENTIFIERS)
def test_ha_client_fire_event_rejects_malicious_event_type_without_io(monkeypatch, event_type):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        ha.fire_event(event_type)


def test_ha_client_get_history_and_logbook_still_work_for_legitimate_calls(monkeypatch):
    """Regression: the defense-in-depth `path_segment` pass over the
    self-computed `start` timestamp must not break a normal call — the
    isoformat() string it always builds contains no path-structural
    character, so nothing here should raise or alter the request."""
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, params=None):
            captured["path"] = path
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setattr(ha, "_client", _FakeClient)

    assert ha.get_history(hours=1) == []
    assert captured["path"].startswith("/api/history/period/")
    for bad in ("/", "?", "#"):
        assert bad not in captured["path"][len("/api/history/period/") :]

    assert ha.get_logbook(hours=1) == []
    assert captured["path"].startswith("/api/logbook/")


# ---------------------------------------------------------------------------
# MCP tool layer — the same payloads through the actual tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity_id", _MALICIOUS_ENTITY_IDS)
def test_get_entity_tool_rejects_malicious_entity_id_without_io(monkeypatch, entity_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        get_entity(entity_id)


def test_bulk_set_state_reports_readable_error_per_item_without_io(monkeypatch):
    """`entities_bulk_set_state`'s broad `except Exception` must turn the
    `ValueError` `ha.set_state` now raises for a malicious `entity_id` into
    a readable per-item `{"ok": False, "error": ...}` dict, not let it
    escape the batch or reach a raw HTTP client."""
    monkeypatch.setattr(ha, "_client", _BoomClient)

    result = bulk_set_state(
        [{"entity_id": "../services/hassio/host_shutdown", "state": "on"}]
    )

    assert len(result) == 1
    assert result[0]["ok"] is False
    assert "error" in result[0] and isinstance(result[0]["error"], str) and result[0]["error"]
    assert result[0]["error_type"] in ("unexpected", "validation")


def test_bulk_set_state_still_works_for_legitimate_entity_ids(monkeypatch):
    calls = []

    def _fake_set_state(entity_id, state, attributes=None):
        calls.append((entity_id, state, attributes))
        return {"ok": True}

    monkeypatch.setattr(ha, "set_state", _fake_set_state)

    result = bulk_set_state([{"entity_id": "light.kitchen", "state": "on"}])

    assert result == [{"entity_id": "light.kitchen", "ok": True}]
    assert calls == [("light.kitchen", "on", None)]


@pytest.mark.parametrize("automation_id", _MALICIOUS_IDENTIFIERS)
def test_set_automation_config_tool_rejects_malicious_id_without_io(monkeypatch, automation_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        set_automation_config(automation_id, {"alias": "x", "trigger": []})


@pytest.mark.parametrize("automation_id", _MALICIOUS_IDENTIFIERS)
def test_delete_automation_tool_rejects_malicious_id_without_io(monkeypatch, automation_id):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        delete_automation(automation_id, confirm=True)
