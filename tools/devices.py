from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read

mcp = FastMCP("devices")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


_DEVICE_FIELDS = (
    "id",
    "name_by_user",
    "name",
    "area_id",
    "manufacturer",
    "model",
    "entry_type",
)


def _slim(d: dict) -> dict:
    """Project device-registry entry down to common fields, plus config_entries."""
    out = {k: d.get(k) for k in _DEVICE_FIELDS}
    if "config_entries" in d:
        out["config_entries"] = d.get("config_entries")
    if "disabled_by" in d:
        out["disabled_by"] = d.get("disabled_by")
    return out


@mcp.tool(annotations=read("List devices (slim fields)"))
def list_devices() -> list[dict]:
    """List all devices with a slimmed set of common fields.

    Calls WS `config/device_registry/list` and projects each entry down to
    id, name_by_user, name, area_id, manufacturer, model, entry_type, plus
    config_entries and disabled_by when present.

    Use when: you need the device registry without every raw field.
    Not for: the full unfiltered registry — use `areas_list_devices`;
    devices scoped to one area — use `devices_list_devices_in_area`; fuzzy
    name-based search — use `search_search_devices`.
    Returns: list of slimmed device dicts as described above."""
    devices = ha._ws_call("config/device_registry/list")
    return [_slim(d) for d in (devices or [])]


@mcp.tool(annotations=destructive("Update device registry entry", idempotent=False))
def update_device(
    device_id: Annotated[str, Field(description="Device registry ID to update; obtain it from devices_list_devices.")],
    name_by_user: Annotated[str | None, Field(description="New user-facing name override for the device; omit to leave the current name unchanged.")] = None,
    area_id: Annotated[str | None, Field(description="Area registry ID to assign the device to, from areas_list_areas; omit to leave the current area unchanged.")] = None,
    disabled_by: Annotated[str | None, Field(description="'user' disables the device and its entities, '' (empty string) re-enables it; omit to leave the disabled state unchanged.")] = None,
) -> dict:
    """Update fields on an existing device registry entry.

    Calls WS `config/device_registry/update`; only the parameters you pass
    are sent, so omitted or null fields are left unchanged.

    Use when: renaming a device, moving it to another area, or enabling or
    disabling it.
    Returns: dict with `device_id` and `result` (the updated device,
    slimmed like `devices_list_devices`).
    Limits: `area_id` can only be set here, not cleared — there is no
    parameter to remove an existing area assignment."""
    payload: dict = {"device_id": device_id}
    if name_by_user is not None:
        payload["name_by_user"] = name_by_user
    if area_id is not None:
        payload["area_id"] = area_id
    if disabled_by is not None:
        payload["disabled_by"] = disabled_by if disabled_by else None
    result = ha._ws_call("config/device_registry/update", **payload)
    return {"device_id": device_id, "result": _slim(result) if isinstance(result, dict) else result}


@mcp.tool(annotations=destructive("Remove device from config entry", idempotent=True))
def remove_device(
    device_id: Annotated[str, Field(description="Device registry ID to remove; obtain it from devices_list_devices.")],
    config_entry_id: Annotated[str, Field(description="Config entry ID the device is attached to; the device is detached only from this entry.")],
) -> dict:
    """Remove a device from Home Assistant after checking it belongs to the given config entry.

    Reads the device registry and refuses unless the device belongs to
    `config_entry_id`, then sends WS `config/device_registry/remove`
    (HA 2026.x); on an HA without that command it sends the older
    `config/device_registry/remove_config_entry`. The integration must
    support device removal (HA asks it first), otherwise HA refuses.

    Use when: removing a stale device an integration no longer provides.
    Not for: renaming, moving or disabling it — use `devices_update_device`.
    Returns: `{"removed": true, "device_id", "config_entry_id", "command"}`.
    Errors: `device_not_found`, `config_entry_not_in_device` (no removal
    sent), `ws_error` with Home Assistant's `code`/`message` (e.g. the
    integration rejected the removal).
    Limits: `config_entry_id` is required as a safety check even though
    HA 2026.x removes the device as a whole."""
    devices = ha._ws_call("config/device_registry/list") or []
    device = next((d for d in devices if isinstance(d, dict) and d.get("id") == device_id), None)
    if device is None:
        return {"error": "device_not_found", "device_id": device_id}
    entries = set(device.get("config_entries") or []) | {device.get("config_entry_id")}
    if config_entry_id not in entries:
        return {
            "error": "config_entry_not_in_device",
            "device_id": device_id,
            "config_entry_id": config_entry_id,
            "config_entries": sorted(e for e in entries if e),
        }
    command = "config/device_registry/remove"
    try:
        try:
            ha._ws_call(command, device_id=device_id)
        except ha.HAWebSocketError as e:
            if e.code != "unknown_command":
                raise
            command = "config/device_registry/remove_config_entry"
            ha._ws_call(command, device_id=device_id, config_entry_id=config_entry_id)
    except ha.HAWebSocketError as e:
        return {"error": "ws_error", "code": e.code, "message": e.message, "command": command}
    return {"removed": True, "device_id": device_id, "config_entry_id": config_entry_id, "command": command}


@mcp.tool(annotations=read("List devices in area"))
def list_devices_in_area(
    area_id: Annotated[str, Field(description="Area registry ID to filter by; obtain it from areas_list_areas.")],
) -> list[dict]:
    """List devices currently assigned to one area.

    Calls WS `config/device_registry/list` and filters the slimmed device
    list (see `devices_list_devices`) down to entries whose `area_id`
    matches.

    Use when: you need only the devices in one area rather than the whole
    registry.
    Not for: the whole device registry — use `devices_list_devices` or
    `areas_list_devices`; fuzzy name-based search — use
    `search_search_devices`.
    Returns: list of slimmed device dicts (see `devices_list_devices`) whose
    area_id equals the given area_id.
    Limits: devices without an area are never returned here."""
    devices = ha._ws_call("config/device_registry/list") or []
    return [_slim(d) for d in devices if d.get("area_id") == area_id]
