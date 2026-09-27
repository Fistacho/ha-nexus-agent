"""Direct entity control and entity registry access.

Turn things on/off, read and write simple values, and manage the entity
registry (rename, area assignment, enable/disable, voice-assistant
exposure). Generic service dispatch beyond these fixed actions lives in
`services_call_service`/`ws_call_service`.
"""
from __future__ import annotations

from typing import Annotated

import httpx
from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("entities")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


_DEFAULT_FIELDS = frozenset({"entity_id", "state", "friendly_name"})
_REGISTRY_FIELDS = frozenset({"area_id", "device_id", "labels"})


@mcp.tool(annotations=read("List entities with filters"))
def list_entities(
    domain: Annotated[
        str | None,
        Field(description="Entity domain prefix filter, e.g. 'light' or 'sensor'. Omit to include every domain."),
    ] = None,
    limit: Annotated[
        int | None,
        Field(description="Maximum number of entities to return after filtering, for pagination. Omit for no limit."),
    ] = None,
    offset: Annotated[
        int,
        Field(description="Number of filtered entities to skip before applying limit. 0 starts from the first match."),
    ] = 0,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Per-entity keys to return. Defaults to entity_id, state, friendly_name. "
                "Also accepts attributes, area_id, device_id, labels."
            )
        ),
    ] = None,
) -> list[dict]:
    """List entity states, optionally filtered by domain, with pagination and field selection.

    Reads live states via `ha.get_states()` over HTTP; when `fields` requests
    `area_id`, `device_id` or `labels`, it also fetches the entity registry
    over WebSocket and joins it by entity ID. Filtering by `domain` and
    pagination (`limit`/`offset`) are applied client-side after the fetch.

    Use when: browsing or searching current entity states across the whole
    installation or one domain.
    Not for: raw registry fields such as disabled/hidden status — use
    `entities_list_entity_registry`; a plain unfiltered state dump — use
    `ws_get_states`.
    Returns: list of dicts, one per entity, containing only the requested
    `fields`.
    Limits: `limit`/`offset` paginate the already-filtered list; no size cap
    beyond that.
    """
    states = ha.get_states()
    if domain:
        states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]

    requested = set(fields) if fields else set(_DEFAULT_FIELDS)
    need_registry = bool(requested & _REGISTRY_FIELDS)
    registry: dict[str, dict] = {}
    if need_registry:
        for e in ha.get_entity_registry():
            registry[e.get("entity_id", "")] = e

    def _build(s: dict) -> dict:
        row: dict = {}
        if "entity_id" in requested:
            row["entity_id"] = s["entity_id"]
        if "state" in requested:
            row["state"] = s["state"]
        if "friendly_name" in requested:
            row["friendly_name"] = s.get("attributes", {}).get("friendly_name")
        if "attributes" in requested:
            row["attributes"] = s.get("attributes", {})
        if need_registry:
            reg = registry.get(s["entity_id"], {})
            if "area_id" in requested:
                row["area_id"] = reg.get("area_id")
            if "device_id" in requested:
                row["device_id"] = reg.get("device_id")
            if "labels" in requested:
                row["labels"] = reg.get("labels", [])
        return row

    result = [_build(s) for s in states]
    if offset:
        result = result[offset:]
    if limit is not None:
        result = result[:limit]
    return result


@mcp.tool(annotations=read("Get entity state"))
def get_entity(
    entity_id: Annotated[str, Field(description="Full entity ID to read, e.g. 'light.kitchen'.")],
) -> dict:
    """Get the full state and attributes of a single entity.

    Calls `ha.get_state(entity_id)`, a single HTTP GET to
    `/api/states/<entity_id>`; the response includes `state`, `attributes`,
    `last_changed` and `last_updated` as reported by Home Assistant.

    Use when: inspecting one entity's current value and attributes before or
    after an action.
    Not for: many entities at once — use `entities_list_entities` or
    `snapshot_get_snapshot`.
    Returns: dict with `entity_id`, `state`, `attributes`, `last_changed`,
    `last_updated`, `context`.
    """
    return ha.get_state(entity_id)


@mcp.tool(annotations=write("Turn on an entity", idempotent=True))
def turn_on(
    entity_id: Annotated[str, Field(description="Full entity ID to turn on, e.g. 'light.kitchen'.")],
    brightness: Annotated[
        int | None,
        Field(description="Light brightness, 0-255. Light-only; passing it for a non-light entity returns an error."),
    ] = None,
    color_temp: Annotated[
        int | None,
        Field(
            description=(
                "Deprecated mired color temperature, light-only. Converted to Kelvin "
                "before the call; prefer color_temp_kelvin."
            )
        ),
    ] = None,
    color_temp_kelvin: Annotated[
        int | None,
        Field(description="Light color temperature in Kelvin, light-only option."),
    ] = None,
    rgb_color: Annotated[
        list[int] | None,
        Field(description="Light RGB color as [r, g, b], each 0-255, light-only option."),
    ] = None,
) -> list[dict] | dict:
    """Turn on an entity, optionally with light-only color and brightness options.

    Calls `<domain>.turn_on` for the entity's domain over HTTP. If any of
    `brightness`, `color_temp`, `color_temp_kelvin` or `rgb_color` is given
    for a non-`light` entity, the service is not called at all — HA's own
    `turn_on` for other domains (e.g. `switch`) rejects unknown fields, so
    this checks the domain first. A given `color_temp` (mireds) is converted
    to Kelvin (`round(1_000_000 / mireds)`) since HA's `light.turn_on` no
    longer accepts mireds.

    Use when: turning on any entity, with or without light color/brightness.
    Not for: a dedicated light color/brightness change without an on/off
    intent — use `services_set_light_color`; an input_boolean-only helper —
    `helpers_set_input_boolean` is the domain-specific equivalent.
    Returns: the list of changed states from Home Assistant, or an error
    dict.
    Errors: `{"error": "..."}` when a light-only option is given for a
    non-light domain.
    """
    domain = entity_id.split(".")[0]
    has_light_only_opts = (
        brightness is not None
        or color_temp is not None
        or color_temp_kelvin is not None
        or rgb_color is not None
    )
    if has_light_only_opts and domain != "light":
        return {
            "error": (
                f"brightness/color_temp/color_temp_kelvin/rgb_color are light-only "
                f"options and are not supported for domain '{domain}'"
            )
        }
    data: dict = {"entity_id": entity_id}
    if brightness is not None:
        data["brightness"] = brightness
    if color_temp_kelvin is not None:
        data["color_temp_kelvin"] = color_temp_kelvin
    elif color_temp is not None:
        data["color_temp_kelvin"] = round(1_000_000 / color_temp)
    if rgb_color is not None:
        data["rgb_color"] = rgb_color
    return ha.call_service(domain, "turn_on", data)


@mcp.tool(annotations=write("Turn off an entity", idempotent=True))
def turn_off(
    entity_id: Annotated[str, Field(description="Full entity ID to turn off, e.g. 'light.kitchen'.")],
) -> list[dict]:
    """Turn off an entity by calling its domain's turn_off action.

    Calls `<domain>.turn_off` with `{"entity_id": entity_id}` over HTTP.

    Use when: turning off any controllable entity regardless of domain.
    Not for: flipping state without knowing the target — use
    `entities_toggle`.
    Returns: the list of changed states from Home Assistant.
    """
    domain = entity_id.split(".")[0]
    return ha.call_service(domain, "turn_off", {"entity_id": entity_id})


@mcp.tool(annotations=write("Toggle an entity", idempotent=False))
def toggle(
    entity_id: Annotated[str, Field(description="Full entity ID to toggle, e.g. 'switch.fan'.")],
) -> list[dict]:
    """Toggle an entity between on and off by calling its domain's toggle action.

    Calls `<domain>.toggle` with `{"entity_id": entity_id}` over HTTP; the
    resulting state depends on the entity's current state, so repeating this
    call flips it back and forth.

    Use when: the desired end state is unknown or irrelevant, only "flip it".
    Not for: a deterministic on/off — use `entities_turn_on`/`entities_turn_off`.
    Returns: the list of changed states from Home Assistant.
    """
    domain = entity_id.split(".")[0]
    return ha.call_service(domain, "toggle", {"entity_id": entity_id})


@mcp.tool(annotations=write("Set entity value", idempotent=True))
def set_value(
    entity_id: Annotated[str, Field(description="Full entity ID to set, e.g. 'input_number.target_temp'.")],
    value: Annotated[float, Field(description="Numeric value to write; interpreted per domain, see Behaviour.")],
) -> list[dict]:
    """Set the numeric value of an input_number, number or climate entity.

    Dispatches to `input_number.set_value`, `number.set_value` or
    `climate.set_temperature` depending on `entity_id`'s domain; any other
    domain raises `ValueError` before any HTTP call is made.

    Use when: writing a target number or temperature to one of these three
    domains.
    Not for: input_text, input_select, input_boolean or input_datetime — use
    `helpers_set_input_text`, `helpers_set_input_select`,
    `helpers_set_input_boolean` or `helpers_set_input_datetime`.
    Returns: the list of changed states from Home Assistant.
    Errors: raises `ValueError` for any domain other than input_number,
    number or climate.
    """
    domain = entity_id.split(".")[0]
    if domain == "input_number":
        return ha.call_service("input_number", "set_value", {"entity_id": entity_id, "value": value})
    if domain == "number":
        return ha.call_service("number", "set_value", {"entity_id": entity_id, "value": value})
    if domain == "climate":
        return ha.call_service("climate", "set_temperature", {"entity_id": entity_id, "temperature": value})
    raise ValueError(f"set_value not supported for domain: {domain}")


@mcp.tool(annotations=write("Select entity option", idempotent=True))
def select_option(
    entity_id: Annotated[str, Field(description="Full entity ID to set, e.g. 'input_select.house_mode'.")],
    option: Annotated[
        str,
        Field(description="Option string to select; must match one of the entity's configured options."),
    ],
) -> list[dict]:
    """Select an option on an input_select or select entity.

    Calls `input_select.select_option` for `input_select.*` entities, or
    `select.select_option` for every other domain (in practice `select.*`).

    Use when: setting the current option of an input_select or select entity.
    Not for: an input_select-only call site — `helpers_set_input_select`
    does the same thing for that domain specifically.
    Returns: the list of changed states from Home Assistant.
    """
    domain = entity_id.split(".")[0]
    if domain == "input_select":
        return ha.call_service("input_select", "select_option", {"entity_id": entity_id, "option": option})
    return ha.call_service("select", "select_option", {"entity_id": entity_id, "option": option})


@mcp.tool(annotations=destructive("Rename entity in registry", idempotent=True))
def update_entity_name(
    entity_id: Annotated[
        str, Field(description="Full entity ID whose registry entry is renamed, e.g. 'light.kitchen'.")
    ],
    name: Annotated[
        str, Field(description="New display name to store in the entity registry, replacing the current one.")
    ],
) -> dict:
    """Rename an entity in the entity registry, replacing its current display name.

    Calls WS `config/entity_registry/update` with `name=name`; the previous
    name is not returned or preserved anywhere by this call.

    Use when: correcting or customising an entity's display name.
    Not for: reassigning area, or enabling/disabling — use
    `entities_assign_entity_to_area`, `entities_disable_entity`,
    `entities_enable_entity`.
    Returns: the updated entity registry entry from Home Assistant.
    """
    return ha.update_entity_registry(entity_id, name=name)


@mcp.tool(annotations=destructive("Assign entity to area", idempotent=True))
def assign_entity_to_area(
    entity_id: Annotated[str, Field(description="Full entity ID to reassign, e.g. 'light.kitchen'.")],
    area_id: Annotated[
        str, Field(description="Area registry ID to assign the entity to, replacing any current area.")
    ],
) -> dict:
    """Assign an entity to an area, replacing its current area assignment.

    Calls WS `config/entity_registry/update` with `area_id=area_id`; any
    previous area assignment is overwritten and not returned.

    Use when: moving an entity to a different area in the registry.
    Not for: renaming it, or toggling exposure — use
    `entities_update_entity_name` or `entities_set_entity_exposure`.
    Returns: the updated entity registry entry from Home Assistant.
    """
    return ha.update_entity_registry(entity_id, area_id=area_id)


@mcp.tool(annotations=write("Disable entity in registry", idempotent=True))
def disable_entity(
    entity_id: Annotated[str, Field(description="Full entity ID to disable, e.g. 'sensor.old_device'.")],
) -> dict:
    """Disable an entity in the registry so Home Assistant stops creating its state.

    Calls WS `config/entity_registry/update` with `disabled_by="user"`.

    Use when: retiring an entity without deleting its registry entry.
    Not for: re-enabling it — use `entities_enable_entity`.
    Returns: the updated entity registry entry from Home Assistant.
    """
    return ha.update_entity_registry(entity_id, disabled_by="user")


@mcp.tool(annotations=write("Enable entity in registry", idempotent=True))
def enable_entity(
    entity_id: Annotated[str, Field(description="Full entity ID to re-enable, e.g. 'sensor.old_device'.")],
) -> dict:
    """Re-enable a previously disabled entity in the registry.

    Calls WS `config/entity_registry/update` with `disabled_by=None`.

    Use when: restoring an entity disabled via `entities_disable_entity`.
    Not for: disabling it — use `entities_disable_entity`.
    Returns: the updated entity registry entry from Home Assistant.
    """
    return ha.update_entity_registry(entity_id, disabled_by=None)


@mcp.tool(annotations=read("List entity registry entries"))
def list_entity_registry(
    domain: Annotated[
        str | None,
        Field(description="Entity domain prefix filter, e.g. 'light' or 'sensor'. Omit to include every domain."),
    ] = None,
) -> list[dict]:
    """Get the raw entity registry, including disabled and hidden entities.

    Calls WS `config/entity_registry/list` and filters client-side by
    `domain` when given. Unlike `entities_list_entities`, this has no state
    values, only registry metadata (`area_id`, `device_id`, `disabled_by`,
    `labels`, ...).

    Use when: auditing registry metadata, including disabled/hidden entities
    that carry no live state.
    Not for: current state values — use `entities_list_entities`.
    Returns: list of raw entity registry entry dicts from Home Assistant.
    """
    entries = ha.get_entity_registry()
    if domain:
        entries = [e for e in entries if e.get("entity_id", "").startswith(f"{domain}.")]
    return entries


# --- Bulk control ---

@mcp.tool(annotations=write("Bulk turn on, off or toggle entities", idempotent=False))
def bulk_control(
    entity_ids: Annotated[
        list[str], Field(description="Full entity IDs to act on, e.g. ['light.a', 'light.b'].")
    ],
    action: Annotated[
        str, Field(description="Service to call per domain group: 'turn_on', 'turn_off' or 'toggle'.")
    ],
) -> dict:
    """Turn on, turn off or toggle many entities in one call, grouped by domain.

    Groups `entity_ids` by domain and issues one `<domain>.<action>` service
    call per domain group (e.g. all `light.*` entities get a single
    `light.turn_on`), instead of one call per entity.

    Use when: applying the same on/off/toggle action to many entities across
    domains at once.
    Not for: writing arbitrary states instead of calling a service — use
    `entities_bulk_set_state`; controlling every entity of one domain in a
    single area by area_id rather than an explicit entity_id list — use
    `areas_control_area`.
    Returns: dict mapping domain to the number of entities affected.
    Errors: raises `ValueError` when `action` is not one of turn_on,
    turn_off, toggle.
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


@mcp.tool(annotations=write("Bulk write virtual entity states", idempotent=True))
def bulk_set_state(
    updates: Annotated[
        list[dict],
        Field(
            description=(
                "List of {entity_id, state, attributes?} dicts describing the "
                "virtual state to write per entity."
            )
        ),
    ],
) -> list[dict]:
    """Write states for many entities directly via the states API, one call per entity.

    Calls `ha.set_state(entity_id, state, attributes)` per item in
    `updates`, which posts to `/api/states/<entity_id>`; this sets a virtual
    state that a real device driver overwrites on its next update, unlike a
    real service call.

    Use when: seeding or overriding state for testing, template sensors, or
    entities with no backing device.
    Not for: actually controlling a device — use `entities_bulk_control`.
    Returns: list of per-update dicts (`{entity_id, ok, error?, error_type?}`);
    a missing `entity_id`/`state` or a failed write is reported per item, not
    raised.
    Errors: per-item `{"ok": false, "error": "...", "error_type": "..."}`;
    `error_type` is `"validation"` for a missing `entity_id`/`state` (no
    write attempted), `"http"` for an `httpx.HTTPError` from the write call
    (bad response or network failure talking to Home Assistant), or
    `"unexpected"` for any other exception. An `"unexpected"` error is still
    caught per item and does not abort the rest of the batch.
    """
    out: list[dict] = []
    for u in updates:
        eid = u.get("entity_id")
        state = u.get("state")
        attrs = u.get("attributes")
        if not eid or state is None:
            out.append({"entity_id": eid, "ok": False, "error": "missing entity_id or state", "error_type": "validation"})
            continue
        try:
            ha.set_state(eid, str(state), attrs)
            out.append({"entity_id": eid, "ok": True})
        except httpx.HTTPError as e:
            out.append({"entity_id": eid, "ok": False, "error": str(e), "error_type": "http"})
        except Exception as e:
            out.append({"entity_id": eid, "ok": False, "error": str(e), "error_type": "unexpected"})
    return out


# --- Voice / assistant exposure ---

_ASSISTANTS = ("conversation", "cloud.alexa", "cloud.google_assistant")


@mcp.tool(annotations=write("Set entity voice assistant exposure", idempotent=True))
def set_entity_exposure(
    entity_id: Annotated[str, Field(description="Full entity ID to expose or hide, e.g. 'light.kitchen'.")],
    assistant: Annotated[
        str,
        Field(description="Voice assistant identifier: 'conversation', 'cloud.alexa' or 'cloud.google_assistant'."),
    ],
    should_expose: Annotated[
        bool, Field(description="True to expose the entity to the assistant, false to hide it.")
    ],
) -> dict:
    """Expose or hide one entity to one voice assistant.

    Calls WS `homeassistant/expose_entity` with a single-element
    `entity_ids`/`assistants` list.

    Use when: changing exposure for one entity and one assistant.
    Not for: many entities or assistants at once — use
    `entities_bulk_set_entity_exposure`.
    Returns: dict with `entity_id`, `assistant`, `should_expose` and the raw
    WS `result`.
    Errors: raises `ValueError` when `assistant` is not one of conversation,
    cloud.alexa, cloud.google_assistant.
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


@mcp.tool(annotations=read("Get entity voice assistant exposure"))
def get_entity_exposure(
    entity_id: Annotated[str, Field(description="Full entity ID to query exposure for, e.g. 'light.kitchen'.")],
) -> dict:
    """Get exposure flags for one entity across all voice assistants.

    Home Assistant has no per-entity exposure query, so this fetches the
    full exposure map via WS `homeassistant/expose_entity/list` and filters
    it client-side; an entity absent from that map is reported as `False`
    for every assistant.

    Use when: checking whether one entity is exposed, and to which
    assistants.
    Not for: the whole exposure map for one assistant — use
    `entities_list_exposed_entities`.
    Returns: dict with `entity_id` and `exposure` (assistant name -> bool for
    conversation, cloud.alexa, cloud.google_assistant).
    """
    result = ha._ws_call("homeassistant/expose_entity/list")
    entity_exposure = (result.get("exposed_entities") or {}).get(entity_id, {})
    exposure = {assistant: bool(entity_exposure.get(assistant, False)) for assistant in _ASSISTANTS}
    return {"entity_id": entity_id, "exposure": exposure}


@mcp.tool(annotations=read("List entities exposed to an assistant"))
def list_exposed_entities(
    assistant: Annotated[
        str,
        Field(description="Voice assistant identifier: 'conversation', 'cloud.alexa' or 'cloud.google_assistant'."),
    ],
) -> dict:
    """List every entity currently exposed to one voice assistant.

    Home Assistant has no per-assistant exposure query, so this fetches the
    full exposure map via WS `homeassistant/expose_entity/list` and filters
    it client-side for the given `assistant`.

    Use when: auditing what one assistant can currently see.
    Not for: exposure of one specific entity — use
    `entities_get_entity_exposure`.
    Returns: dict with `assistant` and `exposed` (list of entity IDs).
    Errors: raises `ValueError` when `assistant` is not one of conversation,
    cloud.alexa, cloud.google_assistant.
    """
    if assistant not in _ASSISTANTS:
        raise ValueError(f"assistant must be one of {_ASSISTANTS}")
    result = ha._ws_call("homeassistant/expose_entity/list")
    exposed_entities = result.get("exposed_entities") or {}
    exposed = [eid for eid, flags in exposed_entities.items() if flags.get(assistant)]
    return {"assistant": assistant, "exposed": exposed}


@mcp.tool(annotations=write("Bulk set entity assistant exposure", idempotent=True))
def bulk_set_entity_exposure(
    entity_ids: Annotated[
        list[str], Field(description="Full entity IDs to expose or hide, e.g. ['light.a', 'light.b'].")
    ],
    should_expose: Annotated[
        bool, Field(description="True to expose the entities to the target assistants, false to hide them.")
    ],
    assistants: Annotated[
        list[str] | None,
        Field(
            description=(
                "Subset of voice assistants to target. Omit to apply to conversation, "
                "cloud.alexa and cloud.google_assistant."
            )
        ),
    ] = None,
) -> dict:
    """Expose or hide many entities across one or more voice assistants in a single call.

    Calls WS `homeassistant/expose_entity` once with the full `entity_ids`
    and `assistants` lists, which HA accepts natively, instead of one call
    per entity/assistant pair. Returns immediately with an empty result when
    `entity_ids` is empty.

    Use when: changing exposure for many entities and/or several assistants
    at once.
    Not for: a single entity and assistant — use
    `entities_set_entity_exposure`.
    Returns: dict with `count`, `assistants`, `should_expose` and the raw WS
    `result`.
    Errors: raises `ValueError` when `assistants` contains an identifier
    outside conversation, cloud.alexa, cloud.google_assistant.
    """
    targets = list(assistants) if assistants else list(_ASSISTANTS)
    invalid = [a for a in targets if a not in _ASSISTANTS]
    if invalid:
        raise ValueError(f"invalid assistants: {invalid}; allowed: {_ASSISTANTS}")
    if not entity_ids:
        return {"count": 0, "assistants": targets, "result": None}
    result = ha._ws_call(
        "homeassistant/expose_entity",
        assistants=targets,
        entity_ids=list(entity_ids),
        should_expose=bool(should_expose),
    )
    return {
        "count": len(entity_ids),
        "assistants": targets,
        "should_expose": bool(should_expose),
        "result": result,
    }
