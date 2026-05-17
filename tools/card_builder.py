"""Card Builder integration tools.

Wraps the WebSocket API exposed by the `card_builder` custom integration
(studiobts/home-assistant-card-builder). The integration registers four
collections via Home Assistant's `DictStorageCollectionWebsocket`:

  - cards                  → card_builder/cards/{list,create,update,delete}
  - style_presets          → card_builder/style_presets/{list,create,update,delete}
  - css_custom_properties  → card_builder/css_custom_properties/{list,create,update,delete}

…and a media manager for the `<config>/www/card_builder/` directory:

  - card_builder/media/{list,upload,delete}

A "card" lives in HA storage and is referenced from a dashboard with
`type: custom:card-builder-renderer-card` + `card_id`.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

import ha_client as ha

mcp = FastMCP("card_builder")


# --- Cards ---------------------------------------------------------------

@mcp.tool()
def list_cards() -> list[dict]:
    """List all Card Builder cards (id, name, description, config, version, tags, …)."""
    return ha._ws_call("card_builder/cards/list")


@mcp.tool()
def get_card(card_id: str) -> dict:
    """Get one card by id. Returns the card dict (including `config`) or an error."""
    for card in ha._ws_call("card_builder/cards/list") or []:
        if card.get("id") == card_id or card.get("card_id") == card_id:
            return card
    return {"error": "not_found", "card_id": card_id}


@mcp.tool()
def create_card(
    name: str,
    config: dict,
    description: str = "",
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    author: str = "",
) -> dict:
    """Create a new Card Builder card.

    `config` is the full block tree (drag-and-drop structure) the panel produces.
    To bootstrap, design a card in the UI, then read it via list_cards and reuse
    its `config` as a template.
    """
    payload: dict[str, Any] = {
        "name": name,
        "config": config,
        "description": description,
    }
    if tags:
        payload["tags"] = tags
    if categories:
        payload["categories"] = categories
    if author:
        payload["author"] = author
    return ha._ws_call("card_builder/cards/create", **payload)


@mcp.tool()
def update_card(
    card_id: str,
    name: str | None = None,
    description: str | None = None,
    config: dict | None = None,
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    skip_version_bump: bool = False,
) -> dict:
    """Update an existing card. Only the provided fields are changed."""
    payload: dict[str, Any] = {"card_id": card_id}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if config is not None:
        payload["config"] = config
    if tags is not None:
        payload["tags"] = tags
    if categories is not None:
        payload["categories"] = categories
    if skip_version_bump:
        payload["_skip_version_bump"] = True
    return ha._ws_call("card_builder/cards/update", **payload)


@mcp.tool()
def delete_card(card_id: str) -> dict:
    """Delete a card by id."""
    return ha._ws_call("card_builder/cards/delete", card_id=card_id)


# --- Style presets -------------------------------------------------------

@mcp.tool()
def list_style_presets() -> list[dict]:
    """List all reusable style presets."""
    return ha._ws_call("card_builder/style_presets/list")


@mcp.tool()
def create_style_preset(
    name: str,
    data: dict,
    description: str = "",
    extends_preset_id: str | None = None,
) -> dict:
    """Create a style preset. `data` is the CSS-style configuration blob."""
    payload: dict[str, Any] = {"name": name, "data": data, "description": description}
    if extends_preset_id:
        payload["extends_preset_id"] = extends_preset_id
    return ha._ws_call("card_builder/style_presets/create", **payload)


@mcp.tool()
def update_style_preset(
    style_preset_id: str,
    name: str | None = None,
    description: str | None = None,
    data: dict | None = None,
    extends_preset_id: str | None = None,
) -> dict:
    """Update a style preset."""
    payload: dict[str, Any] = {"style_preset_id": style_preset_id}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if data is not None:
        payload["data"] = data
    if extends_preset_id is not None:
        payload["extends_preset_id"] = extends_preset_id
    return ha._ws_call("card_builder/style_presets/update", **payload)


@mcp.tool()
def delete_style_preset(style_preset_id: str) -> dict:
    """Delete a style preset."""
    return ha._ws_call("card_builder/style_presets/delete", style_preset_id=style_preset_id)


# --- CSS custom properties ----------------------------------------------

@mcp.tool()
def list_css_custom_properties() -> list[dict]:
    """List CSS custom properties (`@property` registrations)."""
    return ha._ws_call("card_builder/css_custom_properties/list")


@mcp.tool()
def create_css_custom_property(
    name: str,
    syntax: str,
    initial_value: str,
    inherits: bool = False,
) -> dict:
    """Register a CSS custom property. `syntax` is e.g. `"<color>"` or `"<length>"`."""
    return ha._ws_call(
        "card_builder/css_custom_properties/create",
        name=name,
        syntax=syntax,
        initial_value=initial_value,
        inherits=inherits,
    )


@mcp.tool()
def delete_css_custom_property(custom_property_id: str) -> dict:
    """Delete a CSS custom property."""
    return ha._ws_call(
        "card_builder/css_custom_properties/delete",
        custom_property_id=custom_property_id,
    )


# --- Media manager (config/www/card_builder/) ---------------------------

@mcp.tool()
def list_media(path: str = "") -> dict:
    """List files/folders in the Card Builder media directory (relative to `www/card_builder/`)."""
    return ha._ws_call("card_builder/media/list", path=path)


@mcp.tool()
def upload_media(filename: str, content_base64: str, path: str = "") -> dict:
    """Upload a file to the Card Builder media directory.

    `content_base64` must be the raw bytes already base64-encoded. Returns the
    `cb-media://` reference plus a `/local/...` URL usable in any card.
    """
    # Validate base64 early — HA's WS handler will reject it otherwise.
    try:
        base64.b64decode(content_base64, validate=True)
    except Exception as err:
        return {"error": "invalid_base64", "detail": str(err)}
    return ha._ws_call(
        "card_builder/media/upload",
        path=path,
        filename=filename,
        content=content_base64,
    )


@mcp.tool()
def upload_media_from_path(local_path: str, path: str = "", filename: str | None = None) -> dict:
    """Upload a local file (on the nexus host) to the Card Builder media directory."""
    src = Path(local_path)
    if not src.is_file():
        return {"error": "file_not_found", "local_path": local_path}
    payload_b64 = base64.b64encode(src.read_bytes()).decode("ascii")
    return ha._ws_call(
        "card_builder/media/upload",
        path=path,
        filename=filename or src.name,
        content=payload_b64,
    )


@mcp.tool()
def delete_media(path: str) -> dict:
    """Delete a file from the Card Builder media directory by its relative path."""
    return ha._ws_call("card_builder/media/delete", path=path)


# --- Dashboard helper ----------------------------------------------------

@mcp.tool()
def renderer_card_config(card_id: str, entity_slots: dict | None = None) -> dict:
    """Build a Lovelace card config that renders a Card Builder card on a dashboard.

    Use the returned dict as a card in `dashboards_add_card_to_view` (or in a
    saved dashboard config). `entity_slots` maps slot name → entity_id when the
    card defines reusable slots.
    """
    config: dict[str, Any] = {
        "type": "custom:card-builder-renderer-card",
        "card_id": card_id,
    }
    if entity_slots:
        config["entity_slots"] = entity_slots
    return config
