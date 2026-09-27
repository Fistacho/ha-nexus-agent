"""Calendar entities: list calendars, list/create/delete events.

Reads go through HA's calendar API over HTTP (`/api/calendars`); creating an
event goes through the `calendar.create_event` service call; deleting one
goes through the WebSocket API, since the HTTP API has no delete endpoint
for calendar events.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("calendar")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("List calendar entities"))
def list_calendars() -> list[dict]:
    """List every `calendar.*` entity known to HA.

    Calls `GET /api/calendars` over HTTP.

    Use when: discovering which `entity_id` to pass to
    `calendar_list_events`/`calendar_create_event`/`calendar_delete_event`.
    Not for: the events within a calendar — use `calendar_list_events`.
    Returns: list of `{"entity_id", "name"}` dicts, or an empty list if the
    response isn't a JSON list.
    """
    with ha._client() as c:
        r = c.get("/api/calendars")
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


@mcp.tool(annotations=read("List events on a calendar"))
def list_events(
    entity_id: Annotated[
        str,
        Field(description="Calendar entity ID, e.g. 'calendar.family', from `calendar_list_calendars`."),
    ],
    start: Annotated[
        str,
        Field(description="Range start as an ISO 8601 datetime, e.g. '2026-04-28T00:00:00'."),
    ],
    end: Annotated[
        str,
        Field(description="Range end as an ISO 8601 datetime, e.g. '2026-05-05T00:00:00'."),
    ],
) -> list[dict]:
    """List events on one calendar between `start` and `end`.

    Calls `GET /api/calendars/{entity_id}` over HTTP with `start`/`end` as
    query parameters.

    Use when: reading a calendar's upcoming or past events for a known date
    range.
    Not for: listing the calendar entities themselves — use
    `calendar_list_calendars`.
    Returns: list of event dicts as returned by HA (fields include `summary`,
    `start`, `end`, `uid`), or an empty list if the response isn't a JSON
    list.
    """
    with ha._client() as c:
        r = c.get(
            f"/api/calendars/{entity_id}",
            params={"start": start, "end": end},
        )
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


@mcp.tool(annotations=write("Create a calendar event", idempotent=False))
def create_event(
    entity_id: Annotated[
        str,
        Field(description="Calendar entity ID to add the event to, e.g. 'calendar.family'."),
    ],
    summary: Annotated[
        str,
        Field(description="Event title, e.g. 'Dentist appointment'."),
    ],
    start: Annotated[
        str,
        Field(description="Event start as an ISO 8601 datetime, e.g. '2026-04-28T10:00:00'."),
    ],
    end: Annotated[
        str,
        Field(description="Event end as an ISO 8601 datetime, e.g. '2026-04-28T11:00:00'."),
    ],
    description: Annotated[
        str | None,
        Field(description="Optional longer event description/notes. Omit for none."),
    ] = None,
    location: Annotated[
        str | None,
        Field(description="Optional event location text. Omit for none."),
    ] = None,
) -> dict:
    """Create a new event on a calendar via the `calendar.create_event` service.

    Maps `start`/`end` to the service's `start_date_time`/`end_date_time`
    fields. Calling this twice with identical arguments creates two
    separate events, since HA assigns each a new `uid`.

    Use when: adding a new event to a calendar that supports creation (local
    or CalDAV calendars).
    Not for: removing an event — use `calendar_delete_event`.
    Returns: `{"entity_id", "summary", "start", "end", "result": ...}` where
    `result` is the service-call response.
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


@mcp.tool(annotations=destructive("Delete a calendar event", idempotent=True))
def delete_event(
    entity_id: Annotated[
        str,
        Field(description="Calendar entity ID the event belongs to, e.g. 'calendar.family'."),
    ],
    uid: Annotated[
        str,
        Field(description="Event UID to delete, as returned by `calendar_list_events`."),
    ],
    recurrence_id: Annotated[
        str | None,
        Field(
            description=(
                "Narrows deletion to one instance of a recurring event. "
                "Omit to target the whole event/series."
            )
        ),
    ] = None,
    recurrence_range: Annotated[
        str | None,
        Field(
            description=(
                "Extends `recurrence_id` deletion to a range, e.g. "
                "'THISANDFUTURE'. Omit for a single-instance deletion."
            )
        ),
    ] = None,
) -> dict:
    """Delete a calendar event by its `uid` via WS `calendar/event/delete`.

    Only works for calendar platforms that declare
    `CalendarEntityFeature.DELETE_EVENT` (e.g. local/CalDAV calendars);
    read-only integrations such as Google Calendar reject it, and this
    returns `ok: False` with HA's error instead of raising.

    Use when: removing an event or a recurring instance from a calendar
    that supports deletion.
    Not for: creating an event — use `calendar_create_event`; not for
    read-only calendar integrations, which will fail here regardless of
    arguments.
    Returns: `{"entity_id", "uid", "result": ..., "ok": True}` on success.
    Errors: `{"entity_id", "uid", "ok": False, "error": str(e)}` when the
    platform rejects the deletion or the WS call otherwise raises.
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
