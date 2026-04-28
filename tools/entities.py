from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("entities")


@mcp.tool()
def list_entities(domain: str | None = None) -> list[dict]:
    """List all entities, optionally filtered by domain (light, switch, sensor…)."""
    states = ha.get_states()
    if domain:
        states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
        }
        for s in states
    ]


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """Get full state and attributes of a single entity."""
    return ha.get_state(entity_id)


@mcp.tool()
def turn_on(entity_id: str, brightness: int | None = None, color_temp: int | None = None, rgb_color: list[int] | None = None) -> list[dict]:
    """Turn on an entity. Optional: brightness (0-255), color_temp (mireds), rgb_color ([r,g,b])."""
    domain = entity_id.split(".")[0]
    data: dict = {"entity_id": entity_id}
    if brightness is not None:
        data["brightness"] = brightness
    if color_temp is not None:
        data["color_temp"] = color_temp
    if rgb_color is not None:
        data["rgb_color"] = rgb_color
    return ha.call_service(domain, "turn_on", data)


@mcp.tool()
def turn_off(entity_id: str) -> list[dict]:
    """Turn off an entity."""
    domain = entity_id.split(".")[0]
    return ha.call_service(domain, "turn_off", {"entity_id": entity_id})


@mcp.tool()
def toggle(entity_id: str) -> list[dict]:
    """Toggle an entity between on and off."""
    domain = entity_id.split(".")[0]
    return ha.call_service(domain, "toggle", {"entity_id": entity_id})


@mcp.tool()
def set_value(entity_id: str, value: float) -> list[dict]:
    """Set value of input_number, number, or climate temperature."""
    domain = entity_id.split(".")[0]
    if domain == "input_number":
        return ha.call_service("input_number", "set_value", {"entity_id": entity_id, "value": value})
    if domain == "number":
        return ha.call_service("number", "set_value", {"entity_id": entity_id, "value": value})
    if domain == "climate":
        return ha.call_service("climate", "set_temperature", {"entity_id": entity_id, "temperature": value})
    raise ValueError(f"set_value not supported for domain: {domain}")


@mcp.tool()
def select_option(entity_id: str, option: str) -> list[dict]:
    """Select option for input_select or select entity."""
    domain = entity_id.split(".")[0]
    if domain == "input_select":
        return ha.call_service("input_select", "select_option", {"entity_id": entity_id, "option": option})
    return ha.call_service("select", "select_option", {"entity_id": entity_id, "option": option})


@mcp.tool()
def update_entity_name(entity_id: str, name: str) -> dict:
    """Rename an entity in the entity registry."""
    return ha.update_entity_registry(entity_id, name=name)


@mcp.tool()
def assign_entity_to_area(entity_id: str, area_id: str) -> dict:
    """Assign an entity to an area."""
    return ha.update_entity_registry(entity_id, area_id=area_id)


@mcp.tool()
def disable_entity(entity_id: str) -> dict:
    """Disable an entity in the registry."""
    return ha.update_entity_registry(entity_id, disabled_by="user")


@mcp.tool()
def enable_entity(entity_id: str) -> dict:
    """Re-enable a disabled entity."""
    return ha.update_entity_registry(entity_id, disabled_by=None)


@mcp.tool()
def list_entity_registry(domain: str | None = None) -> list[dict]:
    """Get raw entity registry (includes disabled, hidden entities)."""
    entries = ha.get_entity_registry()
    if domain:
        entries = [e for e in entries if e.get("entity_id", "").startswith(f"{domain}.")]
    return entries


# --- Bulk control ---

@mcp.tool()
def bulk_control(entity_ids: list[str], action: str) -> dict:
    """Bulk turn_on/turn_off/toggle a list of entities.

    Groups entities by domain and issues one service call per domain
    (e.g. all `light.*` get a single `light.turn_on`).
    `action` must be one of: "turn_on", "turn_off", "toggle".
    Returns dict mapping domain -> count of entities affected.
    """
    if action not in ("turn_on", "turn_off", "toggle"):
        raise ValueError(f"action must be turn_on, turn_off or toggle (got: {action})")

    by_domain: dict[str, list[str]] = {}
    for eid in entity_ids:
        if "." not in eid:
            continue
        dom = eid.split(".", 1)[0]
        by_domain.setdefault(dom, []).append(eid)

    result: dict[str, int] = {}
    for dom, eids in by_domain.items():
        ha.call_service(dom, action, {"entity_id": eids})
        result[dom] = len(eids)
    return result


@mcp.tool()
def bulk_set_state(updates: list[dict]) -> list[dict]:
    """Bulk-write states using HA's set_state API.

    Each item must contain `entity_id` and `state`, plus optional `attributes` dict.
    Returns a list of per-update result dicts (`{entity_id, ok, error?}`).
    Note: this writes to /api/states which only sets a virtual state — for real
    device control use bulk_control instead.
    """
    out: list[dict] = []
    for u in updates:
        eid = u.get("entity_id")
        state = u.get("state")
        attrs = u.get("attributes")
        if not eid or state is None:
            out.append({"entity_id": eid, "ok": False, "error": "missing entity_id or state"})
            continue
        try:
            ha.set_state(eid, str(state), attrs)
            out.append({"entity_id": eid, "ok": True})
        except Exception as e:
            out.append({"entity_id": eid, "ok": False, "error": str(e)})
    return out


# --- Voice / assistant exposure ---

_ASSISTANTS = ("conversation", "cloud.alexa", "cloud.google_assistant")


@mcp.tool()
def set_entity_exposure(entity_id: str, assistant: str, should_expose: bool) -> dict:
    """Expose or hide an entity to a voice assistant.

    `assistant` must be one of: "conversation", "cloud.alexa", "cloud.google_assistant".
    Uses WS `homeassistant/expose_entity`.
    """
    if assistant not in _ASSISTANTS:
        raise ValueError(f"assistant must be one of {_ASSISTANTS}")
    result = ha._ws_call(
        "homeassistant/expose_entity",
        assistants=[assistant],
        entity_ids=[entity_id],
        should_expose=bool(should_expose),
    )
    return {
        "entity_id": entity_id,
        "assistant": assistant,
        "should_expose": bool(should_expose),
        "result": result,
    }


@mcp.tool()
def get_entity_exposure(entity_id: str) -> dict:
    """Get exposure flags for an entity across all voice assistants."""
    result = ha._ws_call("homeassistant/expose/get", entity_id=entity_id)
    return {"entity_id": entity_id, "exposure": result}


@mcp.tool()
def list_exposed_entities(assistant: str) -> dict:
    """List all entities exposed to a given voice assistant.

    `assistant` must be one of: "conversation", "cloud.alexa", "cloud.google_assistant".
    """
    if assistant not in _ASSISTANTS:
        raise ValueError(f"assistant must be one of {_ASSISTANTS}")
    result = ha._ws_call("homeassistant/expose/list", assistant=assistant)
    return {"assistant": assistant, "exposed": result}
