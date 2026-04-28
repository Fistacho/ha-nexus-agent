from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("calendar")


@mcp.tool()
def list_calendars() -> list[dict]:
    """List all calendar entities (REST: GET /api/calendars).

    Returns a list of dicts with `entity_id` and `name`.
    """
    with ha._client() as c:
        r = c.get("/api/calendars")
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


@mcp.tool()
def list_events(entity_id: str, start: str, end: str) -> list[dict]:
    """List events for a calendar entity between `start` and `end` (ISO 8601 datetimes).

    Uses REST `GET /api/calendars/{entity_id}?start=...&end=...`.
    """
    with ha._client() as c:
        r = c.get(
            f"/api/calendars/{entity_id}",
            params={"start": start, "end": end},
        )
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


@mcp.tool()
def create_event(
    entity_id: str,
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
) -> dict:
    """Create a calendar event via the `calendar.create_event` service.

    `start` / `end` are ISO 8601 datetimes (e.g. "2026-04-28T10:00:00").
    Returns the service-call result wrapped in a dict.
    """
    data: dict = {
        "entity_id": entity_id,
        "summary": summary,
        "start_date_time": start,
        "end_date_time": end,
    }
    if description:
        data["description"] = description
    if location:
        data["location"] = location
    result = ha.call_service("calendar", "create_event", data)
    return {
        "entity_id": entity_id,
        "summary": summary,
        "start": start,
        "end": end,
        "result": result,
    }


@mcp.tool()
def delete_event(entity_id: str, uid: str) -> dict:
    """Delete a calendar event by its `uid`.

    HA does not expose a stable `calendar.delete_event` service in core; this
    attempts the call and otherwise reports `not_implemented`.
    """
    try:
        result = ha.call_service(
            "calendar", "delete_event",
            {"entity_id": entity_id, "uid": uid},
        )
        return {"entity_id": entity_id, "uid": uid, "result": result, "ok": True}
    except Exception as e:
        return {
            "entity_id": entity_id,
            "uid": uid,
            "ok": False,
            "error": str(e),
            "note": "calendar.delete_event is not a core HA service; not implemented for this calendar.",
        }
