"""Todo list entities: list lists, list/add/update/remove items.

Reads and item mutations go through HA's WebSocket API (`todo/item/*`) and
the `todo.*` services; the list of todo entities itself comes from plain
entity states.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("todo")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("List todo list entities"))
def list_todo_lists() -> list[dict]:
    """List all `todo.*` entities currently registered in HA.

    Filters `ha.get_states()` for entity IDs starting with `todo.`.

    Use when: discovering which `entity_id` to pass to
    `todo_list_items`/`todo_add_item`/`todo_update_item`/`todo_remove_item`.
    Not for: the items within a list — use `todo_list_items`.
    Returns: list of `{"entity_id", "state", "friendly_name"}` dicts, one
    per `todo.*` entity.
    """
    states = ha.get_states()
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name"),
        }
        for s in states
        if s["entity_id"].startswith("todo.")
    ]


@mcp.tool(annotations=read("List items on a todo list"))
def list_items(
    entity_id: Annotated[
        str,
        Field(description="Todo list entity ID, e.g. 'todo.shopping_list', from `todo_list_todo_lists`."),
    ],
    status: Annotated[
        str,
        Field(
            description=(
                "Client-side status filter: 'needs_action', 'completed', or "
                "'all' for no filter."
            )
        ),
    ] = "needs_action",
) -> list[dict]:
    """List items on a todo list, optionally filtered by status.

    Calls WS `todo/item/list` and filters the returned items by `status` in
    Python (HA's response shape for this command has varied between "items"
    dicts and a bare list across versions, both are handled here).

    Use when: reading a todo list's current items.
    Not for: listing the todo entities themselves — use
    `todo_list_todo_lists`.
    Returns: list of item dicts as returned by HA (fields include `uid`,
    `summary`/`item` text, `status`).
    """
    result = ha._ws_call("todo/item/list", entity_id=entity_id)

    # HA returns either {"items": [...]} or a list directly depending on version.
    if isinstance(result, dict):
        items = result.get("items", [])
    else:
        items = result or []

    if status and status != "all":
        items = [i for i in items if i.get("status") == status]
    return list(items)


@mcp.tool(annotations=write("Add a todo list item", idempotent=False))
def add_item(
    entity_id: Annotated[
        str,
        Field(description="Todo list entity ID to add the item to, e.g. 'todo.shopping_list'."),
    ],
    item: Annotated[
        str,
        Field(description="Item text, e.g. 'Buy milk'."),
    ],
    due_date: Annotated[
        str | None,
        Field(
            description=(
                "Optional due date ('YYYY-MM-DD') or datetime ISO string. "
                "Omit for no due date."
            )
        ),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Optional longer item description/notes. Omit for none."),
    ] = None,
) -> dict:
    """Add a new item to a todo list via the `todo.add_item` service.

    Sends `due_date` or `due_datetime` depending on whether `due_date`
    contains a time component ('T' or a space). Calling this twice with the
    same `item` text creates two separate items with distinct `uid`s.

    Use when: adding a new entry to a todo list.
    Not for: changing or completing an existing item — use
    `todo_update_item`.
    Returns: `{"entity_id", "item", "result": ...}` where `result` is the
    service-call response.
    """
    data: dict = {"entity_id": entity_id, "item": item}
    if due_date:
        # HA accepts either due_date or due_datetime
        if "T" in due_date or " " in due_date:
            data["due_datetime"] = due_date
        else:
            data["due_date"] = due_date
    if description:
        data["description"] = description
    result = ha.call_service("todo", "add_item", data)
    return {"entity_id": entity_id, "item": item, "result": result}


@mcp.tool(annotations=destructive("Update a todo list item", idempotent=False))
def update_item(
    entity_id: Annotated[
        str,
        Field(description="Todo list entity ID the item belongs to, e.g. 'todo.shopping_list'."),
    ],
    uid: Annotated[
        str,
        Field(description="Item UID to update, as returned by `todo_list_items`."),
    ],
    item: Annotated[
        str | None,
        Field(description="New item text. Omit to keep the current text."),
    ] = None,
    status: Annotated[
        str | None,
        Field(
            description=(
                "New status: 'needs_action' or 'completed'. Omit to keep "
                "the current status."
            )
        ),
    ] = None,
) -> dict:
    """Update a todo item's text and/or status via the `todo.update_item` service.

    Maps `item` to the service's `rename` field. Only the fields you pass
    (`item`/`status`) are sent, so whichever value is omitted is left
    unchanged on HA's side.

    Use when: renaming an item or marking it needs_action/completed.
    Not for: adding a new item — use `todo_add_item`; not for removing one
    — use `todo_remove_item`.
    Returns: `{"entity_id", "uid", "result": ...}` where `result` is the
    service-call response.
    """
    data: dict = {"entity_id": entity_id, "item": uid}
    if item is not None:
        data["rename"] = item
    if status is not None:
        data["status"] = status
    result = ha.call_service("todo", "update_item", data)
    return {"entity_id": entity_id, "uid": uid, "result": result}


@mcp.tool(annotations=destructive("Remove a todo list item", idempotent=True))
def remove_item(
    entity_id: Annotated[
        str,
        Field(description="Todo list entity ID the item belongs to, e.g. 'todo.shopping_list'."),
    ],
    uid: Annotated[
        str,
        Field(description="Item UID to remove, as returned by `todo_list_items`."),
    ],
) -> dict:
    """Remove an item from a todo list by its `uid` via the `todo.remove_item` service.

    Use when: permanently deleting an item, e.g. after completing a task
    you don't want to keep marked completed.
    Not for: marking an item completed without deleting it — use
    `todo_update_item` with `status='completed'`.
    Returns: `{"entity_id", "uid", "result": ...}` where `result` is the
    service-call response.
    """
    result = ha.call_service(
        "todo", "remove_item",
        {"entity_id": entity_id, "item": uid},
    )
    return {"entity_id": entity_id, "uid": uid, "result": result}
