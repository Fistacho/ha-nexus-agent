from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("automations")


@mcp.tool()
def list_automations() -> list[dict]:
    """List all automations with their current state."""
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
            "last_triggered": s.get("attributes", {}).get("last_triggered"),
        }
        for s in states
        if s["entity_id"].startswith("automation.")
    ]


@mcp.tool()
def trigger_automation(entity_id: str) -> list[dict]:
    """Manually trigger an automation."""
    return ha.call_service("automation", "trigger", {"entity_id": entity_id})


@mcp.tool()
def enable_automation(entity_id: str) -> list[dict]:
    """Enable a disabled automation."""
    return ha.call_service("automation", "turn_on", {"entity_id": entity_id})


@mcp.tool()
def disable_automation(entity_id: str) -> list[dict]:
    """Disable an automation."""
    return ha.call_service("automation", "turn_off", {"entity_id": entity_id})


@mcp.tool()
def reload_automations() -> list[dict]:
    """Reload all automations from YAML."""
    return ha.call_service("automation", "reload")


@mcp.tool()
def list_scripts() -> list[dict]:
    """List all scripts."""
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
            "last_triggered": s.get("attributes", {}).get("last_triggered"),
        }
        for s in states
        if s["entity_id"].startswith("script.")
    ]


@mcp.tool()
def run_script(entity_id: str, variables: dict | None = None) -> list[dict]:
    """Run a script, optionally with variables."""
    data: dict = {"entity_id": entity_id}
    if variables:
        data["variables"] = variables
    return ha.call_service("script", "turn_on", data)


@mcp.tool()
def reload_scripts() -> list[dict]:
    """Reload all scripts from YAML."""
    return ha.call_service("script", "reload")


@mcp.tool()
def list_scenes() -> list[dict]:
    """List all scenes."""
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
        }
        for s in states
        if s["entity_id"].startswith("scene.")
    ]


@mcp.tool()
def activate_scene(entity_id: str) -> list[dict]:
    """Activate a scene."""
    return ha.call_service("scene", "turn_on", {"entity_id": entity_id})


@mcp.tool()
def reload_scenes() -> list[dict]:
    """Reload scenes from YAML."""
    return ha.call_service("scene", "reload")
