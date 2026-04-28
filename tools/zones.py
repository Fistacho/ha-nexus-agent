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
    """Create a zone via WS `config/zone/create` (zone helper integration)."""
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
        result = ha._ws_call("config/zone/create", **payload)
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
    """Update a zone via WS `config/zone/update`; only the fields you pass are sent."""
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
        result = ha._ws_call("config/zone/update", **payload)
        return {"status": "updated", "zone_id": zone_id, "result": result}
    except Exception as e:
        return {
            "error": str(e),
            "hint": "Only zones created via UI helpers can be updated through WS.",
            "zone_id": zone_id,
        }


@mcp.tool()
def delete_zone(zone_id: str) -> dict:
    """Delete a zone via WS `config/zone/delete` (accepts zone helper id or `zone.<id>`)."""
    zid = zone_id.split(".", 1)[1] if zone_id.startswith("zone.") else zone_id
    try:
        result = ha._ws_call("config/zone/delete", zone_id=zid)
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
    """List `person.*` entities currently inside the given zone (matched on friendly_name → state)."""
    if not zone_entity_id.startswith("zone."):
        zone_entity_id = f"zone.{zone_entity_id}"
    states = ha.get_states()
    zone = next((s for s in states if s["entity_id"] == zone_entity_id), None)
    if zone is None:
        return []
    zone_name = (zone.get("attributes") or {}).get("friendly_name") or zone_entity_id.split(".", 1)[1]
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": (s.get("attributes") or {}).get("friendly_name"),
            "latitude": (s.get("attributes") or {}).get("latitude"),
            "longitude": (s.get("attributes") or {}).get("longitude"),
        }
        for s in states
        if s["entity_id"].startswith("person.") and s.get("state") == zone_name
    ]


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
