from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("system")


@mcp.tool()
def check_config() -> dict:
    """Validate Home Assistant configuration before restart."""
    with ha._client() as c:
        r = c.post("/api/config/core/check_config")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def restart_ha(confirm: bool = False) -> dict:
    """Restart Home Assistant. WARNING: causes ~30s downtime.

    Set confirm=True to proceed; without it returns a safety prompt.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": "This will restart Home Assistant (~30s downtime). Call again with confirm=True to proceed.",
            "action": "restart_ha(confirm=True)",
        }
    return ha.call_service("homeassistant", "restart")


@mcp.tool()
def stop_ha(confirm: bool = False) -> dict:
    """Stop Home Assistant. WARNING: requires manual restart.

    Set confirm=True to proceed; without it returns a safety prompt.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": "This will STOP Home Assistant and require a manual restart. Call again with confirm=True to proceed.",
            "action": "stop_ha(confirm=True)",
        }
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


@mcp.tool()
def get_system_health() -> dict:
    """Get Home Assistant system health status.

    Returns health checks for core HA subsystems (recorder, websocket, cloud,
    network, etc.) plus basic system info (version, dev mode, virtual env).
    """
    with ha._client() as c:
        r = c.get("/api/system_health")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_repairs() -> list[dict]:
    """List active Home Assistant repair issues.

    Returns current repair issues that require attention (deprecated config,
    broken integrations, required migrations, etc.).
    """
    raw = ha._ws_call("repairs/list")
    issues = raw if isinstance(raw, list) else raw.get("issues", [])
    return [
        {
            "issue_id": i.get("issue_id"),
            "domain": i.get("domain"),
            "severity": i.get("severity"),
            "breaks_in_ha_version": i.get("breaks_in_ha_version"),
            "learn_more_url": i.get("learn_more_url"),
            "translation_key": i.get("translation_key"),
            "ignored": i.get("ignored", False),
            "is_fixable": i.get("is_fixable", False),
        }
        for i in issues
        if isinstance(i, dict)
    ]


@mcp.tool()
def get_updates(pending_only: bool = True) -> list[dict]:
    """List available Home Assistant updates (core, add-ons, HACS, custom components).

    Reads update.* entities from HA states — each represents one updatable component.

    Args:
        pending_only: If True (default), return only components with updates available.
                      Set False to see all update entities including up-to-date ones.
    """
    states = ha.get_states()
    updates = [s for s in states if s["entity_id"].startswith("update.")]

    result = []
    for s in updates:
        attrs = s.get("attributes", {})
        installed = attrs.get("installed_version")
        latest = attrs.get("latest_version")
        has_update = s.get("state") == "on"

        if pending_only and not has_update:
            continue

        result.append({
            "entity_id": s["entity_id"],
            "name": attrs.get("friendly_name", s["entity_id"]),
            "installed_version": installed,
            "latest_version": latest,
            "update_available": has_update,
            "release_url": attrs.get("release_url"),
            "release_notes": attrs.get("release_notes"),
            "skipped_version": attrs.get("skipped_version"),
            "in_progress": attrs.get("in_progress", False),
            "auto_update": attrs.get("auto_update", False),
        })

    result.sort(key=lambda x: (not x["update_available"], x["name"]))
    return result
