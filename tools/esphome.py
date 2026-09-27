"""ESPHome device management tools.

Read configs from /config/esphome/, query HA device/entity registry,
and drive compile / validate / OTA upload via the ESPHome dashboard API.

Dashboard URL: set ESPHOME_DASHBOARD_URL env var (default: http://homeassistant.local:6052).
Inside HA add-on context the default works if ESPHome add-on is installed.

Add-on identity & protocol (re-verified 2026-09-27 against the live add-on list and
GitHub, since the pip `esphome` package dropped its built-in dashboard for the
standalone "ESPHome Device Builder" between esphome 2026.5.0 and 2026.7.0):
- The add-on's Supervisor slug is NOT one of a fixed set of hashes — it is looked up
  dynamically from `GET /addons` (see `_discover_esphome_slug`) instead of guessing.
- `esphome/device-builder` (github.com/esphome/device-builder) is the new dashboard.
  Its `esphome_device_builder/api/legacy.py` keeps exactly six HA-compat routes:
  GET /devices, GET /ping, GET /json-config, POST /encryption-key, and two
  WebSocket routes — GET /compile and GET /upload — confirmed (raw file read
  2026-09-27) to use the *same* spawn wire protocol as the old dashboard
  (`{"type": "spawn", "configuration": ..., "port": ...}` in, `{"event": "line"/"exit"}`
  out), just re-routed through Device Builder's firmware job queue. There is no
  legacy `/validate` or `/clean-mqtt` WebSocket route anymore.
- Validate now goes through Device Builder's newer multiplexed `/ws` command API
  (docs/API.md: `devices/validate` command) — see `_dash_ws_command`.
- Clean-mqtt has no confirmed equivalent in Device Builder at all (searched
  docs/API.md for every mqtt/clean-related command; the closest, `firmware/clean`,
  is a build-artifact clean, not an MQTT discovery-topic clean) — see
  `clean_mqtt`'s docstring for the resulting open risk.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import httpx
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("esphome")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))
_ESPHOME_DIR = _CONFIG_PATH / "esphome"
_DASH_URL = os.getenv("ESPHOME_DASHBOARD_URL", "http://homeassistant.local:6052")

# Historical hard-coded guesses — kept only as a last-ditch fallback for
# _esphome_slug_candidates() when the live `/addons` listing itself is unreachable
# (e.g. SUPERVISOR_TOKEN unset). Real slug resolution is dynamic: see
# _discover_esphome_slug().
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


def _dash_ws_url(path: str) -> str:
    return _DASH_URL.replace("https://", "wss://").replace("http://", "ws://") + path


async def _dash_ws_spawn_async(path: str, payload: dict, timeout: float, tail_lines: int) -> dict:
    """Spawn a command on the ESPHome dashboard over its WebSocket API.

    Protocol (esphome/dashboard/web_server.py, `EsphomeCommandWebSocket`): the
    client sends one `{"type": "spawn", ...payload}` message; the server then
    streams `{"event": "line", "data": <str>}` per output line and finishes
    with `{"event": "exit", "code": <int>}`. There is no auth handshake (the
    dashboard's own cookie/basic auth applies at the HTTP upgrade, not here).
    """
    import websockets

    lines: list[str] = []
    try:
        async with websockets.connect(_dash_ws_url(path), max_size=None) as ws:
            await ws.send(json.dumps({"type": "spawn", **payload}))
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {
                        "error": "timeout",
                        "timeout": timeout,
                        "log_tail": lines[-tail_lines:],
                        "log_lines": len(lines),
                    }
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    return {
                        "error": "timeout",
                        "timeout": timeout,
                        "log_tail": lines[-tail_lines:],
                        "log_lines": len(lines),
                    }
                message = json.loads(raw)
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
    except OSError as e:
        return {"error": f"Cannot connect to ESPHome dashboard at {_DASH_URL}: {e}"}


def _dash_ws_spawn(path: str, payload: dict, timeout: float = 120, tail_lines: int = 200) -> dict:
    coro_factory = lambda: _dash_ws_spawn_async(path, payload, timeout, tail_lines)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro_factory()).result()


async def _dash_ws_command_async(command: str, args: dict, timeout: float, tail_lines: int) -> dict:
    """Run one command on the ESPHome Device Builder multiplexed WebSocket API (`/ws`).

    Protocol (esphome/device-builder, docs/API.md, confirmed on GitHub 2026-09-27):
    on connect the server sends a `ServerInfoMessage` first (checked here for
    `requires_auth` — if the dashboard has a username/password set, this bails out
    immediately with a clear error rather than hanging or guessing credentials, since
    no ESPHOME_USERNAME/ESPHOME_PASSWORD plumbing exists yet). The client then sends
    one `{"command": ..., "message_id": ..., "args": ...}`; the server replies either
    a single `{"message_id", "result"}`, an `{"message_id", "error_code", "details"}`,
    or streams `{"message_id", "event": "output", "data": <str>}` lines followed by a
    terminal `{"message_id", "event": "result", "data": {...}}`.
    """
    import websockets

    message_id = "1"
    lines: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout

    async def _recv(ws):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(ws.recv(), timeout=remaining)

    try:
        async with websockets.connect(_dash_ws_url("/ws"), max_size=None) as ws:
            try:
                raw = await _recv(ws)
            except asyncio.TimeoutError:
                return {"error": "timeout", "timeout": timeout, "log_tail": [], "log_lines": 0}
            server_info = json.loads(raw)
            if server_info.get("requires_auth"):
                return {
                    "error": "Dashboard requires authentication (ESPHOME_USERNAME/"
                             "ESPHOME_PASSWORD) — not supported by this tool.",
                }

            await ws.send(json.dumps({"command": command, "message_id": message_id, "args": args}))

            while True:
                try:
                    raw = await _recv(ws)
                except asyncio.TimeoutError:
                    return {
                        "error": "timeout",
                        "timeout": timeout,
                        "log_tail": lines[-tail_lines:],
                        "log_lines": len(lines),
                    }
                message = json.loads(raw)
                if message.get("message_id") != message_id:
                    continue
                if "error_code" in message:
                    return {
                        "error": message.get("error_code"),
                        "details": message.get("details"),
                        "log_tail": lines[-tail_lines:],
                        "log_lines": len(lines),
                    }
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
    except OSError as e:
        return {"error": f"Cannot connect to ESPHome dashboard at {_DASH_URL}: {e}"}


def _dash_ws_command(command: str, args: dict, timeout: float = 60, tail_lines: int = 200) -> dict:
    coro_factory = lambda: _dash_ws_command_async(command, args, timeout, tail_lines)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro_factory()).result()


def _looks_like_esphome_slug(slug: str) -> bool:
    """True if `slug` matches a known ESPHome add-on naming pattern.

    Add-on slugs are `<repo_hash>_<addon_slug>` — the hash prefix comes from the
    add-on store repository and differs per installation/fork (the official add-on,
    a HACS-style add-on repo, or a community fork such as the historical "ESPHome
    Device Builder" add-on all mint their own hash). Match on suffix/known names
    instead of hard-coding hashes.
    """
    return slug in _ESPHOME_SLUGS or slug == "esphome" or slug.endswith("_esphome")


def _discover_esphome_slug() -> str | None:
    """Find the installed ESPHome add-on's Supervisor slug from the live add-on list.

    Returns None when the Supervisor `/addons` listing itself is unavailable (e.g.
    SUPERVISOR_TOKEN unset) or no add-on matches — callers fall back to
    `_ESPHOME_SLUGS` in that case via `_esphome_slug_candidates()`.
    When several installed add-ons match (e.g. leftover uninstalled-but-cached
    entries, or both an old and new ESPHome add-on side by side), a started one is
    preferred over a stopped one; ties are broken by slug for a stable, repeatable
    result across calls.
    """
    result = _sup_json("GET", "/addons")
    if "error" in result:
        return None
    addons = ((result.get("data") or {}).get("addons")) or []
    matches = [
        a for a in addons
        if isinstance(a, dict) and _looks_like_esphome_slug(a.get("slug") or "")
    ]
    if not matches:
        return None
    matches.sort(key=lambda a: (a.get("state") != "started", a.get("slug") or ""))
    return matches[0].get("slug")


def _esphome_slug_candidates() -> list[str]:
    """Slugs to try, in order, for a Supervisor call against the ESPHome add-on.

    Prefers the dynamically discovered slug (see `_discover_esphome_slug`); falls
    back to the historical hard-coded guesses only when discovery itself couldn't
    reach `/addons` at all (in which case a direct `/addons/<slug>/...` call would
    fail identically for any slug, so this changes nothing in that scenario — it
    only helps if `/addons` is reachable but, for some reason, doesn't include a
    match, e.g. a permissions-scoped Supervisor token).
    """
    discovered = _discover_esphome_slug()
    if discovered:
        return [discovered]
    return list(_ESPHOME_SLUGS)


def _safe_filename(name: str) -> str:
    """Validate a user-supplied ESPHome device name and return its `<name>.yaml` filename.

    Rejects path separators, a leading dot, and blank input (same policy as
    `tools/themes.py::_theme_path`) — this is the boundary that stops
    `write_config(name="../../../etc/cron.d/x", ...)`-style path traversal, applied
    to every device-name parameter in this module (get/write_config, compile/
    validate/upload/clean_mqtt, the LVGL editor tools) before it reaches disk or is
    forwarded to the dashboard as a `configuration` argument.
    """
    if "/" in name or "\\" in name or name.startswith(".") or not name.strip():
        raise ValueError(f"Invalid device name: {name!r}")
    return name if name.endswith(".yaml") else f"{name}.yaml"


def _device_path(name: str) -> Path:
    """Resolve a validated device name to its absolute path under `_ESPHOME_DIR`.

    Defense in depth beyond `_safe_filename`'s separator/dot checks: the resolved
    path must still land inside `_ESPHOME_DIR` after `Path.resolve()` (symlinks,
    a platform-specific separator `_safe_filename` didn't account for, etc.).
    Raises ValueError — callers turn that into `{"error": ...}`.
    """
    filename = _safe_filename(name)
    esphome_root = _ESPHOME_DIR.resolve()
    path = (_ESPHOME_DIR / filename).resolve()
    if not path.is_relative_to(esphome_root):
        raise ValueError(f"Invalid device name: {name!r}")
    return path


def _yaml_names() -> list[str]:
    if not _ESPHOME_DIR.exists():
        return []
    return sorted(
        f.stem for f in _ESPHOME_DIR.glob("*.yaml")
        if not f.name.startswith(".") and f.name != "secrets.yaml"
    )


# ── tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(annotations=read("List ESPHome devices"))
def list_devices() -> dict:
    """List ESPHome devices from YAML configs, the HA device registry and online status.

    Combines `<name>.yaml` files under `/config/esphome/` with matching HA
    device-registry entries (identified via ESPHome config entries,
    `identifiers` domain, or manufacturer as a fallback) and each device's
    `binary_sensor.*_api_connection_status` state.

    Use when: getting an overview of every ESPHome device, whether or not it
    currently has a matching HA device.
    Not for: one device's entities — use `esphome_get_device_entities`.
    Returns: `{"yaml_configs": [...], "ha_devices": [...], "online": <int>,
    "offline": <int>}`.
    Errors: `ha_devices` becomes `[{"error": str(e)}]` if reading the HA
    device registry raises; connection-status lookups are silently skipped
    on error instead of failing the whole call.
    """
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


@mcp.tool(annotations=read("Get ESPHome device YAML config"))
def get_config(
    name: Annotated[
        str,
        Field(
            description=(
                "Device name, with or without the '.yaml' suffix, e.g. "
                "'kitchen_sensor'. Discover names with "
                "`esphome_list_devices`."
            )
        ),
    ],
) -> dict:
    """Read one ESPHome device's YAML config from `/config/esphome/`.

    Resolves `name` to `<name>.yaml` under `_ESPHOME_DIR`, rejecting path
    separators/leading dots/paths that escape that directory.

    Use when: inspecting a device's current configuration before editing it
    with `esphome_write_config` or one of the `esphome_lvgl_*` editors.
    Not for: a device's HA entities — use `esphome_get_device_entities`.
    Returns: `{"name": <filename>, "content": <yaml text>}`.
    Errors: `{"error": "Invalid device name: ..."}` for a path-escaping
    `name`; `{"error": "Not found: ...", "esphome_dir": ...}` when the file
    doesn't exist.
    """
    try:
        path = _device_path(name)
    except ValueError as e:
        return {"error": str(e)}
    if not path.exists():
        return {"error": f"Not found: {path.name}", "esphome_dir": str(_ESPHOME_DIR)}
    return {"name": path.name, "content": path.read_text(encoding="utf-8")}


@mcp.tool(annotations=destructive("Write ESPHome device YAML config", idempotent=True))
def write_config(
    name: Annotated[
        str,
        Field(
            description=(
                "Device name, with or without the '.yaml' suffix, e.g. "
                "'kitchen_sensor'. If the file already exists, its entire "
                "content is replaced."
            )
        ),
    ],
    content: Annotated[
        str,
        Field(description="Full YAML document to write, replacing the file's current content."),
    ],
) -> dict:
    """Write a full ESPHome device YAML config to `/config/esphome/`, overwriting any existing file.

    Validates YAML syntax first (HA custom tags `!secret`/`!include`/
    `!lambda`/`!extend`/`!remove` pass through unchanged) and only writes if
    that succeeds. This replaces the whole file — there is no merge with
    the previous content and, unlike `git_safe_write_with_checkpoint`, no
    checkpoint commit is made first, so the previous content is not
    recoverable through this tool.

    Use when: saving a complete device config you already have in full,
    e.g. after fetching and editing it with `esphome_get_config`.
    Not for: a git-checkpointed config write — use
    `git_safe_write_with_checkpoint`; for a generic `/config` file write use
    `files_write_config_file`; for a single LVGL widget edit use
    `esphome_lvgl_add_widget`/`esphome_lvgl_delete_widget` instead of
    hand-editing the whole YAML text.
    Returns: `{"success": True, "name": ..., "path": ..., "bytes": ...}` on
    success.
    Errors: `{"success": False, "error": "YAML validation failed: ..."}` for
    invalid YAML; `{"success": False, "error": "Invalid device name: ..."}`
    for a path-escaping `name`.
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

    try:
        path = _device_path(name)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"success": True, "name": path.name, "path": str(path), "bytes": len(content.encode())}


@mcp.tool(annotations=read("Get an ESPHome device's HA entities"))
def get_device_entities(
    device_name: Annotated[
        str,
        Field(
            description=(
                "Device name as used in HA, e.g. 'Kitchen Sensor' or "
                "'kitchen_sensor' (matched case-insensitively against a "
                "slugified form)."
            )
        ),
    ],
) -> list[dict]:
    """Get every HA entity belonging to one ESPHome device.

    Slugifies `device_name` (lowercase, spaces/dashes to underscores) and
    matches it against entity registry entries whose `platform` is
    "esphome" and whose `entity_id` contains that slug.

    Use when: inspecting a specific device's exposed entities and their
    current state.
    Not for: the device list itself — use `esphome_list_devices`.
    Returns: list of `{"entity_id", "name", "domain", "state",
    "attributes", "disabled"}` dicts.
    Errors: `[{"error": str(e)}]` when reading the entity registry or
    states raises.
    """
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


@mcp.tool(annotations=write("Compile ESPHome firmware", idempotent=True, open_world=True))
def compile_device(
    name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_sensor'."),
    ],
    only_generate: Annotated[
        bool,
        Field(
            description=(
                "If true, only generate the C++ source and skip the "
                "platform build. Set false for a full firmware build."
            )
        ),
    ] = False,
    timeout: Annotated[
        float,
        Field(description="Maximum seconds to wait for the compile process to finish, e.g. 180."),
    ] = 180,
    log_lines: Annotated[
        int,
        Field(description="Number of trailing build-log lines to return, e.g. 200."),
    ] = 200,
) -> dict:
    """Compile ESPHome firmware for a device via the dashboard WebSocket API (`/compile`).

    Blocks until the compile process exits (~60-180s). Verified compatible
    with the "ESPHome Device Builder" add-on (github.com/esphome/
    device-builder, read on GitHub 2026-09-27): `/compile` is one of the
    two routes it keeps for HA back-compat, with the same spawn wire
    protocol as the old dashboard, now backed by its firmware job queue. On
    a first build for a given platform, the PlatformIO toolchain fetches
    build packages from the internet (registry.platformio.org) into its
    own cache before compiling.

    Use when: building firmware for a device before `esphome_upload_device`.
    Not for: checking config validity without a full build — use
    `esphome_validate_config`, which is faster and does not fetch packages.
    Returns: `{"device": ..., "action": "compile", "exit_code": <int>,
    "success": <bool>, "log_tail": [...], "log_lines": <int>}`.
    Errors: `{"device": ..., "action": "compile", "error": "Invalid device
    name: ..."}` for a path-escaping `name`; `{"error": "timeout", ...}`
    when `timeout` elapses; `{"error": "Cannot connect to ESPHome dashboard
    at ...: ..."}` when unreachable.
    Limits: blocks for up to `timeout` seconds; `log_lines` truncates the
    returned log.
    """
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "compile", "error": str(e)}
    payload = {"configuration": filename}
    if only_generate:
        payload["only_generate"] = True
    result = _dash_ws_spawn("/compile", payload, timeout=timeout, tail_lines=log_lines)
    return {"device": name, "action": "compile", **result}


@mcp.tool(annotations=read("Validate ESPHome device config"))
def validate_config(
    name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_sensor'."),
    ],
    timeout: Annotated[
        float,
        Field(description="Maximum seconds to wait for the validation result, e.g. 60."),
    ] = 60,
    log_lines: Annotated[
        int,
        Field(description="Number of trailing log lines to return, e.g. 200."),
    ] = 200,
) -> dict:
    """Validate an ESPHome device's YAML config without compiling it.

    Uses the `devices/validate` command on Device Builder's newer
    multiplexed `/ws` protocol (github.com/esphome/device-builder,
    docs/API.md, confirmed 2026-09-27) — the legacy `/validate` WebSocket
    route from the old ESPHome dashboard no longer exists in Device
    Builder's `api/legacy.py`. Unlike `esphome_compile_device`, this never
    builds firmware and never fetches PlatformIO packages. If the
    dashboard needs a username/password, this fails fast instead of
    hanging (no credential wiring exists yet).

    Use when: checking a config's validity quickly, e.g. before
    `esphome_compile_device`.
    Not for: a full firmware build — use `esphome_compile_device`; not for
    LVGL-only structural checks — use `esphome_lvgl_validate`, which runs
    without the dashboard.
    Returns: `{"device": ..., "action": "validate", "success": <bool>,
    "result": ..., "log_tail": [...], "log_lines": <int>}`.
    Errors: `{"device": ..., "action": "validate", "error": "Invalid device
    name: ..."}` for a path-escaping `name`; `{"error": "timeout", ...}`
    when `timeout` elapses; `{"error": "Dashboard requires
    authentication..."}` when unsupported credentials are needed;
    `{"error": "Cannot connect to ESPHome dashboard at ...: ..."}` when
    unreachable. Not yet exercised against a live add-on — treat a failure
    as a signal to check `esphome_get_addon_logs`/`esphome_ping_dashboard`
    first.
    """
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "validate", "error": str(e)}
    result = _dash_ws_command("devices/validate", {"configuration": filename}, timeout=timeout, tail_lines=log_lines)
    return {"device": name, "action": "validate", **result}


@mcp.tool(annotations=destructive("Flash firmware to an ESPHome device", idempotent=False))
def upload_device(
    name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_sensor'."),
    ],
    port: Annotated[
        str,
        Field(
            description=(
                "'OTA' for wireless flash (default) or a serial device "
                "path, e.g. '/dev/ttyUSB0', for a wired flash."
            )
        ),
    ] = "OTA",
    timeout: Annotated[
        float,
        Field(description="Maximum seconds to wait for the upload to finish, e.g. 240."),
    ] = 240,
    log_lines: Annotated[
        int,
        Field(description="Number of trailing upload-log lines to return, e.g. 200."),
    ] = 200,
) -> dict:
    """Flash previously compiled firmware to an ESPHome device via the dashboard WebSocket API (`/upload`).

    Requires the device to already be built (`esphome_compile_device`) and,
    for OTA, reachable on the network with a matching OTA password. Blocks
    until done (~30-240s) and overwrites the device's running firmware —
    there is no automatic rollback if the new firmware is broken. Verified
    compatible with the "ESPHome Device Builder" add-on (github.com/esphome/
    device-builder, `api/legacy.py` read on GitHub 2026-09-27): `/upload` is
    the other WebSocket route it keeps for HA back-compat, with the same
    spawn wire protocol as `esphome_compile_device`.

    Use when: deploying a compiled build to the physical device.
    Not for: building the firmware itself — use `esphome_compile_device`
    first.
    Returns: `{"device": ..., "action": "upload", "exit_code": <int>,
    "success": <bool>, "log_tail": [...], "log_lines": <int>}` on
    completion.
    Errors: `{"device": ..., "action": "upload", "error": "Invalid device
    name: ..."}` for a path-escaping `name`; `{"error": "timeout", ...}`
    when `timeout` elapses; `{"error": "Cannot connect to ESPHome dashboard
    at ...: ..."}` when unreachable.
    Limits: blocks for up to `timeout` seconds; overwrites the device's
    firmware with no built-in rollback if the upload succeeds but the new
    firmware is broken.
    """
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "upload", "error": str(e)}
    payload = {"configuration": filename, "port": port}
    result = _dash_ws_spawn("/upload", payload, timeout=timeout, tail_lines=log_lines)
    return {"device": name, "action": "upload", **result}


@mcp.tool(annotations=destructive("Clean stale MQTT discovery entries", idempotent=True))
def clean_mqtt(
    name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_sensor'."),
    ],
    timeout: Annotated[
        float,
        Field(description="Maximum seconds to wait for the operation to finish, e.g. 60."),
    ] = 60,
    log_lines: Annotated[
        int,
        Field(description="Number of trailing log lines to return, e.g. 200."),
    ] = 200,
) -> dict:
    """Remove stale MQTT discovery entries for an ESPHome device (MQTT mode only).

    WARNING: unverified / likely broken against the currently installed
    add-on. This calls the old dashboard's `/clean-mqtt` WebSocket spawn
    endpoint, which does not exist in Device Builder's `api/legacy.py`
    (github.com/esphome/device-builder, read on GitHub 2026-09-27 — it
    keeps only six HA-compat routes, none of them `/clean-mqtt`). Its
    docs/API.md `/ws` command list has no equivalent either — the closest,
    `firmware/clean`, clears build artifacts, not MQTT topics. Verified
    compatible only with the legacy ESPHome pip dashboard (esphome
    <= 2026.5.0).

    Use when: a local MQTT broker still shows discovery entries for a
    device that was removed or renamed, and the installed dashboard still
    serves the legacy `/clean-mqtt` route.
    Not for: the currently documented "ESPHome Device Builder" add-on —
    expect this to fail there (connection/timeout/404-style error) until an
    equivalent command is found or ESPHome restores one; check
    `esphome_get_addon_logs` after a failure.
    Returns: `{"device": ..., "action": "clean_mqtt", "exit_code": <int>,
    "success": <bool>, "log_tail": [...], "log_lines": <int>}` on
    completion.
    Errors: `{"device": ..., "action": "clean_mqtt", "error": "Invalid
    device name: ..."}` for a path-escaping `name`; `{"error": "timeout",
    ...}` when `timeout` elapses; `{"error": "Cannot connect to ESPHome
    dashboard at ...: ..."}` when unreachable.
    """
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "clean_mqtt", "error": str(e)}
    result = _dash_ws_spawn("/clean-mqtt", {"configuration": filename}, timeout=timeout, tail_lines=log_lines)
    return {"device": name, "action": "clean_mqtt", **result}


@mcp.tool(annotations=read("Get ESPHome add-on status"))
def get_addon_info() -> dict:
    """Get the ESPHome add-on's status, version and update availability via the Supervisor API.

    The add-on slug is discovered dynamically from the live `/addons` list
    (see `_discover_esphome_slug`) rather than guessed from a fixed set of
    hashes — the hash prefix in `<hash>_esphome` differs per add-on-store
    repository/installation.

    Use when: checking whether the ESPHome add-on is installed, running,
    and up to date.
    Not for: log output — use `esphome_get_addon_logs`.
    Returns: `{"slug", "name", "state", "version", "version_latest",
    "update_available", "ingress_url"}`.
    Errors: `{"error": "ESPHome add-on not found. Tried: [...]"}` when no
    matching add-on slug responds, or when `SUPERVISOR_TOKEN` is unset.
    """
    for slug in _esphome_slug_candidates():
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
    return {"error": f"ESPHome add-on not found. Tried: {_esphome_slug_candidates()}"}


@mcp.tool(annotations=read("Get ESPHome add-on log output"))
def get_addon_logs(
    lines: Annotated[
        int,
        Field(description="Number of trailing log lines to return, e.g. 150."),
    ] = 150,
) -> dict:
    """Get the ESPHome add-on's recent log output.

    Fetches raw text logs via the Supervisor API and returns the last
    `lines` lines. Shows compile errors, OTA status and device connection
    events. The add-on slug is discovered dynamically — see
    `esphome_get_addon_info`.

    Use when: diagnosing why `esphome_compile_device`/`esphome_upload_device`/
    `esphome_validate_config` failed, or checking recent device activity.
    Not for: any other add-on's logs — use
    `supervisor_get_addon_logs` with its slug instead.
    Returns: `{"slug": ..., "logs": "<text>", "total_lines": <int>}`.
    Errors: `{"error": "ESPHome add-on not found. Tried: [...]"}` when no
    matching slug responds; `{"error": "HTTP <status>"}` for a non-404 HTTP
    error; `{"error": str(e)}` for any other failure.
    """
    candidates = _esphome_slug_candidates()
    for slug in candidates:
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
    return {"error": f"ESPHome add-on not found. Tried: {candidates}"}


@mcp.tool(annotations=read("Check ESPHome dashboard reachability"))
def ping_dashboard() -> dict:
    """Check whether the ESPHome dashboard responds at its configured URL.

    Calls `GET /ping` on `ESPHOME_DASHBOARD_URL` (default
    `http://homeassistant.local:6052`, override via that env var).
    `reachable: false` here does not necessarily mean the add-on is down —
    it means this URL/port isn't reachable from nexus's container; two
    live-only things to check next are whether `mDNS homeassistant.local`
    actually resolves inside the nexus container, and whether the add-on's
    `ingress_url`/host port from `esphome_get_addon_info` matches
    `ESPHOME_DASHBOARD_URL`.

    Use when: diagnosing a connection failure from
    `esphome_compile_device`/`esphome_validate_config`/`esphome_upload_device`.
    Not for: add-on version/update status — use `esphome_get_addon_info`.
    Returns: `{"url": ..., "reachable": <bool>, "result": {...}}`.
    """
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

    try:
        path = _device_path(name)
    except ValueError as e:
        return None, str(e)
    if not path.exists():
        return None, f"Not found: {path.name}"
    try:
        parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=_L)
        return parsed, ""
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"


def _lvgl_save(name: str, config: dict) -> dict:
    """Dump ESPHome config dict back to YAML, restoring NUL-encoded !tag markers.

    This is a full-file rewrite via `yaml.dump` — comments, blank lines and YAML
    anchors/aliases in the original file are NOT preserved. If the file already
    exists, a `<file>.bak` copy of the pre-rewrite content is written first so the
    original formatting can be recovered manually.
    """
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

    try:
        path = _device_path(name)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    backup_path: Path | None = None
    try:
        if path.exists():
            backup_path = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup_path)
        content = yaml.dump(config, Dumper=_D, default_flow_style=False,
                            allow_unicode=True, sort_keys=False)
        path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "name": path.name,
            "bytes": len(content.encode()),
            "backup": str(backup_path) if backup_path else None,
        }
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


@mcp.tool(annotations=read("List ESPHome devices with LVGL support"))
def lvgl_list_devices() -> list[dict]:
    """List ESPHome devices whose YAML config has an `lvgl:` section.

    Parses every device YAML under `/config/esphome/` (tolerating parse
    errors by skipping that device) and reports the ones with LVGL display
    support.

    Use when: finding which devices have an LVGL screen before inspecting
    or editing its pages/widgets.
    Not for: page detail on one device — use `esphome_lvgl_get_pages`.
    Returns: list of `{"device", "pages": <count>, "page_ids": [...]}`.
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


@mcp.tool(annotations=read("Get an ESPHome device's LVGL pages"))
def lvgl_get_pages(
    device_name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_display'."),
    ],
) -> dict:
    """Get the LVGL pages configured on an ESPHome device.

    Parses the device's YAML and reads the `lvgl:` section's `pages` and
    `displays[0].default_page`.

    Use when: getting page IDs, background colors and widget counts before
    drilling into one page with `esphome_lvgl_get_page_widgets`.
    Not for: the widgets on one specific page — use
    `esphome_lvgl_get_page_widgets`.
    Returns: `{"device": ..., "default_page": ..., "pages": [{"id", "bg_color",
    "widget_count"}, ...]}`.
    Errors: `{"error": "..."}` when the file can't be found/parsed, or when
    there is no `lvgl:` section in the device config.
    """
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


@mcp.tool(annotations=read("Get widgets on an ESPHome LVGL page"))
def lvgl_get_page_widgets(
    device_name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_display'."),
    ],
    page_id: Annotated[
        str,
        Field(description="LVGL page ID to inspect, from `esphome_lvgl_get_pages`."),
    ],
) -> dict:
    """Get all LVGL widgets on one page of an ESPHome device.

    Parses the device's YAML and summarises each widget on the requested
    page: type, ID, position and key properties. Lambda callback values are
    shown as the literal string `<lambda>` (opaque here — use
    `esphome_get_config` to see the full source).

    Use when: inspecting an existing page's widget tree before adding or
    removing a widget.
    Not for: page-level metadata only — use `esphome_lvgl_get_pages`.
    Returns: `{"device", "page", "widget_count", "widgets": [...]}`.
    Errors: `{"error": "..."}` when the file can't be found/parsed, when
    there is no `lvgl:` section, or when `page_id` doesn't exist.
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


@mcp.tool(annotations=read("Get an ESPHome device's LVGL styles"))
def lvgl_get_styles(
    device_name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_display'."),
    ],
) -> dict:
    """Get the LVGL theme and style definitions from an ESPHome device config.

    Parses the device's YAML and reads the `lvgl:` section's `theme`,
    `style_definitions` and `gradients` keys.

    Use when: checking what theme/styles a device's LVGL UI currently uses
    before referencing a style name in a new widget.
    Not for: page/widget content — use `esphome_lvgl_get_pages`/
    `esphome_lvgl_get_page_widgets`.
    Returns: `{"device", "theme", "style_definitions", "gradients"}`.
    Errors: `{"error": "..."}` when the file can't be found/parsed, or when
    there is no `lvgl:` section in the device config.
    """
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


@mcp.tool(annotations=read("Validate an ESPHome device's LVGL structure"))
def lvgl_validate(
    device_name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_display'."),
    ],
) -> dict:
    """Validate an ESPHome device's LVGL structure client-side, without the dashboard.

    Checks for duplicate page/widget IDs and `default_page` references that
    don't exist among the parsed pages. This is a structural check only —
    it does not validate widget properties or ESPHome component
    correctness.

    Use when: a quick local pre-check after editing LVGL pages/widgets,
    before a full `esphome_validate_config` or `esphome_compile_device`.
    Not for: full component/property validation — use
    `esphome_validate_config`.
    Returns: `{"device", "valid": <bool>, "pages": <count>, "widgets":
    <count>, "issues": [...]}`.
    Errors: `{"error": "...", "valid": False}` when the file can't be
    found/parsed, or when there is no `lvgl:` section in the device config.
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


@mcp.tool(annotations=destructive("Add an LVGL widget to a page", idempotent=False))
def lvgl_add_widget(
    device_name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_display'."),
    ],
    page_id: Annotated[
        str,
        Field(description="LVGL page ID to add the widget to, from `esphome_lvgl_get_pages`."),
    ],
    widget_type: Annotated[
        str,
        Field(
            description=(
                "LVGL widget type, e.g. 'label', 'button', 'slider', 'arc', "
                "'switch', 'spinbox', 'img', 'line', 'meter'."
            )
        ),
    ],
    properties: Annotated[
        dict,
        Field(
            description=(
                "Widget properties, e.g. {'id': ..., 'x': ..., 'y': ..., "
                "'width': ..., 'height': ..., 'text': ..., 'value': ..., "
                "'styles': ...}."
            )
        ),
    ],
) -> dict:
    """Add an LVGL widget to a page on an ESPHome device and save the whole config.

    Appends `{widget_type: properties}` to the page's `widgets` list, then
    rewrites the entire device YAML file via `yaml.dump` — comments, blank
    lines and YAML anchors/aliases elsewhere in the file are NOT preserved.
    A `<file>.bak` copy of the previous content is written first so the
    original formatting can be recovered manually. Lambda callbacks
    (`on_click`, `on_value_changed`) are not supported here — add them
    manually via `esphome_get_config`/`esphome_write_config`. Calling this
    twice with the same arguments adds two separate widgets.

    Use when: adding one widget to an existing LVGL page without
    hand-editing the full YAML.
    Not for: removing a widget — use `esphome_lvgl_delete_widget`; not for
    lambda callbacks or other structural edits — use
    `esphome_get_config`/`esphome_write_config`. After adding, run
    `esphome_lvgl_validate`/`esphome_validate_config`, then
    `esphome_compile_device` and `esphome_upload_device` to deploy.
    Returns: `{"success": True, "name": ..., "bytes": ..., "backup": ...,
    "added": <widget_type>, "to_page": <page_id>}` on success.
    Errors: `{"error": "..."}` when the file can't be found/parsed, when
    there is no `lvgl:` section, when `page_id` doesn't exist, or when the
    save itself fails (`{"success": False, "error": ...}`).
    Limits: rewrites and overwrites the whole file; only the pre-write
    content is recoverable, via the `.bak` copy.
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


@mcp.tool(annotations=destructive("Delete an LVGL widget from a page", idempotent=True))
def lvgl_delete_widget(
    device_name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_display'."),
    ],
    page_id: Annotated[
        str,
        Field(description="LVGL page ID the widget belongs to, from `esphome_lvgl_get_pages`."),
    ],
    widget_id: Annotated[
        str,
        Field(description="Widget `id` to delete, from `esphome_lvgl_get_page_widgets`."),
    ],
) -> dict:
    """Delete an LVGL widget by id from a page on an ESPHome device and save the whole config.

    Removes the matching widget from the page's `widgets` list, then
    rewrites the entire device YAML file via `yaml.dump` — comments, blank
    lines and YAML anchors/aliases elsewhere in the file are NOT preserved.
    A `<file>.bak` copy of the previous content is written first so the
    original formatting can be recovered manually.

    Use when: removing one widget from an existing LVGL page.
    Not for: adding a widget — use `esphome_lvgl_add_widget`. After
    deleting, run `esphome_lvgl_validate`/`esphome_validate_config`, then
    `esphome_compile_device` and `esphome_upload_device` to deploy.
    Returns: `{"success": True, "name": ..., "bytes": ..., "backup": ...,
    "deleted": <widget_id>, "from_page": <page_id>}` on success.
    Errors: `{"error": "..."}` when the file can't be found/parsed, when
    there is no `lvgl:` section, when `page_id`/`widget_id` doesn't exist,
    or when the save itself fails (`{"success": False, "error": ...}`).
    Limits: rewrites and overwrites the whole file; only the pre-write
    content is recoverable, via the `.bak` copy.
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
