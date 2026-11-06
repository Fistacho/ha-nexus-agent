"""Regression tests for `tools/hacs.py` on Home Assistant 2026.11+ (Marketplace).

From HA 2026.11 HACS is built into core as the **Marketplace**
(`homeassistant/components/marketplace`). Live on HA 2026.11.0 (2026-11-06)
every `hacs/*` WebSocket command answers
`{'code': 'unknown_command', 'message': 'Unknown command.'}` — e.g.
`hacs_list_hacs_critical_updates` returned exactly that. The commands are
`marketplace/*` now (core tag 2026.11.0,
`homeassistant/components/marketplace/websocket/`):

* `marketplace/repositories/list`  — optional `categories` (repositories.py:64)
* `marketplace/repository/info`    — `repository_id` (repository.py:78)
* `marketplace/repository/install` — `repository` + optional `version`,
  `confirm_replace_built_in`; needs the accepted warning (repository.py:256-264)
* `marketplace/repository/uninstall` — `repository` (repository.py:317)
* `marketplace/repository/refresh` — `repository` (repository.py:399)
* `marketplace/repositories/add`   — `repository` + `category`; needs the
  accepted warning and a connected GitHub account (repositories.py:213-220)

Errors before the first-run warning is accepted: code `warning_not_accepted`
(decorators.py:18, 127-136).

These tests drive the real `ha_client._ws_call` against a local fake HA
WebSocket server, so the error shape is exactly what `ha_client` raises in
production.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

import ha_client as ha
from tools import hacs as hacs_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


_UNKNOWN = {"code": "unknown_command", "message": "Unknown command."}

_REPO_LIST = [
    {"id": "1", "full_name": "a/one", "category": "plugin", "installed": True, "pending_upgrade": True},
    {"id": "2", "full_name": "b/two", "category": "integration", "installed": True, "pending_upgrade": False},
]

_NEEDS_WARNING = {"marketplace/repository/install", "marketplace/repositories/add"}
_MARKETPLACE_EMPTY_OK = {
    "marketplace/repository/install",
    "marketplace/repository/uninstall",
    "marketplace/repository/refresh",
    "marketplace/repositories/add",
}


def _marketplace_ha(warning_accepted: bool = True, github_connected: bool = True):
    """Answer like HA 2026.11.0: `marketplace/*` exists, `hacs/*` does not."""

    def answer(msg: dict):
        t = msg["type"]
        if t in _NEEDS_WARNING and not warning_accepted:
            return False, {
                "code": "warning_not_accepted",
                "message": "Open the Marketplace and read the warning first, "
                "installing and updating work once it is accepted.",
            }
        if t == "marketplace/repositories/add" and not github_connected:
            return False, {
                "code": "github_not_connected",
                "message": "Connect a GitHub account to the Marketplace first.",
            }
        if t == "marketplace/repositories/list":
            return True, _REPO_LIST
        if t == "marketplace/repository/info":
            return True, {"id": msg["repository_id"], "available_version": "2.0.0", "installed_version": "1.0.0"}
        if t in _MARKETPLACE_EMPTY_OK:
            return True, None
        return False, _UNKNOWN

    return answer


def _legacy_hacs_ha():
    """Answer like HA before 2026.11 with HACS: no `marketplace/*` commands."""

    def answer(msg: dict):
        t = msg["type"]
        if t == "hacs/repositories/list":
            return True, _REPO_LIST
        if t == "hacs/repository/info":
            return True, {"repository_id": msg["repository_id"], "available_version": "2.0.0"}
        if t in ("hacs/repository/download", "hacs/repository/remove", "hacs/repository/refresh", "hacs/repositories/add"):
            return True, {}
        return False, _UNKNOWN

    return answer


def _run_against_fake_ha(monkeypatch, answer, call, delays: dict[str, float] | None = None):
    """Serve `answer` as a fake HA WS endpoint and run `call()` in a worker thread.

    `delays` maps a command type to seconds the fake waits before answering
    it (a slow install). Returns `(result, types, seen)`: the tool's return
    value, the command types the fake received in order, and the full
    command messages (one connection per `_ws_call`, as in production).
    """
    seen: list[dict] = []

    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.11.0"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.11.0"}))
        async for raw in ws:
            msg = json.loads(raw)
            seen.append(msg)
            if delays and msg["type"] in delays:
                await asyncio.sleep(delays[msg["type"]])
            ok, payload = answer(msg)
            reply = {"id": msg["id"], "type": "result", "success": ok}
            reply["result" if ok else "error"] = payload
            try:
                await ws.send(json.dumps(reply))
            except websockets.ConnectionClosed:
                return  # the client gave up waiting (timeout tests)

    async def drive():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(ha, "_ws_url", lambda: f"ws://127.0.0.1:{port}/api/websocket")
        try:
            return await asyncio.wait_for(asyncio.to_thread(call), timeout=30)
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(drive())
    return result, [m["type"] for m in seen], seen


def _bodies(seen: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if k != "id"} for m in seen]


# --- HA 2026.11+: every tool uses marketplace/* --------------------------------


def test_marketplace_list_critical_updates_does_not_return_unknown_command(monkeypatch):
    """The live failure: `hacs_list_hacs_critical_updates` on HA 2026.11.0."""
    result, types, _ = _run_against_fake_ha(
        monkeypatch, _marketplace_ha(), lambda: _unwrap(hacs_tools.list_hacs_critical_updates)()
    )

    assert types == ["marketplace/repositories/list"]
    assert result == [_REPO_LIST[0]]


def test_marketplace_list_repositories_passes_categories(monkeypatch):
    result, _, seen = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.list_hacs_repositories)(category="plugin"),
    )

    assert _bodies(seen) == [{"type": "marketplace/repositories/list", "categories": ["plugin"]}]
    assert result == _REPO_LIST


def test_marketplace_get_repository_uses_info_with_repository_id(monkeypatch):
    result, _, seen = _run_against_fake_ha(
        monkeypatch, _marketplace_ha(), lambda: _unwrap(hacs_tools.get_hacs_repository)("1")
    )

    assert _bodies(seen) == [{"type": "marketplace/repository/info", "repository_id": "1"}]
    assert result["id"] == "1"


def test_marketplace_install_uses_install_command(monkeypatch):
    _, _, seen = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.install_hacs_repository)("1", version="v1.2.3"),
    )

    assert _bodies(seen) == [{"type": "marketplace/repository/install", "repository": "1", "version": "v1.2.3"}]


def test_marketplace_uninstall_uses_uninstall_command(monkeypatch):
    _, _, seen = _run_against_fake_ha(
        monkeypatch, _marketplace_ha(), lambda: _unwrap(hacs_tools.uninstall_hacs_repository)("1")
    )

    assert _bodies(seen) == [{"type": "marketplace/repository/uninstall", "repository": "1"}]


def test_marketplace_add_custom_repository_uses_repositories_add(monkeypatch):
    _, _, seen = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.add_custom_repository)("https://github.com/foo/bar", "integration"),
    )

    assert _bodies(seen) == [
        {"type": "marketplace/repositories/add", "repository": "https://github.com/foo/bar", "category": "integration"}
    ]


def test_marketplace_update_refreshes_reads_info_then_installs_available_version(monkeypatch):
    _, _, seen = _run_against_fake_ha(
        monkeypatch, _marketplace_ha(), lambda: _unwrap(hacs_tools.update_hacs_repository)("1")
    )

    assert _bodies(seen) == [
        {"type": "marketplace/repository/refresh", "repository": "1"},
        {"type": "marketplace/repository/info", "repository_id": "1"},
        {"type": "marketplace/repository/install", "repository": "1", "version": "2.0.0"},
    ]


# --- HA 2026.11+: first-run warning not accepted yet ---------------------------


def test_marketplace_install_before_warning_accepted_gives_settings_hint(monkeypatch):
    result, types, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(warning_accepted=False),
        lambda: _unwrap(hacs_tools.install_hacs_repository)("1"),
    )

    # Nexus must never accept the warning on the user's behalf.
    assert "marketplace/warning/accept" not in types
    assert types == ["marketplace/repository/install"]
    assert result["code"] == "warning_not_accepted"
    assert "Settings" in result["hint"] and "Marketplace" in result["hint"]
    assert "unknown_command" not in json.dumps(result)


def test_marketplace_update_before_warning_accepted_gives_settings_hint(monkeypatch):
    result, types, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(warning_accepted=False),
        lambda: _unwrap(hacs_tools.update_hacs_repository)("1"),
    )

    assert "marketplace/warning/accept" not in types
    assert types[-1] == "marketplace/repository/install"
    assert result["code"] == "warning_not_accepted"
    assert "Settings" in result["hint"] and "Marketplace" in result["hint"]


def test_marketplace_add_custom_repository_without_github_gives_hint(monkeypatch):
    result, _, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(github_connected=False),
        lambda: _unwrap(hacs_tools.add_custom_repository)("https://github.com/foo/bar", "integration"),
    )

    assert result["code"] == "github_not_connected"
    assert "GitHub" in result["hint"]


# --- HA before 2026.11 with HACS: falls back to hacs/* -------------------------


def test_legacy_hacs_list_critical_updates_falls_back_to_hacs(monkeypatch):
    result, types, _ = _run_against_fake_ha(
        monkeypatch, _legacy_hacs_ha(), lambda: _unwrap(hacs_tools.list_hacs_critical_updates)()
    )

    assert types == ["marketplace/repositories/list", "hacs/repositories/list"]
    assert result == [_REPO_LIST[0]]


def test_legacy_hacs_update_stays_on_hacs_after_detecting_it(monkeypatch):
    _, types, _ = _run_against_fake_ha(
        monkeypatch, _legacy_hacs_ha(), lambda: _unwrap(hacs_tools.update_hacs_repository)("1")
    )

    # One probe of marketplace/*, then the whole update runs on HACS.
    assert types == [
        "marketplace/repository/refresh",
        "hacs/repository/refresh",
        "hacs/repository/info",
        "hacs/repository/download",
    ]


def test_neither_marketplace_nor_hacs_gives_readable_error(monkeypatch):
    result, types, _ = _run_against_fake_ha(
        monkeypatch,
        lambda msg: (False, _UNKNOWN),
        lambda: _unwrap(hacs_tools.list_hacs_repositories)(),
    )

    assert types == ["marketplace/repositories/list", "hacs/repositories/list"]
    assert "Marketplace" in result["error"] and "HACS" in result["error"]
    assert result["hint"]


# --- Through the FastMCP layer: Marketplace answers `result: null` -------------
#
# install/uninstall/refresh/repositories/add answer `result_message(id)` with
# no payload, i.e. `"result": null` (core 2026.11.0
# marketplace/websocket/repository.py:289, 394, 428; repositories.py:309).
# The tools are typed `-> dict | list`, so FastMCP validates the structured
# output: a raw `None` made `call_tool` fail with "Invalid structured content"
# although HA had done the work. `_unwrap()` skips that layer, so these tests
# go through `fastmcp.Client`.

from fastmcp import Client  # noqa: E402

_WRITE_CALLS = [
    ("install_hacs_repository", {"repo_id": "1"}),
    ("install_hacs_repository", {"repo_id": "1", "version": "v1.2.3"}),
    ("uninstall_hacs_repository", {"repo_id": "1"}),
    ("add_custom_repository", {"url": "https://github.com/a/b", "category": "plugin"}),
    ("update_hacs_repository", {"repo_id": "1"}),
]


def _marketplace_null_ws(msg_type, **kwargs):
    if msg_type.startswith("hacs/"):
        raise ha.HAWebSocketError(_UNKNOWN)
    if msg_type == "marketplace/repository/info":
        return {"id": kwargs["repository_id"], "available_version": "2.0.0"}
    return None


def _legacy_empty_ws(msg_type, **kwargs):
    if msg_type.startswith("marketplace/"):
        raise ha.HAWebSocketError(_UNKNOWN)
    if msg_type == "hacs/repository/info":
        return {"repository_id": kwargs["repository_id"], "available_version": "2.0.0"}
    return {}


def _patch_ws(monkeypatch, fake):
    """Fake both `ha_client` entry points (install/update use `_ws_call_timeout`)."""
    monkeypatch.setattr(ha, "_ws_call", fake)
    monkeypatch.setattr(ha, "_ws_call_timeout", lambda msg_type, timeout, **kw: fake(msg_type, **kw))


async def _call_tool(server_mcp, name: str, args: dict):
    async with Client(server_mcp) as client:
        return await client.call_tool(name, args)


def test_marketplace_write_tools_return_status_through_fastmcp(monkeypatch):
    _patch_ws(monkeypatch, _marketplace_null_ws)

    for name, args in _WRITE_CALLS:
        result = asyncio.run(_call_tool(hacs_tools.mcp, name, args))
        assert not result.is_error, name
        assert result.data == {"status": "ok", "backend": "marketplace"}, name


def test_legacy_hacs_write_tools_return_the_same_status_shape(monkeypatch):
    _patch_ws(monkeypatch, _legacy_empty_ws)

    for name, args in _WRITE_CALLS:
        result = asyncio.run(_call_tool(hacs_tools.mcp, name, args))
        assert not result.is_error, name
        assert result.data == {"status": "ok", "backend": "hacs"}, name


def test_marketplace_install_through_full_server(monkeypatch):
    import server

    _patch_ws(monkeypatch, _marketplace_null_ws)

    result = asyncio.run(_call_tool(server.mcp, "hacs_install_hacs_repository", {"repo_id": "1"}))

    assert not result.is_error
    assert result.data == {"status": "ok", "backend": "marketplace"}


# --- No answer from HA (timeout, refused connection) ---------------------------


def test_timeout_on_marketplace_probe_is_not_called_a_rejection_and_does_not_fall_back(monkeypatch):
    """Old HA or new: a probe that gets no answer must not try `hacs/*` next,
    must not claim a backend, and must not say the command was rejected."""
    calls = []

    def ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        raise RuntimeError("HA WebSocket handshake timed out waiting for result")

    _patch_ws(monkeypatch, ws_call)

    result = _unwrap(hacs_tools.install_hacs_repository)("1")

    assert calls == ["marketplace/repository/install"]
    assert result["backend"] is None
    assert result["code"] is None
    assert "rejected" not in json.dumps(result).lower()
    assert "Marketplace:" not in result["error"]
    assert "hacs_get_hacs_repository" in result["hint"]


def test_connection_refused_on_legacy_hacs_is_not_labelled_marketplace(monkeypatch):
    calls = []

    def ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        if msg_type.startswith("marketplace/"):
            raise ha.HAWebSocketError(_UNKNOWN)
        raise ConnectionRefusedError(10061, "refused")

    _patch_ws(monkeypatch, ws_call)

    result = _unwrap(hacs_tools.list_hacs_repositories)()

    assert calls == ["marketplace/repositories/list", "hacs/repositories/list"]
    assert result["backend"] is None
    assert "Marketplace:" not in result["error"]
    assert "rejected" not in json.dumps(result).lower()


def test_timeout_after_backend_detected_keeps_that_backend(monkeypatch):
    """`update` pins the backend after `refresh`; a timeout on the install
    step then names it, and still points at checking state before a retry."""

    def ws_call(msg_type, **kwargs):
        if msg_type == "marketplace/repository/info":
            return {"id": "1", "available_version": "2.0.0"}
        if msg_type == "marketplace/repository/install":
            raise RuntimeError("HA WebSocket handshake timed out waiting for result")
        return None

    _patch_ws(monkeypatch, ws_call)

    result = _unwrap(hacs_tools.update_hacs_repository)("1")

    assert result["backend"] == "marketplace"
    assert "rejected" not in json.dumps(result).lower()
    assert "hacs_get_hacs_repository" in result["hint"]


# --- Slow installs: install/update wait longer than the 10 s default -----------
#
# `ha_client._ws_call` bounds the whole call by `_WS_HANDSHAKE_TIMEOUT` (10 s).
# A Marketplace install downloads up to 100 MB (marketplace/const.py:36) and
# reloads the integration, so on a real HA it would routinely end in "No
# answer" while HA keeps installing. Install and update (both backends) wait
# up to `hacs._INSTALL_TIMEOUT`; every other command keeps the default. The
# constants are shrunk here so the tests stay fast.


def _shrink_timeouts(monkeypatch, default: float = 0.3, install: float = 5.0) -> None:
    monkeypatch.setattr(ha, "_WS_HANDSHAKE_TIMEOUT", default)
    monkeypatch.setattr(hacs_tools, "_INSTALL_TIMEOUT", install, raising=False)


def test_marketplace_install_slower_than_default_timeout_succeeds(monkeypatch):
    _shrink_timeouts(monkeypatch)

    result, types, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.install_hacs_repository)("1"),
        delays={"marketplace/repository/install": 1.0},
    )

    assert types == ["marketplace/repository/install"]
    assert result == {"status": "ok", "backend": "marketplace"}


def test_marketplace_update_slower_than_default_timeout_succeeds(monkeypatch):
    _shrink_timeouts(monkeypatch)

    result, _, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.update_hacs_repository)("1"),
        delays={"marketplace/repository/install": 1.0},
    )

    assert result == {"status": "ok", "backend": "marketplace"}


def test_legacy_hacs_install_and_update_slower_than_default_timeout_succeed(monkeypatch):
    _shrink_timeouts(monkeypatch)

    for call in (
        lambda: _unwrap(hacs_tools.install_hacs_repository)("1", version="2.0.0"),
        lambda: _unwrap(hacs_tools.update_hacs_repository)("1"),
    ):
        result, types, _ = _run_against_fake_ha(
            monkeypatch, _legacy_hacs_ha(), call, delays={"hacs/repository/download": 1.0}
        )
        assert types[-1] == "hacs/repository/download"
        assert result == {"status": "ok", "backend": "hacs"}


def test_other_commands_keep_the_default_timeout(monkeypatch):
    """Only install/update wait longer: a slow list still ends in "No answer"."""
    _shrink_timeouts(monkeypatch)

    result, _, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.list_hacs_repositories)(),
        delays={"marketplace/repositories/list": 1.0},
    )

    assert result["code"] is None
    assert result["error"].startswith("No answer from Home Assistant")


def test_install_waits_still_bounded_by_install_timeout(monkeypatch):
    _shrink_timeouts(monkeypatch, install=0.5)

    result, _, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.install_hacs_repository)("1"),
        delays={"marketplace/repository/install": 2.0},
    )

    assert result["error"].startswith("No answer from Home Assistant")
    assert "hacs_get_hacs_repository" in result["hint"]


def test_ws_call_timeout_passes_a_command_field_named_timeout_through(monkeypatch):
    """`timeout` is positional-only, so an HA command field of that name
    still reaches HA (ha_client.py: why `_ws_call` has no timeout keyword)."""
    _, _, seen = _run_against_fake_ha(
        monkeypatch,
        lambda msg: (True, None),
        lambda: ha._ws_call_timeout("some/command", 5.0, timeout=42),
    )

    assert _bodies(seen) == [{"type": "some/command", "timeout": 42}]


# --- Add-on mode: nexus reaches HA as the Supervisor system user ---------------
#
# As an add-on nexus talks to `http://supervisor/core` with SUPERVISOR_TOKEN
# (run.sh, auth.get_ha_token). Supervisor's WS proxy logs in to Core with its
# own token or the Supervisor unix socket (supervisor 2026.10.5
# homeassistant/api.py:254-279; core 2026.11.0 http/auth.py:189-210), so
# `connection.user` is the Supervisor system user, never the owner. The
# Marketplace stores the warning acceptance per user (marketplace/base.py:
# 483-485, websocket/decorators.py:127-136) and nobody can accept it as that
# user in the UI — the standalone hint ("accept it as the token's user") is
# not doable there.


def _warning_not_accepted_ws(msg_type, **kwargs):
    if msg_type.startswith("marketplace/"):
        raise ha.HAWebSocketError({"code": "warning_not_accepted", "message": "Open the Marketplace first."})
    raise ha.HAWebSocketError(_UNKNOWN)


def test_addon_mode_warning_hint_does_not_promise_accepting_as_token_user(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    _patch_ws(monkeypatch, _warning_not_accepted_ws)

    result = _unwrap(hacs_tools.install_hacs_repository)("1")

    hint = result["hint"]
    assert result["code"] == "warning_not_accepted"
    assert "Supervisor" in hint
    assert "owns nexus's access token" not in hint
    # the routes that do work: the owner in the UI, or an automation's update.install
    assert "Settings -> Marketplace" in hint
    assert "update.install" in hint and "automation" in hint
    # the automation route is the owner's call, never nexus's own initiative
    assert "only when the owner explicitly asks for it" in hint
    assert "never on its own after a `warning_not_accepted` answer" in hint


def test_standalone_warning_hint_points_at_the_token_user(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    _patch_ws(monkeypatch, _warning_not_accepted_ws)

    result = _unwrap(hacs_tools.install_hacs_repository)("1")

    assert "owns nexus's access token" in result["hint"]
    assert "Supervisor" not in result["hint"]


# --- update stops at a refresh the Marketplace rejects -------------------------


def test_update_stops_when_marketplace_refresh_is_rejected(monkeypatch):
    calls = []

    def ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        if msg_type == "marketplace/repository/refresh":
            raise ha.HAWebSocketError({"code": "github_rate_limited", "message": "GitHub limits ..."})
        return {"id": "1", "available_version": "2.0.0"}

    _patch_ws(monkeypatch, ws_call)

    result = _unwrap(hacs_tools.update_hacs_repository)("1")

    assert calls == ["marketplace/repository/refresh"]
    assert result["code"] == "github_rate_limited"
    assert result["backend"] == "marketplace"


# --- uninstall may refresh from GitHub first: same longer bound ----------------


def test_install_timeout_is_120_seconds():
    assert hacs_tools._INSTALL_TIMEOUT == 120.0


def test_marketplace_uninstall_slower_than_default_timeout_succeeds(monkeypatch):
    """With GitHub connected, uninstall runs update_repository(force=True)
    before removing files (marketplace/websocket/repository.py:372-379)."""
    _shrink_timeouts(monkeypatch)

    result, _, _ = _run_against_fake_ha(
        monkeypatch,
        _marketplace_ha(),
        lambda: _unwrap(hacs_tools.uninstall_hacs_repository)("1"),
        delays={"marketplace/repository/uninstall": 1.0},
    )

    assert result == {"status": "ok", "backend": "marketplace"}


# --- only transport/HA failures become "No answer"; bugs are not masked ---------


def test_programming_error_is_not_reported_as_no_answer(monkeypatch):
    def ws_call(msg_type, **kwargs):
        raise KeyError("result")

    _patch_ws(monkeypatch, ws_call)

    with pytest.raises(KeyError):
        _unwrap(hacs_tools.list_hacs_repositories)()


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("HA WebSocket handshake timed out waiting for result"),
        ConnectionRefusedError(10061, "refused"),
        TimeoutError("timed out"),
        websockets.ConnectionClosedError(None, None),
        websockets.InvalidURI("ws://x", "bad"),
        json.JSONDecodeError("Expecting value", "", 0),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transport_failures_are_reported_as_no_answer(monkeypatch, exc):
    def ws_call(msg_type, **kwargs):
        raise exc

    _patch_ws(monkeypatch, ws_call)

    result = _unwrap(hacs_tools.list_hacs_repositories)()

    assert result["error"].startswith("No answer from Home Assistant")
