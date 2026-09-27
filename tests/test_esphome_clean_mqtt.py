"""Tests for `esphome_clean_mqtt` (nexus 0.22.0, coordinator decision D3).

Prior behaviour (see `tests/test_audit_discover_esphome_blueprints.py`, F23):
`clean_mqtt` called the legacy ESPHome dashboard's `/clean-mqtt` WebSocket
route, which does not exist in the "ESPHome Device Builder" add-on that
replaced the pip dashboard (confirmed against `docs/API.md` on GitHub
2026-09-27 — no MQTT-topic-clearing command exists there at all).

D3 bypasses the dashboard entirely and goes through HA's own `mqtt`
integration (confirmed against `homeassistant/components/mqtt/__init__.py` +
`debug_info.py` on github.com/home-assistant/core, 2026-09-27):

* No `mqtt:` section in the device YAML -> skipped, zero network calls
  (fact: none of the 7 devices on the live HA instance use MQTT).
* `esphome.name` (with `substitutions:` applied, ESPHome's own `$x`/`${x}`
  syntax, two-pass compound resolution per esphome.io/components/
  substitutions) resolves the MQTT node name; `mqtt.discovery_prefix`
  defaults to `"homeassistant"`.
* A matching HA device-registry entry -> WS `mqtt/device/debug_info`
  (`{device_id: str}` -> `{"entities": [...], "triggers": [...]}`, each with
  its own `discovery_data.topic`) for a precise topic list.
* No matching device -> a short `mqtt/subscribe` on
  `{prefix}/+/{node_name}/#`, collecting retained topics for `timeout`
  seconds (bounded to `_MQTT_DISCOVERY_WINDOW` = 3s).
* Either way, each topic is cleared via the `mqtt.publish` service with
  `payload=""`, `retain=True` — `MQTT_PUBLISH_SCHEMA` accepts `payload=None
  | str` (so `""` validates) and an empty retained publish is the standard
  MQTT mechanism to delete a broker's retained message on a topic.

These tests were RED against the pre-D3 implementation (which tried to open
a WebSocket to `{ESPHOME_DASHBOARD_URL}/clean-mqtt` for every case, ignoring
`mqtt:`/`esphome.name`/`substitutions:` entirely, and never touched
`ha_client` at all) and are GREEN against the D3 rewrite.
"""
from __future__ import annotations

import pytest

from tools import esphome as esphome_tools


def _unwrap(tool):
    return getattr(tool, "fn", tool)


def _write_device(esphome_dir, name: str, content: str):
    esphome_dir.mkdir(parents=True, exist_ok=True)
    (esphome_dir / f"{name}.yaml").write_text(content, encoding="utf-8")


def _no_registry(*a, **k):
    raise AssertionError("must not read the HA device registry")


def _no_ws_call(*a, **k):
    raise AssertionError("must not make any HA WebSocket call")


def _no_ws_collect(*a, **k):
    raise AssertionError("must not make any HA WebSocket subscribe call")


def _no_publish(*a, **k):
    raise AssertionError("must not call any HA service")


@pytest.fixture(autouse=True)
def _esphome_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    return tmp_path


# --- no mqtt: section -> skip, zero network calls -----------------------------------

def test_clean_mqtt_skips_when_no_mqtt_section(_esphome_dir, monkeypatch):
    _write_device(_esphome_dir, "no_mqtt_device", "esphome:\n  name: no_mqtt_device\n")
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", _no_registry)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", _no_ws_call)
    monkeypatch.setattr(esphome_tools.ha, "_ws_collect_events", _no_ws_collect)
    monkeypatch.setattr(esphome_tools.ha, "call_service", _no_publish)

    result = _unwrap(esphome_tools.clean_mqtt)("no_mqtt_device")

    assert result["device"] == "no_mqtt_device"
    assert result["action"] == "clean_mqtt"
    assert result["skipped"] is True
    assert "mqtt" in result["reason"].lower()


# --- debug_info path: HA device registry has a matching entry ------------------------

def test_clean_mqtt_uses_debug_info_when_device_matches(_esphome_dir, monkeypatch):
    _write_device(
        _esphome_dir, "kitchen_sensor",
        "esphome:\n  name: kitchen_sensor\nmqtt: {}\n",
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "dev123", "name": "kitchen_sensor", "manufacturer": "espressif"}],
    )

    ws_calls = []

    def fake_ws_call(msg_type, **kwargs):
        ws_calls.append((msg_type, kwargs))
        if msg_type == "config_entries/get":
            return []
        assert msg_type == "mqtt/device/debug_info"
        assert kwargs == {"device_id": "dev123"}
        return {
            "entities": [
                {"discovery_data": {"topic": "homeassistant/sensor/kitchen_sensor/temperature/config"}},
            ],
            "triggers": [
                {"discovery_data": {"topic": "homeassistant/device_automation/kitchen_sensor/button/config"}},
            ],
        }

    monkeypatch.setattr(esphome_tools.ha, "_ws_call", fake_ws_call)
    monkeypatch.setattr(esphome_tools.ha, "_ws_collect_events", _no_ws_collect)

    published = []
    monkeypatch.setattr(
        esphome_tools.ha, "call_service",
        lambda domain, service, data: published.append((domain, service, data)) or {},
    )

    result = _unwrap(esphome_tools.clean_mqtt)("kitchen_sensor")

    assert result["method"] == "debug_info"
    assert result["success"] is True
    assert result["count"] == 2
    assert set(result["topics_cleared"]) == {
        "homeassistant/sensor/kitchen_sensor/temperature/config",
        "homeassistant/device_automation/kitchen_sensor/button/config",
    }
    assert len(published) == 2
    for domain, service, data in published:
        assert (domain, service) == ("mqtt", "publish")
        assert data["payload"] == ""
        assert data["retain"] is True
        assert data["topic"] in result["topics_cleared"]
    assert any(c[0] == "mqtt/device/debug_info" for c in ws_calls)


# --- wildcard_subscribe fallback: no matching HA device -------------------------------

def test_clean_mqtt_wildcard_subscribe_when_no_device_match(_esphome_dir, monkeypatch):
    _write_device(
        _esphome_dir, "attic_sensor",
        "esphome:\n  name: attic_sensor\nmqtt: {}\n",
    )
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", lambda t, **k: [] if t == "config_entries/get" else _no_ws_call())

    captured = {}

    def fake_collect(msg_type, is_last, timeout, **kwargs):
        captured["msg_type"] = msg_type
        captured["timeout"] = timeout
        captured["kwargs"] = kwargs
        return [
            {"topic": "homeassistant/sensor/attic_sensor/humidity/config", "retain": True, "payload": "{}"},
            {"topic": "homeassistant/status", "retain": False, "payload": "online"},
        ]

    monkeypatch.setattr(esphome_tools.ha, "_ws_collect_events", fake_collect)

    published = []
    monkeypatch.setattr(
        esphome_tools.ha, "call_service",
        lambda domain, service, data: published.append(data) or {},
    )

    result = _unwrap(esphome_tools.clean_mqtt)("attic_sensor", timeout=10)

    assert captured["msg_type"] == "mqtt/subscribe"
    assert captured["kwargs"]["topic"] == "homeassistant/+/attic_sensor/#"
    assert captured["timeout"] == esphome_tools._MQTT_DISCOVERY_WINDOW  # bounded, not the full 10s
    assert result["method"] == "wildcard_subscribe"
    assert result["topics_cleared"] == ["homeassistant/sensor/attic_sensor/humidity/config"]
    assert len(published) == 1


def test_clean_mqtt_wildcard_window_bounded_by_short_timeout(_esphome_dir, monkeypatch):
    _write_device(_esphome_dir, "attic_sensor", "esphome:\n  name: attic_sensor\nmqtt: {}\n")
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", lambda t, **k: [])

    captured = {}

    def fake_collect(msg_type, is_last, timeout, **kwargs):
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(esphome_tools.ha, "_ws_collect_events", fake_collect)
    monkeypatch.setattr(esphome_tools.ha, "call_service", _no_publish)

    _unwrap(esphome_tools.clean_mqtt)("attic_sensor", timeout=1.0)

    assert captured["timeout"] == 1.0  # min(3.0, 1.0)


# --- substitutions -------------------------------------------------------------------

def test_clean_mqtt_resolves_bare_dollar_substitution_in_node_name(_esphome_dir, monkeypatch):
    _write_device(
        _esphome_dir, "device1",
        "substitutions:\n  name: attic_real\nesphome:\n  name: $name\nmqtt: {}\n",
    )
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", lambda t, **k: [])

    captured = {}
    monkeypatch.setattr(
        esphome_tools.ha, "_ws_collect_events",
        lambda msg_type, is_last, timeout, **k: captured.update(k) or [],
    )
    monkeypatch.setattr(esphome_tools.ha, "call_service", _no_publish)

    _unwrap(esphome_tools.clean_mqtt)("device1")

    assert captured["topic"] == "homeassistant/+/attic_real/#"


def test_clean_mqtt_resolves_brace_substitution_in_node_name(_esphome_dir, monkeypatch):
    _write_device(
        _esphome_dir, "device2",
        "substitutions:\n  name: attic\nesphome:\n  name: ${name}_v2\nmqtt: {}\n",
    )
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", lambda t, **k: [])

    captured = {}
    monkeypatch.setattr(
        esphome_tools.ha, "_ws_collect_events",
        lambda msg_type, is_last, timeout, **k: captured.update(k) or [],
    )
    monkeypatch.setattr(esphome_tools.ha, "call_service", _no_publish)

    _unwrap(esphome_tools.clean_mqtt)("device2")

    assert captured["topic"] == "homeassistant/+/attic_v2/#"


def test_clean_mqtt_errors_on_unresolvable_substitution_before_any_network_call(_esphome_dir, monkeypatch):
    _write_device(_esphome_dir, "device3", "esphome:\n  name: $undefined_var\nmqtt: {}\n")
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", _no_registry)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", _no_ws_call)
    monkeypatch.setattr(esphome_tools.ha, "_ws_collect_events", _no_ws_collect)
    monkeypatch.setattr(esphome_tools.ha, "call_service", _no_publish)

    result = _unwrap(esphome_tools.clean_mqtt)("device3")

    assert "error" in result
    assert "esphome.name" in result["error"]
    assert "unresolved" in result["error"].lower()


def test_clean_mqtt_errors_when_node_name_uses_secret_tag(_esphome_dir, monkeypatch):
    _write_device(_esphome_dir, "device4", "esphome:\n  name: !secret device_name\nmqtt: {}\n")
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", _no_registry)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", _no_ws_call)

    result = _unwrap(esphome_tools.clean_mqtt)("device4")

    assert "error" in result
    assert "!secret" in result["error"]


def test_clean_mqtt_errors_when_no_esphome_name(_esphome_dir, monkeypatch):
    _write_device(_esphome_dir, "device5", "esphome: {}\nmqtt: {}\n")
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", _no_registry)

    result = _unwrap(esphome_tools.clean_mqtt)("device5")

    assert "error" in result
    assert "esphome.name" in result["error"]


def test_clean_mqtt_custom_discovery_prefix(_esphome_dir, monkeypatch):
    _write_device(
        _esphome_dir, "device6",
        "esphome:\n  name: device6\nmqtt:\n  discovery_prefix: custom_prefix\n",
    )
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "_ws_call", lambda t, **k: [])

    captured = {}
    monkeypatch.setattr(
        esphome_tools.ha, "_ws_collect_events",
        lambda msg_type, is_last, timeout, **k: captured.update(k) or [],
    )
    monkeypatch.setattr(esphome_tools.ha, "call_service", _no_publish)

    _unwrap(esphome_tools.clean_mqtt)("device6")

    assert captured["topic"] == "custom_prefix/+/device6/#"


# --- partial publish failure ----------------------------------------------------------

def test_clean_mqtt_reports_partial_publish_failures(_esphome_dir, monkeypatch):
    _write_device(_esphome_dir, "device7", "esphome:\n  name: device7\nmqtt: {}\n")
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d7", "name": "device7", "manufacturer": "esphome"}],
    )

    def fake_ws_call(msg_type, **kwargs):
        if msg_type == "config_entries/get":
            return []
        return {
            "entities": [
                {"discovery_data": {"topic": "homeassistant/sensor/device7/a/config"}},
                {"discovery_data": {"topic": "homeassistant/sensor/device7/b/config"}},
            ],
            "triggers": [],
        }

    monkeypatch.setattr(esphome_tools.ha, "_ws_call", fake_ws_call)

    def flaky_publish(domain, service, data):
        if data["topic"].endswith("/b/config"):
            raise RuntimeError("broker refused")
        return {}

    monkeypatch.setattr(esphome_tools.ha, "call_service", flaky_publish)

    result = _unwrap(esphome_tools.clean_mqtt)("device7")

    assert result["success"] is False
    assert result["topics_cleared"] == ["homeassistant/sensor/device7/a/config"]
    assert any("b/config" in e for e in result["errors"])


# --- path traversal / missing file, unchanged safety net ------------------------------

def test_clean_mqtt_rejects_path_traversal(_esphome_dir, monkeypatch):
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", _no_registry)

    result = _unwrap(esphome_tools.clean_mqtt)("../../etc/passwd")

    assert "error" in result


def test_clean_mqtt_missing_file(_esphome_dir):
    result = _unwrap(esphome_tools.clean_mqtt)("does_not_exist")

    assert "error" in result
    assert "not found" in result["error"].lower()
