"""ESPHome Device Builder dashboard client (ADR-0004).

This module is the I/O layer between nexus and the ESPHome "Device
Builder" dashboard (github.com/esphome/device-builder) — the successor to
the old `esphome` pip package's built-in dashboard. It sits at the same
level as `ha_client.py`: `tools/esphome.py` (ADR-0004 partition B) is the
only intended caller, translating the typed exceptions raised here into
the `{"error": ...}` tool-contract shape (ADR-0002).

Three pieces, per ADR-0004 D2/D3:

- `DashboardLocator` resolves *where* the dashboard is (D2). Order:
  explicit `ESPHOME_DASHBOARD_URL` env var (mode `"explicit"`, no cache,
  no retry) → in add-on mode (`SUPERVISOR_TOKEN` present) the installed
  ESPHome add-on's own ingress port, always dialled at
  `http://127.0.0.1:<ingress_port>` (mode `"supervisor_ingress"`, cached
  in memory) → otherwise `esphome_not_configured`. All I/O is injected
  (`supervisor_get`, `discover_slug`) so the locator itself never touches
  the network — that keeps it unit-testable and keeps this module
  ignorant of *how* nexus talks to the Supervisor API (see ADR-0004 D-4
  debt note about the three separate Supervisor client copies).

- `DashboardClient` is the closed set of operations nexus is allowed to
  perform against the dashboard (S5): `ping`, `validate`, `compile`,
  `upload`. There is deliberately no method that forwards an arbitrary
  `/ws` command name or an arbitrary HTTP route — a site-ingress peer
  (which is what nexus's `host_network` connection to Device Builder is,
  per ADR-0004 D1) gets a *fully unauthenticated* connection to the
  dashboard, including destructive commands nexus has no business
  exposing (`config/set_secret`, `devices/delete`,
  `version_history/restore`, `remote_build/*`, ...). Adding a new
  dashboard capability means adding a new named method here, reviewed
  against S5 — not widening an existing one to accept a command name.

- `DashboardAuth` is a `Protocol` for the one auth decision this client
  makes: whether to refuse a session because the dashboard says it needs
  a username/password. `NoAuth` is the only implementation — there is no
  credential wiring, so `requires_auth: true` (from the WebSocket
  `ServerInfoMessage`) or an HTTP 401/403 at the WebSocket handshake
  fails fast as `esphome_auth_required` rather than hanging or guessing.

Protocol notes (Device Builder 1.14.9, re-verified against
`esphome_device_builder/api/legacy.py` and `docs/API.md` on GitHub
2026-09-27, same as `tools/esphome.py`'s prior `_dash_ws_spawn_async`/
`_dash_ws_command_async`, whose logic this module carries forward
unchanged beyond the error handling ADR-0004 requires):

- `/compile` and `/upload` are legacy WebSocket routes kept for HA
  Core's own back-compat. The client sends one
  `{"type": "spawn", "configuration": ..., ["port": ...]}` message; the
  server streams `{"event": "line", "data": <str>}` per output line and
  finishes with `{"event": "exit", "code": <int>}`.
- Validation goes through the newer multiplexed `/ws` command API: the
  server sends a `ServerInfoMessage` first, the client replies with one
  `{"command": "devices/validate", "message_id": ..., "args": {...}}`,
  and the server answers with either a streamed
  `{"message_id", "event": "output"/"result", "data": ...}` sequence, a
  single `{"message_id", "result": ...}`, or an
  `{"message_id", "error_code", "details"}` error.

Stable error codes (ADR-0004 D3) — every public method raises a
`DashboardError` subclass with one of these `.code` values, never a bare
exception:

| `.code`                    | Raised by                                                            |
|----------------------------|-----------------------------------------------------------------------|
| `esphome_not_configured`   | `DashboardLocator` — no `ESPHOME_DASHBOARD_URL` and not in add-on mode, or an invalid explicit URL |
| `esphome_unreachable`      | `DashboardLocator` — add-on not found/stopped/without ingress, or an invalid `ingress_port`; `DashboardClient` — TCP connect refused/failed |
| `esphome_auth_required`    | `ServerInfoMessage.requires_auth == true`, or HTTP 401/403 at the WebSocket handshake |
| `esphome_protocol_error`   | missing/invalid `ServerInfo`, a non-JSON or non-object frame, or the connection closing mid-session |
| `esphome_command_failed`   | the dashboard replied with an `error_code` (`dashboard_error_code` + `details` carry the original) |
| `timeout`                  | the single deadline (connect + handshake + operation) elapsed        |
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import websockets


# ── errors ──────────────────────────────────────────────────────────────────


class DashboardError(Exception):
    """Base for every typed error this module raises.

    `code` is the stable, machine-readable identifier from ADR-0004 D3 —
    callers match on `.code`, never on the message text. `extra` carries
    whatever structured context a subclass attached (e.g. `slug`,
    `dashboard_url`), for a caller that wants to build a richer
    `{"error": ...}` payload than the message alone.
    """

    code: str = "esphome_error"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


class NotConfiguredError(DashboardError):
    """No usable dashboard configuration exists (see module docstring)."""

    code = "esphome_not_configured"


class UnreachableError(DashboardError):
    """The dashboard's location is known but it can't be reached right now."""

    code = "esphome_unreachable"


class AuthRequiredError(DashboardError):
    """The dashboard requires a username/password this client cannot supply."""

    code = "esphome_auth_required"


class ProtocolError(DashboardError):
    """The dashboard's response didn't match the protocol this client speaks."""

    code = "esphome_protocol_error"


class CommandFailedError(DashboardError):
    """The dashboard understood the request and reported its own failure."""

    code = "esphome_command_failed"

    def __init__(self, message: str, *, dashboard_error_code: Any, details: Any = None) -> None:
        super().__init__(message, dashboard_error_code=dashboard_error_code, details=details)
        self.dashboard_error_code = dashboard_error_code
        self.details = details


class DashboardTimeoutError(DashboardError):
    """The single `timeout` deadline (connect + handshake + operation) elapsed.

    `log_tail`/`log_lines` mirror the shape callers already returned before
    ADR-0004 (a trailing slice of output lines and the total count seen so
    far) so a caller can still report partial progress. `job_may_still_be_running`
    is `True` for `compile`/`upload` — the dashboard's own firmware job queue
    does not cancel a job just because this client stopped waiting for it —
    and `False` for `validate`/`ping`, which do not enqueue anything.
    """

    code = "timeout"

    def __init__(
        self,
        message: str,
        *,
        timeout: float,
        log_tail: list[str] | None = None,
        log_lines: int = 0,
        job_may_still_be_running: bool = False,
    ) -> None:
        super().__init__(
            message,
            timeout=timeout,
            log_tail=log_tail or [],
            log_lines=log_lines,
            job_may_still_be_running=job_may_still_be_running,
        )
        self.timeout = timeout
        self.log_tail = log_tail or []
        self.log_lines = log_lines
        self.job_may_still_be_running = job_may_still_be_running


class _ConnectFailed(Exception):
    """Internal signal: connecting to the dashboard failed before anything was sent.

    Never escapes this module's public API — `DashboardClient._with_retry`
    catches it to decide whether a single retry (ADR-0004 D2) applies, then
    re-raises `.wrapped` (a real `DashboardError`) either way.
    """

    def __init__(self, wrapped: DashboardError) -> None:
        super().__init__(str(wrapped))
        self.wrapped = wrapped


# ── locator ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DashboardEndpoint:
    """Where the ESPHome Device Builder dashboard is, and how we found it."""

    base_url: str
    source: Literal["explicit", "supervisor_ingress"]
    slug: str | None
    ingress_port: int | None


class DashboardLocator:
    """Resolves the dashboard's `DashboardEndpoint` (ADR-0004 D2).

    All I/O is injected so this class performs none itself:

    - `env`: mapping to read `ESPHOME_DASHBOARD_URL`/`SUPERVISOR_TOKEN`
      from (normally `os.environ`, but never read directly here).
    - `supervisor_get`: `path -> dict`, called as
      `supervisor_get(f"/addons/{slug}/info")`; expected to return the
      Supervisor API's own envelope (a `"data"` key holding `state`,
      `ingress`, `ingress_port`), or raise on failure to reach Supervisor
      at all — either way turned into `esphome_unreachable` here.
    - `discover_slug`: `() -> str | None`, the installed ESPHome add-on's
      Supervisor slug, or `None` if it can't be found.

    Resolution in add-on mode is cached in memory (thread-safe) after the
    first successful `locate()`, since it costs two Supervisor calls;
    `invalidate()` clears that cache. Explicit mode (`ESPHOME_DASHBOARD_URL`
    set) is never cached and always re-validated — it's a single env-var
    read, and re-validating costs nothing.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        supervisor_get: Callable[[str], dict],
        discover_slug: Callable[[], str | None],
    ) -> None:
        self._env = env
        self._supervisor_get = supervisor_get
        self._discover_slug = discover_slug
        self._lock = threading.Lock()
        self._cached: DashboardEndpoint | None = None

    def locate(self) -> DashboardEndpoint:
        explicit = (self._env.get("ESPHOME_DASHBOARD_URL") or "").strip()
        if explicit:
            return self._explicit_endpoint(explicit)

        with self._lock:
            cached = self._cached
        if cached is not None:
            return cached

        endpoint = self._locate_supervisor_ingress()

        with self._lock:
            self._cached = endpoint
        return endpoint

    def invalidate(self) -> None:
        """Drop the cached `supervisor_ingress` endpoint, if any.

        A no-op in explicit mode (there is nothing cached to drop).
        """
        with self._lock:
            self._cached = None

    # -- internal --

    def _explicit_endpoint(self, url: str) -> DashboardEndpoint:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise NotConfiguredError(
                f"ESPHOME_DASHBOARD_URL is set but is not a valid http(s) URL: {url!r}",
                dashboard_url=url,
            )
        return DashboardEndpoint(base_url=url.rstrip("/"), source="explicit", slug=None, ingress_port=None)

    def _locate_supervisor_ingress(self) -> DashboardEndpoint:
        if not (self._env.get("SUPERVISOR_TOKEN") or "").strip():
            raise NotConfiguredError(
                "ESPHOME_DASHBOARD_URL is not set and this is not running as an HA "
                "add-on (SUPERVISOR_TOKEN unset) — nowhere to discover the ESPHome "
                "dashboard from."
            )

        try:
            slug = self._discover_slug()
        except Exception as e:
            raise UnreachableError(f"Could not discover the ESPHome add-on's slug: {e}") from e

        if not slug:
            raise UnreachableError("ESPHome add-on not found in the Supervisor add-on list.")

        try:
            response = self._supervisor_get(f"/addons/{slug}/info")
        except Exception as e:
            raise UnreachableError(
                f"Could not reach Supervisor to get add-on '{slug}' info: {e}", slug=slug,
            ) from e

        data = (response or {}).get("data") or {}

        state = data.get("state")
        if state != "started":
            raise UnreachableError(
                f"ESPHome add-on '{slug}' is not running (state={state!r}).",
                slug=slug, state=state,
            )

        if not data.get("ingress"):
            raise UnreachableError(
                f"ESPHome add-on '{slug}' does not have ingress enabled.", slug=slug,
            )

        port = data.get("ingress_port")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise UnreachableError(
                f"ESPHome add-on '{slug}' has an invalid ingress_port: {port!r}.",
                slug=slug, ingress_port=port,
            )

        return DashboardEndpoint(
            base_url=f"http://127.0.0.1:{port}",
            source="supervisor_ingress",
            slug=slug,
            ingress_port=port,
        )


# ── auth ────────────────────────────────────────────────────────────────────


class DashboardAuth(Protocol):
    """The one auth decision this client makes: refuse, or proceed.

    `NoAuth` is the only implementation today (ADR-0004 D3) — a future
    standalone-with-credentials mode would add a second implementation
    here, not grow this protocol's surface for arbitrary login flows.
    """

    def check_server_info(self, server_info: dict) -> None:
        """Raise `AuthRequiredError` if `server_info` says auth is required."""
        ...


class NoAuth:
    """Refuses any dashboard that reports `requires_auth: true`.

    There is no username/password plumbing in nexus for the ESPHome
    dashboard, so this fails fast with `esphome_auth_required` instead of
    hanging on a handshake the server will never complete, or guessing
    credentials.
    """

    def check_server_info(self, server_info: dict) -> None:
        if server_info.get("requires_auth"):
            raise AuthRequiredError(
                "ESPHome Device Builder requires authentication (username/password) "
                "— not supported by this client."
            )


# ── wire helpers ──────────────────────────────────────────────────────────────


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("https://", "wss://").replace("http://", "ws://") + path


def _handshake_status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status code from a `websockets` handshake failure.

    `InvalidStatus` (current) carries it on `.response.status_code`; older
    releases within the `requirements.txt` pin (`websockets>=12.0,<17`)
    used `InvalidStatusCode` with `.status_code` directly — checked via
    `getattr` so this works across that whole pinned range without an
    `isinstance` on a name that may not exist in every version.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        return getattr(response, "status_code", None)
    return getattr(exc, "status_code", None)


def _parse_json_object(raw: Any, *, what: str) -> dict:
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise ProtocolError(f"Non-JSON {what} frame from the ESPHome dashboard: {e}") from e
    if not isinstance(message, dict):
        raise ProtocolError(f"{what} frame from the ESPHome dashboard was not a JSON object.")
    return message


async def _bounded_recv(ws, deadline: float, what: str) -> Any:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError(what)
    try:
        return await asyncio.wait_for(ws.recv(), timeout=remaining)
    except websockets.exceptions.ConnectionClosed as e:
        raise ProtocolError(f"WebSocket connection closed while waiting for {what}: {e}") from e


_CLOSE_TIMEOUT = 5.0
"""Bound on the closing handshake `ws.close()` performs in every `finally`
below. `websockets.connect()` defaults this to 10s -- since closing is
cleanup after this module's own `timeout` deadline has already fired (or
the operation already succeeded), an unrelated multi-second wait for the
peer's close acknowledgement must not be allowed to silently extend a
call well past the caller's requested `timeout`."""


async def _ws_connect(base_url: str, path: str):
    """Connect to `<base_url><path>`, translating connect-time failures.

    Raised exceptions here all happen before anything is sent (ADR-0004
    D2's "before anything was sent" retry condition) — this is exactly the
    boundary `DashboardClient._with_retry` uses to decide whether a single
    retry applies.
    """
    url = _ws_url(base_url, path)
    try:
        return await websockets.connect(url, max_size=None, close_timeout=_CLOSE_TIMEOUT)
    except OSError as e:
        raise _ConnectFailed(
            UnreachableError(f"Cannot connect to ESPHome dashboard at {base_url}: {e}", base_url=base_url)
        ) from e
    except websockets.exceptions.InvalidHandshake as e:
        # Covers `InvalidStatus` (a non-101 HTTP response -- has a real
        # status code) and every other handshake failure in this family
        # (e.g. `InvalidMessage` for a malformed/absent HTTP response,
        # observed in practice on flaky links): only the former can be
        # `esphome_auth_required`, everything else is a shape the dashboard
        # should never send and is `esphome_protocol_error`.
        status = _handshake_status_code(e)
        if status in (401, 403):
            raise AuthRequiredError(
                f"ESPHome dashboard requires authentication (HTTP {status} at handshake)."
            ) from e
        raise ProtocolError(f"Unexpected failure at WebSocket handshake: {e}") from e


# ── ping ──────────────────────────────────────────────────────────────────────


def _ping_once(base_url: str, timeout: float) -> dict:
    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            response = client.get("/ping")
    except httpx.ConnectError as e:
        raise _ConnectFailed(
            UnreachableError(f"Cannot connect to ESPHome dashboard at {base_url}: {e}", base_url=base_url)
        ) from e
    except httpx.TimeoutException as e:
        raise DashboardTimeoutError(
            f"Timed out connecting to ESPHome dashboard at {base_url}.", timeout=timeout,
        ) from e

    if response.status_code in (401, 403):
        raise AuthRequiredError(f"ESPHome dashboard requires authentication (HTTP {response.status_code}).")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ProtocolError(f"Unexpected HTTP status from /ping: {e}") from e
    try:
        return response.json()
    except ValueError as e:
        raise ProtocolError(f"Non-JSON response from /ping: {e}") from e


# ── validate (multiplexed /ws command API) ────────────────────────────────────


async def _run_command_session(
    ws, auth: DashboardAuth, command: str, args: dict, timeout: float, tail_lines: int,
) -> dict:
    message_id = "1"
    lines: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout

    try:
        raw = await _bounded_recv(ws, deadline, "ServerInfo")
    except asyncio.TimeoutError as e:
        raise DashboardTimeoutError(
            "Timed out waiting for ServerInfo from the ESPHome dashboard.", timeout=timeout,
        ) from e
    server_info = _parse_json_object(raw, what="ServerInfo")
    auth.check_server_info(server_info)

    await ws.send(json.dumps({"command": command, "message_id": message_id, "args": args}))

    while True:
        try:
            raw = await _bounded_recv(ws, deadline, f"'{command}' result")
        except asyncio.TimeoutError as e:
            raise DashboardTimeoutError(
                f"Timed out waiting for the '{command}' result from the ESPHome dashboard.",
                timeout=timeout, log_tail=lines[-tail_lines:], log_lines=len(lines),
            ) from e
        message = _parse_json_object(raw, what="command response")
        if message.get("message_id") != message_id:
            continue
        if "error_code" in message:
            raise CommandFailedError(
                f"ESPHome dashboard command '{command}' failed: {message.get('error_code')}",
                dashboard_error_code=message.get("error_code"),
                details=message.get("details"),
            )
        if "result" in message:
            return {
                "success": True,
                "result": message["result"],
                "log_tail": lines[-tail_lines:],
                "log_lines": len(lines),
            }
        event = message.get("event")
        if event == "output":
            lines.append(message.get("data", ""))
        elif event == "result":
            data = message.get("data") or {}
            success = data.get("success")
            return {
                "success": bool(success) if success is not None else None,
                "exit_code": data.get("code"),
                "result": data,
                "log_tail": lines[-tail_lines:],
                "log_lines": len(lines),
            }


async def _validate_async(
    base_url: str, auth: DashboardAuth, configuration: str, timeout: float, tail_lines: int,
) -> dict:
    ws = await _ws_connect(base_url, "/ws")
    try:
        return await _run_command_session(
            ws, auth, "devices/validate", {"configuration": configuration}, timeout, tail_lines,
        )
    finally:
        await ws.close()


# ── compile / upload (legacy /compile, /upload spawn API) ─────────────────────


async def _run_spawn_session(ws, payload: dict, timeout: float, tail_lines: int) -> dict:
    lines: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout

    # Sending this is the point of no return for the "before anything was
    # sent" retry condition (ADR-0004 D2) — everything below here raises
    # `DashboardTimeoutError`/`ProtocolError`, never `_ConnectFailed`.
    await ws.send(json.dumps({"type": "spawn", **payload}))

    while True:
        try:
            raw = await _bounded_recv(ws, deadline, "job output")
        except asyncio.TimeoutError as e:
            raise DashboardTimeoutError(
                "Timed out waiting for the job to finish.",
                timeout=timeout, log_tail=lines[-tail_lines:], log_lines=len(lines),
                job_may_still_be_running=True,
            ) from e
        message = _parse_json_object(raw, what="job")
        event = message.get("event")
        if event == "line":
            lines.append(message.get("data", ""))
        elif event == "exit":
            code = message.get("code")
            return {
                "exit_code": code,
                "success": code == 0,
                "log_tail": lines[-tail_lines:],
                "log_lines": len(lines),
            }


async def _spawn_async(base_url: str, path: str, payload: dict, timeout: float, tail_lines: int) -> dict:
    ws = await _ws_connect(base_url, path)
    try:
        return await _run_spawn_session(ws, payload, timeout, tail_lines)
    finally:
        await ws.close()


# ── client ──────────────────────────────────────────────────────────────────


def _run_sync(coro):
    """Run `coro` to completion from sync code, from any calling context.

    Same pattern as `ha_client._ws_call`: `asyncio.run` directly when
    there's no running loop, else hand the coroutine to a fresh event loop
    on a worker thread (a sync tool function can itself be invoked from
    inside FastMCP's own event loop).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro).result()


class DashboardClient:
    """The closed set of operations nexus performs against the dashboard (S5).

    Every method resolves its endpoint via the injected `DashboardLocator`
    and, in `supervisor_ingress` mode only, retries exactly once if the
    *initial connection itself* fails and re-resolving the endpoint yields
    a different `ingress_port` (ADR-0004 D2) — a stale cached port after
    the add-on restarted is the one case a retry can plausibly fix. A
    connect failure in `explicit` mode never retries (nothing is cached to
    refresh), and a failure *after* a command has already been sent never
    retries either (this client does not know whether the dashboard's
    firmware job queue already started work).
    """

    def __init__(self, locator: DashboardLocator, auth: DashboardAuth | None = None) -> None:
        self._locator = locator
        self._auth = auth or NoAuth()

    def ping(self, *, timeout: float = 5.0) -> dict:
        """Check whether the dashboard responds, via `GET /ping`."""
        return self._with_retry(lambda ep: _ping_once(ep.base_url, timeout))

    def validate(self, configuration: str, *, timeout: float = 60.0, tail_lines: int = 200) -> dict:
        """Validate `configuration` (an ESPHome device YAML filename) without building it."""
        return self._with_retry(
            lambda ep: _run_sync(_validate_async(ep.base_url, self._auth, configuration, timeout, tail_lines))
        )

    def compile(self, configuration: str, *, timeout: float = 180.0, tail_lines: int = 200) -> dict:
        """Build firmware for `configuration`, blocking until the job exits."""
        return self._with_retry(
            lambda ep: _run_sync(
                _spawn_async(ep.base_url, "/compile", {"configuration": configuration}, timeout, tail_lines)
            )
        )

    def upload(self, configuration: str, port: str, *, timeout: float = 240.0, tail_lines: int = 200) -> dict:
        """Flash previously built firmware for `configuration` to `port` ('OTA' or a serial path)."""
        return self._with_retry(
            lambda ep: _run_sync(
                _spawn_async(
                    ep.base_url, "/upload", {"configuration": configuration, "port": port}, timeout, tail_lines,
                )
            )
        )

    # -- internal --

    def _with_retry(self, op: Callable[[DashboardEndpoint], dict]) -> dict:
        endpoint = self._locator.locate()
        try:
            return op(endpoint)
        except _ConnectFailed as failure:
            if endpoint.source != "supervisor_ingress":
                raise failure.wrapped from failure

            self._locator.invalidate()
            try:
                retried_endpoint = self._locator.locate()
            except DashboardError:
                raise failure.wrapped from failure

            if retried_endpoint.ingress_port == endpoint.ingress_port:
                raise failure.wrapped from failure

            try:
                return op(retried_endpoint)
            except _ConnectFailed as second_failure:
                raise second_failure.wrapped from second_failure
