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
def delete_event(
    entity_id: str,
    uid: str,
    recurrence_id: str | None = None,
    recurrence_range: str | None = None,
) -> dict:
    """Delete a calendar event by its `uid` via WS `calendar/event/delete`.

    Only works for calendar platforms that declare
    `CalendarEntityFeature.DELETE_EVENT` support (e.g. local/CalDAV calendars);
    read-only integrations (like Google Calendar) reject it and this returns
    `ok: False` with HA's error. `recurrence_id` narrows deletion to one
    instance of a recurring event; `recurrence_range` (e.g. "THISANDFUTURE")
    extends that to a range.
    """
    payload: dict = {"entity_id": entity_id, "uid": uid}
    if recurrence_id is not None:
        payload["recurrence_id"] = recurrence_id
    if recurrence_range is not None:
        payload["recurrence_range"] = recurrence_range
    try:
        result = ha._ws_call("calendar/event/delete", **payload)
        return {"entity_id": entity_id, "uid": uid, "result": result, "ok": True}
    except Exception as e:
        return {
            "entity_id": entity_id,
            "uid": uid,
            "ok": False,
            "error": str(e),
        }
