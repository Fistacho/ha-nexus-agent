"""Home Assistant Core system control — config validation, restart/stop, reload,
backups, integrations overview, health, repairs and updates via the core REST/
WebSocket API. Supervisor-specific add-on/host management lives in
`tools/supervisor.py`.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("system")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("Validate HA configuration"))
def check_config() -> dict:
    """Validate the current Home Assistant configuration without applying anything.

    Calls `POST /api/config/core/check_config`, the same YAML validation HA
    runs internally before a restart; nothing is reloaded or restarted by
    this call.

    Use when: verifying `configuration.yaml` and related files are valid
    before calling `system_restart_ha`.
    Not for: applying a change into the running instance — use
    `system_reload_all` for a no-downtime reload or `system_restart_ha` for a
    full restart.
    Returns: HA's check payload, typically `{"result": "valid"|"invalid",
    "errors": ...}`.
    Errors: raises `httpx.HTTPStatusError` if the HA API request itself
    fails (e.g. HA unreachable).
    """
    with ha._client() as c:
        r = c.post("/api/config/core/check_config")
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=destructive("Restart Home Assistant", idempotent=True))
def restart_ha(
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually restart HA. False (default) performs no "
                "restart and instead returns a confirmation prompt."
            )
        ),
    ] = False,
) -> dict:
    """Restart Home Assistant after an explicit confirmation.

    Calls the `homeassistant.restart` service, which causes roughly 30
    seconds of downtime for the whole instance, including this add-on.
    Without `confirm=True` nothing is restarted; the call only returns a
    safety prompt describing how to confirm.

    Use when: a config change requires a full restart, e.g. after
    `system_check_config` reports the config is valid.
    Not for: a no-downtime reload — use `system_reload_all`; stopping HA
    without restarting it — use `system_stop_ha`; restarting only Core under
    Supervisor — use `supervisor_restart_core`.
    Returns: the `homeassistant.restart` service-call result when confirmed,
    otherwise a confirmation prompt.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false.
    Limits: interrupts Home Assistant for about 30 seconds; requires
    `confirm=True`.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": "This will restart Home Assistant (~30s downtime). Call again with confirm=True to proceed.",
            "action": "restart_ha(confirm=True)",
        }
    return ha.call_service("homeassistant", "restart")


@mcp.tool(annotations=destructive("Stop Home Assistant", idempotent=True))
def stop_ha(
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually stop HA. False (default) performs no "
                "action and instead returns a confirmation prompt."
            )
        ),
    ] = False,
) -> dict:
    """Stop Home Assistant after an explicit confirmation.

    Calls the `homeassistant.stop` service. Unlike a restart, nothing brings
    the instance back automatically — a manual restart is required. Without
    `confirm=True` nothing is stopped; the call only returns a safety
    prompt.

    Use when: HA needs to be taken fully offline, e.g. before host
    maintenance.
    Not for: a temporary interruption that comes back on its own — use
    `system_restart_ha`.
    Returns: the `homeassistant.stop` service-call result when confirmed,
    otherwise a confirmation prompt.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false.
    Limits: requires a manual restart to bring HA back online; requires
    `confirm=True`.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": "This will STOP Home Assistant and require a manual restart. Call again with confirm=True to proceed.",
            "action": "stop_ha(confirm=True)",
        }
    return ha.call_service("homeassistant", "stop")


@mcp.tool(annotations=write("Reload core configuration", idempotent=True))
def reload_all() -> dict:
    """Reload core Home Assistant configuration without restarting the instance.

    Calls the `homeassistant.reload_all` service, which HA fans out to every
    component that supports a config reload (automations, scripts, scenes,
    groups and more), re-reading their YAML from disk.

    Use when: YAML for automations/scripts/scenes/groups changed and a fast,
    no-downtime reload is enough.
    Not for: changes that require a full restart, e.g. new integrations or
    structural `configuration.yaml` edits — use `system_restart_ha`; for one
    integration only — use `system_reload_integration`.
    Returns: `{"status": "reload_all triggered"}`; it does not wait for or
    report individual component reload results.
    """
    ha.call_service("homeassistant", "reload_all")
    return {"status": "reload_all triggered"}


@mcp.tool(annotations=destructive("Create automatic HA backup", idempotent=False))
def create_backup() -> dict:
    """Create a Home Assistant backup using whichever backup service the instance registers.

    On Supervisor installs `backup.create` does not exist, so this calls
    `backup.create_automatic` instead, which creates a backup using the
    automatic-backup settings configured in Settings -> System -> Backups;
    when a retention count is configured there, those same settings prune
    older automatic backups as a side effect of this call. On HA
    Core/Container installs, where `backup.create` exists, that one is
    called instead — no name, folder selection or password. Returns an
    error if neither service is registered.

    Use when: HA already has the wanted automatic-backup settings configured
    and a quick backup with no extra parameters is enough.
    Not for: a named, partial, and/or password-protected backup on a
    Supervisor install, or a backup that never deletes older ones — use
    `supervisor_create_backup`.
    Returns: the called service's result, or an error dict when neither
    service is registered.
    Errors: `{"error": "Neither backup.create nor backup.create_automatic is
    registered on this Home Assistant instance."}`.
    Limits: on Supervisor installs, applies the instance's configured
    retention count, which can delete older backups.
    """
    domains = {d.get("domain"): (d.get("services") or {}) for d in ha.list_services()}
    backup_services = domains.get("backup", {})
    if "create" in backup_services:
        return ha.call_service("backup", "create")
    if "create_automatic" in backup_services:
        return ha.call_service("backup", "create_automatic")
    return {
        "error": (
            "Neither backup.create nor backup.create_automatic is registered on this Home "
            "Assistant instance."
        )
    }


@mcp.tool(annotations=read("List all config entries"))
def list_integrations() -> list[dict]:
    """List every Home Assistant config entry, whatever its state.

    Calls the raw `config_entries/get` WebSocket command and returns it
    unfiltered: every field HA stores per entry, in any state (`loaded`,
    `not_loaded`, `setup_error`, `disabled`). On instances with many
    integrations this can be a large payload.

    Use when: the raw config-entry objects, with every field, are needed.
    Not for: a compact, easier-to-scan listing — use
    `system_get_all_integrations`, which strips this down to
    entry_id/domain/title/state/source.
    Returns: list of raw config-entry dicts as returned by HA's WebSocket
    API.
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. auth or WS-level error).
    Limits: no pagination or filtering; large HA instances get a large
    response.
    """
    return ha.get_config_entries()


@mcp.tool(annotations=write("Reload one config entry", idempotent=True))
def reload_integration(
    entry_id: Annotated[
        str,
        Field(
            description=(
                "Config entry ID to reload, from `system_list_integrations` or "
                "`system_get_all_integrations`."
            )
        ),
    ],
) -> list[dict]:
    """Reload a single integration's config entry without restarting Home Assistant.

    Calls the `homeassistant.reload_config_entry` service for the given
    `entry_id`, which unloads and re-sets-up that one integration only.

    Use when: one integration needs to pick up new options or recover from a
    transient setup error.
    Not for: reloading every reloadable component at once — use
    `system_reload_all`.
    Returns: the `homeassistant.reload_config_entry` service-call result.
    Errors: raises `httpx.HTTPStatusError` if `entry_id` does not exist or
    the domain does not support config-entry reload.
    """
    return ha.call_service("homeassistant", "reload_config_entry", {"entry_id": entry_id})


@mcp.tool(annotations=read("Check HA API reachability"))
def ping_ha() -> dict:
    """Check whether the Home Assistant API is reachable over HTTP.

    Calls `GET /api/` through `ha.ping()`, which catches any connection
    error internally and reports plain reachability, not health or
    subsystem status.

    Use when: confirming HA is up before other calls, quick liveness
    probing.
    Not for: the raw `/api/` payload itself — use `history_get_system_info`;
    for HA version/location/config use `history_get_ha_config`; for
    subsystem health use `system_get_system_health`.
    Returns: `{"reachable": bool, "url": <configured HA_URL>}`.
    """
    ok = ha.ping()
    return {"reachable": ok, "url": ha._HA_URL}


@mcp.tool(annotations=read("List config entries compactly"))
def get_all_integrations() -> list[dict]:
    """List every config entry as a compact summary of its key fields.

    Calls the same `config_entries/get` WebSocket command as
    `system_list_integrations` and projects each entry down to entry_id,
    domain, title, state and source; every state is included, so filter on
    `state` yourself.

    Use when: scanning or filtering integrations by domain/state without the
    full raw payload.
    Not for: every field HA stores on the entry — use
    `system_list_integrations`.
    Returns: list of `{entry_id, domain, title, state, source}` dicts.
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. auth or WS-level error).
    """
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


@mcp.tool(annotations=read("Get system health checks"))
def get_system_health() -> dict:
    """Get Home Assistant's system-health report across its core subsystems.

    Runs the `system_health/info` WebSocket subscription to completion and
    merges its initial/update/finish events into one dict, since HA exposes
    system health only as a WebSocket stream, not over HTTP.

    Use when: checking recorder/websocket/cloud/network-style health checks
    and basic system info (version, dev mode, virtualenv) in one call.
    Not for: a plain liveness check — use `system_ping_ha`; for HA
    version/location/config use `history_get_ha_config`.
    Returns: dict keyed by integration domain, each with an `info` dict
    mapping check name to value (or `{"error": ...}` for a failed check).
    Errors: raises `RuntimeError` on WebSocket auth failure or a WS-level
    error from `system_health/info`.
    Limits: waits up to an internal 15-second timeout for the stream's
    `finish` event; on timeout it returns whatever partial data arrived
    instead of raising.
    """
    return ha.collect_system_health()


@mcp.tool(annotations=read("List active repair issues"))
def get_repairs() -> list[dict]:
    """List Home Assistant's active repair issues.

    Calls the `repairs/list_issues` WebSocket command and projects each
    issue down to its id, domain, severity, `breaks_in_ha_version`,
    learn-more URL, translation key, and `ignored`/`is_fixable` flags.

    Use when: surfacing deprecated config, broken integrations or required
    migrations that need attention.
    Not for: the full raw repair-issue payload with every field HA stores —
    this always returns the projected subset above.
    Returns: list of `{issue_id, domain, severity, breaks_in_ha_version,
    learn_more_url, translation_key, ignored, is_fixable}` dicts.
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. auth or WS-level error).
    """
    raw = ha._ws_call("repairs/list_issues")
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


@mcp.tool(annotations=read("List available HA updates"))
def get_updates(
    pending_only: Annotated[
        bool,
        Field(
            description=(
                "If true (default), return only components with an update "
                "available. Set false to also include up-to-date components."
            )
        ),
    ] = True,
) -> list[dict]:
    """List Home Assistant's `update.*` entities, one per updatable component.

    Reads all HA states and filters to entities in the `update` domain
    (core, add-ons, HACS repositories, custom components), extracts
    installed/latest version and release info from their attributes, and
    sorts pending updates first.

    Use when: checking what has an update available across the whole
    instance in one call.
    Not for: live Supervisor add-on state beyond this snapshot — use
    `supervisor_get_addon` or `supervisor_list_addons`.
    Returns: list of `{entity_id, name, installed_version, latest_version,
    update_available, release_url, release_notes, skipped_version,
    in_progress, auto_update}`, sorted with available updates first.
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
