import os
import asyncio
import json
import httpx
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from dotenv import load_dotenv

import service_guard

load_dotenv()

_HA_URL = os.getenv("HA_URL", "http://homeassistant.local:8123").rstrip("/")

def _load_ha_token() -> str:
    from auth import get_ha_token
    try:
        return get_ha_token()
    except RuntimeError:
        return ""

_HA_TOKEN = _load_ha_token()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_HA_TOKEN}",
        "Content-Type": "application/json",
    }


def _client() -> httpx.Client:
    return httpx.Client(base_url=_HA_URL, headers=_headers(), timeout=30)


def _ws_url() -> str:
    return _HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


# `websockets.connect(...)` defaults `close_timeout` to 10s. Both `async with
# websockets.connect(...) as ws:` blocks below run `ws.close()` on every
# exit — including a `TimeoutError`/`RuntimeError` raised by an already-
# bounded receive — so against a peer that never completes the closing
# handshake, the unbounded default silently adds ~10s on top of whatever
# timeout the caller already waited for (see `tools/websocket.py`'s
# `_WS_CLOSE_TIMEOUT` for the full analysis). Capping it here bounds that
# teardown cost instead.
_WS_CLOSE_TIMEOUT = 1.0

# `_ws_call_async` has no caller-facing `timeout` parameter (its `_ws_call`
# wrapper is used by ~30 `tools/*.py` modules purely as `(msg_type, **kwargs)`
# with `kwargs` forwarded verbatim as the WS command's payload fields — adding
# a `timeout` keyword risks colliding with a future HA command that happens to
# have a field of that name). `_WS_HANDSHAKE_TIMEOUT` is the reasonable
# constant used instead, bounding the *entire* call, matching the value
# already hardcoded for the post-auth response wait before this fix.
_WS_HANDSHAKE_TIMEOUT = 10.0


async def _ws_recv_within(ws, deadline: float, waiting_for: str) -> dict:
    """`ws.recv()` bounded by `deadline` (an `asyncio` loop-clock time).

    Raises `RuntimeError` naming `waiting_for` when the deadline is already
    passed or is reached before a message arrives, instead of blocking
    forever. Shared by `_ws_call_async` and `_ws_collect_events_async` so the
    `auth_required` -> `auth` -> `auth_ok` handshake in both honours the same
    single deadline as the rest of the call — mirrors
    `tools/websocket.py`'s `_ws_recv_within`, which fixed the same gap for
    `ws_*` MCP tools (ADR-0003 #7); this is `ha_client`'s own WS layer, used
    directly by ~30 `tools/*.py` modules via `_ws_call`/`_ws_collect_events`,
    so it needed the identical fix independently.
    """
    remaining = deadline - asyncio.get_event_loop().time()
    if remaining <= 0:
        raise RuntimeError(f"HA WebSocket handshake timed out waiting for {waiting_for}")
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
    except TimeoutError:
        raise RuntimeError(f"HA WebSocket handshake timed out waiting for {waiting_for}") from None
    return json.loads(raw)


async def _ws_call_async(msg_type: str, **kwargs) -> Any:
    import websockets
    token = _HA_TOKEN
    # max_size=None disables the 1 MB frame cap — HACS repository lists,
    # large registries and full traces routinely exceed that.
    async with websockets.connect(_ws_url(), max_size=None, close_timeout=_WS_CLOSE_TIMEOUT) as ws:
        deadline = asyncio.get_event_loop().time() + _WS_HANDSHAKE_TIMEOUT
        greeting = await _ws_recv_within(ws, deadline, "auth_required")
        if greeting.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WS greeting: {greeting}")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_ok = await _ws_recv_within(ws, deadline, "auth_ok")
        if auth_ok["type"] != "auth_ok":
            raise RuntimeError(f"WS auth failed: {auth_ok}")
        payload = {"id": 1, "type": msg_type, **kwargs}
        await ws.send(json.dumps(payload))
        while True:
            data = await _ws_recv_within(ws, deadline, "result")
            if data.get("id") == 1 and data.get("type") == "result":
                if not data.get("success"):
                    raise RuntimeError(f"WS error: {data.get('error')}")
                return data["result"]


def _ws_call(msg_type: str, **kwargs) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_ws_call_async(msg_type, **kwargs))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, _ws_call_async(msg_type, **kwargs)).result()


# --- Subscription-style WS commands ---
#
# Some HA commands answer with an empty `result` that only confirms the
# subscription, then stream the payload as `event` messages. `_ws_call` would
# return None for those, so they need to be collected until a terminal event.

async def _ws_collect_events_async(
    msg_type: str,
    is_last: Callable[[dict], bool],
    timeout: float = 15.0,
    **kwargs,
) -> list[dict]:
    import websockets
    events: list[dict] = []
    async with websockets.connect(_ws_url(), max_size=None, close_timeout=_WS_CLOSE_TIMEOUT) as ws:
        # `deadline` is set once, before the handshake, so `timeout` bounds
        # the *entire* call (handshake included) rather than only the event
        # loop below — a peer that never sends `auth_required`/`auth_ok`
        # would otherwise hang here forever regardless of `timeout`.
        deadline = asyncio.get_running_loop().time() + timeout
        greeting = await _ws_recv_within(ws, deadline, "auth_required")
        if greeting.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WS greeting: {greeting}")
        await ws.send(json.dumps({"type": "auth", "access_token": _HA_TOKEN}))
        auth_ok = await _ws_recv_within(ws, deadline, "auth_ok")
        if auth_ok["type"] != "auth_ok":
            raise RuntimeError(f"WS auth failed: {auth_ok}")
        await ws.send(json.dumps({"id": 1, "type": msg_type, **kwargs}))
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                # Partial data beats nothing — a slow platform must not sink the call.
                return events
            try:
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            except asyncio.TimeoutError:
                return events
            if data.get("id") != 1:
                continue
            if data.get("type") == "result" and not data.get("success", True):
                raise RuntimeError(f"WS error: {data.get('error')}")
            if data.get("type") != "event":
                continue
            event = data.get("event") or {}
            events.append(event)
            if is_last(event):
                return events


def _ws_collect_events(
    msg_type: str,
    is_last: Callable[[dict], bool],
    timeout: float = 15.0,
    **kwargs,
) -> list[dict]:
    def coro_factory():
        return _ws_collect_events_async(msg_type, is_last, timeout, **kwargs)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro_factory()).result()


# --- System health ---

def merge_system_health_events(events: list[dict]) -> dict:
    """Fold a `system_health/info` event stream into one {domain: {...}} dict.

    The stream is an `initial` snapshot in which slow values are placeholders
    (`{"type": "pending"}`), followed by one `update` per resolved value and a
    final `finish`. Pure function — the network lives in `collect_system_health`.
    """
    data: dict = {}
    for event in events:
        kind = event.get("type")
        if kind == "initial":
            data = event.get("data") or {}
        elif kind == "update":
            domain = data.setdefault(event.get("domain"), {})
            info = domain.setdefault("info", {})
            key = event.get("key")
            if event.get("success"):
                info[key] = event.get("data")
            else:
                info[key] = {"error": event.get("error")}
    return data


def collect_system_health(timeout: float = 15.0) -> dict:
    """Run the `system_health/info` subscription and return the merged result."""
    events = _ws_collect_events(
        "system_health/info",
        is_last=lambda e: e.get("type") == "finish",
        timeout=timeout,
    )
    return merge_system_health_events(events)


# --- Error log ---

def format_system_log_entries(entries: list[dict]) -> str:
    """Render `system_log/list` records as log-file-like text.

    Used when `/api/error_log` is unavailable — HA only registers that view when
    it logs to a file, which Supervisor installs disable by default.
    """
    lines: list[str] = []
    for entry in entries:
        ts = entry.get("timestamp")
        try:
            stamp = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            stamp = "?"
        messages = entry.get("message") or []
        if isinstance(messages, str):
            messages = [messages]
        source = entry.get("source") or []
        where = f"{source[0]}:{source[1]}" if len(source) >= 2 else ""
        count = entry.get("count") or 1
        repeats = f" (x{count})" if count > 1 else ""
        head = f"{stamp} {entry.get('level', '?')} [{entry.get('name', '?')}]"
        if where:
            head += f" {where}"
        lines.append(f"{head}{repeats} {' | '.join(str(m) for m in messages)}")
        if entry.get("exception"):
            lines.append(str(entry["exception"]).rstrip())
    return "\n".join(lines)


# --- States ---

def get_states() -> list[dict]:
    with _client() as c:
        r = c.get("/api/states")
        r.raise_for_status()
        return r.json()


def get_state(entity_id: str) -> dict:
    """Call `GET /api/states/<entity_id>`.

    Security review follow-up (2026-09-28, "same class as F3"): raises
    `ValueError` for an `entity_id` that fails HA Core's own
    `valid_entity_id` pattern (`service_guard.valid_entity_id`), before the
    URL is built -- otherwise a value like `"../services/hassio/
    addon_stop"` would resolve to a different endpoint than
    `/api/states/<entity_id>`, the same structural bug ADR-0006 F3 closed
    for `services_call_service`.
    """
    entity_id = service_guard.path_segment(entity_id, "entity_id")
    with _client() as c:
        r = c.get(f"/api/states/{entity_id}")
        r.raise_for_status()
        return r.json()


def set_state(entity_id: str, state: str, attributes: dict | None = None) -> dict:
    """Call `POST /api/states/<entity_id>`. Same `entity_id` validation as
    `get_state` (see its docstring), raising `ValueError` before any
    request is made."""
    entity_id = service_guard.path_segment(entity_id, "entity_id")
    payload: dict[str, Any] = {"state": state}
    if attributes:
        payload["attributes"] = attributes
    with _client() as c:
        r = c.post(f"/api/states/{entity_id}", json=payload)
        r.raise_for_status()
        return r.json()


# --- Services ---

def call_service(domain: str, service: str, data: dict | None = None) -> list[dict]:
    """Call `POST /api/services/<domain>/<service>`.

    ADR-0006 D3 (obrona w glab): raises `ValueError` for a `domain`/`service`
    that isn't a bare `[A-Za-z0-9_]+` identifier, before the URL is built --
    every call site in `tools/` already passes either a hardcoded literal or
    a name validated one layer up (`service_guard.validate_service_name` in
    `tools/services.py`/`tools/websocket.py`), so this only ever fires for a
    caller that skipped that check, not for the normal literal-name calls
    made throughout the rest of `tools/`.
    """
    if not (service_guard.validate_service_name(domain) and service_guard.validate_service_name(service)):
        raise ValueError(f"invalid service name: domain={domain!r} service={service!r}")
    with _client() as c:
        r = c.post(f"/api/services/{domain}/{service}", json=data or {})
        r.raise_for_status()
        return r.json()


def list_services() -> list[dict]:
    with _client() as c:
        r = c.get("/api/services")
        r.raise_for_status()
        return r.json()


# --- Events ---

def fire_event(event_type: str, data: dict | None = None) -> dict:
    """Call `POST /api/events/<event_type>`. `event_type` is percent-encoded
    via `service_guard.path_segment(event_type, "identifier")` first (Security
    review follow-up, 2026-09-28) -- unlike a service `domain`/`service`, a
    real HA event type has no single fixed character-class validator to
    reproduce, so it is encoded rather than whitelisted; raises `ValueError`
    for an empty/non-string `event_type` before any request is made.
    """
    event_type = service_guard.path_segment(event_type, "identifier")
    with _client() as c:
        r = c.post(f"/api/events/{event_type}", json=data or {})
        r.raise_for_status()
        return r.json()


# --- Config ---

def get_config() -> dict:
    with _client() as c:
        r = c.get("/api/config")
        r.raise_for_status()
        return r.json()


# --- Logbook & History ---

def get_history(entity_id: str | None = None, hours: int = 24) -> list:
    """Call `GET /api/history/period/<start>`. `start` is always computed
    here from `hours` (an `int`, never a caller-supplied path string) via
    `datetime.isoformat()`, then still routed through
    `service_guard.path_segment(start, "identifier")` before use (Security
    review follow-up, 2026-09-28) as defense in depth against a future
    refactor that accepts a raw `start` string directly.
    """
    from datetime import datetime, timedelta, timezone
    start = service_guard.path_segment(
        (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), "identifier"
    )
    url = f"/api/history/period/{start}"
    params = {}
    if entity_id:
        params["filter_entity_id"] = entity_id
    with _client() as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def get_logbook(entity_id: str | None = None, hours: int = 24) -> list:
    """Call `GET /api/logbook/<start>`. Same `start` handling as
    `get_history` (see its docstring)."""
    from datetime import datetime, timedelta, timezone
    start = service_guard.path_segment(
        (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), "identifier"
    )
    url = f"/api/logbook/{start}"
    params = {}
    if entity_id:
        params["entity_id"] = entity_id
    with _client() as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


# --- Templates ---

def render_template(template: str) -> str:
    with _client() as c:
        r = c.post("/api/template", json={"template": template})
        r.raise_for_status()
        return r.text


# --- Config entries (integrations — WebSocket only) ---

def get_config_entries(domain: str | None = None) -> list[dict]:
    kwargs = {}
    if domain:
        kwargs["domain"] = domain
    return _ws_call("config_entries/get", **kwargs)


# --- Entity registry (WebSocket only) ---

def get_entity_registry() -> list[dict]:
    return _ws_call("config/entity_registry/list")


def update_entity_registry(entity_id: str, **kwargs) -> dict:
    return _ws_call("config/entity_registry/update", entity_id=entity_id, **kwargs)


# --- Device registry (WebSocket only) ---

def get_device_registry() -> list[dict]:
    return _ws_call("config/device_registry/list")


# --- Area registry (WebSocket only) ---

def get_area_registry() -> list[dict]:
    return _ws_call("config/area_registry/list")


def create_area(name: str) -> dict:
    return _ws_call("config/area_registry/create", name=name)


def delete_area(area_id: str) -> dict:
    return _ws_call("config/area_registry/delete", area_id=area_id)


# --- Floor registry (WebSocket only) ---

def get_floor_registry() -> list[dict]:
    return _ws_call("config/floor_registry/list")


# --- Check API ---

def ping() -> bool:
    try:
        with _client() as c:
            r = c.get("/api/")
            return r.status_code == 200
    except Exception:
        return False


# --- Statistics ---

def get_statistics_metadata(statistic_ids: list[str] | None = None) -> list[dict]:
    payload = {}
    if statistic_ids:
        payload["statistic_ids"] = statistic_ids
    with _client() as c:
        r = c.post("/api/recorder/statistics_metadata", json=payload)
        r.raise_for_status()
        return r.json()


# --- Automation config CRUD (REST) ---

def get_automation_config(automation_id: str) -> dict | None:
    """Fetch a single automation YAML config as a dict. Returns None if not found.

    Security review follow-up (2026-09-28, "same class as F3"):
    `automation_id` is percent-encoded via `service_guard.path_segment(...,
    "identifier")` before use, raising `ValueError` for an empty/non-string
    id before any request is made.
    """
    automation_id = service_guard.path_segment(automation_id, "identifier")
    with _client() as c:
        r = c.get(f"/api/config/automation/config/{automation_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def set_automation_config(automation_id: str, config: dict) -> dict:
    """Create or overwrite an automation. `config` is the YAML-as-dict
    payload. Same `automation_id` handling as `get_automation_config`."""
    automation_id = service_guard.path_segment(automation_id, "identifier")
    with _client() as c:
        r = c.post(f"/api/config/automation/config/{automation_id}", json=config)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}


def delete_automation_config(automation_id: str) -> dict:
    """Delete an automation config by ID. Returns {'status': 'not_found'} if
    it does not exist. Same `automation_id` handling as
    `get_automation_config`."""
    automation_id = service_guard.path_segment(automation_id, "identifier")
    with _client() as c:
        r = c.delete(f"/api/config/automation/config/{automation_id}")
        if r.status_code == 404:
            return {"status": "not_found"}
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}


# --- Script config CRUD (REST) ---

def get_script_config(script_id: str) -> dict | None:
    """Fetch a single script YAML config as a dict. Returns None if not
    found. Same `service_guard.path_segment` treatment as
    `get_automation_config` (see its docstring), applied to `script_id`."""
    script_id = service_guard.path_segment(script_id, "identifier")
    with _client() as c:
        r = c.get(f"/api/config/script/config/{script_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def set_script_config(script_id: str, config: dict) -> dict:
    """Create or overwrite a script. `config` is the YAML-as-dict payload.
    Same `script_id` handling as `get_script_config`."""
    script_id = service_guard.path_segment(script_id, "identifier")
    with _client() as c:
        r = c.post(f"/api/config/script/config/{script_id}", json=config)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}


def delete_script_config(script_id: str) -> dict:
    """Delete a script config by ID. Returns {'status': 'not_found'} if it
    does not exist. Same `script_id` handling as `get_script_config`."""
    script_id = service_guard.path_segment(script_id, "identifier")
    with _client() as c:
        r = c.delete(f"/api/config/script/config/{script_id}")
        if r.status_code == 404:
            return {"status": "not_found"}
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}
