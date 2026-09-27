from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import read

mcp = FastMCP("search")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


def _score(query: str, target: str | None) -> float:
    """Fuzzy match score between query and target (0..1). Substring match scores 1.0."""
    if not target:
        return 0.0
    q, t = query.lower(), target.lower()
    if not q:
        return 0.0
    if q in t:
        return 1.0
    return SequenceMatcher(None, q, t).ratio()


def _best_score(query: str, targets: list[str | None]) -> float:
    """Return the best fuzzy score across multiple candidate strings."""
    best = 0.0
    for t in targets:
        s = _score(query, t)
        if s > best:
            best = s
    return best


def _safe_ws(msg_type: str, **kwargs) -> list[dict]:
    """Call a WS command; return [] on failure to keep search tolerant."""
    try:
        result = ha._ws_call(msg_type, **kwargs)
        return result if isinstance(result, list) else []
    except Exception:
        return []


@mcp.tool(annotations=read("Search entities"))
def search_entities(
    query: Annotated[str, Field(description="Search text to fuzzy-match against entity_id, friendly_name and device_id.")],
    limit: Annotated[int, Field(description="Maximum number of ranked results to return; defaults to 20.")] = 20,
) -> list[dict]:
    """Fuzzy-rank entities by entity_id, friendly_name and device_id.

    Scores every entity against `query` with substring match (score 1.0)
    or a sequence-similarity ratio, keeps only scores above 0, and returns
    the top `limit` matches sorted descending.

    Use when: you don't know the exact entity_id and want a ranked guess
    from a name fragment.
    Returns: list of dicts (entity_id, friendly_name, state, device_id,
    score) up to `limit` entries.
    Limits: entities that score exactly 0 are dropped entirely, not just
    deprioritised."""
    states = ha.get_states() or []
    results: list[dict] = []
    for s in states:
        entity_id = s.get("entity_id", "")
        attrs = s.get("attributes") or {}
        friendly = attrs.get("friendly_name")
        device_id = attrs.get("device_id")
        score = _best_score(query, [entity_id, friendly, device_id])
        if score > 0:
            results.append({
                "entity_id": entity_id,
                "friendly_name": friendly,
                "state": s.get("state"),
                "device_id": device_id,
                "score": score,
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


@mcp.tool(annotations=read("Search devices"))
def search_devices(
    query: Annotated[str, Field(description="Search text to fuzzy-match against a device's name, name_by_user, manufacturer and model.")],
    limit: Annotated[int, Field(description="Maximum number of ranked results to return; defaults to 20.")] = 20,
) -> list[dict]:
    """Fuzzy-rank devices by name, name_by_user, manufacturer and model.

    Scores every device from the device registry against `query`, keeps
    only scores above 0, and returns the top `limit` matches sorted
    descending.

    Use when: you don't know a device's exact id and want a ranked guess
    from a name fragment.
    Not for: an exact, unranked device list — use `devices_list_devices`,
    `areas_list_devices` or `devices_list_devices_in_area`.
    Returns: list of dicts (id, name, name_by_user, manufacturer, model,
    area_id, score) up to `limit` entries.
    Limits: devices that score exactly 0 are dropped entirely; a WS
    failure yields an empty list rather than an error."""
    devices = _safe_ws("config/device_registry/list")
    results: list[dict] = []
    for d in devices:
        name = d.get("name")
        name_by_user = d.get("name_by_user")
        manufacturer = d.get("manufacturer")
        model = d.get("model")
        score = _best_score(query, [name, name_by_user, manufacturer, model])
        if score > 0:
            results.append({
                "id": d.get("id"),
                "name": name,
                "name_by_user": name_by_user,
                "manufacturer": manufacturer,
                "model": model,
                "area_id": d.get("area_id"),
                "score": score,
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


@mcp.tool(annotations=read("Search areas"))
def search_areas(
    query: Annotated[str, Field(description="Search text to fuzzy-match against area names.")],
    limit: Annotated[int, Field(description="Maximum number of ranked results to return; defaults to 20.")] = 20,
) -> list[dict]:
    """Fuzzy-rank areas by name.

    Scores every area from the area registry against `query`, keeps only
    scores above 0, and returns the top `limit` matches sorted descending.

    Use when: you don't know an area's exact id and want a ranked guess
    from a name fragment.
    Not for: an exact, unranked area list — use `areas_list_areas`.
    Returns: list of dicts (area_id, name, floor_id, icon, score) up to
    `limit` entries.
    Limits: areas that score exactly 0 are dropped entirely; a WS failure
    yields an empty list rather than an error."""
    areas = _safe_ws("config/area_registry/list")
    results: list[dict] = []
    for a in areas:
        name = a.get("name")
        score = _score(query, name)
        if score > 0:
            results.append({
                "area_id": a.get("area_id"),
                "name": name,
                "floor_id": a.get("floor_id"),
                "icon": a.get("icon"),
                "score": score,
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


@mcp.tool(annotations=read("Search entities, devices and areas"))
def deep_search(
    query: Annotated[str, Field(description="Search text to fuzzy-match across entities, devices and areas.")],
    limit: Annotated[int, Field(description="Maximum number of ranked results to return; defaults to 20.")] = 20,
) -> list[dict]:
    """Fuzzy-search entities, devices and areas together in one ranked list.

    Scores states (tagging automation.* entities as kind 'automation',
    others as 'entity'), devices and areas against `query` the same way as
    the single-kind search tools, merges all matches, and returns the top
    `limit` sorted descending.

    Use when: you don't know whether what you're looking for is an entity,
    device or area.
    Not for: results scoped to one kind — use `search_search_entities`,
    `search_search_devices` or `search_search_areas`.
    Returns: list of dicts tagged with `kind` ('entity'/'automation'/
    'device'/'area'), each with kind-specific fields plus `score`, up to
    `limit` entries."""
    combined: list[dict] = []

    states = ha.get_states() or []
    for s in states:
        entity_id = s.get("entity_id", "")
        attrs = s.get("attributes") or {}
        friendly = attrs.get("friendly_name")
        kind = "automation" if entity_id.startswith("automation.") else "entity"
        score = _best_score(query, [entity_id, friendly])
        if score > 0:
            combined.append({
                "kind": kind,
                "entity_id": entity_id,
                "friendly_name": friendly,
                "state": s.get("state"),
                "score": score,
            })

    devices = _safe_ws("config/device_registry/list")
    for d in devices:
        score = _best_score(query, [d.get("name"), d.get("name_by_user"), d.get("manufacturer"), d.get("model")])
        if score > 0:
            combined.append({
                "kind": "device",
                "id": d.get("id"),
                "name": d.get("name"),
                "name_by_user": d.get("name_by_user"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "score": score,
            })

    areas = _safe_ws("config/area_registry/list")
    for a in areas:
        score = _score(query, a.get("name"))
        if score > 0:
            combined.append({
                "kind": "area",
                "area_id": a.get("area_id"),
                "name": a.get("name"),
                "floor_id": a.get("floor_id"),
                "score": score,
            })

    combined.sort(key=lambda r: r["score"], reverse=True)
    return combined[:limit]


@mcp.tool(annotations=read("Find related entities and registry context"))
def find_related(
    entity_id: Annotated[str, Field(description="Entity ID to look up, e.g. 'light.kitchen'; must exist in the entity registry.")],
) -> dict:
    """Return an entity's device, area, floor and sibling entities.

    Looks up `entity_id` in the entity registry, resolves its device
    (falling back to the device's area if the entity itself has none) and
    area/floor, then collects other entities on the same device and other
    entities/devices in the same area.

    Use when: exploring what else is connected to one entity before
    changing or troubleshooting it.
    Returns: dict with entity_id, device_id, area_id, floor_id, device,
    area, same_device_entities, same_area_entities.
    Errors: `{"entity_id": ..., "error": "entity not found in registry"}`
    if the entity isn't in the registry, or `{"entity_id": ..., "error":
    "<message>"}` on a WS failure."""
    try:
        entity_registry = ha._ws_call("config/entity_registry/list") or []
    except Exception as e:
        return {"entity_id": entity_id, "error": str(e)}

    target = next((e for e in entity_registry if e.get("entity_id") == entity_id), None)
    if target is None:
        return {"entity_id": entity_id, "error": "entity not found in registry"}

    device_id = target.get("device_id")
    area_id = target.get("area_id")

    devices = _safe_ws("config/device_registry/list")
    areas = _safe_ws("config/area_registry/list")

    device = next((d for d in devices if d.get("id") == device_id), None) if device_id else None
    if device and not area_id:
        area_id = device.get("area_id")
    area = next((a for a in areas if a.get("area_id") == area_id), None) if area_id else None
    floor_id = area.get("floor_id") if area else None

    same_device = [
        {"entity_id": e.get("entity_id"), "name": e.get("name") or e.get("original_name")}
        for e in entity_registry
        if device_id and e.get("device_id") == device_id and e.get("entity_id") != entity_id
    ]

    device_ids_in_area = {d.get("id") for d in devices if d.get("area_id") == area_id} if area_id else set()
    same_area = [
        {"entity_id": e.get("entity_id"), "name": e.get("name") or e.get("original_name")}
        for e in entity_registry
        if e.get("entity_id") != entity_id
        and (
            (area_id and e.get("area_id") == area_id)
            or (e.get("device_id") in device_ids_in_area)
        )
    ]

    return {
        "entity_id": entity_id,
        "device_id": device_id,
        "area_id": area_id,
        "floor_id": floor_id,
        "device": device,
        "area": area,
        "same_device_entities": same_device,
        "same_area_entities": same_area,
    }


def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


@mcp.tool(annotations=read("Find possibly unused entities"))
def find_unused_entities() -> list[dict]:
    """Heuristically list entities that look unused or orphaned.

    Flags entities whose state has been 'unavailable' or 'unknown' for
    more than 7 days (or has no `last_changed` at all), plus any
    entity-registry entries with no `device_id` — which normally means
    helpers, automations and template entities, not proof of disuse.

    Use when: hunting for cleanup candidates, always reviewing each result
    before deleting anything.
    Returns: list of dicts (entity_id, state, last_changed, device_id,
    reasons) — `reasons` explains which heuristic(s) matched.
    Limits: purely heuristic; the 7-day unavailability threshold and a
    missing device_id are weak signals, not confirmation that an entity is
    truly unused."""
    states = ha.get_states() or []
    try:
        entity_registry = ha._ws_call("config/entity_registry/list") or []
    except Exception:
        entity_registry = []
    registry_by_id = {e.get("entity_id"): e for e in entity_registry}

    now = datetime.now(timezone.utc)
    threshold_days = 7
    results: list[dict] = []
    for s in states:
        entity_id = s.get("entity_id", "")
        state_val = (s.get("state") or "").lower()
        last_changed = _parse_iso(s.get("last_changed"))
        reg = registry_by_id.get(entity_id, {})
        device_id = reg.get("device_id") or (s.get("attributes") or {}).get("device_id")
        reasons: list[str] = []

        if state_val in ("unavailable", "unknown"):
            if last_changed is not None:
                age_days = (now - last_changed).total_seconds() / 86400
                if age_days > threshold_days:
                    reasons.append(f"{state_val} for {age_days:.1f} days")
            else:
                reasons.append(f"{state_val} (no last_changed)")

        if reg and not device_id:
            reasons.append("orphan (no device_id)")

        if reasons:
            results.append({
                "entity_id": entity_id,
                "state": s.get("state"),
                "last_changed": s.get("last_changed"),
                "device_id": device_id,
                "reasons": reasons,
            })
    return results


@mcp.tool(annotations=read("Find orphan devices"))
def find_orphan_devices() -> list[dict]:
    """List devices with no entities associated with them in the entity registry.

    Compares every device's id against the set of `device_id` values
    referenced by entity-registry entries and returns devices that no
    entity points to.

    Use when: hunting for device-registry entries that may be safe to
    remove, always reviewing each result first.
    Returns: list of dicts (id, name, name_by_user, manufacturer, model,
    area_id, config_entries) for devices with no referencing entity.
    Limits: a WS failure on either registry silently yields an empty
    underlying list rather than an error, which can under-report orphans."""
    devices = _safe_ws("config/device_registry/list")
    try:
        entity_registry = ha._ws_call("config/entity_registry/list") or []
    except Exception:
        entity_registry = []
    used_device_ids = {e.get("device_id") for e in entity_registry if e.get("device_id")}
    orphans = [
        {
            "id": d.get("id"),
            "name": d.get("name"),
            "name_by_user": d.get("name_by_user"),
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "area_id": d.get("area_id"),
            "config_entries": d.get("config_entries"),
        }
        for d in devices
        if d.get("id") not in used_device_ids
    ]
    return orphans
