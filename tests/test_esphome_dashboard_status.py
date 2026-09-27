"""Tests for nexus 0.22.0 coordinator decisions D4 and item 3 (ESPHome partition).

D4 — no Supervisor ingress route for the ESPHome dashboard (live fact,
2026-09-27: `GET {HA_URL}/api/hassio_ingress/<token>/ping` without a session
-> 401; a Supervisor ingress session isn't scoped to one add-on, rejected by
Panel Security). Instead, `esphome_ping_dashboard` and the dashboard-calling
tools (`compile_device`/`validate_config`/`upload_device`) attach a
`diagnosis` built from the add-on's own `GET /addons/<slug>/info`
(developers.home-assistant.io/docs/api/supervisor/endpoints, confirmed
2026-09-27: response has `network` — a dict of published port mappings,
empty when nothing is published — and `ingress` bool) whenever the
dashboard itself can't be reached, explaining whether port 6052 is mapped
and what to change if not.

Item 3 — device connected status. Live fact (2026-09-27): `list_devices`
returned `"connected": null` for all 7 registered devices; matching on
`binary_sensor.*_api_connection_status` does not reflect reality. Per
`homeassistant/components/esphome/entity.py` (confirmed on GitHub,
2026-09-27), every non-deep-sleep entity's `available` — and thus its
`"unavailable"` state when false — tracks one shared per-config-entry flag,
so `list_devices`/`get_device_entities` now match a device's own entities by
the entity registry's `device_id` and read connectivity off their states.
"""
from __future__ import annotations

from tools import esphome as esphome_tools


def _unwrap(tool):
    return getattr(tool, "fn", tool)


# === D4: Supervisor-based diagnosis when the dashboard is unreachable ===============

def test_ping_dashboard_reachable_has_no_diagnosis(monkeypatch):
    monkeypatch.setattr(esphome_tools, "_dash", lambda method, path, timeout=5: {"ok": True})

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result["reachable"] is True
    assert "diagnosis" not in result


def test_ping_dashboard_unreachable_reports_port_not_mapped(monkeypatch):
    monkeypatch.setattr(
        esphome_tools, "_dash",
        lambda method, path, timeout=5: {"error": f"Cannot connect to ESPHome dashboard at {esphome_tools._DASH_URL}."},
    )

    def fake_sup_json(method, path, body=None, timeout=30):
        if path == "/addons":
            return {"data": {"addons": [{"slug": "5c53de3b_esphome", "state": "started"}]}}
        assert path == "/addons/5c53de3b_esphome/info"
        return {"data": {"network": {}, "ingress": True}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result["reachable"] is False
    diag = result["diagnosis"]
    assert diag["slug"] == "5c53de3b_esphome"
    assert diag["port_6052_mapped"] is False
    assert diag["ingress_only"] is True
    assert "6052" in diag["advice"]


def test_ping_dashboard_unreachable_reports_port_mapped_but_still_unreachable(monkeypatch):
    monkeypatch.setattr(
        esphome_tools, "_dash",
        lambda method, path, timeout=5: {"error": f"Cannot connect to ESPHome dashboard at {esphome_tools._DASH_URL}."},
    )

    def fake_sup_json(method, path, body=None, timeout=30):
        if path == "/addons":
            return {"data": {"addons": [{"slug": "5c53de3b_esphome", "state": "started"}]}}
        return {"data": {"network": {"6052/tcp": 6052}, "ingress": True}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    result = _unwrap(esphome_tools.ping_dashboard)()

    diag = result["diagnosis"]
    assert diag["port_6052_mapped"] is True
    assert diag["ingress_only"] is False


def test_ping_dashboard_unreachable_and_supervisor_unreachable_reports_error(monkeypatch):
    monkeypatch.setattr(
        esphome_tools, "_dash",
        lambda method, path, timeout=5: {"error": f"Cannot connect to ESPHome dashboard at {esphome_tools._DASH_URL}."},
    )
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {"error": "SUPERVISOR_TOKEN not set"},
    )

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result["reachable"] is False
    assert "error" in result["diagnosis"]


def _raise_oserror(*a, **k):
    raise OSError("Connection refused")


def test_compile_device_attaches_diagnosis_on_connect_failure(monkeypatch):
    import websockets

    monkeypatch.setattr(websockets, "connect", _raise_oserror)

    def fake_sup_json(method, path, body=None, timeout=30):
        if path == "/addons":
            return {"data": {"addons": [{"slug": "5c53de3b_esphome", "state": "started"}]}}
        return {"data": {"network": {}, "ingress": True}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    result = _unwrap(esphome_tools.compile_device)("livingroom")

    assert "Cannot connect to ESPHome dashboard" in result["error"]
    assert result["diagnosis"]["port_6052_mapped"] is False


def test_validate_config_attaches_diagnosis_on_connect_failure(monkeypatch):
    import websockets

    monkeypatch.setattr(websockets, "connect", _raise_oserror)
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: (
            {"data": {"addons": [{"slug": "esphome_esphome", "state": "started"}]}}
            if path == "/addons"
            else {"data": {"network": {"6052/tcp": 6052}, "ingress": False}}
        ),
    )

    result = _unwrap(esphome_tools.validate_config)("livingroom")

    assert "Cannot connect to ESPHome dashboard" in result["error"]
    assert result["diagnosis"]["port_6052_mapped"] is True


def test_upload_device_attaches_diagnosis_on_connect_failure(monkeypatch):
    import websockets

    monkeypatch.setattr(websockets, "connect", _raise_oserror)
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: (
            {"data": {"addons": [{"slug": "esphome_esphome", "state": "started"}]}}
            if path == "/addons"
            else {"data": {"network": {}, "ingress": True}}
        ),
    )

    result = _unwrap(esphome_tools.upload_device)("livingroom")

    assert "Cannot connect to ESPHome dashboard" in result["error"]
    assert "diagnosis" in result


def test_compile_device_timeout_does_not_get_a_connect_diagnosis(monkeypatch):
    """A `timeout` error is not a connection failure — no Supervisor call should
    happen, and no misleading `diagnosis` should be attached."""
    import asyncio as _asyncio

    import websockets

    class _NeverExitsWS:
        async def recv(self):
            await _asyncio.sleep(0)
            raise TimeoutError

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, message):
            return None

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _NeverExitsWS())
    monkeypatch.setattr(esphome_tools, "_sup_json", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call Supervisor for a plain timeout")
    ))

    result = _unwrap(esphome_tools.compile_device)("livingroom", timeout=0.01)

    assert result["error"] == "timeout"
    assert "diagnosis" not in result


# === Item 3: device connected status from entity registry + states =================

def _entity(entity_id, device_id, platform="esphome"):
    return {"entity_id": entity_id, "device_id": device_id, "platform": platform}


def test_list_devices_connected_true_when_an_entity_is_available(monkeypatch):
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [_entity("sensor.kitchen_sensor_temperature", "d1")],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [{"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"}],
    )

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is True
    assert result["online"] == 1
    assert result["offline"] == 0


def test_list_devices_connected_false_when_all_entities_unavailable(monkeypatch):
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [
            _entity("sensor.kitchen_sensor_temperature", "d1"),
            _entity("sensor.kitchen_sensor_humidity", "d1"),
        ],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [
            {"entity_id": "sensor.kitchen_sensor_temperature", "state": "unavailable"},
            {"entity_id": "sensor.kitchen_sensor_humidity", "state": "unavailable"},
        ],
    )

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is False
    assert result["online"] == 0
    assert result["offline"] == 1


def test_list_devices_connected_none_when_device_has_no_known_entities(monkeypatch):
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(esphome_tools.ha, "get_entity_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "get_states", list)

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is None
    assert result["online"] == 0
    assert result["offline"] == 0


def test_list_devices_does_not_rely_on_api_connection_status_binary_sensor(monkeypatch):
    """RED against the pre-fix code: a `binary_sensor.*_api_connection_status`
    entity that isn't linked to the device via `device_id` (e.g. it doesn't
    exist at all, matching the live fact) must not affect the result, and a
    real entity that IS linked must still correctly report connected."""
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [_entity("sensor.kitchen_sensor_temperature", "d1")],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [
            {"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"},
            {"entity_id": "binary_sensor.kitchen_sensor_api_connection_status", "state": "off"},
        ],
    )

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is True


def test_get_device_entities_matches_by_device_id_not_substring(monkeypatch):
    """RED against the pre-fix code: slug substring matching would also match
    'sensor' from an unrelated device whose slug happens to contain it."""
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [
            {"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"},
            {"id": "d2", "name": "Kitchen Sensor 2", "manufacturer": "espressif"},
        ],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [
            _entity("sensor.kitchen_sensor_temperature", "d1"),
            _entity("sensor.kitchen_sensor_2_temperature", "d2"),
        ],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [
            {"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"},
            {"entity_id": "sensor.kitchen_sensor_2_temperature", "state": "22.0"},
        ],
    )

    result = _unwrap(esphome_tools.get_device_entities)("Kitchen Sensor")

    assert [e["entity_id"] for e in result] == ["sensor.kitchen_sensor_temperature"]


def test_get_device_entities_falls_back_to_slug_match_without_device_registry(monkeypatch):
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [_entity("sensor.kitchen_sensor_temperature", None)],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [{"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"}],
    )

    result = _unwrap(esphome_tools.get_device_entities)("kitchen_sensor")

    assert len(result) == 1
    assert result[0]["entity_id"] == "sensor.kitchen_sensor_temperature"
