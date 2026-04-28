from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("todo")


@mcp.tool()
def list_todo_lists() -> list[dict]:
    """List all `todo.*` entities currently registered in HA."""
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


@mcp.tool()
def list_items(entity_id: str, status: str = "needs_action") -> list[dict]:
    """List items in a todo list via WS `todo/item/list`.

    `status` filters items client-side: typical values are
    "needs_action", "completed", or "all" (no filter).
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


@mcp.tool()
def add_item(
    entity_id: str,
    item: str,
    due_date: str | None = None,
    description: str | None = None,
) -> dict:
    """Add a new item to a todo list (`todo.add_item` service).

    `due_date` may be a date (YYYY-MM-DD) or datetime ISO string.
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


@mcp.tool()
def update_item(
    entity_id: str,
    uid: str,
    item: str | None = None,
    status: str | None = None,
) -> dict:
    """Update a todo item by `uid`. Optional new `item` text and `status`.

    `status` should be "needs_action" or "completed".
    """
    data: dict = {"entity_id": entity_id, "item": uid}
    if item is not None:
        data["rename"] = item
    if status is not None:
        data["status"] = status
    result = ha.call_service("todo", "update_item", data)
    return {"entity_id": entity_id, "uid": uid, "result": result}


@mcp.tool()
def remove_item(entity_id: str, uid: str) -> dict:
    """Remove a todo item from a list by its `uid`."""
    result = ha.call_service(
        "todo", "remove_item",
        {"entity_id": entity_id, "item": uid},
    )
    return {"entity_id": entity_id, "uid": uid, "result": result}
