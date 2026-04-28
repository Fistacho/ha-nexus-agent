from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("areas")


@mcp.tool()
def list_areas() -> list[dict]:
    """List all areas (rooms) in Home Assistant."""
    return ha.get_area_registry()


@mcp.tool()
def create_area(name: str) -> dict:
    """Create a new area."""
    return ha.create_area(name)


@mcp.tool()
def delete_area(area_id: str) -> dict:
    """Delete an area by its area_id."""
    return ha.delete_area(area_id)


@mcp.tool()
def list_floors() -> list[dict]:
    """List all floors."""
    return ha.get_floor_registry()


@mcp.tool()
def list_devices() -> list[dict]:
    """List all devices in the device registry."""
    return ha.get_device_registry()


@mcp.tool()
def get_area_entities(area_id: str) -> list[dict]:
    """Get all entities belonging to an area."""
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


@mcp.tool()
def get_area_states(area_id: str) -> list[dict]:
    """Get current states of all entities in an area."""
    entities = get_area_entities(area_id)
    entity_ids = {e["entity_id"] for e in entities}
    all_states = ha.get_states()
    return [s for s in all_states if s["entity_id"] in entity_ids]


@mcp.tool()
def control_area(area_id: str, action: str, domain: str = "light") -> list[dict]:
    """Turn on/off/toggle all entities of a domain in an area.
    action: 'turn_on' | 'turn_off' | 'toggle'
    """
    return ha.call_service(domain, action, {"area_id": area_id})
