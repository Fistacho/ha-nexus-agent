"""ESPHome device management tools.

Read configs from /config/esphome/, query HA device/entity registry,
and drive compile / validate / OTA upload via ESPHome Dashboard API.

Dashboard URL: set ESPHOME_DASHBOARD_URL env var (default: http://homeassistant.local:6052).
Inside HA add-on context the default works if ESPHome add-on is installed.
"""
from __future__ import annotations

import os
import httpx
from pathlib import Path
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("esphome")

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))
_ESPHOME_DIR = _CONFIG_PATH / "esphome"
_DASH_URL = os.getenv("ESPHOME_DASHBOARD_URL", "http://homeassistant.local:6052")
_ESPHOME_SLUGS = ["a0d7b954_esphome", "esphome_esphome"]


# ── internal helpers ──────────────────────────────────────────────────────────

def _sup_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return {"error": "SUPERVISOR_TOKEN not set — not running as HA add-on"}
    try:
        with httpx.Client(
            base_url="http://supervisor",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        ) as c:
            r = c.request(method, path, json=body)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:200]}
    except Exception as e:
        return {"error": str(e)}


def _sup_text(path: str, timeout: int = 30) -> str:
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return ""
    with httpx.Client(
        base_url="http://supervisor",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    ) as c:
        r = c.get(path)
        r.raise_for_status()
        return r.text


def _dash(method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict:
    try:
        with httpx.Client(base_url=_DASH_URL, timeout=timeout) as c:
            r = c.request(method, path, json=body)
            if "application/json" in r.headers.get("content-type", ""):
                return r.json()
            return {"status_code": r.status_code, "text": r.text[:2000]}
    except httpx.ConnectError:
        return {"error": f"Cannot connect to ESPHome dashboard at {_DASH_URL}. Set ESPHOME_DASHBOARD_URL if needed."}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:200]}
    except Exception as e:
        return {"error": str(e)}


def _yaml_names() -> list[str]:
    if not _ESPHOME_DIR.exists():
        return []
    return sorted(
        f.stem for f in _ESPHOME_DIR.glob("*.yaml")
        if not f.name.startswith(".") and f.name != "secrets.yaml"
    )


# ── tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_devices() -> dict:
    """List ESPHome devices: YAML configs on disk + HA device registry entries + online status."""
    configs = _yaml_names()

    # Connection status from HA entity states
    connected: dict[str, bool] = {}
    try:
        for s in ha.get_states():
            eid = s.get("entity_id", "")
            if "api_connection_status" in eid:
                slug = eid.replace("binary_sensor.", "").replace("_api_connection_status", "")
                connected[slug] = s.get("state") == "on"
    except Exception:
        pass

    # ESPHome devices from HA device registry
    ha_devices: list[dict] = []
    try:
        for d in ha.get_device_registry():
            ids = d.get("identifiers", [])
            is_esphome = any(
                isinstance(i, (list, tuple)) and len(i) >= 1 and str(i[0]) == "esphome"
                for i in ids
            )
            if not is_esphome:
                continue
            slug = next(
                (str(i[1]).replace(".local", "").replace("-", "_").lower()
                 for i in ids if isinstance(i, (list, tuple)) and len(i) >= 2 and str(i[0]) == "esphome"),
                None,
            )
            ha_devices.append({
                "name": d.get("name_by_user") or d.get("name"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "sw_version": d.get("sw_version"),
                "hw_version": d.get("hw_version"),
                "area_id": d.get("area_id"),
                "connected": connected.get(slug) if slug else None,
                "ha_device_id": d.get("id"),
            })
    except Exception as e:
        ha_devices = [{"error": str(e)}]

    return {
        "yaml_configs": configs,
        "ha_devices": ha_devices,
        "online": sum(1 for v in connected.values() if v),
        "offline": sum(1 for v in connected.values() if not v),
    }


@mcp.tool()
def get_config(name: str) -> dict:
    """Read ESPHome device YAML config from /config/esphome/. Pass name with or without .yaml."""
    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    path = _ESPHOME_DIR / filename
    if not path.exists():
        return {"error": f"Not found: {filename}", "esphome_dir": str(_ESPHOME_DIR)}
    return {"name": filename, "content": path.read_text(encoding="utf-8")}


@mcp.tool()
def get_device_entities(device_name: str) -> list[dict]:
    """Get all HA entities belonging to an ESPHome device (match by name/slug)."""
    try:
        entity_reg = ha.get_entity_registry()
        states = {s["entity_id"]: s for s in ha.get_states()}
    except Exception as e:
        return [{"error": str(e)}]

    slug = device_name.lower().replace("-", "_").replace(" ", "_")
    result = []
    for e in entity_reg:
        eid = e.get("entity_id", "")
        if e.get("platform") == "esphome" and slug in eid.lower():
            s = states.get(eid, {})
            result.append({
                "entity_id": eid,
                "name": e.get("name") or e.get("original_name"),
                "domain": eid.split(".")[0] if "." in eid else "",
                "state": s.get("state"),
                "attributes": s.get("attributes", {}),
                "disabled": e.get("disabled_by") is not None,
            })
    return result


@mcp.tool()
def compile_device(name: str) -> dict:
    """Compile ESPHome firmware for a device via Dashboard API. Blocks until done (~60-120s)."""
    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    return {"device": name, "action": "compile", **_dash("POST", "/compile", {"configuration": filename}, timeout=180)}


@mcp.tool()
def validate_config(name: str) -> dict:
    """Validate ESPHome YAML config via Dashboard API (no compile, fast)."""
    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    return {"device": name, "action": "validate", **_dash("POST", "/validate", {"configuration": filename})}


@mcp.tool()
def upload_device(name: str) -> dict:
    """OTA flash compiled firmware to an ESPHome device via Dashboard API.

    Requires device on the network and matching OTA password. Blocks until done (~30-60s).
    """
    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    return {"device": name, "action": "upload", **_dash("POST", "/upload", {"configuration": filename}, timeout=240)}


@mcp.tool()
def clean_mqtt(name: str) -> dict:
    """Remove stale MQTT discovery entries for an ESPHome device (MQTT mode only)."""
    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    return {"device": name, "action": "clean_mqtt", **_dash("POST", "/clean-mqtt", {"configuration": filename})}


@mcp.tool()
def get_addon_info() -> dict:
    """Get ESPHome add-on status, version, and update availability via Supervisor API."""
    for slug in _ESPHOME_SLUGS:
        result = _sup_json("GET", f"/addons/{slug}/info")
        if "error" not in result:
            data = result.get("data", {})
            return {
                "slug": slug,
                "name": data.get("name"),
                "state": data.get("state"),
                "version": data.get("version"),
                "version_latest": data.get("version_latest"),
                "update_available": data.get("update_available"),
                "ingress_url": data.get("ingress_url"),
            }
    return {"error": f"ESPHome add-on not found. Tried: {_ESPHOME_SLUGS}"}


@mcp.tool()
def get_addon_logs(lines: int = 150) -> dict:
    """Get ESPHome add-on log output (last N lines). Shows compile errors, OTA status, device connections."""
    for slug in _ESPHOME_SLUGS:
        try:
            text = _sup_text(f"/addons/{slug}/logs")
            if text:
                log_lines = text.splitlines()
                return {"slug": slug, "logs": "\n".join(log_lines[-lines:]), "total_lines": len(log_lines)}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            return {"error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"error": str(e)}
    return {"error": f"ESPHome add-on not found. Tried: {_ESPHOME_SLUGS}"}


@mcp.tool()
def ping_dashboard() -> dict:
    """Check ESPHome dashboard reachability at ESPHOME_DASHBOARD_URL."""
    result = _dash("GET", "/ping", timeout=5)
    return {"url": _DASH_URL, "reachable": "error" not in result, "result": result}
