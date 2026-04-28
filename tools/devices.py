from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("devices")


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


@mcp.tool()
def list_devices() -> list[dict]:
    """List all devices via WS `config/device_registry/list`.

    Each entry includes id, name_by_user, name, area_id, manufacturer,
    model, entry_type, plus config_entries and disabled_by.
    """
    devices = ha._ws_call("config/device_registry/list")
    return [_slim(d) for d in (devices or [])]


@mcp.tool()
def update_device(
    device_id: str,
    name_by_user: str | None = None,
    area_id: str | None = None,
    disabled_by: str | None = None,
) -> dict:
    """Update a device registry entry.

    Pass only the fields you want to change. `disabled_by` should be "user"
    or None (to re-enable). Uses WS `config/device_registry/update`.
    """
    payload: dict = {"device_id": device_id}
    if name_by_user is not None:
        payload["name_by_user"] = name_by_user
    if area_id is not None:
        payload["area_id"] = area_id
    if disabled_by is not None:
        payload["disabled_by"] = disabled_by if disabled_by else None
    result = ha._ws_call("config/device_registry/update", **payload)
    return {"device_id": device_id, "result": _slim(result) if isinstance(result, dict) else result}


@mcp.tool()
def remove_device(device_id: str, config_entry_id: str) -> dict:
    """Remove a device from a specific config entry.

    Uses WS `config_entries/remove_device`. Both `device_id` and the owning
    `config_entry_id` are required.
    """
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


@mcp.tool()
def list_devices_in_area(area_id: str) -> list[dict]:
    """List devices currently assigned to a given area_id."""
    devices = ha._ws_call("config/device_registry/list") or []
    return [_slim(d) for d in devices if d.get("area_id") == area_id]
