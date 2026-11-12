"""Two fixes found while cleaning up the live HA (2026-11-12).

`devices_remove_device` sent `config_entries/remove_device`, a command HA core
2026.11.1 does not have (`components/config/device_registry.py` registers
`config/device_registry/remove` {device_id} and its deprecated alias
`config/device_registry/remove_config_entry` {device_id, config_entry_id},
removed in HA 2027.9). It now checks the device belongs to `config_entry_id`
and sends `config/device_registry/remove`, falling back to the alias on an HA
that does not know the new command.

`history_get_state_history` / `history_get_logbook` with `hours > 24` returned
one day only: without `end_time` HA's REST history ends at `start + 1 day`
(`components/history/__init__.py`: `end_time = start_time + _ONE_DAY`) and the
logbook at `start + period days` (`components/logbook/rest_api.py`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

import ha_client as ha
from tools import devices

remove_device = getattr(devices.remove_device, "fn", devices.remove_device)

_DEVICES = [{"id": "dev1", "config_entries": ["ce1"], "config_entry_id": "ce1"}]


def _fake_ws(known: set[str], calls: list):
    def ws(msg_type, **kw):
        calls.append((msg_type, kw))
        if msg_type == "config/device_registry/list":
            return list(_DEVICES)
        if msg_type not in known:
            raise ha.HAWebSocketError({"code": "unknown_command", "message": "Unknown command."})
        return None

    return ws


def test_remove_device_uses_the_command_ha_has(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ha, "_ws_call", _fake_ws({"config/device_registry/remove"}, calls))

    result = remove_device("dev1", "ce1")

    assert result["removed"] is True
    assert ("config/device_registry/remove", {"device_id": "dev1"}) in calls
    assert not any(t == "config_entries/remove_device" for t, _ in calls)


def test_remove_device_falls_back_to_the_alias(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ha, "_ws_call", _fake_ws({"config/device_registry/remove_config_entry"}, calls))

    result = remove_device("dev1", "ce1")

    assert result["removed"] is True and result["command"] == "config/device_registry/remove_config_entry"
    assert ("config/device_registry/remove_config_entry", {"device_id": "dev1", "config_entry_id": "ce1"}) in calls


def test_remove_device_checks_membership_first(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ha, "_ws_call", _fake_ws({"config/device_registry/remove"}, calls))

    assert remove_device("dev1", "other")["error"] == "config_entry_not_in_device"
    assert remove_device("nope", "ce1")["error"] == "device_not_found"
    assert [t for t, _ in calls if "remove" in t] == []


class _Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=[])


@pytest.mark.parametrize("fn", [ha.get_history, ha.get_logbook])
def test_history_and_logbook_send_end_time(monkeypatch, fn):
    rec = _Recorder()
    real = httpx.Client
    monkeypatch.setattr(ha, "_client", lambda: real(base_url="http://ha.test", transport=httpx.MockTransport(rec.handler)))

    fn(entity_id="sensor.t", hours=72)

    request = rec.requests[0]
    end = datetime.fromisoformat(request.url.params["end_time"])
    start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[1])
    assert abs((end - start) - timedelta(hours=72)) < timedelta(seconds=5)
    assert abs(end - datetime.now(UTC)) < timedelta(seconds=30)
