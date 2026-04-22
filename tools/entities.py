from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("entities")


@mcp.tool()
def list_entities(domain: str | None = None) -> list[dict]:
    """List all entities, optionally filtered by domain (light, switch, sensor…)."""
    states = ha.get_states()
    if domain:
        states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
        }
        for s in states
    ]


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """Get full state and attributes of a single entity."""
    return ha.get_state(entity_id)


@mcp.tool()
def turn_on(entity_id: str, brightness: int | None = None, color_temp: int | None = None, rgb_color: list[int] | None = None) -> list[dict]:
    """Turn on an entity. Optional: brightness (0-255), color_temp (mireds), rgb_color ([r,g,b])."""
    domain = entity_id.split(".")[0]
    data: dict = {"entity_id": entity_id}
    if brightness is not None:
        data["brightness"] = brightness
    if color_temp is not None:
        data["color_temp"] = color_temp
    if rgb_color is not None:
        data["rgb_color"] = rgb_color
    return ha.call_service(domain, "turn_on", data)


@mcp.tool()
def turn_off(entity_id: str) -> list[dict]:
    """Turn off an entity."""
    domain = entity_id.split(".")[0]
    return ha.call_service(domain, "turn_off", {"entity_id": entity_id})


@mcp.tool()
def toggle(entity_id: str) -> list[dict]:
    """Toggle an entity between on and off."""
    domain = entity_id.split(".")[0]
    return ha.call_service(domain, "toggle", {"entity_id": entity_id})


@mcp.tool()
def set_value(entity_id: str, value: float) -> list[dict]:
    """Set value of input_number, number, or climate temperature."""
    domain = entity_id.split(".")[0]
    if domain == "input_number":
        return ha.call_service("input_number", "set_value", {"entity_id": entity_id, "value": value})
    if domain == "number":
        return ha.call_service("number", "set_value", {"entity_id": entity_id, "value": value})
    if domain == "climate":
        return ha.call_service("climate", "set_temperature", {"entity_id": entity_id, "temperature": value})
    raise ValueError(f"set_value not supported for domain: {domain}")


@mcp.tool()
def select_option(entity_id: str, option: str) -> list[dict]:
    """Select option for input_select or select entity."""
    domain = entity_id.split(".")[0]
    if domain == "input_select":
        return ha.call_service("input_select", "select_option", {"entity_id": entity_id, "option": option})
    return ha.call_service("select", "select_option", {"entity_id": entity_id, "option": option})


@mcp.tool()
def update_entity_name(entity_id: str, name: str) -> dict:
    """Rename an entity in the entity registry."""
    return ha.update_entity_registry(entity_id, name=name)


@mcp.tool()
def assign_entity_to_area(entity_id: str, area_id: str) -> dict:
    """Assign an entity to an area."""
    return ha.update_entity_registry(entity_id, area_id=area_id)


@mcp.tool()
def disable_entity(entity_id: str) -> dict:
    """Disable an entity in the registry."""
    return ha.update_entity_registry(entity_id, disabled_by="user")


@mcp.tool()
def enable_entity(entity_id: str) -> dict:
    """Re-enable a disabled entity."""
    return ha.update_entity_registry(entity_id, disabled_by=None)


@mcp.tool()
def list_entity_registry(domain: str | None = None) -> list[dict]:
    """Get raw entity registry (includes disabled, hidden entities)."""
    entries = ha.get_entity_registry()
    if domain:
        entries = [e for e in entries if e.get("entity_id", "").startswith(f"{domain}.")]
    return entries
