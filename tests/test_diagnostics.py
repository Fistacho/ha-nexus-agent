"""Regression tests for the diagnostic tools (repairs, system health, error log).

All three were calling endpoints that do not exist in Home Assistant:

* ``repairs/list``       — the real WS command is ``repairs/list_issues``
* ``GET /api/system_health`` — no such REST view; only WS ``system_health/info``
* ``GET /api/error_log`` — only registered when HA logs to a file, which is off
  by default on Supervisor installs (``duplicate_log_file: false``)

Verified against home-assistant/core: ``components/repairs/websocket_api.py``,
``components/system_health/__init__.py``, ``components/api/__init__.py``.
"""
import httpx
import pytest

import ha_client as ha
from tools import history as history_tools
from tools import supervisor as supervisor_tools
from tools import system as system_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


# --- repairs -----------------------------------------------------------------

def test_get_repairs_uses_the_list_issues_command(monkeypatch):
    """`repairs/list` does not exist and HA answers `unknown_command`."""
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        return {"issues": []}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)
    _unwrap(system_tools.get_repairs)()

    assert calls == ["repairs/list_issues"]


def test_get_repairs_maps_issue_fields(monkeypatch):
    monkeypatch.setattr(
        ha,
        "_ws_call",
        lambda *a, **k: {
            "issues": [
                {
                    "issue_id": "deprecated_yaml",
                    "domain": "hue",
                    "severity": "warning",
                    "breaks_in_ha_version": "2027.1",
                    "translation_key": "deprecated_yaml",
                    "is_fixable": True,
                }
            ]
        },
    )

    (issue,) = _unwrap(system_tools.get_repairs)()

    assert issue["issue_id"] == "deprecated_yaml"
    assert issue["domain"] == "hue"
    assert issue["is_fixable"] is True
    assert issue["ignored"] is False


# --- system health -----------------------------------------------------------

def test_merge_system_health_events_folds_initial_and_updates():
    """`system_health/info` streams: initial snapshot, then per-key updates."""
    events = [
        {
            "type": "initial",
            "data": {
                "homeassistant": {"info": {"version": "2026.8.0"}},
                "cloud": {"info": {"logged_in": True, "can_reach_cert_server": {"type": "pending"}}},
            },
        },
        {"type": "update", "domain": "cloud", "key": "can_reach_cert_server", "success": True, "data": "ok"},
        {"type": "finish"},
    ]

    merged = ha.merge_system_health_events(events)

    assert merged["homeassistant"]["info"]["version"] == "2026.8.0"
    assert merged["cloud"]["info"]["can_reach_cert_server"] == "ok"


def test_merge_system_health_events_records_failed_keys():
    events = [
        {"type": "initial", "data": {"cloud": {"info": {"can_reach_cert_server": {"type": "pending"}}}}},
        {
            "type": "update",
            "domain": "cloud",
            "key": "can_reach_cert_server",
            "success": False,
            "error": {"type": "failed", "error": "unknown"},
        },
        {"type": "finish"},
    ]

    merged = ha.merge_system_health_events(events)

    assert merged["cloud"]["info"]["can_reach_cert_server"] == {"error": {"type": "failed", "error": "unknown"}}


def test_get_system_health_uses_websocket_not_rest(monkeypatch):
    """The REST call was a 404 on every install — it must not be attempted."""
    def explode(*a, **k):
        raise AssertionError("get_system_health must not use the REST client")

    monkeypatch.setattr(ha, "_client", explode)
    monkeypatch.setattr(ha, "collect_system_health", lambda **k: {"homeassistant": {"info": {}}})

    assert _unwrap(system_tools.get_system_health)() == {"homeassistant": {"info": {}}}


# --- error log ---------------------------------------------------------------

def test_format_system_log_entries_renders_readable_lines():
    entries = [
        {
            "timestamp": 1785000000.0,
            "level": "ERROR",
            "name": "homeassistant.components.xiaomi_miot",
            "message": ["Setup failed"],
            "source": ["custom_components/xiaomi_miot/__init__.py", 120],
            "count": 3,
            "exception": "Traceback (most recent call last):\n  boom",
        }
    ]

    text = ha.format_system_log_entries(entries)

    assert "ERROR" in text
    assert "homeassistant.components.xiaomi_miot" in text
    assert "Setup failed" in text
    assert "custom_components/xiaomi_miot/__init__.py:120" in text
    assert "x3" in text
    assert "Traceback" in text


def test_format_system_log_entries_handles_empty_list():
    assert ha.format_system_log_entries([]) == ""


def test_get_error_log_falls_back_to_system_log_on_404(monkeypatch):
    """/api/error_log is absent unless HA logs to a file — fall back, don't raise."""
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path):
            request = httpx.Request("GET", f"http://ha.test:8123{path}")
            return httpx.Response(404, request=request)

    ws_calls = []

    def fake_ws_call(msg_type, **kwargs):
        ws_calls.append(msg_type)
        return [
            {
                "timestamp": 1785000000.0,
                "level": "ERROR",
                "name": "custom_components.xiaomi_miot",
                "message": ["Config entry failed to set up"],
                "source": ["custom_components/xiaomi_miot/__init__.py", 42],
                "count": 1,
            }
        ]

    monkeypatch.setattr(ha, "_client", lambda: FakeClient())
    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    text = _unwrap(history_tools.get_error_log)()

    assert ws_calls == ["system_log/list"]
    assert "xiaomi_miot" in text
    assert "Config entry failed to set up" in text


def test_get_error_log_prefers_rest_when_available(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path):
            request = httpx.Request("GET", f"http://ha.test:8123{path}")
            return httpx.Response(200, text="2026-08-06 09:25:31 ERROR real file log", request=request)

    def explode(*a, **k):
        raise AssertionError("must not fall back when REST works")

    monkeypatch.setattr(ha, "_client", lambda: FakeClient())
    monkeypatch.setattr(ha, "_ws_call", explode)

    assert "real file log" in _unwrap(history_tools.get_error_log)()


# --- supervisor add-on options ----------------------------------------------

def test_set_addon_options_builds_a_posix_path(monkeypatch):
    """Guard the Supervisor path shape — a stray backslash would become an escape."""
    seen = {}

    def fake_request(method, path, json=None):
        seen["method"] = method
        seen["path"] = path
        seen["json"] = json
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)
    _unwrap(supervisor_tools.set_addon_options)("core_ssh", {"packages": ["nmap"]})

    assert seen["path"] == "/addons/core_ssh/options"
    assert "\\" not in seen["path"]
    assert "\a" not in seen["path"]
    assert seen["json"] == {"options": {"packages": ["nmap"]}}
