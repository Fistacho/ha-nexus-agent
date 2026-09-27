"""Aggregated HA snapshot — one call, multiple registries.

Instead of forcing the AI client to make 5+ round-trips (states + areas +
devices + entity registry + integrations…) to build context, return a single
filtered payload. Saves a lot of tokens on larger setups.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import read

mcp = FastMCP("snapshot")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


_DEFAULT_INCLUDE = ("states", "areas", "devices", "entities", "integrations")
_ALL_INCLUDES = (
    "states",
    "areas",
    "floors",
    "devices",
    "entities",
    "integrations",
    "config",
)


def _filter_states(
    states: list[dict],
    domains: list[str] | None,
    area_id: str | None,
    entity_ids: list[dict] | None,
    summary: bool,
    state_fields: list[str] | None = None,
) -> list[dict]:
    if domains:
        domset = set(domains)
        states = [s for s in states if s["entity_id"].split(".", 1)[0] in domset]

    if area_id and entity_ids is not None:
        device_area: dict[str, str] = {}
        entity_area: dict[str, str] = {}
        try:
            devices = ha.get_device_registry()
            for d in devices:
                if d.get("area_id"):
                    device_area[d["id"]] = d["area_id"]
        except Exception:
            pass
        for e in entity_ids:
            eid = e.get("entity_id")
            if not eid:
                continue
            aid = e.get("area_id")
            if not aid and e.get("device_id"):
                aid = device_area.get(e["device_id"])
            if aid:
                entity_area[eid] = aid
        states = [s for s in states if entity_area.get(s["entity_id"]) == area_id]

    if state_fields:
        keep = set(state_fields)

        def _project(s: dict) -> dict:
            row: dict = {}
            if "entity_id" in keep:
                row["entity_id"] = s["entity_id"]
            if "state" in keep:
                row["state"] = s["state"]
            if "friendly_name" in keep:
                row["friendly_name"] = s.get("attributes", {}).get("friendly_name")
            if "attributes" in keep:
                row["attributes"] = s.get("attributes", {})
            if "last_changed" in keep:
                row["last_changed"] = s.get("last_changed")
            if "last_updated" in keep:
                row["last_updated"] = s.get("last_updated")
            return row

        return [_project(s) for s in states]

    if summary:
        return [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "friendly_name": s.get("attributes", {}).get("friendly_name"),
            }
            for s in states
        ]
    return states


@mcp.tool(annotations=read("Get filtered HA snapshot"))
def get_snapshot(
    include: Annotated[
        list[str] | None,
        Field(
            description=(
                "Sections to return. Omit for the default "
                "states/areas/devices/entities/integrations set. Valid values: "
                "states, areas, floors, devices, entities, integrations, config."
            )
        ),
    ] = None,
    domains: Annotated[
        list[str] | None,
        Field(
            description=(
                "Entity domain filter applied to the states/entities sections, "
                "e.g. ['light', 'climate']. Omit to include every domain."
            )
        ),
    ] = None,
    area_id: Annotated[
        str | None,
        Field(
            description=(
                "Area registry ID; keeps only states/devices/entities assigned to "
                "this area, directly or via their device. Omit for no area filter."
            )
        ),
    ] = None,
    summary: Annotated[
        bool,
        Field(
            description=(
                "If true, each state is reduced to entity_id/state/friendly_name. "
                "Set false for full attributes; ignored when state_fields is given."
            )
        ),
    ] = True,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Maximum number of states to return after filtering, for "
                "pagination on large setups. Omit for no limit."
            )
        ),
    ] = None,
    offset: Annotated[
        int,
        Field(
            description=(
                "Number of filtered states to skip before applying limit, for "
                "pagination. 0 starts from the first matching state."
            )
        ),
    ] = 0,
    state_fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Explicit per-state keys to return, overriding summary. Valid: "
                "entity_id, state, friendly_name, attributes, last_changed, "
                "last_updated. Omit to use summary instead."
            )
        ),
    ] = None,
) -> dict:
    """Return a single filtered snapshot combining HA states, registries and config.

    Reads live states via `ha.get_states()` and, depending on `include`, the
    area/floor/device/entity registries and config entries over HTTP in one
    call, applies `domains`/`area_id` filtering and `limit`/`offset`
    pagination to the states list, and returns one dict keyed by section
    name. Every call in this path is a read; nothing here writes to HA.

    Use when: a client needs several registries and/or states together and
    should not spend several round-trips assembling that context.
    Not for: a single registry without filtering — use `entities_list_entities`
    or `ws_get_states` for a plain state list, or `areas_list_devices`/
    `devices_list_devices` for a plain device list.
    Returns: dict keyed by requested section (`states`, `areas`, `floors`,
    `devices`, `entities`, `integrations`, `config`), plus `_meta` (echoed
    filters and per-section counts) and `_states_total` (pre-pagination
    count) when `states` is included.
    Errors: `{"error": "unknown_include", "unknown": [...], "valid": [...]}`
    when `include` names a section outside the valid set.
    Limits: `limit`/`offset` paginate only the `states` section; every other
    section is always returned in full.
    """
    sections = list(include) if include else list(_DEFAULT_INCLUDE)
    unknown = [s for s in sections if s not in _ALL_INCLUDES]
    if unknown:
        return {"error": "unknown_include", "unknown": unknown, "valid": list(_ALL_INCLUDES)}

    out: dict[str, object] = {}
    entity_registry: list[dict] | None = None

    if "entities" in sections or area_id:
        entity_registry = ha.get_entity_registry()

    if "states" in sections:
        states = ha.get_states()
        filtered = _filter_states(states, domains, area_id, entity_registry, summary, state_fields)
        total = len(filtered)
        if offset:
            filtered = filtered[offset:]
        if limit is not None:
            filtered = filtered[:limit]
        out["states"] = filtered
        out["_states_total"] = total

    if "areas" in sections:
        out["areas"] = ha.get_area_registry()

    if "floors" in sections:
        out["floors"] = ha.get_floor_registry()

    if "devices" in sections:
        devices = ha.get_device_registry()
        if area_id:
            devices = [d for d in devices if d.get("area_id") == area_id]
        out["devices"] = devices

    if "entities" in sections and entity_registry is not None:
        ents = entity_registry
        if domains:
            domset = set(domains)
            ents = [e for e in ents if e.get("entity_id", "").split(".", 1)[0] in domset]
        if area_id:
            ents = [e for e in ents if e.get("area_id") == area_id]
        out["entities"] = ents

    if "integrations" in sections:
        out["integrations"] = ha.get_config_entries()

    if "config" in sections:
        out["config"] = ha.get_config()

    out["_meta"] = {
        "include": sections,
        "filters": {
            "domains": domains,
            "area_id": area_id,
            "summary": summary,
            "limit": limit,
            "offset": offset,
            "state_fields": state_fields,
        },
        "counts": {k: len(v) for k, v in out.items() if isinstance(v, list)},
    }
    return out


@mcp.tool(annotations=read("Get area-scoped HA snapshot"))
def get_area_snapshot(
    area_id: Annotated[
        str,
        Field(
            description=(
                "Area registry ID to scope the snapshot to; states, devices and "
                "entities outside this area are excluded."
            )
        ),
    ],
    summary: Annotated[
        bool,
        Field(
            description=(
                "If true, each state is reduced to entity_id/state/friendly_name. "
                "Set false for full attributes."
            )
        ),
    ] = True,
) -> dict:
    """Return states, devices and entities for one area in a single call.

    Convenience wrapper that calls `snapshot_get_snapshot` with
    `include=["states", "devices", "entities", "areas"]` and `area_id` fixed
    to the given area; it adds no filtering beyond `summary`.

    Use when: exploring or controlling everything in one physical area
    without composing `snapshot_get_snapshot`'s `include`/`area_id`
    arguments by hand.
    Not for: a snapshot spanning several areas or other sections — call
    `snapshot_get_snapshot` directly; entity registry rows without state
    values — use `areas_get_area_entities`; live state values without
    registry context — use `areas_get_area_states`.
    Returns: same dict shape as `snapshot_get_snapshot` with
    `include=["states", "devices", "entities", "areas"]`.
    Limits: no pagination; every state/device/entity in the area is
    returned.
    """
    return get_snapshot(
        include=["states", "devices", "entities", "areas"],
        area_id=area_id,
        summary=summary,
    )
