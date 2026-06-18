"""Long-term statistics tools — HA recorder data (daily/weekly/monthly aggregates).

Distinct from history (short-term raw state changes): statistics are stored
by the recorder as pre-aggregated sum/mean/min/max values, retained indefinitely.
Useful for energy analysis, temperature trends, and sensor summaries.
"""
from __future__ import annotations

import datetime
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("statistics")


def _iso(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


def _parse_dt(s: str) -> datetime.datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")


@mcp.tool()
def list_statistic_ids(statistic_type: str = "sum") -> list[dict]:
    """List all available statistic IDs (sensor entities tracked by recorder).

    statistic_type: 'sum' (counters: energy, gas, water) or 'mean' (sensors: temperature, humidity).
    Returns id, name, source, unit_of_measurement.
    """
    if statistic_type not in ("sum", "mean"):
        return [{"error": "statistic_type must be 'sum' or 'mean'"}]
    try:
        result = ha._ws_call("recorder/list_statistic_ids", statistic_type=statistic_type)
        if not isinstance(result, list):
            return [{"error": str(result)}]
        return [
            {
                "statistic_id": r.get("statistic_id"),
                "name": r.get("name"),
                "source": r.get("source"),
                "unit": r.get("unit_of_measurement"),
                "has_sum": r.get("has_sum", False),
                "has_mean": r.get("has_mean", False),
            }
            for r in result
        ]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def get_statistics(
    statistic_ids: list[str],
    start: str,
    end: str | None = None,
    period: str = "day",
    types: list[str] | None = None,
) -> dict:
    """Get aggregated statistics for one or more entities over a time range.

    Args:
        statistic_ids: list of entity IDs or statistic IDs, e.g. ["sensor.energy_consumption"]
        start: start date/time, e.g. "2024-01-01" or "2024-01-01T00:00:00"
        end: end date/time (defaults to now)
        period: aggregation period — "hour", "day", "week", "month", "5minute"
        types: which aggregates to return — any of ["sum", "mean", "min", "max", "state", "change"]
                defaults to ["sum", "mean", "min", "max"]

    Returns dict of {statistic_id: [{"start": ..., "sum": ..., "mean": ..., ...}]}.
    """
    if not statistic_ids:
        return {"error": "statistic_ids must be a non-empty list"}
    if period not in ("5minute", "hour", "day", "week", "month"):
        return {"error": "period must be one of: 5minute, hour, day, week, month"}

    allowed_types = {"sum", "mean", "min", "max", "state", "change"}
    req_types = types or ["sum", "mean", "min", "max"]
    bad = [t for t in req_types if t not in allowed_types]
    if bad:
        return {"error": f"Invalid types: {bad}. Allowed: {sorted(allowed_types)}"}

    try:
        start_dt = _parse_dt(start)
    except ValueError as e:
        return {"error": str(e)}

    end_dt = datetime.datetime.now(datetime.timezone.utc) if not end else None
    if end:
        try:
            end_dt = _parse_dt(end)
        except ValueError as e:
            return {"error": str(e)}

    try:
        raw = ha._ws_call(
            "recorder/statistics_during_period",
            start_time=_iso(start_dt),
            end_time=_iso(end_dt),
            statistic_ids=statistic_ids,
            period=period,
            types=req_types,
            units={},
        )
    except Exception as e:
        return {"error": str(e)}

    if not isinstance(raw, dict):
        return {"error": f"Unexpected response: {raw!r}"}

    # Convert epoch timestamps → ISO strings for readability
    result = {}
    for sid, rows in raw.items():
        clean_rows = []
        for row in (rows or []):
            entry = {}
            if "start" in row:
                try:
                    entry["start"] = datetime.datetime.fromtimestamp(
                        row["start"], tz=datetime.timezone.utc
                    ).isoformat()
                except Exception:
                    entry["start"] = row["start"]
            for key in ("sum", "mean", "min", "max", "state", "change"):
                if key in row and row[key] is not None:
                    entry[key] = round(row[key], 4)
            clean_rows.append(entry)
        result[sid] = clean_rows

    return {
        "period": period,
        "start": _iso(start_dt),
        "end": _iso(end_dt),
        "types": req_types,
        "data": result,
    }


@mcp.tool()
def get_energy_statistics(days: int = 30, period: str = "day") -> dict:
    """Get sum statistics for all energy-related sensors (kWh, gas m³) for the last N days.

    Convenience wrapper around get_statistics — auto-discovers 'sum' statistic IDs.
    Useful for EON energy consumption analysis.
    """
    if days < 1 or days > 365:
        return {"error": "days must be between 1 and 365"}

    try:
        all_stats = ha._ws_call("recorder/list_statistic_ids", statistic_type="sum")
        if not isinstance(all_stats, list):
            return {"error": f"Unexpected response: {all_stats!r}"}
    except Exception as e:
        return {"error": str(e)}

    # Filter for energy-related units
    energy_units = {"kWh", "MWh", "Wh", "m³", "ft³", "L", "gal", "W"}
    ids = [
        r["statistic_id"]
        for r in all_stats
        if r.get("has_sum") and r.get("unit_of_measurement") in energy_units
    ]

    if not ids:
        return {"message": "No energy statistic IDs found (check that recorder is enabled)"}

    start_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    end_dt = datetime.datetime.now(datetime.timezone.utc)

    try:
        raw = ha._ws_call(
            "recorder/statistics_during_period",
            start_time=_iso(start_dt),
            end_time=_iso(end_dt),
            statistic_ids=ids,
            period=period,
            types=["sum", "change"],
            units={},
        )
    except Exception as e:
        return {"error": str(e)}

    result = {}
    for sid, rows in (raw or {}).items():
        unit = next((r["unit_of_measurement"] for r in all_stats if r["statistic_id"] == sid), "")
        clean = []
        for row in (rows or []):
            entry = {}
            if "start" in row:
                try:
                    entry["start"] = datetime.datetime.fromtimestamp(
                        row["start"], tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d")
                except Exception:
                    entry["start"] = row["start"]
            if row.get("change") is not None:
                entry["change"] = round(row["change"], 4)
            if row.get("sum") is not None:
                entry["sum"] = round(row["sum"], 4)
            clean.append(entry)
        result[sid] = {"unit": unit, "rows": clean}

    return {
        "days": days,
        "period": period,
        "statistic_count": len(ids),
        "data": result,
    }


@mcp.tool()
def get_statistics_metadata(statistic_ids: list[str]) -> list[dict]:
    """Get metadata (unit, source, name) for specific statistic IDs."""
    try:
        result = ha._ws_call("recorder/get_statistics_metadata", statistic_ids=statistic_ids)
        if not isinstance(result, list):
            return [{"error": str(result)}]
        return [
            {
                "statistic_id": r.get("statistic_id"),
                "name": r.get("name"),
                "source": r.get("source"),
                "unit": r.get("unit_of_measurement"),
                "has_sum": r.get("has_sum"),
                "has_mean": r.get("has_mean"),
            }
            for r in result
        ]
    except Exception as e:
        return [{"error": str(e)}]
