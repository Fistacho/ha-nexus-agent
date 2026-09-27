"""Regression tests for prompt-audit findings F2, F4, F8.

* F2 ``ws_render_template`` — HA's WS ``render_template`` command is a
  subscription: the first ``result`` message only acks the subscription
  (``result: null``); the rendered value arrives in a follow-up ``event``.
  The old code returned the ack's ``null`` instead of waiting for that event.
* F8 ``ws_listen_state_changes`` — subscribes to the ``state_changed`` event
  type, which HA does not filter by entity server-side (confirmed against
  ``components/websocket_api/commands.py``: ``handle_subscribe_events`` only
  takes ``event_type``). The old code counted *any* entity's event toward
  ``count`` and only filtered by ``entity_id`` afterwards, so on a busy
  instance it could stop collecting before the target entity ever fired.
* F4 ``system_create_backup`` — called ``backup.create`` unconditionally.
  Verified against ``components/backup/services.py``: ``create`` is only
  registered ``if not is_hassio(hass)``; Supervisor installs only expose
  ``create_automatic``. The fix checks which service actually exists.
"""
import pytest

from tools import system as system_tools
from tools import websocket as websocket_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


# --- F2: render_template ------------------------------------------------------

def test_render_template_returns_rendered_value_not_null(monkeypatch):
    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        return [
            {"id": 1, "type": "result", "success": True, "result": None},
            {
                "id": 1,
                "type": "event",
                "event": {"result": "2", "listeners": {"all": False, "domains": [], "entities": []}},
            },
        ]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    assert _unwrap(websocket_tools.render_template)("{{ 1 + 1 }}") == "2"


def test_render_template_raises_on_template_error(monkeypatch):
    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        return [
            {"id": 1, "type": "result", "success": True, "result": None},
            {
                "id": 1,
                "type": "event",
                "event": {"error": "UndefinedError: 'x' is undefined", "level": "ERROR"},
            },
        ]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    with pytest.raises(RuntimeError, match="UndefinedError"):
        _unwrap(websocket_tools.render_template)("{{ x }}")


def test_render_template_raises_when_no_event_arrives(monkeypatch):
    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        return [{"id": 1, "type": "result", "success": True, "result": None}]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    with pytest.raises(RuntimeError, match="timed out"):
        _unwrap(websocket_tools.render_template)("{{ 1 + 1 }}")


# --- F8: listen_state_changes --------------------------------------------------

def test_listen_state_changes_ignores_events_for_other_entities(monkeypatch):
    """A busy HA fires state_changed for many entities; the target entity's events
    must not be displaced by unrelated ones when counting toward `count`."""

    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        noisy = [
            {
                "type": "event",
                "event": {
                    "data": {
                        "entity_id": "sensor.other",
                        "old_state": {"state": "1"},
                        "new_state": {"state": "2", "last_changed": "t"},
                    }
                },
            }
            for _ in range(10)
        ]
        target = [
            {
                "type": "event",
                "event": {
                    "data": {
                        "entity_id": "sensor.target",
                        "old_state": {"state": "10"},
                        "new_state": {"state": "11", "last_changed": "t1"},
                    }
                },
            },
            {
                "type": "event",
                "event": {
                    "data": {
                        "entity_id": "sensor.target",
                        "old_state": {"state": "11"},
                        "new_state": {"state": "12", "last_changed": "t2"},
                    }
                },
            },
        ]
        results = []
        collected = 0
        for evt in noisy + target:
            results.append(evt)
            if event_filter is None or event_filter(evt):
                collected += 1
                if collect_events > 0 and collected >= collect_events:
                    break
        return results

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    events = _unwrap(websocket_tools.listen_state_changes)("sensor.target", count=2, timeout=5.0)

    assert [e["new_state"] for e in events] == ["11", "12"]
    assert all(e["entity_id"] == "sensor.target" for e in events)


# --- F4: create_backup ---------------------------------------------------------

def test_create_backup_prefers_create_when_available(monkeypatch):
    """HA Core/Container installs register `backup.create`; use it when present."""
    calls = []

    monkeypatch.setattr(
        system_tools.ha,
        "list_services",
        lambda: [{"domain": "backup", "services": {"create": {}, "create_automatic": {}}}],
    )
    monkeypatch.setattr(
        system_tools.ha,
        "call_service",
        lambda domain, service, data=None: calls.append((domain, service)) or {"ok": True},
    )

    _unwrap(system_tools.create_backup)()

    assert calls == [("backup", "create")]


def test_create_backup_falls_back_to_create_automatic_on_supervisor(monkeypatch):
    """Supervisor installs only register `backup.create_automatic` — `create` is 404/unknown_command."""
    calls = []

    monkeypatch.setattr(
        system_tools.ha,
        "list_services",
        lambda: [{"domain": "backup", "services": {"create_automatic": {}}}],
    )
    monkeypatch.setattr(
        system_tools.ha,
        "call_service",
        lambda domain, service, data=None: calls.append((domain, service)) or {"ok": True},
    )

    _unwrap(system_tools.create_backup)()

    assert calls == [("backup", "create_automatic")]


def test_create_backup_returns_error_when_neither_service_exists(monkeypatch):
    calls = []

    monkeypatch.setattr(system_tools.ha, "list_services", lambda: [])
    monkeypatch.setattr(
        system_tools.ha,
        "call_service",
        lambda domain, service, data=None: calls.append((domain, service)) or {"ok": True},
    )

    result = _unwrap(system_tools.create_backup)()

    assert calls == []
    assert "error" in result
