"""Home Assistant "helper" domains: input_*, counter, timer.

Dedicated, domain-specific setters for the input_* helper entities plus
counter/timer control. `entities_set_value`/`entities_select_option` and
`entities_turn_on`/`entities_turn_off` overlap several of these for the
input_number/input_select/input_boolean domains specifically — see each
tool's "Not for" for which one is narrower.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import read, write

mcp = FastMCP("helpers")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_HELPER_DOMAINS = [
    "input_boolean", "input_number", "input_text",
    "input_select", "input_datetime", "input_button",
    "counter", "timer", "schedule",
]


@mcp.tool(annotations=read("List helper entities"))
def list_helpers() -> list[dict]:
    """List every helper entity across all helper domains, with its current state.

    Reads all states via `ha.get_states()` over HTTP and keeps only entities
    whose domain is one of input_boolean, input_number, input_text,
    input_select, input_datetime, input_button, counter, timer, schedule.

    Use when: browsing which helpers exist and their current values.
    Not for: helpers of one specific domain filtered generically — use
    `entities_list_entities` with `domain` set.
    Returns: list of dicts with `entity_id`, `state`, `attributes` for each
    matching helper entity.
    """
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "attributes": s.get("attributes", {}),
        }
        for s in states
        if any(s["entity_id"].startswith(f"{d}.") for d in _HELPER_DOMAINS)
    ]


@mcp.tool(annotations=write("Set an input_boolean value", idempotent=True))
def set_input_boolean(
    entity_id: Annotated[str, Field(description="Full input_boolean entity ID to set, e.g. 'input_boolean.guest_mode'.")],
    value: Annotated[bool, Field(description="Target boolean value; true calls turn_on, false calls turn_off.")],
) -> list[dict]:
    """Set an input_boolean helper to true or false.

    Calls `input_boolean.turn_on` when `value` is true, otherwise
    `input_boolean.turn_off`, over HTTP.

    Use when: setting a known boolean helper to a specific value.
    Not for: any other domain's on/off state — use `entities_turn_on`/
    `entities_turn_off`, which work generically across domains including
    input_boolean.
    Returns: the list of states changed by the call.
    """
    service = "turn_on" if value else "turn_off"
    return ha.call_service("input_boolean", service, {"entity_id": entity_id})


@mcp.tool(annotations=write("Set an input_number value", idempotent=True))
def set_input_number(
    entity_id: Annotated[str, Field(description="Full input_number entity ID to set, e.g. 'input_number.target_temp'.")],
    value: Annotated[float, Field(description="Target numeric value to write to the input_number.")],
) -> list[dict]:
    """Set an input_number helper's numeric value.

    Calls `input_number.set_value` with `{"entity_id": entity_id, "value":
    value}` over HTTP.

    Use when: writing a specific number to an input_number helper.
    Not for: a number/climate entity too — `entities_set_value` covers
    input_number identically plus the number and climate domains.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("input_number", "set_value", {"entity_id": entity_id, "value": value})


@mcp.tool(annotations=write("Set an input_text value", idempotent=True))
def set_input_text(
    entity_id: Annotated[str, Field(description="Full input_text entity ID to set, e.g. 'input_text.last_visitor'.")],
    value: Annotated[str, Field(description="Target text value to write to the input_text.")],
) -> list[dict]:
    """Set an input_text helper's text value.

    Calls `input_text.set_value` with `{"entity_id": entity_id, "value":
    value}` over HTTP.

    Use when: writing a specific string to an input_text helper.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("input_text", "set_value", {"entity_id": entity_id, "value": value})


@mcp.tool(annotations=write("Select an input_select option", idempotent=True))
def set_input_select(
    entity_id: Annotated[str, Field(description="Full input_select entity ID to set, e.g. 'input_select.house_mode'.")],
    option: Annotated[
        str, Field(description="Option string to select; must match one of the input_select's configured options.")
    ],
) -> list[dict]:
    """Set an input_select helper to one of its configured options.

    Calls `input_select.select_option` with `{"entity_id": entity_id,
    "option": option}` over HTTP.

    Use when: setting a known input_select helper to a specific option.
    Not for: the generic select/input_select dispatch that also handles
    plain select entities — `entities_select_option` covers input_select
    identically plus the select domain.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("input_select", "select_option", {"entity_id": entity_id, "option": option})


@mcp.tool(annotations=write("Set an input_datetime value", idempotent=True))
def set_input_datetime(
    entity_id: Annotated[str, Field(description="Full input_datetime entity ID to set, e.g. 'input_datetime.reminder'.")],
    date: Annotated[
        str | None, Field(description="Date to set, formatted YYYY-MM-DD. Omit to leave the date unchanged.")
    ] = None,
    time: Annotated[
        str | None, Field(description="Time to set, formatted HH:MM:SS. Omit to leave the time unchanged.")
    ] = None,
    datetime: Annotated[
        str | None,
        Field(
            description=(
                "Combined date and time, formatted 'YYYY-MM-DD HH:MM:SS'. Takes precedence "
                "over date/time when given."
            )
        ),
    ] = None,
) -> list[dict]:
    """Set an input_datetime helper's date, time or combined datetime value.

    Calls `input_datetime.set_datetime` over HTTP with whichever of `date`,
    `time`, `datetime` are given; passing none of them still calls the
    service with only `entity_id`.

    Use when: writing a specific date/time to an input_datetime helper.
    Returns: the list of states changed by the call.
    """
    data: dict = {"entity_id": entity_id}
    if date:
        data["date"] = date
    if time:
        data["time"] = time
    if datetime:
        data["datetime"] = datetime
    return ha.call_service("input_datetime", "set_datetime", data)


@mcp.tool(annotations=write("Increment a counter helper", idempotent=False))
def increment_counter(
    entity_id: Annotated[str, Field(description="Full counter entity ID to increment, e.g. 'counter.visits'.")],
) -> list[dict]:
    """Increment a counter helper by its configured step.

    Calls `counter.increment` with `{"entity_id": entity_id}` over HTTP;
    repeating this call keeps increasing the counter's value.

    Use when: counting up an event occurrence.
    Not for: setting the counter back to zero — use
    `helpers_reset_counter`.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("counter", "increment", {"entity_id": entity_id})


@mcp.tool(annotations=write("Reset a counter helper", idempotent=True))
def reset_counter(
    entity_id: Annotated[str, Field(description="Full counter entity ID to reset, e.g. 'counter.visits'.")],
) -> list[dict]:
    """Reset a counter helper's value back to 0.

    Calls `counter.reset` with `{"entity_id": entity_id}` over HTTP; the
    counter is runtime UI state, not stored configuration.

    Use when: clearing a counter back to its initial value.
    Not for: increasing it by one — use `helpers_increment_counter`.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("counter", "reset", {"entity_id": entity_id})


@mcp.tool(annotations=write("Start a timer helper", idempotent=True))
def start_timer(
    entity_id: Annotated[str, Field(description="Full timer entity ID to start, e.g. 'timer.laundry'.")],
    duration: Annotated[
        str | None,
        Field(
            description=(
                "Duration to run for, formatted HH:MM:SS or a plain number of seconds. "
                "Omit to reuse the timer's configured duration."
            )
        ),
    ] = None,
) -> list[dict]:
    """Start a timer helper, optionally overriding its duration.

    Calls `timer.start` over HTTP with `entity_id` and `duration` when
    given; calling this on an already-running timer restarts it with the
    new duration.

    Use when: starting a countdown, e.g. for a reminder or process timeout.
    Not for: stopping it early — use `helpers_cancel_timer`.
    Returns: the list of states changed by the call.
    """
    data: dict = {"entity_id": entity_id}
    if duration:
        data["duration"] = duration
    return ha.call_service("timer", "start", data)


@mcp.tool(annotations=write("Cancel a timer helper", idempotent=True))
def cancel_timer(
    entity_id: Annotated[str, Field(description="Full timer entity ID to cancel, e.g. 'timer.laundry'.")],
) -> list[dict]:
    """Cancel a running timer helper.

    Calls `timer.cancel` with `{"entity_id": entity_id}` over HTTP.

    Use when: stopping a timer before it finishes.
    Not for: starting one — use `helpers_start_timer`.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("timer", "cancel", {"entity_id": entity_id})


@mcp.tool(annotations=write("Reload helper entities from config", idempotent=True))
def reload_helpers() -> dict:
    """Reload every helper domain's entities from configuration in one call.

    Calls `<domain>.reload` over HTTP for each of input_boolean,
    input_number, input_text, input_select, input_datetime, counter, timer
    in turn; a domain whose reload call raises is skipped but its error is
    captured rather than silently dropped.

    Use when: applying YAML changes across several helper domains without
    restarting Home Assistant.
    Not for: reloading one specific domain — use `services_reload_config`.
    Returns: `{"reloaded": [...], "failed": {domain: message}}` — combined
    list of states changed across every domain that reloaded successfully,
    plus a per-domain map of the error message for every domain whose
    reload call raised.
    Errors: a domain's failure never raises out of this call; instead its
    exception message is recorded under `failed[<domain>]` so the caller can
    tell which domain, if any, did not reload.
    """
    reloaded: list[dict] = []
    failed: dict[str, str] = {}
    for domain in ["input_boolean", "input_number", "input_text", "input_select", "input_datetime", "counter", "timer"]:
        try:
            reloaded.extend(ha.call_service(domain, "reload"))
        except Exception as e:
            failed[domain] = str(e)
    return {"reloaded": reloaded, "failed": failed}
