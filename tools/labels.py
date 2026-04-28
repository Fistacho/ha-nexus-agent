from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("labels")


# --- Labels ---


@mcp.tool()
def list_labels() -> list[dict]:
    """List all labels via WS `config/label_registry/list`."""
    try:
        result = ha._ws_call("config/label_registry/list")
        return result if isinstance(result, list) else []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def create_label(
    name: str,
    icon: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> dict:
    """Create a new label via WS `config/label_registry/create`."""
    payload: dict = {"name": name}
    if icon is not None:
        payload["icon"] = icon
    if color is not None:
        payload["color"] = color
    if description is not None:
        payload["description"] = description
    try:
        return ha._ws_call("config/label_registry/create", **payload)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def update_label(
    label_id: str,
    name: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an existing label via WS `config/label_registry/update`."""
    payload: dict = {"label_id": label_id}
    if name is not None:
        payload["name"] = name
    if icon is not None:
        payload["icon"] = icon
    if color is not None:
        payload["color"] = color
    if description is not None:
        payload["description"] = description
    try:
        return ha._ws_call("config/label_registry/update", **payload)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def delete_label(label_id: str) -> dict:
    """Delete a label via WS `config/label_registry/delete`."""
    try:
        result = ha._ws_call("config/label_registry/delete", label_id=label_id)
        return {"label_id": label_id, "result": result, "ok": True}
    except Exception as e:
        return {"label_id": label_id, "ok": False, "error": str(e)}


def _entity_labels(entity_id: str) -> list[str]:
    """Return current labels list for an entity from entity registry."""
    registry = ha._ws_call("config/entity_registry/list") or []
    for entry in registry:
        if entry.get("entity_id") == entity_id:
            labels = entry.get("labels") or []
            return list(labels)
    return []


def _device_labels(device_id: str) -> list[str]:
    """Return current labels list for a device from device registry."""
    registry = ha._ws_call("config/device_registry/list") or []
    for entry in registry:
        if entry.get("id") == device_id:
            labels = entry.get("labels") or []
            return list(labels)
    return []


@mcp.tool()
def assign_label_to_entity(entity_id: str, label_id: str) -> dict:
    """Assign a label to an entity (preserves existing labels)."""
    try:
        labels = _entity_labels(entity_id)
        if label_id not in labels:
            labels.append(label_id)
        result = ha._ws_call(
            "config/entity_registry/update",
            entity_id=entity_id,
            labels=labels,
        )
        return {"entity_id": entity_id, "labels": labels, "result": result, "ok": True}
    except Exception as e:
        return {"entity_id": entity_id, "label_id": label_id, "ok": False, "error": str(e)}


@mcp.tool()
def remove_label_from_entity(entity_id: str, label_id: str) -> dict:
    """Remove a label from an entity (preserves remaining labels)."""
    try:
        labels = _entity_labels(entity_id)
        labels = [l for l in labels if l != label_id]
        result = ha._ws_call(
            "config/entity_registry/update",
            entity_id=entity_id,
            labels=labels,
        )
        return {"entity_id": entity_id, "labels": labels, "result": result, "ok": True}
    except Exception as e:
        return {"entity_id": entity_id, "label_id": label_id, "ok": False, "error": str(e)}


@mcp.tool()
def assign_label_to_device(device_id: str, label_id: str) -> dict:
    """Assign a label to a device (preserves existing labels)."""
    try:
        labels = _device_labels(device_id)
        if label_id not in labels:
            labels.append(label_id)
        result = ha._ws_call(
            "config/device_registry/update",
            device_id=device_id,
            labels=labels,
        )
        return {"device_id": device_id, "labels": labels, "result": result, "ok": True}
    except Exception as e:
        return {"device_id": device_id, "label_id": label_id, "ok": False, "error": str(e)}


@mcp.tool()
def remove_label_from_device(device_id: str, label_id: str) -> dict:
    """Remove a label from a device (preserves remaining labels)."""
    try:
        labels = _device_labels(device_id)
        labels = [l for l in labels if l != label_id]
        result = ha._ws_call(
            "config/device_registry/update",
            device_id=device_id,
            labels=labels,
        )
        return {"device_id": device_id, "labels": labels, "result": result, "ok": True}
    except Exception as e:
        return {"device_id": device_id, "label_id": label_id, "ok": False, "error": str(e)}


@mcp.tool()
def list_entities_with_label(label_id: str) -> list[dict]:
    """List all entities that have the given label assigned."""
    try:
        registry = ha._ws_call("config/entity_registry/list") or []
        return [e for e in registry if label_id in (e.get("labels") or [])]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def list_devices_with_label(label_id: str) -> list[dict]:
    """List all devices that have the given label assigned."""
    try:
        registry = ha._ws_call("config/device_registry/list") or []
        return [d for d in registry if label_id in (d.get("labels") or [])]
    except Exception as e:
        return [{"error": str(e)}]


# --- Categories ---


@mcp.tool()
def list_categories(scope: str = "automation") -> list[dict]:
    """List categories for a scope (e.g. 'automation') via WS `config/category_registry/list`."""
    try:
        result = ha._ws_call("config/category_registry/list", scope=scope)
        return result if isinstance(result, list) else []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def create_category(scope: str, name: str, icon: str | None = None) -> dict:
    """Create a category in the given scope via WS `config/category_registry/create`."""
    payload: dict = {"scope": scope, "name": name}
    if icon is not None:
        payload["icon"] = icon
    try:
        return ha._ws_call("config/category_registry/create", **payload)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def update_category(
    scope: str,
    category_id: str,
    name: str | None = None,
    icon: str | None = None,
) -> dict:
    """Update a category via WS `config/category_registry/update`."""
    payload: dict = {"scope": scope, "category_id": category_id}
    if name is not None:
        payload["name"] = name
    if icon is not None:
        payload["icon"] = icon
    try:
        return ha._ws_call("config/category_registry/update", **payload)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def delete_category(scope: str, category_id: str) -> dict:
    """Delete a category via WS `config/category_registry/delete`."""
    try:
        result = ha._ws_call(
            "config/category_registry/delete",
            scope=scope,
            category_id=category_id,
        )
        return {"scope": scope, "category_id": category_id, "result": result, "ok": True}
    except Exception as e:
        return {"scope": scope, "category_id": category_id, "ok": False, "error": str(e)}
