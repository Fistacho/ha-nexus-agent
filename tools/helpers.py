from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("helpers")

_HELPER_DOMAINS = [
    "input_boolean", "input_number", "input_text",
    "input_select", "input_datetime", "input_button",
    "counter", "timer", "schedule",
]


@mcp.tool()
def list_helpers() -> list[dict]:
    """List all helper entities (input_boolean, input_number, input_select, counter, timer…)."""
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


@mcp.tool()
def set_input_boolean(entity_id: str, value: bool) -> list[dict]:
    """Set input_boolean to true or false."""
    service = "turn_on" if value else "turn_off"
    return ha.call_service("input_boolean", service, {"entity_id": entity_id})


@mcp.tool()
def set_input_number(entity_id: str, value: float) -> list[dict]:
    """Set an input_number value."""
    return ha.call_service("input_number", "set_value", {"entity_id": entity_id, "value": value})


@mcp.tool()
def set_input_text(entity_id: str, value: str) -> list[dict]:
    """Set an input_text value."""
    return ha.call_service("input_text", "set_value", {"entity_id": entity_id, "value": value})


@mcp.tool()
def set_input_select(entity_id: str, option: str) -> list[dict]:
    """Set an input_select to a specific option."""
    return ha.call_service("input_select", "select_option", {"entity_id": entity_id, "option": option})


@mcp.tool()
def set_input_datetime(entity_id: str, date: str | None = None, time: str | None = None, datetime: str | None = None) -> list[dict]:
    """Set input_datetime. Provide date (YYYY-MM-DD), time (HH:MM:SS) or datetime (YYYY-MM-DD HH:MM:SS)."""
    data: dict = {"entity_id": entity_id}
    if date:
        data["date"] = date
    if time:
        data["time"] = time
    if datetime:
        data["datetime"] = datetime
    return ha.call_service("input_datetime", "set_datetime", data)


@mcp.tool()
def increment_counter(entity_id: str) -> list[dict]:
    """Increment a counter helper."""
    return ha.call_service("counter", "increment", {"entity_id": entity_id})


@mcp.tool()
def reset_counter(entity_id: str) -> list[dict]:
    """Reset a counter helper to 0."""
    return ha.call_service("counter", "reset", {"entity_id": entity_id})


@mcp.tool()
def start_timer(entity_id: str, duration: str | None = None) -> list[dict]:
    """Start a timer. duration format: HH:MM:SS or number of seconds."""
    data: dict = {"entity_id": entity_id}
    if duration:
        data["duration"] = duration
    return ha.call_service("timer", "start", data)


@mcp.tool()
def cancel_timer(entity_id: str) -> list[dict]:
    """Cancel a running timer."""
    return ha.call_service("timer", "cancel", {"entity_id": entity_id})


@mcp.tool()
def reload_helpers() -> list[dict]:
    """Reload all helper entities from configuration."""
    results = []
    for domain in ["input_boolean", "input_number", "input_text", "input_select", "input_datetime", "counter", "timer"]:
        try:
            results.extend(ha.call_service(domain, "reload"))
        except Exception:
            pass
    return results
