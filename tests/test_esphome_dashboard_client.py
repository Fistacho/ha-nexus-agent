"""Tests for `esphome_dashboard.py` (ADR-0004 partition A).

Covers, in order:

1. `DashboardLocator` resolution order, URL shape, `ingress_port`
   validation, add-on state/ingress checks, caching, `invalidate()`.
2. `DashboardClient`'s single-retry-on-port-change rule and its absence
   in explicit mode / after a command has already been sent.
3. Error-code mapping for every wire-level failure mode: connection
   refusal, HTTP 401/403 at the WebSocket handshake, `requires_auth:
   true`, a non-JSON frame, a dashboard `error_code`, and timeouts.
4. The closed public method set on `DashboardClient` (S5).

WebSocket-level tests that only assert a frame-shape -> exception/result
mapping (ServerInfo, non-JSON frames, `error_code`, successful results,
spawn protocol) are driven through `_FakeDashboardConnection`, an
in-process stand-in for the dashboard side of the session — no real
socket, no second thread, no close-handshake to race. Only the tests that
need genuine concurrent client/server behaviour a fake can't produce (a
real timeout elapsing, an actual TCP refusal) still run a local
`websockets.serve` server and the (synchronous) `DashboardClient` call
against it on the *same* event loop (`_drive_ws_test`, same overall shape
as `tests/test_ws_timeouts.py`'s `_run_against_fake_server`) — the
synchronous client call itself runs on a worker thread via
`loop.run_in_executor` so it can block without starving the loop that's
servicing the fake server's handler. Calling `DashboardClient` directly
from the server's own loop/thread would deadlock it: `_run_sync` (see
`esphome_dashboard.py`) opens its own nested event loop on whatever thread
it's called from, and that thread being the one and only thread servicing
the fake server means the fake server can never run while the client call
blocks waiting for it.

HTTP-level tests (`ping`) don't have that problem — `httpx`'s blocking
`Client.get` and Python's threaded `http.server` are already on separate
threads by construction — so they use a plain background thread instead.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

import esphome_dashboard as dash


# ── helpers ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _client_for_ws_server(port: int) -> dash.DashboardClient:
    locator = dash.DashboardLocator(
        env={"ESPHOME_DASHBOARD_URL": f"http://127.0.0.1:{port}"},
        supervisor_get=lambda path: {},
        discover_slug=lambda: None,
    )
    return dash.DashboardClient(locator)


async def _drive_ws_test(handler, run_client, *, process_request=None):
    """Run `handler` as a local WS server and `run_client(port)` (a plain
    sync callable) against it, on the same event loop but different
    threads (see module docstring). Returns whatever `run_client` returns;
    an exception `run_client` raises propagates out of this coroutine
    exactly as if it had been called directly.
    """
    server = await websockets.serve(handler, "127.0.0.1", 0, process_request=process_request)
    try:
        port = server.sockets[0].getsockname()[1]
        return await asyncio.get_event_loop().run_in_executor(None, lambda: run_client(port))
    finally:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


def _make_locator(
    *,
    env: dict | None = None,
    supervisor_get=None,
    discover_slug=None,
) -> dash.DashboardLocator:
    return dash.DashboardLocator(
        env=env if env is not None else {},
        supervisor_get=supervisor_get if supervisor_get is not None else (lambda path: {}),
        discover_slug=discover_slug if discover_slug is not None else (lambda: None),
    )


def _addon_info(*, state="started", ingress=True, ingress_port=65490) -> dict:
    return {"data": {"state": state, "ingress": ingress, "ingress_port": ingress_port}}


class _FakeDashboardConnection:
    """In-process stand-in for the dashboard side of a WebSocket session.

    Records every frame the client sends (`.sent`) and answers each
    `recv()` from a pre-scripted queue — no real socket, no second event
    loop/thread. Each item in `script` is either a `str` (returned as-is)
    or a callable `(last_sent: str | None) -> str` (invoked lazily so a
    reply can echo something from the client's own last frame, e.g. its
    `message_id` — see `test_validate_success_result`).

    Only usable for tests where send/recv ordering is entirely known
    upfront (every happy-path test below). Tests that need genuine
    concurrent client/server behaviour (a real timeout elapsing, an actual
    TCP refusal) still drive a real local `websockets.serve`/socket — see
    `_drive_ws_test`/`_client_for_ws_server` and their own docstrings.
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self):
        if not self._script:
            raise AssertionError(
                "_FakeDashboardConnection script exhausted — recv() called more "
                "times than scripted"
            )
        item = self._script.pop(0)
        if callable(item):
            return item(self.sent[-1] if self.sent else None)
        return item

    async def close(self) -> None:
        return None


def _fake_connect_returning(conn: "_FakeDashboardConnection"):
    """A `dash.websockets.connect` replacement that hands back `conn` directly.

    Matches `_ws_connect`'s own usage (`return await websockets.connect(...)`,
    never `async with`) — same shape as the `fake_connect` already used by
    `test_validate_http_401_or_403_at_handshake_is_auth_required` below, just
    returning a connection instead of raising.
    """

    async def fake_connect(url, **kwargs):
        return conn

    return fake_connect


class _CountingSupervisorGet:
    """Fake `supervisor_get` that counts calls and returns a fixed response."""

    def __init__(self, response: dict):
        self.response = response
        self.calls: list[str] = []

    def __call__(self, path: str) -> dict:
        self.calls.append(path)
        return self.response


class _CountingDiscoverSlug:
    def __init__(self, slug: str | None):
        self.slug = slug
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.slug


# ── 1. locator: resolution order ─────────────────────────────────────────────


def test_explicit_url_wins_even_in_addon_mode_and_skips_supervisor():
    supervisor_get = _CountingSupervisorGet(_addon_info())
    discover_slug = _CountingDiscoverSlug("a0d7b954_esphome")
    locator = _make_locator(
        env={"ESPHOME_DASHBOARD_URL": "http://example.local:6052", "SUPERVISOR_TOKEN": "tok"},
        supervisor_get=supervisor_get,
        discover_slug=discover_slug,
    )

    endpoint = locator.locate()

    assert endpoint.source == "explicit"
    assert endpoint.base_url == "http://example.local:6052"
    assert endpoint.slug is None
    assert endpoint.ingress_port is None
    assert supervisor_get.calls == []
    assert discover_slug.calls == 0


def test_explicit_url_rejects_non_http_scheme():
    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "ftp://example.local/dash"})

    with pytest.raises(dash.NotConfiguredError) as exc_info:
        locator.locate()

    assert exc_info.value.code == "esphome_not_configured"


def test_explicit_url_rejects_missing_host():
    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http:///no-host"})

    with pytest.raises(dash.NotConfiguredError):
        locator.locate()


def test_no_explicit_url_and_no_supervisor_token_is_not_configured():
    locator = _make_locator(env={})

    with pytest.raises(dash.NotConfiguredError) as exc_info:
        locator.locate()

    assert exc_info.value.code == "esphome_not_configured"


def test_addon_mode_url_is_always_127_0_0_1_regardless_of_addon_reported_host():
    """`GET /addons/<slug>/info` carries no reachable host for the dashboard
    itself (only `ingress_port`) — the URL must always be the loopback
    address nexus shares with the add-on under `host_network` (ADR-0004 D1),
    never anything derived from Supervisor's own view of the container."""
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"},
        supervisor_get=lambda path: _addon_info(ingress_port=65490),
        discover_slug=lambda: "a0d7b954_esphome",
    )

    endpoint = locator.locate()

    assert endpoint.base_url == "http://127.0.0.1:65490"
    assert endpoint.source == "supervisor_ingress"
    assert endpoint.slug == "a0d7b954_esphome"
    assert endpoint.ingress_port == 65490


def test_addon_not_found_is_unreachable():
    locator = _make_locator(env={"SUPERVISOR_TOKEN": "tok"}, discover_slug=lambda: None)

    with pytest.raises(dash.UnreachableError) as exc_info:
        locator.locate()

    assert exc_info.value.code == "esphome_unreachable"


def test_addon_stopped_is_unreachable():
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"},
        supervisor_get=lambda path: _addon_info(state="stopped"),
        discover_slug=lambda: "a0d7b954_esphome",
    )

    with pytest.raises(dash.UnreachableError) as exc_info:
        locator.locate()

    assert exc_info.value.code == "esphome_unreachable"
    assert "stopped" in str(exc_info.value) or exc_info.value.extra.get("state") == "stopped"


def test_addon_without_ingress_is_unreachable():
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"},
        supervisor_get=lambda path: _addon_info(ingress=False),
        discover_slug=lambda: "a0d7b954_esphome",
    )

    with pytest.raises(dash.UnreachableError):
        locator.locate()


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 999999, None, "65490", True])
def test_invalid_ingress_port_is_unreachable(bad_port):
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"},
        supervisor_get=lambda path: _addon_info(ingress_port=bad_port),
        discover_slug=lambda: "a0d7b954_esphome",
    )

    with pytest.raises(dash.UnreachableError):
        locator.locate()


def test_supervisor_get_exception_is_unreachable():
    def boom(path):
        raise ConnectionError("supervisor unreachable")

    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"}, supervisor_get=boom, discover_slug=lambda: "a0d7b954_esphome",
    )

    with pytest.raises(dash.UnreachableError):
        locator.locate()


# ── 1b. locator: caching and invalidate() ────────────────────────────────────


def test_addon_mode_result_is_cached_across_locate_calls():
    supervisor_get = _CountingSupervisorGet(_addon_info())
    discover_slug = _CountingDiscoverSlug("a0d7b954_esphome")
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"}, supervisor_get=supervisor_get, discover_slug=discover_slug,
    )

    first = locator.locate()
    second = locator.locate()

    assert first == second
    assert len(supervisor_get.calls) == 1
    assert discover_slug.calls == 1


def test_invalidate_forces_a_fresh_lookup():
    supervisor_get = _CountingSupervisorGet(_addon_info())
    discover_slug = _CountingDiscoverSlug("a0d7b954_esphome")
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"}, supervisor_get=supervisor_get, discover_slug=discover_slug,
    )

    locator.locate()
    locator.invalidate()
    locator.locate()

    assert len(supervisor_get.calls) == 2
    assert discover_slug.calls == 2


def test_explicit_mode_is_never_cached():
    supervisor_get = _CountingSupervisorGet(_addon_info())
    locator = _make_locator(
        env={"ESPHOME_DASHBOARD_URL": "http://example.local:6052"},
        supervisor_get=supervisor_get,
    )

    locator.locate()
    locator.locate()

    # Nothing to "cache" in explicit mode -- confirm indirectly: supervisor
    # is never consulted at all, on either call.
    assert supervisor_get.calls == []


# ── 2. DashboardClient: retry-on-port-change ─────────────────────────────────


def test_ping_retries_once_after_connect_error_when_port_changed(monkeypatch):
    calls: list[str] = []

    def fake_ping_once(base_url, timeout):
        calls.append(base_url)
        if base_url == "http://127.0.0.1:100":
            raise dash._ConnectFailed(dash.UnreachableError("refused"))
        return {"ok": True}

    monkeypatch.setattr(dash, "_ping_once", fake_ping_once)

    ports = iter([100, 200])
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"},
        supervisor_get=lambda path: _addon_info(ingress_port=next(ports)),
        discover_slug=lambda: "a0d7b954_esphome",
    )
    client = dash.DashboardClient(locator)

    result = client.ping()

    assert result == {"ok": True}
    assert calls == ["http://127.0.0.1:100", "http://127.0.0.1:200"]


def test_ping_does_not_retry_when_port_is_unchanged(monkeypatch):
    calls: list[str] = []

    def fake_ping_once(base_url, timeout):
        calls.append(base_url)
        raise dash._ConnectFailed(dash.UnreachableError("refused"))

    monkeypatch.setattr(dash, "_ping_once", fake_ping_once)

    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"},
        supervisor_get=lambda path: _addon_info(ingress_port=65490),
        discover_slug=lambda: "a0d7b954_esphome",
    )
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.UnreachableError):
        client.ping()

    assert calls == ["http://127.0.0.1:65490"]


def test_ping_does_not_retry_in_explicit_mode(monkeypatch):
    calls: list[str] = []

    def fake_ping_once(base_url, timeout):
        calls.append(base_url)
        raise dash._ConnectFailed(dash.UnreachableError("refused"))

    monkeypatch.setattr(dash, "_ping_once", fake_ping_once)

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://example.local:6052"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.UnreachableError):
        client.ping()

    assert calls == ["http://example.local:6052"]


def test_no_retry_once_spawn_payload_already_sent(monkeypatch):
    """Any failure from the wire-level spawn session (i.e. anything other
    than `_ConnectFailed` -- everything past the point `/compile`'s spawn
    payload is sent) must surface as-is, with no second connection attempt
    — nexus cannot know whether the dashboard's firmware job queue already
    started work. `_spawn_async` is faked here (rather than driven through
    a real closed WebSocket) because what this test asserts is
    `DashboardClient._with_retry`'s own dispatch logic, not the wire-level
    `ConnectionClosed` -> `ProtocolError` mapping `_run_spawn_session`
    already carries out (and already exercises for `validate`'s equivalent
    path in `test_validate_non_json_frame_is_protocol_error`)."""

    calls: list[str] = []

    async def fake_spawn_async(base_url, path, payload, timeout, tail_lines):
        calls.append(base_url)
        raise dash.ProtocolError("WebSocket connection closed mid-session")

    monkeypatch.setattr(dash, "_spawn_async", fake_spawn_async)

    supervisor_get = _CountingSupervisorGet(_addon_info(ingress_port=65490))
    locator = _make_locator(
        env={"SUPERVISOR_TOKEN": "tok"}, supervisor_get=supervisor_get, discover_slug=lambda: "slug",
    )
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.ProtocolError):
        client.compile("device.yaml", timeout=5)

    assert calls == ["http://127.0.0.1:65490"]
    # Exactly one Supervisor lookup -- no invalidate()+re-locate retry.
    assert len(supervisor_get.calls) == 1


# ── 3. error-code mapping over real local WS/HTTP servers ───────────────────


@pytest.mark.slow
def test_validate_connection_refused_is_unreachable():
    port = _free_port()  # nothing listens here
    client = _client_for_ws_server(port)

    with pytest.raises(dash.UnreachableError) as exc_info:
        client.validate("device.yaml", timeout=3)

    assert exc_info.value.code == "esphome_unreachable"


class _FakeHandshakeResponse:
    """Minimal stand-in for `websockets.http11.Response` — `InvalidStatus`
    only ever reads `.status_code` off it (see its `__init__`/`__str__` in
    `websockets/exceptions.py`), so a real HTTP response is unnecessary."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.mark.parametrize("status_code", [401, 403])
def test_validate_http_401_or_403_at_handshake_is_auth_required(monkeypatch, status_code):
    """HTTP 401/403 at the WebSocket handshake maps to `esphome_auth_required`.

    Originally driven through a real local `websockets.serve` fake server
    that returned a raw HTTP 401 response before the WebSocket upgrade.
    That was flaky on this sandbox: confirmed, via a bare
    `websockets.connect()` reproduction with none of this module's code
    involved, that `websockets.asyncio.server`'s own connection-teardown
    (`transport.abort()`, called right after `connection.respond()` writes
    the response) races the client's read of that same response — the
    client sometimes sees a clean HTTP/1.1 401 status line (raised as
    `InvalidStatus`, the case this test targets) and sometimes sees the
    socket close mid-read (raised as `InvalidMessage`, a *different*,
    equally real `_ws_connect` code path already covered by
    `test_validate_other_invalid_handshake_is_protocol_error` below) — an
    environment/timing artifact of that specific fake server, independent
    of the 401/403 -> `esphome_auth_required` mapping this test actually
    checks. Replaced with a deterministic `websockets.connect` monkeypatch
    that raises the exact exception `_ws_connect` maps, so this test no
    longer depends on any real socket race.
    """

    async def fake_connect(url, **kwargs):
        raise websockets.exceptions.InvalidStatus(_FakeHandshakeResponse(status_code))

    monkeypatch.setattr(dash.websockets, "connect", fake_connect)

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.AuthRequiredError) as exc_info:
        client.validate("device.yaml", timeout=3)
    assert exc_info.value.code == "esphome_auth_required"


def test_validate_other_invalid_handshake_is_protocol_error(monkeypatch):
    """Any `InvalidHandshake` that is *not* a real HTTP status response
    (e.g. a malformed/absent HTTP response, `InvalidMessage`) must not be
    mistaken for `esphome_auth_required` — `_handshake_status_code` finds
    no `.response`/`.status_code` on it, so `_ws_connect` falls through to
    `esphome_protocol_error`, matching real observed instability on this
    sandbox's loopback networking (see the 401/403 test's docstring)."""

    async def fake_connect(url, **kwargs):
        raise websockets.exceptions.InvalidMessage("did not receive a valid HTTP response")

    monkeypatch.setattr(dash.websockets, "connect", fake_connect)

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.ProtocolError) as exc_info:
        client.validate("device.yaml", timeout=3)
    assert exc_info.value.code == "esphome_protocol_error"


def test_validate_requires_auth_true_in_server_info_is_auth_required(monkeypatch):
    """`requires_auth: true` must fail fast before anything else is sent —
    asserted explicitly here (not just "an `AuthRequiredError` was raised
    eventually") because `NoAuth.check_server_info` runs *before*
    `_run_command_session` sends the `devices/validate` command; a
    regression that reordered those two statements would still raise
    `AuthRequiredError` eventually but would have leaked a command frame to
    a dashboard this client just told the caller it refuses to talk to.

    Driven through `_FakeDashboardConnection` rather than a real local
    `websockets.serve` server: this only asserts the ServerInfo -> exception
    mapping (no real socket behaviour is under test), and the previous
    real-socket version was flaky on this sandbox — `_validate_async`'s own
    `finally: await ws.close()` (see `esphome_dashboard.py`) waits up to
    `_CLOSE_TIMEOUT` (5s) for the peer's close-handshake acknowledgement,
    and that handshake between two real local sockets on this sandbox
    sporadically didn't complete promptly, intermittently stalling this
    test by ~5s instead of failing it outright (same class of loopback
    flakiness already documented on the 401/403 handshake test above)."""

    conn = _FakeDashboardConnection([json.dumps({"requires_auth": True})])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.AuthRequiredError) as exc_info:
        client.validate("device.yaml", timeout=3)
    assert exc_info.value.code == "esphome_auth_required"
    assert conn.sent == []


def test_validate_non_json_frame_is_protocol_error(monkeypatch):
    """Driven through `_FakeDashboardConnection` (see the previous test's
    docstring for why): this only asserts the non-JSON-frame -> exception
    mapping, not any real socket/close-handshake behaviour."""

    conn = _FakeDashboardConnection(["not json at all {{{"])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.ProtocolError) as exc_info:
        client.validate("device.yaml", timeout=3)
    assert exc_info.value.code == "esphome_protocol_error"


def test_validate_error_code_is_command_failed(monkeypatch):
    """Driven through `_FakeDashboardConnection` (see
    `test_validate_requires_auth_true_in_server_info_is_auth_required`'s
    docstring for why): this only asserts the `error_code` -> exception
    mapping, not any real socket/close-handshake behaviour."""

    conn = _FakeDashboardConnection([
        json.dumps({"requires_auth": False}),
        lambda sent: json.dumps({
            "message_id": json.loads(sent)["message_id"],
            "error_code": "invalid_configuration",
            "details": "boom",
        }),
    ])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.CommandFailedError) as exc_info:
        client.validate("device.yaml", timeout=3)
    assert exc_info.value.code == "esphome_command_failed"
    assert exc_info.value.dashboard_error_code == "invalid_configuration"
    assert exc_info.value.details == "boom"


def test_validate_success_result(monkeypatch):
    """Also pins the exact shape of the frame `validate()` sends on the wire
    (module docstring: `{"command": "devices/validate", "message_id": ...,
    "args": {"configuration": ...}}`) — an assertion on `result["success"]`
    alone would pass even if the command name or args shape drifted, since
    this fake connection echoes back whatever `message_id` it was sent
    regardless of the rest of the frame."""

    conn = _FakeDashboardConnection([
        json.dumps({"requires_auth": False}),
        lambda sent: json.dumps({
            "message_id": json.loads(sent)["message_id"],
            "event": "result",
            "data": {"success": True, "code": 0},
        }),
    ])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    result = client.validate("device.yaml", timeout=3)
    assert result["success"] is True

    assert len(conn.sent) == 1
    sent = json.loads(conn.sent[0])
    assert isinstance(sent.get("message_id"), str)
    assert sent == {
        "command": "devices/validate",
        "message_id": sent["message_id"],
        "args": {"configuration": "device.yaml"},
    }


@pytest.mark.slow
def test_validate_times_out_within_deadline():
    async def handler(ws):
        await ws.send(json.dumps({"requires_auth": False}))
        await ws.wait_closed()  # simulate an unresponsive dashboard

    def run_client(port):
        _client_for_ws_server(port).validate("device.yaml", timeout=1)

    with pytest.raises(dash.DashboardTimeoutError) as exc_info:
        asyncio.run(_drive_ws_test(handler, run_client))
    assert exc_info.value.code == "timeout"
    assert exc_info.value.job_may_still_be_running is False


@pytest.mark.slow
def test_compile_timeout_marks_job_may_still_be_running():
    async def handler(ws):
        await ws.recv()  # spawn message
        await ws.wait_closed()  # simulate an unresponsive dashboard

    def run_client(port):
        _client_for_ws_server(port).compile("device.yaml", timeout=1)

    with pytest.raises(dash.DashboardTimeoutError) as exc_info:
        asyncio.run(_drive_ws_test(handler, run_client))
    assert exc_info.value.job_may_still_be_running is True


def test_compile_success_via_spawn_protocol(monkeypatch):
    conn = _FakeDashboardConnection([
        json.dumps({"event": "line", "data": "building..."}),
        json.dumps({"event": "exit", "code": 0}),
    ])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    result = client.compile("device.yaml", timeout=5)

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert "building..." in result["log_tail"]

    assert len(conn.sent) == 1
    msg = json.loads(conn.sent[0])
    assert msg["type"] == "spawn"
    assert msg["configuration"] == "device.yaml"


def test_upload_sends_port_in_spawn_payload(monkeypatch):
    conn = _FakeDashboardConnection([json.dumps({"event": "exit", "code": 0})])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    result = client.upload("device.yaml", "OTA", timeout=5)

    assert result["success"] is True
    assert len(conn.sent) == 1
    msg = json.loads(conn.sent[0])
    assert msg["configuration"] == "device.yaml"
    assert msg["port"] == "OTA"


def test_compile_exit_code_1_is_a_failed_result_not_an_exception():
    """A non-zero exit code is a normal (if unsuccessful) job outcome, not a
    wire-level failure — `compile()` must return a result dict with
    `success: False`, never raise. Regression guard: `_run_spawn_session`'s
    `"success": code == 0` mapping is the only thing standing between this
    and a caller mistaking a *failed build* for a *transport error*."""

    async def handler(ws):
        raw = await ws.recv()
        msg = json.loads(raw)
        assert msg["type"] == "spawn"
        assert msg["configuration"] == "device.yaml"
        await ws.send(json.dumps({"event": "line", "data": "error: something broke"}))
        await ws.send(json.dumps({"event": "exit", "code": 1}))

    def run_client(port):
        return _client_for_ws_server(port).compile("device.yaml", timeout=5)

    result = asyncio.run(_drive_ws_test(handler, run_client))

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert "error: something broke" in result["log_tail"]
    assert result["log_lines"] == 1


def test_upload_exit_code_1_is_a_failed_result_not_an_exception(monkeypatch):
    """Same failed-result guarantee as `compile()`'s exit-code-1 case above,
    for `upload()` — and re-confirms (independent of the exit code) that
    `port` reaches the spawn payload, since `upload()`'s only difference
    from `compile()` is that extra field."""

    conn = _FakeDashboardConnection([
        json.dumps({"event": "line", "data": "flash failed"}),
        json.dumps({"event": "exit", "code": 1}),
    ])
    monkeypatch.setattr(dash.websockets, "connect", _fake_connect_returning(conn))

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    result = client.upload("device.yaml", "OTA", timeout=5)

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert "flash failed" in result["log_tail"]
    assert result["log_lines"] == 1

    assert len(conn.sent) == 1
    msg = json.loads(conn.sent[0])
    assert msg["type"] == "spawn"
    assert msg["configuration"] == "device.yaml"
    assert msg["port"] == "OTA"


# ── ping ──────────────────────────────────────────────────────────────────────
#
# Success is exercised via `httpx.MockTransport` rather than a real local HTTP
# server: Python's `http.server` defaults to HTTP/1.0 (closes the connection
# after every response), which — empirically, on this Windows sandbox, in
# roughly half of repeated runs — races httpx's own connection teardown into
# a spurious `ECONNRESET`/`httpx.ReadError` unrelated to anything this client
# does. `_ping_once` builds its own `httpx.Client(base_url=..., timeout=...)`
# with no injectable transport, so the swap happens one level up, on the real
# `httpx.Client` class itself (reverted automatically by `monkeypatch`).
# `test_ping_connection_refused_is_unreachable` below still dials a real (but
# unlistened) TCP port -- a connection refusal has no such race to have.


def test_ping_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/ping"
        return httpx.Response(200, json={"device.yaml": True})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)

    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": "http://dashboard.invalid"})
    client = dash.DashboardClient(locator)

    result = client.ping()

    assert result == {"device.yaml": True}


@pytest.mark.slow
def test_ping_connection_refused_is_unreachable():
    port = _free_port()
    locator = _make_locator(env={"ESPHOME_DASHBOARD_URL": f"http://127.0.0.1:{port}"})
    client = dash.DashboardClient(locator)

    with pytest.raises(dash.UnreachableError) as exc_info:
        client.ping()

    assert exc_info.value.code == "esphome_unreachable"


# ── 4. closed public method set (S5) ─────────────────────────────────────────


def test_dashboard_client_public_methods_are_exactly_the_closed_set():
    import inspect

    public = {
        name
        for name, member in inspect.getmembers(dash.DashboardClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }

    assert public == {"ping", "validate", "compile", "upload"}


def test_no_public_method_accepts_an_arbitrary_command_or_route_argument():
    import inspect

    for name in ("ping", "validate", "compile", "upload"):
        sig = inspect.signature(getattr(dash.DashboardClient, name))
        params = set(sig.parameters) - {"self"}
        assert "command" not in params
        assert "route" not in params
        assert "path" not in params
