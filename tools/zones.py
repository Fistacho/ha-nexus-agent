from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("zones")


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


@mcp.tool()
def list_zones() -> list[dict]:
    """List all `zone.*` entities with latitude/longitude/radius/friendly_name/passive."""
    return [_slim_zone(s) for s in ha.get_states() if s["entity_id"].startswith("zone.")]


@mcp.tool()
def get_zone(entity_id: str) -> dict:
    """Get a single zone entity (full state + attributes)."""
    if not entity_id.startswith("zone."):
        entity_id = f"zone.{entity_id}"
    try:
        return ha.get_state(entity_id)
    except Exception as e:
        return {"error": str(e), "entity_id": entity_id}


@mcp.tool()
def create_zone(
    name: str,
    latitude: float,
    longitude: float,
    radius: float = 100,
    icon: str | None = None,
    passive: bool = False,
) -> dict:
    """Create a zone via WS `zone/create` (the zone storage/helper collection).

    Only creates a UI-helper zone; `zone.home` (core config) and YAML-defined
    zones live outside this storage collection and cannot be created this way.
    """
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


@mcp.tool()
def update_zone(
    entity_id: str,
    latitude: float | None = None,
    longitude: float | None = None,
    radius: float | None = None,
    name: str | None = None,
    icon: str | None = None,
    passive: bool | None = None,
) -> dict:
    """Update a zone via WS `zone/update`; only the fields you pass are sent.

    Only works for zones created as UI helpers (storage collection). `zone.home`
    and YAML-defined zones are not editable through this WS command and return
    an error — edit `configuration.yaml` (or Settings → Zones for `zone.home`)
    and call `reload_zones()` instead.
    """
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


@mcp.tool()
def delete_zone(zone_id: str) -> dict:
    """Delete a zone via WS `zone/delete` (accepts zone helper id or `zone.<id>`).

    Only works for zones created as UI helpers. `zone.home` and YAML-defined
    zones are not editable through this WS command and return an error.
    """
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


@mcp.tool()
def reload_zones() -> dict:
    """Reload zones from YAML via service `zone.reload`."""
    try:
        result = ha.call_service("zone", "reload")
        return {"status": "reloaded", "result": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_persons_in_zone(zone_entity_id: str) -> list[dict]:
    """List `person.*` entities currently inside the given zone.

    Reads the zone entity's own `persons` attribute — the list HA maintains by
    tracking each person's `in_zones` attribute (`components/zone`) — rather
    than matching state strings. This also handles `zone.home` correctly: HA
    hardcodes a person's state to `"home"` for the home zone regardless of its
    `friendly_name` (e.g. "Dom"), so naive friendly_name matching always
    returned an empty list for home. Falls back to state-string matching only
    if a zone state has no `persons` attribute at all (non-standard platform).
    """
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


@mcp.tool()
def get_user_location(person_entity_id: str) -> dict:
    """Return {latitude, longitude, gps_accuracy, source, state} for a `person.*` entity."""
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
