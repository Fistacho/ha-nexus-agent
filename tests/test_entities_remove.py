"""`entities_remove_entity` — WS `config/entity_registry/remove` (HA core 2026.11.1
`components/config/entity_registry.py` `websocket_remove_entity`: `entity_id`
must be in the registry, else `not_found`; answers with no payload).

An entity whose integration still provides it gets its registry entry back at
the next reload, so the tool refuses unless `force`: "provided" means HA has a
state for it that is not a restored placeholder (`attributes.restored` is what
HA sets on states of entities no integration provides any more).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastmcp import Client

import ha_client as ha
from tools import entities

remove_entity = getattr(entities.remove_entity, "fn", entities.remove_entity)

_ENTRY = {"entity_id": "sensor.old_plug_power", "platform": "tuya", "config_entry_id": "01CE"}


@pytest.fixture
def fake(monkeypatch):
    calls: list[tuple[str, dict]] = []
    state: dict = {"value": {"entity_id": "sensor.old_plug_power", "state": "unavailable",
                             "attributes": {"restored": True}}}

    def ws(msg_type, **kw):
        calls.append((msg_type, kw))
        if msg_type == "config/entity_registry/get":
            if kw["entity_id"] != _ENTRY["entity_id"]:
                raise ha.HAWebSocketError({"code": "not_found", "message": "Entity not found"})
            return dict(_ENTRY)
        if msg_type == "config/entity_registry/remove":
            return None
        raise AssertionError(msg_type)

    def get_state(entity_id):
        if state["value"] is None:
            request = httpx.Request("GET", f"http://ha.test/api/states/{entity_id}")
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        return state["value"]

    monkeypatch.setattr(ha, "_ws_call", ws)
    monkeypatch.setattr(ha, "get_state", get_state)
    return calls, state


def test_removes_an_orphaned_entity(fake):
    calls, _ = fake

    result = remove_entity("sensor.old_plug_power")

    assert result == {"removed": True, "entity_id": "sensor.old_plug_power", "platform": "tuya", "was_provided": False}
    assert calls[-1] == ("config/entity_registry/remove", {"entity_id": "sensor.old_plug_power"})


def test_removes_an_entity_without_any_state(fake):
    calls, state = fake
    state["value"] = None

    assert remove_entity("sensor.old_plug_power")["removed"] is True


def test_refuses_an_entity_still_provided_unless_forced(fake):
    calls, state = fake
    state["value"] = {"entity_id": "sensor.old_plug_power", "state": "12.5", "attributes": {}}

    refused = remove_entity("sensor.old_plug_power")
    assert refused["error"] == "entity_still_provided"
    assert not any(t == "config/entity_registry/remove" for t, _ in calls)

    forced = remove_entity("sensor.old_plug_power", force=True)
    assert forced["removed"] is True and forced["was_provided"] is True


def test_unknown_entity_and_bad_id(fake):
    calls, _ = fake

    assert remove_entity("sensor.nope")["error"] == "not_found"
    assert remove_entity("../x")["error"] == "invalid_entity_id"
    assert not any(t == "config/entity_registry/remove" for t, _ in calls)


def test_result_is_an_object_over_mcp(fake):
    async def go():
        async with Client(entities.mcp) as client:
            return await client.call_tool("remove_entity", {"entity_id": "sensor.old_plug_power"}, raise_on_error=False)

    result = asyncio.run(go())

    assert not result.is_error and result.structured_content["removed"] is True
