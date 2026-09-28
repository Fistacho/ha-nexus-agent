from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Annotated

from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

import self_protection
import service_guard
from tools._contract import destructive, read

load_dotenv()

mcp = FastMCP("websocket")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_HA_URL = os.getenv("HA_URL", "http://homeassistant.local:8123").rstrip("/")

# `websockets.connect(...)` defaults `close_timeout` to 10s. `_ws_send_recv`'s
# `async with websockets.connect(...) as ws:` runs `ws.close()` on every exit
# — including the `RuntimeError` raised by `_ws_recv_within` once `timeout`
# elapses — so against a peer that never completes the closing handshake,
# the unbounded default adds a further ~10s on top of the caller's `timeout`,
# breaking every `ws_*` tool's documented "blocks for up to `timeout`
# seconds" (ADR-0003 #7). Capping it here bounds that teardown cost instead.
_WS_CLOSE_TIMEOUT = 1.0


def _ws_url() -> str:
    return _HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


def _get_token() -> str:
    from auth import get_ha_token
    return get_ha_token()


async def _ws_recv_within(ws, deadline: float, waiting_for: str) -> dict:
    """`ws.recv()` bounded by `deadline` (an `asyncio` loop-clock time).

    Raises `RuntimeError` naming `waiting_for` when the deadline is already
    passed or is reached before a message arrives, instead of blocking
    forever — this is what makes the `auth_required`/`auth_ok` handshake in
    `_ws_send_recv` honour the caller's `timeout` the same way the
    post-handshake response loop already does.
    """
    remaining = deadline - asyncio.get_event_loop().time()
    if remaining <= 0:
        raise RuntimeError(f"HA WebSocket handshake timed out waiting for {waiting_for}")
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
    except TimeoutError:
        raise RuntimeError(f"HA WebSocket handshake timed out waiting for {waiting_for}") from None
    return json.loads(raw)


async def _ws_send_recv(
    messages: list[dict],
    collect_events: int = 0,
    timeout: float = 10.0,
    event_filter: Callable[[dict], bool] | None = None,
) -> list[dict]:
    """Open WebSocket to HA, authenticate, send messages, collect responses.

    Every message received is appended to the returned list regardless of
    `event_filter`. `event_filter`, when given, only decides which `event`
    messages count toward `collect_events` — this lets a subscription like
    `state_changed` (which HA never filters server-side by entity) stop only
    once the events a caller actually cares about have arrived, instead of
    being displaced by unrelated traffic.

    The entire call, including the `auth_required` -> `auth` -> `auth_ok`
    handshake, is bounded by `timeout`: a HA instance that sends
    `auth_required` and then never answers (or never sends anything at all)
    raises `RuntimeError` once `timeout` elapses instead of blocking forever.
    """
    import websockets

    results = []
    ws_url = _ws_url()
    token = _get_token()

    deadline = asyncio.get_event_loop().time() + timeout

    async with websockets.connect(ws_url, close_timeout=_WS_CLOSE_TIMEOUT) as ws:
        # auth_required
        msg = await _ws_recv_within(ws, deadline, "auth_required")
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected HA WebSocket greeting: {msg}")

        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_ok = await _ws_recv_within(ws, deadline, "auth_ok")
        if auth_ok["type"] != "auth_ok":
            raise RuntimeError(f"HA WebSocket auth failed: {auth_ok}")

        msg_id = 1
        for payload in messages:
            payload["id"] = msg_id
            await ws.send(json.dumps(payload))
            msg_id += 1

        collected = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                data = json.loads(raw)
                results.append(data)
                if data.get("type") == "event" and (event_filter is None or event_filter(data)):
                    collected += 1
                    if collect_events > 0 and collected >= collect_events:
                        break
                if collect_events == 0 and data.get("type") == "result":
                    break
            except TimeoutError:
                break

    return results


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@mcp.tool(annotations=read("Get all entity states via WebSocket"))
def get_states(
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for the WebSocket handshake and the "
                "result message before raising an error; must be between 1 "
                "and 300."
            ),
        ),
    ] = 10.0,
) -> list[dict]:
    """Get all entity states over the WebSocket API in one call.

    Sends `get_states` and waits up to `timeout` seconds for the matching
    `result` message; this can be faster than the HTTP `/api/states`
    endpoint on large installs since it avoids per-entity HTTP overhead.

    Use when: fetching every entity's state on a large installation.
    Not for: a filtered or paginated state list — use
    `entities_list_entities`.
    Returns: the list of state dicts from HA's `result.result`.
    Errors: raises `RuntimeError` when the WebSocket handshake or the
    `result` message does not arrive within `timeout`, or when a `result`
    message arrives but is not successful.
    Limits: blocks for up to `timeout` seconds (default 10), including the
    initial handshake.
    """
    results = _run(_ws_send_recv([{"type": "get_states"}], timeout=timeout))
    for r in results:
        if r.get("type") == "result" and r.get("success"):
            return r["result"]
    raise RuntimeError("get_states failed")


@mcp.tool(annotations=destructive("Call a service and return its response", idempotent=False, open_world=True))
def call_service(
    domain: Annotated[str, Field(description="Service domain to call, e.g. 'weather' or 'calendar'.")],
    service: Annotated[
        str, Field(description="Service name within the domain, e.g. 'get_forecasts' or 'get_events'.")
    ],
    data: Annotated[
        dict | None,
        Field(
            description=(
                "Service data: fields plus targets (entity_id/area_id/device_id). "
                "Omit for a call that needs no data."
            )
        ),
    ] = None,
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for the WebSocket handshake and the "
                "result message before giving up; must be between 1 and 300."
            ),
        ),
    ] = 10.0,
) -> dict:
    """Call a Home Assistant action over WebSocket and return its response data.

    Sends `call_service` with `return_response=True` always set, so actions
    that support a response (e.g. `weather.get_forecasts`,
    `calendar.get_events`) return it; actions without one (`light.turn_on`,
    most control actions) fail with a validation error. `domain`/`service`
    must match `[A-Za-z0-9_]+` (ADR-0006 D3) or the call is refused first.
    Refuses `hassio.addon_stop`/`app_stop`/`addon_stdin`/`app_stdin` against
    nexus's own add-on (`self_protection.is_own_addon`); `addon_restart`/
    `app_restart` stay allowed. Outright refuses the eight services
    `services_call_service` gates behind `confirm` (ADR-0006 D5) — none
    support a response (F1).

    Use when: the action's response payload is needed, not just the changed
    states.
    Not for: a plain control action, or one of the eight guarded services —
    use `services_call_service` for both.
    Returns: raw WS result: `{success, result: {context, response}}` on
    success, or a failure dict from HA; `{"success": False}` if no `result`
    message arrives.
    Errors: raises `RuntimeError` on a handshake timeout; `{"error":
    "invalid_service_name"}` for a malformed domain/service; `{"error":
    "self_addon_hassio_service_blocked", "message": ...}` for a blocked
    `hassio.*` case; `{"error": "guarded_service_refused", "message": ...,
    "use": "services_call_service"}` for a guarded service.
    Limits: blocks for up to `timeout` seconds (default 10), including the
    initial handshake.
    """
    if not (service_guard.validate_service_name(domain) and service_guard.validate_service_name(service)):
        return {"error": "invalid_service_name"}
    blocked = self_protection.blocked_hassio_service_call(domain, service, data)
    if blocked is not None:
        return blocked
    if service_guard.is_guarded(domain, service):
        return {
            "error": "guarded_service_refused",
            "message": (
                f"'{domain}.{service}' is a guarded Home Assistant service (ADR-0006) that "
                "never returns a response payload. Use services_call_service instead, which "
                "accepts confirm=True for it."
            ),
            "use": "services_call_service",
        }
    payload = {
        "type": "call_service",
        "domain": domain,
        "service": service,
        "service_data": data or {},
        "return_response": True,
    }
    results = _run(_ws_send_recv([payload], timeout=timeout))
    for r in results:
        if r.get("type") == "result":
            return r
    return {"success": False}


@mcp.tool(annotations=read("Render a Jinja2 template via subscription"))
def render_template(
    template: Annotated[str, Field(description="Jinja2 template string to render, e.g. '{{ states(\"sensor.temp\") }}'.")],
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for the rendered value before "
                "raising an error; must be between 1 and 300."
            ),
        ),
    ] = 10.0,
) -> str:
    """Render a Jinja2 template through HA's WebSocket render_template subscription.

    Sends `render_template`; HA acknowledges the subscription with a null
    `result`, then streams the rendered value in a follow-up `event`. This
    waits up to `timeout` seconds for that event and returns its `result`.

    Use when: the WebSocket transport itself is specifically required.
    Not for: a one-shot render — use `services_render_template`, a plain
    HTTP call with no subscription semantics.
    Returns: the rendered template value from the `event`'s `result` field.
    Errors: raises `RuntimeError` when the template itself errors (e.g. an
    undefined variable), when the WebSocket handshake does not complete
    within `timeout`, or when no event arrives within `timeout`.
    Limits: blocks for up to `timeout` seconds (default 10), including the
    initial handshake.
    """
    payload = {"type": "render_template", "template": template}
    results = _run(_ws_send_recv([payload], collect_events=1, timeout=timeout))
    for r in results:
        if r.get("type") == "event":
            event = r.get("event", {})
            if "error" in event:
                raise RuntimeError(f"Template render failed: {event['error']}")
            return event.get("result")
    raise RuntimeError("Template render timed out waiting for a result event")


@mcp.tool(annotations=read("Listen for one entity's state changes"))
def listen_state_changes(
    entity_id: Annotated[
        str, Field(description="Full entity ID to watch for state changes, e.g. 'binary_sensor.motion'.")
    ],
    count: Annotated[
        int, Field(description="Maximum number of matching events to collect before returning early.")
    ] = 5,
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for `count` matching events before "
                "returning what arrived; must be between 1 and 300."
            ),
        ),
    ] = 30.0,
) -> list[dict]:
    """Passively wait for and collect state_changed events for one entity.

    Subscribes to HA's `state_changed` event type, which has no server-side
    per-entity filter, and counts only events matching `entity_id` toward
    `count`, so unrelated entities' events do not displace the target's.
    Returns early once `count` matching events arrive, or whatever arrived
    once `timeout` elapses.

    Use when: waiting for a specific entity to change state, e.g. to confirm
    an action took effect.
    Not for: any entity's events — use `ws_listen_events` with
    event_type='state_changed'; a one-shot trigger condition — use
    `ws_subscribe_trigger`.
    Returns: list of dicts with `entity_id`, `old_state`, `new_state`,
    `last_changed`.
    Errors: raises `RuntimeError` when the WebSocket handshake does not
    complete within `timeout`.
    Limits: blocks for up to `timeout` seconds, including the initial
    handshake; collects at most `count` events.
    """
    def _is_target_entity(data: dict) -> bool:
        return data.get("event", {}).get("data", {}).get("entity_id") == entity_id

    payload = {
        "type": "subscribe_events",
        "event_type": "state_changed",
    }
    results = _run(
        _ws_send_recv([payload], collect_events=count, timeout=timeout, event_filter=_is_target_entity)
    )
    events = []
    for r in results:
        if r.get("type") == "event":
            event_data = r.get("event", {}).get("data", {})
            if event_data.get("entity_id") == entity_id:
                events.append({
                    "entity_id": event_data["entity_id"],
                    "old_state": event_data.get("old_state", {}).get("state"),
                    "new_state": event_data.get("new_state", {}).get("state"),
                    "last_changed": event_data.get("new_state", {}).get("last_changed"),
                })
    return events


@mcp.tool(annotations=read("Listen for a HA event type"))
def listen_events(
    event_type: Annotated[str, Field(description="HA event type to subscribe to, e.g. 'zha_event' or 'call_service'.")],
    count: Annotated[
        int, Field(description="Maximum number of events to collect before returning early.")
    ] = 10,
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for `count` events before returning "
                "what arrived; must be between 1 and 300."
            ),
        ),
    ] = 15.0,
) -> list[dict]:
    """Passively wait for and collect events of one HA event type.

    Subscribes to `event_type` via `subscribe_events` and collects up to
    `count` events, returning early once that many arrive or whatever
    arrived once `timeout` elapses.

    Use when: observing any event type, e.g. integration or automation
    events not tied to a single entity's state.
    Not for: state changes on one specific entity — use
    `ws_listen_state_changes`.
    Returns: list of raw `event` payloads as HA sent them.
    Errors: raises `RuntimeError` when the WebSocket handshake does not
    complete within `timeout`.
    Limits: blocks for up to `timeout` seconds, including the initial
    handshake; collects at most `count` events.
    """
    payload = {"type": "subscribe_events", "event_type": event_type}
    results = _run(_ws_send_recv([payload], collect_events=count, timeout=timeout))
    return [
        r["event"]
        for r in results
        if r.get("type") == "event"
    ]


@mcp.tool(annotations=read("Get HA config via WebSocket"))
def get_config(
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for the WebSocket handshake and the "
                "result message before raising an error; must be between 1 "
                "and 300."
            ),
        ),
    ] = 10.0,
) -> dict:
    """Get Home Assistant's own configuration over WebSocket.

    Sends `get_config` and waits up to `timeout` seconds for the matching
    `result` message.

    Use when: reading HA's location, unit system, version and component
    list over the WebSocket transport.
    Not for: the same data over HTTP — use `history_get_ha_config`.
    Returns: HA's config dict from `result.result` (location, unit_system,
    version, components, ...).
    Errors: raises `RuntimeError` when the WebSocket handshake or the
    `result` message does not arrive within `timeout`, or when a `result`
    message arrives but is not successful.
    Limits: blocks for up to `timeout` seconds (default 10), including the
    initial handshake.
    """
    results = _run(_ws_send_recv([{"type": "get_config"}], timeout=timeout))
    for r in results:
        if r.get("type") == "result" and r.get("success"):
            return r["result"]
    raise RuntimeError("get_config failed")


@mcp.tool(annotations=read("Wait for a HA trigger to fire"))
def subscribe_trigger(
    trigger: Annotated[
        dict,
        Field(
            description=(
                "HA trigger definition dict, e.g. {'platform': 'state', "
                "'entity_id': 'binary_sensor.motion', 'to': 'on'}."
            )
        ),
    ],
    timeout: Annotated[
        float,
        Field(
            ge=1,
            le=300,
            description=(
                "Maximum seconds to wait for the trigger to fire before "
                "returning None; must be between 1 and 300."
            ),
        ),
    ] = 30.0,
) -> dict | None:
    """Passively wait for a Home Assistant trigger definition to fire once.

    Sends `subscribe_trigger` with the given `trigger` and waits up to
    `timeout` seconds for the matching `event`.

    Use when: waiting for a specific trigger condition (state, time, numeric
    state, ...) rather than polling.
    Not for: raw state_changed events on one entity — use
    `ws_listen_state_changes`.
    Returns: the trigger's `event` context dict when it fires, or `None` if
    `timeout` elapses first.
    Errors: raises `RuntimeError` when the WebSocket handshake does not
    complete within `timeout`.
    Limits: blocks for up to `timeout` seconds, including the initial
    handshake.
    """
    payload = {"type": "subscribe_trigger", "trigger": trigger}
    results = _run(_ws_send_recv([payload], collect_events=1, timeout=timeout))
    for r in results:
        if r.get("type") == "event":
            return r.get("event")
    return None
