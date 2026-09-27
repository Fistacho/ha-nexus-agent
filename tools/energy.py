"""Home Assistant Energy Dashboard preferences and sources.

Reads and edits the Energy Dashboard configuration (`energy_sources`,
`device_consumption`) over HA's WebSocket API (`energy/*`). The `add_*`/
`remove_energy_source` helpers do a read-modify-write on top of
`get_energy_prefs`/`save_energy_prefs` so callers don't have to hand-build
the full `energy_sources` list themselves.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("energy")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


# --- Raw Energy Dashboard prefs (WebSocket: energy/*) ---


@mcp.tool(annotations=read("Get energy dashboard preferences"))
def get_energy_prefs() -> dict:
    """Return the full HA Energy Dashboard preferences.

    Calls the WebSocket endpoint `energy/get_prefs` and returns its payload
    unchanged, including the configured `energy_sources` and
    `device_consumption` lists.

    Use when: you need the current preferences before editing them with
    `energy_save_energy_prefs` or one of the `energy_add_*`/
    `energy_remove_energy_source` helpers.
    Not for: adding or removing a single source without hand-building the
    full list — use `energy_add_grid_consumption`, `energy_add_grid_return`,
    `energy_add_solar_source`, `energy_add_battery_source` or
    `energy_remove_energy_source` instead.
    Returns: dict with `energy_sources` (list) and `device_consumption`
    (list), exactly as HA stores them.
    Errors: `{"error": "..."}` when the WebSocket call raises.
    """
    try:
        return ha._ws_call("energy/get_prefs")
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=destructive("Replace energy dashboard source lists", idempotent=True))
def save_energy_prefs(
    energy_sources: Annotated[
        list[dict] | None,
        Field(
            description=(
                "Full replacement for the Energy Dashboard's energy_sources "
                "list; each dict is one grid/solar/battery/gas/water source. "
                "Omit to leave the currently saved sources untouched."
            )
        ),
    ] = None,
    device_consumption: Annotated[
        list[dict] | None,
        Field(
            description=(
                "Full replacement for the Energy Dashboard's "
                "device_consumption list, one dict per tracked device. Omit "
                "to leave the currently saved device list untouched."
            )
        ),
    ] = None,
    currency: Annotated[
        str | None,
        Field(
            description=(
                "Deprecated and not part of this WS schema; passing anything "
                "other than None now returns an error instead of being "
                "silently ignored. Omit this parameter."
            )
        ),
    ] = None,
    energy_per_unit: Annotated[
        float | None,
        Field(
            description=(
                "Deprecated and not part of this WS schema; passing anything "
                "other than None now returns an error instead of being "
                "silently ignored. Omit this parameter."
            )
        ),
    ] = None,
) -> dict:
    """Save Energy Dashboard preferences via WS `energy/save_prefs`.

    Each of `energy_sources`/`device_consumption` you pass replaces the
    entire corresponding saved list — it is not merged with what's already
    there. Use `energy_add_grid_consumption`, `energy_add_grid_return`,
    `energy_add_solar_source`, `energy_add_battery_source` or
    `energy_remove_energy_source` for incremental edits instead of
    hand-building the full list. `currency`/`energy_per_unit` are kept only
    for backward compatibility and are rejected: HA's currency lives in core
    config (`homeassistant.currency`), not this WS schema, and
    `energy_per_unit` does not exist in it at all.

    Use when: you already hold the full desired `energy_sources`/
    `device_consumption` list, e.g. from `energy_get_energy_prefs`.
    Not for: adding or removing one source — use the `energy_add_*`/
    `energy_remove_energy_source` helpers, which read-modify-write for you.
    Returns: `{"status": "saved", "result": ..., "fields": [...]}` on
    success.
    Errors: `{"error": "no fields to save", ...}` when both list parameters
    are omitted; `{"error": "currency and energy_per_unit are not fields of
    HA's energy/save_prefs WS schema...", ...}` when either deprecated
    parameter is passed; `{"error": str(e), ...}` when the WS call raises.
    """
    if currency is not None or energy_per_unit is not None:
        return {
            "error": "currency and energy_per_unit are not fields of HA's energy/save_prefs WS schema; nothing was saved",
            "hint": "currency is core config (homeassistant.currency), not an Energy Dashboard pref; these parameters are deprecated and ignored",
        }
    payload: dict = {}
    if energy_sources is not None:
        payload["energy_sources"] = energy_sources
    if device_consumption is not None:
        payload["device_consumption"] = device_consumption
    if not payload:
        return {"error": "no fields to save", "hint": "pass at least one of energy_sources, device_consumption"}
    try:
        result = ha._ws_call("energy/save_prefs", **payload)
        return {"status": "saved", "result": result, "fields": list(payload.keys())}
    except Exception as e:
        return {"error": str(e), "fields": list(payload.keys())}


@mcp.tool(annotations=read("Get energy dashboard info"))
def get_energy_info() -> dict:
    """Return Energy Dashboard info such as cost sensors and currency.

    Calls the WebSocket endpoint `energy/info` and returns its payload
    unchanged.

    Use when: you need metadata about the Energy Dashboard (available cost
    sensor types, currency) rather than the source configuration itself.
    Not for: the actual source/device list — use `energy_get_energy_prefs`.
    Returns: dict as returned by HA's `energy/info` WS command.
    Errors: `{"error": str(e)}` when the WS call raises.
    """
    try:
        return ha._ws_call("energy/info")
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=read("Validate energy dashboard configuration"))
def validate_energy_prefs() -> dict:
    """Validate the currently saved Energy Dashboard configuration.

    Calls the WebSocket endpoint `energy/validate`, which checks the saved
    `energy_sources`/`device_consumption` for issues (e.g. a statistic ID
    that no longer exists) without changing anything.

    Use when: checking whether the current Energy Dashboard setup has
    issues, e.g. after editing sources with `energy_save_energy_prefs` or
    one of the `energy_add_*` helpers.
    Not for: reading the configuration itself — use
    `energy_get_energy_prefs`.
    Returns: dict as returned by HA's `energy/validate` WS command, listing
    any detected issues per source.
    Errors: `{"error": str(e)}` when the WS call raises.
    """
    try:
        return ha._ws_call("energy/validate")
    except Exception as e:
        return {"error": str(e)}


# --- Convenience helpers (read-modify-write on top of get_prefs / save_prefs) ---


def _current_sources() -> tuple[dict, list[dict]]:
    """Internal: fetch full prefs and return (prefs, energy_sources_copy)."""
    prefs = ha._ws_call("energy/get_prefs") or {}
    sources = list(prefs.get("energy_sources") or [])
    return prefs, sources


@mcp.tool(annotations=write("Add grid consumption flow", idempotent=False))
def add_grid_consumption(
    stat_energy_from: Annotated[
        str,
        Field(
            description=(
                "Statistic/entity ID of the grid-import energy sensor to "
                "add, e.g. 'sensor.grid_energy_import'. Discover candidates "
                "with `statistics_list_statistic_ids`."
            )
        ),
    ],
    stat_cost: Annotated[
        str | None,
        Field(
            description=(
                "Optional statistic/entity ID of the matching cost sensor "
                "for this flow, e.g. 'sensor.grid_cost'. Omit if no cost "
                "sensor should be linked to this flow."
            )
        ),
    ] = None,
) -> dict:
    """Add a grid consumption flow to the Energy Dashboard's grid source.

    Reads the current preferences via `ha._ws_call("energy/get_prefs")`,
    appends `stat_energy_from` (and `stat_cost` if given) as a new
    `flow_from` entry on the existing grid source, creating that grid
    source first if none exists yet, then saves the updated list via WS
    `energy/save_prefs`. Calling it twice with the same `stat_energy_from`
    adds a second, duplicate flow entry.

    Use when: registering a grid-import sensor as an Energy Dashboard
    consumption flow.
    Not for: grid export/solar compensation — use
    `energy_add_grid_return`; for a full source-list replacement use
    `energy_save_energy_prefs`.
    Returns: `{"status": "added", "stat_energy_from": ..., "result": ...}`.
    Errors: `{"error": str(e), "stat_energy_from": ...}` when either WS call
    raises.
    """
    try:
        _, sources = _current_sources()
        flow: dict = {"stat_energy_from": stat_energy_from}
        if stat_cost:
            flow["stat_cost"] = stat_cost
        grid = next((s for s in sources if s.get("type") == "grid"), None)
        if grid is None:
            grid = {"type": "grid", "flow_from": [flow], "flow_to": [], "cost_adjustment_day": 0}
            sources.append(grid)
        else:
            grid.setdefault("flow_from", []).append(flow)
        result = ha._ws_call("energy/save_prefs", energy_sources=sources)
        return {"status": "added", "stat_energy_from": stat_energy_from, "result": result}
    except Exception as e:
        return {"error": str(e), "stat_energy_from": stat_energy_from}


@mcp.tool(annotations=write("Add grid return flow", idempotent=False))
def add_grid_return(
    stat_energy_to: Annotated[
        str,
        Field(
            description=(
                "Statistic/entity ID of the grid-export energy sensor to "
                "add, e.g. 'sensor.solar_export'. Discover candidates with "
                "`statistics_list_statistic_ids`."
            )
        ),
    ],
    stat_compensation: Annotated[
        str | None,
        Field(
            description=(
                "Optional statistic/entity ID of the matching compensation/"
                "cost sensor for this export flow. Omit if no compensation "
                "sensor should be linked."
            )
        ),
    ] = None,
) -> dict:
    """Add a grid return (export) flow to the Energy Dashboard's grid source.

    Reads the current preferences via `ha._ws_call("energy/get_prefs")`,
    appends `stat_energy_to` (and `stat_compensation` if given) as a new
    `flow_to` entry on the existing grid source, creating that grid source
    first if none exists yet, then saves via WS `energy/save_prefs`. Calling
    it twice with the same `stat_energy_to` adds a second, duplicate entry.

    Use when: registering a solar-export or other grid-return sensor, e.g.
    solar sold back to the grid.
    Not for: grid import — use `energy_add_grid_consumption`; for a full
    source-list replacement use `energy_save_energy_prefs`.
    Returns: `{"status": "added", "stat_energy_to": ..., "result": ...}`.
    Errors: `{"error": str(e), "stat_energy_to": ...}` when either WS call
    raises.
    """
    try:
        _, sources = _current_sources()
        flow: dict = {"stat_energy_to": stat_energy_to}
        if stat_compensation:
            flow["stat_compensation"] = stat_compensation
        grid = next((s for s in sources if s.get("type") == "grid"), None)
        if grid is None:
            grid = {"type": "grid", "flow_from": [], "flow_to": [flow], "cost_adjustment_day": 0}
            sources.append(grid)
        else:
            grid.setdefault("flow_to", []).append(flow)
        result = ha._ws_call("energy/save_prefs", energy_sources=sources)
        return {"status": "added", "stat_energy_to": stat_energy_to, "result": result}
    except Exception as e:
        return {"error": str(e), "stat_energy_to": stat_energy_to}


@mcp.tool(annotations=write("Add solar production source", idempotent=False))
def add_solar_source(
    stat_energy_from: Annotated[
        str,
        Field(
            description=(
                "Statistic/entity ID of the solar production sensor to add, "
                "e.g. 'sensor.solar_production'. Discover candidates with "
                "`statistics_list_statistic_ids`."
            )
        ),
    ],
    config_entry_solar_forecast: Annotated[
        str | None,
        Field(
            description=(
                "Optional config entry ID of a solar forecast integration "
                "(e.g. Forecast.Solar) to link to this source. Omit if no "
                "forecast should be linked."
            )
        ),
    ] = None,
) -> dict:
    """Add a solar production source to the Energy Dashboard.

    Reads the current preferences, appends a new `{"type": "solar", ...}`
    entry to the `energy_sources` list, and saves it via WS
    `energy/save_prefs`. Calling it twice with the same `stat_energy_from`
    adds a second, duplicate source.

    Use when: registering a solar production sensor as a new Energy
    Dashboard source.
    Not for: an existing source's export flow — use
    `energy_add_grid_return`; for a full source-list replacement use
    `energy_save_energy_prefs`.
    Returns: `{"status": "added", "stat_energy_from": ..., "result": ...}`.
    Errors: `{"error": str(e), "stat_energy_from": ...}` when either WS call
    raises.
    """
    try:
        _, sources = _current_sources()
        src: dict = {"type": "solar", "stat_energy_from": stat_energy_from}
        if config_entry_solar_forecast:
            src["config_entry_solar_forecast"] = config_entry_solar_forecast
        sources.append(src)
        result = ha._ws_call("energy/save_prefs", energy_sources=sources)
        return {"status": "added", "stat_energy_from": stat_energy_from, "result": result}
    except Exception as e:
        return {"error": str(e), "stat_energy_from": stat_energy_from}


@mcp.tool(annotations=write("Add battery source", idempotent=False))
def add_battery_source(
    stat_energy_from: Annotated[
        str,
        Field(
            description=(
                "Statistic/entity ID of the battery discharge (energy out) "
                "sensor to add, e.g. 'sensor.battery_discharge'."
            )
        ),
    ],
    stat_energy_to: Annotated[
        str,
        Field(
            description=(
                "Statistic/entity ID of the battery charge (energy in) "
                "sensor to add, e.g. 'sensor.battery_charge'."
            )
        ),
    ],
) -> dict:
    """Add a battery source to the Energy Dashboard.

    Reads the current preferences, appends a new `{"type": "battery", ...}`
    entry combining `stat_energy_from` (discharge) and `stat_energy_to`
    (charge) to the `energy_sources` list, and saves it via WS
    `energy/save_prefs`. Calling it twice with the same arguments adds a
    second, duplicate source.

    Use when: registering a home battery's charge and discharge sensors as
    one Energy Dashboard source.
    Not for: grid or solar sources — use `energy_add_grid_consumption`/
    `energy_add_grid_return`/`energy_add_solar_source`.
    Returns: `{"status": "added", "stat_energy_from": ...,
    "stat_energy_to": ..., "result": ...}`.
    Errors: `{"error": str(e)}` when either WS call raises.
    """
    try:
        _, sources = _current_sources()
        sources.append({
            "type": "battery",
            "stat_energy_from": stat_energy_from,
            "stat_energy_to": stat_energy_to,
        })
        result = ha._ws_call("energy/save_prefs", energy_sources=sources)
        return {
            "status": "added",
            "stat_energy_from": stat_energy_from,
            "stat_energy_to": stat_energy_to,
            "result": result,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=destructive("Remove energy source", idempotent=True))
def remove_energy_source(
    stat_energy_from: Annotated[
        str,
        Field(
            description=(
                "Statistic/entity ID identifying the source to remove, as "
                "originally passed to one of the `energy_add_*` tools, e.g. "
                "'sensor.solar_production'."
            )
        ),
    ],
) -> dict:
    """Remove an energy source identified by its `stat_energy_from`.

    Reads the current preferences, drops any solar/battery/gas/water source
    whose `stat_energy_from` matches, also strips matching `flow_from`
    entries from the grid source, and saves the resulting list via WS
    `energy/save_prefs`. This replaces the whole `energy_sources` list with
    one that has the matching entries removed — anything else in the list
    is preserved as-is.

    Use when: undoing an `energy_add_solar_source`/`energy_add_battery_source`/
    `energy_add_grid_consumption` call, or dropping a source that no longer
    exists.
    Not for: replacing the whole source list at once — use
    `energy_save_energy_prefs`.
    Returns: `{"status": "removed", "removed": <count>, "stat_energy_from":
    ..., "result": ...}` on success, or `{"status": "not_found",
    "stat_energy_from": ...}` when nothing matched.
    Errors: `{"error": str(e), "stat_energy_from": ...}` when either WS call
    raises.
    """
    try:
        _, sources = _current_sources()
        new_sources: list[dict] = []
        removed = 0
        for s in sources:
            stype = s.get("type")
            if stype in ("solar", "battery", "gas", "water") and s.get("stat_energy_from") == stat_energy_from:
                removed += 1
                continue
            if stype == "grid":
                flow_from = s.get("flow_from") or []
                kept = [f for f in flow_from if f.get("stat_energy_from") != stat_energy_from]
                if len(kept) != len(flow_from):
                    removed += len(flow_from) - len(kept)
                    s = {**s, "flow_from": kept}
            new_sources.append(s)
        if removed == 0:
            return {"status": "not_found", "stat_energy_from": stat_energy_from}
        result = ha._ws_call("energy/save_prefs", energy_sources=new_sources)
        return {"status": "removed", "removed": removed, "stat_energy_from": stat_energy_from, "result": result}
    except Exception as e:
        return {"error": str(e), "stat_energy_from": stat_energy_from}
