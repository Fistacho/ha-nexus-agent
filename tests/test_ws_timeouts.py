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
4. The *closing* handshake after a bounded handshake timeout is itself
   bounded — `websockets.connect(...)` defaults `close_timeout` to 10s,
   which `_ws_send_recv` didn't override, so a peer that never completes the
   WS closing handshake added ~10s on top of `timeout` regardless of how
   small `timeout` was.
5. `ha_client._ws_call_async` / `ha_client._ws_collect_events_async` — a
   second, independent WebSocket layer used directly by ~30 `tools/*.py`
   modules via `ha._ws_call`/`ha._ws_collect_events` — had the same
   unbounded `auth_required`/`auth_ok` handshake as (3.) predated the fix
   there, with no equivalent fix of its own.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets

import ha_client
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


# --- 4. the closing handshake after a bounded handshake timeout is itself bounded ---
#
# `websockets.connect(...)` defaults `close_timeout` to 10s. `_ws_send_recv`'s
# `async with websockets.connect(...) as ws:` calls `ws.close()` on *every*
# exit, including the `RuntimeError` raised by `_ws_recv_within` once the
# caller's own `timeout` elapses. Against a peer that never completes the
# closing handshake, that adds the library's default 10s on top of `timeout`
# — a `timeout=1` call can take ~11s, breaking the "blocks for up to
# `timeout` seconds" promise in every `ws_*` tool's docstring (ADR-0003 #7).
#
# The `websockets.serve`-based fixtures above (tests 3.) don't reliably show
# this: a `websockets.serve` handler stuck in `asyncio.sleep(3600)` still gets
# its closing handshake auto-echoed by the *library's own* transport-level
# `data_received` callback, which runs independently of the stuck handler
# task — so whether the close resolves quickly or blocks for the full
# `close_timeout` is a race that was observed to flip roughly 50/50 across
# runs of this file (confirmed by instrumented reruns), not a dependable
# regression signal. A raw `asyncio.start_server` that speaks just enough of
# the opening handshake by hand and then goes completely silent has no such
# library-level auto-echo to race against, so the closing handshake
# deterministically never completes on its own — this is what makes the test
# below a dependable RED before the fix and a dependable GREEN after it.

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(sec_key: str) -> str:
    import base64
    import hashlib

    # SHA-1 here is RFC 6455's mandated Sec-WebSocket-Accept checksum, not a
    # security control, so it's exempt from the "insecure hash" lint.
    digest = hashlib.sha1((sec_key + _WS_GUID).encode(), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode()


def _encode_text_frame(payload: str) -> bytes:
    data = payload.encode("utf-8")
    length = len(data)
    if length < 126:
        header = bytes([0x81, length])
    else:
        header = bytes([0x81, 126]) + length.to_bytes(2, "big")
    return header + data


async def _raw_stuck_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Complete the WS opening handshake by hand, send `auth_required`, then
    go completely silent at the socket level — no more reads, no more
    writes, no close. Unlike a `websockets.serve` handler stuck in
    `asyncio.sleep`, there is no library-level protocol object on this side
    to auto-echo a close frame back, so the client's closing handshake is
    guaranteed to never complete by itself.
    """
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = await reader.read(4096)
        if not chunk:
            return
        request += chunk
    headers = {}
    for line in request.split(b"\r\n")[1:]:
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.strip().lower()] = value.strip()
    accept = _ws_accept_key(headers.get(b"sec-websocket-key", b"").decode())
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    writer.write(response.encode())
    await writer.drain()
    writer.write(_encode_text_frame(json.dumps({"type": "auth_required", "ha_version": "2024.1.0"})))
    await writer.drain()
    await asyncio.sleep(3600)


def test_close_after_stuck_handshake_does_not_add_default_close_timeout(monkeypatch):
    """The whole call — handshake wait *and* teardown — must stay close to
    `timeout`, not `timeout` plus the library's unrelated 10s default
    `close_timeout`.

    Bound is deliberately tight (`timeout + 1.5s`), unlike test 3.'s `< 25.0`
    margin: this test's raw server (see module comment above) makes the
    closing handshake deterministically never resolve on its own, so there
    is no race to leave slack for — pre-fix this reliably takes ~11s for a
    `timeout=1.0` call (confirmed: 11.01s, 11.01s, 11.01s, 11.03s across
    repeated runs); post-fix it reliably takes ~2.0s.

    Uses `127.0.0.1` rather than `localhost`: resolving the literal
    `localhost` hostname adds a reproducible ~2s of its own on this dev
    machine (IPv6 `::1` attempted before falling back to IPv4), which would
    swamp the ~1s difference this test is bounding — an unrelated, purely
    environmental cost the other tests in this file sidestep with their much
    wider margins.
    """
    call_timeout = 1.0

    async def _drive():
        server = await asyncio.start_server(_raw_stuck_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(websocket_tools, "_ws_url", lambda: f"ws://127.0.0.1:{port}/api/websocket")
        monkeypatch.setattr(websocket_tools, "_get_token", lambda: "test-token")

        start = time.monotonic()
        caught: BaseException | None = None
        try:
            await asyncio.wait_for(
                websocket_tools._ws_send_recv([{"type": "get_states"}], timeout=call_timeout),
                timeout=call_timeout + 20.0,
            )
        except BaseException as exc:  # noqa: BLE001 - test wants to inspect whatever comes back
            caught = exc
        elapsed = time.monotonic() - start
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=1.0)
        except TimeoutError:
            pass
        return elapsed, caught

    elapsed, caught = asyncio.run(_drive())

    assert isinstance(caught, RuntimeError), f"expected the tool's own RuntimeError, got {caught!r}"
    assert elapsed <= call_timeout + 1.5, (
        f"handshake+close blocked for {elapsed:.2f}s against timeout={call_timeout}s — "
        "the WebSocket closing handshake is adding the library's default "
        "close_timeout (10s) on top of the caller's `timeout`"
    )


# --- 5. ha_client's own WS handshake (_ws_call_async / _ws_collect_events_async) ---
#
# `ha_client._ws_call_async` and `ha_client._ws_collect_events_async` open
# their own `websockets.connect(...)` directly — a second, independent
# WebSocket layer from `tools/websocket.py`'s `_ws_send_recv`, used by ~30
# `tools/*.py` modules via `ha._ws_call`/`ha._ws_collect_events`. Unlike
# `_ws_send_recv`, their `auth_required` -> `auth` -> `auth_ok` handshake had
# *no* bound at all — a peer that sent nothing, or sent `auth_required` and
# then nothing else, hung the call forever, regardless of whether the
# function even accepts a `timeout` parameter.
#
# Same raw-server pattern as section 4. (deterministic: no `websockets`
# library protocol object on the server side to race against), reused here
# via `_raw_stuck_server` (silent *after* `auth_required`) plus a new
# `_raw_silent_from_start` (silent *before* it — the other place a peer can
# go quiet). Pre-fix, both hang forever; the outer `asyncio.wait_for` in
# `_run_ha_client_against_raw_server` is what turns that into a bounded,
# observable RED (`asyncio.TimeoutError`, not the tool's own `RuntimeError`)
# instead of actually hanging the test suite.


async def _raw_silent_from_start(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Complete the WS opening HTTP handshake, then never send anything at
    all — not even `auth_required` — and never read again."""
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = await reader.read(4096)
        if not chunk:
            return
        request += chunk
    headers = {}
    for line in request.split(b"\r\n")[1:]:
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.strip().lower()] = value.strip()
    accept = _ws_accept_key(headers.get(b"sec-websocket-key", b"").decode())
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    writer.write(response.encode())
    await writer.drain()
    await asyncio.sleep(3600)


def _run_ha_client_against_raw_server(monkeypatch, server_handler, call_coro_factory, outer_timeout: float):
    """Start `server_handler` as a raw TCP/WS server, call
    `call_coro_factory()` against it, and return `(elapsed, exception)`.

    `outer_timeout` bounds the whole drive so a pre-fix unbounded hang shows
    up as a prompt `asyncio.TimeoutError` rather than an actual multi-hour
    stall — mirrors `_run_against_fake_server`'s `outer_margin` above, with
    an explicit value per call site instead of a shared default, since (3.)'s
    tests bound an already-somewhat-bounded call while these bound a
    previously fully unbounded one.
    """
    async def _drive():
        server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(ha_client, "_ws_url", lambda: f"ws://127.0.0.1:{port}/api/websocket")
        monkeypatch.setattr(ha_client, "_HA_TOKEN", "test-token")

        start = time.monotonic()
        caught: BaseException | None = None
        try:
            await asyncio.wait_for(call_coro_factory(), timeout=outer_timeout)
        except BaseException as exc:  # noqa: BLE001 - test wants to inspect whatever comes back
            caught = exc
        elapsed = time.monotonic() - start
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=1.0)
        except TimeoutError:
            pass
        return elapsed, caught

    return asyncio.run(_drive())


_WS_CLOSE_TIMEOUT_FOR_ASSERT = 1.0  # mirrors ha_client._WS_CLOSE_TIMEOUT


@pytest.mark.parametrize(
    "server_handler",
    [_raw_silent_from_start, _raw_stuck_server],
    ids=["silent-before-auth_required", "silent-after-auth_required"],
)
def test_ws_call_async_handshake_is_bounded(monkeypatch, server_handler):
    """`_ws_call_async` has no `timeout` parameter (see `_WS_HANDSHAKE_TIMEOUT`'s
    comment for why one wasn't added), so its whole call — handshake included
    — must be bounded by that constant instead. Monkeypatched down to 1.0s
    here so the test runs in ~2s instead of ~11s; production keeps 10.0s.
    """
    monkeypatch.setattr(ha_client, "_WS_HANDSHAKE_TIMEOUT", 1.0)

    def call_coro_factory():
        return ha_client._ws_call_async("get_states")

    elapsed, caught = _run_ha_client_against_raw_server(
        monkeypatch, server_handler, call_coro_factory, outer_timeout=8.0
    )

    assert isinstance(caught, RuntimeError), (
        f"expected _ws_call_async's own bounded RuntimeError, not {caught!r} — "
        "if this is an outer asyncio.TimeoutError instead, the handshake is "
        "still unbounded"
    )
    assert "handshake" in str(caught).lower() and "timed out" in str(caught).lower()
    assert elapsed <= 1.0 + _WS_CLOSE_TIMEOUT_FOR_ASSERT + 1.5, (
        f"_ws_call_async blocked for {elapsed:.2f}s against a 1.0s handshake bound"
    )


@pytest.mark.parametrize(
    "server_handler",
    [_raw_silent_from_start, _raw_stuck_server],
    ids=["silent-before-auth_required", "silent-after-auth_required"],
)
def test_ws_collect_events_async_handshake_is_bounded(monkeypatch, server_handler):
    """`_ws_collect_events_async` already accepts a `timeout` parameter (used
    by `collect_system_health` and the `esphome_*` MQTT tools) — that budget
    must now cover the handshake too, not just the post-auth event loop.
    """
    call_timeout = 1.0

    def call_coro_factory():
        return ha_client._ws_collect_events_async(
            "subscribe_events", is_last=lambda e: False, timeout=call_timeout
        )

    elapsed, caught = _run_ha_client_against_raw_server(
        monkeypatch, server_handler, call_coro_factory, outer_timeout=8.0
    )

    assert isinstance(caught, RuntimeError), (
        f"expected _ws_collect_events_async's own bounded RuntimeError, not {caught!r} — "
        "if this is an outer asyncio.TimeoutError instead, the handshake is "
        "still unbounded"
    )
    assert "handshake" in str(caught).lower() and "timed out" in str(caught).lower()
    assert elapsed <= call_timeout + _WS_CLOSE_TIMEOUT_FOR_ASSERT + 1.5, (
        f"_ws_collect_events_async blocked for {elapsed:.2f}s against timeout={call_timeout}s"
    )
