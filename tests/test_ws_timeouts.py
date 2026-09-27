"""Regression tests for ADR-0003 #7 — `timeout` bounds and handshake in `tools/websocket.py`.

Three things are covered:

1. `ws_get_states`, `ws_get_config`, `ws_call_service` gain a `timeout`
   parameter that reaches `_ws_send_recv` (previously hard-coded to 10.0
   with no way for a caller to change it).
2. Every `timeout` parameter across all seven `ws_*` tools is bounded to
   `[1, 300]` at the schema level (`Field(ge=1, le=300)`), so FastMCP's
   pydantic validation rejects `0` and `301` before any WebSocket I/O runs.
3. The `auth_required` -> `auth` -> `auth_ok` handshake in `_ws_send_recv`
   is itself bounded by `timeout`. Before this fix, a HA instance that sent
   `auth_required` and then never answered `auth_ok` (or never sent
   anything at all) made a call hang indefinitely, regardless of
   `timeout` — confirmed against a local fake WS server: an outer 8s
   `asyncio.wait_for` around a call with `timeout=2.0` still fired,
   proving the wait was unbounded.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets

from tools import websocket as websocket_tools

_ALL_TIMEOUT_TOOLS = [
    "get_states",
    "get_config",
    "call_service",
    "render_template",
    "listen_state_changes",
    "listen_events",
    "subscribe_trigger",
]

_MIN_VALID_ARGS = {
    "get_states": {},
    "get_config": {},
    "call_service": {"domain": "weather", "service": "get_forecasts"},
    "render_template": {"template": "{{ 1 + 1 }}"},
    "listen_state_changes": {"entity_id": "sensor.x"},
    "listen_events": {"event_type": "zha_event"},
    "subscribe_trigger": {"trigger": {"platform": "state", "entity_id": "sensor.x"}},
}


def _unwrap(tool):
    return getattr(tool, "fn", tool)


# --- 1. new `timeout` param on get_states/get_config/call_service reaches _ws_send_recv ---


def test_get_states_passes_timeout_to_ws_send_recv(monkeypatch):
    captured = {}

    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        captured["timeout"] = timeout
        return [{"type": "result", "success": True, "result": []}]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    _unwrap(websocket_tools.get_states)(timeout=42.0)

    assert captured["timeout"] == 42.0


def test_get_config_passes_timeout_to_ws_send_recv(monkeypatch):
    captured = {}

    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        captured["timeout"] = timeout
        return [{"type": "result", "success": True, "result": {}}]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    _unwrap(websocket_tools.get_config)(timeout=99.0)

    assert captured["timeout"] == 99.0


def test_call_service_passes_timeout_to_ws_send_recv(monkeypatch):
    captured = {}

    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        captured["timeout"] = timeout
        return [{"type": "result", "success": True, "result": {}}]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    _unwrap(websocket_tools.call_service)("weather", "get_forecasts", timeout=17.0)

    assert captured["timeout"] == 17.0


def test_get_states_default_timeout_unchanged(monkeypatch):
    """Default stays 10.0 — no behaviour change for existing callers."""
    captured = {}

    async def fake_ws_send_recv(messages, collect_events=0, timeout=10.0, event_filter=None):
        captured["timeout"] = timeout
        return [{"type": "result", "success": True, "result": []}]

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fake_ws_send_recv)

    _unwrap(websocket_tools.get_states)()

    assert captured["timeout"] == 10.0


# --- 2. schema-level bounds [1, 300] on all seven ws_* timeout parameters ---


@pytest.mark.parametrize("tool_local_name", _ALL_TIMEOUT_TOOLS, ids=_ALL_TIMEOUT_TOOLS)
def test_timeout_schema_has_min_1_max_300(tool_local_name):
    schema = _unwrap_schema(tool_local_name)
    assert schema["minimum"] == 1
    assert schema["maximum"] == 300


def _unwrap_schema(tool_local_name: str) -> dict:
    tools = asyncio.run(websocket_tools.mcp.list_tools())
    by_name = {t.name: t.to_mcp_tool() for t in tools}
    return by_name[tool_local_name].inputSchema["properties"]["timeout"]


@pytest.mark.parametrize("tool_local_name", _ALL_TIMEOUT_TOOLS, ids=_ALL_TIMEOUT_TOOLS)
def test_timeout_0_rejected_by_schema_validation(tool_local_name, monkeypatch):
    """`timeout=0` never reaches I/O: pydantic validation rejects it first."""
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("_ws_send_recv must not run when timeout fails validation")

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fail_if_called)

    args = dict(_MIN_VALID_ARGS[tool_local_name])
    args["timeout"] = 0

    with pytest.raises(Exception) as exc_info:
        asyncio.run(websocket_tools.mcp.call_tool(tool_local_name, args))
    assert "greater_than_equal" in str(exc_info.value) or "greater than or equal" in str(exc_info.value)


@pytest.mark.parametrize("tool_local_name", _ALL_TIMEOUT_TOOLS, ids=_ALL_TIMEOUT_TOOLS)
def test_timeout_301_rejected_by_schema_validation(tool_local_name, monkeypatch):
    """`timeout=301` never reaches I/O: pydantic validation rejects it first."""
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("_ws_send_recv must not run when timeout fails validation")

    monkeypatch.setattr(websocket_tools, "_ws_send_recv", fail_if_called)

    args = dict(_MIN_VALID_ARGS[tool_local_name])
    args["timeout"] = 301

    with pytest.raises(Exception) as exc_info:
        asyncio.run(websocket_tools.mcp.call_tool(tool_local_name, args))
    assert "less_than_equal" in str(exc_info.value) or "less than or equal" in str(exc_info.value)


# --- 3. handshake in _ws_send_recv is bounded by `timeout` ---


async def _close_server_soon(server) -> None:
    """Close a fake server without blocking on stuck-forever handler tasks.

    Handlers used by these tests deliberately `await asyncio.sleep(3600)` to
    simulate a stuck HA. `server.close()` alone doesn't cancel that Python-
    level sleep, so a plain `await server.wait_closed()` would itself hang
    for up to an hour. Bounding it is cleanup-only and asserts nothing.
    """
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=1.0)
    except TimeoutError:
        pass


def _run_against_fake_server(
    monkeypatch, handler, call_timeout: float, outer_margin: float = 30.0
) -> tuple[float, BaseException]:
    """Start `handler` as a local fake WS server, call `_ws_send_recv` against
    it with `timeout=call_timeout`, and return `(elapsed, exception)`.

    The call itself is additionally wrapped in an outer bounded
    `asyncio.wait_for` (`call_timeout + outer_margin`) — this proves the
    assertion is about `_ws_send_recv` returning promptly, not about
    pytest's own runner eventually giving up on a hung test. `outer_margin`
    is deliberately generous (tens of seconds, not fractions of a second):
    this suite runs alongside other agents' concurrent test runs on the
    same shared dev machine, so wall-clock scheduling delay is expected —
    the pre-fix bug this guards against was an unbounded, hours-long hang
    (`asyncio.sleep(3600)` in the fake handler), not a difference of a few
    seconds.
    """
    async def _drive():
        server = await websockets.serve(handler, "localhost", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(websocket_tools, "_ws_url", lambda: f"ws://localhost:{port}/api/websocket")
        monkeypatch.setattr(websocket_tools, "_get_token", lambda: "test-token")

        start = time.monotonic()
        caught: BaseException | None = None
        try:
            await asyncio.wait_for(
                websocket_tools._ws_send_recv([{"type": "get_states"}], timeout=call_timeout),
                timeout=call_timeout + outer_margin,
            )
        except BaseException as exc:  # noqa: BLE001 - test wants to inspect whatever comes back
            caught = exc
        elapsed = time.monotonic() - start
        await _close_server_soon(server)
        return elapsed, caught

    return asyncio.run(_drive())


def test_handshake_stuck_after_auth_required_ends_within_timeout(monkeypatch):
    """A server that sends `auth_required` and then goes silent must not hang
    past `timeout` — this is RED against the pre-fix code, which awaits
    `ws.recv()` for `auth_ok` with no timeout at all."""
    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2024.1.0"}))
        # Never send auth_ok, never close — simulates a stuck/unresponsive HA.
        await asyncio.sleep(3600)

    elapsed, caught = _run_against_fake_server(monkeypatch, handler, call_timeout=2.0)

    assert isinstance(caught, RuntimeError), (
        "expected the tool's own RuntimeError from a bounded handshake, not "
        f"{caught!r} — if this is the outer safety margin's TimeoutError instead, "
        "the handshake is not honouring `timeout`"
    )
    assert elapsed < 25.0, f"handshake blocked for {elapsed:.1f}s — pre-fix this hung for 3600s"


def test_handshake_stuck_before_auth_required_ends_within_timeout(monkeypatch):
    """A server that never sends anything (not even `auth_required`) must
    also not hang past `timeout`."""
    async def handler(ws):
        await asyncio.sleep(3600)

    elapsed, caught = _run_against_fake_server(monkeypatch, handler, call_timeout=2.0)

    assert isinstance(caught, RuntimeError), (
        "expected the tool's own RuntimeError from a bounded handshake, not "
        f"{caught!r} — if this is the outer safety margin's TimeoutError instead, "
        "the handshake is not honouring `timeout`"
    )
    assert elapsed < 25.0, f"handshake blocked for {elapsed:.1f}s — pre-fix this hung for 3600s"


def test_handshake_timeout_raises_readable_error(monkeypatch):
    """The error raised on a stuck handshake names what it was waiting for,
    not a bare `TimeoutError` with no context."""
    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2024.1.0"}))
        await asyncio.sleep(3600)

    elapsed, caught = _run_against_fake_server(monkeypatch, handler, call_timeout=1.0)

    assert isinstance(caught, RuntimeError), f"expected a readable RuntimeError, got {caught!r}"
    assert "handshake" in str(caught).lower() and "timed out" in str(caught).lower()
    assert elapsed < 25.0, f"handshake blocked for {elapsed:.1f}s — pre-fix this hung for 3600s"
