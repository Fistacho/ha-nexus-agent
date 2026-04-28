import asyncio
import json
import os
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("websocket")

_HA_URL = os.getenv("HA_URL", "http://homeassistant.local:8123").rstrip("/")


def _ws_url() -> str:
    return _HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


def _get_token() -> str:
    from auth import get_ha_token
    return get_ha_token()


async def _ws_send_recv(messages: list[dict], collect_events: int = 0, timeout: float = 10.0) -> list[dict]:
    """Open WebSocket to HA, authenticate, send messages, collect responses."""
    import websockets

    results = []
    ws_url = _ws_url()
    token = _get_token()

    async with websockets.connect(ws_url) as ws:
        # auth_required
        msg = json.loads(await ws.recv())
        assert msg["type"] == "auth_required"

        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_ok = json.loads(await ws.recv())
        if auth_ok["type"] != "auth_ok":
            raise RuntimeError(f"HA WebSocket auth failed: {auth_ok}")

        msg_id = 1
        for payload in messages:
            payload["id"] = msg_id
            await ws.send(json.dumps(payload))
            msg_id += 1

        deadline = asyncio.get_event_loop().time() + timeout
        collected = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                data = json.loads(raw)
                results.append(data)
                if data.get("type") == "event":
                    collected += 1
                    if collect_events > 0 and collected >= collect_events:
                        break
                if collect_events == 0 and data.get("type") == "result":
                    break
            except asyncio.TimeoutError:
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


@mcp.tool()
def get_states() -> list[dict]:
    """Get all entity states via WebSocket (faster than REST for large installs)."""
    results = _run(_ws_send_recv([{"type": "get_states"}]))
    for r in results:
        if r.get("type") == "result" and r.get("success"):
            return r["result"]
    raise RuntimeError("get_states failed")


@mcp.tool()
def call_service(domain: str, service: str, data: dict | None = None) -> dict:
    """Call a HA service via WebSocket."""
    payload = {
        "type": "call_service",
        "domain": domain,
        "service": service,
        "service_data": data or {},
        "return_response": True,
    }
    results = _run(_ws_send_recv([payload]))
    for r in results:
        if r.get("type") == "result":
            return r
    return {"success": False}


@mcp.tool()
def render_template(template: str) -> str:
    """Render a Jinja2 template via WebSocket."""
    payload = {"type": "render_template", "template": template}
    results = _run(_ws_send_recv([payload]))
    for r in results:
        if r.get("type") == "result" and r.get("success"):
            return r["result"]
    raise RuntimeError("Template render failed")


@mcp.tool()
def listen_state_changes(entity_id: str, count: int = 5, timeout: float = 30.0) -> list[dict]:
    """Listen for state change events for an entity. Returns up to `count` events within `timeout` seconds.
    Useful for watching a sensor, waiting for a motion trigger, etc.
    """
    payload = {
        "type": "subscribe_events",
        "event_type": "state_changed",
    }
    results = _run(_ws_send_recv([payload], collect_events=count, timeout=timeout))
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


@mcp.tool()
def listen_events(event_type: str, count: int = 10, timeout: float = 15.0) -> list[dict]:
    """Listen for any HA event type (e.g. 'zha_event', 'mobile_app_notification_action', 'call_service').
    Returns up to `count` events within `timeout` seconds.
    """
    payload = {"type": "subscribe_events", "event_type": event_type}
    results = _run(_ws_send_recv([payload], collect_events=count, timeout=timeout))
    return [
        r["event"]
        for r in results
        if r.get("type") == "event"
    ]


@mcp.tool()
def get_config() -> dict:
    """Get HA config via WebSocket."""
    results = _run(_ws_send_recv([{"type": "get_config"}]))
    for r in results:
        if r.get("type") == "result" and r.get("success"):
            return r["result"]
    raise RuntimeError("get_config failed")


@mcp.tool()
def subscribe_trigger(trigger: dict, timeout: float = 30.0) -> dict | None:
    """Wait for a HA trigger to fire. Returns the trigger context when it fires.
    Example trigger: {"platform": "state", "entity_id": "binary_sensor.motion", "to": "on"}
    """
    payload = {"type": "subscribe_trigger", "trigger": trigger}
    results = _run(_ws_send_recv([payload], collect_events=1, timeout=timeout))
    for r in results:
        if r.get("type") == "event":
            return r.get("event")
    return None
