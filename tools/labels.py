from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("labels")

TOOL_CONTRACT = {
    "version": 1,
    "long_ok": {},
    "heuristic_exceptions": {
        "labels_remove_label_from_entity": (
            "Only drops a caller-supplied label_id from an entity's own labels list "
            "(R5b, all lost information came from the caller); it does not delete the "
            "label itself, unlike labels_delete_label which is destructive."
        ),
        "labels_remove_label_from_device": (
            "Only drops a caller-supplied label_id from a device's own labels list "
            "(R5b, all lost information came from the caller); it does not delete the "
            "label itself, unlike labels_delete_label which is destructive."
        ),
    },
}


# --- Labels ---


@mcp.tool(annotations=read("List labels"))
def list_labels() -> list[dict]:
    """List all labels defined in Home Assistant.

    Calls WS `config/label_registry/list` and returns the result unfiltered;
    returns an empty list if the WS response is not a list.

    Use when: you need label IDs and names before filtering or assigning
    them to entities or devices.
    Returns: list of label registry dicts (label_id, name, icon, color,
    description).
    Errors: on a WS failure, returns `[{"error": "<message>"}]` instead of
    raising."""
    try:
        result = ha._ws_call("config/label_registry/list")
        return result if isinstance(result, list) else []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool(annotations=write("Create label", idempotent=False))
def create_label(
    name: Annotated[str, Field(description="Display name for the new label, e.g. 'Battery Low'.")],
    icon: Annotated[str | None, Field(description="Optional mdi icon identifier, e.g. 'mdi:alert'; omit for no icon.")] = None,
    color: Annotated[str | None, Field(description="Optional label color name recognised by the frontend, e.g. 'red'; omit for no color.")] = None,
    description: Annotated[str | None, Field(description="Optional free-text description of the label's purpose; omit for none.")] = None,
) -> dict:
    """Create a new label.

    Calls WS `config/label_registry/create` with `name` and any of `icon`,
    `color` and `description` you pass; Home Assistant assigns a new
    label_id, so calling this twice with the same name creates two separate
    labels.

    Use when: you need a new label to tag entities or devices with.
    Returns: the created label registry dict (label_id, name, icon, color,
    description).
    Errors: on a WS failure, returns `{"error": "<message>"}` instead of
    raising."""
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


@mcp.tool(annotations=destructive("Update label", idempotent=False))
def update_label(
    label_id: Annotated[str, Field(description="Label registry ID to update; obtain it from labels_list_labels.")],
    name: Annotated[str | None, Field(description="New display name; omit to leave the current name unchanged.")] = None,
    icon: Annotated[str | None, Field(description="New mdi icon identifier, e.g. 'mdi:alert'; omit to leave the current icon unchanged.")] = None,
    color: Annotated[str | None, Field(description="New label color name; omit to leave the current color unchanged.")] = None,
    description: Annotated[str | None, Field(description="New free-text description; omit to leave the current description unchanged.")] = None,
) -> dict:
    """Update fields on an existing label.

    Calls WS `config/label_registry/update`; only the parameters you pass
    are sent, so omitted fields keep their current value.

    Use when: renaming a label or changing its icon, color or description.
    Returns: the updated label registry dict.
    Errors: on a WS failure, returns `{"error": "<message>"}` instead of
    raising."""
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


@mcp.tool(annotations=destructive("Delete label", idempotent=True))
def delete_label(
    label_id: Annotated[str, Field(description="Label registry ID to delete; obtain it from labels_list_labels.")],
) -> dict:
    """Delete a label from the label registry.

    Calls WS `config/label_registry/delete` with the given `label_id` and
    returns Home Assistant's result unchanged.

    Use when: a label is no longer needed.
    Returns: dict with `label_id`, the raw WS `result`, and `ok` (true on
    success).
    Errors: on a WS failure, returns `{"label_id": ..., "ok": false,
    "error": "<message>"}` instead of raising."""
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


@mcp.tool(annotations=write("Assign label to entity", idempotent=True))
def assign_label_to_entity(
    entity_id: Annotated[str, Field(description="Entity ID to tag, e.g. 'light.kitchen'.")],
    label_id: Annotated[str, Field(description="Label registry ID to assign; obtain it from labels_list_labels.")],
) -> dict:
    """Assign a label to an entity, keeping its existing labels.

    Reads the entity's current `labels` list from the entity registry,
    appends `label_id` if not already present, and writes the full list
    back via WS `config/entity_registry/update`.

    Use when: tagging one entity with a label without disturbing labels
    already on it.
    Not for: removing a label — use `labels_remove_label_from_entity`.
    Returns: dict with `entity_id`, the resulting `labels` list, the raw WS
    `result`, and `ok`.
    Errors: on a WS failure, returns `{"entity_id": ..., "label_id": ...,
    "ok": false, "error": "<message>"}`."""
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


@mcp.tool(annotations=write("Remove label from entity", idempotent=True))
def remove_label_from_entity(
    entity_id: Annotated[str, Field(description="Entity ID to untag, e.g. 'light.kitchen'.")],
    label_id: Annotated[str, Field(description="Label registry ID to remove; obtain it from labels_list_labels.")],
) -> dict:
    """Remove a label from an entity, keeping its other labels.

    Reads the entity's current `labels` list from the entity registry,
    drops `label_id` if present, and writes the remaining list back via WS
    `config/entity_registry/update`.

    Use when: untagging one entity from a label without touching its other
    labels.
    Not for: deleting the label itself everywhere — use
    `labels_delete_label`.
    Returns: dict with `entity_id`, the resulting `labels` list, the raw WS
    `result`, and `ok`.
    Errors: on a WS failure, returns `{"entity_id": ..., "label_id": ...,
    "ok": false, "error": "<message>"}`."""
    try:
        labels = _entity_labels(entity_id)
        labels = [lbl for lbl in labels if lbl != label_id]
        result = ha._ws_call(
            "config/entity_registry/update",
            entity_id=entity_id,
            labels=labels,
        )
        return {"entity_id": entity_id, "labels": labels, "result": result, "ok": True}
    except Exception as e:
        return {"entity_id": entity_id, "label_id": label_id, "ok": False, "error": str(e)}


@mcp.tool(annotations=write("Assign label to device", idempotent=True))
def assign_label_to_device(
    device_id: Annotated[str, Field(description="Device registry ID to tag; obtain it from devices_list_devices.")],
    label_id: Annotated[str, Field(description="Label registry ID to assign; obtain it from labels_list_labels.")],
) -> dict:
    """Assign a label to a device, keeping its existing labels.

    Reads the device's current `labels` list from the device registry,
    appends `label_id` if not already present, and writes the full list
    back via WS `config/device_registry/update`.

    Use when: tagging one device with a label without disturbing labels
    already on it.
    Not for: removing a label — use `labels_remove_label_from_device`.
    Returns: dict with `device_id`, the resulting `labels` list, the raw WS
    `result`, and `ok`.
    Errors: on a WS failure, returns `{"device_id": ..., "label_id": ...,
    "ok": false, "error": "<message>"}`."""
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


@mcp.tool(annotations=write("Remove label from device", idempotent=True))
def remove_label_from_device(
    device_id: Annotated[str, Field(description="Device registry ID to untag; obtain it from devices_list_devices.")],
    label_id: Annotated[str, Field(description="Label registry ID to remove; obtain it from labels_list_labels.")],
) -> dict:
    """Remove a label from a device, keeping its other labels.

    Reads the device's current `labels` list from the device registry,
    drops `label_id` if present, and writes the remaining list back via WS
    `config/device_registry/update`.

    Use when: untagging one device from a label without touching its other
    labels.
    Not for: deleting the label itself everywhere — use
    `labels_delete_label`.
    Returns: dict with `device_id`, the resulting `labels` list, the raw WS
    `result`, and `ok`.
    Errors: on a WS failure, returns `{"device_id": ..., "label_id": ...,
    "ok": false, "error": "<message>"}`."""
    try:
        labels = _device_labels(device_id)
        labels = [lbl for lbl in labels if lbl != label_id]
        result = ha._ws_call(
            "config/device_registry/update",
            device_id=device_id,
            labels=labels,
        )
        return {"device_id": device_id, "labels": labels, "result": result, "ok": True}
    except Exception as e:
        return {"device_id": device_id, "label_id": label_id, "ok": False, "error": str(e)}


@mcp.tool(annotations=read("List entities with label"))
def list_entities_with_label(
    label_id: Annotated[str, Field(description="Label registry ID to filter by; obtain it from labels_list_labels.")],
) -> list[dict]:
    """List entities that currently have a given label assigned.

    Reads the entity registry over WebSocket and keeps rows whose `labels`
    list contains `label_id`.

    Use when: finding everything tagged with one label, e.g. before a bulk
    action.
    Returns: list of entity registry dicts that have the label.
    Errors: on a WS failure, returns `[{"error": "<message>"}]` instead of
    raising."""
    try:
        registry = ha._ws_call("config/entity_registry/list") or []
        return [e for e in registry if label_id in (e.get("labels") or [])]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool(annotations=read("List devices with label"))
def list_devices_with_label(
    label_id: Annotated[str, Field(description="Label registry ID to filter by; obtain it from labels_list_labels.")],
) -> list[dict]:
    """List devices that currently have a given label assigned.

    Reads the device registry over WebSocket and keeps rows whose `labels`
    list contains `label_id`.

    Use when: finding everything tagged with one label, e.g. before a bulk
    action.
    Returns: list of device registry dicts that have the label.
    Errors: on a WS failure, returns `[{"error": "<message>"}]` instead of
    raising."""
    try:
        registry = ha._ws_call("config/device_registry/list") or []
        return [d for d in registry if label_id in (d.get("labels") or [])]
    except Exception as e:
        return [{"error": str(e)}]


# --- Categories ---


@mcp.tool(annotations=read("List categories"))
def list_categories(
    scope: Annotated[str, Field(description="Registry scope the categories belong to, e.g. 'automation'; defaults to 'automation'.")] = "automation",
) -> list[dict]:
    """List categories defined for a given registry scope.

    Calls WS `config/category_registry/list` with `scope` and returns the
    result unfiltered; returns an empty list if the WS response is not a
    list.

    Use when: you need category IDs for one scope, e.g. before tagging
    automations.
    Returns: list of category registry dicts (category_id, name, icon) for
    the given scope.
    Errors: on a WS failure, returns `[{"error": "<message>"}]` instead of
    raising."""
    try:
        result = ha._ws_call("config/category_registry/list", scope=scope)
        return result if isinstance(result, list) else []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool(annotations=write("Create category", idempotent=False))
def create_category(
    scope: Annotated[str, Field(description="Registry scope the new category belongs to, e.g. 'automation'.")],
    name: Annotated[str, Field(description="Display name for the new category.")],
    icon: Annotated[str | None, Field(description="Optional mdi icon identifier, e.g. 'mdi:tag'; omit for no icon.")] = None,
) -> dict:
    """Create a category within a registry scope.

    Calls WS `config/category_registry/create` with `scope` and `name`,
    plus `icon` if given; Home Assistant assigns a new category_id, so
    calling this twice with the same name creates two separate categories.

    Use when: you need a new category to group items within one scope, e.g.
    automations.
    Returns: the created category registry dict.
    Errors: on a WS failure, returns `{"error": "<message>"}` instead of
    raising."""
    payload: dict = {"scope": scope, "name": name}
    if icon is not None:
        payload["icon"] = icon
    try:
        return ha._ws_call("config/category_registry/create", **payload)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=destructive("Update category", idempotent=False))
def update_category(
    scope: Annotated[str, Field(description="Registry scope the category belongs to, e.g. 'automation'.")],
    category_id: Annotated[str, Field(description="Category registry ID to update; obtain it from labels_list_categories.")],
    name: Annotated[str | None, Field(description="New display name; omit to leave the current name unchanged.")] = None,
    icon: Annotated[str | None, Field(description="New mdi icon identifier; omit to leave the current icon unchanged.")] = None,
) -> dict:
    """Update fields on an existing category.

    Calls WS `config/category_registry/update`; only the parameters you
    pass are sent, so omitted fields keep their current value.

    Use when: renaming a category or changing its icon within its scope.
    Returns: the updated category registry dict.
    Errors: on a WS failure, returns `{"error": "<message>"}` instead of
    raising."""
    payload: dict = {"scope": scope, "category_id": category_id}
    if name is not None:
        payload["name"] = name
    if icon is not None:
        payload["icon"] = icon
    try:
        return ha._ws_call("config/category_registry/update", **payload)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=destructive("Delete category", idempotent=True))
def delete_category(
    scope: Annotated[str, Field(description="Registry scope the category belongs to, e.g. 'automation'.")],
    category_id: Annotated[str, Field(description="Category registry ID to delete; obtain it from labels_list_categories.")],
) -> dict:
    """Delete a category from a registry scope.

    Calls WS `config/category_registry/delete` with `scope` and
    `category_id` and returns Home Assistant's result unchanged.

    Use when: a category is no longer needed within its scope.
    Returns: dict with `scope`, `category_id`, the raw WS `result`, and
    `ok`.
    Errors: on a WS failure, returns `{"scope": ..., "category_id": ...,
    "ok": false, "error": "<message>"}`."""
    try:
        result = ha._ws_call(
            "config/category_registry/delete",
            scope=scope,
            category_id=category_id,
        )
        return {"scope": scope, "category_id": category_id, "result": result, "ok": True}
    except Exception as e:
        return {"scope": scope, "category_id": category_id, "ok": False, "error": str(e)}
