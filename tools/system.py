from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("system")


@mcp.tool()
def check_config() -> dict:
    """Validate Home Assistant configuration before restart."""
    with ha._client() as c:
        r = c.get("/api/config/core/check_config")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def restart_ha() -> dict:
    """Restart Home Assistant. WARNING: causes ~30s downtime."""
    return ha.call_service("homeassistant", "restart")


@mcp.tool()
def stop_ha() -> dict:
    """Stop Home Assistant. WARNING: requires manual restart."""
    return ha.call_service("homeassistant", "stop")


@mcp.tool()
def reload_all() -> dict:
    """Reload core configuration (automations, scripts, scenes, groups, etc.)."""
    ha.call_service("homeassistant", "reload_all")
    return {"status": "reload_all triggered"}


@mcp.tool()
def create_backup() -> dict:
    """Create a full Home Assistant backup."""
    return ha.call_service("backup", "create")


@mcp.tool()
def list_integrations() -> list[dict]:
    """List all loaded integrations (config entries)."""
    return ha.get_config_entries()


@mcp.tool()
def reload_integration(entry_id: str) -> list[dict]:
    """Reload a specific integration config entry."""
    return ha.call_service("homeassistant", "reload_config_entry", {"entry_id": entry_id})


@mcp.tool()
def ping_ha() -> dict:
    """Check if Home Assistant is reachable."""
    ok = ha.ping()
    return {"reachable": ok, "url": ha._HA_URL}


@mcp.tool()
def get_all_integrations() -> list[dict]:
    """Get all active config entries (integrations, their state and domain)."""
    entries = ha.get_config_entries()
    return [
        {
            "entry_id": e.get("entry_id"),
            "domain": e.get("domain"),
            "title": e.get("title"),
            "state": e.get("state"),
            "source": e.get("source"),
        }
        for e in entries
    ]
