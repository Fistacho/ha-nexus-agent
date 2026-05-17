"""Card Builder integration tools.

Wraps the WebSocket API exposed by the `card_builder` custom integration
(studiobts/home-assistant-card-builder) and ships **embedded knowledge** of
its block schema so AI clients can build cards in one shot — no need to
scrape the upstream repo every time.

The integration registers three storage collections via Home Assistant's
`DictStorageCollectionWebsocket`:

  - cards                  → card_builder/cards/{list,create,update,delete}
  - style_presets          → card_builder/style_presets/{list,create,update,delete}
  - css_custom_properties  → card_builder/css_custom_properties/{list,create,update,delete}

…and a media manager for the `<config>/www/card_builder/` directory:

  - card_builder/media/{list,upload,delete}

A "card" lives in HA storage and is referenced from a dashboard with
`type: custom:card-builder-renderer-card` + `card_id` (+ optional
`slot_entities` / `slot_actions`).
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

import ha_client as ha

mcp = FastMCP("card_builder")


# =========================================================================
# Embedded knowledge — synced from upstream
# https://github.com/studiobts/home-assistant-card-builder @ main
# Run `check_schema_sync()` to verify the upstream package version matches.
# =========================================================================

UPSTREAM_REPO = "studiobts/home-assistant-card-builder"
UPSTREAM_SCHEMA_SYNC = {
    "documented_against": "1.x (main, 2026-05)",
    "document_model_version": 3,
    "source_files": [
        "frontend/src/common/core/model/types.ts",
        "frontend/src/common/types/style-preset.ts",
        "frontend/src/common/core/style-resolver/style-units.ts",
        "frontend/src/common/core/style-resolver/resolved-to-css.ts",
        "frontend/src/common/blocks/components/**/*.ts",
        "docs/panel-blocks.md",
    ],
}


# Style categories with the full list of property names *Card Builder
# recognises*. Property names are camelCase (NOT kebab-case) — anything
# else is silently discarded by `resolved-to-css.ts`. Length units default
# to "px" unless noted otherwise.
STYLE_CATEGORIES: dict[str, dict[str, Any]] = {
    "layout": {
        "properties": ["display", "show", "positionX", "positionY", "zIndex"],
    },
    "size": {
        "properties": ["width", "height", "minWidth", "maxWidth", "minHeight", "maxHeight"],
        "length_units": ["px", "rem", "em", "%", "vh", "vw", "auto"],
    },
    "spacing": {
        "properties": [
            "margin", "padding",
            "marginTop", "marginRight", "marginBottom", "marginLeft",
            "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
        ],
        "length_units": ["px", "rem", "em", "%"],
        "notes": "margin/padding accept either a single value or {top, right, bottom, left} object.",
    },
    "typography": {
        "properties": [
            "fontFamily", "fontSize", "fontWeight", "fontStyle",
            "lineHeight", "letterSpacing",
            "textAlign", "textDecoration", "textTransform", "textShadow",
            "whiteSpace", "color",
        ],
    },
    "background": {
        "properties": [
            "backgroundColor", "backgroundImage", "backgroundSize",
            "backgroundPosition", "backgroundRepeat", "backgroundBlendMode",
            "boxShadow",
        ],
        "legacy_aliases": {"color": "backgroundColor", "image": "backgroundImage", "size": "backgroundSize", "repeat": "backgroundRepeat", "position": "backgroundPosition"},
    },
    "border": {
        "properties": [
            "borderWidth", "borderStyle", "borderColor", "borderRadius",
            "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
            "borderTopLeftRadius", "borderTopRightRadius",
            "borderBottomRightRadius", "borderBottomLeftRadius",
        ],
        "legacy_aliases": {"width": "borderWidth", "style": "borderStyle", "color": "borderColor", "radius": "borderRadius"},
    },
    "flex": {
        "properties": [
            "flexDirection", "justifyContent", "alignItems", "flexWrap",
            "gap", "rowGap", "columnGap",
            "flexGrow", "flexShrink", "flexBasis",
        ],
        "legacy_aliases": {"direction": "flexDirection", "justify": "justifyContent", "align": "alignItems", "wrap": "flexWrap"},
        "notes": "display:flex is set by the component itself — don't add it manually.",
    },
    "effects": {
        "properties": ["opacity", "boxShadow", "transform", "filter", "rotate"],
    },
    "svg": {
        "properties": [
            "stroke", "strokeWidth", "strokeLinecap", "strokeLinejoin",
            "strokeDasharray", "strokeDashoffset", "strokeMiterlimit",
            "strokeOpacity", "fill", "fillOpacity",
        ],
    },
}


# Button-toggle / Select-menu feature IDs, namespaced by domain. Pass one of
# these as the `feature` prop on block-button-toggle / block-select-menu — or
# leave the default "auto" to let Card Builder pick the first available.
# Source: frontend/src/common/blocks/components/controls/base-option-selector.ts
FEATURE_DEFINITIONS: dict[str, list[dict[str, str]]] = {
    "climate": [
        {"id": "climate_hvac_mode", "label": "HVAC Mode"},
        {"id": "climate_fan_mode", "label": "Fan Mode"},
        {"id": "climate_swing_mode", "label": "Swing Mode"},
        {"id": "climate_preset_mode", "label": "Preset Mode"},
    ],
    "fan": [
        {"id": "fan_power", "label": "Power"},
        {"id": "fan_preset_mode", "label": "Preset Mode"},
        {"id": "fan_speed", "label": "Speed"},
    ],
    "light": [
        {"id": "light_power", "label": "Power"},
        {"id": "light_effect", "label": "Effect"},
    ],
    "media_player": [
        {"id": "media_player_source", "label": "Source"},
        {"id": "media_player_sound_mode", "label": "Sound Mode"},
    ],
    "humidifier": [{"id": "humidifier_mode", "label": "Mode"}],
    "water_heater": [{"id": "water_heater_operation_mode", "label": "Operation Mode"}],
    "cover": [{"id": "cover_state", "label": "Open/Close"}],
    "lock": [{"id": "lock_state", "label": "Lock/Unlock"}],
    "vacuum": [{"id": "vacuum_fan_speed", "label": "Fan Speed"}],
    "switch": [{"id": "switch_power", "label": "Power"}],
    "input_boolean": [{"id": "input_boolean_state", "label": "State"}],
    "siren": [{"id": "siren_state", "label": "State"}],
    "automation": [{"id": "automation_enabled", "label": "Enabled"}],
    "select": [{"id": "select_option", "label": "Option"}],
    "input_select": [{"id": "input_select_option", "label": "Option"}],
}


# Style targets per block — sub-components you can style independently.
# Source: docs/panel-blocks.md. "block" is the default (whole element);
# blocks with sub-components add named targets. Active targets layer on top
# of base ones (e.g. "optionActive" overrides "option" only for selected).
STYLE_TARGETS: dict[str, list[str]] = {
    "block-text": ["block"],
    "block-icon": ["block", "icon", "preTemplate", "postTemplate"],
    "block-image": ["block"],
    "block-container": ["block"],
    "block-columns": ["block"],
    "block-grid": ["block"],
    "block-drop-zone": ["block"],
    "block-entity-field-state": ["block", "state", "unit"],
    "block-entity-field-name": ["block"],
    "block-entity-field-icon": ["block"],
    "block-entity-field-attribute": ["block", "label", "value"],
    "block-entity-field-image": ["block"],
    "block-slider": [
        "track", "trackInactive", "trackActive",
        "thumb", "thumbLow", "thumbHigh",
        "value", "tooltip",
    ],
    "block-button-toggle": [
        "container", "option", "optionActive",
        "icon", "iconActive", "label", "labelActive",
    ],
    "block-select-menu": [
        "container", "dropdown",
        "containerIcon", "containerLabel",
        "dropdownIcon", "dropdownLabel",
        "selectedIcon", "selectedLabel",
    ],
    "block-weather-background": ["block"],
}


# Ready-to-use style snippets — drop into a node's `styles` (block-level)
# or `dz_styles` (layout's drop-zone). All shaped for the "desktop"
# container. Compose multiple snippets with `build_styles`.
STYLE_SNIPPETS: dict[str, dict[str, Any]] = {
    "card_padded": {
        "spacing": {"padding": {"value": 16, "unit": "px"}},
        "border": {"borderRadius": {"value": 14, "unit": "px"}},
        "background": {"backgroundColor": {"value": "var(--ha-card-background, var(--card-background-color))"}},
    },
    "card_compact": {
        "spacing": {"padding": {"value": 10, "unit": "px"}},
        "border": {"borderRadius": {"value": 10, "unit": "px"}},
        "background": {"backgroundColor": {"value": "var(--ha-card-background, var(--card-background-color))"}},
    },
    "vertical_stack": {
        "flex": {
            "flexDirection": {"value": "column"},
            "gap": {"value": 12, "unit": "px"},
        },
    },
    "vertical_stack_tight": {
        "flex": {
            "flexDirection": {"value": "column"},
            "gap": {"value": 4, "unit": "px"},
        },
    },
    "horizontal_row": {
        "flex": {
            "flexDirection": {"value": "row"},
            "alignItems": {"value": "center"},
            "gap": {"value": 12, "unit": "px"},
        },
    },
    "centered_tile": {
        "flex": {
            "flexDirection": {"value": "column"},
            "alignItems": {"value": "center"},
            "justifyContent": {"value": "center"},
            "gap": {"value": 6, "unit": "px"},
        },
    },
    "header_row": {
        # icon on the left, name+state column on the right
        "flex": {
            "flexDirection": {"value": "row"},
            "alignItems": {"value": "center"},
            "gap": {"value": 12, "unit": "px"},
        },
    },
    "fill_remaining": {
        "flex": {"flexGrow": {"value": "1"}},
    },
    "text_primary": {
        "typography": {
            "fontWeight": {"value": "600"},
            "fontSize": {"value": 14, "unit": "px"},
        },
    },
    "text_secondary": {
        "typography": {
            "fontSize": {"value": 12, "unit": "px"},
            "color": {"value": "var(--secondary-text-color)"},
        },
    },
    "text_heading": {
        "typography": {
            "fontWeight": {"value": "600"},
            "fontSize": {"value": 18, "unit": "px"},
        },
    },
    "text_huge": {
        "typography": {
            "fontWeight": {"value": "700"},
            "fontSize": {"value": 28, "unit": "px"},
        },
    },
}


# =========================================================================
# Block schema (from upstream v1.x — sync if upstream adds new block types).
# =========================================================================

BLOCK_TYPES: dict[str, dict[str, Any]] = {
    # --- Basic ---
    "block-text": {
        "category": "basic",
        "label": "Text",
        "accepts_children": False,
        "requires_entity": False,
        "props": {
            "text": {"type": "string", "default": "", "binding": True},
        },
    },
    "block-icon": {
        "category": "basic",
        "label": "Icon",
        "accepts_children": False,
        "requires_entity": False,
        "props": {
            "iconSource": {"type": "enum", "values": ["list", "template"], "default": "list"},
            "icon": {"type": "string", "default": "mdi:star-outline"},
            "iconTemplate": {"type": "string", "default": ""},
            "preTemplate": {"type": "string", "default": ""},
            "postTemplate": {"type": "string", "default": ""},
        },
    },
    "block-image": {
        "category": "basic",
        "label": "Image",
        "accepts_children": False,
        "requires_entity": False,
        "props": {
            "imageSource": {"type": "enum", "values": ["none", "url", "media"], "default": "none"},
            "imageUrl": {"type": "string", "default": "", "binding": True},
            "media": {"type": "string", "default": "", "binding": True},
            "imageFit": {"type": "enum", "values": ["none", "contain", "cover", "stretch", "scale-down", "original"], "default": "none"},
            "imagePosition": {"type": "string", "default": "center"},
            "customPosition": {"type": "string", "default": ""},
        },
    },
    # --- Layout ---
    "block-container": {
        "category": "layout",
        "label": "Container",
        "accepts_children": True,
        "requires_entity": False,
        "props": {},
        "notes": "Auto-creates one block-drop-zone child. User-blocks go into that drop-zone, not directly into the container.",
    },
    "block-columns": {
        "category": "layout",
        "label": "Columns",
        "accepts_children": True,
        "requires_entity": False,
        "props": {
            "columns": {"type": "int", "default": 2, "min": 2, "max": 12},
            "gap": {"type": "int", "default": 0, "unit": "px"},
        },
        "notes": "Each column gets its own block-drop-zone.",
    },
    "block-grid": {
        "category": "layout",
        "label": "Grid",
        "accepts_children": True,
        "requires_entity": False,
        "props": {
            "rows": {"type": "int", "default": 2},
            "columns": {"type": "int", "default": 2},
            "rowSizes": {"type": "list", "default": []},
            "columnSizes": {"type": "list", "default": []},
            "areas": {"type": "list", "default": []},
            "gap": {"type": "object", "default": {"row": 0, "column": 0}},
        },
    },
    "block-drop-zone": {
        "category": "layout",
        "label": "Drop Zone (virtual)",
        "accepts_children": True,
        "requires_entity": False,
        "props": {},
        "internal": True,
        "notes": "Virtual block — auto-created by layout blocks. Never instantiate directly via the UI; build_from_recipe handles it.",
    },
    # --- Entity fields (display) ---
    "block-entity-field-state": {
        "category": "entities",
        "label": "Entity State",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "format": {"type": "enum", "values": ["text", "numeric", "integer", "datetime", "boolean", "template"], "default": "text"},
            "precision": {"type": "int", "default": 1, "min": 0, "max": 10},
            "dateFormat": {"type": "enum", "values": ["full", "long", "medium", "short", "time", "datetime", "relative", "iso"], "default": "medium"},
            "formatTemplate": {"type": "string", "default": ""},
            "customState": {"type": "string", "default": ""},
            "showUnit": {"type": "bool", "default": True},
            "customUnit": {"type": "string", "default": ""},
        },
    },
    "block-entity-field-name": {
        "category": "entities",
        "label": "Entity Name",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "customName": {"type": "string", "default": ""},
            "case": {"type": "enum", "values": ["none", "upper", "lower", "title", "kebab", "camel"], "default": "none"},
            "maxLength": {"type": "int", "default": 0, "min": 0},
            "useEllipsis": {"type": "bool", "default": True},
        },
    },
    "block-entity-field-icon": {
        "category": "entities",
        "label": "Entity Icon",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "iconSize": {"type": "int", "default": 24, "min": 12, "max": 128},
            "colorMode": {"type": "enum", "values": ["fixed", "state-based", "availability-based"], "default": "state-based"},
            "color": {"type": "string", "default": ""},
            "availableColor": {"type": "string", "default": ""},
            "unavailableColor": {"type": "string", "default": "gray"},
        },
    },
    "block-entity-field-attribute": {
        "category": "entities",
        "label": "Entity Attribute",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "attributeName": {"type": "string", "default": ""},
            "showLabel": {"type": "bool", "default": False},
            "customLabel": {"type": "string", "default": ""},
            "labelPosition": {"type": "enum", "values": ["top", "left", "inline"], "default": "left"},
            "format": {"type": "enum", "values": ["text", "numeric", "integer", "datetime", "boolean", "template"], "default": "text"},
            "precision": {"type": "int", "default": 1},
            "dateFormat": {"type": "string", "default": "medium"},
            "formatTemplate": {"type": "string", "default": ""},
            "prefix": {"type": "string", "default": ""},
            "suffix": {"type": "string", "default": ""},
        },
    },
    "block-entity-field-image": {
        "category": "entities",
        "label": "Entity Image",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "customImageUrl": {"type": "string", "default": "", "binding": True},
            "fallbackIcon": {"type": "string", "default": "mdi:image-off-outline", "binding": True},
        },
    },
    # --- Controls ---
    "block-slider": {
        "category": "controls",
        "label": "Slider",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "orientation": {"type": "enum", "values": ["horizontal", "vertical"], "default": "horizontal"},
            "shape": {"type": "enum", "values": ["rounded", "square"], "default": "rounded"},
            "showThumb": {"type": "bool", "default": True},
            "showValue": {"type": "bool", "default": True},
            "valuePositionHorizontal": {"type": "enum", "values": ["inline", "tooltip", "inside"], "default": "inline"},
            "inlinePositionHorizontal": {"type": "enum", "values": ["left", "right"], "default": "right"},
            "insidePositionHorizontal": {"type": "enum", "values": ["left", "center", "right"], "default": "center"},
            "valuePositionVertical": {"type": "enum", "values": ["top", "bottom", "inside", "tooltip"], "default": "top"},
            "insidePositionVertical": {"type": "enum", "values": ["top", "middle", "bottom"], "default": "middle"},
            "invert": {"type": "bool", "default": False},
            "activationMode": {"type": "enum", "values": ["press", "hold"], "default": "press"},
            "holdTapEnabled": {"type": "bool", "default": False},
            "holdTapAction": {"type": "enum", "values": ["more-info", "toggle"], "default": "more-info"},
            "mode": {"type": "enum", "values": ["auto", "single", "range"], "default": "auto"},
            "coverControl": {"type": "enum", "values": ["auto", "position", "tilt"], "default": "auto", "applies_when_domain": "cover"},
            "valueSource": {"type": "enum", "values": ["state", "attribute"], "default": "state"},
            "valueAttribute": {"type": "string", "default": ""},
            "displayMode": {"type": "enum", "values": ["auto", "raw", "percent", "custom"], "default": "auto"},
            "displayMin": {"type": "int", "default": 0},
            "displayMax": {"type": "int", "default": 100},
            "commitMode": {"type": "enum", "values": ["onRelease", "throttled", "debounced"], "default": "onRelease"},
            "commitThrottleMs": {"type": "int", "default": 200, "unit": "ms"},
            "commitDebounceMs": {"type": "int", "default": 300, "unit": "ms"},
            "disableMode": {"type": "enum", "values": ["auto", "custom", "never"], "default": "auto"},
            "disabled": {"type": "bool", "default": False},
            "rangeMinGap": {"type": "int", "default": 0},
            "useMinOverride": {"type": "bool", "default": False},
            "minOverride": {"type": "int", "default": 0},
            "useMaxOverride": {"type": "bool", "default": False},
            "maxOverride": {"type": "int", "default": 100},
            "useStepOverride": {"type": "bool", "default": False},
            "stepOverride": {"type": "int", "default": 1},
            "usePrecisionOverride": {"type": "bool", "default": False},
        },
        "supported_domains": ["light", "fan", "cover", "media_player", "climate", "humidifier", "water_heater", "input_number", "number", "valve"],
        "notes": "Value-position props are split by orientation: valuePositionHorizontal vs valuePositionVertical. commitMode value 'onRelease' (NOT 'release').",
    },
    "block-button-toggle": {
        "category": "controls",
        "label": "Button Toggle",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "feature": {"type": "string", "default": "auto", "notes": "Domain-namespaced ID (e.g. climate_hvac_mode, cover_state, light_power). Use 'auto' to let Card Builder pick the first available. See list_button_toggle_features(domain)."},
            "orientation": {"type": "enum", "values": ["horizontal", "vertical"], "default": "horizontal"},
            "showIcon": {"type": "bool", "default": True},
            "showLabel": {"type": "bool", "default": True},
            "contentLayout": {"type": "enum", "values": ["horizontal", "vertical"], "default": "horizontal", "notes": "Was iconLabelLayout in the panel docs."},
            "contentOrder": {"type": "enum", "values": ["icon-first", "label-first"], "default": "icon-first"},
            "verticalAlign": {"type": "enum", "values": ["left", "center", "right"], "default": "center"},
        },
        "supported_domains": ["climate", "fan", "light", "media_player", "humidifier", "water_heater", "cover", "lock", "vacuum", "switch", "input_boolean", "siren", "automation", "select", "input_select"],
    },
    "block-select-menu": {
        "category": "controls",
        "label": "Select Menu",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "feature": {"type": "string", "default": "auto", "notes": "Domain-namespaced ID — same convention as block-button-toggle. See list_button_toggle_features(domain)."},
            "containerShowIcon": {"type": "bool", "default": True},
            "containerShowLabel": {"type": "bool", "default": True},
            "dropdownShowIcon": {"type": "bool", "default": True},
            "dropdownShowLabel": {"type": "bool", "default": True},
            "contentOrder": {"type": "enum", "values": ["icon-first", "label-first"], "default": "icon-first"},
            "dropdownPlacement": {"type": "enum", "values": ["down", "up"], "default": "down"},
        },
        "supported_domains": ["climate", "fan", "light", "media_player", "humidifier", "water_heater", "cover", "lock", "vacuum", "switch", "input_boolean", "siren", "automation", "select", "input_select"],
    },
    # --- Advanced ---
    "block-weather-background": {
        "category": "advanced",
        "label": "Weather Background",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "svgSource": {"type": "enum", "values": ["default", "media"], "default": "default"},
            "defaultBackground": {"type": "string", "default": "background-1"},
            "customSvg": {"type": "string", "default": ""},
            "showSvgWarnings": {"type": "bool", "default": True},
            "enableAnimations": {"type": "bool", "default": True},
            "updateInterval": {"type": "int", "default": 10, "min": 5, "max": 60, "unit": "minutes"},
        },
        "entity_domain": "weather",
    },
}


# =========================================================================
# Cards CRUD
# =========================================================================

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

    `config` is the full block tree (DocumentData v3). Build it with
    `build_from_recipe` instead of hand-crafting JSON. Validate with
    `validate_config` before saving. See `recipe_guide` for the full how-to.
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
    """Delete a card by id. HA's WS handler returns an empty payload on success."""
    try:
        result = ha._ws_call("card_builder/cards/delete", card_id=card_id)
    except Exception as err:
        return {"status": "error", "card_id": card_id, "error": str(err)}
    return {"status": "deleted", "card_id": card_id, "result": result}


# =========================================================================
# Style presets
# =========================================================================

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
    try:
        result = ha._ws_call("card_builder/style_presets/delete", style_preset_id=style_preset_id)
    except Exception as err:
        return {"status": "error", "style_preset_id": style_preset_id, "error": str(err)}
    return {"status": "deleted", "style_preset_id": style_preset_id, "result": result}


# =========================================================================
# CSS custom properties
# =========================================================================

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
    try:
        result = ha._ws_call(
            "card_builder/css_custom_properties/delete",
            custom_property_id=custom_property_id,
        )
    except Exception as err:
        return {"status": "error", "custom_property_id": custom_property_id, "error": str(err)}
    return {"status": "deleted", "custom_property_id": custom_property_id, "result": result}


# =========================================================================
# Media manager (config/www/card_builder/)
# =========================================================================

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
    try:
        result = ha._ws_call("card_builder/media/delete", path=path)
    except Exception as err:
        return {"status": "error", "path": path, "error": str(err)}
    return {"status": "deleted", "path": path, "result": result}


# =========================================================================
# Dashboard helper
# =========================================================================

@mcp.tool()
def renderer_card_config(
    card_id: str,
    slot_entities: dict | None = None,
    slot_actions: dict | None = None,
) -> dict:
    """Build a Lovelace card config that renders a Card Builder card on a dashboard.

    Returns:
        {"type": "custom:card-builder-renderer-card", "card_id": ..., "slot_entities": {...}, "slot_actions": {...}}

    Use as a card in `dashboards_add_card_to_view`. `slot_entities` maps each
    slot id defined in the card to a HA entity_id; `slot_actions` overrides
    per-instance action configs.
    """
    config: dict[str, Any] = {
        "type": "custom:card-builder-renderer-card",
        "card_id": card_id,
    }
    if slot_entities:
        config["slot_entities"] = slot_entities
    if slot_actions:
        config["slot_actions"] = slot_actions
    return config


# =========================================================================
# Introspection
# =========================================================================

@mcp.tool()
def list_block_types(category: str | None = None, include_internal: bool = False) -> list[dict]:
    """List every block type understood by Card Builder.

    Pass `category` to filter (basic, layout, entities, controls, advanced).
    Returns a compact list — call `get_block_schema(type)` for full prop details.
    """
    out: list[dict] = []
    for type_name, info in BLOCK_TYPES.items():
        if not include_internal and info.get("internal"):
            continue
        if category and info.get("category") != category:
            continue
        out.append(
            {
                "type": type_name,
                "category": info.get("category"),
                "label": info.get("label"),
                "accepts_children": info.get("accepts_children", False),
                "requires_entity": info.get("requires_entity", False),
                "prop_count": len(info.get("props", {})),
            }
        )
    return out


@mcp.tool()
def get_block_schema(block_type: str) -> dict:
    """Full schema for one block type: props with types/defaults/enum values, notes, supported domains, style targets."""
    info = BLOCK_TYPES.get(block_type)
    if not info:
        return {"error": "unknown_block_type", "block_type": block_type, "known": list(BLOCK_TYPES.keys())}
    return {
        "type": block_type,
        **info,
        "style_targets": STYLE_TARGETS.get(block_type, ["block"]),
    }


# =========================================================================
# Style introspection
# =========================================================================

@mcp.tool()
def list_button_toggle_features(domain: str | None = None) -> list[dict]:
    """List the `feature` IDs accepted by block-button-toggle / block-select-menu.

    Feature IDs are **namespaced by domain** — e.g. `climate_hvac_mode` for an
    AC, `cover_state` for a blind, `light_power` for a bulb. Pass `domain` to
    filter; omit to see everything.

    `feature: "auto"` (the block default) lets Card Builder pick the first
    available feature for the entity at runtime — usually what you want for
    reusable templates.
    """
    if domain:
        defs = FEATURE_DEFINITIONS.get(domain) or []
        return [{"domain": domain, **f} for f in defs]
    out: list[dict] = []
    for d, feats in FEATURE_DEFINITIONS.items():
        for f in feats:
            out.append({"domain": d, **f})
    return out


@mcp.tool()
def list_style_categories() -> list[dict]:
    """List CSS style categories with their property names and unit conventions.

    Property names are camelCase (e.g. ``flexDirection``, ``backgroundColor``).
    Anything else gets discarded by Card Builder's CSS resolver.
    """
    return [
        {
            "category": cat,
            "properties": info["properties"],
            "length_units": info.get("length_units"),
            "legacy_aliases": info.get("legacy_aliases"),
            "notes": info.get("notes"),
        }
        for cat, info in STYLE_CATEGORIES.items()
    ]


@mcp.tool()
def list_style_targets(block_type: str) -> dict:
    """Style targets available for a block — sub-components you can style independently.

    Example: ``block-entity-field-state`` has ``state`` and ``unit`` targets so you
    can colour the number and the unit differently.
    """
    if block_type not in BLOCK_TYPES:
        return {"error": "unknown_block_type", "block_type": block_type}
    targets = STYLE_TARGETS.get(block_type, ["block"])
    return {"block_type": block_type, "targets": targets}


@mcp.tool()
def list_style_snippets() -> list[dict]:
    """List built-in style snippets — drop them straight into a node's ``styles`` or ``dz_styles``.

    Compose multiple snippets with ``build_styles(["card_padded", "vertical_stack"])``.
    """
    return [
        {"name": name, "categories": list(snippet.keys())}
        for name, snippet in STYLE_SNIPPETS.items()
    ]


@mcp.tool()
def get_style_snippet(name: str, target: str = "block", container: str = "desktop") -> dict:
    """Fetch one snippet wrapped in the full target/container envelope.

    Returns ``{<target>: {containers: {<container>: <snippet>}}}`` — ready to assign
    to a block's ``styles`` directly.
    """
    snippet = STYLE_SNIPPETS.get(name)
    if not snippet:
        return {"error": "unknown_snippet", "name": name, "known": list(STYLE_SNIPPETS.keys())}
    return {target: {"containers": {container: snippet}}}


@mcp.tool()
def build_styles(
    snippet_names: list[str],
    target: str = "block",
    container: str = "desktop",
    extra: dict | None = None,
) -> dict:
    """Compose a styles object from one or more snippets plus optional extra overrides.

    ``extra`` is shaped like the inner ContainerStyleData (``{category: {prop: {value, unit}}}``).
    Later snippets override earlier ones at the property level.

    Example::

        build_styles(["card_padded", "vertical_stack"])
        -> {"block": {"containers": {"desktop": {"spacing": {...}, "border": {...}, "background": {...}, "flex": {...}}}}}
    """
    merged: dict[str, Any] = {}
    for name in snippet_names:
        snippet = STYLE_SNIPPETS.get(name)
        if not snippet:
            return {"error": "unknown_snippet", "name": name, "known": list(STYLE_SNIPPETS.keys())}
        for cat, props in snippet.items():
            merged.setdefault(cat, {}).update(props)
    if extra:
        for cat, props in extra.items():
            merged.setdefault(cat, {}).update(props)
    return {target: {"containers": {container: merged}}}


# =========================================================================
# Schema sync
# =========================================================================

@mcp.tool()
def check_schema_sync() -> dict:
    """Compare the embedded schema with upstream Card Builder's HEAD manifest.

    Hits ``raw.githubusercontent.com`` for ``custom_components/card_builder/manifest.json``
    and reports whether the embedded schema still matches the upstream version.
    Use this when a recipe stops working — the upstream may have shipped a
    breaking schema change.
    """
    import json
    import urllib.request

    url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main/custom_components/card_builder/manifest.json"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            upstream = json.load(r)
    except Exception as err:
        return {"status": "fetch_failed", "error": str(err), "embedded": UPSTREAM_SCHEMA_SYNC}

    upstream_version = upstream.get("version", "?")
    return {
        "status": "ok",
        "upstream_version": upstream_version,
        "upstream_repo": UPSTREAM_REPO,
        "embedded_doc_version": UPSTREAM_SCHEMA_SYNC["document_model_version"],
        "embedded_documented_against": UPSTREAM_SCHEMA_SYNC["documented_against"],
        "advice": (
            "If upstream_version drifted significantly from the embedded "
            "'documented_against' tag, refresh BLOCK_TYPES / STYLE_CATEGORIES / "
            "STYLE_TARGETS / STYLE_SNIPPETS in tools/card_builder.py."
        ),
    }


# =========================================================================
# Recipe guide — embedded how-to so AI clients don't have to scrape upstream
# =========================================================================

_RECIPE_GUIDE_MD = """# Card Builder Recipe Guide

A Card Builder card is a `DocumentData` blob (`version: 3`) with a tree of blocks.

## Structure

```
{
  "version": 3,
  "rootId": "<id>",
  "slots": {
    "entities": { "<slot_id>": {"id": "<slot_id>", "name": "...", "domains": [...]} },
    "actions":  { "<slot_id>": {"id": "<slot_id>", "trigger": "tap", "action": {...}} }
  },
  "blocks": {
    "<id>": {
      "id": "<id>",
      "type": "block-...",
      "parentId": "<parent_id>" | null,
      "children": ["<child_id>", ...],
      "layout": "flow" | "absolute" | "static",
      "order": <int>,
      "zIndex": <int>,
      "parentManaged": <bool>,
      "props": { ... },
      "entityConfig": { "mode": "inherited" | "slot" | "fixed", "slotId"?: "...", "entityId"?: "..." }
    }
  }
}
```

## Block prop values MUST be wrapped

Every prop value is a `TraitPropertyValue`: `{"value": <x>}` (or `{"binding": ...}`
for dynamic bindings). Raw scalars are silently ignored and the block falls
back to defaults. So:

```
"props": {"iconSize": {"value": 40}, "colorMode": {"value": "state-based"}}
```

NOT:

```
"props": {"iconSize": 40, "colorMode": "state-based"}   # ← ignored, defaults kick in
```

`build_from_recipe` auto-wraps raw scalars in `{"value": ...}` so you can
write the shorter form in recipes — direct `create_card` / `update_card`
callers must wrap by hand.

## Block types

Discover them with `list_block_types()` / `get_block_schema(type)`.

Five categories:
- **basic**: `block-text`, `block-icon`, `block-image`
- **layout**: `block-container`, `block-columns`, `block-grid` (plus internal `block-drop-zone`)
- **entities**: `block-entity-field-{state,name,icon,attribute,image}` — all require an entity
- **controls**: `block-slider`, `block-button-toggle`, `block-select-menu` — auto-detect features per domain
- **advanced**: `block-weather-background`, `block-hourly-forecast`, `block-link`

## Entity inheritance

A child block with `entityConfig.mode = "inherited"` (the default) walks up the
tree until it finds an ancestor that defines an entity (fixed or slot). Put
`entityConfig: {mode: "slot", slotId: "<id>"}` on the root container of a
reusable template — every child block then reads from that slot.

## Layout blocks need drop-zones

`block-container`, `block-columns`, and `block-grid` always have a
`block-drop-zone` child (virtual). User-blocks live inside the drop-zone.
`build_from_recipe` adds them automatically — don't add them by hand.

## Reusable templates (slots)

Define slots in `slots.entities`:

```
slots: {
  entities: {
    "climate": {"id": "climate", "name": "Climate", "domains": ["climate"]}
  }
}
```

The root container's `entityConfig.slotId` references the slot. On a dashboard
instance, configure `slot_entities: {climate: "climate.salon"}` on the
renderer card. One template → many instances.

## Dashboard embedding

Use `renderer_card_config(card_id, slot_entities={...})` to build the Lovelace
card config:

```
{"type": "custom:card-builder-renderer-card", "card_id": "...", "slot_entities": {...}}
```

## Recipe shorthand (for build_from_recipe)

Instead of writing the full DocumentData, pass a `recipe`:

```
{
  "slots": {"climate": {"name": "Climate", "domains": ["climate"]}},
  "root_slot": "climate",
  "root_styles":    {"block": {"containers": {"desktop": {"spacing": {"padding": {"value": 16, "unit": "px"}}, "border": {"border-radius": {"value": 14, "unit": "px"}}}}}},
  "root_dz_styles": {"block": {"containers": {"desktop": {"flex":    {"flex-direction": {"value": "column"}, "gap": {"value": 12, "unit": "px"}}}}}},
  "blocks": [
    {"type": "block-entity-field-icon",   "props": {"iconSize": 36}},
    {"type": "block-entity-field-name"},
    {"type": "block-entity-field-state"},
    {"type": "block-button-toggle", "props": {"feature": "hvac_mode"}},
    {"type": "block-slider"}
  ]
}
```

Raw scalars in `props` get auto-wrapped (`40` → `{"value": 40}`).

Layout blocks can nest — children of a layout block belong to its auto
drop-zone. Stylowanie flex/spacing/typography idzie na drop-zone, NIE na
container; pass it via `dz_styles` on the layout node:

```
{"type": "block-container",
 "dz_styles": {"block": {"containers": {"desktop": {"flex": {"flex-direction": {"value": "row"}, "gap": {"value": 12, "unit": "px"}}}}}},
 "children": [
   {"type": "block-entity-field-icon"},
   {"type": "block-entity-field-name"}
 ]}
```

## Styles shape

Block `styles` is keyed by **target** (default `"block"`, plus per-block
sub-component targets — see `list_style_targets(type)`) then **container**
(use `"desktop"` for the responsive default; Card Builder also supports
`"tablet"` and `"mobile"`). Categories: `layout`, `size`, `spacing`,
`typography`, `background`, `border`, `flex`, `effects`, `svg` — full
property lists via `list_style_categories()`.

**Property names are camelCase**: `flexDirection`, `alignItems`, `borderRadius`,
`backgroundColor`, `fontSize`, `paddingTop`. kebab-case (`flex-direction`,
`background-color`) is silently discarded.

Each property value is a `StylePropertyValue`: `{"value": ..., "unit"?: "px|%|rem|..."}`.

```
"styles": {
  "block": {
    "containers": {
      "desktop": {
        "spacing":    {"padding": {"value": 16, "unit": "px"}},
        "border":     {"borderRadius": {"value": 14, "unit": "px"}},
        "background": {"backgroundColor": {"value": "var(--ha-card-background)"}},
        "flex":       {"flexDirection": {"value": "column"}, "gap": {"value": 12, "unit": "px"}},
        "typography": {"fontWeight": {"value": "600"}, "fontSize": {"value": 14, "unit": "px"}}
      }
    }
  }
}
```

## Button-toggle / select-menu features

`block-button-toggle` and `block-select-menu` pick what to control via the
`feature` prop. **Feature IDs are namespaced by domain** — e.g.
`climate_hvac_mode`, `cover_state`, `light_power`. Default `auto` lets
Card Builder pick the first available feature for the entity at runtime
(safest choice for reusable templates).

Discover IDs via `list_button_toggle_features(domain)`. Example for an AC:

```
{"type": "block-button-toggle", "props": {"feature": "auto"}}
# or explicitly:
{"type": "block-button-toggle", "props": {"feature": "climate_hvac_mode"}}
```

## Slider prop names — Horizontal / Vertical suffix

`block-slider` value placement props are split per orientation:
`valuePositionHorizontal` / `valuePositionVertical`,
`inlinePositionHorizontal`, `insidePositionHorizontal` / Vertical. Other
props use camelCase too: `activationMode`, `holdTapEnabled`,
`holdTapAction`, `commitMode` (values: `onRelease` | `throttled` |
`debounced`), `commitThrottleMs`, `commitDebounceMs`. The legacy unsuffixed
names (`valuePosition`, `inlinePosition`, `activation`, …) are silently
ignored — verify with `get_block_schema("block-slider")`.

## Style snippets (shortcut)

Don't hand-write the envelope every time — use `build_styles([names])`:

```
build_styles(["card_padded", "vertical_stack"])
# → ready-to-use styles for a vertical-flex card with padding + radius + bg
```

`list_style_snippets()` returns every available preset. Common picks:
- `card_padded` / `card_compact` — padding + border-radius + bg
- `vertical_stack` / `vertical_stack_tight` — flex column with gap
- `horizontal_row` / `header_row` — flex row with center alignment
- `centered_tile` — tile centered column
- `fill_remaining` — flex-grow:1 (for filler children)
- `text_primary` / `text_secondary` / `text_heading` / `text_huge` — typography preset

## Validation

`validate_config(config)` checks the structure (block types, parent/child
links, required entities) before you send it to `create_card`.
"""


@mcp.tool()
def recipe_guide() -> str:
    """Embedded how-to for designing Card Builder cards programmatically.

    Read this once at the start of a card-building task. Covers DocumentData
    layout, block tree, entity inheritance, slots, drop-zones, dashboard
    embedding, and the recipe shorthand consumed by `build_from_recipe`.
    """
    return _RECIPE_GUIDE_MD


# =========================================================================
# Recipe builder — turns a shorthand recipe into a full DocumentData
# =========================================================================

def _new_id() -> str:
    return uuid.uuid4().hex


def _wrap_props(props: dict | None) -> dict:
    """Auto-wrap raw scalar prop values in `{"value": ...}` (TraitPropertyValue shape).

    Card Builder's `getPropertyValue` discards anything that isn't an object
    with a `value` or `binding` key — raw `"iconSize": 40` silently falls
    back to the block's default. This helper makes the recipe shorthand
    `"props": {"iconSize": 40}` work the same as the verbose
    `"props": {"iconSize": {"value": 40}}`.
    """
    if not props:
        return {}
    wrapped: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, dict) and ("value" in v or "binding" in v):
            wrapped[k] = v
        else:
            wrapped[k] = {"value": v}
    return wrapped


def _make_block(
    block_type: str,
    parent_id: str | None,
    *,
    order: int = 0,
    parent_managed: bool = False,
    props: dict | None = None,
    entity_config: dict | None = None,
    children: list[str] | None = None,
    styles: dict | None = None,
) -> dict:
    block: dict[str, Any] = {
        "id": _new_id(),
        "type": block_type,
        "parentId": parent_id,
        "children": list(children or []),
        "layout": "flow",
        "order": order,
        "zIndex": 0,
        "parentManaged": parent_managed,
        "props": _wrap_props(props),
    }
    if entity_config is not None:
        block["entityConfig"] = entity_config
    if styles is not None:
        block["styles"] = styles
    return block


def _build_block_tree(
    nodes: list[dict],
    parent_id: str,
    blocks: dict[str, dict],
) -> list[str]:
    """Recursively materialise recipe nodes under `parent_id`. Returns child ids."""
    ids: list[str] = []
    for order, node in enumerate(nodes):
        block_type = node.get("type")
        if not block_type or block_type not in BLOCK_TYPES:
            raise ValueError(f"Unknown block type in recipe: {block_type!r}")
        info = BLOCK_TYPES[block_type]
        if info.get("internal"):
            raise ValueError(f"{block_type} is internal — don't put it in a recipe; the builder adds drop-zones automatically.")
        block = _make_block(
            block_type,
            parent_id=parent_id,
            order=order,
            props=node.get("props"),
            entity_config=node.get("entityConfig"),
            styles=node.get("styles"),
        )
        blocks[block["id"]] = block

        # Wrap layout blocks in their auto drop-zone (styles on the layout
        # block typically belong on the drop-zone, since that's where flex /
        # spacing / typography are applied to laid-out children).
        if info.get("accepts_children"):
            dz = _make_block(
                "block-drop-zone",
                parent_id=block["id"],
                order=0,
                parent_managed=True,
                styles=node.get("drop_zone_styles") or node.get("dz_styles"),
            )
            blocks[dz["id"]] = dz
            block["children"] = [dz["id"]]
            inner_children = node.get("children") or []
            dz["children"] = _build_block_tree(inner_children, dz["id"], blocks)
        else:
            if node.get("children"):
                raise ValueError(f"Block type {block_type!r} does not accept children.")

        ids.append(block["id"])
    return ids


@mcp.tool()
def build_from_recipe(recipe: dict) -> dict:
    """Materialise a recipe shorthand into a full DocumentData (config) dict.

    Recipe shape::

        {
          "slots": {"<slot_id>": {"name": "...", "description": "...", "domains": [...]}},
          "root_slot": "<slot_id>",          # binds root container to that slot
          "root_entity": "entity.full_id",    # OR set a fixed entity on root
          "blocks": [                         # children of the root container
            {"type": "block-...", "props": {...}, "children": [...]},
            ...
          ]
        }

    Returns a dict ready to pass as `config` to `create_card`. Run
    `validate_config` on it first if you want a sanity check.
    """
    slots_def = recipe.get("slots") or {}
    root_slot = recipe.get("root_slot")
    root_entity = recipe.get("root_entity")
    children = recipe.get("blocks") or []

    if root_slot and root_slot not in slots_def:
        raise ValueError(f"root_slot {root_slot!r} not defined in recipe.slots")

    blocks: dict[str, dict] = {}

    # Root container with optional entity config
    root_entity_config = None
    if root_slot:
        root_entity_config = {"mode": "slot", "slotId": root_slot}
    elif root_entity:
        root_entity_config = {"mode": "fixed", "entityId": root_entity}

    root = _make_block(
        "block-container",
        parent_id=None,
        order=0,
        entity_config=root_entity_config,
        styles=recipe.get("root_styles"),
    )
    blocks[root["id"]] = root

    root_dz = _make_block(
        "block-drop-zone",
        parent_id=root["id"],
        order=0,
        parent_managed=True,
        styles=recipe.get("root_dz_styles") or recipe.get("layout_styles"),
    )
    blocks[root_dz["id"]] = root_dz
    root["children"] = [root_dz["id"]]
    root_dz["children"] = _build_block_tree(children, root_dz["id"], blocks)

    # Normalize slot definitions into full EntitySlot shape
    slots_full: dict[str, dict] = {}
    for slot_id, spec in slots_def.items():
        entry = {"id": slot_id}
        for k in ("name", "description", "domains", "entityId"):
            if k in spec:
                entry[k] = spec[k]
        slots_full[slot_id] = entry

    return {
        "version": 3,
        "rootId": root["id"],
        "slots": {"entities": slots_full, "actions": recipe.get("action_slots") or {}},
        "blocks": blocks,
    }


# =========================================================================
# Validation
# =========================================================================

@mcp.tool()
def validate_config(config: dict) -> dict:
    """Lightweight structural validation for a Card Builder config (DocumentData).

    Checks: version, rootId presence, every block has known type, parent/child
    links are consistent, entity-required blocks have an entity available
    (inherited, slot, or fixed). Returns `{ok: bool, errors: [...], warnings: [...]}`.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(config, dict):
        return {"ok": False, "errors": ["config must be a dict"], "warnings": []}

    if config.get("version") != 3:
        warnings.append(f"version is {config.get('version')!r}, expected 3 — Card Builder migrates older versions but newer features may not load.")

    root_id = config.get("rootId")
    blocks = config.get("blocks") or {}
    if not isinstance(blocks, dict) or not blocks:
        errors.append("blocks must be a non-empty dict")
    if root_id and root_id not in blocks:
        errors.append(f"rootId {root_id!r} does not refer to a block in `blocks`")

    slots = (config.get("slots") or {}).get("entities") or {}

    # Per-block checks
    for bid, block in blocks.items():
        if not isinstance(block, dict):
            errors.append(f"block {bid!r} is not a dict")
            continue
        btype = block.get("type")
        if btype not in BLOCK_TYPES:
            errors.append(f"block {bid!r}: unknown type {btype!r}")
            continue
        info = BLOCK_TYPES[btype]
        # Props must use TraitPropertyValue wrapper — raw scalars get silently ignored.
        for pname, pval in (block.get("props") or {}).items():
            if not isinstance(pval, dict) or not ("value" in pval or "binding" in pval):
                warnings.append(
                    f"block {bid!r}: prop {pname!r} = {pval!r} is not wrapped as "
                    f"{{value: ...}} — Card Builder will discard it and fall back "
                    f"to the block default. Use {{\"value\": {pval!r}}}."
                )
        # Parent/child sanity
        parent = block.get("parentId")
        if parent is not None and parent not in blocks:
            errors.append(f"block {bid!r}: parentId {parent!r} not in blocks")
        for c in block.get("children", []):
            if c not in blocks:
                errors.append(f"block {bid!r}: child {c!r} not in blocks")
            else:
                child_parent = blocks[c].get("parentId")
                if child_parent != bid:
                    errors.append(f"block {c!r}: parentId {child_parent!r} ≠ {bid!r}")
        # Required entity
        if info.get("requires_entity"):
            # Walk up to find an entity provider
            cur = block
            seen = set()
            provider = None
            while cur and cur["id"] not in seen:
                seen.add(cur["id"])
                ec = cur.get("entityConfig") or {}
                mode = ec.get("mode")
                if mode == "fixed" and ec.get("entityId"):
                    provider = ("fixed", ec["entityId"])
                    break
                if mode == "slot" and ec.get("slotId"):
                    if ec["slotId"] in slots:
                        provider = ("slot", ec["slotId"])
                        break
                    errors.append(f"block {bid!r}: ancestor references slot {ec['slotId']!r} which is not defined in slots.entities")
                    break
                # default inherited — walk up
                pid = cur.get("parentId")
                cur = blocks.get(pid) if pid else None
            if provider is None:
                errors.append(f"block {bid!r} ({btype}) requires an entity but no ancestor provides one (slot / fixed)")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "block_count": len(blocks), "slot_count": len(slots)}
