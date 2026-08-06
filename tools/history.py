import httpx
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("history")


@mcp.tool()
def get_state_history(entity_id: str, hours: int = 24) -> list:
    """Get state history for an entity over the last N hours."""
    return ha.get_history(entity_id=entity_id, hours=hours)


@mcp.tool()
def get_logbook(entity_id: str | None = None, hours: int = 24) -> list:
    """Get logbook entries, optionally filtered by entity_id."""
    return ha.get_logbook(entity_id=entity_id, hours=hours)


@mcp.tool()
def get_error_log() -> str:
    """Fetch the Home Assistant error log.

    Prefers the raw log file via /api/error_log. That view only exists when HA
    logs to a file — Supervisor installs ship with `duplicate_log_file: false`,
    so it 404s there. In that case the WARNING/ERROR records held by the
    `system_log` integration are rendered instead.
    """
    import ha_client as hac
    try:
        with hac._client() as c:
            r = c.get("/api/error_log")
            r.raise_for_status()
            return r.text
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    return hac.format_system_log_entries(hac._ws_call("system_log/list"))


@mcp.tool()
def get_system_info() -> dict:
    """Get Home Assistant system/version info."""
    import ha_client as hac
    with hac._client() as c:
        r = c.get("/api/")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_ha_config() -> dict:
    """Get current Home Assistant configuration (location, units, version, components)."""
    return ha.get_config()
