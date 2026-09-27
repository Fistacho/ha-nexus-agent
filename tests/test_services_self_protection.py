"""W3 Security review M1: `services_call_service` must refuse `hassio.*`
service calls that would target nexus's own add-on — the bypass the first
W3 pass missed entirely (blocking only `tools/supervisor.py`, while this
generic dispatcher reaches the identical Supervisor operations through HA
Core's own `hassio.addon_stop`/`app_stop`/`addon_stdin`/`app_stdin`
services). See `self_protection.py` for the shared guard and the exact
`hassio.*` service list this is checked against.
"""
from __future__ import annotations

import pytest

import ha_client as ha
import self_protection
from tools import services as services_tools

call_service = getattr(services_tools.call_service, "fn", services_tools.call_service)

_OWN_SLUG = "5c53de3b_nexus"


@pytest.fixture(autouse=True)
def _reset_self_protection():
    self_protection.reset_for_tests()
    yield
    self_protection.reset_for_tests()


def test_blocks_addon_stop_against_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    result = call_service("hassio", "addon_stop", {"addon": _OWN_SLUG})

    assert result["error"] == "self_addon_hassio_service_blocked"


def test_blocks_addon_stop_against_literal_self_alias(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    result = call_service("hassio", "addon_stop", {"addon": "self"})

    assert result["error"] == "self_addon_hassio_service_blocked"


def test_allows_addon_stop_against_other_addons(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [{"ok": True}])

    result = call_service("hassio", "addon_stop", {"addon": "core_mosquitto"})

    assert result == [{"ok": True}]
    assert calls == [("hassio", "addon_stop", {"addon": "core_mosquitto"})]


def test_allows_addon_restart_against_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [{"ok": True}])

    result = call_service("hassio", "addon_restart", {"addon": _OWN_SLUG})

    assert result == [{"ok": True}]
    assert calls


def test_blocks_addon_stdin_against_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    result = call_service("hassio", "addon_stdin", {"addon": _OWN_SLUG, "input": "hello"})

    assert result["error"] == "self_addon_hassio_service_blocked"


def test_unrelated_domains_are_never_blocked(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [])

    result = call_service("light", "turn_on", {"entity_id": "light.kitchen"})

    assert result == []
    assert calls == [("light", "turn_on", {"entity_id": "light.kitchen"})]


def test_inert_outside_addon_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [])

    result = call_service("hassio", "addon_stop", {"addon": "core_mosquitto"})

    assert result == []
    assert calls
