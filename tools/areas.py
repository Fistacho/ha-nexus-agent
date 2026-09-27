from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("areas")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("List areas"))
def list_areas() -> list[dict]:
    """List all Home Assistant areas (rooms).

    Reads the area registry over WebSocket (`config/area_registry/list`) and
    returns every entry exactly as Home Assistant reports it, unfiltered.

    Use when: you need area IDs and names before assigning entities, devices
    or labels to an area.
    Returns: list of area registry dicts (area_id, name, floor_id, icon,
    aliases, picture)."""
    return ha.get_area_registry()


@mcp.tool(annotations=write("Create area", idempotent=False))
def create_area(
    name: Annotated[str, Field(description="Display name for the new area, e.g. 'Kitchen'.")],
) -> dict:
    """Create a new Home Assistant area (room).

    Calls WS `config/area_registry/create` with `name`; Home Assistant
    assigns a new area_id and there is no way to request a specific one, so
    calling this twice with the same name creates two distinct areas.

    Use when: you need a new area to assign devices, entities or labels to.
    Not for: renaming or moving an existing area — no update tool exists
    for areas in this namespace.
    Returns: the created area registry dict (area_id, name, floor_id, icon,
    aliases, picture)."""
    return ha.create_area(name)


@mcp.tool(annotations=destructive("Delete area", idempotent=True))
def delete_area(
    area_id: Annotated[str, Field(description="Area registry ID to delete; obtain it from areas_list_areas.")],
) -> dict:
    """Delete a Home Assistant area by its area_id.

    Calls WS `config/area_registry/delete` and returns Home Assistant's
    result unchanged; the area disappears from the registry immediately.

    Use when: removing an area that is no longer used.
    Returns: the raw WS deletion result.
    Limits: irreversible — there is no restore tool for a deleted area."""
    return ha.delete_area(area_id)


@mcp.tool(annotations=read("List floors"))
def list_floors() -> list[dict]:
    """List all floors defined in Home Assistant.

    Reads the floor registry over WebSocket (`config/floor_registry/list`)
    and returns every entry exactly as Home Assistant reports it, unfiltered.

    Use when: grouping areas by floor, e.g. before building a floor-scoped
    dashboard.
    Returns: list of floor registry dicts as reported by Home Assistant."""
    return ha.get_floor_registry()


@mcp.tool(annotations=read("List all devices unfiltered"))
def list_devices() -> list[dict]:
    """List all devices in Home Assistant's device registry, unfiltered.

    Calls WS `config/device_registry/list` via ha_client and returns every
    registry entry exactly as Home Assistant reports it, with every field
    (unlike the slimmed projection other tools apply).

    Use when: you need the complete raw device registry, including fields
    not exposed elsewhere.
    Not for: a smaller per-field view — use `devices_list_devices`, which
    projects each entry down to id/name/area_id/manufacturer/model/
    entry_type plus config_entries/disabled_by; devices scoped to one area —
    use `devices_list_devices_in_area`; fuzzy name-based search — use
    `search_search_devices`.
    Returns: list of full device registry dicts as returned by
    config/device_registry/list."""
    return ha.get_device_registry()


@mcp.tool(annotations=read("Get entities in area"))
def get_area_entities(
    area_id: Annotated[str, Field(description="Area registry ID whose entities to return; obtain it from areas_list_areas.")],
) -> list[dict]:
    """Get every entity registered in a given area, directly or via its device.

    Reads the entity and device registries over WebSocket, then keeps
    entity-registry rows whose own `area_id` matches, or whose `device_id`
    belongs to a device assigned to the area.

    Use when: you need the entity registry rows for one area, e.g. before
    bulk-editing labels or categories.
    Not for: current state values — use `areas_get_area_states`, or a
    combined view — use `snapshot_get_area_snapshot`.
    Returns: list of entity registry dicts (entity_id, device_id, area_id,
    labels, …) belonging to the area.
    Limits: an unknown area_id returns an empty list rather than an error."""
    entity_registry = ha.get_entity_registry()
    device_registry = ha.get_device_registry()

    device_ids_in_area = {
        d["id"] for d in device_registry if d.get("area_id") == area_id
    }

    return [
        e for e in entity_registry
        if e.get("area_id") == area_id
        or e.get("device_id") in device_ids_in_area
    ]


@mcp.tool(annotations=read("Get entity states in area"))
def get_area_states(
    area_id: Annotated[str, Field(description="Area registry ID whose entity states to return; obtain it from areas_list_areas.")],
) -> list[dict]:
    """Get current states of every entity in a given area.

    Calls `areas_get_area_entities` for the area, then filters the full
    state list (`ha.get_states()`) down to the entity_ids found in that call.

    Use when: you need live state values scoped to one area, e.g. before
    deciding what to control.
    Not for: registry rows without state values — use
    `areas_get_area_entities`, or a combined view — use
    `snapshot_get_area_snapshot`.
    Returns: list of HA state objects (entity_id, state, attributes,
    last_changed, last_updated) for entities in the area."""
    entities = get_area_entities(area_id)
    entity_ids = {e["entity_id"] for e in entities}
    all_states = ha.get_states()
    return [s for s in all_states if s["entity_id"] in entity_ids]


@mcp.tool(annotations=write("Control area entities by domain", idempotent=False))
def control_area(
    area_id: Annotated[str, Field(description="Area registry ID to target; obtain it from areas_list_areas.")],
    action: Annotated[str, Field(description="Service to call on the domain: 'turn_on', 'turn_off' or 'toggle'.")],
    domain: Annotated[str, Field(description="Entity domain to target within the area, e.g. 'light' or 'switch'; defaults to 'light'.")] = "light",
) -> list[dict]:
    """Turn on, off or toggle every entity of one domain in an area.

    Calls service `<domain>.<action>` over HTTP with `{"area_id": area_id}`
    as the target, so Home Assistant dispatches it to every entity of that
    domain in the area.

    Use when: controlling a whole area by domain, e.g. all lights in a room.
    Not for: controlling an explicit list of entity_ids across areas — use
    `entities_bulk_control`.
    Returns: list of state-change dicts as returned by the HA service call.
    Limits: `action` is passed through unchecked; an invalid action or
    domain combination fails with the underlying HTTP error from Home
    Assistant."""
    return ha.call_service(domain, action, {"area_id": area_id})
