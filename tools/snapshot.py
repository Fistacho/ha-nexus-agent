"""Aggregated HA snapshot — one call, multiple registries.

Instead of forcing the AI client to make 5+ round-trips (states + areas +
devices + entity registry + integrations…) to build context, return a single
filtered payload. Saves a lot of tokens on larger setups.
"""
from __future__ import annotations

from fastmcp import FastMCP

import ha_client as ha

mcp = FastMCP("snapshot")


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


@mcp.tool()
def get_snapshot(
    include: list[str] | None = None,
    domains: list[str] | None = None,
    area_id: str | None = None,
    summary: bool = True,
) -> dict:
    """One-shot aggregated view of HA state, filtered.

    `include` controls which sections appear (defaults to
    states/areas/devices/entities/integrations). Valid values:
    states, areas, floors, devices, entities, integrations, config.

    `domains` restricts the states list (e.g. ["light", "climate"]).
    `area_id` keeps only states whose entity/device belongs to that area.
    `summary` strips state objects down to entity_id/state/friendly_name
    (set False for full attributes — usually much larger).

    Returns: {"section_name": [...], ..., "_meta": {...}}.
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
        out["states"] = _filter_states(states, domains, area_id, entity_registry, summary)

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
        "filters": {"domains": domains, "area_id": area_id, "summary": summary},
        "counts": {k: len(v) for k, v in out.items() if isinstance(v, list)},
    }
    return out


@mcp.tool()
def get_area_snapshot(area_id: str, summary: bool = True) -> dict:
    """Convenience: full snapshot scoped to one area (states + devices + entities)."""
    return get_snapshot(
        include=["states", "devices", "entities", "areas"],
        area_id=area_id,
        summary=summary,
    )
