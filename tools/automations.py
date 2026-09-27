from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("automations")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


def _strip_prefix(entity_or_id: str, prefix: str) -> str:
    """Accept either 'automation.foo' / 'script.foo' or a bare id. Strip the domain prefix if present."""
    if entity_or_id.startswith(prefix + "."):
        return entity_or_id[len(prefix) + 1:]
    return entity_or_id


@mcp.tool(annotations=read("List automations"))
def list_automations() -> list[dict]:
    """List all automations with their current state.

    Reads `ha.get_states()` and keeps entries whose `entity_id` starts with
    'automation.'.

    Returns: list[dict] `{"entity_id", "state", "friendly_name",
    "last_triggered"}` per automation.
    """
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


@mcp.tool(annotations=write("Trigger an automation now", idempotent=False))
def trigger_automation(
    entity_id: Annotated[
        str,
        Field(description="Full automation entity ID to run now, e.g. 'automation.morning_routine'."),
    ],
) -> list[dict]:
    """Run an automation's actions now, skipping its normal trigger.

    Calls the `automation.trigger` service with `entity_id`; Home Assistant
    skips the automation's conditions by default (`skip_condition=true`),
    so this can fire actions the automation's own conditions would
    otherwise block.

    Use when: testing or forcing an automation's actions on demand.
    Returns: list[dict], the raw `automation.trigger` service-call
    response from Home Assistant.
    """
    return ha.call_service("automation", "trigger", {"entity_id": entity_id})


@mcp.tool(annotations=write("Enable a disabled automation", idempotent=True))
def enable_automation(
    entity_id: Annotated[
        str,
        Field(description="Full automation entity ID to enable, e.g. 'automation.morning_routine'."),
    ],
) -> list[dict]:
    """Enable a disabled automation.

    Calls the `automation.turn_on` service with `entity_id`.

    Not for: running the automation's actions immediately — use
    `automations_trigger_automation`.
    Returns: list[dict], the raw `automation.turn_on` service-call response.
    """
    return ha.call_service("automation", "turn_on", {"entity_id": entity_id})


@mcp.tool(annotations=write("Disable an automation", idempotent=True))
def disable_automation(
    entity_id: Annotated[
        str,
        Field(description="Full automation entity ID to disable, e.g. 'automation.morning_routine'."),
    ],
) -> list[dict]:
    """Disable an automation so it stops reacting to its triggers.

    Calls the `automation.turn_off` service with `entity_id`; the
    automation's stored config is untouched, only its runtime state
    changes.

    Returns: list[dict], the raw `automation.turn_off` service-call
    response.
    """
    return ha.call_service("automation", "turn_off", {"entity_id": entity_id})


@mcp.tool(annotations=write("Reload all automations from YAML", idempotent=True))
def reload_automations() -> list[dict]:
    """Reload all automations from YAML.

    Calls the `automation.reload` service, which re-reads
    automations.yaml/packages and re-applies enabled/disabled state; any
    automations created only via `automations_set_automation_config` since
    the last reload are included too, since that tool writes through the
    same config store.

    Returns: list[dict], the raw `automation.reload` service-call
    response.
    """
    return ha.call_service("automation", "reload")


@mcp.tool(annotations=read("List scripts"))
def list_scripts() -> list[dict]:
    """List all scripts with their current state.

    Reads `ha.get_states()` and keeps entries whose `entity_id` starts with
    'script.'.

    Returns: list[dict] `{"entity_id", "state", "friendly_name",
    "last_triggered"}` per script.
    """
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


@mcp.tool(annotations=write("Start a script", idempotent=False))
def run_script(
    entity_id: Annotated[
        str,
        Field(description="Full script entity ID to start, e.g. 'script.goodnight'."),
    ],
    variables: Annotated[
        dict | None,
        Field(
            description=(
                "Values passed as the script's input fields. Omit to run "
                "with no variables."
            )
        ),
    ] = None,
) -> list[dict]:
    """Start a script and return immediately without waiting for it to finish.

    Calls the `script.turn_on` service with `entity_id` and, when given,
    `variables` as the script's fields; the call returns as soon as Home
    Assistant accepts it and does not return the script's response
    variables.

    Not for: getting the script's result — this tool never returns it,
    regardless of the script's own `response_variable`.
    Returns: list[dict], the raw `script.turn_on` service-call response.
    """
    data: dict = {"entity_id": entity_id}
    if variables:
        data["variables"] = variables
    return ha.call_service("script", "turn_on", data)


@mcp.tool(annotations=write("Reload all scripts from YAML", idempotent=True))
def reload_scripts() -> list[dict]:
    """Reload all scripts from YAML.

    Calls the `script.reload` service, which re-reads scripts.yaml/packages
    and re-registers script entities.

    Returns: list[dict], the raw `script.reload` service-call response.
    """
    return ha.call_service("script", "reload")


@mcp.tool(annotations=read("List scenes"))
def list_scenes() -> list[dict]:
    """List all scenes.

    Reads `ha.get_states()` and keeps entries whose `entity_id` starts with
    'scene.'.

    Returns: list[dict] `{"entity_id", "friendly_name"}` per scene.
    """
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
        }
        for s in states
        if s["entity_id"].startswith("scene.")
    ]


@mcp.tool(annotations=write("Activate a scene", idempotent=True))
def activate_scene(
    entity_id: Annotated[
        str,
        Field(description="Full scene entity ID to activate, e.g. 'scene.movie_night'."),
    ],
) -> list[dict]:
    """Activate a scene, applying its stored entity states.

    Calls the `scene.turn_on` service with `entity_id`.

    Returns: list[dict], the raw `scene.turn_on` service-call response.
    """
    return ha.call_service("scene", "turn_on", {"entity_id": entity_id})


@mcp.tool(annotations=write("Reload scenes from YAML", idempotent=True))
def reload_scenes() -> list[dict]:
    """Reload scenes from YAML.

    Calls the `scene.reload` service, which re-reads scenes.yaml and
    re-registers scene entities from it; scenes saved via
    `automations_set_scene_config` are included since that tool writes to
    the same config store.

    Returns: list[dict], the raw `scene.reload` service-call response.
    """
    return ha.call_service("scene", "reload")


# --- Scene config CRUD ---

@mcp.tool(annotations=read("Get a scene's stored config"))
def get_scene_config(
    scene_id: Annotated[
        str,
        Field(
            description=(
                "Scene id to look up, bare (e.g. 'movie_night') or "
                "prefixed ('scene.movie_night'); the 'scene.' prefix is "
                "stripped if present."
            )
        ),
    ],
) -> dict | None:
    """Get a scene's stored config (member entities and their target states).

    Requests `/api/config/scene/config/<id>` over HTTP and returns the
    parsed JSON, or `None` on a 404.

    Not for: the scene's current runtime state — use
    `automations_list_scenes` or `entities_get_entity` for that.
    Returns: dict with `id`, `name` and `entities` (mapping entity_id to
    its target state dict), or `None` when no scene has that id.
    """
    sid = _strip_prefix(scene_id, "scene")
    with ha._client() as c:
        r = c.get(f"/api/config/scene/config/{sid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=destructive("Create or overwrite a scene config", idempotent=True))
def set_scene_config(
    scene_id: Annotated[
        str,
        Field(
            description=(
                "Scene id to create or overwrite, bare (e.g. "
                "'movie_night') or prefixed ('scene.movie_night')."
            )
        ),
    ],
    name: Annotated[
        str,
        Field(description="Friendly name shown for the scene in the UI."),
    ],
    entities: Annotated[
        dict,
        Field(
            description=(
                "Non-empty mapping of entity_id to its target state dict "
                "for this scene, e.g. {'light.living_room': {'state': "
                "'on', 'brightness': 76}}. Brightness is 0-255 (30% ~ 76, "
                "50% ~ 128, 100% = 255)."
            )
        ),
    ],
) -> dict:
    """Create or overwrite a scene's stored config, then reload scenes.

    Posts `{id, name, entities}` to `/api/config/scene/config/<id>`,
    replacing any existing config at that id, then calls the
    `scene.reload` service so the change takes effect immediately.

    Not for: applying the scene's states right now — use
    `automations_activate_scene`.
    Returns: dict `{"scene_id", "name", "entity_count", ...}` merged with
    HA's save response.
    Errors: returns `{"error": "entities must be a non-empty dict of
    {entity_id: state_dict}"}` when `entities` is empty or not a dict.
    Limits: no `confirm` parameter; an existing scene at `scene_id` is
    replaced without a prompt.
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


@mcp.tool(annotations=destructive("Delete a scene config", idempotent=True))
def delete_scene(
    scene_id: Annotated[
        str,
        Field(
            description=(
                "Scene id to delete, bare (e.g. 'movie_night') or "
                "prefixed ('scene.movie_night')."
            )
        ),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Must be True to actually delete the scene. False "
                "(default) returns a safety prompt instead of deleting "
                "anything."
            )
        ),
    ] = False,
) -> dict:
    """Delete a scene config, then reload scenes.

    Without `confirm=True` returns a safety prompt and touches nothing;
    with `confirm=True` deletes `/api/config/scene/config/<id>` and calls
    the `scene.reload` service.

    Returns: dict `{"deleted": sid, ...}` merged with HA's delete response,
    or `{"status": "not_found", "scene_id": sid}` when no scene has that
    id.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is not True.
    Limits: requires `confirm=True`; deletion is immediate and permanent.
    """
    sid = _strip_prefix(scene_id, "scene")
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will permanently delete scene '{sid}'. Call again with confirm=True.",
            "action": f"delete_scene(scene_id='{scene_id}', confirm=True)",
        }
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

@mcp.tool(annotations=read("Get an automation's stored config"))
def get_automation_config(
    automation_id: Annotated[
        str,
        Field(
            description=(
                "Automation config id — the entity's `id` attribute, e.g. "
                "'1733294456585' — not its entity_id. A leading "
                "'automation.' is stripped, but the rest must still be the "
                "config id (read it from `entities_get_entity(entity_id)` "
                "-> attributes.id), which differs from the entity's "
                "object_id for UI-created automations."
            )
        ),
    ],
) -> dict | None:
    """Get the stored config of one automation (automations.yaml) as a dict.

    Requests `/api/config/automation/config/<id>` over HTTP and returns
    the parsed JSON, or `None` on a 404 — including for automations defined
    in packages/YAML without an id, since those have no config-id route.

    Not for: the automation's current runtime state — use
    `automations_list_automations` or `entities_get_entity` for that.
    Returns: dict with `id`, `alias`, `trigger`/`triggers`,
    `condition`/`conditions`, `action`/`actions` and `mode`, or `None` when
    no automation has that config id.
    """
    aid = _strip_prefix(automation_id, "automation")
    return ha.get_automation_config(aid)


@mcp.tool(annotations=destructive("Create or overwrite an automation config", idempotent=True))
def set_automation_config(
    automation_id: Annotated[
        str,
        Field(
            description=(
                "Automation config id to create or replace — the entity's "
                "`id` attribute, not its entity_id. An id that does not "
                "exist yet creates a NEW automation instead of editing one; "
                "read the current id from `entities_get_entity(entity_id)` "
                "-> attributes.id before editing."
            )
        ),
    ],
    config: Annotated[
        dict,
        Field(
            description=(
                "Full automation config replacing whatever is stored at "
                "`automation_id` (alias, triggers, conditions, actions, "
                "mode). Must contain 'alias' or 'trigger'/'triggers'."
            )
        ),
    ],
) -> dict:
    """Create or replace the automation whose config id is `automation_id`.

    Posts `config` to `/api/config/automation/config/<id>`, replacing the
    whole stored config at that id (or creating a new automation if the id
    is unused), then calls the `automation.reload` service.

    Not for: editing a single field without resending the rest of the
    config — this tool always replaces the whole stored config.
    Returns: dict `{"status": "saved", "automation_id", "result"}`.
    Errors: returns `{"error": "config must be a dict"}` when `config`
    isn't a dict, or `{"error": "config must contain at least 'alias' or
    'trigger'/'triggers'"}` when both are missing.
    Limits: no `confirm` parameter; an existing automation at
    `automation_id` is replaced without a prompt.
    """
    if not isinstance(config, dict):
        return {"error": "config must be a dict"}
    if "alias" not in config and "trigger" not in config and "triggers" not in config:
        return {"error": "config must contain at least 'alias' or 'trigger'/'triggers'"}
    aid = _strip_prefix(automation_id, "automation")
    result = ha.set_automation_config(aid, config)
    ha.call_service("automation", "reload")
    return {"status": "saved", "automation_id": aid, "result": result}


@mcp.tool(annotations=destructive("Delete an automation config", idempotent=True))
def delete_automation(
    automation_id: Annotated[
        str,
        Field(
            description=(
                "Automation config id to delete — the entity's `id` "
                "attribute, not its entity_id. A leading 'automation.' is "
                "stripped if present."
            )
        ),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Must be True to actually delete the automation. False "
                "(default) returns a safety prompt instead of deleting "
                "anything."
            )
        ),
    ] = False,
) -> dict:
    """Delete an automation by config id, then reload automations.

    Without `confirm=True` returns a safety prompt; with `confirm=True`
    deletes `/api/config/automation/config/<id>` and calls the
    `automation.reload` service.

    Returns: dict `{"status": "deleted", "automation_id", "result"}`.
    Errors: returns `{"error": "confirmation_required", "message":
    ..., "action": ...}` when `confirm` is not True, or `{"error": "not
    found", "automation_id": aid}` when no automation has that config id.
    Limits: requires `confirm=True`; deletion is immediate and permanent.
    """
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

@mcp.tool(annotations=read("Get a script's stored YAML config"))
def get_script_config(
    script_id: Annotated[
        str,
        Field(
            description=(
                "Script id to look up, bare (e.g. 'goodnight') or prefixed "
                "('script.goodnight'); the 'script.' prefix is stripped if "
                "present."
            )
        ),
    ],
) -> dict | None:
    """Get a script's stored YAML config as a dict.

    Requests `/api/config/script/config/<id>` over HTTP and returns the
    parsed JSON, or `None` on a 404.

    Not for: the script's current runtime state — use
    `automations_list_scripts` or `entities_get_entity` for that.
    Returns: dict with the script's `alias`, `sequence`, `fields` and
    `mode`, or `None` when no script has that id.
    """
    sid = _strip_prefix(script_id, "script")
    return ha.get_script_config(sid)


@mcp.tool(annotations=destructive("Create or overwrite a script config", idempotent=True))
def set_script_config(
    script_id: Annotated[
        str,
        Field(
            description=(
                "Script id to create or replace, bare (e.g. 'goodnight') "
                "or prefixed ('script.goodnight'). An id that does not "
                "exist yet creates a new script."
            )
        ),
    ],
    config: Annotated[
        dict,
        Field(
            description=(
                "Full script config replacing whatever is stored at "
                "`script_id` (sequence, fields, alias, mode). Must contain "
                "at least 'alias' or 'sequence'."
            )
        ),
    ],
) -> dict:
    """Create or overwrite a script's stored YAML config, then reload scripts.

    Posts `config` to `/api/config/script/config/<id>`, replacing the whole
    stored config at that id (or creating a new script if the id is
    unused), then calls the `script.reload` service.

    Not for: running the script — use `automations_run_script`.
    Returns: dict `{"status": "saved", "script_id", "result"}`.
    Errors: returns `{"error": "config must be a dict"}` when `config`
    isn't a dict, or `{"error": "config must contain at least 'alias' or
    'sequence'"}` when both are missing.
    Limits: no `confirm` parameter; an existing script at `script_id` is
    replaced without a prompt.
    """
    if not isinstance(config, dict):
        return {"error": "config must be a dict"}
    if "sequence" not in config and "alias" not in config:
        return {"error": "config must contain at least 'alias' or 'sequence'"}
    sid = _strip_prefix(script_id, "script")
    result = ha.set_script_config(sid, config)
    ha.call_service("script", "reload")
    return {"status": "saved", "script_id": sid, "result": result}


@mcp.tool(annotations=destructive("Delete a script config", idempotent=True))
def delete_script(
    script_id: Annotated[
        str,
        Field(
            description=(
                "Script id to delete, bare (e.g. 'goodnight') or prefixed "
                "('script.goodnight')."
            )
        ),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Must be True to actually delete the script. False "
                "(default) returns a safety prompt instead of deleting "
                "anything."
            )
        ),
    ] = False,
) -> dict:
    """Delete a script by id, then reload scripts.

    Without `confirm=True` returns a safety prompt; with `confirm=True`
    deletes `/api/config/script/config/<id>` and calls the `script.reload`
    service.

    Returns: dict `{"status": "deleted", "script_id", "result"}`.
    Errors: returns `{"error": "confirmation_required", "message":
    ..., "action": ...}` when `confirm` is not True, or `{"error": "not
    found", "script_id": sid}` when no script has that id.
    Limits: requires `confirm=True`; deletion is immediate and permanent.
    """
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

@mcp.tool(annotations=read("List automation trace summaries"))
def list_automation_traces(
    automation_id: Annotated[
        str,
        Field(
            description=(
                "Automation config id to list traces for, bare or prefixed "
                "('automation.<id>')."
            )
        ),
    ],
) -> list[dict]:
    """List trace summaries (recent runs) for an automation.

    Calls the `trace/list` WebSocket command for domain 'automation' and
    `automation_id`.

    Returns: list[dict] of trace summaries (run_id, timestamps,
    `script_execution` outcome) — no step-by-step detail; use
    `automations_get_automation_trace` for one run's full detail.
    Limits: blocks for the WebSocket round trip.
    """
    aid = _strip_prefix(automation_id, "automation")
    return ha._ws_call("trace/list", domain="automation", item_id=aid)


@mcp.tool(annotations=read("Get one automation trace"))
def get_automation_trace(
    automation_id: Annotated[
        str,
        Field(
            description=(
                "Automation config id the trace belongs to, bare or "
                "prefixed ('automation.<id>')."
            )
        ),
    ],
    run_id: Annotated[
        str,
        Field(description="Run id from `automations_list_automation_traces`, identifying one specific run."),
    ],
) -> dict:
    """Get a full automation trace (steps, timing, condition results) for a specific run.

    Calls the `trace/get` WebSocket command for domain 'automation',
    `automation_id` and `run_id`.

    Not for: finding which run_id to use — use
    `automations_list_automation_traces` or
    `automations_get_last_automation_trace` first.
    Returns: dict with the full step-by-step trace for that run.
    Limits: blocks for the WebSocket round trip.
    """
    aid = _strip_prefix(automation_id, "automation")
    return ha._ws_call("trace/get", domain="automation", item_id=aid, run_id=run_id)


@mcp.tool(annotations=read("List script trace summaries"))
def list_script_traces(
    script_id: Annotated[
        str,
        Field(description="Script id to list traces for, bare or prefixed ('script.<id>')."),
    ],
) -> list[dict]:
    """List trace summaries (recent runs) for a script.

    Calls the `trace/list` WebSocket command for domain 'script' and
    `script_id`.

    Returns: list[dict] of trace summaries (run_id, timestamps,
    `script_execution` outcome) — no step-by-step detail; use
    `automations_get_script_trace` for one run's full detail.
    Limits: blocks for the WebSocket round trip.
    """
    sid = _strip_prefix(script_id, "script")
    return ha._ws_call("trace/list", domain="script", item_id=sid)


@mcp.tool(annotations=read("Get one script trace"))
def get_script_trace(
    script_id: Annotated[
        str,
        Field(description="Script id the trace belongs to, bare or prefixed ('script.<id>')."),
    ],
    run_id: Annotated[
        str,
        Field(description="Run id from `automations_list_script_traces`, identifying one specific run."),
    ],
) -> dict:
    """Get a full script trace (steps, timing, action results) for a specific run.

    Calls the `trace/get` WebSocket command for domain 'script',
    `script_id` and `run_id`.

    Not for: finding which run_id to use — use
    `automations_list_script_traces` or `automations_get_last_script_trace`
    first.
    Returns: dict with the full step-by-step trace for that run.
    Limits: blocks for the WebSocket round trip.
    """
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


@mcp.tool(annotations=read("Get the most recent automation trace"))
def get_last_automation_trace(
    automation_id: Annotated[
        str,
        Field(
            description=(
                "Automation config id to fetch the latest trace for, bare "
                "or prefixed ('automation.<id>')."
            )
        ),
    ],
    failed_only: Annotated[
        bool,
        Field(
            description=(
                "If True, pick the most recent run that did not finish "
                "cleanly instead of the most recent run overall. Defaults "
                "to False."
            )
        ),
    ] = False,
) -> dict:
    """Fetch the most recent trace for an automation in one call.

    Calls `trace/list` for `automation_id`, then picks a run_id (the most
    recent overall, or with `failed_only=True` the most recent whose
    `script_execution` isn't 'finished') and calls `trace/get` for it.

    Use when: debugging "why didn't this fire?" without first listing
    traces and picking a run_id by hand.
    Returns: dict with the full step-by-step trace for the picked run.
    Errors: returns `{"error": "no_traces"|"no_failed_traces",
    "automation_id": aid}` when no matching run exists.
    Limits: blocks for two sequential WebSocket round trips.
    """
    aid = _strip_prefix(automation_id, "automation")
    traces = ha._ws_call("trace/list", domain="automation", item_id=aid) or []
    run_id = _resolve_run_id(traces, failed_only)
    if not run_id:
        return {"error": "no_traces" if not failed_only else "no_failed_traces", "automation_id": aid}
    return ha._ws_call("trace/get", domain="automation", item_id=aid, run_id=run_id)


@mcp.tool(annotations=read("Get the most recent script trace"))
def get_last_script_trace(
    script_id: Annotated[
        str,
        Field(description="Script id to fetch the latest trace for, bare or prefixed ('script.<id>')."),
    ],
    failed_only: Annotated[
        bool,
        Field(
            description=(
                "If True, pick the most recent run that did not finish "
                "cleanly instead of the most recent run overall. Defaults "
                "to False."
            )
        ),
    ] = False,
) -> dict:
    """Fetch the most recent (or most recent failed) trace for a script.

    Calls `trace/list` for `script_id`, then picks a run_id (the most
    recent overall, or with `failed_only=True` the most recent whose
    `script_execution` isn't 'finished') and calls `trace/get` for it.

    Use when: debugging why a script didn't behave as expected without
    first listing traces and picking a run_id by hand.
    Returns: dict with the full step-by-step trace for the picked run.
    Errors: returns `{"error": "no_traces"|"no_failed_traces",
    "script_id": sid}` when no matching run exists.
    Limits: blocks for two sequential WebSocket round trips.
    """
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


@mcp.tool(annotations=read("Validate automation YAML authoring rules"))
def validate_best_practices(
    yaml_content: Annotated[
        str,
        Field(
            description=(
                "YAML text of a single automation dict or a list of "
                "automation dicts to lint. Parsed with plain "
                "`yaml.safe_load`, so HA-only tags like '!input'/'!secret' "
                "are not resolved."
            )
        ),
    ],
) -> dict:
    """Validate automation YAML against static Home Assistant authoring rules.

    Parses `yaml_content` with plain `yaml.safe_load` (HA-only tags like
    `!input`/`!secret` are NOT resolved and make parsing fail with a YAML
    parse error, same as any other unknown tag) and runs fixed checks: a
    state trigger without 'for:', a missing 'mode:', multiple triggers
    without 'id:', a missing 'alias:'/'id:', the deprecated 'service:'
    action key, 'restart' mode, and a missing 'description:'.

    Use when: linting automation YAML before saving it with
    `automations_set_automation_config`.
    Not for: checking that referenced entities/services actually exist —
    use `automations_validate_automation_references`; blueprint content
    with unresolved '!input'/'!secret' tags — extract them to plain YAML
    first.
    Returns: dict `{valid_yaml, automations_checked, issues_total,
    warnings, infos, issues[], summary}`; each issue is `{automation,
    rule, severity, message}` with severity 'warning' (should fix) or
    'info' (good to know).
    Errors: returns `{"valid_yaml": False, "error": "YAML parse error:
    ...", "issues": []}` for invalid YAML.
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
            "✅ No issues found"
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
    """Recursively walk automation dict and collect literal service/action values.

    The keys 'service'/'action' are ambiguous in the HA automation schema:
    at one step they hold the literal "domain.service" string to call, but
    the *legacy* top-level automation key is spelled 'action:' (singular)
    and holds a LIST of step dicts, not a string. The same ambiguity exists
    one level down for 'default'/'then'/'else'/'sequence' contents reached
    through 'choose'/'if'/'parallel'/'repeat'. Whenever the value under
    'service'/'action' is not itself a plain string, it must still be
    walked — otherwise the entire legacy 'action:' step list (and anything
    nested under it) is silently skipped.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("service", "action") and isinstance(v, str) and "{{" not in v and "." in v:
                found.add(v)
            else:
                _extract_services(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _extract_services(item, found)


@mcp.tool(annotations=read("Cross-check automation YAML against live HA"))
def validate_automation_references(
    yaml_content: Annotated[
        str,
        Field(
            description=(
                "YAML text of a single automation dict or a list of "
                "automation dicts to check. Parsed with plain "
                "`yaml.safe_load`, so HA-only tags like '!input'/'!secret' "
                "are not resolved."
            )
        ),
    ],
) -> dict:
    """Cross-check entity IDs and services used in automation YAML against the live registry.

    Unlike `automations_validate_best_practices` (static), this calls
    `ha.get_states()` and `ha.list_services()` to verify that every literal
    `entity_id` value and every literal `service:`/`action:` string in
    `yaml_content` exists. It only looks at literal values — it does NOT
    resolve `device_id`, `area_id`, `label_id`, or any templated reference,
    not just the ones containing '{{ ... }}' (which are explicitly skipped
    and counted in `template_refs_skipped`).

    Use when: checking that an automation's references are real before
    saving it with `automations_set_automation_config`.
    Not for: YAML syntax or authoring-rule checks — use
    `automations_validate_best_practices`.
    Returns: dict `{valid_yaml, automations_checked, entities_checked,
    services_checked, missing_entities[], missing_services[],
    template_refs_skipped, entities_check, services_check, errors[],
    summary}`. `entities_check`/`services_check` are 'ok' or 'unavailable'.
    Errors: returns `{"valid_yaml": False, "error": "YAML parse error:
    ..."}` for invalid YAML. When `ha.get_states()` or `ha.list_services()`
    raises, the matching `entities_check`/`services_check` is 'unavailable'
    and `errors` names which fetch failed; the corresponding `missing_*`
    list stays empty rather than reporting a false "nothing missing" or a
    false "everything is missing".
    Limits: fetches the full live state list and full service list on
    every call.
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

    # Fetch live data. A fetch failure must never be mistaken for "nothing
    # missing" (services) or silently reported as "everything is missing"
    # (entities, since an empty live set makes every reference look absent) —
    # both are false results, not real validation. `entities_check`/
    # `services_check` say explicitly whether each side was actually checked;
    # the corresponding `missing_*` list stays empty (not "all") when its
    # check is "unavailable", and `errors` names which fetch failed and why.
    errors: list[str] = []

    entities_check = "ok"
    try:
        live_entity_ids = {s["entity_id"] for s in ha.get_states()}
    except Exception as e:
        live_entity_ids = set()
        entities_check = "unavailable"
        errors.append(f"could not fetch live entity states (ha.get_states failed): {e}")

    services_check = "ok"
    try:
        live_services: set = set()
        for entry in ha.list_services():
            domain = entry.get("domain", "")
            for svc in entry.get("services") or {}:
                live_services.add(f"{domain}.{svc}")
    except Exception as e:
        live_services = set()
        services_check = "unavailable"
        errors.append(f"could not fetch live service list (ha.list_services failed): {e}")

    missing_entities = sorted(entity_refs - live_entity_ids) if entities_check == "ok" else []
    missing_services = sorted(service_refs - live_services) if services_check == "ok" else []

    total_missing = len(missing_entities) + len(missing_services)
    if entities_check == "unavailable" or services_check == "unavailable":
        summary = f"⚠️ validation incomplete: {'; '.join(errors)}"
    elif not total_missing:
        summary = "✅ All references exist in Home Assistant"
    else:
        summary = f"❌ {len(missing_entities)} missing entity(s), {len(missing_services)} missing service(s)"

    return {
        "valid_yaml": True,
        "automations_checked": len(automations),
        "entities_checked": len(entity_refs),
        "services_checked": len(service_refs),
        "template_refs_skipped": template_count,
        "missing_entities": missing_entities,
        "missing_services": missing_services,
        "entities_check": entities_check,
        "services_check": services_check,
        "errors": errors,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Entity group management (group.set / group.remove)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=read("List entity groups"))
def list_groups() -> list[dict]:
    """List all Home Assistant entity groups (group.* entities).

    Reads `ha.get_states()` and keeps entries whose `entity_id` starts with
    'group.', with their members, icon and current state.

    Returns: list[dict] `{"entity_id", "state", "friendly_name",
    "entity_ids", "icon", "all", "order"}` per group.
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


@mcp.tool(annotations=write("Create or update a runtime entity group", idempotent=False))
def set_group(
    group_id: Annotated[
        str,
        Field(description="Bare group id (e.g. 'living_room_lights') — no 'group.' prefix."),
    ],
    name: Annotated[
        str | None,
        Field(description="Friendly name for the group. Omit to leave it unset/unchanged."),
    ] = None,
    entities: Annotated[
        list[str] | None,
        Field(
            description=(
                "Full list of entity_ids to set as the group's members, "
                "replacing the current member list. Omit to leave members "
                "unchanged (use add_entities/remove_entities instead)."
            )
        ),
    ] = None,
    icon: Annotated[
        str | None,
        Field(description="MDI icon string, e.g. 'mdi:lightbulb-group'. Omit to leave it unset/unchanged."),
    ] = None,
    all_entities: Annotated[
        bool,
        Field(
            description=(
                "If True, the group's state is 'on' only when every member "
                "is on. False (default) does not clear an existing "
                "all:true."
            )
        ),
    ] = False,
    add_entities: Annotated[
        list[str] | None,
        Field(description="Entity_ids to add to the existing member list. Do not combine with `entities`."),
    ] = None,
    remove_entities: Annotated[
        list[str] | None,
        Field(description="Entity_ids to remove from the existing member list. Do not combine with `entities`."),
    ] = None,
) -> dict:
    """Create or update an old-style group.* entity at runtime.

    Calls the `group.set` service with `object_id=group_id` and whichever
    of `name`/`entities`/`icon`/`all`/`add_entities`/`remove_entities` were
    given. The group is NOT written to groups.yaml and disappears on
    restart or `automations_reload_groups`-style YAML reload; for a
    persistent group use a Group helper or add it to groups.yaml with the
    `files_write_config_file` tools. Does not create light/switch/cover
    group helpers.

    Not for: a group that survives a restart — define it in groups.yaml
    instead (`files_write_config_file`) or create a Group helper.
    Returns: dict `{"status": "set", "group_id": "group.<id>"}`.
    Errors: returns `{"error": "entities cannot be combined with
    add_entities/remove_entities — pass a full member list via entities, or
    incremental changes via add_entities/remove_entities, not both"}` when
    `entities` is given together with `add_entities` or `remove_entities`;
    `group.set` is not called in that case.
    Limits: `entities` replaces the whole member list and must not be
    combined with `add_entities`/`remove_entities` in the same call.
    """
    if entities is not None and (add_entities or remove_entities):
        return {
            "error": (
                "entities cannot be combined with add_entities/remove_entities — "
                "pass a full member list via entities, or incremental changes via "
                "add_entities/remove_entities, not both"
            )
        }
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


@mcp.tool(annotations=destructive("Remove a runtime entity group", idempotent=True))
def remove_group(
    group_id: Annotated[
        str,
        Field(description="Bare group id or 'group.<id>' to remove."),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Must be True to actually remove the group. False "
                "(default) returns a safety prompt instead of removing "
                "anything."
            )
        ),
    ] = False,
) -> dict:
    """Remove a group created with automations_set_group.

    Without `confirm=True` returns a safety prompt; with `confirm=True`
    calls the `group.remove` service with `object_id`. Groups defined in
    groups.yaml come back on the next restart or YAML reload — this only
    removes the runtime entity.

    Returns: dict `{"status": "removed", "group_id": "group.<id>"}`.
    Errors: returns `{"error": "confirmation_required", "message":
    ..., "action": ...}` when `confirm` is not True.
    Limits: requires `confirm=True`; has no effect on a group defined in
    groups.yaml beyond the next reload/restart.
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
