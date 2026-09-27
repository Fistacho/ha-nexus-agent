"""Regression tests for prompt-audit findings F6, F7, F22
(tools/integrations.py, tools/entities.py, tools/services.py).

* F6 — config/options flow tools called WebSocket message types that do not
  exist (``config_entries/flow/init``, ``/configure``, ``/abort``, ``/get``,
  ``config_entries/options/flow/*``, ``config_entries/remove``); HA answers
  ``unknown_command`` for all of them. The real flow API is REST-only.
  Verified against home-assistant/core:
  ``helpers/data_entry_flow.py`` (``FlowManagerIndexView``/
  ``FlowManagerResourceView``: POST .../flow, GET/POST/DELETE .../flow/{id}),
  ``components/config/config_entries.py`` (``ConfigManagerFlowIndexView``,
  ``OptionManagerFlowIndexView`` — handler for options is the entry_id;
  ``ConfigManagerEntryResourceView.delete`` for removal). ``config_entries/get``,
  ``config_entries/update``, ``config_entries/disable`` and
  ``config_entries/flow/progress`` ARE real WS commands and are left untouched.

* F7 — ``get_entity_exposure``/``list_exposed_entities`` called WS
  ``homeassistant/expose/get``/``homeassistant/expose/list``, which do not
  exist. The real command is ``homeassistant/expose_entity/list`` (no
  arguments) which returns *every* exposed entity in one shot; filtering by
  entity_id / assistant now happens client-side. Verified against
  home-assistant/core: ``components/homeassistant/exposed_entities.py``
  (``ws_list_exposed_entities``). ``set_entity_exposure``/
  ``bulk_set_entity_exposure`` already used the correct
  ``homeassistant/expose_entity`` command and are unaffected.

* F22 — ``light.turn_on`` stopped accepting ``color_temp`` (mireds) in HA
  2026.3; only ``color_temp_kelvin`` is accepted now. ``entities_turn_on``
  also forwarded light-only fields (brightness/color_temp/rgb_color) to
  every domain, which non-light services (e.g. ``switch.turn_on``) reject.
"""
import httpx
import pytest

import ha_client as ha
from tools import entities as entities_tools
from tools import integrations as integrations_tools
from tools import services as services_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


def _fake_rest_client(calls, response_json=None, status=200):
    """A minimal stand-in for httpx.Client that records every call."""
    body = {} if response_json is None else response_json

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path):
            calls.append(("GET", path, None))
            request = httpx.Request("GET", f"http://ha.test:8123{path}")
            return httpx.Response(status, json=body, request=request)

        def post(self, path, json=None):
            calls.append(("POST", path, json))
            request = httpx.Request("POST", f"http://ha.test:8123{path}")
            return httpx.Response(status, json=body, request=request)

        def delete(self, path):
            calls.append(("DELETE", path, None))
            request = httpx.Request("DELETE", f"http://ha.test:8123{path}")
            return httpx.Response(status, json=body, request=request)

    return FakeClient()


# --- F6: config flow ----------------------------------------------------------

def test_start_config_flow_uses_rest_not_websocket(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("start_config_flow must not use the WS API")

    monkeypatch.setattr(ha, "_ws_call", explode)
    calls = []
    monkeypatch.setattr(
        ha, "_client",
        lambda: _fake_rest_client(calls, {"type": "form", "flow_id": "abc", "step_id": "user", "data_schema": []}),
    )

    result = _unwrap(integrations_tools.start_config_flow)("shelly")

    assert calls[0][:2] == ("POST", "/api/config/config_entries/flow")
    assert calls[0][2]["handler"] == "shelly"
    assert result["flow_id"] == "abc"


def test_submit_config_flow_step_posts_to_flow_resource(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"type": "create_entry", "flow_id": "abc"}))
    monkeypatch.setattr(ha, "_ws_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must use REST")))

    result = _unwrap(integrations_tools.submit_config_flow_step)("abc", {"host": "1.2.3.4"})

    assert calls[0] == ("POST", "/api/config/config_entries/flow/abc", {"host": "1.2.3.4"})
    assert result["flow_id"] == "abc"


def test_abort_config_flow_deletes_flow_resource(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"message": "Flow aborted"}))

    result = _unwrap(integrations_tools.abort_config_flow)("abc")

    assert calls[0] == ("DELETE", "/api/config/config_entries/flow/abc", None)
    assert result["message"] == "Flow aborted"


def test_get_config_flow_reads_flow_resource(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"flow_id": "abc", "step_id": "user"}))

    result = _unwrap(integrations_tools.get_config_flow)("abc")

    assert calls[0] == ("GET", "/api/config/config_entries/flow/abc", None)
    assert result["step_id"] == "user"


# --- F6: options flow ---------------------------------------------------------

def test_start_options_flow_posts_entry_id_as_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"flow_id": "opt1", "step_id": "init"}))

    result = _unwrap(integrations_tools.start_options_flow)("entry123")

    assert calls[0][:2] == ("POST", "/api/config/config_entries/options/flow")
    assert calls[0][2]["handler"] == "entry123"
    assert result["flow_id"] == "opt1"


def test_submit_options_flow_step_posts_to_options_flow_resource(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"type": "create_entry"}))

    _unwrap(integrations_tools.submit_options_flow_step)("opt1", {"scan_interval": 30})

    assert calls[0] == ("POST", "/api/config/config_entries/options/flow/opt1", {"scan_interval": 30})


def test_abort_options_flow_deletes_options_flow_resource(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"message": "Flow aborted"}))

    _unwrap(integrations_tools.abort_options_flow)("opt1")

    assert calls[0] == ("DELETE", "/api/config/config_entries/options/flow/opt1", None)


# --- F6: remove_integration -----------------------------------------------------

def test_remove_integration_uses_rest_delete_not_websocket(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("remove_integration must not use WS config_entries/remove")

    monkeypatch.setattr(ha, "_ws_call", explode)
    calls = []
    monkeypatch.setattr(ha, "_client", lambda: _fake_rest_client(calls, {"require_restart": False}))

    result = _unwrap(integrations_tools.remove_integration)("entry123", confirm=True)

    assert calls[0] == ("DELETE", "/api/config/config_entries/entry/entry123", None)
    assert result["require_restart"] is False


def test_remove_integration_still_requires_confirmation(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call HA without confirm=True")

    monkeypatch.setattr(ha, "_client", explode)
    monkeypatch.setattr(ha, "_ws_call", explode)

    result = _unwrap(integrations_tools.remove_integration)("entry123")

    assert result["error"] == "confirmation_required"


# --- F6: unaffected commands stay on WebSocket ----------------------------------

def test_disable_integration_still_uses_websocket(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or {"require_restart": False})

    _unwrap(integrations_tools.disable_integration)("entry123")

    assert calls == [("config_entries/disable", {"entry_id": "entry123", "disabled_by": "user"})]


def test_list_flows_in_progress_still_uses_websocket(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or [])

    _unwrap(integrations_tools.list_flows_in_progress)()

    assert calls == [("config_entries/flow/progress", {})]


# --- F7: entity exposure ---------------------------------------------------------

def test_get_entity_exposure_uses_expose_entity_list(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {"exposed_entities": {"light.kitchen": {"conversation": True}}}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(entities_tools.get_entity_exposure)("light.kitchen")

    assert calls == [("homeassistant/expose_entity/list", {})]
    assert result["exposure"]["conversation"] is True
    assert result["exposure"]["cloud.alexa"] is False
    assert result["exposure"]["cloud.google_assistant"] is False


def test_get_entity_exposure_defaults_to_not_exposed_when_absent(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", lambda *a, **k: {"exposed_entities": {}})

    result = _unwrap(entities_tools.get_entity_exposure)("light.unknown")

    assert result["exposure"] == {
        "conversation": False,
        "cloud.alexa": False,
        "cloud.google_assistant": False,
    }


def test_list_exposed_entities_uses_expose_entity_list_and_filters_by_assistant(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {
            "exposed_entities": {
                "light.kitchen": {"conversation": True},
                "light.hallway": {"conversation": True, "cloud.alexa": True},
                "switch.fan": {"cloud.alexa": True},
            }
        }

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(entities_tools.list_exposed_entities)("conversation")

    assert calls == [("homeassistant/expose_entity/list", {})]
    assert set(result["exposed"]) == {"light.kitchen", "light.hallway"}


def test_list_exposed_entities_rejects_unknown_assistant():
    with pytest.raises(ValueError):
        _unwrap(entities_tools.list_exposed_entities)("not_a_real_assistant")


def test_set_entity_exposure_still_uses_expose_entity_command(monkeypatch):
    """Guard against regressing the tool that was already correct."""
    calls = []
    monkeypatch.setattr(ha, "_ws_call", lambda msg_type, **kw: calls.append((msg_type, kw)) or None)

    _unwrap(entities_tools.set_entity_exposure)("light.kitchen", "conversation", True)

    assert calls == [
        ("homeassistant/expose_entity", {
            "assistants": ["conversation"],
            "entity_ids": ["light.kitchen"],
            "should_expose": True,
        })
    ]


# --- F22: color_temp_kelvin -----------------------------------------------------

def test_turn_on_converts_deprecated_mireds_to_kelvin(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda domain, service, data: calls.append((domain, service, data)) or [])

    _unwrap(entities_tools.turn_on)("light.kitchen", color_temp=370)

    domain, service, data = calls[0]
    assert "color_temp" not in data
    assert data["color_temp_kelvin"] == round(1_000_000 / 370)


def test_turn_on_accepts_color_temp_kelvin_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda domain, service, data: calls.append((domain, service, data)) or [])

    _unwrap(entities_tools.turn_on)("light.kitchen", color_temp_kelvin=2700)

    assert calls[0][2]["color_temp_kelvin"] == 2700


def test_turn_on_rejects_light_options_for_non_light_domain(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call a service when rejecting light-only options")

    monkeypatch.setattr(ha, "call_service", explode)

    result = _unwrap(entities_tools.turn_on)("switch.fan", brightness=100)

    assert "error" in result


def test_turn_on_plain_non_light_entity_is_unaffected(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda domain, service, data: calls.append((domain, service, data)) or [])

    _unwrap(entities_tools.turn_on)("switch.fan")

    assert calls[0] == ("switch", "turn_on", {"entity_id": "switch.fan"})


def test_set_light_color_converts_deprecated_mireds_to_kelvin(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda domain, service, data: calls.append((domain, service, data)) or [])

    _unwrap(services_tools.set_light_color)("light.kitchen", color_temp=250)

    domain, service, data = calls[0]
    assert "color_temp" not in data
    assert data["color_temp_kelvin"] == round(1_000_000 / 250)


def test_set_light_color_accepts_color_temp_kelvin_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda domain, service, data: calls.append((domain, service, data)) or [])

    _unwrap(services_tools.set_light_color)("light.kitchen", color_temp_kelvin=3000)

    assert calls[0][2]["color_temp_kelvin"] == 3000
