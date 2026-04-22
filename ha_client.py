import os
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


# --- Config entries (integrations) ---

def get_config_entries() -> list[dict]:
    with _client() as c:
        r = c.get("/api/config/config_entries/entry")
        r.raise_for_status()
        return r.json()


# --- Entity registry ---

def get_entity_registry() -> list[dict]:
    with _client() as c:
        r = c.get("/api/config/entity_registry/list")
        r.raise_for_status()
        return r.json()


def update_entity_registry(entity_id: str, **kwargs) -> dict:
    with _client() as c:
        r = c.post(
            "/api/config/entity_registry/update",
            json={"entity_id": entity_id, **kwargs},
        )
        r.raise_for_status()
        return r.json()


# --- Device registry ---

def get_device_registry() -> list[dict]:
    with _client() as c:
        r = c.get("/api/config/device_registry/list")
        r.raise_for_status()
        return r.json()


# --- Area registry ---

def get_area_registry() -> list[dict]:
    with _client() as c:
        r = c.get("/api/config/area_registry/list")
        r.raise_for_status()
        return r.json()


def create_area(name: str) -> dict:
    with _client() as c:
        r = c.post("/api/config/area_registry/create", json={"name": name})
        r.raise_for_status()
        return r.json()


def delete_area(area_id: str) -> dict:
    with _client() as c:
        r = c.post("/api/config/area_registry/delete", json={"area_id": area_id})
        r.raise_for_status()
        return r.json()


# --- Floor registry ---

def get_floor_registry() -> list[dict]:
    with _client() as c:
        r = c.get("/api/config/floor_registry/list")
        r.raise_for_status()
        return r.json()


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
