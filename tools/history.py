"""State history, logbook and raw diagnostic reads — entity history over time,
human-readable logbook entries, the error log, and the two smallest HA
liveness/config reads (`system_info`, `ha_config`).
"""
from __future__ import annotations

from typing import Annotated

import httpx
from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import read

mcp = FastMCP("history")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("Get entity state history"))
def get_state_history(
    entity_id: Annotated[
        str,
        Field(description="Full entity ID to fetch history for, e.g. 'light.kitchen'."),
    ],
    hours: Annotated[
        int,
        Field(description="How many hours back from now to fetch, e.g. 24 for one day. Default 24."),
    ] = 24,
) -> list:
    """Get an entity's recorded state history over the last N hours.

    Calls `GET /api/history/period/<start>` filtered to `entity_id`, where
    `start` is `now - hours`; returns whatever the recorder database has
    kept for that window.

    Use when: seeing how one entity's state changed over time, e.g. a
    temperature trend.
    Not for: human-readable event descriptions — use
    `history_get_logbook`; this is raw, short-term state history, not the
    recorder's long-term pre-aggregated sum/mean/min/max values — use
    `statistics_get_statistics` for those, or `statistics_list_statistic_ids`
    to discover which entities have them.
    Returns: HA's raw history-period payload — a list of state-change
    records for the entity.
    Errors: raises `httpx.HTTPStatusError` if the HA API request fails.
    Limits: only as far back as the recorder integration has retained data
    for this entity.
    """
    return ha.get_history(entity_id=entity_id, hours=hours)


@mcp.tool(annotations=read("Get logbook entries"))
def get_logbook(
    entity_id: Annotated[
        str | None,
        Field(
            description=(
                "Full entity ID to filter to, e.g. 'light.kitchen'. Omit to "
                "return logbook entries for every entity."
            )
        ),
    ] = None,
    hours: Annotated[
        int,
        Field(description="How many hours back from now to fetch, e.g. 24 for one day. Default 24."),
    ] = 24,
) -> list:
    """Get human-readable logbook entries from the last N hours.

    Calls `GET /api/logbook/<start>`, optionally filtered by `entity_id`,
    where `start` is `now - hours`.

    Use when: a narrative of what happened (state changes, triggered
    automations) is more useful than raw state-history records.
    Not for: raw per-state timestamps and values for one entity — use
    `history_get_state_history`.
    Returns: HA's raw logbook payload — a list of `{when, name, message,
    entity_id, ...}`-shaped entries.
    Errors: raises `httpx.HTTPStatusError` if the HA API request fails.
    Limits: only as far back as the recorder integration has retained
    logbook data.
    """
    return ha.get_logbook(entity_id=entity_id, hours=hours)


@mcp.tool(annotations=read("Get HA error log"))
def get_error_log() -> str:
    """Fetch Home Assistant's error log as plain text.

    Prefers the raw log file via `GET /api/error_log`. That view only
    exists when HA logs to a file — Supervisor installs ship with
    `duplicate_log_file: false`, so it 404s there, in which case the
    warning/error records held by the `system_log` integration are rendered
    into log-file-like text instead.

    Use when: diagnosing recent warnings/errors across the whole instance.
    Not for: one entity's history or logbook — use
    `history_get_state_history` or `history_get_logbook`.
    Returns: plain-text log content, from either source.
    Errors: re-raises `httpx.HTTPStatusError` for any status other than 404
    from `/api/error_log`.
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


@mcp.tool(annotations=read("Check API liveness"))
def get_system_info() -> dict:
    """Get Home Assistant's raw API liveness message.

    Calls `GET /api/`, which returns only a fixed liveness message (e.g.
    `{"message": "API running."}`) — no version or system data despite the
    tool's name.

    Use when: a plain proof that the HTTP API is answering is enough.
    Not for: version/location/units/components — use
    `history_get_ha_config`; for subsystem health — use
    `system_get_system_health`; for Core/OS details — use
    `supervisor_get_core_info` or `supervisor_get_host_info`; for a simple
    reachable/unreachable boolean — use `system_ping_ha`.
    Returns: `{"message": "API running."}` (HA's fixed liveness payload).
    Errors: raises `httpx.HTTPStatusError` if the HA API request fails.
    """
    import ha_client as hac
    with hac._client() as c:
        r = c.get("/api/")
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=read("Get HA configuration"))
def get_ha_config() -> dict:
    """Get Home Assistant's current configuration.

    Calls `GET /api/config` for location, unit system, HA version and
    loaded components.

    Use when: version, location, unit system or the loaded-components list
    is needed.
    Not for: the equivalent WebSocket command — use `ws_get_config`; for
    subsystem health checks — use `system_get_system_health`.
    Returns: HA's raw config payload (`latitude`, `longitude`,
    `unit_system`, `version`, `components`, and related fields).
    Errors: raises `httpx.HTTPStatusError` if the HA API request fails.
    """
    return ha.get_config()
