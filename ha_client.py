import os
import asyncio
import json
import httpx
from typing import Any
from dotenv import load_dotenv

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


async def _ws_call_async(msg_type: str, **kwargs) -> Any:
    import websockets
    token = _HA_TOKEN
    async with websockets.connect(_ws_url()) as ws:
        greeting = json.loads(await ws.recv())
        assert greeting["type"] == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_ok = json.loads(await ws.recv())
        if auth_ok["type"] != "auth_ok":
            raise RuntimeError(f"WS auth failed: {auth_ok}")
        payload = {"id": 1, "type": msg_type, **kwargs}
        await ws.send(json.dumps(payload))
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
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


# --- States ---

def get_states() -> list[dict]:
    with _client() as c:
        r = c.get("/api/states")
        r.raise_for_status()
        return r.json()


def get_state(entity_id: str) -> dict:
    with _client() as c:
        r = c.get(f"/api/states/{entity_id}")
        r.raise_for_status()
        return r.json()


def set_state(entity_id: str, state: str, attributes: dict | None = None) -> dict:
    payload: dict[str, Any] = {"state": state}
    if attributes:
        payload["attributes"] = attributes
    with _client() as c:
        r = c.post(f"/api/states/{entity_id}", json=payload)
        r.raise_for_status()
        return r.json()


# --- Services ---

def call_service(domain: str, service: str, data: dict | None = None) -> list[dict]:
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
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    url = f"/api/history/period/{start}"
    params = {}
    if entity_id:
        params["filter_entity_id"] = entity_id
    with _client() as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def get_logbook(entity_id: str | None = None, hours: int = 24) -> list:
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
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


# --- File operations (via HA REST - requires config access) ---

def read_config_file(path: str) -> str:
    """path relative to /config, e.g. 'automations.yaml'"""
    with _client() as c:
        r = c.get(f"/api/config/core/check_config")
        # File read via SSH/direct. Here we use HA file editor add-on style.
        # Falls back to direct filesystem read if HA_CONFIG_PATH is set.
        raise NotImplementedError("Use tools/files.py for file operations")


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
    """Fetch a single automation YAML config as a dict. Returns None if not found."""
    with _client() as c:
        r = c.get(f"/api/config/automation/config/{automation_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def set_automation_config(automation_id: str, config: dict) -> dict:
    """Create or overwrite an automation. `config` is the YAML-as-dict payload."""
    with _client() as c:
        r = c.post(f"/api/config/automation/config/{automation_id}", json=config)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}


def delete_automation_config(automation_id: str) -> dict:
    """Delete an automation config by ID. Returns {'status': 'not_found'} if it does not exist."""
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
    """Fetch a single script YAML config as a dict. Returns None if not found."""
    with _client() as c:
        r = c.get(f"/api/config/script/config/{script_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def set_script_config(script_id: str, config: dict) -> dict:
    """Create or overwrite a script. `config` is the YAML-as-dict payload."""
    with _client() as c:
        r = c.post(f"/api/config/script/config/{script_id}", json=config)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}


def delete_script_config(script_id: str) -> dict:
    """Delete a script config by ID. Returns {'status': 'not_found'} if it does not exist."""
    with _client() as c:
        r = c.delete(f"/api/config/script/config/{script_id}")
        if r.status_code == 404:
            return {"status": "not_found"}
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}
