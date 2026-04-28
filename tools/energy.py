from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("energy")


# --- Raw Energy Dashboard prefs (WebSocket: energy/*) ---


@mcp.tool()
def get_energy_prefs() -> dict:
    """Return the full Home Assistant Energy Dashboard preferences (WS `energy/get_prefs`)."""
    try:
        return ha._ws_call("energy/get_prefs")
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def save_energy_prefs(
    energy_sources: list[dict] | None = None,
    device_consumption: list[dict] | None = None,
    currency: str | None = None,
    energy_per_unit: float | None = None,
) -> dict:
    """Save Energy Dashboard preferences (WS `energy/save_prefs`); only the fields you pass are sent."""
    payload: dict = {}
    if energy_sources is not None:
        payload["energy_sources"] = energy_sources
    if device_consumption is not None:
        payload["device_consumption"] = device_consumption
    if currency is not None:
        payload["currency"] = currency
    if energy_per_unit is not None:
        payload["energy_per_unit"] = energy_per_unit
    if not payload:
        return {"error": "no fields to save", "hint": "pass at least one of energy_sources, device_consumption, currency, energy_per_unit"}
    try:
        result = ha._ws_call("energy/save_prefs", **payload)
        return {"status": "saved", "result": result, "fields": list(payload.keys())}
    except Exception as e:
        return {"error": str(e), "fields": list(payload.keys())}


@mcp.tool()
def get_energy_info() -> dict:
    """Return Energy Dashboard info (cost sensors, currency, etc.) via WS `energy/info`."""
    try:
        return ha._ws_call("energy/info")
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def validate_energy_prefs() -> dict:
    """Validate the current Energy Dashboard configuration (WS `energy/validate`)."""
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


@mcp.tool()
def add_grid_consumption(stat_energy_from: str, stat_cost: str | None = None) -> dict:
    """Add a grid consumption flow (`flow_from`) to the Energy Dashboard's grid source, creating the grid source if needed."""
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


@mcp.tool()
def add_grid_return(stat_energy_to: str, stat_compensation: str | None = None) -> dict:
    """Add a grid return flow (`flow_to`, e.g. solar export) to the Energy Dashboard's grid source."""
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


@mcp.tool()
def add_solar_source(stat_energy_from: str, config_entry_solar_forecast: str | None = None) -> dict:
    """Add a solar production source to the Energy Dashboard."""
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


@mcp.tool()
def add_battery_source(stat_energy_from: str, stat_energy_to: str) -> dict:
    """Add a battery source to the Energy Dashboard (both charge `stat_energy_to` and discharge `stat_energy_from`)."""
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


@mcp.tool()
def remove_energy_source(stat_energy_from: str) -> dict:
    """Remove an energy source identified by its `stat_energy_from` (also strips matching grid `flow_from` entries)."""
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
