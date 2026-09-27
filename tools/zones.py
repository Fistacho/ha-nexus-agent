from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("zones")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


_ZONE_ATTR_FIELDS = ("latitude", "longitude", "radius", "friendly_name", "passive", "icon")


def _slim_zone(state: dict) -> dict:
    """Project a zone state down to the common fields the LLM cares about."""
    attrs = state.get("attributes") or {}
    out = {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
    }
    for k in _ZONE_ATTR_FIELDS:
        if k in attrs:
            out[k] = attrs[k]
    return out


@mcp.tool(annotations=read("List zones"))
def list_zones() -> list[dict]:
    """List all zone entities with their common location attributes.

    Reads all HA states and keeps `zone.*` entities, projecting each down
    to entity_id, state, and any of latitude, longitude, radius,
    friendly_name, passive and icon present in its attributes.

    Use when: you need every zone's location and radius, e.g. before
    checking who is inside one.
    Returns: list of slimmed zone dicts as described above."""
    return [_slim_zone(s) for s in ha.get_states() if s["entity_id"].startswith("zone.")]


@mcp.tool(annotations=read("Get zone"))
def get_zone(
    entity_id: Annotated[str, Field(description="Zone entity_id, e.g. 'zone.office'; a bare id like 'office' is prefixed with 'zone.' automatically.")],
) -> dict:
    """Get one zone entity's full state and attributes.

    Prefixes `entity_id` with `zone.` if missing, then fetches it with
    `ha.get_state`.

    Use when: you need every attribute of a single zone rather than the
    slimmed list from `zones_list_zones`.
    Returns: the full HA state object for the zone (entity_id, state,
    attributes).
    Errors: `{"error": "<message>", "entity_id": ...}` if the zone entity
    does not exist."""
    if not entity_id.startswith("zone."):
        entity_id = f"zone.{entity_id}"
    try:
        return ha.get_state(entity_id)
    except Exception as e:
        return {"error": str(e), "entity_id": entity_id}


@mcp.tool(annotations=write("Create zone helper", idempotent=False))
def create_zone(
    name: Annotated[str, Field(description="Display name for the new zone, e.g. 'Office'.")],
    latitude: Annotated[float, Field(description="Latitude in decimal degrees, e.g. 52.23.")],
    longitude: Annotated[float, Field(description="Longitude in decimal degrees, e.g. 21.01.")],
    radius: Annotated[float, Field(description="Zone radius in meters; defaults to 100.")] = 100,
    icon: Annotated[str | None, Field(description="Optional mdi icon identifier, e.g. 'mdi:office-building'; omit for none.")] = None,
    passive: Annotated[bool, Field(description="If true, excludes the zone from device-tracker zone suggestions; defaults to false.")] = False,
) -> dict:
    """Create a zone as a UI helper (storage collection), not a YAML zone.

    Calls WS `zone/create` with the given coordinates and radius; only
    zones created this way can later be changed through
    `zones_update_zone` or `zones_delete_zone`. `zone.home` (core config)
    and YAML-defined zones live outside this storage collection and cannot
    be created here.

    Use when: adding a new geofence zone without editing configuration.yaml.
    Returns: dict with `status`, `name`, and the raw WS `result`.
    Errors: on failure, returns `{"error": "<message>", "hint": "...",
    "name": ...}` instead of raising."""
    payload: dict = {
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "radius": radius,
        "passive": passive,
    }
    if icon:
        payload["icon"] = icon
    try:
        result = ha._ws_call("zone/create", **payload)
        return {"status": "created", "name": name, "result": result}
    except Exception as e:
        return {
            "error": str(e),
            "hint": "Zones in YAML are not editable via WS; use Helpers UI or configuration.yaml + reload_zones().",
            "name": name,
        }


@mcp.tool(annotations=destructive("Update zone helper", idempotent=False))
def update_zone(
    entity_id: Annotated[str, Field(description="Zone entity_id to update, e.g. 'zone.office'; obtain it from zones_list_zones.")],
    latitude: Annotated[float | None, Field(description="New latitude in decimal degrees; omit to leave the current latitude unchanged.")] = None,
    longitude: Annotated[float | None, Field(description="New longitude in decimal degrees; omit to leave the current longitude unchanged.")] = None,
    radius: Annotated[float | None, Field(description="New zone radius in meters; omit to leave the current radius unchanged.")] = None,
    name: Annotated[str | None, Field(description="New display name; omit to leave the current name unchanged.")] = None,
    icon: Annotated[str | None, Field(description="New mdi icon identifier; omit to leave the current icon unchanged.")] = None,
    passive: Annotated[bool | None, Field(description="New passive flag; omit to leave the current setting unchanged.")] = None,
) -> dict:
    """Update fields on an existing zone helper.

    Calls WS `zone/update`; only the parameters you pass are sent, so
    omitted fields keep their current value. Only works for zones created
    via `zones_create_zone` (UI helpers) — `zone.home` and YAML-defined
    zones reject this WS command.

    Use when: adjusting a UI-helper zone's coordinates, radius or name.
    Returns: dict with `status`, `zone_id`, and the raw WS `result`.
    Errors: on failure (e.g. a YAML zone or `zone.home`), returns
    `{"error": "<message>", "hint": "...", "zone_id": ...}` instead of
    raising."""
    zone_id = entity_id.split(".", 1)[1] if entity_id.startswith("zone.") else entity_id
    payload: dict = {"zone_id": zone_id}
    if latitude is not None:
        payload["latitude"] = latitude
    if longitude is not None:
        payload["longitude"] = longitude
    if radius is not None:
        payload["radius"] = radius
    if name is not None:
        payload["name"] = name
    if icon is not None:
        payload["icon"] = icon
    if passive is not None:
        payload["passive"] = passive
    try:
        result = ha._ws_call("zone/update", **payload)
        return {"status": "updated", "zone_id": zone_id, "result": result}
    except Exception as e:
        return {
            "error": str(e),
            "hint": "Only zones created via UI helpers can be updated through WS.",
            "zone_id": zone_id,
        }


@mcp.tool(annotations=destructive("Delete zone helper", idempotent=True))
def delete_zone(
    zone_id: Annotated[str, Field(description="Zone helper id or full entity_id like 'zone.office'; obtain it from zones_list_zones.")],
) -> dict:
    """Delete a zone helper.

    Calls WS `zone/delete`; accepts either the bare helper id or the full
    `zone.<id>` entity_id. Only works for zones created via
    `zones_create_zone` — `zone.home` and YAML-defined zones reject this WS
    command.

    Use when: removing a UI-helper zone that is no longer needed.
    Returns: dict with `status`, `zone_id`, and the raw WS `result`.
    Errors: on failure, returns `{"error": "<message>", "hint": "...",
    "zone_id": ...}` instead of raising."""
    zid = zone_id.split(".", 1)[1] if zone_id.startswith("zone.") else zone_id
    try:
        result = ha._ws_call("zone/delete", zone_id=zid)
        return {"status": "deleted", "zone_id": zid, "result": result}
    except Exception as e:
        return {
            "error": str(e),
            "hint": "Only zones created via UI helpers can be deleted through WS.",
            "zone_id": zid,
        }


@mcp.tool(annotations=write("Reload zones from YAML", idempotent=True))
def reload_zones() -> dict:
    """Reload zones defined in configuration.yaml.

    Calls service `zone.reload` over HTTP, which makes Home Assistant
    re-read YAML-defined zones without a full restart.

    Use when: you edited zone YAML and want the change applied, or after
    `zones_create_zone` reports that YAML zones aren't supported.
    Returns: dict with `status` and the raw service-call `result`.
    Errors: on failure, returns `{"error": "<message>"}` instead of
    raising."""
    try:
        result = ha.call_service("zone", "reload")
        return {"status": "reloaded", "result": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=read("List persons in zone"))
def list_persons_in_zone(
    zone_entity_id: Annotated[str, Field(description="Zone entity_id, e.g. 'zone.home'; a bare id like 'home' is prefixed with 'zone.' automatically.")],
) -> list[dict]:
    """List person entities currently inside a given zone.

    Reads the zone's own `persons` attribute (maintained by Home
    Assistant's zone component); falls back to matching each person's
    state string against the zone only when that attribute is entirely
    absent, which also special-cases `zone.home` (Home Assistant always
    reports `"home"` there regardless of the zone's friendly_name).

    Use when: checking who is currently inside a specific zone.
    Returns: list of dicts (entity_id, state, friendly_name, latitude,
    longitude) for persons in the zone; empty list if the zone does not
    exist."""
    if not zone_entity_id.startswith("zone."):
        zone_entity_id = f"zone.{zone_entity_id}"
    states = ha.get_states()
    zone = next((s for s in states if s["entity_id"] == zone_entity_id), None)
    if zone is None:
        return []
    zone_attrs = zone.get("attributes") or {}
    person_ids = zone_attrs.get("persons")

    if person_ids is None:
        # Defensive fallback: reproduce HA's own state string for this zone
        # (device_tracker/legacy.py: STATE_HOME for zone.home, else the
        # zone's `name` i.e. friendly_name or slug-with-spaces).
        if zone_entity_id == "zone.home":
            zone_state_value = "home"
        else:
            zone_state_value = zone_attrs.get("friendly_name") or zone_entity_id.split(".", 1)[1].replace("_", " ")
        person_ids = [
            s["entity_id"]
            for s in states
            if s["entity_id"].startswith("person.") and s.get("state") == zone_state_value
        ]

    by_id = {s["entity_id"]: s for s in states}
    result = []
    for pid in person_ids:
        s = by_id.get(pid)
        if s is None:
            continue
        attrs = s.get("attributes") or {}
        result.append(
            {
                "entity_id": s["entity_id"],
                "state": s.get("state"),
                "friendly_name": attrs.get("friendly_name"),
                "latitude": attrs.get("latitude"),
                "longitude": attrs.get("longitude"),
            }
        )
    return result


@mcp.tool(annotations=read("Get person location"))
def get_user_location(
    person_entity_id: Annotated[str, Field(description="Person entity_id, e.g. 'person.alice'; a bare id like 'alice' is prefixed with 'person.' automatically.")],
) -> dict:
    """Get one person entity's current location.

    Prefixes `person_entity_id` with `person.` if missing, then reads its
    state and location attributes.

    Use when: you need just one person's coordinates rather than scanning
    a zone with `zones_list_persons_in_zone`.
    Returns: dict with entity_id, state, latitude, longitude, gps_accuracy,
    source.
    Errors: `{"error": "<message>", "entity_id": ...}` if the person entity
    does not exist."""
    if not person_entity_id.startswith("person."):
        person_entity_id = f"person.{person_entity_id}"
    try:
        s = ha.get_state(person_entity_id)
    except Exception as e:
        return {"error": str(e), "entity_id": person_entity_id}
    attrs = s.get("attributes") or {}
    return {
        "entity_id": s.get("entity_id"),
        "state": s.get("state"),
        "latitude": attrs.get("latitude"),
        "longitude": attrs.get("longitude"),
        "gps_accuracy": attrs.get("gps_accuracy"),
        "source": attrs.get("source"),
    }
