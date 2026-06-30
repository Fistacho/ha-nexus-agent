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

    # ESPHome config entry IDs — reliable way to identify ESPHome devices
    # regardless of how identifiers are serialised in this HA version
    esphome_entry_ids: set[str] = set()
    try:
        for entry in ha._ws_call("config_entries/get", domain="esphome"):
            esphome_entry_ids.add(entry.get("entry_id", ""))
    except Exception:
        pass

    _ESPHOME_MANUFACTURERS = {"espressif", "esphome"}

    # ESPHome devices from HA device registry
    ha_devices: list[dict] = []
    try:
        for d in ha.get_device_registry():
            # Strategy 1: any config entry belongs to ESPHome integration
            by_entry = bool(esphome_entry_ids and
                set(d.get("config_entries", [])) & esphome_entry_ids)
            # Strategy 2: identifiers domain == "esphome"
            ids = d.get("identifiers", [])
            by_id = any(
                isinstance(i, (list, tuple)) and len(i) >= 1 and str(i[0]) == "esphome"
                for i in ids
            )
            # Strategy 3: manufacturer is Espressif / esphome (fallback)
            by_mfr = str(d.get("manufacturer") or "").lower() in _ESPHOME_MANUFACTURERS

            if not (by_entry or by_id or by_mfr):
                continue

            # Derive slug for connection-status lookup
            name = (d.get("name_by_user") or d.get("name") or "").lower()
            slug = name.replace(" ", "_").replace("-", "_")
            ha_devices.append({
                "name": d.get("name_by_user") or d.get("name"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "sw_version": d.get("sw_version"),
                "hw_version": d.get("hw_version"),
                "area_id": d.get("area_id"),
                "connected": connected.get(slug),
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
def write_config(name: str, content: str) -> dict:
    """Write ESPHome device YAML config to /config/esphome/. Validates YAML syntax before saving.

    Supports HA custom tags (!secret, !include) — they pass through validation unchanged.
    Creates the file if it doesn't exist.
    """
    import yaml

    class _ESPHomeLoader(yaml.SafeLoader):
        pass

    def _tag_passthrough(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        return None

    for _tag in ("!secret", "!include", "!lambda", "!extend", "!remove"):
        _ESPHomeLoader.add_constructor(
            _tag, lambda l, n, t=_tag: _tag_passthrough(l, t, n)
        )

    try:
        yaml.load(content, Loader=_ESPHomeLoader)
    except yaml.YAMLError as e:
        return {"success": False, "error": f"YAML validation failed: {e}"}

    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    path = _ESPHOME_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"success": True, "name": filename, "path": str(path), "bytes": len(content.encode())}


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


# ── LVGL tools ────────────────────────────────────────────────────────────────

def _lvgl_load(name: str) -> tuple[dict | None, str]:
    """Parse ESPHome YAML preserving !tag markers as NUL-encoded strings.

    Tags are encoded as '\\x00!tag\\x00value' so they survive the Python dict
    round-trip and can be restored to proper YAML tags on save.
    Returns (parsed_dict, error_str). On error parsed_dict is None.
    """
    import yaml

    class _L(yaml.SafeLoader):
        pass

    def _tag_ctor(loader, tag, node):
        val = loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else ""
        return f"\x00{tag}\x00{val}"

    for _t in ("!secret", "!include", "!lambda", "!extend", "!remove"):
        _L.add_constructor(_t, lambda l, n, t=_t: _tag_ctor(l, t, n))

    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    path = _ESPHOME_DIR / filename
    if not path.exists():
        return None, f"Not found: {filename}"
    try:
        parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=_L)
        return parsed, ""
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"


def _lvgl_save(name: str, config: dict) -> dict:
    """Dump ESPHome config dict back to YAML, restoring NUL-encoded !tag markers."""
    import yaml

    class _D(yaml.SafeDumper):
        pass

    def _str_repr(dumper, data):
        if isinstance(data, str) and data.startswith("\x00!"):
            parts = data.split("\x00", 2)
            tag = parts[1]
            val = parts[2] if len(parts) > 2 else ""
            style = "|" if "\n" in val else None
            return dumper.represent_scalar(tag, val, style=style)
        if isinstance(data, str) and "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _D.add_representer(str, _str_repr)

    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    path = _ESPHOME_DIR / filename
    try:
        content = yaml.dump(config, Dumper=_D, default_flow_style=False,
                            allow_unicode=True, sort_keys=False)
        path.write_text(content, encoding="utf-8")
        return {"success": True, "name": filename, "bytes": len(content.encode())}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _summarize_widget(w: dict) -> dict:
    """Return type + key properties of an LVGL widget dict."""
    if not isinstance(w, dict):
        return {}
    for wtype, props in w.items():
        if not isinstance(props, dict):
            continue
        summary: dict = {"type": wtype}
        for key in ("id", "x", "y", "width", "height", "text", "value", "styles"):
            if key in props:
                v = props[key]
                summary[key] = "<lambda>" if isinstance(v, str) and v.startswith("\x00!lambda") else v
        children = props.get("widgets", [])
        if children:
            summary["children"] = len(children)
        return summary
    return {}


@mcp.tool()
def lvgl_list_devices() -> list[dict]:
    """List ESPHome devices that have LVGL display support (lvgl: key present in config).

    Returns device names, page count, and page IDs for each LVGL-capable device.
    """
    result = []
    for name in _yaml_names():
        parsed, err = _lvgl_load(name)
        if err or not parsed:
            continue
        lvgl = parsed.get("lvgl")
        if not isinstance(lvgl, dict):
            continue
        pages = lvgl.get("pages", [])
        result.append({
            "device": name,
            "pages": len(pages),
            "page_ids": [p.get("id") for p in pages if isinstance(p, dict) and p.get("id")],
        })
    return result


@mcp.tool()
def lvgl_get_pages(device_name: str) -> dict:
    """Get LVGL pages configured on an ESPHome device with widget counts and background colors."""
    parsed, err = _lvgl_load(device_name)
    if err:
        return {"error": err}
    lvgl = parsed.get("lvgl") if parsed else None
    if not isinstance(lvgl, dict):
        return {"error": f"No lvgl: section in {device_name}"}

    displays = lvgl.get("displays", [])
    default_page = None
    if isinstance(displays, list) and displays and isinstance(displays[0], dict):
        default_page = displays[0].get("default_page")

    pages = []
    for p in lvgl.get("pages", []):
        if not isinstance(p, dict):
            continue
        widgets = p.get("widgets", [])
        pages.append({
            "id": p.get("id"),
            "bg_color": p.get("bg_color"),
            "widget_count": len(widgets) if isinstance(widgets, list) else 0,
        })
    return {"device": device_name, "default_page": default_page, "pages": pages}


@mcp.tool()
def lvgl_get_page_widgets(device_name: str, page_id: str) -> dict:
    """Get all LVGL widgets on a specific page of an ESPHome device.

    Returns widget types, IDs, positions, and key properties.
    Lambda callback values are shown as '<lambda>' (opaque — use esphome_get_config to see full source).
    """
    parsed, err = _lvgl_load(device_name)
    if err:
        return {"error": err}
    lvgl = parsed.get("lvgl") if parsed else None
    if not isinstance(lvgl, dict):
        return {"error": f"No lvgl: section in {device_name}"}

    for page in lvgl.get("pages", []):
        if isinstance(page, dict) and page.get("id") == page_id:
            widgets = page.get("widgets", [])
            return {
                "device": device_name,
                "page": page_id,
                "widget_count": len(widgets),
                "widgets": [_summarize_widget(w) for w in widgets],
            }
    return {"error": f"Page '{page_id}' not found in {device_name}"}


@mcp.tool()
def lvgl_get_styles(device_name: str) -> dict:
    """Get LVGL theme and style definitions from an ESPHome device config."""
    parsed, err = _lvgl_load(device_name)
    if err:
        return {"error": err}
    lvgl = parsed.get("lvgl") if parsed else None
    if not isinstance(lvgl, dict):
        return {"error": f"No lvgl: section in {device_name}"}
    return {
        "device": device_name,
        "theme": lvgl.get("theme"),
        "style_definitions": lvgl.get("style_definitions"),
        "gradients": lvgl.get("gradients"),
    }


@mcp.tool()
def lvgl_validate(device_name: str) -> dict:
    """Validate LVGL config client-side: unique widget/page IDs, valid page references.

    Runs without ESPHome Dashboard — useful as a pre-check before compiling.
    For full component/property validation use esphome_validate_config.
    """
    parsed, err = _lvgl_load(device_name)
    if err:
        return {"error": err, "valid": False}
    lvgl = parsed.get("lvgl") if parsed else None
    if not isinstance(lvgl, dict):
        return {"error": f"No lvgl: section in {device_name}", "valid": False}

    issues: list[str] = []
    page_ids: set[str] = set()
    widget_ids: set[str] = set()

    for page in lvgl.get("pages", []):
        if not isinstance(page, dict):
            continue
        pid = page.get("id")
        if pid:
            if pid in page_ids:
                issues.append(f"Duplicate page id: '{pid}'")
            page_ids.add(pid)
        for widget in page.get("widgets", []):
            if not isinstance(widget, dict):
                continue
            for _, props in widget.items():
                if isinstance(props, dict):
                    wid = props.get("id")
                    if wid:
                        if wid in widget_ids:
                            issues.append(f"Duplicate widget id: '{wid}' (page '{pid}')")
                        widget_ids.add(wid)

    for display in (lvgl.get("displays") or []):
        if isinstance(display, dict):
            dp = display.get("default_page")
            if dp and dp not in page_ids:
                issues.append(f"default_page '{dp}' not in pages: {sorted(page_ids)}")

    return {
        "device": device_name,
        "valid": len(issues) == 0,
        "pages": len(page_ids),
        "widgets": len(widget_ids),
        "issues": issues,
    }


@mcp.tool()
def lvgl_add_widget(device_name: str, page_id: str, widget_type: str, properties: dict) -> dict:
    """Add an LVGL widget to a page on an ESPHome device and save the config.

    widget_type: LVGL widget type — label, button, slider, arc, switch, spinbox, img, line, meter, etc.
    properties: dict of widget properties (id, x, y, width, height, text, value, styles, ...).

    Lambda callbacks (on_click, on_value_changed) are not supported here — add them manually
    via esphome_get_config / esphome_write_config. After adding, run esphome_validate_config
    then esphome_compile_device + esphome_upload_device to deploy.
    """
    parsed, err = _lvgl_load(device_name)
    if err:
        return {"error": err}
    lvgl = parsed.get("lvgl") if parsed else None
    if not isinstance(lvgl, dict):
        return {"error": f"No lvgl: section in {device_name}"}

    for page in lvgl.get("pages", []):
        if not isinstance(page, dict) or page.get("id") != page_id:
            continue
        if "widgets" not in page:
            page["widgets"] = []
        page["widgets"].append({widget_type: properties})
        return _lvgl_save(device_name, parsed) | {"added": widget_type, "to_page": page_id}

    return {"error": f"Page '{page_id}' not found in {device_name}"}


@mcp.tool()
def lvgl_delete_widget(device_name: str, page_id: str, widget_id: str) -> dict:
    """Delete an LVGL widget by id from a page on an ESPHome device and save the config.

    After deleting, run esphome_validate_config then esphome_compile_device + esphome_upload_device.
    """
    parsed, err = _lvgl_load(device_name)
    if err:
        return {"error": err}
    lvgl = parsed.get("lvgl") if parsed else None
    if not isinstance(lvgl, dict):
        return {"error": f"No lvgl: section in {device_name}"}

    for page in lvgl.get("pages", []):
        if not isinstance(page, dict) or page.get("id") != page_id:
            continue
        widgets = page.get("widgets", [])
        before = len(widgets)
        page["widgets"] = [
            w for w in widgets
            if not (isinstance(w, dict) and
                    any(isinstance(v, dict) and v.get("id") == widget_id
                        for v in w.values()))
        ]
        removed = before - len(page["widgets"])
        if removed == 0:
            return {"error": f"Widget id '{widget_id}' not found on page '{page_id}'"}
        return _lvgl_save(device_name, parsed) | {"deleted": widget_id, "from_page": page_id}

    return {"error": f"Page '{page_id}' not found in {device_name}"}
