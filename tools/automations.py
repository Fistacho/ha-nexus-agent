from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("automations")


def _strip_prefix(entity_or_id: str, prefix: str) -> str:
    """Accept either 'automation.foo' / 'script.foo' or a bare id. Strip the domain prefix if present."""
    if entity_or_id.startswith(prefix + "."):
        return entity_or_id[len(prefix) + 1:]
    return entity_or_id


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


# --- Automation config CRUD ---

@mcp.tool()
def get_automation_config(automation_id: str) -> dict | None:
    """Get an automation's YAML config as a dict. Accepts bare id or 'automation.<id>'. Returns None if not found."""
    aid = _strip_prefix(automation_id, "automation")
    return ha.get_automation_config(aid)


@mcp.tool()
def set_automation_config(automation_id: str, config: dict) -> dict:
    """Create or overwrite an automation YAML config (alias, trigger, action, condition, ...). Auto-reloads automations."""
    if not isinstance(config, dict):
        return {"error": "config must be a dict"}
    if "alias" not in config and "trigger" not in config and "triggers" not in config:
        return {"error": "config must contain at least 'alias' or 'trigger'/'triggers'"}
    aid = _strip_prefix(automation_id, "automation")
    result = ha.set_automation_config(aid, config)
    ha.call_service("automation", "reload")
    return {"status": "saved", "automation_id": aid, "result": result}


@mcp.tool()
def delete_automation(automation_id: str) -> dict:
    """Delete an automation by id. Auto-reloads automations afterwards."""
    aid = _strip_prefix(automation_id, "automation")
    result = ha.delete_automation_config(aid)
    if result.get("status") == "not_found":
        return {"error": "not found", "automation_id": aid}
    ha.call_service("automation", "reload")
    return {"status": "deleted", "automation_id": aid, "result": result}


# --- Script config CRUD ---

@mcp.tool()
def get_script_config(script_id: str) -> dict | None:
    """Get a script's YAML config as a dict. Accepts bare id or 'script.<id>'. Returns None if not found."""
    sid = _strip_prefix(script_id, "script")
    return ha.get_script_config(sid)


@mcp.tool()
def set_script_config(script_id: str, config: dict) -> dict:
    """Create or overwrite a script YAML config (sequence, fields, ...). Auto-reloads scripts."""
    if not isinstance(config, dict):
        return {"error": "config must be a dict"}
    if "sequence" not in config and "alias" not in config:
        return {"error": "config must contain at least 'alias' or 'sequence'"}
    sid = _strip_prefix(script_id, "script")
    result = ha.set_script_config(sid, config)
    ha.call_service("script", "reload")
    return {"status": "saved", "script_id": sid, "result": result}


@mcp.tool()
def delete_script(script_id: str) -> dict:
    """Delete a script by id. Auto-reloads scripts afterwards."""
    sid = _strip_prefix(script_id, "script")
    result = ha.delete_script_config(sid)
    if result.get("status") == "not_found":
        return {"error": "not found", "script_id": sid}
    ha.call_service("script", "reload")
    return {"status": "deleted", "script_id": sid, "result": result}


# --- Traces (WebSocket) ---

@mcp.tool()
def list_automation_traces(automation_id: str) -> list[dict]:
    """List trace summaries (recent runs) for an automation. Accepts bare id or 'automation.<id>'."""
    aid = _strip_prefix(automation_id, "automation")
    return ha._ws_call("trace/list", domain="automation", item_id=aid)


@mcp.tool()
def get_automation_trace(automation_id: str, run_id: str) -> dict:
    """Get a full automation trace (steps, timing, condition results) for a specific run."""
    aid = _strip_prefix(automation_id, "automation")
    return ha._ws_call("trace/get", domain="automation", item_id=aid, run_id=run_id)


@mcp.tool()
def list_script_traces(script_id: str) -> list[dict]:
    """List trace summaries (recent runs) for a script. Accepts bare id or 'script.<id>'."""
    sid = _strip_prefix(script_id, "script")
    return ha._ws_call("trace/list", domain="script", item_id=sid)


@mcp.tool()
def get_script_trace(script_id: str, run_id: str) -> dict:
    """Get a full script trace (steps, timing, action results) for a specific run."""
    sid = _strip_prefix(script_id, "script")
    return ha._ws_call("trace/get", domain="script", item_id=sid, run_id=run_id)
