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
    """Remove a device from one of its config entries.

    Calls WS `config_entries/remove_device` with both IDs; if the device
    belongs to other config entries it keeps existing under those.

    Use when: detaching a device from one integration without touching its
    other config entries.
    Returns: dict with `device_id`, `config_entry_id` and the raw WS
    `result`.
    Limits: both `device_id` and `config_entry_id` are required — there is
    no way to remove a device from every config entry in one call."""
    result = ha._ws_call(
        "config_entries/remove_device",
        device_id=device_id,
        config_entry_id=config_entry_id,
    )
    return {
        "device_id": device_id,
        "config_entry_id": config_entry_id,
        "result": result,
    }


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
