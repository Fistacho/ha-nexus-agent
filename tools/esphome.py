"""ESPHome device management tools.

Read configs from /config/esphome/, query HA device/entity registry, and drive
compile / validate / OTA-upload via the ESPHome "Device Builder" dashboard.

All dashboard I/O goes through `esphome_dashboard.DashboardClient`
(ADR-0004 partition A) — this module never opens a socket to the dashboard
itself. It owns dashboard *discovery* only: in add-on mode (`SUPERVISOR_TOKEN`
set) the dashboard is always dialled at `http://127.0.0.1:<ingress_port>`, the
installed ESPHome add-on's own site-ingress port, found from the live
Supervisor `/addons` listing (`_discover_esphome_slug`) and
`GET /addons/<slug>/info`; this only works because nexus itself runs with
`host_network: true` (ADR-0004 D1) and shares the host's loopback with the
ESPHome add-on's own `host_network` container. In standalone/HACS mode, set
`ESPHOME_DASHBOARD_URL` to the dashboard's own URL — there is no default
guess. `esphome_dashboard.DashboardError` subclasses raised by the client are
translated here into this module's `{"error": ...}` tool-result shape
(ADR-0002); see `_dashboard_error_response`.

Add-on identity & protocol (re-verified 2026-09-27 against the live add-on list and
GitHub, since the pip `esphome` package dropped its built-in dashboard for the
standalone "ESPHome Device Builder" between esphome 2026.5.0 and 2026.7.0):
- The add-on's Supervisor slug is NOT one of a fixed set of hashes — it is looked up
  dynamically from `GET /addons` (see `_discover_esphome_slug`) instead of guessing.
- `esphome/device-builder` (github.com/esphome/device-builder) is the dashboard.
  Its `esphome_device_builder/api/legacy.py` keeps exactly six HA-compat routes:
  GET /devices, GET /ping, GET /json-config, POST /encryption-key, and two
  WebSocket routes — GET /compile and GET /upload — confirmed (raw file read
  2026-09-27) to use the *same* spawn wire protocol as the old dashboard
  (`{"type": "spawn", "configuration": ..., "port": ...}` in, `{"event": "line"/"exit"}`
  out), just re-routed through Device Builder's firmware job queue. There is no
  legacy `/validate` or `/clean-mqtt` WebSocket route anymore.
- Validate goes through Device Builder's newer multiplexed `/ws` command API
  (docs/API.md: `devices/validate` command) — see
  `esphome_dashboard.DashboardClient.validate`.
- Clean-mqtt has no confirmed equivalent in Device Builder at all (searched
  docs/API.md for every mqtt/clean-related command; the closest, `firmware/clean`,
  is a build-artifact clean, not an MQTT discovery-topic clean). `clean_mqtt`
  therefore bypasses the dashboard entirely and goes through HA's own `mqtt`
  integration instead (confirmed 2026-09-27 against `homeassistant/components/
  mqtt/__init__.py` and `debug_info.py` on github.com/home-assistant/core):
  WS command `mqtt/device/debug_info` (schema `{device_id: str}`, returns
  `{"entities": [...], "triggers": [...]}` with each entry's
  `discovery_data.topic`) when the device has a matching HA device-registry
  entry, else a short `mqtt/subscribe` window on a discovery-prefix wildcard;
  either way, clearing a topic is the `mqtt.publish` service with
  `payload=""` and `retain=True` (`MQTT_PUBLISH_SCHEMA` accepts
  `payload=None|str`, so `""` validates, and an empty retained publish is
  the standard MQTT way to delete a broker's retained message on a topic).
- Connecting a device's online/offline status is not read from a
  `binary_sensor.*_api_connection_status` entity — confirmed live 2026-09-27
  that entity does not reliably exist. Instead this follows
  `homeassistant/components/esphome/entity.py`'s own logic: every non-deep-
  -sleep entity's `available` (and thus its state, `"unavailable"` when
  false) is driven by one shared `RuntimeEntryData.available` flag per
  config entry, so any of a device's own entities reporting a state other
  than `"unavailable"` means that device is connected.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import httpx
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import esphome_dashboard as dash
import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("esphome")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))
_ESPHOME_DIR = _CONFIG_PATH / "esphome"

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


def _supervisor_get(path: str) -> dict:
    """Adapt `_sup_json` to `esphome_dashboard.DashboardLocator`'s `supervisor_get` contract.

    `_sup_json` reports every failure — missing `SUPERVISOR_TOKEN`, an HTTP
    error, or a network exception — as a returned `{"error": ...}` dict
    rather than raising. `DashboardLocator` needs the opposite (ADR-0004 D3
    point 1: "supervisor_get ... raise[s] on failure to reach Supervisor at
    all"), so it can turn any of those into `esphome_unreachable` uniformly
    without needing to know `_sup_json`'s own error shape. This is that thin
    adapter; it performs no I/O of its own.
    """
    result = _sup_json("GET", path)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


_dashboard_lock = threading.RLock()  # reentrant: _get_dashboard_client() calls _get_dashboard_locator()
                                      # while already holding this lock.
_dashboard_locator: dash.DashboardLocator | None = None
_dashboard_client: dash.DashboardClient | None = None


def _get_dashboard_locator() -> dash.DashboardLocator:
    """Process-wide `DashboardLocator` singleton, built lazily on first use.

    Reads `os.environ` live on every `locate()` call (see
    `esphome_dashboard.DashboardLocator`), so this does not need rebuilding
    when `ESPHOME_DASHBOARD_URL`/`SUPERVISOR_TOKEN` change — only its
    in-memory `supervisor_ingress` cache (`invalidate()`) does. Reset for
    tests with `_reset_dashboard_client()`.
    """
    global _dashboard_locator
    with _dashboard_lock:
        if _dashboard_locator is None:
            _dashboard_locator = dash.DashboardLocator(
                env=os.environ,
                supervisor_get=_supervisor_get,
                discover_slug=_discover_esphome_slug,
            )
        return _dashboard_locator


def _get_dashboard_client() -> dash.DashboardClient:
    """Process-wide `DashboardClient` singleton (one client/locator per process, ADR-0004 D3)."""
    global _dashboard_client
    with _dashboard_lock:
        if _dashboard_client is None:
            _dashboard_client = dash.DashboardClient(_get_dashboard_locator())
        return _dashboard_client


def _reset_dashboard_client() -> None:
    """Test-only: drop the cached locator/client singletons so a fresh env is picked up."""
    global _dashboard_locator, _dashboard_client
    with _dashboard_lock:
        _dashboard_locator = None
        _dashboard_client = None


def _diagnose_dashboard_unreachable() -> dict:
    """Build the `diagnosis` dict attached to an `esphome_unreachable`/`esphome_auth_required` error.

    Re-derives everything fresh from env/Supervisor rather than from the
    raised `DashboardError`'s own `.extra` — that works whether the failure
    happened while resolving the endpoint at all (`DashboardLocator.locate()`
    itself raised) or while talking to an endpoint that did resolve
    (connection refused, or the dashboard refusing auth after connecting).

    Returns `{"mode", "dashboard_url", "slug", "addon_state", "ingress",
    "ingress_port", "advice"}` (ADR-0004 D3) — `mode` is `"explicit"`,
    `"supervisor_ingress"`, or `"unknown"` (neither `ESPHOME_DASHBOARD_URL`
    nor `SUPERVISOR_TOKEN` set); every field besides `mode`/`dashboard_url`/
    `advice` is `None` outside `supervisor_ingress` mode, since there is no
    add-on to describe.
    """
    explicit_url = (os.getenv("ESPHOME_DASHBOARD_URL") or "").strip()
    if explicit_url:
        return {
            "mode": "explicit",
            "dashboard_url": explicit_url,
            "slug": None,
            "addon_state": None,
            "ingress": None,
            "ingress_port": None,
            "advice": (
                f"ESPHOME_DASHBOARD_URL is set to {explicit_url!r} but the dashboard did not "
                "respond as expected. Check that an ESPHome Device Builder is actually "
                "listening there and reachable from this container/host."
            ),
        }

    if not (os.getenv("SUPERVISOR_TOKEN") or "").strip():
        return {
            "mode": "unknown",
            "dashboard_url": None,
            "slug": None,
            "addon_state": None,
            "ingress": None,
            "ingress_port": None,
            "advice": (
                "Neither ESPHOME_DASHBOARD_URL nor SUPERVISOR_TOKEN is set — there is no "
                "configured way to reach an ESPHome dashboard from here."
            ),
        }

    slug = _discover_esphome_slug()
    if not slug:
        return {
            "mode": "supervisor_ingress",
            "dashboard_url": None,
            "slug": None,
            "addon_state": None,
            "ingress": None,
            "ingress_port": None,
            "advice": (
                "The ESPHome add-on was not found in the Supervisor add-on list — install it, "
                "or check that it is not stopped/uninstalled."
            ),
        }

    info = _sup_json("GET", f"/addons/{slug}/info")
    if "error" in info:
        return {
            "mode": "supervisor_ingress",
            "dashboard_url": None,
            "slug": slug,
            "addon_state": None,
            "ingress": None,
            "ingress_port": None,
            "advice": f"Could not reach Supervisor to get add-on '{slug}' info: {info['error']}.",
        }

    data = info.get("data", {})
    state = data.get("state")
    ingress = bool(data.get("ingress"))
    port = data.get("ingress_port")
    valid_port = isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
    dashboard_url = f"http://127.0.0.1:{port}" if valid_port else None

    if state != "started":
        advice = f"ESPHome add-on '{slug}' is not running (state={state!r}) — start it under Settings > Add-ons."
    elif not ingress:
        advice = (
            f"ESPHome add-on '{slug}' does not have ingress enabled — nexus needs it (over its own "
            "host_network, ADR-0004) to reach the dashboard at 127.0.0.1:<ingress_port>."
        )
    elif not valid_port:
        advice = f"ESPHome add-on '{slug}' reported no usable ingress_port ({port!r})."
    else:
        advice = (
            f"Add-on '{slug}' looks installed, running and ingress-enabled at {dashboard_url}, but the "
            "connection still failed. Check `esphome_get_addon_logs` for a crash there, and that nexus "
            "itself is running with host_network enabled (ADR-0004) so it shares the host's loopback."
        )

    return {
        "mode": "supervisor_ingress",
        "dashboard_url": dashboard_url,
        "slug": slug,
        "addon_state": state,
        "ingress": ingress,
        "ingress_port": port if valid_port else None,
        "advice": advice,
    }


def _dashboard_error_response(
    e: dash.DashboardError, *, device: str | None = None, action: str | None = None,
) -> dict:
    """Map one `esphome_dashboard.DashboardError` to this module's `{"error": ...}` shape.

    Shared by every tool that talks to `DashboardClient` (`ping_dashboard`,
    `compile_device`, `validate_config`, `upload_device`) so the mapping —
    stable `.code` values, `dashboard_error_code`/`details` for a dashboard-
    reported failure, `log_tail`/`log_lines`/`job_may_still_be_running` for a
    timeout, and a Supervisor-derived `diagnosis` for `esphome_unreachable`/
    `esphome_auth_required` (ADR-0004 D3) — lives in exactly one place.
    """
    result: dict = {}
    if device is not None:
        result["device"] = device
    if action is not None:
        result["action"] = action
    result["error"] = e.code
    message = str(e)

    if isinstance(e, dash.CommandFailedError):
        result["dashboard_error_code"] = e.dashboard_error_code
        result["details"] = e.details
    elif isinstance(e, dash.DashboardTimeoutError):
        result["timeout"] = e.timeout
        result["log_tail"] = e.log_tail
        result["log_lines"] = e.log_lines
        result["job_may_still_be_running"] = e.job_may_still_be_running
        if e.job_may_still_be_running:
            message += (
                " Do not retry — the dashboard's firmware job queue does not cancel a job just "
                "because this call stopped waiting for it; check esphome_get_addon_logs instead."
            )
    elif isinstance(e, (dash.UnreachableError, dash.AuthRequiredError)):
        result["diagnosis"] = _diagnose_dashboard_unreachable()

    result["message"] = message
    return result


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


_BLOCKED_DEVICE_NAMES = {"secrets", "secrets.yaml"}
"""Device names this module refuses everywhere (ADR-0004 D-3): ESPHome's own
per-device secrets file, `/config/esphome/secrets.yaml`, is not a device
config. `files.py`'s `_BLOCKED_PATHS` blocks the equivalent generic
`/config/secrets.yaml`; this module has its own denylist because device
names here are bare stems/filenames, not `files`-style relative paths."""


def _safe_filename(name: str) -> str:
    """Validate a user-supplied ESPHome device name and return its `<name>.yaml` filename.

    Rejects path separators, a leading dot, blank input, and (case-
    insensitively) `secrets`/`secrets.yaml` (ADR-0004 D-3) — this is the
    boundary that stops `write_config(name="../../../etc/cron.d/x", ...)`-
    style path traversal and `get_config(name="secrets")`-style access to
    ESPHome's own secrets file, applied to every device-name parameter in
    this module (get/write_config, compile/validate/upload/clean_mqtt, the
    LVGL editor tools) before it reaches disk or is forwarded to the
    dashboard as a `configuration` argument.
    """
    if "/" in name or "\\" in name or name.startswith(".") or not name.strip():
        raise ValueError(f"Invalid device name: {name!r}")
    if name.strip().lower() in _BLOCKED_DEVICE_NAMES:
        raise ValueError(f"Access to '{name}' is blocked: it is not a device config.")
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
        if not f.name.startswith(".") and f.stem.lower() not in _BLOCKED_DEVICE_NAMES
    )


# ── tools ─────────────────────────────────────────────────────────────────────

_ESPHOME_MANUFACTURERS = {"espressif", "esphome"}


def _esphome_config_entry_ids() -> set[str]:
    """Every HA config-entry ID belonging to the `esphome` integration, or empty on error."""
    ids: set[str] = set()
    try:
        for entry in ha._ws_call("config_entries/get", domain="esphome"):
            ids.add(entry.get("entry_id", ""))
    except Exception:
        pass
    return ids


def _is_esphome_device(d: dict, entry_ids: set[str]) -> bool:
    """True if device-registry entry `d` belongs to the ESPHome integration.

    Three independent strategies, any one of which is enough: a config entry
    in `entry_ids`, an `identifiers` tuple whose domain is `"esphome"`, or
    (fallback) a `manufacturer` of Espressif/esphome.
    """
    by_entry = bool(entry_ids and set(d.get("config_entries", [])) & entry_ids)
    ids = d.get("identifiers", [])
    by_id = any(
        isinstance(i, (list, tuple)) and len(i) >= 1 and str(i[0]) == "esphome"
        for i in ids
    )
    by_mfr = str(d.get("manufacturer") or "").lower() in _ESPHOME_MANUFACTURERS
    return by_entry or by_id or by_mfr


def _device_connected(device_entities: list[dict], states: dict[str, dict]) -> bool | None:
    """Whether a device is connected, from its own entities' states.

    Per `homeassistant/components/esphome/entity.py`, every non-deep-sleep
    entity's `available` — and thus its state, `"unavailable"` when false —
    tracks one shared per-config-entry flag, so any entity reporting a state
    other than `"unavailable"` means the device is connected. Returns `None`
    (unknown) when the device has no known entities or none of them have a
    recorded state.
    """
    saw_a_state = False
    for e in device_entities:
        s = states.get(e.get("entity_id", ""))
        if s is None:
            continue
        saw_a_state = True
        if s.get("state") != "unavailable":
            return True
    return False if saw_a_state else None


@mcp.tool(annotations=read("List ESPHome devices"))
def list_devices() -> dict:
    """List ESPHome devices from YAML configs, the HA device registry and online status.

    Combines `<name>.yaml` files under `/config/esphome/` with matching HA
    device-registry entries (identified via ESPHome config entries,
    `identifiers` domain, or manufacturer as a fallback). A device's
    connected status comes from its own entities' states (entity registry
    `device_id` match): any entity in a state other than `"unavailable"`
    means connected, since ESPHome entities share one per-config-entry
    availability flag (`homeassistant/components/esphome/entity.py`) —
    not from a `binary_sensor.*_api_connection_status` entity, which is not
    guaranteed to exist.

    Use when: getting an overview of every ESPHome device, whether or not it
    currently has a matching HA device.
    Not for: one device's entities — use `esphome_get_device_entities`.
    Returns: `{"yaml_configs": [...], "ha_devices": [...], "online": <int>,
    "offline": <int>}`, where each `ha_devices` entry's `connected` is `True`,
    `False`, or `None` (no known entities/states yet).
    Errors: `ha_devices` becomes `[{"error": str(e)}]` if reading the HA
    device registry raises; entity-registry/config-entry/state lookups are
    silently skipped (treated as empty) on error instead of failing the
    whole call.
    """
    configs = _yaml_names()
    entry_ids = _esphome_config_entry_ids()

    entities_by_device: dict[str, list[dict]] = {}
    try:
        for e in ha.get_entity_registry():
            device_id = e.get("device_id")
            if device_id:
                entities_by_device.setdefault(device_id, []).append(e)
    except Exception:
        pass

    states: dict[str, dict] = {}
    try:
        states = {s["entity_id"]: s for s in ha.get_states()}
    except Exception:
        pass

    ha_devices: list[dict] = []
    online = offline = 0
    try:
        for d in ha.get_device_registry():
            if not _is_esphome_device(d, entry_ids):
                continue

            device_id = d.get("id")
            connected = _device_connected(entities_by_device.get(device_id, []), states)
            if connected is True:
                online += 1
            elif connected is False:
                offline += 1

            ha_devices.append({
                "name": d.get("name_by_user") or d.get("name"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "sw_version": d.get("sw_version"),
                "hw_version": d.get("hw_version"),
                "area_id": d.get("area_id"),
                "connected": connected,
                "ha_device_id": device_id,
            })
    except Exception as e:
        ha_devices = [{"error": str(e)}]

    return {
        "yaml_configs": configs,
        "ha_devices": ha_devices,
        "online": online,
        "offline": offline,
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


_REMOTE_SOURCE_RE = re.compile(r"^(?:https?|git|github|gitlab|codeberg)://", re.IGNORECASE)


def _is_remote_source_string(value: str) -> bool:
    """True if `value` looks like a remote `external_components:`/`packages:` source.

    Matches every remote-source prefix ESPHome itself recognizes
    (esphome.io/components/external_components, esphome.io/components/
    packages): `github://`, `gitlab://`, `codeberg://`, `git://`,
    `http(s)://`. A bare relative path — `external_components`'s local
    shorthand, or a `packages:` value that was a `!include` in the source
    YAML (its tag is stripped by `write_config`'s loader, leaving just the
    plain filename) — has none of these and is treated as local.
    """
    return bool(_REMOTE_SOURCE_RE.match(value.strip()))


def _remote_source_from_dict(source: dict) -> str | None:
    """Extract a remote source string from one `source:`/packages dict entry, or None if local.

    `type: local` (`external_components`'s own local dict form) is always
    local, regardless of any other key. Otherwise a `url` key — required by
    `packages`' remote dict form and used by `external_components`'
    `type: git` dict form — marks the entry remote. A dict with neither
    `type: local` nor a `url` matches no documented remote form and is
    skipped rather than guessed at.
    """
    if str(source.get("type", "")).strip().lower() == "local":
        return None
    url = source.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _detect_remote_sources(parsed: dict) -> list[str]:
    """Scan a parsed ESPHome config's `external_components:`/`packages:` for remote sources.

    Static structural check on the already-parsed YAML only — no network
    I/O, nothing fetched or executed here. `external_components:` is a list
    of `{source: ..., ...}` entries whose `source` is a string (a
    `github://...` shorthand, a `type: git` dict, or a local path/`type:
    local` dict) per esphome.io/components/external_components. `packages:`
    is a dict or list whose entries are a local `!include`/bare-path string,
    a remote shorthand string (`github://`/`gitlab://`/`codeberg://`), or a
    dict carrying a `url`, per esphome.io/components/packages. Returns the
    detected remote source strings in encounter order (external_components
    first, then packages); duplicates are preserved, not deduplicated.
    """
    sources: list[str] = []

    external_components = parsed.get("external_components")
    if isinstance(external_components, list):
        for entry in external_components:
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if isinstance(source, str):
                if _is_remote_source_string(source):
                    sources.append(source.strip())
            elif isinstance(source, dict):
                found = _remote_source_from_dict(source)
                if found:
                    sources.append(found)

    packages = parsed.get("packages")
    if isinstance(packages, dict):
        package_values = list(packages.values())
    elif isinstance(packages, list):
        package_values = packages
    else:
        package_values = []
    for value in package_values:
        if isinstance(value, str):
            if _is_remote_source_string(value):
                sources.append(value.strip())
        elif isinstance(value, dict):
            found = _remote_source_from_dict(value)
            if found:
                sources.append(found)

    return sources


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
    success; a remote `external_components:`/`packages:` source
    (`github://`/`gitlab://`/`codeberg://`/`git://`/`http(s)://`, or a dict
    with a `url`) adds `"warning"` (review before `esphome_compile_device`,
    which fetches and runs it) and `"external_sources": [...]`; a config
    with none, or only local (`type: local`, bare-path, `!include`) ones,
    gets neither key.
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
            _tag, lambda loader, node, t=_tag: _tag_passthrough(loader, t, node)
        )

    try:
        # _ESPHomeLoader subclasses yaml.SafeLoader (not yaml.Loader/UnsafeLoader) and
        # only adds passthrough constructors for scalar ESPHome tags -- it cannot
        # construct arbitrary Python objects, so this is not the S506 vulnerability.
        parsed = yaml.load(content, Loader=_ESPHomeLoader)  # noqa: S506
    except yaml.YAMLError as e:
        return {"success": False, "error": f"YAML validation failed: {e}"}

    try:
        path = _device_path(name)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result = {"success": True, "name": path.name, "path": str(path), "bytes": len(content.encode())}

    remote_sources = _detect_remote_sources(parsed if isinstance(parsed, dict) else {})
    if remote_sources:
        result["warning"] = (
            "This config's external_components/packages fetch and execute code from a "
            "remote source at compile time -- review external_sources before calling "
            "esphome_compile_device."
        )
        result["external_sources"] = remote_sources
    return result


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

    Slugifies `device_name` (lowercase, spaces/dashes to underscores) and, if
    a device-registry entry's own slugified name matches, matches entities by
    that device's `device_id` (precise: immune to one device's slug being a
    substring of another's). Falls back to matching entity registry entries
    whose `platform` is "esphome" and whose `entity_id` contains the slug
    when no device-registry entry matches (or that lookup fails).

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

    device_id = None
    try:
        entry_ids = _esphome_config_entry_ids()
        for d in ha.get_device_registry():
            if not _is_esphome_device(d, entry_ids):
                continue
            dname = (d.get("name_by_user") or d.get("name") or "").lower()
            dslug = dname.replace("-", "_").replace(" ", "_")
            if dslug == slug:
                device_id = d.get("id")
                break
    except Exception:
        device_id = None

    result = []
    for e in entity_reg:
        eid = e.get("entity_id", "")
        if device_id is not None:
            matched = e.get("device_id") == device_id
        else:
            matched = e.get("platform") == "esphome" and slug in eid.lower()
        if matched:
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
    """Compile ESPHome firmware for a device via the dashboard's `/compile` WebSocket route.

    Blocks until the compile process exits (~60-180s). Compatible with the
    "ESPHome Device Builder" add-on (esphome/device-builder): `/compile`
    keeps the same spawn wire protocol as the old dashboard, now backed by
    its firmware job queue. A first build for a platform fetches PlatformIO
    packages from the internet before compiling. `only_generate=True` is
    rejected outright (see Errors) — Device Builder ignores that field and
    always runs a full build (ADR-0004 D4); it stays in the signature only
    for input-shape compatibility.

    Use when: building firmware before `esphome_upload_device`.
    Not for: checking config validity only — use `esphome_validate_config`,
    which is faster and skips package downloads.
    Returns: `{"device", "action": "compile", "exit_code", "success",
    "log_tail", "log_lines"}`.
    Errors: `{"error": "esphome_unsupported_option", ...}` for
    `only_generate=True` (no I/O); `{"error": "Invalid device name: ..."}`
    for a path-escaping or blocked (e.g. 'secrets') `name`; otherwise one of
    `esphome_dashboard.DashboardError`'s stable codes —
    `esphome_unreachable`/`esphome_auth_required` add `diagnosis` (see
    `esphome_ping_dashboard`); `timeout` adds `log_tail`/`log_lines`/
    `job_may_still_be_running` (always `True` — do not retry, check
    `esphome_get_addon_logs`); `esphome_command_failed` adds
    `dashboard_error_code`/`details`.
    Limits: blocks up to `timeout` seconds; `log_lines` truncates the log.
    """
    if only_generate:
        return {
            "device": name,
            "action": "compile",
            "error": "esphome_unsupported_option",
            "message": (
                "only_generate=True is not supported: ESPHome Device Builder's /compile route "
                "ignores this field and always runs a full build (ADR-0004 D4) — there is no "
                "generate-only mode to request."
            ),
        }
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "compile", "error": str(e)}
    try:
        result = _get_dashboard_client().compile(filename, timeout=timeout, tail_lines=log_lines)
    except dash.DashboardError as e:
        return _dashboard_error_response(e, device=name, action="compile")
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
    docs/API.md) — the legacy `/validate` route from the old ESPHome
    dashboard no longer exists. Unlike `esphome_compile_device`, this never
    builds firmware or fetches PlatformIO packages. If the dashboard needs
    a username/password, this fails fast instead of hanging (no credential
    wiring exists here).

    Use when: checking a config's validity quickly, e.g. before
    `esphome_compile_device`.
    Not for: a full firmware build — use `esphome_compile_device`; not for
    LVGL-only structural checks — use `esphome_lvgl_validate`, which runs
    without the dashboard.
    Returns: `{"device", "action": "validate", "success", "result",
    "log_tail", "log_lines"}`.
    Errors: `{"error": "Invalid device name: ..."}` for a path-escaping or
    blocked (e.g. 'secrets') `name`; otherwise one of
    `esphome_dashboard.DashboardError`'s stable codes —
    `esphome_auth_required` (credentials this tool cannot supply) and
    `esphome_unreachable` add `diagnosis` (see `esphome_ping_dashboard`);
    `timeout` adds `log_tail`/`log_lines`/`job_may_still_be_running`
    (always `False` — validation enqueues no job); `esphome_command_failed`
    adds `dashboard_error_code`/`details`.
    """
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "validate", "error": str(e)}
    try:
        result = _get_dashboard_client().validate(filename, timeout=timeout, tail_lines=log_lines)
    except dash.DashboardError as e:
        return _dashboard_error_response(e, device=name, action="validate")
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
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually flash the device. False (default) "
                "performs no action and instead returns a confirmation "
                "prompt; `name` is not resolved or touched in that case."
            )
        ),
    ] = False,
) -> dict:
    """Flash previously compiled firmware to an ESPHome device via the dashboard's `/upload` WebSocket route.

    Requires the device to already be built (`esphome_compile_device`) and,
    for OTA, reachable with a matching OTA password. Without `confirm=True`
    returns a confirmation prompt and performs no I/O (ADR-0004 D5) — this
    overwrites the running firmware with no automatic rollback if broken.
    Same "ESPHome Device Builder" (esphome/device-builder) spawn wire
    protocol as `esphome_compile_device`, on `/upload`.

    Use when: deploying a compiled build to the device, once confirmed.
    Not for: building the firmware — use `esphome_compile_device` first.
    Returns: `{"device", "action": "upload", "exit_code", "success",
    "log_tail", "log_lines"}` on completion.
    Errors: `{"error": "confirmation_required", "message", "action"}` when
    `confirm` is false (checked first, before `name` is resolved);
    `{"error": "Invalid device name: ..."}` for a path-escaping or blocked
    (e.g. 'secrets') `name`; otherwise one of
    `esphome_dashboard.DashboardError`'s stable codes —
    `esphome_unreachable`/`esphome_auth_required` add `diagnosis` (see
    `esphome_ping_dashboard`); `timeout` adds `log_tail`/`log_lines`/
    `job_may_still_be_running` (always `True` — do not retry, check
    `esphome_get_addon_logs`); `esphome_command_failed` adds
    `dashboard_error_code`/`details`.
    Limits: requires `confirm=True`; blocks up to `timeout` seconds; no
    rollback if the upload succeeds but firmware is broken.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                f"This will overwrite the running firmware on device '{name}' with no "
                "automatic rollback if it is broken. Call again with confirm=True."
            ),
            "action": f"upload_device(name={name!r}, port={port!r}, confirm=True)",
        }
    try:
        filename = _safe_filename(name)
    except ValueError as e:
        return {"device": name, "action": "upload", "error": str(e)}
    try:
        result = _get_dashboard_client().upload(filename, port, timeout=timeout, tail_lines=log_lines)
    except dash.DashboardError as e:
        return _dashboard_error_response(e, device=name, action="upload")
    return {"device": name, "action": "upload", **result}


_SUB_BRACE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SUB_BARE_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# Window for the wildcard-subscribe fallback in `clean_mqtt`: ESPHome/HA send
# retained discovery messages immediately on subscribe, so a few seconds is
# enough; bounded further by the tool's own `timeout` argument.
_MQTT_DISCOVERY_WINDOW = 3.0


def _apply_substitutions(value: str, substitutions: dict) -> str:
    """Resolve ESPHome `${key}`/`$key` substitution syntax against `substitutions`.

    Two passes, matching ESPHome's own compound-substitution behaviour
    (esphome.io/components/substitutions: "two substitution passes are
    performed allowing compound replacements") — a substitution whose value
    itself contains another substitution still resolves. A token with no
    matching key in `substitutions` is left as-is; callers detect that by
    re-scanning the result with the same patterns.
    """
    def _sub(m: re.Match) -> str:
        v = substitutions.get(m.group(1))
        return str(v) if v is not None else m.group(0)

    for _ in range(2):
        value = _SUB_BRACE_RE.sub(_sub, value)
        value = _SUB_BARE_RE.sub(_sub, value)
    return value


def _resolve_scalar(value, substitutions: dict) -> tuple[str | None, str | None]:
    """Resolve one YAML scalar that may use ESPHome substitutions or carry an HA `!tag`.

    Returns `(resolved, error)` — exactly one is not `None`. A value
    NUL-encoded by `_lvgl_load` (i.e. it was `!secret`/`!include`/`!lambda`/
    etc. in the YAML) can't be resolved outside ESPHome itself; a
    substitution with no matching `substitutions:` key is likewise left
    unresolved. Both are reported as errors rather than used as literal text.
    """
    if value is None:
        return None, "value is not set"
    if not isinstance(value, str):
        return str(value), None
    if value.startswith("\x00!"):
        tag = value.split("\x00", 2)[1]
        return None, f"uses a {tag} tag, which cannot be resolved outside ESPHome itself"
    resolved = _apply_substitutions(value, substitutions)
    if _SUB_BRACE_RE.search(resolved) or _SUB_BARE_RE.search(resolved):
        return None, f"unresolved substitution in {value!r} (not defined in this config's substitutions:)"
    return resolved, None


@mcp.tool(annotations=destructive("Clean stale MQTT discovery entries", idempotent=True))
def clean_mqtt(
    name: Annotated[
        str,
        Field(description="Device name, with or without the '.yaml' suffix, e.g. 'kitchen_sensor'."),
    ],
    timeout: Annotated[
        float,
        Field(
            description=(
                "Maximum seconds to spend collecting retained MQTT discovery "
                "topics via a wildcard subscribe, e.g. 60. Only used when no "
                "matching HA device is found for this config's node name."
            )
        ),
    ] = 60,
    log_lines: Annotated[
        int,
        Field(
            description=(
                "Unused by this tool's HA-mqtt-integration implementation "
                "(kept only for API/parameter-surface compatibility with "
                "earlier versions that spawned a dashboard process and "
                "returned trailing log lines), e.g. 200."
            )
        ),
    ] = 200,
) -> dict:
    """Remove stale MQTT discovery entries for an ESPHome device, through HA's own MQTT integration.

    Skips with zero network calls when the config has no `mqtt:` section.
    Otherwise resolves the node name (`esphome.name`, with `substitutions:`)
    and `mqtt.discovery_prefix` (default `"homeassistant"`). With a matching
    HA device, reads topics via WS `mqtt/device/debug_info`; otherwise
    briefly `mqtt/subscribe`s to `{prefix}/+/{node_name}/#` and collects
    retained topics. Clears each topic via the `mqtt.publish` service,
    `payload=""` and `retain=True` -- MQTT's own way to delete a retained
    message. Never touches the ESPHome dashboard, which has no
    MQTT-topic-clearing command.

    Use when: a removed/renamed device's old MQTT discovery entries still
    linger in Home Assistant.
    Not for: a config with no `mqtt:` section; firmware artifacts -- use
    `esphome_compile_device`.
    Returns: `{"device", "action": "clean_mqtt", "skipped": True, "reason":
    ...}` with no `mqtt:` section; otherwise `{"success": <bool>, "method":
    "debug_info"|"wildcard_subscribe", "topics_cleared": [...], "count":
    <int>}`, plus `"errors": [...]` on partial failures.
    Errors: `{"error": "..."}` for an invalid/missing device file, an
    unresolvable `esphome.name`/`mqtt.discovery_prefix`, or a WebSocket/
    service-call failure.
    Limits: the wildcard fallback only collects for `min(timeout, 3)`
    seconds -- may miss a slow message or catch an unrelated topic.
    """
    parsed, err = _lvgl_load(name)
    if err:
        return {"device": name, "action": "clean_mqtt", "error": err}

    mqtt_cfg = parsed.get("mqtt") if parsed else None
    if not isinstance(mqtt_cfg, dict):
        return {
            "device": name,
            "action": "clean_mqtt",
            "skipped": True,
            "reason": "No mqtt: section in config — this device does not use MQTT discovery.",
        }

    substitutions = parsed.get("substitutions")
    if not isinstance(substitutions, dict):
        substitutions = {}

    esphome_cfg = parsed.get("esphome")
    node_name_raw = esphome_cfg.get("name") if isinstance(esphome_cfg, dict) else None
    if node_name_raw is None:
        return {
            "device": name, "action": "clean_mqtt",
            "error": "No esphome.name in config — cannot determine the MQTT node name.",
        }
    node_name, node_err = _resolve_scalar(node_name_raw, substitutions)
    if node_err:
        return {"device": name, "action": "clean_mqtt", "error": f"Cannot resolve esphome.name: {node_err}"}

    prefix_raw = mqtt_cfg.get("discovery_prefix")
    if prefix_raw is None:
        discovery_prefix = "homeassistant"
    else:
        discovery_prefix, prefix_err = _resolve_scalar(prefix_raw, substitutions)
        if prefix_err:
            return {
                "device": name, "action": "clean_mqtt",
                "error": f"Cannot resolve mqtt.discovery_prefix: {prefix_err}",
            }

    device_id = None
    try:
        entry_ids = _esphome_config_entry_ids()
        node_slug = node_name.lower().replace("-", "_").replace(" ", "_")
        for d in ha.get_device_registry():
            if not _is_esphome_device(d, entry_ids):
                continue
            dslug = (d.get("name_by_user") or d.get("name") or "").lower().replace("-", "_").replace(" ", "_")
            if dslug == node_slug:
                device_id = d.get("id")
                break
    except Exception:
        device_id = None

    topics: set[str] = set()
    try:
        if device_id is not None:
            method = "debug_info"
            info = ha._ws_call("mqtt/device/debug_info", device_id=device_id) or {}
            for entity in info.get("entities", []) or []:
                topic = (entity.get("discovery_data") or {}).get("topic")
                if topic:
                    topics.add(topic)
            for trigger in info.get("triggers", []) or []:
                topic = (trigger.get("discovery_data") or {}).get("topic")
                if topic:
                    topics.add(topic)
        else:
            method = "wildcard_subscribe"
            wildcard = f"{discovery_prefix}/+/{node_name}/#"
            window = min(_MQTT_DISCOVERY_WINDOW, timeout)
            events = ha._ws_collect_events(
                "mqtt/subscribe", is_last=lambda e: False, timeout=window, topic=wildcard,
            )
            for event in events:
                if event.get("retain") and event.get("topic"):
                    topics.add(event["topic"])
    except Exception as e:
        return {"device": name, "action": "clean_mqtt", "error": str(e)}

    cleared: list[str] = []
    publish_errors: list[str] = []
    for topic in sorted(topics):
        try:
            ha.call_service("mqtt", "publish", {"topic": topic, "payload": "", "retain": True})
            cleared.append(topic)
        except Exception as e:
            publish_errors.append(f"{topic}: {e}")

    result = {
        "device": name,
        "action": "clean_mqtt",
        "success": not publish_errors,
        "method": method,
        "topics_cleared": cleared,
        "count": len(cleared),
    }
    if publish_errors:
        result["errors"] = publish_errors
    return result


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
    """Check whether the ESPHome Device Builder dashboard responds.

    Resolves where the dashboard is (`esphome_dashboard.DashboardLocator`,
    ADR-0004 D2) — an explicit `ESPHOME_DASHBOARD_URL` (mode `"explicit"`)
    if set, else in add-on mode the installed ESPHome add-on's own
    site-ingress port, always dialled at `http://127.0.0.1:<ingress_port>`
    (mode `"supervisor_ingress"`) — then calls `GET /ping` there. A `False`
    `reachable` here does not necessarily mean the add-on itself is down;
    see `diagnosis`/`advice` for what to check next.

    Use when: diagnosing a connection failure from
    `esphome_compile_device`/`esphome_validate_config`/`esphome_upload_device`,
    or confirming the dashboard is reachable before calling one of them.
    Not for: add-on version/update status — use `esphome_get_addon_info`.
    Returns: `{"reachable": True, "url": ..., "mode": "explicit"|
    "supervisor_ingress", "result": {...}}` (the raw `/ping` JSON) on
    success.
    Errors: never raises. On failure: `{"reachable": False, "error":
    <stable code>, "message": ...}` — `"esphome_not_configured"` (neither
    `ESPHOME_DASHBOARD_URL` nor the add-on's own info could be resolved) has
    no `url`/`mode`; `"esphome_unreachable"`/`"esphome_auth_required"` add
    `url`, `mode` and a `diagnosis`: `{"mode", "dashboard_url", "slug",
    "addon_state", "ingress", "ingress_port", "advice"}` built from the
    ESPHome add-on's own `GET /addons/<slug>/info` (`slug`/`addon_state`/
    `ingress`/`ingress_port` are `None` outside `supervisor_ingress` mode).
    """
    locator = _get_dashboard_locator()
    try:
        endpoint = locator.locate()
    except dash.DashboardError as e:
        return {**_dashboard_error_response(e), "reachable": False}

    try:
        result = _get_dashboard_client().ping()
    except dash.DashboardError as e:
        return {
            **_dashboard_error_response(e),
            "reachable": False,
            "url": endpoint.base_url,
            "mode": endpoint.source,
        }

    return {"reachable": True, "url": endpoint.base_url, "mode": endpoint.source, "result": result}


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
        _L.add_constructor(_t, lambda loader, node, t=_t: _tag_ctor(loader, t, node))

    try:
        path = _device_path(name)
    except ValueError as e:
        return None, str(e)
    if not path.exists():
        return None, f"Not found: {path.name}"
    try:
        # _L subclasses yaml.SafeLoader (not yaml.Loader/UnsafeLoader) and only adds
        # passthrough constructors for scalar ESPHome tags -- it cannot construct
        # arbitrary Python objects, so this is not the S506 vulnerability.
        parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=_L)  # noqa: S506
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
            for props in widget.values():
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
