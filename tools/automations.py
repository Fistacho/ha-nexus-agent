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


# --- Scene config CRUD ---

@mcp.tool()
def get_scene_config(scene_id: str) -> dict | None:
    """Get a scene's config dict (entities + their target states). Accepts bare id or 'scene.<id>'. Returns None if not found."""
    sid = _strip_prefix(scene_id, "scene")
    with ha._client() as c:
        r = c.get(f"/api/config/scene/config/{sid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


@mcp.tool()
def set_scene_config(scene_id: str, name: str, entities: dict) -> dict:
    """Create or overwrite a scene config. Auto-reloads scenes.

    entities format — keys are entity_ids, values are the target state dict:
      {
        "light.living_room": {"state": "on", "brightness": 76},
        "cover.roleta":      {"state": "closed", "position": 0},
        "switch.fan":        {"state": "off"}
      }

    brightness range is 0-255 (30% ≈ 76, 50% ≈ 128, 100% = 255).
    """
    if not isinstance(entities, dict) or not entities:
        return {"error": "entities must be a non-empty dict of {entity_id: state_dict}"}
    sid = _strip_prefix(scene_id, "scene")
    config = {"id": sid, "name": name, "entities": entities}
    with ha._client() as c:
        r = c.post(f"/api/config/scene/config/{sid}", json=config)
        r.raise_for_status()
        try:
            result = r.json()
        except Exception:
            result = {"status": "ok"}
    ha.call_service("scene", "reload")
    return {"scene_id": sid, "name": name, "entity_count": len(entities), **result}


@mcp.tool()
def delete_scene(scene_id: str, confirm: bool = False) -> dict:
    """Delete a scene config. Requires confirm=True. Auto-reloads scenes."""
    if not confirm:
        sid = _strip_prefix(scene_id, "scene")
        return {"error": "set confirm=True to delete", "command": f'delete_scene("{scene_id}", confirm=True)'}
    sid = _strip_prefix(scene_id, "scene")
    with ha._client() as c:
        r = c.delete(f"/api/config/scene/config/{sid}")
        if r.status_code == 404:
            return {"status": "not_found", "scene_id": sid}
        r.raise_for_status()
        try:
            result = r.json()
        except Exception:
            result = {"status": "ok"}
    ha.call_service("scene", "reload")
    return {"deleted": sid, **result}


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
def delete_automation(automation_id: str, confirm: bool = False) -> dict:
    """Delete an automation by id. Auto-reloads automations afterwards. Set confirm=True to proceed."""
    aid = _strip_prefix(automation_id, "automation")
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will permanently delete automation '{aid}'. Call again with confirm=True.",
            "action": f"delete_automation(automation_id='{automation_id}', confirm=True)",
        }
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
def delete_script(script_id: str, confirm: bool = False) -> dict:
    """Delete a script by id. Auto-reloads scripts afterwards. Set confirm=True to proceed."""
    sid = _strip_prefix(script_id, "script")
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will permanently delete script '{sid}'. Call again with confirm=True.",
            "action": f"delete_script(script_id='{script_id}', confirm=True)",
        }
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


def _resolve_run_id(traces: list[dict], failed_only: bool) -> str | None:
    candidates = traces or []
    if failed_only:
        candidates = [t for t in candidates if t.get("state") == "stopped" and t.get("script_execution") in ("error", "failed", "aborted")]
        if not candidates:
            candidates = [t for t in (traces or []) if t.get("script_execution") and t.get("script_execution") != "finished"]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t.get("timestamp", {}).get("start", ""), reverse=True)
    return candidates[0].get("run_id")


@mcp.tool()
def get_last_automation_trace(automation_id: str, failed_only: bool = False) -> dict:
    """Shortcut: fetch the most recent trace for an automation in one call.

    Use `failed_only=True` to grab the latest run that didn't finish cleanly —
    handy for debugging "why didn't this fire?".
    """
    aid = _strip_prefix(automation_id, "automation")
    traces = ha._ws_call("trace/list", domain="automation", item_id=aid) or []
    run_id = _resolve_run_id(traces, failed_only)
    if not run_id:
        return {"error": "no_traces" if not failed_only else "no_failed_traces", "automation_id": aid}
    return ha._ws_call("trace/get", domain="automation", item_id=aid, run_id=run_id)


@mcp.tool()
def get_last_script_trace(script_id: str, failed_only: bool = False) -> dict:
    """Shortcut: fetch the most recent (or most recent failed) trace for a script."""
    sid = _strip_prefix(script_id, "script")
    traces = ha._ws_call("trace/list", domain="script", item_id=sid) or []
    run_id = _resolve_run_id(traces, failed_only)
    if not run_id:
        return {"error": "no_traces" if not failed_only else "no_failed_traces", "script_id": sid}
    return ha._ws_call("trace/get", domain="script", item_id=sid, run_id=run_id)


# ---------------------------------------------------------------------------
# Best-practice linter
# ---------------------------------------------------------------------------

def _bp_state_trigger_no_for(auto: dict) -> bool:
    triggers = auto.get("trigger") or auto.get("triggers") or []
    if isinstance(triggers, dict):
        triggers = [triggers]
    return any(
        isinstance(t, dict) and (t.get("platform") or t.get("trigger")) == "state" and "for" not in t
        for t in triggers
    )


def _bp_triggers_without_ids(auto: dict) -> bool:
    triggers = auto.get("trigger") or auto.get("triggers") or []
    if isinstance(triggers, dict):
        triggers = [triggers]
    return len(triggers) >= 2 and any("id" not in t for t in triggers if isinstance(t, dict))


def _bp_deprecated_service_key(auto: dict) -> bool:
    def _scan(steps) -> bool:
        if not isinstance(steps, list):
            return False
        for step in steps:
            if not isinstance(step, dict):
                continue
            if "service" in step:
                return True
            for nested in ("sequence", "then", "else", "default", "parallel"):
                if _scan(step.get(nested)):
                    return True
        return False
    actions = auto.get("action") or auto.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    return _scan(actions)


_CHECKS: list[tuple[str, str, object]] = [
    (
        "state_trigger_no_for",
        "warning",
        _bp_state_trigger_no_for,
    ),
    (
        "missing_mode",
        "info",
        lambda a: "mode" not in a,
    ),
    (
        "triggers_without_ids",
        "info",
        _bp_triggers_without_ids,
    ),
    (
        "no_alias",
        "warning",
        lambda a: not a.get("alias") and not a.get("id"),
    ),
    (
        "deprecated_service_key",
        "info",
        _bp_deprecated_service_key,
    ),
    (
        "no_description",
        "info",
        lambda a: not a.get("description"),
    ),
    (
        "restart_mode_caution",
        "info",
        lambda a: a.get("mode") == "restart",
    ),
]

_MESSAGES: dict[str, str] = {
    "state_trigger_no_for": (
        "State trigger without 'for:' duration fires on every transient state change. "
        "Add 'for: \"00:00:02\"' or longer to debounce."
    ),
    "missing_mode": (
        "No 'mode:' declared — defaults to 'single', which silently drops concurrent triggers. "
        "Add 'mode: single|restart|queued|parallel' to make intent explicit."
    ),
    "triggers_without_ids": (
        "Multiple triggers found but some lack 'id:' fields. "
        "Trigger IDs are required for trigger.id conditions and choose/if branches."
    ),
    "no_alias": (
        "Automation has no 'alias:' and no 'id:'. "
        "A descriptive alias makes logs and the UI much easier to read."
    ),
    "deprecated_service_key": (
        "'service:' key used in action step. HA 2024.8+ prefers 'action:' instead. "
        "Both work, but new automations should use 'action:'."
    ),
    "no_description": (
        "No 'description:' field. A short description helps with maintenance."
    ),
    "restart_mode_caution": (
        "Mode 'restart' cancels the running automation on each new trigger. "
        "This can cause unexpected side-effects if actions have already started."
    ),
}


@mcp.tool()
def validate_best_practices(yaml_content: str) -> dict:
    """Validate automation YAML against HA best practices (static linter).

    Accepts a single automation dict or a list of automation dicts.

    Checks:
    - state trigger without 'for:' (bounce risk)
    - missing 'mode:' declaration
    - multiple triggers without 'id:' fields
    - missing 'alias:' / 'id:'
    - deprecated 'service:' key in actions (use 'action:' in HA 2024.8+)
    - 'restart' mode caution
    - missing 'description:'

    Returns: {valid_yaml, automations_checked, issues_total, warnings, infos, issues[], summary}
    Each issue: {automation, rule, severity, message}
    severity: 'warning' = should fix, 'info' = good to know.
    """
    import yaml as _yaml

    try:
        parsed = _yaml.safe_load(yaml_content)
    except _yaml.YAMLError as e:
        return {"valid_yaml": False, "error": f"YAML parse error: {e}", "issues": []}

    if parsed is None:
        return {"valid_yaml": True, "automations_checked": 0, "issues": [], "summary": "✅ Nothing to check"}

    if isinstance(parsed, dict):
        automations = [parsed]
    elif isinstance(parsed, list):
        automations = parsed
    else:
        return {"valid_yaml": True, "error": "Expected a dict or list of automation dicts", "issues": []}

    all_issues: list[dict] = []
    for idx, auto in enumerate(automations):
        if not isinstance(auto, dict):
            continue
        label = auto.get("alias") or auto.get("id") or f"automation[{idx}]"
        for rule, severity, check_fn in _CHECKS:
            try:
                if check_fn(auto):
                    all_issues.append({"automation": label, "rule": rule, "severity": severity, "message": _MESSAGES[rule]})
            except Exception:
                pass

    warnings = sum(1 for i in all_issues if i["severity"] == "warning")
    infos = sum(1 for i in all_issues if i["severity"] == "info")

    return {
        "valid_yaml": True,
        "automations_checked": len(automations),
        "issues_total": len(all_issues),
        "warnings": warnings,
        "infos": infos,
        "issues": all_issues,
        "summary": (
            f"✅ No issues found"
            if not all_issues
            else f"⚠️ {warnings} warning(s), ℹ️ {infos} info(s) across {len(automations)} automation(s)"
        ),
    }


# ---------------------------------------------------------------------------
# Live reference validator
# ---------------------------------------------------------------------------

def _extract_entity_ids(obj: object, found: set) -> None:
    """Recursively walk automation dict and collect literal entity_id values."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "entity_id":
                if isinstance(v, str) and "{{" not in v:
                    found.add(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and "{{" not in item:
                            found.add(item)
            else:
                _extract_entity_ids(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _extract_entity_ids(item, found)


def _extract_services(obj: object, found: set) -> None:
    """Recursively walk automation dict and collect literal service/action values."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("service", "action"):
                if isinstance(v, str) and "{{" not in v and "." in v:
                    found.add(v)
            else:
                _extract_services(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _extract_services(item, found)


@mcp.tool()
def validate_automation_references(yaml_content: str) -> dict:
    """Cross-check entity IDs and services used in automation YAML against the live HA registry.

    Unlike validate_best_practices (static), this tool calls Home Assistant to verify
    that every referenced entity_id exists and every service is registered.
    Template values ({{ ... }}) are skipped — they can't be resolved statically.

    Returns: {
        valid_yaml, entities_checked, services_checked,
        missing_entities[], missing_services[], template_refs_skipped,
        summary
    }
    """
    import yaml as _yaml

    try:
        parsed = _yaml.safe_load(yaml_content)
    except _yaml.YAMLError as e:
        return {"valid_yaml": False, "error": f"YAML parse error: {e}"}

    if parsed is None:
        return {"valid_yaml": True, "summary": "✅ Nothing to check"}

    if isinstance(parsed, dict):
        automations = [parsed]
    elif isinstance(parsed, list):
        automations = parsed
    else:
        return {"valid_yaml": True, "error": "Expected a dict or list of automation dicts"}

    # Extract all literal references
    entity_refs: set = set()
    service_refs: set = set()
    for auto in automations:
        _extract_entity_ids(auto, entity_refs)
        _extract_services(auto, service_refs)

    template_count = 0
    for auto in automations:
        import re
        template_count += len(re.findall(r"\{\{", str(auto)))

    # Fetch live data
    try:
        live_entity_ids = {s["entity_id"] for s in ha.get_states()}
    except Exception:
        live_entity_ids = set()

    try:
        live_services: set = set()
        for entry in ha.list_services():
            domain = entry.get("domain", "")
            for svc in (entry.get("services") or {}).keys():
                live_services.add(f"{domain}.{svc}")
    except Exception:
        live_services = set()

    missing_entities = sorted(entity_refs - live_entity_ids)
    missing_services = sorted(service_refs - live_services) if live_services else []

    total_missing = len(missing_entities) + len(missing_services)
    return {
        "valid_yaml": True,
        "automations_checked": len(automations),
        "entities_checked": len(entity_refs),
        "services_checked": len(service_refs),
        "template_refs_skipped": template_count,
        "missing_entities": missing_entities,
        "missing_services": missing_services,
        "summary": (
            "✅ All references exist in Home Assistant"
            if not total_missing
            else f"❌ {len(missing_entities)} missing entity(s), {len(missing_services)} missing service(s)"
        ),
    }


# ---------------------------------------------------------------------------
# Entity group management (group.set / group.remove)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_groups() -> list[dict]:
    """List all Home Assistant entity groups (group.* entities).

    Returns groups with their members, icon, and current state.
    """
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
            "entity_ids": s.get("attributes", {}).get("entity_id", []),
            "icon": s.get("attributes", {}).get("icon"),
            "all": s.get("attributes", {}).get("all", False),
            "order": s.get("attributes", {}).get("order"),
        }
        for s in states
        if s["entity_id"].startswith("group.")
    ]


@mcp.tool()
def set_group(
    group_id: str,
    name: str | None = None,
    entities: list[str] | None = None,
    icon: str | None = None,
    all_entities: bool = False,
    add_entities: list[str] | None = None,
    remove_entities: list[str] | None = None,
) -> dict:
    """Create or update a Home Assistant entity group.

    Wraps the group.set service. Group is stored in groups.yaml and persists restarts.

    Args:
        group_id: Bare group id (e.g. 'living_room_lights') — no 'group.' prefix.
        name: Friendly name (optional).
        entities: Full list of entity_ids to include (replaces current members).
        icon: MDI icon string (e.g. 'mdi:lightbulb-group').
        all_entities: If True, group state = ON only when ALL members are on.
        add_entities: Add these entity_ids to existing members.
        remove_entities: Remove these entity_ids from existing members.
    """
    gid = _strip_prefix(group_id, "group")
    data: dict = {"object_id": gid}
    if name is not None:
        data["name"] = name
    if entities is not None:
        data["entities"] = entities
    if icon is not None:
        data["icon"] = icon
    if all_entities:
        data["all"] = True
    if add_entities:
        data["add_entities"] = add_entities
    if remove_entities:
        data["remove_entities"] = remove_entities

    ha.call_service("group", "set", data)
    return {"status": "set", "group_id": f"group.{gid}"}


@mcp.tool()
def remove_group(group_id: str, confirm: bool = False) -> dict:
    """Remove a Home Assistant entity group. Set confirm=True to proceed.

    Args:
        group_id: Bare group id or 'group.<id>'.
    """
    gid = _strip_prefix(group_id, "group")
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will permanently remove group 'group.{gid}'. Call again with confirm=True.",
            "action": f"remove_group(group_id='{group_id}', confirm=True)",
        }
    ha.call_service("group", "remove", {"object_id": gid})
    return {"status": "removed", "group_id": f"group.{gid}"}
