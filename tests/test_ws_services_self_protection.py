"""W3 Security review M1: `ws_call_service` must refuse `hassio.*` service
calls that would target nexus's own add-on — the same bypass closed in
`services_call_service` (`tests/test_services_self_protection.py`), reached
here through the WebSocket `call_service` transport instead of REST. See
`self_protection.py` for the shared guard.
"""
from __future__ import annotations

import pytest

import self_protection
from tools import websocket as websocket_tools

call_service = getattr(websocket_tools.call_service, "fn", websocket_tools.call_service)

_OWN_SLUG = "5c53de3b_nexus"


@pytest.fixture(autouse=True)
def _reset_self_protection():
    self_protection.reset_for_tests()
    yield
    self_protection.reset_for_tests()


def _boom_ws_send_recv(monkeypatch):
    async def _boom(messages, collect_events=0, timeout=10.0, event_filter=None):
        raise AssertionError("no WS call should happen for a blocked hassio.* service call")

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", _boom)


def _fake_ws_send_recv(monkeypatch, captured=None):
    async def fake(messages, collect_events=0, timeout=10.0, event_filter=None):
        if captured is not None:
            captured["messages"] = messages
        return [{"type": "result", "success": True, "result": {}}]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake)


def test_blocks_addon_stop_against_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    _boom_ws_send_recv(monkeypatch)

    result = call_service("hassio", "addon_stop", {"addon": _OWN_SLUG})

    assert result["error"] == "self_addon_hassio_service_blocked"


def test_blocks_app_stdin_against_literal_self_alias(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    _boom_ws_send_recv(monkeypatch)

    result = call_service("hassio", "app_stdin", {"app": "self", "input": "hi"})

    assert result["error"] == "self_addon_hassio_service_blocked"


def test_allows_addon_stop_against_other_addons(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    captured = {}
    _fake_ws_send_recv(monkeypatch, captured)

    result = call_service("hassio", "addon_stop", {"addon": "core_mosquitto"})

    assert result == {"type": "result", "success": True, "result": {}}
    assert captured["messages"][0]["service_data"] == {"addon": "core_mosquitto"}


def test_allows_addon_restart_against_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    _fake_ws_send_recv(monkeypatch)

    result = call_service("hassio", "addon_restart", {"addon": _OWN_SLUG})

    assert result == {"type": "result", "success": True, "result": {}}


def test_unrelated_domains_are_never_blocked(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    _fake_ws_send_recv(monkeypatch)

    result = call_service("weather", "get_forecasts", {"entity_id": "weather.home"})

    assert result == {"type": "result", "success": True, "result": {}}


def test_inert_outside_addon_mode(monkeypatch):
    _fake_ws_send_recv(monkeypatch)

    result = call_service("hassio", "addon_stop", {"addon": "core_mosquitto"})

    assert result == {"type": "result", "success": True, "result": {}}
