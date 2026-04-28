from datetime import datetime, timezone
from difflib import SequenceMatcher
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("search")


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


@mcp.tool()
def search_entities(query: str, limit: int = 20) -> list[dict]:
    """Fuzzy search entities by entity_id, friendly_name, and device_id."""
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


@mcp.tool()
def search_devices(query: str, limit: int = 20) -> list[dict]:
    """Fuzzy search devices by name, name_by_user, manufacturer, and model."""
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


@mcp.tool()
def search_areas(query: str, limit: int = 20) -> list[dict]:
    """Fuzzy search areas by name."""
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


@mcp.tool()
def deep_search(query: str, limit: int = 20) -> list[dict]:
    """Search across entities, devices, areas, and automations; tagged with `kind`."""
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


@mcp.tool()
def find_related(entity_id: str) -> dict:
    """Return device, area, floor, sibling entities (same device), and area-mates for an entity."""
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
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


@mcp.tool()
def find_unused_entities() -> list[dict]:
    """Heuristic: entities that look unused (unavailable/unknown for >7 days, or orphan with no device)."""
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


@mcp.tool()
def find_orphan_devices() -> list[dict]:
    """List devices that have no entities associated (orphans in device registry)."""
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
