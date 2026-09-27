"""Long-term statistics tools — HA recorder data (daily/weekly/monthly aggregates).

Distinct from history (short-term raw state changes): statistics are stored
by the recorder as pre-aggregated sum/mean/min/max values, retained indefinitely.
Useful for energy analysis, temperature trends, and sensor summaries.
"""
from __future__ import annotations

import datetime
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import read

mcp = FastMCP("statistics")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


def _iso(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


def _parse_dt(s: str) -> datetime.datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            # DTZ007: strptime() here is intentionally naive for the two formats
            # with no %z -- the very next check backfills UTC on any naive
            # result, so the function never returns a naive datetime (see
            # tests/test_findings_ci_b.py::test_parse_dt_never_returns_naive_datetime).
            # A caller-supplied offset (third format, %z/'Z') is preserved as-is
            # instead of being overridden, matching this module's documented
            # "parsed into UTC timestamps" contract (get_statistics docstring)
            # and the same UTC-assumption convention already used by
            # ha_client.get_history() for HA REST/WS calls.
            dt = datetime.datetime.strptime(s, fmt)  # noqa: DTZ007
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")


@mcp.tool(annotations=read("List recorder statistic IDs"))
def list_statistic_ids(
    statistic_type: Annotated[
        str,
        Field(
            description=(
                "Which kind of statistic to list: 'sum' for counters (energy, "
                "gas, water) or 'mean' for sensors (temperature, humidity)."
            )
        ),
    ] = "sum",
) -> list[dict]:
    """List recorder statistic IDs of the requested type.

    Calls WS `recorder/list_statistic_ids` and returns one entry per
    statistic the recorder tracks, with its source and unit.

    Use when: discovering which entities have long-term statistics before
    calling `statistics_get_statistics` or `statistics_get_statistics_metadata`.
    Not for: raw short-term state history — use
    `history_get_state_history` instead.
    Returns: list of `{"statistic_id", "name", "source", "unit", "has_sum",
    "has_mean"}` dicts.
    Errors: `[{"error": "..."}]` when `statistic_type` is not 'sum'/'mean',
    when the WS response isn't a list, or when the WS call raises.
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


@mcp.tool(annotations=read("Get aggregated statistics for a time range"))
def get_statistics(
    statistic_ids: Annotated[
        list[str],
        Field(
            description=(
                "Entity or statistic IDs to fetch, e.g. "
                "['sensor.energy_consumption']. Discover valid values with "
                "`statistics_list_statistic_ids`."
            )
        ),
    ],
    start: Annotated[
        str,
        Field(
            description=(
                "Start of the range: a date ('2024-01-01') or datetime "
                "('2024-01-01T00:00:00')."
            )
        ),
    ],
    end: Annotated[
        str | None,
        Field(
            description=(
                "End of the range, same formats as `start`. Omit to use the "
                "current time."
            )
        ),
    ] = None,
    period: Annotated[
        str,
        Field(
            description=(
                "Aggregation bucket size: one of '5minute', 'hour', 'day', "
                "'week', 'month'."
            )
        ),
    ] = "day",
    types: Annotated[
        list[str] | None,
        Field(
            description=(
                "Which aggregates to include per bucket, any of 'sum', "
                "'mean', 'min', 'max', 'state', 'change'. Omit to use "
                "['sum', 'mean', 'min', 'max']."
            )
        ),
    ] = None,
) -> dict:
    """Get aggregated recorder statistics for one or more IDs over a time range.

    Calls WS `recorder/statistics_during_period` with `start`/`end` parsed
    into UTC timestamps, converts each row's epoch `start` back to an ISO
    string, and rounds numeric aggregates to 4 decimals.

    Use when: analysing energy/temperature/etc. trends over hours, days,
    weeks or months using the recorder's pre-aggregated data.
    Not for: raw short-term state changes between two points in time — use
    `history_get_state_history` instead.
    Returns: `{"period", "start", "end", "types", "data": {statistic_id:
    [{"start": <iso>, "sum": ..., "mean": ..., ...}, ...]}}`.
    Errors: `{"error": "..."}` for an empty `statistic_ids`, an invalid
    `period`/`types` value, an unparsable `start`/`end`, a non-dict WS
    response, or when the WS call raises.
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


@mcp.tool(annotations=read("Get energy sum statistics for recent days"))
def get_energy_statistics(
    days: Annotated[
        int,
        Field(
            description=(
                "Number of days back from now to include, between 1 and "
                "365."
            )
        ),
    ] = 30,
    period: Annotated[
        str,
        Field(
            description=(
                "Aggregation bucket size passed to the underlying "
                "statistics query, e.g. 'day', 'week', 'month'."
            )
        ),
    ] = "day",
) -> dict:
    """Get sum statistics for every energy-related sensor over the last N days.

    Auto-discovers 'sum' statistic IDs via WS `recorder/list_statistic_ids`,
    keeps only those whose unit is an energy/volume unit (kWh, MWh, Wh, m³,
    ft³, L, gal — instantaneous power sensors in 'W' are excluded since a
    'sum' statistic on a power sensor is not a meaningful energy value),
    then fetches `sum`/`change` via WS `recorder/statistics_during_period`
    for the requested window. This is a convenience wrapper around
    `statistics_get_statistics`.

    Use when: a quick overview of overall energy consumption trends is
    needed without first listing statistic IDs by hand.
    Not for: a hand-picked set of statistic IDs or non-energy sensors — use
    `statistics_get_statistics` directly.
    Returns: `{"days", "period", "statistic_count", "data": {statistic_id:
    {"unit": ..., "rows": [{"start": <date>, "change": ..., "sum": ...},
    ...]}}}`, or `{"message": "..."}` when no energy statistic IDs are
    found.
    Errors: `{"error": "days must be between 1 and 365"}` for an
    out-of-range `days`; `{"error": "..."}` when either WS call raises or
    returns an unexpected shape.
    """
    if days < 1 or days > 365:
        return {"error": "days must be between 1 and 365"}

    try:
        all_stats = ha._ws_call("recorder/list_statistic_ids", statistic_type="sum")
        if not isinstance(all_stats, list):
            return {"error": f"Unexpected response: {all_stats!r}"}
    except Exception as e:
        return {"error": str(e)}

    # Filter for energy-related units. 'W' (instantaneous power) is deliberately
    # excluded — it's not an energy unit and recorder 'sum' statistics for a
    # power sensor are not meaningful energy consumption.
    energy_units = {"kWh", "MWh", "Wh", "m³", "ft³", "L", "gal"}
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


@mcp.tool(annotations=read("Get statistics metadata"))
def get_statistics_metadata(
    statistic_ids: Annotated[
        list[str],
        Field(
            description=(
                "Statistic IDs to fetch metadata for, e.g. "
                "['sensor.energy_consumption']. Discover valid values with "
                "`statistics_list_statistic_ids`."
            )
        ),
    ],
) -> list[dict]:
    """Get metadata (unit, source, name) for specific recorder statistic IDs.

    Calls WS `recorder/get_statistics_metadata` and returns one entry per
    requested ID.

    Use when: you already know the statistic IDs and only need their
    unit/source/name, without their historical values.
    Not for: the actual aggregated values — use `statistics_get_statistics`.
    Returns: list of `{"statistic_id", "name", "source", "unit", "has_sum",
    "has_mean"}` dicts.
    Errors: `[{"error": "..."}]` when the WS response isn't a list or the
    WS call raises.
    """
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
