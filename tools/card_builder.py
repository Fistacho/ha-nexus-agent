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
import os
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("card_builder")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


# =========================================================================
# Media upload safety — shared between upload_media_from_path (local file)
# and upload_image_from_url (remote fetch).
# =========================================================================

# Image/vector formats Card Builder's blocks actually consume
# (block-image / block-weather-background `mediaReference`). Deliberately
# excludes anything executable or text-based (no .py, .yaml, .json, ...).
_ALLOWED_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}

# Content-Type prefixes accepted from a remote download.
_ALLOWED_MEDIA_CONTENT_TYPE_PREFIX = "image/"

# Cap on a remote image download — background/SVG assets, not video.
_MAX_URL_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MiB

# Cap on the decoded/raw bytes of a direct upload (upload_media, upload_svg).
# Same ceiling as a remote download — these are card backgrounds/SVGs, not
# video or archives — kept as one named constant so the two call sites can't
# silently drift apart.
_MAX_MEDIA_UPLOAD_BYTES = _MAX_URL_IMAGE_BYTES

# Hard cap on the number of elements an uploaded SVG may contain, checked
# right after XML parsing (in `sanitize_svg`, before the sanitizer recurses
# into the tree). The DOCTYPE/ENTITY substring check above closes classic
# XXE / "billion laughs" entity expansion, but a flat or deeply nested
# element bomb (thousands of plain `<rect>`s, or `<g>` nested thousands of
# levels deep) needs no entity declaration at all and would still cost the
# sanitizer — and Home Assistant's own SVG renderer — O(n) time/memory
# proportional to attacker input. A simple post-parse element count is the
# cheapest guard that doesn't require re-implementing a streaming/depth-aware
# parser: `sum(1 for _ in root.iter())` is O(n) over an already-parsed tree,
# and 5000 elements is far beyond any legitimate card background, weather
# SVG or icon this codebase generates.
_SVG_MAX_ELEMENTS = 5000


def _media_upload_roots() -> list[Path]:
    """Directories `upload_media_from_path` may read from.

    Limited to `<HA_CONFIG_PATH>/www` and `<HA_CONFIG_PATH>/media` — the only
    directories this add-on is granted (`config:rw` in config.yaml) that are
    also meant to hold user media. Explicitly excludes the rest of /config
    (secrets.yaml, .storage/, packages, …) and anything under /data.
    """
    config_root = Path(os.getenv("HA_CONFIG_PATH", "/config")).resolve()
    return [config_root / "www", config_root / "media"]


def _resolve_media_source(local_path: str) -> tuple[Path | None, dict | None]:
    """Resolve and validate `local_path` for upload_media_from_path.

    Returns `(path, None)` on success or `(None, error_dict)` — never raises.
    """
    try:
        src = Path(local_path).resolve()
    except (OSError, RuntimeError) as err:
        return None, {"error": "invalid_path", "local_path": local_path, "detail": str(err)}

    roots = _media_upload_roots()
    if not any(src.is_relative_to(root) for root in roots):
        return None, {
            "error": "path_not_allowed",
            "local_path": local_path,
            "detail": f"local_path must resolve under one of: {[str(r) for r in roots]}",
        }

    if src.suffix.lower() not in _ALLOWED_MEDIA_EXTENSIONS:
        return None, {
            "error": "extension_not_allowed",
            "local_path": local_path,
            "detail": f"Extension '{src.suffix}' not allowed; use one of {sorted(_ALLOWED_MEDIA_EXTENSIONS)}",
        }

    return src, None


def _has_path_traversal(value: str) -> bool:
    """True if `value` (the `path=` sub-folder arg to the media WS API)
    contains a `..` segment, an absolute-path anchor (`/foo`), or a Windows
    drive letter (`C:...`) — any of which could walk the upload outside
    `www/card_builder/` on the HA side. `card_builder/media/upload` is an
    integration-owned WS handler; this process has no visibility into
    whether it re-validates `path` itself, so treat this check as the only
    guard.
    """
    if not value:
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True
    return any(part == ".." for part in normalized.split("/"))


def _validate_upload_filename_and_path(filename: str, path: str) -> dict | None:
    """Shared guard for `upload_media`/`upload_svg`: reject anything that
    isn't a bare, allowlisted media filename, or a `path` that could escape
    `www/card_builder/`. Returns an error dict, or `None` if `filename`/
    `path` are both safe to hand to `card_builder/media/upload`.
    """
    if not filename or not filename.strip():
        return {"error": "invalid_filename", "filename": filename}

    if "/" in filename or "\\" in filename or "\x00" in filename:
        return {
            "error": "path_not_allowed",
            "filename": filename,
            "detail": "filename must be a bare name with no path separators — use `path=` for sub-folders",
        }

    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_MEDIA_EXTENSIONS:
        return {
            "error": "extension_not_allowed",
            "filename": filename,
            "detail": f"Extension '{suffix}' not allowed; use one of {sorted(_ALLOWED_MEDIA_EXTENSIONS)}",
        }

    if _has_path_traversal(path):
        return {
            "error": "path_not_allowed",
            "path": path,
            "detail": "path must not contain '..' segments, an absolute anchor, or a drive letter",
        }

    return None


# =========================================================================
# SVG sanitization — mandatory before ANY SVG reaches www/card_builder/.
#
# `<config>/www` is served by Home Assistant at `/local/...` with NO
# authentication, in the same origin as the HA frontend. An SVG opened
# directly (not as an <img>/background — as its own document, e.g. by
# visiting the /local/ URL, or certain embed contexts) executes embedded
# `<script>`, `on*=` handlers and `javascript:` URIs like any other HTML
# document — a stored-XSS path into an authenticated HA admin session.
# Every write of SVG bytes under www/card_builder/ MUST go through
# `sanitize_svg()`.
#
# Design: allowlist, not blocklist. We parse with the stdlib
# `xml.etree.ElementTree` (expat-backed) and rebuild the tree keeping only a
# fixed set of SVG presentation elements — anything else (`script`,
# `foreignObject`, `iframe`, `embed`, `object`, SMIL `animate*`, ...) is
# dropped together with its whole subtree, so nothing can smuggle markup
# inside an element we don't recognise.
#
# `xml.etree.ElementTree` is documented as NOT hardened against maliciously
# crafted XML (XXE / "billion laughs" entity expansion). `defusedxml` is not
# currently a dependency of this project (checked requirements.txt /
# pyproject.toml) — adding it purely for this would be reasonable but is a
# call for whoever owns dependency review, not made unilaterally here.
# Instead we reject any `<!DOCTYPE` / `<!ENTITY` declaration by a raw
# substring check on the input *before* it ever reaches the parser: a
# legitimate SVG image has no use for either, so this closes the XXE and
# entity-expansion vectors without adding a dependency — expat is only ever
# asked to parse markup that has no DOCTYPE, so there is nothing for it to
# resolve or expand.
#
# `<style>` is dropped entirely rather than sanitized in place: CSS can hide
# the same `javascript:` / `@import` / `expression()` primitives an
# attribute-value check would have to re-implement (nested `@import`,
# comments splitting tokens, media queries, ...). The legitimate use cases in
# this codebase (card backgrounds, weather SVGs) only need presentation
# *attributes* (`fill`, `stroke`, gradients, ...), which stay fully
# supported — so dropping `<style>` loses nothing we use while removing a
# whole class of CSS-based smuggling.

# Namespace prefixes used when re-serializing sanitized SVG — registered so
# output reads as plain `xmlns="..."` instead of ElementTree's default
# `ns0:` prefix. Process-wide (xml.etree module state); harmless to set more
# than once.
ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

_SVG_ALLOWED_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "text", "tspan", "defs", "lineargradient", "radialgradient",
    "stop", "clippath", "mask", "pattern", "use", "symbol", "title", "desc",
    "filter",
    # SVG filter primitives (fe*)
    "feblend", "fecolormatrix", "fecomponenttransfer", "fecomposite",
    "feconvolvematrix", "fediffuselighting", "fedisplacementmap",
    "fedistantlight", "fedropshadow", "feflood", "fefunca", "fefuncb",
    "fefuncg", "fefuncr", "fegaussianblur", "feimage", "femerge",
    "femergenode", "femorphology", "feoffset", "fepointlight",
    "fespecularlighting", "fespotlight", "fetile", "feturbulence",
}

# Attribute-value substrings that make ANY attribute unsafe, wherever they
# appear (not just href) — checked case-insensitively after stripping
# whitespace/control characters used to dodge naive filters
# (e.g. "java\tscript:", "java\nscript:").
_SVG_UNSAFE_VALUE_SUBSTRINGS = (
    "javascript:", "vbscript:", "data:text/html", "@import", "expression(",
)

# Raster data URIs allowed as href/xlink:href targets. Deliberately excludes
# `data:image/svg+xml` (recursive SVG-in-SVG re-opens the same XSS surface).
_SVG_HREF_DATA_PREFIXES = ("data:image/png", "data:image/jpeg", "data:image/gif", "data:image/webp")


def _svg_local_name(tag: str) -> str:
    return tag.split("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _svg_href_allowed(value: str) -> bool:
    """`href`/`xlink:href` is safe only as a same-document fragment (`#id`,
    e.g. `<use href="#icon">`) or a raster data: URI. Everything else
    (http(s), javascript:, data:image/svg+xml, ...) is stripped."""
    v = (value or "").strip()
    if v.startswith("#"):
        return True
    return v.lower().startswith(_SVG_HREF_DATA_PREFIXES)


def _sanitize_svg_tree(elem: ET.Element) -> ET.Element | None:
    """Recursively keep only allowlisted elements/attributes.

    Returns `None` to signal `elem` (and its whole subtree) must be dropped.
    """
    if _svg_local_name(elem.tag) not in _SVG_ALLOWED_TAGS:
        return None

    for key in list(elem.attrib.keys()):
        attr_local = _svg_local_name(key)
        raw_value = elem.attrib[key]
        normalized = re.sub(r"[\s\x00-\x1f]+", "", raw_value or "").lower()

        if attr_local.startswith("on"):
            del elem.attrib[key]
            continue
        if attr_local == "href":
            if not _svg_href_allowed(raw_value or ""):
                del elem.attrib[key]
            continue
        if any(bad in normalized for bad in _SVG_UNSAFE_VALUE_SUBSTRINGS):
            del elem.attrib[key]

    for child in list(elem):
        elem.remove(child)
        sanitized_child = _sanitize_svg_tree(child)
        if sanitized_child is not None:
            elem.append(sanitized_child)

    return elem


def sanitize_svg(svg_text: str) -> tuple[str | None, dict | None]:
    """Sanitize untrusted SVG markup before it is written under www/card_builder/.

    Returns `(sanitized_text, None)` on success, or `(None, error_dict)` if
    the input is rejected outright (DOCTYPE/ENTITY present, not well-formed
    XML, or root element isn't `<svg>`). Never raises.
    """
    text = svg_text or ""
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        return None, {
            "error": "unsafe_svg",
            "detail": "DOCTYPE/ENTITY declarations are not allowed in uploaded SVG (XXE / billion-laughs risk).",
        }

    try:
        root = ET.fromstring(text)  # noqa: S314 -- DOCTYPE/ENTITY rejected above; see design note ~line 193
    except ET.ParseError as err:
        return None, {"error": "invalid_svg", "detail": str(err)}

    if _svg_local_name(root.tag) != "svg":
        return None, {"error": "not_svg", "detail": "Root element must be <svg>."}

    element_count = sum(1 for _ in root.iter())
    if element_count > _SVG_MAX_ELEMENTS:
        return None, {
            "error": "svg_too_complex",
            "detail": f"SVG has {element_count} elements; limit is {_SVG_MAX_ELEMENTS}.",
        }

    sanitized_root = _sanitize_svg_tree(root)
    if sanitized_root is None:
        return None, {"error": "not_svg", "detail": "Root <svg> element was rejected by the sanitizer."}

    return ET.tostring(sanitized_root, encoding="unicode"), None


# =========================================================================
# Embedded knowledge — synced from upstream
# https://github.com/studiobts/home-assistant-card-builder @ main
# Run `check_schema_sync()` to verify the upstream package version matches.
# =========================================================================

UPSTREAM_REPO = "studiobts/home-assistant-card-builder"
UPSTREAM_SCHEMA_SYNC = {
    "documented_against": "2.3.0 (main, 2026-05)",
    "document_model_version": 3,
    "source_files": [
        "frontend/src/common/blocks/loader.ts",
        "frontend/src/common/core/model/types.ts",
        "frontend/src/common/types/style-preset.ts",
        "frontend/src/common/core/style-resolver/style-units.ts",
        "frontend/src/common/core/style-resolver/resolved-to-css.ts",
        "frontend/src/common/blocks/components/**/*.ts",
        "docs/panel-blocks.md",
        "docs/block-link.md",
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
        "length_units": ["px", "rem", "em", "%", "vh", "vw", "vmin", "vmax", "ch", "ex", "cm", "mm", "in", "pt", "pc", "auto", "none"],
    },
    "spacing": {
        "properties": [
            "margin", "padding",
            "marginTop", "marginRight", "marginBottom", "marginLeft",
            "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
        ],
        "length_units": ["px", "rem", "em", "%", "vh", "vw", "vmin", "vmax", "ch", "ex", "cm", "mm", "in", "pt", "pc"],
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
    "block-link": ["block", "path", "particle"],
    "block-weather-background": ["block"],
    "block-hourly-forecast": [
        "block", "container", "header", "title", "badge", "range",
        "range-high", "range-low", "strip", "hour", "now", "time",
        "time-meridiem", "icon", "temperature", "temperature-unit",
        "thermalBar", "secondary", "secondary-icon", "secondary-unit",
        "secondary-precipitation-probability",
        "secondary-precipitation-probability-icon",
        "secondary-precipitation-probability-unit",
        "secondary-precipitation", "secondary-precipitation-icon",
        "secondary-precipitation-unit", "secondary-wind-speed",
        "secondary-wind-speed-icon", "secondary-wind-speed-unit",
        "secondary-wind-bearing", "secondary-wind-bearing-icon",
        "secondary-wind-bearing-unit", "secondary-humidity",
        "secondary-humidity-icon", "secondary-humidity-unit",
        "secondary-dew-point", "secondary-dew-point-icon",
        "secondary-dew-point-unit", "secondary-cloud-coverage",
        "secondary-cloud-coverage-icon", "secondary-cloud-coverage-unit",
        "secondary-uv-index", "secondary-uv-index-icon",
        "secondary-uv-index-unit", "secondary-pressure",
        "secondary-pressure-icon", "secondary-pressure-unit",
        "secondary-apparent-temperature",
        "secondary-apparent-temperature-icon",
        "secondary-apparent-temperature-unit", "placeholder",
    ],
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
# Block schema (from upstream v2.3.0 — sync if upstream adds new block types).
# =========================================================================

BLOCK_TYPES: dict[str, dict[str, Any]] = {
    # --- Root ---
    "canvas": {
        "category": "root",
        "label": "Card",
        "accepts_children": True,
        "requires_entity": False,
        "props": {
            "overflow_show": {"type": "bool", "default": True},
            "overflow_allow_blocks_outside": {"type": "bool", "default": True},
        },
        "special_fields": ["canBeDeleted", "canBeDuplicated", "canChangeLayoutMode", "requireEntity", "isHidden"],
        "notes": "ROOT block type of every Card Builder card. Use as `rootId` in DocumentData. Canvas has children directly (no auto drop-zone wrapper, unlike block-container). Standard pattern: canvas → single block-container or block-grid → content. build_from_recipe auto-wraps your recipe blocks in a container under canvas.",
    },
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
            "iconSize": {"type": "int", "default": 24, "min": 1},
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
            "imageUrl": {"type": "string", "default": None, "binding": True, "nullable": True},
            "mediaReference": {"type": "string", "default": "", "binding": True, "notes": "Use cb-media://local/card_builder/<filename> from upload_media response."},
            "objectFit": {"type": "enum", "values": ["initial", "none", "contain", "cover", "stretch", "scale-down", "original"], "default": "initial"},
            "objectPositionMode": {"type": "string", "default": "center", "notes": "Anchor: center, top, bottom, left, right, top-left, top-right, bottom-left, bottom-right, custom."},
            "objectPositionCustom": {"type": "string", "default": "center"},
        },
        "notes": "Prop names confirmed from marketplace cards: mediaReference (NOT media), objectFit (NOT imageFit), objectPositionMode (NOT imagePosition), objectPositionCustom (NOT customPosition).",
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
            "gridConfig": {
                "type": "object",
                "default": {
                    "rows": 2,
                    "columns": 2,
                    "gap": {"row": 0, "column": 0},
                    "areas": [],
                    "rowSizes": [{"unit": "fr", "value": 1}, {"unit": "fr", "value": 1}],
                    "columnSizes": [{"unit": "fr", "value": 1}, {"unit": "fr", "value": 1}],
                },
                "notes": "All grid config nested under one key. Each size entry: {unit: 'fr'|'px'|'%'|'auto', value: int}. Grid auto-creates one drop-zone per cell (rows × columns).",
            },
        },
        "notes": "All grid params live in nested `gridConfig` object — flat `rows`/`columns`/`gap` props at the top level are silently ignored (confirmed via marketplace card inspection).",
    },
    "block-drop-zone": {
        "category": "layout",
        "label": "Drop Zone (virtual)",
        "accepts_children": True,
        "requires_entity": False,
        "props": {
            "zoneIndex": {"type": "int", "default": 0, "raw": True},
            "columnIndex": {"type": "int", "raw": True},
            "row": {"type": "int", "raw": True},
            "column": {"type": "int", "raw": True},
            "gridArea": {"type": "string", "raw": True},
        },
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
            "dateFormat": {"type": "enum", "values": ["full", "long", "medium", "short", "time", "datetime", "relative", "iso"], "default": "full"},
            "formatTemplate": {"type": "string", "default": ""},
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
            "ellipsis": {"type": "bool", "default": True, "notes": "Renamed from legacy useEllipsis."},
        },
    },
    "block-entity-field-icon": {
        "category": "entities",
        "label": "Entity Icon",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "iconSize": {"type": "int", "default": 24, "min": 12, "max": 128},
            "colorMode": {"type": "enum", "values": ["fixed", "state", "availability"], "default": "fixed", "legacy_values": {"state-based": "state", "availability-based": "availability"}},
            "color": {"type": "string", "default": ""},
            "stateColors": {"type": "list", "default": [], "notes": "List of {state, color}; used when colorMode=state."},
            "availableColor": {"type": "string", "default": ""},
            "unavailableColor": {"type": "string", "default": "#9e9e9e"},
        },
    },
    "block-entity-field-attribute": {
        "category": "entities",
        "label": "Entity Attribute",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "attributeName": {"type": "string", "default": ""},
            "showLabel": {"type": "bool", "default": True},
            "customLabel": {"type": "string", "default": ""},
            "labelPosition": {"type": "enum", "values": ["top", "left", "inline"], "default": "top"},
            "format": {"type": "enum", "values": ["text", "numeric", "integer", "datetime", "boolean", "template"], "default": "text"},
            "precision": {"type": "int", "default": 1},
            "dateFormat": {"type": "string", "default": "full"},
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
            "precisionOverride": {"type": "int", "default": 0},
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
    # --- Advanced / Weather ---
    "block-link": {
        "category": "advanced",
        "label": "Link",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "points": {"type": "list", "default": [], "raw": True, "notes": "Raw LinkPoint[]; do not wrap in {value: ...}."},
            "segments": {"type": "list", "default": [], "raw": True, "notes": "Raw LinkSegment[]; do not wrap in {value: ...}."},
            "renderStyle": {"type": "enum", "values": ["particle"], "default": "particle"},
            "particleSize": {"type": "int", "default": 0, "min": 1, "max": 48, "notes": "0/empty uses the renderer default."},
            "flowEnabled": {"type": "bool", "default": True},
            "flowDirectionPositive": {"type": "enum", "values": ["forward", "reverse"], "default": "forward"},
            "speedSource": {"type": "enum", "values": ["state", "attribute"], "default": "state"},
            "speedAttribute": {"type": "string", "default": ""},
            "valueMin": {"type": "number", "default": 0},
            "valueMax": {"type": "number", "default": 0},
            "speedMultiplier": {"type": "number", "default": 1},
            "smoothingEnabled": {"type": "bool", "default": False},
            "smoothingTension": {"type": "number", "default": 0.15},
        },
        "notes": "Special SVG link block introduced in Card Builder 2.0. It is normally created by the builder's Link mode, not by dragging from the block palette.",
    },
    "block-weather-background": {
        "category": "weather",
        "label": "Weather Background",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "svgSource": {"type": "enum", "values": ["default", "media"], "default": "default"},
            "defaultSvgBackground": {"type": "string", "default": "background-1", "notes": "Renamed from legacy defaultBackground."},
            "mediaReference": {"type": "string", "default": "", "notes": "Renamed from legacy customSvg; use cb-media://local/card_builder/<filename>."},
            "showSvgWarnings": {"type": "bool", "default": True},
            "animationsEnabled": {"type": "bool", "default": True, "notes": "Renamed from legacy enableAnimations."},
            "sunPositionUpdateMinutes": {"type": "int", "default": 10, "min": 5, "max": 60, "unit": "minutes", "notes": "Renamed from legacy updateInterval."},
        },
        "entity_domain": "weather",
    },
    "block-hourly-forecast": {
        "category": "weather",
        "label": "Hourly Forecast",
        "accepts_children": False,
        "requires_entity": True,
        "props": {
            "hours": {"type": "int", "default": 12, "min": 4, "max": 24},
            "layout_direction": {"type": "enum", "values": ["horizontal", "vertical"], "default": "horizontal"},
            "horizontal_column_mode": {"type": "enum", "values": ["auto", "fill", "custom"], "default": "auto"},
            "auto_column_min_width": {"type": "int", "default": 90, "min": 1, "unit": "px"},
            "auto_column_max_width": {"type": "string", "default": ""},
            "custom_column_width": {"type": "string", "default": "52px"},
            "show_now_indicator": {"type": "bool", "default": True},
            "show_day_separator": {"type": "bool", "default": True},
            "show_condition_icons": {"type": "bool", "default": True},
            "show_temperature": {"type": "bool", "default": True},
            "show_temperature_unit": {"type": "bool", "default": True},
            "show_thermal_bars": {"type": "bool", "default": True},
            "bar_height": {"type": "int", "default": 40, "min": 10, "max": 100, "unit": "px"},
            "vertical_bar_width_mode": {"type": "enum", "values": ["fill", "custom"], "default": "fill"},
            "vertical_bar_width": {"type": "int", "default": 120, "min": 24, "max": 260, "unit": "px"},
            "show_rain_badge": {"type": "bool", "default": True},
            "rain_threshold": {"type": "number", "default": 0},
            "color_ramp": {"type": "enum", "values": ["none", "thermal", "blue", "amber", "teal", "custom"], "default": "thermal"},
            "temperature_color_range_mode": {"type": "enum", "values": ["forecast", "custom"], "default": "forecast"},
            "temperature_color_min": {"type": "number", "default": 0},
            "temperature_color_max": {"type": "number", "default": 40},
            "color_cold": {"type": "string", "default": "#60a5fa"},
            "color_warm": {"type": "string", "default": "#f59e0b"},
            "color_ramp_interpolation": {"type": "enum", "values": ["rgb", "hsl"], "default": "rgb"},
            "color_ramp_reverse_hue": {"type": "bool", "default": False},
            "humidity_color_ramp": {"type": "enum", "values": ["humidity", "custom"], "default": "humidity"},
            "humidity_color_low": {"type": "string", "default": "#f59e0b"},
            "humidity_color_high": {"type": "string", "default": "#2563eb"},
            "cloud_coverage_color_ramp": {"type": "enum", "values": ["cloud", "custom"], "default": "cloud"},
            "cloud_coverage_color_low": {"type": "string", "default": "#f8fafc"},
            "cloud_coverage_color_high": {"type": "string", "default": "#64748b"},
            "uv_index_color_ramp": {"type": "enum", "values": ["uv", "custom"], "default": "uv"},
            "uv_index_color_low": {"type": "string", "default": "#22c55e"},
            "uv_index_color_high": {"type": "string", "default": "#8b5cf6"},
            "show_precipitation_probability": {"type": "bool", "default": True},
            "show_precipitation": {"type": "bool", "default": False},
            "show_wind_speed": {"type": "bool", "default": True},
            "show_wind_bearing": {"type": "bool", "default": False},
            "show_humidity": {"type": "bool", "default": False},
            "show_dew_point": {"type": "bool", "default": False},
            "show_cloud_coverage": {"type": "bool", "default": False},
            "show_uv_index": {"type": "bool", "default": False},
            "show_pressure": {"type": "bool", "default": False},
            "show_apparent_temperature": {"type": "bool", "default": False},
        },
        "entity_domain": "weather",
        "notes": "Introduced in Card Builder 2.3.0. Requires a weather entity that provides hourly forecasts.",
    },
}


# =========================================================================
# Cards CRUD
# =========================================================================

@mcp.tool(annotations=read("List Card Builder cards"))
def list_cards() -> list[dict]:
    """List every Card Builder card stored on this Home Assistant instance.

    Calls the `card_builder/cards/list` WebSocket command and returns the
    raw collection: each entry carries `id`, `name`, `description`,
    `config` (the DocumentData block tree), `version`, `tags` and
    `categories`.

    Use when: discovering existing cards before reading one in full with
    `card_builder_get_card` or editing one with `card_builder_update_card`.
    Returns: list of card dicts, in storage order, unfiltered.
    """
    return ha._ws_call("card_builder/cards/list")


@mcp.tool(annotations=read("Get Card Builder card by ID"))
def get_card(
    card_id: Annotated[
        str,
        Field(description="Card id or card_id from card_builder_list_cards, e.g. a UUID string."),
    ],
) -> dict:
    """Get one Card Builder card by id, including its full config.

    Fetches the whole card collection via `card_builder/cards/list` and
    matches on `id` or the legacy `card_id` field client-side — there is no
    dedicated get-by-id WebSocket command.

    Use when: inspecting or copying one card's `config` before calling
    `card_builder_update_card` or `card_builder_renderer_card_config`.
    Returns: the matching card dict (same shape as one entry from
    `card_builder_list_cards`).
    Errors: `{"error": "not_found", "card_id": ...}` when no card matches.
    """
    for card in ha._ws_call("card_builder/cards/list") or []:
        if card.get("id") == card_id or card.get("card_id") == card_id:
            return card
    return {"error": "not_found", "card_id": card_id}


@mcp.tool(annotations=write("Create Card Builder card", idempotent=False))
def create_card(
    name: Annotated[str, Field(description="Display name for the new card, e.g. 'AC Salon'.")],
    config: Annotated[
        dict,
        Field(
            description=(
                "Full DocumentData v3 block tree (version/rootId/slots/blocks). "
                "Build it with `card_builder_build_from_recipe` rather than "
                "hand-writing it."
            )
        ),
    ],
    description: Annotated[str, Field(description="Free-text card description. Empty string if omitted.")] = "",
    tags: Annotated[
        list[str] | None,
        Field(description="Freeform tags for filtering in the Card Builder UI. Omit for no tags."),
    ] = None,
    categories: Annotated[
        list[str] | None,
        Field(description="Category labels (e.g. domain names) for the Card Builder UI. Omit for none."),
    ] = None,
    author: Annotated[str, Field(description="Author name to store on the card. Empty string if omitted.")] = "",
) -> dict:
    """Create a new Card Builder card from a DocumentData config.

    Sends `config` and the metadata fields to `card_builder/cards/create`,
    which assigns a new id server-side (confirmed in upstream `storage.py`:
    every create path generates a fresh `uuid4`) — repeating this call with
    identical arguments creates a second, distinct card rather than
    updating the first.

    Use when: saving a card built with `card_builder_build_from_recipe` or
    `card_builder_make_template_card`'s underlying recipe.
    Not for: changing an existing card — use `card_builder_update_card`.
    Returns: the created card dict, including its new `id`.
    Limits: run `card_builder_validate_config` on `config` first; this tool
    does not validate the block tree before sending it.
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


@mcp.tool(annotations=destructive("Update Card Builder card", idempotent=False))
def update_card(
    card_id: Annotated[str, Field(description="Id of the card to update, from card_builder_list_cards.")],
    name: Annotated[str | None, Field(description="New display name. Omit to leave the current name unchanged.")] = None,
    description: Annotated[str | None, Field(description="New description text. Omit to leave it unchanged.")] = None,
    config: Annotated[
        dict | None,
        Field(description="New DocumentData v3 block tree, replacing the whole config. Omit to keep the current config."),
    ] = None,
    tags: Annotated[list[str] | None, Field(description="Replacement tag list. Omit to leave tags unchanged.")] = None,
    categories: Annotated[
        list[str] | None,
        Field(description="Replacement category list. Omit to leave categories unchanged."),
    ] = None,
    skip_version_bump: Annotated[
        bool,
        Field(description="If true, do not bump the card's stored version counter for this change."),
    ] = False,
) -> dict:
    """Update fields of an existing Card Builder card.

    Sends only the parameters that are not omitted to
    `card_builder/cards/update`; every omitted parameter's prior value in
    storage is left untouched, but any parameter that is supplied
    (including `config`) fully replaces the previous value for that field
    with no way to recover it afterwards.

    Use when: renaming a card, editing its `config`, or retagging it.
    Not for: creating a new card — use `card_builder_create_card`.
    Returns: the updated card dict as returned by the WebSocket command.
    """
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


@mcp.tool(annotations=destructive("Delete Card Builder card", idempotent=True))
def delete_card(
    card_id: Annotated[str, Field(description="Id of the card to delete, from card_builder_list_cards.")],
) -> dict:
    """Permanently delete one Card Builder card by id.

    Calls `card_builder/cards/delete`; Home Assistant's WebSocket handler
    returns an empty payload on success. Any dashboard still referencing
    this `card_id` via `custom:card-builder-renderer-card` will show a
    broken/empty card afterwards — this tool does not check for or update
    such references.

    Use when: removing a card that is no longer used on any dashboard.
    Returns: `{"status": "deleted", "card_id": ..., "result": ...}` on
    success.
    Errors: on failure, returns `{"status": "error", "card_id": ...,
    "error": <exception text>}` instead of raising.
    """
    try:
        result = ha._ws_call("card_builder/cards/delete", card_id=card_id)
    except Exception as err:
        return {"status": "error", "card_id": card_id, "error": str(err)}
    return {"status": "deleted", "card_id": card_id, "result": result}


# =========================================================================
# Style presets
# =========================================================================

@mcp.tool(annotations=read("List Card Builder style presets"))
def list_style_presets() -> list[dict]:
    """List every reusable Card Builder style preset.

    Calls `card_builder/style_presets/list` and returns the raw
    collection: each entry has `id`, `name`, `description`, `data` (the
    CSS-style configuration blob) and, if present, `extends_preset_id`.

    Use when: picking an existing preset to extend, or checking presets
    before creating a new one with `card_builder_create_style_preset`.
    Returns: list of style-preset dicts, unfiltered.
    """
    return ha._ws_call("card_builder/style_presets/list")


@mcp.tool(annotations=write("Create Card Builder style preset", idempotent=False))
def create_style_preset(
    name: Annotated[str, Field(description="Display name for the new preset, e.g. 'Dark Hero'.")],
    data: Annotated[
        dict,
        Field(description="CSS-style configuration blob, shaped like a style category map (see card_builder_list_style_categories)."),
    ],
    description: Annotated[str, Field(description="Free-text preset description. Empty string if omitted.")] = "",
    extends_preset_id: Annotated[
        str | None,
        Field(description="Id of another style preset this one extends/inherits from. Omit for no inheritance."),
    ] = None,
) -> dict:
    """Create a new reusable Card Builder style preset.

    Sends `data` and the metadata fields to `card_builder/style_presets/create`,
    which assigns a new id server-side (confirmed in upstream `storage.py`:
    every create path generates a fresh `uuid4`) — repeating this call
    creates a second, distinct preset rather than updating the first.

    Use when: packaging a reusable set of style properties (e.g. a card
    theme) to apply across multiple cards.
    Not for: changing an existing preset — use
    `card_builder_update_style_preset`.
    Returns: the created style-preset dict, including its new `id`.
    """
    payload: dict[str, Any] = {"name": name, "data": data, "description": description}
    if extends_preset_id:
        payload["extends_preset_id"] = extends_preset_id
    return ha._ws_call("card_builder/style_presets/create", **payload)


@mcp.tool(annotations=destructive("Update Card Builder style preset", idempotent=False))
def update_style_preset(
    style_preset_id: Annotated[str, Field(description="Id of the preset to update, from card_builder_list_style_presets.")],
    name: Annotated[str | None, Field(description="New display name. Omit to leave the current name unchanged.")] = None,
    description: Annotated[str | None, Field(description="New description text. Omit to leave it unchanged.")] = None,
    data: Annotated[
        dict | None,
        Field(description="Replacement CSS-style configuration blob. Omit to leave the current data unchanged."),
    ] = None,
    extends_preset_id: Annotated[
        str | None,
        Field(description="Replacement parent preset id to inherit from. Omit to leave inheritance unchanged."),
    ] = None,
) -> dict:
    """Update fields of an existing Card Builder style preset.

    Sends only the parameters that are not omitted to
    `card_builder/style_presets/update`; any parameter that is supplied
    (including `data`) fully replaces the previous value for that field,
    with no way to recover the prior value afterwards.

    Use when: adjusting an existing preset's style data or metadata.
    Not for: creating a new preset — use
    `card_builder_create_style_preset`.
    Returns: the updated style-preset dict as returned by the WebSocket
    command.
    """
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


@mcp.tool(annotations=destructive("Delete Card Builder style preset", idempotent=True))
def delete_style_preset(
    style_preset_id: Annotated[str, Field(description="Id of the preset to delete, from card_builder_list_style_presets.")],
) -> dict:
    """Permanently delete one Card Builder style preset by id.

    Calls `card_builder/style_presets/delete`. Any card whose style data
    references this preset via `extends_preset_id` keeps that dangling id —
    this tool does not check for or update such references.

    Use when: removing a preset that is no longer extended by any card or
    other preset.
    Returns: `{"status": "deleted", "style_preset_id": ..., "result": ...}`
    on success.
    Errors: on failure, returns `{"status": "error", "style_preset_id":
    ..., "error": <exception text>}` instead of raising.
    """
    try:
        result = ha._ws_call("card_builder/style_presets/delete", style_preset_id=style_preset_id)
    except Exception as err:
        return {"status": "error", "style_preset_id": style_preset_id, "error": str(err)}
    return {"status": "deleted", "style_preset_id": style_preset_id, "result": result}


# =========================================================================
# CSS custom properties
# =========================================================================

@mcp.tool(annotations=read("List Card Builder CSS custom properties"))
def list_css_custom_properties() -> list[dict]:
    """List every registered CSS custom property (`@property` registration).

    Calls `card_builder/css_custom_properties/list` and returns the raw
    collection: each entry has `id`, `name`, `syntax` (e.g. `<color>`),
    `initial_value` and `inherits`.

    Use when: checking which custom properties already exist before
    registering a new one with
    `card_builder_create_css_custom_property`.
    Returns: list of CSS-custom-property dicts, unfiltered.
    """
    return ha._ws_call("card_builder/css_custom_properties/list")


@mcp.tool(annotations=write("Create CSS custom property", idempotent=False))
def create_css_custom_property(
    name: Annotated[str, Field(description="CSS custom property name, e.g. '--card-accent-color' (with the leading '--').")],
    syntax: Annotated[str, Field(description="CSS @property syntax descriptor, e.g. '<color>' or '<length>'.")],
    initial_value: Annotated[str, Field(description="Initial value matching `syntax`, e.g. '#2196f3' for a <color>.")],
    inherits: Annotated[
        bool,
        Field(description="Whether the property inherits from parent elements, per the CSS @property spec. Defaults to false."),
    ] = False,
) -> dict:
    """Register a new CSS custom property (`@property`) for use in card styles.

    Sends the four fields to `card_builder/css_custom_properties/create`,
    which assigns a new id server-side (confirmed in upstream
    `storage.py`: every create path generates a fresh `uuid4`) — repeating
    this call registers a second, distinct property rather than updating
    the first.

    Use when: defining an animatable/typed CSS variable (e.g. a themeable
    accent color) that card styles can reference by name.
    Returns: the created CSS-custom-property dict, including its new `id`.
    """
    return ha._ws_call(
        "card_builder/css_custom_properties/create",
        name=name,
        syntax=syntax,
        initial_value=initial_value,
        inherits=inherits,
    )


@mcp.tool(annotations=destructive("Delete CSS custom property", idempotent=True))
def delete_css_custom_property(
    custom_property_id: Annotated[str, Field(description="Id of the property to delete, from card_builder_list_css_custom_properties.")],
) -> dict:
    """Permanently delete one registered CSS custom property by id.

    Calls `card_builder/css_custom_properties/delete`. Any card style
    still referencing this property's `name` keeps that reference — the
    browser falls back to an unset CSS variable — this tool does not check
    for or update such references.

    Use when: removing a custom property that is no longer used by any
    card style.
    Returns: `{"status": "deleted", "custom_property_id": ..., "result":
    ...}` on success.
    Errors: on failure, returns `{"status": "error", "custom_property_id":
    ..., "error": <exception text>}` instead of raising.
    """
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

@mcp.tool(annotations=read("List Card Builder media files"))
def list_media(
    path: Annotated[
        str,
        Field(description="Sub-folder relative to www/card_builder/ to list. Empty string lists the root."),
    ] = "",
) -> dict:
    """List files and folders in the Card Builder media directory.

    Calls `card_builder/media/list`, scoped to `www/card_builder/<path>`.

    Use when: checking what's already uploaded before calling
    `card_builder_upload_media` or `card_builder_delete_media`.
    Returns: dict with the media-browser entries HA's WS handler returns
    for that path (files and sub-folders).
    """
    return ha._ws_call("card_builder/media/list", path=path)


@mcp.tool(annotations=destructive("Upload media to Card Builder library", idempotent=True))
def upload_media(
    filename: Annotated[
        str,
        Field(description="Bare target filename with an allowed image/SVG extension, e.g. 'bg-1.png'. No path separators."),
    ],
    content_base64: Annotated[str, Field(description="Raw file bytes, base64-encoded.")],
    path: Annotated[
        str,
        Field(description="Sub-folder under www/card_builder/ to upload into. Empty string uploads to the root."),
    ] = "",
) -> dict:
    """Upload base64-encoded bytes as a file in the Card Builder media directory.

    Decodes `content_base64` and writes it under `www/card_builder/<path>/
    <filename>` via `card_builder/media/upload`. `filename` must be a bare
    name (no `/`, `\\`, or `..`) with an allowed extension — `.png`,
    `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.avif` (case-insensitive). A
    `.svg` filename gets its decoded content sanitized (script/handler/
    unsafe-URL stripping, element-count cap) — see `sanitize_svg`. The
    file becomes **public** under `/local/` with **no authentication**,
    same origin as the HA frontend. Upstream's `_write_media_file` writes
    with no existence check, so uploading onto an existing `filename`/
    `path` **silently overwrites it**, with no backup.

    Use when: the file bytes are already in memory (e.g. generated inline).
    Not for: a file already on the HA config filesystem — use
    `card_builder_upload_media_from_path`; a remote image — use
    `card_builder_upload_image_from_url`; raw SVG you'd rather not
    base64-encode — use `card_builder_upload_svg`.
    Returns: `{reference, path, url}` — `reference` is the
    `cb-media://local/card_builder/<filename>` URI for a block-image's
    `mediaReference` prop.
    Errors: `{"error": "extension_not_allowed" | "invalid_base64" |
    "file_too_large" | "invalid_svg" | ...}` (plus `sanitize_svg` errors)
    when validation or sanitization rejects the input.
    Limits: decoded content over `_MAX_MEDIA_UPLOAD_BYTES` (15 MiB) is
    refused.
    """
    filename_err = _validate_upload_filename_and_path(filename, path)
    if filename_err:
        return filename_err

    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception as err:
        return {"error": "invalid_base64", "detail": str(err)}

    if len(raw) > _MAX_MEDIA_UPLOAD_BYTES:
        return {"error": "file_too_large", "filename": filename, "limit_bytes": _MAX_MEDIA_UPLOAD_BYTES}

    content_b64 = content_base64
    if filename.lower().endswith(".svg"):
        try:
            svg_text = raw.decode("utf-8")
        except UnicodeDecodeError as err:
            return {"error": "invalid_svg", "detail": f"SVG must be UTF-8 text: {err}"}
        sanitized, err = sanitize_svg(svg_text)
        if err:
            return err
        content_b64 = base64.b64encode(sanitized.encode("utf-8")).decode("ascii")

    return ha._ws_call(
        "card_builder/media/upload",
        path=path,
        filename=filename,
        content=content_b64,
    )


@mcp.tool(annotations=destructive("Upload local file to Card Builder media", idempotent=True))
def upload_media_from_path(
    local_path: Annotated[
        str,
        Field(description="Absolute or relative path to the source file; must resolve under <config>/www or <config>/media."),
    ],
    path: Annotated[
        str,
        Field(description="Sub-folder under www/card_builder/ to upload into. Empty string uploads to the root."),
    ] = "",
    filename: Annotated[
        str | None,
        Field(description="Target filename to store as. Omit to reuse local_path's own filename."),
    ] = None,
) -> dict:
    """Read a local file under the HA config directory and upload it as Card Builder media.

    `local_path` must resolve (after following symlinks) under `<config>/www`
    or `<config>/media`, and have an image/SVG extension — see
    `_ALLOWED_MEDIA_EXTENSIONS`. Anything else (e.g. `secrets.yaml`,
    `/data/options.json`, a sibling directory) is refused before the file is
    read. `.svg` files are sanitized (script/event-handler/unsafe-URL
    stripping) before upload — see `sanitize_svg`. The uploaded file
    becomes **public** under Home Assistant's `/local/` path with **no
    authentication**. Upstream's `_write_media_file` writes with no
    existence check, so uploading onto a `path`/`filename` that already
    holds a file **silently overwrites it**, with no backup.

    Use when: re-publishing a file that already exists on the config
    filesystem (e.g. under `www/`) into the Card Builder media library.
    Not for: bytes already in memory — use `card_builder_upload_media`; a
    remote URL — use `card_builder_upload_image_from_url`.
    Returns: `{reference, path, url}` on success, same shape as
    `card_builder_upload_media`.
    Errors: `{"error": "invalid_path" | "path_not_allowed" |
    "extension_not_allowed" | "file_not_found" | "invalid_svg" | ...}`
    (plus the `sanitize_svg` errors) when the source path is outside the
    allowed roots, has a disallowed extension, doesn't exist, or fails
    sanitization.
    """
    src, err = _resolve_media_source(local_path)
    if err:
        return err
    if not src.is_file():
        return {"error": "file_not_found", "local_path": local_path}

    raw = src.read_bytes()
    if src.suffix.lower() == ".svg":
        try:
            svg_text = raw.decode("utf-8")
        except UnicodeDecodeError as decode_err:
            return {"error": "invalid_svg", "detail": f"SVG must be UTF-8 text: {decode_err}"}
        sanitized, sanitize_err = sanitize_svg(svg_text)
        if sanitize_err:
            return sanitize_err
        payload_b64 = base64.b64encode(sanitized.encode("utf-8")).decode("ascii")
    else:
        payload_b64 = base64.b64encode(raw).decode("ascii")

    return ha._ws_call(
        "card_builder/media/upload",
        path=path,
        filename=filename or src.name,
        content=payload_b64,
    )


@mcp.tool(annotations=destructive("Upload SVG markup to Card Builder media", idempotent=True))
def upload_svg(
    svg_content: Annotated[str, Field(description="Raw SVG markup, starting with '<svg' or '<?xml ...'.")],
    filename: Annotated[str, Field(description="Bare target filename, e.g. 'gradient-bg'. '.svg' is appended if missing.")],
    path: Annotated[
        str,
        Field(description="Sub-folder under www/card_builder/ to upload into. Empty string uploads to the root."),
    ] = "",
) -> dict:
    """Sanitize SVG markup and upload it to the Card Builder media directory.

    `svg_content` must start with `<svg` or `<?xml`. `.svg` is appended to
    `filename` if missing; the result is stored under
    `www/card_builder/<path>/<filename>`. `svg_content` is always
    sanitized first: `<script>`, `<style>`, `<foreignObject>`, `<iframe>`,
    `<embed>`, `<object>` and other non-presentation elements are stripped
    with their subtree, along with every `on*=` handler and any
    `href`/`xlink:href` pointing at `javascript:` or an external/
    `data:image/svg+xml` URL — see `sanitize_svg` for the allowlist. This
    matters because the file becomes **public**, unauthenticated, under
    `/local/`, where an unsanitized SVG opened directly executes embedded
    script as stored XSS into an admin session. `_write_media_file` writes
    with no existence check, so uploading onto an existing `path`/
    `filename` **silently overwrites it**, with no backup.

    Use when: publishing generated or hand-written SVG markup as media.
    Not for: an SVG file on disk — use
    `card_builder_upload_media_from_path`; a remote SVG URL — use
    `card_builder_upload_image_from_url`.
    Returns: `{reference, path, url}` — `reference` is the
    `cb-media://local/card_builder/<filename>` URI for a block-image's
    `mediaReference` prop.
    Errors: `{"error": "not_svg" | "file_too_large" | "path_not_allowed" |
    "svg_too_complex"}` on invalid, unsafe, or oversized input.
    Limits: capped at `_MAX_MEDIA_UPLOAD_BYTES` (15 MiB), then
    `_SVG_MAX_ELEMENTS` elements after parsing.
    """
    txt = (svg_content or "").lstrip()
    if not (txt.startswith("<svg") or txt.startswith("<?xml")):
        return {"error": "not_svg", "detail": "Content must start with '<svg' or '<?xml ...'"}

    raw_len = len((svg_content or "").encode("utf-8"))
    if raw_len > _MAX_MEDIA_UPLOAD_BYTES:
        return {"error": "file_too_large", "filename": filename, "limit_bytes": _MAX_MEDIA_UPLOAD_BYTES}

    final_name = filename if filename.lower().endswith(".svg") else f"{filename}.svg"
    filename_err = _validate_upload_filename_and_path(final_name, path)
    if filename_err:
        return filename_err

    sanitized, err = sanitize_svg(svg_content)
    if err:
        return err
    b64 = base64.b64encode(sanitized.encode("utf-8")).decode("ascii")
    return ha._ws_call(
        "card_builder/media/upload",
        path=path,
        filename=final_name,
        content=b64,
    )


@mcp.tool(annotations=destructive("Download and upload image from URL", idempotent=True, open_world=True))
def upload_image_from_url(
    url: Annotated[str, Field(description="http(s) URL of the image to download, e.g. 'https://example.com/bg.png'.")],
    filename: Annotated[
        str | None,
        Field(description="Target filename to store as. Omit to derive one from the URL's last path segment."),
    ] = None,
    path: Annotated[
        str,
        Field(description="Sub-folder under www/card_builder/ to upload into. Empty string uploads to the root."),
    ] = "",
) -> dict:
    """Download an image from an http(s) URL and upload it to the Card Builder media library.

    Only `http`/`https` schemes are accepted. The response must declare an
    `image/*` Content-Type (covers `image/svg+xml`) and stay within
    `_MAX_URL_IMAGE_BYTES`, else nothing is uploaded. Falls back to
    deriving `filename` from the URL path. An `image/svg+xml` response is
    sanitized (script/handler/unsafe-URL stripping) before upload — see
    `sanitize_svg` — regardless of the requested `filename`, since the
    remote server controls the actual bytes. The file becomes **public**
    under `/local/` with **no authentication**. `card_builder/media/upload`
    writes with no existence check, so uploading onto an existing
    `path`/`filename` **silently overwrites it**, with no backup.

    Use when: mirroring a remote image (e.g. a weather-background asset)
    into the media library so a card can reference it locally.
    Not for: bytes already in memory — use `card_builder_upload_media`; a
    file already on the HA config filesystem — use
    `card_builder_upload_media_from_path`.
    Returns: `{reference, path, url}`, same shape as
    `card_builder_upload_media`.
    Errors: `{"error": "scheme_not_allowed" | "content_type_not_allowed" |
    "file_too_large" | "download_failed" | "invalid_svg" | ...}` (plus
    `sanitize_svg` errors) when the URL, response, or SVG fails validation.
    Limits: 30-second download timeout, capped at `_MAX_URL_IMAGE_BYTES`
    (15 MiB); the one Card Builder tool that reaches outside the HA
    instance and its host.
    """
    import urllib.request
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": "scheme_not_allowed", "url": url, "detail": "Only http/https URLs are allowed"}

    final_name = filename
    if not final_name:
        final_name = parsed.path.rsplit("/", 1)[-1] or "image"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nexus/0.20"})  # noqa: S310 -- scheme checked above
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 -- scheme checked above
            content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith(_ALLOWED_MEDIA_CONTENT_TYPE_PREFIX):
                return {"error": "content_type_not_allowed", "url": url, "content_type": content_type or None}
            content = r.read(_MAX_URL_IMAGE_BYTES + 1)
            if len(content) > _MAX_URL_IMAGE_BYTES:
                return {"error": "file_too_large", "url": url, "limit_bytes": _MAX_URL_IMAGE_BYTES}
    except Exception as err:
        return {"error": "download_failed", "url": url, "detail": str(err)}

    if not final_name.lower().endswith(tuple(_ALLOWED_MEDIA_EXTENSIONS)):
        # Guess extension from content-type if filename has no extension.
        ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif"}
        if content_type in ext_map and not Path(final_name).suffix:
            final_name = final_name + ext_map[content_type]

    if content_type == "image/svg+xml" or final_name.lower().endswith(".svg"):
        try:
            svg_text = content.decode("utf-8")
        except UnicodeDecodeError as err:
            return {"error": "invalid_svg", "url": url, "detail": f"SVG must be UTF-8 text: {err}"}
        sanitized, err = sanitize_svg(svg_text)
        if err:
            return {**err, "url": url}
        content = sanitized.encode("utf-8")
        if not final_name.lower().endswith(".svg"):
            final_name += ".svg"

    b64 = base64.b64encode(content).decode("ascii")
    return ha._ws_call(
        "card_builder/media/upload",
        path=path,
        filename=final_name,
        content=b64,
    )


@mcp.tool(annotations=destructive("Delete Card Builder media file", idempotent=True))
def delete_media(
    path: Annotated[str, Field(description="Path of the file to delete, relative to www/card_builder/ (see card_builder_list_media).")],
) -> dict:
    """Permanently delete one file from the Card Builder media directory.

    Calls `card_builder/media/delete` with `path` relative to
    `www/card_builder/`. Any card whose `mediaReference` points at this
    file (e.g. via `block-image` or `block-weather-background`) will fail
    to load it afterwards — this tool does not check for or update such
    references.

    Use when: removing an uploaded image/SVG that no card uses anymore.
    Returns: `{"status": "deleted", "path": ..., "result": ...}` on
    success.
    Errors: on failure, returns `{"status": "error", "path": ..., "error":
    <exception text>}` instead of raising.
    """
    try:
        result = ha._ws_call("card_builder/media/delete", path=path)
    except Exception as err:
        return {"status": "error", "path": path, "error": str(err)}
    return {"status": "deleted", "path": path, "result": result}


# =========================================================================
# Dashboard helper
# =========================================================================

@mcp.tool(annotations=read("Build renderer card config"))
def renderer_card_config(
    card_id: Annotated[str, Field(description="Id of the Card Builder card to render, from card_builder_list_cards.")],
    slot_entities: Annotated[
        dict | None,
        Field(description="Maps each slot id defined in the card to a HA entity_id, e.g. {'climate': 'climate.salon'}. Omit for no slot bindings."),
    ] = None,
    slot_actions: Annotated[
        dict | None,
        Field(description="Per-instance overrides for the card's action slots. Omit to use the card's own defaults."),
    ] = None,
) -> dict:
    """Build the Lovelace card config dict that renders a Card Builder card.

    Pure computation — assembles
    `{"type": "custom:card-builder-renderer-card", "card_id": ...}` plus
    the optional `slot_entities`/`slot_actions` keys; makes no call to
    Home Assistant.

    Use when: embedding an existing Card Builder card on a dashboard.
    Not for: adding the resulting config to a dashboard — pass it as
    `card_config` to `dashboards_add_card_to_section` (sections views) or
    `dashboards_add_card_to_view` (masonry/panel views).
    Returns: `{"type": "custom:card-builder-renderer-card", "card_id":
    ..., "slot_entities": {...}, "slot_actions": {...}}` — the latter two
    keys are omitted when not provided.
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

@mcp.tool(annotations=read("List Card Builder block types"))
def list_block_types(
    category: Annotated[
        str | None,
        Field(description="Block category to filter to: basic, layout, entities, controls, advanced, or weather. Omit for every category."),
    ] = None,
    include_internal: Annotated[
        bool,
        Field(description="If true, also list internal-only block types (e.g. block-drop-zone) that a recipe must not use directly."),
    ] = False,
) -> list[dict]:
    """List every block type from the embedded Card Builder schema (`BLOCK_TYPES`).

    Reads the module-level `BLOCK_TYPES` constant synced from upstream Card
    Builder (see `card_builder_check_schema_sync`); makes no call to Home
    Assistant.

    Use when: discovering which block types exist before writing a recipe
    for `card_builder_build_from_recipe`.
    Not for: full prop details of one type — use
    `card_builder_get_block_schema`.
    Returns: list of `{type, category, label, accepts_children,
    requires_entity, prop_count}`, one entry per block type.
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


@mcp.tool(annotations=read("Get Card Builder block schema"))
def get_block_schema(
    block_type: Annotated[str, Field(description="Block type id, e.g. 'block-slider', from card_builder_list_block_types.")],
) -> dict:
    """Get the full embedded schema for one Card Builder block type.

    Looks up `block_type` in the module-level `BLOCK_TYPES` constant and
    adds its style targets from `STYLE_TARGETS`; makes no call to Home
    Assistant.

    Use when: checking a block's exact prop names, types, defaults, enum
    values, and supported entity domains before writing a recipe.
    Returns: `{type, category, label, accepts_children, requires_entity,
    props, notes, supported_domains, style_targets, ...}` — the same
    entry as in `BLOCK_TYPES` plus `style_targets`.
    Errors: `{"error": "unknown_block_type", "block_type": ..., "known":
    [...]}` when `block_type` isn't in the embedded schema.
    """
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

@mcp.tool(annotations=read("List button-toggle feature IDs"))
def list_button_toggle_features(
    domain: Annotated[
        str | None,
        Field(description="HA entity domain to filter to, e.g. 'climate' or 'cover'. Omit to list every domain's features."),
    ] = None,
) -> list[dict]:
    """List the `feature` IDs accepted by block-button-toggle / block-select-menu.

    Reads the module-level `FEATURE_DEFINITIONS` constant; makes no call
    to Home Assistant. Feature IDs are **namespaced by domain** — e.g.
    `climate_hvac_mode` for an AC, `cover_state` for a blind, `light_power`
    for a bulb.

    Use when: picking an explicit `feature` value for a
    `block-button-toggle`/`block-select-menu` prop instead of the default
    `"auto"`, which lets Card Builder pick the first available feature for
    the entity at runtime.
    Returns: list of `{domain, id, label}` dicts.
    """
    if domain:
        defs = FEATURE_DEFINITIONS.get(domain) or []
        return [{"domain": domain, **f} for f in defs]
    out: list[dict] = []
    for d, feats in FEATURE_DEFINITIONS.items():
        for f in feats:
            out.append({"domain": d, **f})
    return out


@mcp.tool(annotations=read("List Card Builder style categories"))
def list_style_categories() -> list[dict]:
    """List CSS style categories with their property names and unit conventions.

    Reads the module-level `STYLE_CATEGORIES` constant; makes no call to
    Home Assistant. Property names are **camelCase** (e.g.
    `flexDirection`, `backgroundColor`) — anything else is silently
    discarded by Card Builder's CSS resolver.

    Use when: looking up the exact property names/units for a style
    category before hand-writing a `styles`/`dz_styles` blob.
    Not for: ready-to-use presets — use
    `card_builder_list_style_snippets`.
    Returns: list of `{category, properties, length_units,
    legacy_aliases, notes}` dicts.
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


@mcp.tool(annotations=read("List style targets for a block type"))
def list_style_targets(
    block_type: Annotated[str, Field(description="Block type id, e.g. 'block-entity-field-state', from card_builder_list_block_types.")],
) -> dict:
    """List the style targets (sub-components) one block type supports.

    Looks up `block_type` in `STYLE_TARGETS` (defaulting to the single
    `"block"` target); makes no call to Home Assistant. Example:
    `block-entity-field-state` has `state` and `unit` targets so you can
    colour the number and the unit differently.

    Use when: styling a sub-component of a block (e.g. a slider's thumb)
    rather than the whole block.
    Returns: `{block_type, targets}` where `targets` is the list of valid
    keys for that block's `styles` dict.
    Errors: `{"error": "unknown_block_type", "block_type": ...}` when
    `block_type` isn't in the embedded schema.
    """
    if block_type not in BLOCK_TYPES:
        return {"error": "unknown_block_type", "block_type": block_type}
    targets = STYLE_TARGETS.get(block_type, ["block"])
    return {"block_type": block_type, "targets": targets}


@mcp.tool(annotations=read("List built-in style snippets"))
def list_style_snippets() -> list[dict]:
    """List built-in Card Builder style snippets by name and style category.

    Reads the module-level `STYLE_SNIPPETS` constant; makes no call to
    Home Assistant.

    Use when: browsing ready-made style presets (padding, flex layout,
    typography scale) before composing them with
    `card_builder_build_styles`.
    Not for: the full property list per category — use
    `card_builder_list_style_categories`; the full body of one snippet —
    use `card_builder_get_style_snippet`.
    Returns: list of `{name, categories}` dicts, where `categories` are
    the style-category keys that snippet sets.
    """
    return [
        {"name": name, "categories": list(snippet.keys())}
        for name, snippet in STYLE_SNIPPETS.items()
    ]


@mcp.tool(annotations=read("Get one style snippet"))
def get_style_snippet(
    name: Annotated[str, Field(description="Snippet name, e.g. 'card_padded', from card_builder_list_style_snippets.")],
    target: Annotated[str, Field(description="Style target key to wrap the snippet under, e.g. 'block' or 'state'. Defaults to 'block'.")] = "block",
    container: Annotated[str, Field(description="Responsive container to wrap the snippet under: 'desktop', 'tablet', or 'mobile'. Defaults to 'desktop'.")] = "desktop",
) -> dict:
    """Fetch one built-in style snippet wrapped in the full target/container envelope.

    Looks up `name` in `STYLE_SNIPPETS` and wraps it as
    `{target: {containers: {container: <snippet>}}}`; makes no call to
    Home Assistant.

    Use when: assigning exactly one snippet to a block's `styles` without
    composing several via `card_builder_build_styles`.
    Returns: `{<target>: {containers: {<container>: <snippet>}}}` — ready
    to assign to a block's `styles` directly.
    Errors: `{"error": "unknown_snippet", "name": ..., "known": [...]}`
    when `name` isn't a known snippet.
    """
    snippet = STYLE_SNIPPETS.get(name)
    if not snippet:
        return {"error": "unknown_snippet", "name": name, "known": list(STYLE_SNIPPETS.keys())}
    return {target: {"containers": {container: snippet}}}


@mcp.tool(annotations=read("Compose styles from snippets"))
def build_styles(
    snippet_names: Annotated[list[str], Field(description="Ordered list of snippet names from card_builder_list_style_snippets to merge, e.g. ['card_padded', 'vertical_stack'].")],
    target: Annotated[str, Field(description="Style target key to wrap the result under, e.g. 'block' or 'state'. Defaults to 'block'.")] = "block",
    container: Annotated[str, Field(description="Responsive container to wrap the result under: 'desktop', 'tablet', or 'mobile'. Defaults to 'desktop'.")] = "desktop",
    extra: Annotated[
        dict | None,
        Field(description="Extra ContainerStyleData ({category: {prop: {value, unit}}}) merged in last, overriding the snippets. Omit for none."),
    ] = None,
) -> dict:
    """Compose a styles object from one or more built-in snippets plus optional overrides.

    Merges each named entry from `STYLE_SNIPPETS` in order, then `extra`,
    at the property level — later entries override earlier ones for the
    same category/property; makes no call to Home Assistant.

    Use when: assembling a block's `styles`/`dz_styles` from reusable
    presets instead of hand-writing the full envelope.
    Returns: `{<target>: {containers: {<container>: {<category>:
    {<prop>: <value>, ...}, ...}}}}`.
    Errors: `{"error": "unknown_snippet", "name": ..., "known": [...]}`
    when a name in `snippet_names` isn't a known snippet.
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

@mcp.tool(annotations=read("Check embedded schema against upstream", open_world=True))
def check_schema_sync() -> dict:
    """Compare the embedded block schema against upstream Card Builder's main branch on GitHub.

    Fetches `custom_components/card_builder/manifest.json` and
    `frontend/src/common/blocks/loader.ts` from
    `raw.githubusercontent.com/{UPSTREAM_REPO}/main` over HTTP and diffs
    the upstream version/block list against this module's `BLOCK_TYPES`
    and `UPSTREAM_SCHEMA_SYNC` constants. Reaches outside the HA instance
    and its host.

    Use when: a recipe that used to work stops producing the expected
    card — the upstream integration may have shipped a breaking schema
    change.
    Returns: `{status: "ok"|"drift"|"fetch_failed", upstream_version,
    embedded_version, version_matches, missing_block_types,
    extra_embedded_block_types, advice, embedded_schema, ...}`.
    Errors: `{"status": "fetch_failed", "error": <exception text>,
    "embedded": ...}` when the manifest request itself fails (a timeout or
    network error); a failed loader fetch instead sets `loader_fetch_error`
    and still returns `status`.
    Limits: two GitHub requests with a 10-second timeout each.
    """
    import json
    import re
    import urllib.request

    base_url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main"
    manifest_url = f"{base_url}/custom_components/card_builder/manifest.json"
    loader_url = f"{base_url}/frontend/src/common/blocks/loader.ts"
    try:
        with urllib.request.urlopen(manifest_url, timeout=10) as r:  # noqa: S310 -- hardcoded https URL, not user input
            upstream = json.load(r)
    except Exception as err:
        return {"status": "fetch_failed", "error": str(err), "embedded": UPSTREAM_SCHEMA_SYNC}

    loader_error = None
    upstream_block_types: list[str] = []
    try:
        with urllib.request.urlopen(loader_url, timeout=10) as r:  # noqa: S310 -- hardcoded https URL, not user input
            loader_text = r.read().decode("utf-8", errors="replace")
        upstream_block_types = sorted(set(re.findall(r"\{type:\s*['\"]([^'\"]+)['\"]", loader_text)))
    except Exception as err:
        loader_error = str(err)

    upstream_version = upstream.get("version", "?")
    embedded_version = str(UPSTREAM_SCHEMA_SYNC["documented_against"]).split()[0]
    embedded_block_types = sorted(k for k in BLOCK_TYPES if k != "canvas")
    missing_block_types = [b for b in upstream_block_types if b not in BLOCK_TYPES]
    extra_block_types = [b for b in embedded_block_types if upstream_block_types and b not in upstream_block_types]
    version_matches = upstream_version == embedded_version
    blocks_match = bool(upstream_block_types) and not missing_block_types

    return {
        "status": "ok" if version_matches and blocks_match else "drift",
        "upstream_version": upstream_version,
        "upstream_repo": UPSTREAM_REPO,
        "embedded_version": embedded_version,
        "embedded_doc_version": UPSTREAM_SCHEMA_SYNC["document_model_version"],
        "embedded_documented_against": UPSTREAM_SCHEMA_SYNC["documented_against"],
        "version_matches": version_matches,
        "upstream_block_count": len(upstream_block_types) if upstream_block_types else None,
        "embedded_block_count": len(embedded_block_types),
        "missing_block_types": missing_block_types,
        "extra_embedded_block_types": extra_block_types,
        "loader_fetch_error": loader_error,
        "advice": (
            "If status is 'drift', refresh BLOCK_TYPES / STYLE_CATEGORIES / "
            "STYLE_TARGETS / STYLE_SNIPPETS in tools/card_builder.py from the "
            "upstream source files listed in embedded_schema."
        ),
        "embedded_schema": UPSTREAM_SCHEMA_SYNC,
    }


# =========================================================================
# Recipe guide — embedded how-to so AI clients don't have to scrape upstream
# =========================================================================

_RECIPE_GUIDE_MD = """# Card Builder Recipe Guide

A Card Builder card is a `DocumentData` blob (`version: 3`) with a tree of blocks.

## Root block is `canvas` (NOT block-container)

Every card's `rootId` points to a special **`canvas`** block — confirmed by
inspecting upstream marketplace cards. Canvas carries the card-wide
`entityConfig`, has props `overflow_show` and `overflow_allow_blocks_outside`,
plus the protected flags `canBeDeleted: false`, `canBeDuplicated: false`,
`canChangeLayoutMode: false`. Its children are DIRECT (no auto drop-zone) —
typically one `block-grid` or `block-container` that wraps the actual content.

`build_from_recipe` handles all of this for you: it produces a canvas root
with a single wrapper container, and your recipe blocks live inside that
container's drop-zone where flex/spacing styles take effect.

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
"props": {"iconSize": {"value": 40}, "colorMode": {"value": "availability"}}
```

NOT:

```
"props": {"iconSize": 40, "colorMode": "availability"}   # ignored if sent directly to create_card/update_card
```

`build_from_recipe` auto-wraps raw scalars in `{"value": ...}` so you can
write the shorter form in recipes — direct `create_card` / `update_card`
callers must wrap by hand.

## Block types

Discover them with `list_block_types()` / `get_block_schema(type)`.

Six categories:
- **basic**: `block-text`, `block-icon`, `block-image`
- **layout**: `block-container`, `block-columns`, `block-grid` (plus internal `block-drop-zone`)
- **entities**: `block-entity-field-{state,name,icon,attribute,image}` — all require an entity
- **controls**: `block-slider`, `block-button-toggle`, `block-select-menu` — auto-detect features per domain
- **weather**: `block-weather-background`, `block-hourly-forecast`
- **advanced**: `block-link`

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

## Turnkey templates (the fast path)

For common HA entities, skip the recipe step entirely and use the built-in
template library:

```
list_card_templates()                       # discover the 10 presets
get_card_template("climate_full")          # preview the config (no save)
make_template_card("climate_full",
                  name="AC Salon",
                  slot="climate")           # creates the card in storage
```

Available templates: tile_simple, tile_action, climate_full, cover_panel,
light_dimmer, sensor_hero, media_panel, weather_pretty, gauge_radial,
stat_compare. Each ships polished spacing, typography (camelCase!), HA CSS
custom property colours, and the right control props.
"""


@mcp.tool(annotations=read("Get Card Builder recipe guide"))
def recipe_guide() -> str:
    """Get the embedded how-to for building Card Builder cards programmatically.

    Returns the static `_RECIPE_GUIDE_MD` constant; makes no call to Home
    Assistant.

    Use when: starting a card build and needing the DocumentData shape,
    prop-wrapping rules, slots, or recipe shorthand before calling
    `card_builder_build_from_recipe`.
    Not for: UX/visual guidance — use `card_builder_design_principles`.
    Returns: markdown text covering DocumentData layout, the block tree,
    entity inheritance, slots, drop-zones, dashboard embedding, and the
    recipe shorthand consumed by `card_builder_build_from_recipe`.
    """
    return _RECIPE_GUIDE_MD


# =========================================================================
# UX layer — design principles, pattern catalog, and intent-based assistant.
# Companion to `recipe_guide` (which covers HOW to build) — these tools
# cover WHAT to build for the user experience to actually feel polished.
# =========================================================================

_DESIGN_PRINCIPLES_MD = """# Card Builder Design Principles

Cards on a dashboard compete for attention. The difference between a
"functional" card and a "premium" one is rarely the data shown — it's the
visual hierarchy, state feedback, and the rhythm of spacing and typography.

## 1. Hierarchy: pick ONE primary number

Every card should answer one question at a glance. Make that answer huge:

| Card type | Primary | Secondary | Tertiary |
| --- | --- | --- | --- |
| Climate | `current_temperature` (56-60px bold) | target temp (16px) | mode chips, min/max |
| Sensor hero | the value (32px+ bold) | unit, label (12px uppercase) | trend, history |
| Tile | icon (32-48px, state-colored) | name (14px 600) | state (12px secondary) |
| Cover | icon (32px) | name + state | position slider |

Anti-pattern: 24px grey circle icon next to 14px name on a flat dark
background. Nothing draws the eye — the card is invisible.

## 2. State feedback via color binding

Use `binding: {mode: "map", map: {state1: color1, ...}}` on icons,
backgrounds, and badges to make state visible without reading the text:

```
"icon": {
  "value": "mdi:snowflake",
  "binding": {"mode": "map", "map": {
    "off": "mdi:power-off",
    "cool": "mdi:snowflake",
    "heat": "mdi:fire",
    "auto": "mdi:autorenew"
  }}
}
```

Same pattern works on `color`, `backgroundColor`, `borderColor`,
`opacity`. Default if state not mapped: provide one via `default:` key
so unknown states don't render blank.

## 3. Spacing rhythm: 4 / 8 / 12 / 16 / 24

Don't pick arbitrary numbers. Stick to the 4px-multiple scale:

- 4px — tight space between related items (label/value)
- 8px — between sibling controls in a row
- 12px — between sections inside a card
- 16px — card padding
- 24px — between cards on a dashboard

## 4. Typography scale: 60 / 32 / 17 / 14 / 12 / 11 / 10

| Size | Use | Weight | Notes |
| --- | --- | --- | --- |
| 60px | hero number on premium climate / energy hero | 700 | line-height: 1 |
| 32px | sensor hero number | 700 | |
| 17-18px | section heading | 600 | |
| 14-15px | card name | 600 | |
| 13px | secondary attribute / metric in footer | 500 | |
| 12px | body text / state line | 400-500 | |
| 11px | mode chip text | 700 | uppercase, letter-spacing: 1 |
| 10px | label above value ("TARGET", "CURRENT") | 600 | uppercase, letter-spacing: 2 |

## 5. Semantic colors — use HA CSS variables

Avoid hardcoded `#xxxxxx` (breaks dark theme). Lean on HA's tokens:

| Token | When |
| --- | --- |
| `var(--ha-card-background, var(--card-background-color))` | card bg |
| `var(--primary-text-color)` | main text on plain bg |
| `var(--secondary-text-color)` | small secondary text |
| `var(--accent-color, var(--primary-color))` | brand accents |
| `var(--success-color, #4caf50)` | on / open / success |
| `var(--warning-color, #ff9800)` | heat / caution |
| `var(--error-color, #f44336)` | offline / unavailable |
| `var(--info-color, #2196f3)` | cool / info |

When a card has its own dark gradient bg image, use white-with-alpha:
`#ffffff`, `rgba(255,255,255,0.85)`, `rgba(255,255,255,0.55)`,
`rgba(255,255,255,0.15)`. Build a 4-step scale.

## 6. Layout: grid for 2D, flow for linear, absolute for sparingly

- **block-grid + cells** → 2D layouts (header + content + controls + footer).
  Each cell is a drop-zone with its own flex direction. Predictable.
- **block-container + drop-zone (flow)** → linear vertical/horizontal
  stacks. Tile-style cards. Simple.
- **`layout: "absolute"` with positionX/positionY** → only for overlapping
  decorative elements (background image, glow, small badge corner).
  NEVER for primary content layout — positions break on different card
  widths.

Anti-pattern: nesting drop-zones with different flex directions and
hoping the renderer respects them. Card Builder collapses nested
mismatched flex into column. Use `block-grid` instead.

## 7. State badges, chips, indicators

A small uppercase badge (OFF / COOL / 23%) in the top-right of a card is
worth more than a paragraph of explanation. Pattern:

```
"styles": {
  "block": {"containers": {"desktop": {
    "typography": {
      "fontSize": {"value": 10}, "letterSpacing": {"value": 1.5},
      "fontWeight": {"value": "700"}, "textTransform": {"value": "uppercase"}
    },
    "spacing": {"padding": {"value": {"top": 4, "right": 10, "bottom": 4, "left": 10}, "unit": "px"}},
    "background": {"backgroundColor": {"binding": {"mode": "map", "map": {...}}}},
    "border": {"borderRadius": {"value": 999, "unit": "px"}}
  }}}
}
```

`borderRadius: 999px` = pill shape regardless of content width.

## 8. Background images: SVG gradients beat plain dark

Plain `var(--ha-card-background)` is fine but boring. A subtle SVG
gradient via `block-image` (absolute, 100×100%, zIndex below content)
adds depth without competing with the data. Generate one with
`upload_svg(...)` — Claude designs SVG inline matching the card domain
(blue tones for water/sensor, warm for heat, neutral dark for entry/lock).

## 9. Empty states / unavailable / unknown

Don't leave gaps when an entity is `unavailable` or `unknown`. Either:
- Bind to an "unavailable" icon (`mdi:circle-off-outline`) and grey colour.
- Or use `display: none` on the slider/control via state-based binding so
  broken UI doesn't show.

## 10. Anti-patterns to avoid

- **Tiny grey 24px icons** on flat dark cards (invisible).
- **Hard-coded card width** assumptions (don't use `positionX: 400` for
  "right side" — card width varies).
- **block-button-toggle on ESPHome climate** — upstream `filterOptionsByServices`
  drops options when SUPPORT_HVAC_MODE bit is missing. Use display-only
  mode chips (block-text with state binding) instead.
- **Setting `colorMode: "state-based"` on entity-field-icon** — the
  Card Builder 2.x renderer expects `"state"` (rename, see schema).
- **Nesting block-container > drop-zone > block-container > drop-zone
  with different flex directions** — renderer collapses the inner one.
  Use block-grid cells.
- **Negative `positionX` with `top-right` anchor** — renderer ignores the
  anchor and treats positionX as offset from LEFT. Result: content off-screen.
"""


@mcp.tool(annotations=read("Get Card Builder design principles"))
def design_principles() -> str:
    """Get the embedded UX design playbook for Card Builder cards.

    Returns the static `_DESIGN_PRINCIPLES_MD` constant; makes no call to
    Home Assistant.

    Use when: deciding hierarchy, state feedback, spacing, typography, or
    layout choices for a card, before or after using
    `card_builder_recipe_guide` for the mechanics.
    Not for: HOW to build the block tree itself — use
    `card_builder_recipe_guide`.
    Returns: markdown text covering hierarchy, state feedback via binding,
    spacing rhythm, typography scale, semantic colors, layout choice
    (grid vs flow vs absolute), state badges, background images, empty
    states, and anti-patterns that produce flat-feeling cards.
    """
    return _DESIGN_PRINCIPLES_MD


# Curated catalog of design patterns. Each entry returns a recipe-style
# spec PLUS UX notes — so AI clients get *both* the config to use and
# the reasoning behind it.
DESIGN_PATTERNS: dict[str, dict[str, Any]] = {
    "climate_premium": {
        "intent_keywords": ["climate", "ac", "thermostat", "hvac", "air conditioner"],
        "domains": ["climate"],
        "label": "Climate Premium (Hero)",
        "description": (
            "Full-height (340px) climate card mirroring the Card Builder marketing card. "
            "Header with state-bound icon and pill badge, big current_temperature in centre, "
            "MIN/MAX markers, mode chips row (OFF/COOL/HEAT/AUTO highlighted by binding), "
            "temp slider, and Fan/Current footer."
        ),
        "ux_notes": [
            "Hero number = current_temperature, not the state. State is shown as a badge + icon colour.",
            "Mode chips are DISPLAY-ONLY for ESPHome climate (filterOptionsByServices kills functional toggle).",
            "Icon and badge bg use map-mode binding so card visually conveys state without reading text.",
            "Gauge SVG background gives the card a 'designed' feel without competing with data.",
        ],
        "recommended_template": "L10 Climate Hero pattern (canvas 340px + grid 6 rows + block-image bg).",
        "anti_patterns": [
            "Don't add block-button-toggle for HVAC modes — empty for ESPHome AC.",
            "Don't put HVAC mode in primary slot — use as binding key instead.",
        ],
    },
    "sensor_hero": {
        "intent_keywords": ["sensor", "level", "battery", "yield", "humidity", "value", "stat"],
        "domains": ["sensor"],
        "label": "Sensor Hero",
        "description": (
            "Compact card with uppercase letter-spaced LABEL on top and a huge bold "
            "value below. Best for one-number-at-a-glance: water level, battery %, "
            "daily solar yield, today's consumption."
        ),
        "ux_notes": [
            "Label uppercase + letter-spacing 1-2px = badge feel, draws less attention than the number.",
            "Format `numeric` with `precision: 0-2` — never show raw '77.0000076293945'.",
            "Add a state-based bg gradient hint (red < 20%, amber < 50%, green ≥ 50%) for ranges.",
        ],
        "recommended_template": "sensor_hero recipe (CARD_RECIPES['sensor_hero']).",
        "anti_patterns": [
            "Don't show the entity name as the primary — value is the primary.",
        ],
    },
    "tile_with_state_color": {
        "intent_keywords": ["tile", "toggle", "switch", "lock", "binary"],
        "domains": ["switch", "light", "lock", "input_boolean", "binary_sensor"],
        "label": "Tile with state-coloured icon",
        "description": (
            "Marketplace 2×2 grid tile (canvas 150px). Icon left (40px, state-bound colour), "
            "state + name absolute-positioned on the right. Slider hidden. Tap → toggle."
        ),
        "ux_notes": [
            "Icon 40px (NOT 24) — primary visual anchor.",
            "Bind icon colour: on/locked → amber, off/unlocked → grey, unavailable → dim grey.",
            "State text uppercase, letter-spaced, secondary colour — it's a label not a value.",
            "Card-level action: tap = toggle. Name target: tap = more-info.",
        ],
        "recommended_template": "compact_tile recipe (CARD_RECIPES['compact_tile']).",
        "anti_patterns": [
            "Don't use 24px greyscale icon — invisible on dark dashboard.",
            "Don't centre-stack everything when you have a left column free.",
        ],
    },
    "cover_panel": {
        "intent_keywords": ["cover", "blind", "shutter", "garage", "gate", "roller"],
        "domains": ["cover"],
        "label": "Cover panel",
        "description": (
            "Marketplace 2×2 grid with icon, state, name, and a position slider in "
            "the bottom-left cell. coverControl: auto handles position vs tilt detection."
        ),
        "ux_notes": [
            (
                "Slider works only if cover supports SET_POSITION (bit 4 of supported_features). "
                "For Supla/Netatmo without it, the slider reads state (open=100, closed=0) and "
                "tap-toggles open/close instead."
            ),
            "Show position % in the slider value label.",
        ],
        "recommended_template": "compact_cover recipe.",
        "anti_patterns": [
            "Don't show slider if entity supports neither SET_POSITION nor SET_TILT_POSITION.",
        ],
    },
    "light_dimmer": {
        "intent_keywords": ["light", "lamp", "bulb", "dimmer", "brightness"],
        "domains": ["light"],
        "label": "Light dimmer",
        "description": (
            "Marketplace 2×2 grid with brightness slider in percent mode + power toggle. "
            "Icon bound to state (mdi:lightbulb-on / lightbulb-off) via map binding."
        ),
        "ux_notes": [
            "displayMode: 'percent' so the slider shows 0-100 not 0-255.",
            "Power toggle (feature: light_power) on tile-area or icon — works on most lights.",
            "For RGB lights add a secondary slot 'power_sensor' or 'colour' to surface live wattage / colour temp.",
        ],
        "recommended_template": "compact_light recipe.",
        "anti_patterns": [
            "Don't fight Card Builder for colour-picker UI — open more-info dialog instead.",
        ],
    },
    "multi_entity_flow": {
        "intent_keywords": ["energy", "flow", "house", "solar", "grid", "battery"],
        "domains": ["sensor"],
        "label": "Multi-entity flow (energy / network)",
        "description": (
            "Inspired by 'House Energy Flow with Background' marketplace card. "
            "9 slots, animated SVG link blocks between nodes, background image. "
            "For dashboards that want to visualise a system of relationships, not "
            "a single entity."
        ),
        "ux_notes": [
            "Use block-link with renderStyle: particle + speed bound to entity state.",
            "Background image (block-image, absolute fill) anchors the visual.",
            "Bottom strip: block-columns with icon+value pairs per node.",
            "This is heavy — only attempt when you have 5+ related entities and a clear topology.",
        ],
        "recommended_template": "Clone from marketplace card 'House Energy Flow with Background' and rebind slots.",
        "anti_patterns": [
            "Don't try to build this from scratch — start from the marketplace card config.",
        ],
    },
}


@mcp.tool(annotations=read("List curated design patterns"))
def list_design_patterns(
    domain: Annotated[
        str | None,
        Field(description="HA entity domain to filter to, e.g. 'climate' or 'sensor'. Omit to list every pattern."),
    ] = None,
) -> list[dict]:
    """List curated UX design patterns, each pairing a recipe recommendation with the reasoning behind it.

    Reads the module-level `DESIGN_PATTERNS` constant; makes no call to
    Home Assistant.

    Use when: browsing available design patterns before picking one by
    hand, rather than letting `card_builder_design_for_intent` score them.
    Returns: list of pattern dicts, each with `name`, `intent_keywords`,
    `domains`, `label`, `description`, `ux_notes`,
    `recommended_template`, `anti_patterns`.
    """
    out: list[dict] = []
    for name, info in DESIGN_PATTERNS.items():
        if domain and info.get("domains") and domain not in info["domains"]:
            continue
        out.append({"name": name, **info})
    return out


@mcp.tool(annotations=read("Get one design pattern"))
def get_design_pattern(
    name: Annotated[str, Field(description="Pattern name, e.g. 'sensor_hero', from card_builder_list_design_patterns.")],
) -> dict:
    """Get one curated design pattern by name, in full.

    Looks up `name` in the module-level `DESIGN_PATTERNS` constant; makes
    no call to Home Assistant.

    Use when: reading the full rationale/anti-patterns for one pattern
    already picked (e.g. via `card_builder_design_for_intent`).
    Returns: same fields as one entry from
    `card_builder_list_design_patterns`.
    Errors: `{"error": "unknown_pattern", "name": ..., "known": [...]}`
    when `name` isn't a known pattern.
    """
    info = DESIGN_PATTERNS.get(name)
    if not info:
        return {"error": "unknown_pattern", "name": name, "known": list(DESIGN_PATTERNS.keys())}
    return {"name": name, **info}


@mcp.tool(annotations=read("Recommend a design pattern for intent"))
def design_for_intent(
    intent: Annotated[str, Field(description="Free-text description of what the card should do, e.g. 'show battery level'.")],
    domain: Annotated[
        str | None,
        Field(description="HA entity domain to weight the match toward, e.g. 'sensor'. Omit to infer from entity_id or match on keywords alone."),
    ] = None,
    entity_id: Annotated[
        str | None,
        Field(description="Full entity id, e.g. 'sensor.battery'; used to infer domain when domain is omitted."),
    ] = None,
) -> dict:
    """Score curated design patterns against a free-text intent and recommend the top match.

    Scores each entry in `DESIGN_PATTERNS` by domain match (weight 5) and
    intent-keyword overlap (weight 1.5 per hit against `intent.lower()`),
    then returns the top scorer plus up to three runners-up; makes no
    call to Home Assistant.

    Use when: turning a user's free-text card request into a concrete
    pattern/template recommendation.
    Not for: the full pattern body — call
    `card_builder_get_design_pattern(name)` on the recommendation; the
    underlying design rules — call `card_builder_design_principles`.
    Returns: `{intent, domain, entity_id, recommendation, score, label,
    description, ux_notes, recommended_template, anti_patterns,
    next_steps, alternatives}`, or `{..., "recommendation": None, "note":
    ...}` when nothing scores above zero.
    """
    intent_norm = (intent or "").lower()
    if not domain and entity_id and "." in entity_id:
        domain = entity_id.split(".", 1)[0]

    # Score each pattern: domain match + keyword overlap.
    scored: list[tuple[float, str, dict]] = []
    for name, info in DESIGN_PATTERNS.items():
        score = 0.0
        if domain and info.get("domains") and domain in info["domains"]:
            score += 5.0
        for kw in info.get("intent_keywords") or []:
            if kw in intent_norm:
                score += 1.5
        if score > 0:
            scored.append((score, name, info))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return {
            "intent": intent,
            "domain": domain,
            "entity_id": entity_id,
            "recommendation": None,
            "note": "No pattern matched. Fall back to `tile_simple` from CARD_RECIPES, then enrich based on `design_principles()`.",
        }

    top_score, top_name, top_info = scored[0]
    return {
        "intent": intent,
        "domain": domain,
        "entity_id": entity_id,
        "recommendation": top_name,
        "score": top_score,
        "label": top_info.get("label"),
        "description": top_info.get("description"),
        "ux_notes": top_info.get("ux_notes"),
        "recommended_template": top_info.get("recommended_template"),
        "anti_patterns": top_info.get("anti_patterns"),
        "next_steps": [
            "Read full pattern: `get_design_pattern(name)`",
            "Read design rules: `design_principles()`",
            "Build with `make_template_card(template_name, ...)` or `build_from_recipe({...})`",
            "Validate with `validate_config(config)` before saving",
        ],
        "alternatives": [{"name": n, "score": s, "label": i.get("label")} for s, n, i in scored[1:4]],
    }


# Native MCP prompts — clients that support prompts (Claude Code, Cursor)
# can call these to set design context before generating cards.

@mcp.prompt(name="design_dashboard")
def _prompt_design_dashboard() -> str:
    """Set context for designing a whole Home Assistant dashboard."""
    return (
        "You are designing a Home Assistant dashboard using the Card Builder MCP tools "
        "in the `card_builder` namespace. Follow this flow:\n\n"
        "1. Call `card_builder_design_principles()` — read the UX playbook.\n"
        "2. Call `card_builder_list_design_patterns()` — see the curated catalogue.\n"
        "3. Group the user's entities by domain. For each group:\n"
        "   a. Call `card_builder_design_for_intent(intent, domain)` to pick a pattern.\n"
        "   b. Use `card_builder_make_template_card(template, ...)` to instantiate.\n"
        "4. Compose them in a view via `dashboards_add_view_to_dashboard(...)`.\n"
        "5. Generate a background SVG with `card_builder_upload_svg(svg_content, filename)` "
        "for any hero card that needs a designed bg.\n\n"
        "ANTI-PATTERNS (do not):\n"
        "- Use `block-button-toggle` for ESPHome climate (filterOptionsByServices kills options).\n"
        "- Use nested drop-zones with different flex directions (renderer collapses inner).\n"
        "- Use 24px greyscale icons on dark cards (invisible).\n"
        "- Use absolute positioning for primary content layout — use block-grid cells.\n"
    )


@mcp.prompt(name="design_card")
def _prompt_design_card(domain: str, intent: str = "") -> str:
    """Set context for designing one Card Builder card for an entity domain."""
    return (
        f"You are designing a single Card Builder card for HA domain `{domain}`"
        + (f", intent: {intent!r}" if intent else "")
        + ".\n\n"
        "Workflow:\n"
        f"1. `card_builder_design_for_intent({intent!r}, {domain!r})` — get pattern recommendation.\n"
        f"2. `card_builder_get_design_pattern(<recommended>)` — full pattern with UX notes.\n"
        "3. `card_builder_design_principles()` if you need the broader rules.\n"
        "4. `card_builder_list_block_types(category)` and `card_builder_get_block_schema(type)` "
        "if you need to verify a block's prop names.\n"
        "5. Build the config (via recipe or hand-crafted), `validate_config()`, then `create_card()`.\n\n"
        "Hierarchy reminder: pick ONE primary value and make it big (60px for hero, 32px for "
        "sensor). Everything else is secondary. Use state-based icon/colour bindings — never "
        "render a dead grey circle.\n"
    )


@mcp.prompt(name="pick_template")
def _prompt_pick_template(entity_id: str = "") -> str:
    """Help an AI client choose the right pre-built template for an entity."""
    return (
        "Pick a Card Builder template for a HA entity. Steps:\n"
        f"1. `card_builder_design_for_intent('', None, {entity_id!r})` — domain-driven pick.\n"
        "2. `card_builder_list_card_templates(domain)` — see what's pre-built.\n"
        "3. `card_builder_get_card_template(name)` — preview the config (no save).\n"
        "4. `card_builder_make_template_card(template, name, slot='main')` — create.\n\n"
        "If no built-in template fits, fall back to `compact_tile` (simple) and add "
        "extra blocks via direct config edit + `update_card()`.\n"
    )


# =========================================================================
# Recipe builder — turns a shorthand recipe into a full DocumentData
# =========================================================================

def _new_id() -> str:
    return uuid.uuid4().hex


# Props that hold complex objects (NOT TraitPropertyValue) and must NOT be
# wrapped in {value: ...}. Confirmed via marketplace card inspection.
_RAW_PROPS = {
    "block-grid": {"gridConfig"},
    "block-link": {"points", "segments"},
    # Grid cells carry raw geometry metadata (no value-wrapper), confirmed via
    # marketplace card inspection.
    "block-drop-zone": {"row", "column", "gridArea", "zoneIndex", "columnIndex"},
}


_PROP_RENAMES: dict[str, dict[str, str]] = {
    "block-entity-field-name": {
        "useEllipsis": "ellipsis",
    },
    "block-weather-background": {
        "defaultBackground": "defaultSvgBackground",
        "customSvg": "mediaReference",
        "enableAnimations": "animationsEnabled",
        "updateInterval": "sunPositionUpdateMinutes",
    },
}


_PROP_VALUE_RENAMES: dict[tuple[str, str], dict[Any, Any]] = {
    ("block-entity-field-icon", "colorMode"): {
        "state-based": "state",
        "availability-based": "availability",
    },
}


def _make_absolute_position_styles(x: int, y: int, anchor: str = "top-left", unit: str = "px") -> dict:
    """Build the styles envelope for an absolute-positioned block.

    Marketplace pattern: `styles.block.containers.desktop.layout.positionX/Y`
    plus a `_internal.position_config` mirror used by the Card Builder UI
    for anchor/origin semantics.
    """
    return {
        "block": {
            "containers": {
                "desktop": {
                    "layout": {
                        "positionX": {"value": x},
                        "positionY": {"value": y},
                    },
                    "_internal": {
                        "position_config": {
                            "value": {
                                "x": x,
                                "y": y,
                                "anchor": anchor,
                                "unitSystem": unit,
                                "originPoint": anchor,
                            }
                        }
                    },
                }
            }
        }
    }


def _make_size_styles(
    *,
    width: int | None = None,
    height: int | None = None,
    max_width: int | None = None,
    max_height: int | None = None,
    unit: str = "px",
) -> dict:
    """Build a styles envelope with only the size category set."""
    size: dict[str, Any] = {}
    if width is not None:
        size["width"] = {"value": width, "unit": unit}
    if height is not None:
        size["height"] = {"value": height, "unit": unit}
    if max_width is not None:
        size["maxWidth"] = {"value": max_width, "unit": unit}
    if max_height is not None:
        size["maxHeight"] = {"value": max_height, "unit": unit}
    if not size:
        return {}
    return {"block": {"containers": {"desktop": {"size": size}}}}


def _merge_styles(*style_blobs: dict | None) -> dict:
    """Deep-merge multiple style envelopes at the property level.

    Each input is shaped like `{target: {containers: {container: {category: {prop: value}}}}}`.
    Later inputs override earlier ones at the property granularity.
    """
    out: dict[str, Any] = {}
    for blob in style_blobs:
        if not blob:
            continue
        for target, target_data in blob.items():
            out.setdefault(target, {})
            containers = (target_data or {}).get("containers") or {}
            out[target].setdefault("containers", {})
            for cont, cont_data in containers.items():
                out[target]["containers"].setdefault(cont, {})
                for cat, cat_data in (cont_data or {}).items():
                    if isinstance(cat_data, dict):
                        out[target]["containers"][cont].setdefault(cat, {})
                        out[target]["containers"][cont][cat].update(cat_data)
                    else:
                        out[target]["containers"][cont][cat] = cat_data
    return out


def _make_canvas_root(
    children_ids: list[str],
    entity_config: dict | None,
    styles: dict | None = None,
    actions: dict | None = None,
) -> dict:
    """Build the root `canvas` block — required as the rootId of every card.

    Canvas is special vs other layout blocks: it does NOT auto-create a
    drop-zone wrapper. Children are direct (typically a single block-grid
    or block-container). Canvas carries the card-wide entityConfig and
    optional action assignments (`actions.targets.block`).
    """
    block: dict[str, Any] = {
        "id": "root",
        "type": "canvas",
        "label": "Card",
        "parentId": None,
        "children": list(children_ids),
        "layout": "flow",
        "order": 0,
        "zIndex": 0,
        "parentManaged": False,
        "canBeDeleted": False,
        "canBeDuplicated": False,
        "canChangeLayoutMode": False,
        "isHidden": False,
        "requireEntity": False,
        "props": {
            "overflow_show": {"value": True},
            "overflow_allow_blocks_outside": {"value": True},
        },
    }
    if entity_config is not None:
        block["entityConfig"] = entity_config
    if styles is not None:
        block["styles"] = styles
    if actions is not None:
        block["actions"] = actions
    return block


def _wrap_props(props: dict | None, block_type: str | None = None) -> dict:
    """Auto-wrap raw scalar prop values in `{"value": ...}` (TraitPropertyValue shape).

    Card Builder's `getPropertyValue` discards anything that isn't an object
    with a `value` or `binding` key — raw `"iconSize": 40` silently falls
    back to the block's default. This helper makes the recipe shorthand
    `"props": {"iconSize": 40}` work the same as the verbose
    `"props": {"iconSize": {"value": 40}}`.

    A few props on specific blocks are *raw objects* (NOT TraitPropertyValue)
    and must pass through unchanged — for example `block-grid.gridConfig`
    holds the nested {rows, columns, gap, …} shape directly. They're
    tracked in `_RAW_PROPS` (cross-referenced against marketplace cards).
    """
    if not props:
        return {}
    raw_keys = _RAW_PROPS.get(block_type, set()) if block_type else set()
    prop_renames = _PROP_RENAMES.get(block_type or "", {})
    wrapped: dict[str, Any] = {}
    for original_key, v in props.items():
        k = prop_renames.get(original_key, original_key)
        value_renames = _PROP_VALUE_RENAMES.get((block_type or "", k), {})
        if value_renames:
            if isinstance(v, dict) and "value" in v:
                v = {**v, "value": value_renames.get(v.get("value"), v.get("value"))}
            else:
                v = value_renames.get(v, v)
        if k in raw_keys:
            wrapped[k] = v
        elif isinstance(v, dict) and ("value" in v or "binding" in v):
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
        "props": _wrap_props(props, block_type=block_type),
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


@mcp.tool(annotations=read("Build card config from recipe"))
def build_from_recipe(
    recipe: Annotated[
        dict,
        Field(
            description=(
                "Recipe shorthand: {slots, root_slot|root_entity, blocks, "
                "root_styles, root_dz_styles, canvas_styles, action_slots, "
                "...} — see card_builder_recipe_guide for the full shape."
            )
        ),
    ],
) -> dict:
    """Materialise a recipe shorthand into a full DocumentData (config) dict.

    Builds a `canvas` root wrapping one `block-container`, recursively
    turns `recipe["blocks"]` into the block tree via `_build_block_tree`
    (auto-inserting `block-drop-zone` children for layout blocks and
    auto-wrapping raw prop scalars in `{"value": ...}`), and returns the
    assembled `{version, rootId, slots, blocks}` dict. Pure computation —
    makes no call to Home Assistant.

    Use when: turning a recipe (hand-written or from
    `card_builder_get_card_template`) into the config to pass to
    `card_builder_create_card`/`card_builder_update_card`.
    Not for: hand-crafted low-level DocumentData — assemble that directly
    instead; a template-driven one-shot creation — use
    `card_builder_make_template_card`.
    Returns: a dict ready to pass as `config` to `card_builder_create_card`.
    Errors: raises `ValueError` when `recipe["root_slot"]` isn't defined in
    `recipe["slots"]`, when a block in `recipe["blocks"]` has an unknown or
    internal `type`, or when a non-container block type is given
    `children`.
    Limits: run `card_builder_validate_config` on the result before
    saving — this tool does not validate the produced tree itself.
    """
    slots_def = recipe.get("slots") or {}
    root_slot = recipe.get("root_slot")
    root_entity = recipe.get("root_entity")
    children = recipe.get("blocks") or []

    if root_slot and root_slot not in slots_def:
        raise ValueError(f"root_slot {root_slot!r} not defined in recipe.slots")

    blocks: dict[str, dict] = {}

    # Resolve root entityConfig once — applied to canvas so every descendant inherits.
    root_entity_config = None
    if root_slot:
        root_entity_config = {"mode": "slot", "slotId": root_slot}
    elif root_entity:
        root_entity_config = {"mode": "fixed", "entityId": root_entity}

    # Wrapper container (single canvas child) — holds card-wide spacing,
    # background, border-radius. Its auto drop-zone is where the recipe
    # blocks actually live, so flex/typography on dz_styles affect children.
    wrapper = _make_block(
        "block-container",
        parent_id="root",
        order=0,
        styles=recipe.get("root_styles"),
    )
    blocks[wrapper["id"]] = wrapper

    wrapper_dz = _make_block(
        "block-drop-zone",
        parent_id=wrapper["id"],
        order=0,
        parent_managed=True,
        styles=recipe.get("root_dz_styles") or recipe.get("layout_styles"),
    )
    blocks[wrapper_dz["id"]] = wrapper_dz
    wrapper["children"] = [wrapper_dz["id"]]
    wrapper_dz["children"] = _build_block_tree(children, wrapper_dz["id"], blocks)

    # Canvas root — fixed id "root" matches what Card Builder UI produces.
    canvas = _make_canvas_root(
        children_ids=[wrapper["id"]],
        entity_config=root_entity_config,
        styles=recipe.get("canvas_styles"),
        actions=recipe.get("canvas_actions"),
    )
    blocks[canvas["id"]] = canvas

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
        "rootId": canvas["id"],
        "slots": {"entities": slots_full, "actions": recipe.get("action_slots") or {}},
        "blocks": blocks,
    }


# =========================================================================
# Validation
# =========================================================================

@mcp.tool(annotations=read("Validate Card Builder config"))
def validate_config(
    config: Annotated[
        dict,
        Field(description="DocumentData config to check, as produced by card_builder_build_from_recipe or returned by card_builder_get_card."),
    ],
) -> dict:
    """Run lightweight structural validation on a Card Builder config (DocumentData).

    Checks `version`, `rootId` presence, that every block has a known
    type, that parent/child links are mutually consistent, that
    entity-required blocks resolve an entity provider (inherited, slot, or
    fixed) by walking up the tree, and that prop values use the
    `{value: ...}`/`{binding: ...}` wrapper Card Builder requires. Pure
    computation — makes no call to Home Assistant and does not check
    against the live upstream schema (see
    `card_builder_check_schema_sync` for that).

    Use when: sanity-checking a config before
    `card_builder_create_card`/`card_builder_update_card`.
    Returns: `{ok: bool, errors: [...], warnings: [...], block_count,
    slot_count}` — `ok` is false only when `errors` is non-empty;
    `warnings` flags renamed/unwrapped/unknown props without failing.
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
        # Exception: keys listed in _RAW_PROPS for that block type are intentionally raw.
        raw_keys = _RAW_PROPS.get(btype, set())
        known_props = set((info.get("props") or {}).keys())
        prop_renames = _PROP_RENAMES.get(btype, {})
        for pname, pval in (block.get("props") or {}).items():
            if pname in prop_renames:
                warnings.append(
                    f"block {bid!r}: prop {pname!r} was renamed to "
                    f"{prop_renames[pname]!r} in Card Builder 2.x."
                )
            elif known_props and pname not in known_props and not str(pname).startswith("_"):
                warnings.append(
                    f"block {bid!r}: prop {pname!r} is not in the embedded "
                    f"schema for {btype!r}; Card Builder may ignore it."
                )
            value_renames = _PROP_VALUE_RENAMES.get((btype, pname), {})
            if value_renames:
                raw_value = pval.get("value") if isinstance(pval, dict) else pval
                if raw_value in value_renames:
                    warnings.append(
                        f"block {bid!r}: prop {pname!r} value {raw_value!r} "
                        f"was renamed to {value_renames[raw_value]!r} in Card Builder 2.x."
                    )
            if pname in raw_keys:
                continue
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


# =========================================================================
# Card recipes — turnkey templates that produce a polished card config in
# one call. Each recipe returns a *recipe shorthand* that you feed straight
# into `build_from_recipe`. Use `make_template_card` to skip both steps and
# create the card in storage directly.
#
# Recipes lean on HA's CSS custom properties for colours so they look
# correct in both light and dark themes.
# =========================================================================

_COLOR = {
    "card_bg": "var(--ha-card-background, var(--card-background-color))",
    "fg_primary": "var(--primary-text-color)",
    "fg_secondary": "var(--secondary-text-color)",
    "accent": "var(--accent-color, var(--primary-color))",
    "success": "var(--success-color, #4caf50)",
    "warning": "var(--warning-color, #ff9800)",
    "error": "var(--error-color, #f44336)",
    "info": "var(--info-color, #2196f3)",
    "divider": "var(--divider-color)",
}


def _bs(category_data: dict) -> dict:
    """Wrap a ContainerStyleData payload in the full target/container envelope."""
    return {"block": {"containers": {"desktop": category_data}}}


def _recipe_tile_simple(slot: str = "entity") -> dict:
    """Vertical-centered tile: icon + name + state."""
    return {
        "slots": {slot: {"name": "Entity", "description": "Entity displayed on this tile"}},
        "root_slot": slot,
        "root_styles": _bs({
            "spacing": {"padding": {"value": 16, "unit": "px"}},
            "border": {"borderRadius": {"value": 14, "unit": "px"}},
            "background": {"backgroundColor": {"value": _COLOR["card_bg"]}},
        }),
        "root_dz_styles": _bs({
            "flex": {
                "flexDirection": {"value": "column"},
                "alignItems": {"value": "center"},
                "justifyContent": {"value": "center"},
                "gap": {"value": 6, "unit": "px"},
            },
        }),
        "blocks": [
            {"type": "block-entity-field-icon", "props": {"iconSize": 40, "colorMode": "availability"}},
            {
                "type": "block-entity-field-name",
                "props": {"maxLength": 22, "ellipsis": True},
                "styles": _bs({"typography": {"fontWeight": {"value": "600"}, "fontSize": {"value": 14, "unit": "px"}}}),
            },
            {
                "type": "block-entity-field-state",
                "props": {"showUnit": True},
                "styles": _bs({"typography": {"fontSize": {"value": 12, "unit": "px"}, "color": {"value": _COLOR["fg_secondary"]}}}),
            },
        ],
    }


def _recipe_tile_action(slot: str = "entity") -> dict:
    """Tile with a `tap` action slot (typically wired to a toggle/service)."""
    recipe = _recipe_tile_simple(slot)
    recipe["action_slots"] = {
        "tap": {"id": "tap", "name": "Tap", "description": "Run on single tap", "trigger": "tap", "action": {"action": "toggle"}},
    }
    return recipe


def _recipe_climate_full(slot: str = "climate") -> dict:
    """AC card: header (icon + name/state) + HVAC toggle + target-temp slider."""
    return {
        "slots": {slot: {"name": "Climate", "description": "Climate entity to control", "domains": ["climate"]}},
        "root_slot": slot,
        "root_styles": _bs({
            "spacing": {"padding": {"value": 16, "unit": "px"}},
            "border": {"borderRadius": {"value": 14, "unit": "px"}},
            "background": {"backgroundColor": {"value": _COLOR["card_bg"]}},
        }),
        "root_dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "gap": {"value": 12, "unit": "px"}}}),
        "blocks": [
            {
                "type": "block-container",
                "dz_styles": _bs({"flex": {"flexDirection": {"value": "row"}, "alignItems": {"value": "center"}, "gap": {"value": 12, "unit": "px"}}}),
                "children": [
                    {"type": "block-entity-field-icon", "props": {"iconSize": 44, "colorMode": "availability"}},
                    {
                        "type": "block-container",
                        "styles": _bs({"flex": {"flexGrow": {"value": "1"}, "flexDirection": {"value": "column"}}}),
                        "dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "gap": {"value": 2, "unit": "px"}}}),
                        "children": [
                            {"type": "block-entity-field-name", "styles": _bs({"typography": {"fontWeight": {"value": "600"}, "fontSize": {"value": 15, "unit": "px"}}})},
                            {
                                "type": "block-entity-field-state",
                                "props": {"showUnit": True},
                                "styles": _bs({"typography": {"fontSize": {"value": 12, "unit": "px"}, "color": {"value": _COLOR["fg_secondary"]}}}),
                            },
                        ],
                    },
                ],
            },
            {
                "type": "block-button-toggle",
                "props": {"feature": "auto", "orientation": "horizontal", "showIcon": True, "showLabel": False},
            },
            {
                "type": "block-slider",
                "props": {
                    "orientation": "horizontal",
                    "shape": "rounded",
                    "showThumb": True,
                    "showValue": True,
                    "valuePositionHorizontal": "inline",
                    "inlinePositionHorizontal": "right",
                },
            },
        ],
    }


def _recipe_cover_panel(slot: str = "cover") -> dict:
    """Cover card: header + open/close toggle + position slider."""
    recipe = _recipe_climate_full(slot)
    recipe["slots"] = {slot: {"name": "Cover", "description": "Cover/blind entity", "domains": ["cover"]}}
    recipe["root_slot"] = slot
    # Replace climate-specific control props with cover ones.
    blocks = recipe["blocks"]
    for b in blocks:
        if b["type"] == "block-button-toggle":
            b["props"] = {"feature": "auto", "orientation": "horizontal", "showIcon": True, "showLabel": False}
        elif b["type"] == "block-slider":
            b["props"]["coverControl"] = "auto"
    return recipe


def _recipe_light_dimmer(slot: str = "light") -> dict:
    """Light card: large icon, name, brightness slider + on/off toggle."""
    return {
        "slots": {slot: {"name": "Light", "description": "Light entity", "domains": ["light"]}},
        "root_slot": slot,
        "root_styles": _bs({
            "spacing": {"padding": {"value": 16, "unit": "px"}},
            "border": {"borderRadius": {"value": 14, "unit": "px"}},
            "background": {"backgroundColor": {"value": _COLOR["card_bg"]}},
        }),
        "root_dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "gap": {"value": 12, "unit": "px"}}}),
        "blocks": [
            {
                "type": "block-container",
                "dz_styles": _bs({"flex": {"flexDirection": {"value": "row"}, "alignItems": {"value": "center"}, "gap": {"value": 12, "unit": "px"}}}),
                "children": [
                    {"type": "block-entity-field-icon", "props": {"iconSize": 48, "colorMode": "availability"}},
                    {
                        "type": "block-container",
                        "styles": _bs({"flex": {"flexGrow": {"value": "1"}, "flexDirection": {"value": "column"}}}),
                        "dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "gap": {"value": 2, "unit": "px"}}}),
                        "children": [
                            {"type": "block-entity-field-name", "styles": _bs({"typography": {"fontWeight": {"value": "600"}, "fontSize": {"value": 15, "unit": "px"}}})},
                            {"type": "block-entity-field-state", "props": {"showUnit": True}, "styles": _bs({"typography": {"fontSize": {"value": 12, "unit": "px"}, "color": {"value": _COLOR["fg_secondary"]}}})},
                        ],
                    },
                ],
            },
            {
                "type": "block-button-toggle",
                "props": {"feature": "light_power", "orientation": "horizontal", "showIcon": True, "showLabel": False},
            },
            {
                "type": "block-slider",
                "props": {
                    "orientation": "horizontal",
                    "shape": "rounded",
                    "showThumb": True,
                    "showValue": True,
                    "displayMode": "percent",
                    "valuePositionHorizontal": "inline",
                    "inlinePositionHorizontal": "right",
                },
            },
        ],
    }


def _recipe_sensor_hero(slot: str = "entity", unit: str | None = None) -> dict:
    """Sensor hero: uppercase label on top, huge value below."""
    state_props: dict = {"showUnit": True, "format": "numeric", "precision": 0}
    if unit:
        state_props["customUnit"] = unit
    return {
        "slots": {slot: {"name": "Entity", "description": "Sensor to display as a hero stat"}},
        "root_slot": slot,
        "root_styles": _bs({
            "spacing": {"padding": {"value": 20, "unit": "px"}},
            "border": {"borderRadius": {"value": 14, "unit": "px"}},
            "background": {"backgroundColor": {"value": _COLOR["card_bg"]}},
        }),
        "root_dz_styles": _bs({
            "flex": {"flexDirection": {"value": "column"}, "alignItems": {"value": "center"}, "justifyContent": {"value": "center"}, "gap": {"value": 4, "unit": "px"}},
        }),
        "blocks": [
            {
                "type": "block-entity-field-name",
                "styles": _bs({"typography": {
                    "fontSize": {"value": 12, "unit": "px"},
                    "color": {"value": _COLOR["fg_secondary"]},
                    "textTransform": {"value": "uppercase"},
                    "letterSpacing": {"value": 1, "unit": "px"},
                }}),
            },
            {
                "type": "block-entity-field-state",
                "props": state_props,
                "styles": _bs({"typography": {"fontWeight": {"value": "700"}, "fontSize": {"value": 32, "unit": "px"}}}),
            },
        ],
    }


def _recipe_media_panel(slot: str = "media") -> dict:
    """Media player card: album art, title/state, play controls + volume slider."""
    return {
        "slots": {slot: {"name": "Media", "description": "Media player entity", "domains": ["media_player"]}},
        "root_slot": slot,
        "root_styles": _bs({
            "spacing": {"padding": {"value": 14, "unit": "px"}},
            "border": {"borderRadius": {"value": 14, "unit": "px"}},
            "background": {"backgroundColor": {"value": _COLOR["card_bg"]}},
        }),
        "root_dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "gap": {"value": 10, "unit": "px"}}}),
        "blocks": [
            {
                "type": "block-container",
                "dz_styles": _bs({"flex": {"flexDirection": {"value": "row"}, "alignItems": {"value": "center"}, "gap": {"value": 12, "unit": "px"}}}),
                "children": [
                    {
                        "type": "block-entity-field-image",
                        "props": {"fallbackIcon": "mdi:music-circle"},
                        "styles": _bs({"size": {"width": {"value": 64, "unit": "px"}, "height": {"value": 64, "unit": "px"}}, "border": {"borderRadius": {"value": 10, "unit": "px"}}}),
                    },
                    {
                        "type": "block-container",
                        "styles": _bs({"flex": {"flexGrow": {"value": "1"}, "flexDirection": {"value": "column"}}}),
                        "dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "gap": {"value": 2, "unit": "px"}}}),
                        "children": [
                            {"type": "block-entity-field-name", "styles": _bs({"typography": {"fontWeight": {"value": "600"}, "fontSize": {"value": 15, "unit": "px"}}})},
                            {"type": "block-entity-field-state", "styles": _bs({"typography": {"fontSize": {"value": 12, "unit": "px"}, "color": {"value": _COLOR["fg_secondary"]}}})},
                        ],
                    },
                ],
            },
            {
                "type": "block-slider",
                "props": {
                    "orientation": "horizontal",
                    "shape": "rounded",
                    "showThumb": True,
                    "showValue": True,
                    "displayMode": "percent",
                    "valuePositionHorizontal": "inline",
                    "inlinePositionHorizontal": "right",
                },
            },
        ],
    }


def _recipe_weather_pretty(slot: str = "weather") -> dict:
    """Weather card: animated SVG background + huge condition + temperature."""
    return {
        "slots": {slot: {"name": "Weather", "description": "Weather entity", "domains": ["weather"]}},
        "root_slot": slot,
        "root_styles": _bs({
            "spacing": {"padding": {"value": 18, "unit": "px"}},
            "border": {"borderRadius": {"value": 14, "unit": "px"}},
            "size": {"height": {"value": 180, "unit": "px"}},
            "layout": {"positionX": {"value": 0}, "positionY": {"value": 0}},
        }),
        "root_dz_styles": _bs({"flex": {"flexDirection": {"value": "column"}, "justifyContent": {"value": "space-between"}, "gap": {"value": 6, "unit": "px"}}}),
        "blocks": [
            {
                "type": "block-weather-background",
                "props": {"svgSource": "default", "defaultSvgBackground": "background-1", "animationsEnabled": True},
                "styles": _bs({
                    "layout": {"zIndex": {"value": -1}},
                    "size": {"width": {"value": 100, "unit": "%"}, "height": {"value": 100, "unit": "%"}},
                }),
            },
            {
                "type": "block-entity-field-state",
                "styles": _bs({"typography": {"fontWeight": {"value": "600"}, "fontSize": {"value": 18, "unit": "px"}, "textTransform": {"value": "capitalize"}, "color": {"value": "#ffffff"}}}),
            },
            {
                "type": "block-entity-field-attribute",
                "props": {"attributeName": "temperature", "showLabel": False, "format": "numeric", "precision": 1, "suffix": "°"},
                "styles": _bs({"typography": {"fontWeight": {"value": "700"}, "fontSize": {"value": 44, "unit": "px"}, "color": {"value": "#ffffff"}}}),
            },
        ],
    }


def _recipe_gauge_radial(slot: str = "entity") -> dict:
    """Radial-ish gauge: huge centered value plus an entity name label.

    Card Builder has no native gauge block — this approximates with a hero
    layout on a tinted background. For a true radial SVG, design one in the
    Card Builder UI and reuse its config.
    """
    recipe = _recipe_sensor_hero(slot, unit="%")
    recipe["root_styles"] = _bs({
        "spacing": {"padding": {"value": 24, "unit": "px"}},
        "border": {"borderRadius": {"value": 999, "unit": "px"}},
        "background": {"backgroundColor": {"value": _COLOR["card_bg"]}},
        "size": {"minHeight": {"value": 160, "unit": "px"}},
    })
    return recipe


def _recipe_stat_compare(slot: str = "entity", label: str = "Today") -> dict:
    """Energy/counter card: small uppercase label + big number."""
    recipe = _recipe_sensor_hero(slot)
    recipe["blocks"][0]["props"] = {"customName": label}
    # Hint the state block to format with kWh by default (override via Card Builder UI if needed).
    for b in recipe["blocks"]:
        if b["type"] == "block-entity-field-state":
            b["props"] = {"showUnit": True, "format": "numeric", "precision": 2}
    return recipe


# =========================================================================
# COMPACT MARKETPLACE-STYLE RECIPES — block-grid + absolute positioning
#
# These bypass `build_from_recipe` because the marketplace pattern uses
# canvas → block-grid(2×2) → grid-cell drop-zones with `layout: "absolute"`
# children + pixel-perfect positionX/positionY. This is the only layout
# that survives Card Builder's renderer for non-trivial cards (confirmed
# by inspecting Light Dimmer Power Sensor from the marketplace).
# =========================================================================

def _make_compact_card_config(
    *,
    slot_id: str,
    slot_name: str = "Entity",
    slot_domains: list[str] | None = None,
    icon: str | None = None,
    icon_size: int = 24,
    has_slider: bool = True,
    slider_props: dict | None = None,
    secondary_slot_id: str | None = None,
    secondary_slot_name: str = "Secondary",
    secondary_slot_domains: list[str] | None = None,
    action_id: str = "main_action",
    action_type: str = "toggle",
    max_height: int = 150,
) -> dict:
    """Produce a marketplace-style 2×2-grid compact card config.

    Layout (mirrors Light Dimmer Power Sensor):
    - Grid: rows=2, columns=2, columnSizes=[1fr, 6fr]
    - Cell (0,0): icon
    - Cell (0,1): entity-state + entity-name (absolute positioned)
    - Cell (1,0): slider (if has_slider)
    - Cell (1,1): hidden (display:none)
    Canvas has `maxHeight: 150px` for compact dashboard tile look.
    """
    # Canvas-level action slot (tap toggle)
    action_slot = {
        "id": action_id,
        "action": {"action": action_type},
        "trigger": "tap",
    }
    secondary_action_id = f"{secondary_slot_id}_more_info" if secondary_slot_id else None
    name_action_id = "entity_name_more_info"
    actions_slots = {action_id: action_slot, name_action_id: {"id": name_action_id, "action": {"action": "more-info"}, "trigger": "tap"}}
    if secondary_action_id:
        actions_slots[secondary_action_id] = {"id": secondary_action_id, "action": {"action": "more-info"}, "trigger": "tap"}

    entity_slots = {slot_id: {"id": slot_id, "name": slot_id, "domains": slot_domains or []}}
    if secondary_slot_id:
        entity_slots[secondary_slot_id] = {"id": secondary_slot_id, "name": secondary_slot_id, "domains": secondary_slot_domains or []}

    # Generate block IDs
    grid_id = _new_id()
    cell00_id = _new_id()
    cell01_id = _new_id()
    cell10_id = _new_id()
    cell11_id = _new_id()
    icon_id = _new_id()
    name_id = _new_id()
    state_id = _new_id()
    slider_id = _new_id() if has_slider else None

    blocks: dict[str, dict] = {}

    # Root canvas (id is "root" to match marketplace)
    blocks["root"] = {
        "id": "root",
        "type": "canvas",
        "label": "Card",
        "order": 0,
        "props": {
            "overflow_show": {"value": True},
            "overflow_allow_blocks_outside": {"value": True},
        },
        "layout": "flow",
        "styles": _make_size_styles(max_height=max_height),
        "zIndex": 0,
        "actions": {"targets": {"block": [action_id]}},
        "children": [grid_id],
        "isHidden": False,
        "parentId": None,
        "canBeDeleted": False,
        "entityConfig": {"mode": "slot", "slotId": slot_id},
        "parentManaged": False,
        "requireEntity": False,
        "canBeDuplicated": False,
        "canChangeLayoutMode": False,
    }

    # The 2×2 grid
    blocks[grid_id] = {
        "id": grid_id,
        "type": "block-grid",
        "order": 0,
        "props": {
            "gridConfig": {
                "gap": {"row": 0, "column": 0},
                "rows": 2,
                "areas": [],
                "columns": 2,
                "rowSizes": [{"unit": "fr", "value": 1}, {"unit": "fr", "value": 1}],
                "columnSizes": [{"unit": "fr", "value": 1}, {"unit": "fr", "value": 6}],
            },
        },
        "layout": "flow",
        "zIndex": 8,
        "children": [cell00_id, cell01_id, cell10_id, cell11_id],
        "parentId": "root",
        "entityConfig": {"mode": "inherited"},
        "parentManaged": False,
    }

    # Grid cells (auto drop-zones)
    blocks[cell00_id] = {
        "id": cell00_id, "type": "block-drop-zone", "label": "Grid Area 1", "order": 0,
        "props": {"row": 0, "column": 0, "gridArea": "1 / 1 / span 1 / span 1", "zoneIndex": 0},
        "layout": "flow",
        "styles": {"block": {"containers": {"desktop": {"flex": {"alignItems": {"value": "flex-start"}, "justifyContent": {"value": "flex-start"}}}}}},
        "zIndex": 9, "children": [icon_id], "parentId": grid_id, "canBeDeleted": False,
        "entityConfig": {"mode": "inherited"}, "parentManaged": False,
        "canBeDuplicated": False, "canChangeLayoutMode": False,
    }
    blocks[cell01_id] = {
        "id": cell01_id, "type": "block-drop-zone", "label": "Grid Area 2", "order": 0,
        "props": {"row": 0, "column": 1, "gridArea": "1 / 2 / span 1 / span 1", "zoneIndex": 1},
        "layout": "flow",
        "styles": {"block": {"containers": {"desktop": {"flex": {"alignItems": {"value": "flex-start"}, "flexDirection": {"value": "row"}, "justifyContent": {"value": "flex-start"}}}}}},
        "zIndex": 10, "children": [state_id, name_id], "parentId": grid_id, "canBeDeleted": False,
        "entityConfig": {"mode": "inherited"}, "parentManaged": False,
        "canBeDuplicated": False, "canChangeLayoutMode": False,
    }
    blocks[cell10_id] = {
        "id": cell10_id, "type": "block-drop-zone", "label": "Grid Area 3", "order": 0,
        "props": {"row": 1, "column": 0, "gridArea": "2 / 1 / span 1 / span 1", "zoneIndex": 2},
        "layout": "flow",
        "styles": {"block": {"containers": {"desktop": {"flex": {"flexDirection": {"value": "row"}, "justifyContent": {"value": "flex-start"}}, "layout": {"display": {"value": "block" if has_slider else "none"}}}}}},
        "zIndex": 11, "children": [slider_id] if has_slider else [], "parentId": grid_id, "canBeDeleted": False,
        "entityConfig": {"mode": "inherited"}, "parentManaged": False,
        "canBeDuplicated": False, "canChangeLayoutMode": False,
    }
    blocks[cell11_id] = {
        "id": cell11_id, "type": "block-drop-zone", "label": "Grid Area 4", "order": 0,
        "props": {"row": 1, "column": 1, "gridArea": "2 / 2 / span 1 / span 1", "zoneIndex": 3},
        "layout": "flow",
        "styles": {"block": {"containers": {"desktop": {"layout": {"display": {"value": "none"}}}}}},
        "zIndex": 12, "children": [], "parentId": grid_id, "canBeDeleted": False,
        "entityConfig": {"mode": "inherited"}, "parentManaged": False,
        "canBeDuplicated": False, "canChangeLayoutMode": False,
    }

    # Icon (cell 0,0)
    icon_props: dict[str, Any] = {"iconSize": {"value": icon_size}, "iconSource": {"value": "list"}, "preTemplate": {"value": None}, "iconTemplate": {"value": None}, "postTemplate": {"value": None}}
    if icon:
        icon_props["icon"] = {"value": icon}
    blocks[icon_id] = {
        "id": icon_id, "type": "block-icon", "order": 0,
        "props": icon_props,
        "layout": "flow", "zIndex": 1,
        "actions": {"targets": {"block": [action_id]}},
        "children": [], "parentId": cell00_id,
        "entityConfig": {"mode": "inherited"}, "parentManaged": False,
    }

    # State (cell 0,1, absolute) — uses secondary slot if provided, else inherited
    state_entity = {"mode": "slot", "slotId": secondary_slot_id} if secondary_slot_id else {"mode": "inherited"}
    state_actions = {"targets": {"block": [secondary_action_id]}} if secondary_action_id else {}
    state_block: dict[str, Any] = {
        "id": state_id, "type": "block-entity-field-state", "order": 0,
        "props": {"format": {"value": "text"}, "showUnit": {"value": True}, "precision": {"value": 1}, "customUnit": {"value": None}, "dateFormat": {"value": "full"}, "formatTemplate": {"value": None}},
        "layout": "absolute",
        "styles": _merge_styles(
            _make_absolute_position_styles(9, 16),
        ),
        "zIndex": 12,
        "children": [], "parentId": cell01_id,
        "entityConfig": state_entity, "parentManaged": False, "requireEntity": True,
    }
    if state_actions:
        state_block["actions"] = state_actions
    blocks[state_id] = state_block

    # Name (cell 0,1, absolute, more-info action)
    blocks[name_id] = {
        "id": name_id, "type": "block-entity-field-name", "order": 1,
        "props": {"case": {"value": "none"}, "ellipsis": {"value": True}, "maxLength": {"value": 0}, "customName": {"value": None}},
        "layout": "absolute",
        "styles": _make_absolute_position_styles(9, -9),
        "zIndex": 6,
        "actions": {"targets": {"block": [name_action_id]}},
        "children": [], "parentId": cell01_id,
        "entityConfig": {"mode": "inherited"}, "parentManaged": False, "requireEntity": True,
    }

    # Slider (cell 1,0)
    if has_slider:
        s_props_base = {
            "mode": "auto", "shape": "rounded", "invert": False, "disabled": False,
            "showThumb": True, "showValue": True, "commitMode": "onRelease",
            "displayMax": 100, "displayMin": 0, "disableMode": "auto", "displayMode": "auto",
            "maxOverride": 100, "minOverride": 0, "orientation": "horizontal", "rangeMinGap": 0,
            "valueSource": "state", "coverControl": "auto", "stepOverride": 1,
            "holdTapAction": "more-info", "activationMode": "press", "holdTapEnabled": False,
            "useMaxOverride": False, "useMinOverride": False, "valueAttribute": None,
            "useStepOverride": False, "commitDebounceMs": 300, "commitThrottleMs": 200,
            "precisionOverride": 0, "usePrecisionOverride": False,
            "valuePositionVertical": "top", "insidePositionVertical": "middle",
            "valuePositionHorizontal": "inline", "inlinePositionHorizontal": "right",
            "insidePositionHorizontal": "center",
        }
        if slider_props:
            s_props_base.update(slider_props)
        blocks[slider_id] = {
            "id": slider_id, "type": "block-slider", "order": 0,
            "props": _wrap_props(s_props_base, block_type="block-slider"),
            "layout": "flow",
            "styles": _merge_styles(
                _make_size_styles(width=170, height=30, max_width=200),
                _make_absolute_position_styles(11, 17),
                {"block": {"containers": {"desktop": {"flex": {"alignItems": {"value": "flex-start"}, "flexDirection": {"value": "row"}, "justifyContent": {"value": "flex-start"}}}}}},
            ),
            "zIndex": 2,
            "children": [], "parentId": cell10_id,
            "entityConfig": {"mode": "inherited"}, "parentManaged": False, "requireEntity": True,
        }

    return {
        "version": 3,
        "rootId": "root",
        "slots": {"entities": entity_slots, "actions": actions_slots},
        "blocks": blocks,
    }


def _recipe_compact_light(slot: str = "main") -> dict:
    return _make_compact_card_config(
        slot_id=slot, slot_name="Light", slot_domains=["light"],
        icon="mdi:lightbulb", has_slider=True,
        slider_props={"displayMode": "percent"},
        action_id="toggle_light", action_type="toggle",
    )


def _recipe_compact_climate(slot: str = "main") -> dict:
    # No HVAC button-toggle — upstream Card Builder × ESPHome incompatibility
    # makes it render empty. Tap on the card opens more-info instead.
    return _make_compact_card_config(
        slot_id=slot, slot_name="Climate", slot_domains=["climate"],
        icon="mdi:air-conditioner", has_slider=True,
        action_id="toggle_climate", action_type="toggle",
    )


def _recipe_compact_cover(slot: str = "main") -> dict:
    return _make_compact_card_config(
        slot_id=slot, slot_name="Cover", slot_domains=["cover"],
        icon="mdi:window-shutter", has_slider=True,
        slider_props={"coverControl": "auto"},
        action_id="toggle_cover", action_type="toggle",
    )


def _recipe_compact_tile(slot: str = "main") -> dict:
    return _make_compact_card_config(
        slot_id=slot, slot_name="Entity",
        icon="mdi:checkbox-blank-circle", has_slider=False,
        action_id="tap", action_type="toggle",
    )


CARD_RECIPES: dict[str, dict] = {
    # Compact marketplace-style (block-grid + absolute, fixed 150px height)
    "compact_tile": {"fn": _recipe_compact_tile, "label": "Compact tile (marketplace style)", "domains": [], "description": "Marketplace 2×2-grid layout: icon left, name+state absolute-positioned right. No slider. 150px tall."},
    "compact_light": {"fn": _recipe_compact_light, "label": "Compact light dimmer (marketplace style)", "domains": ["light"], "description": "Marketplace 2×2-grid: icon + state/name + brightness slider (percent). Mirrors the Light Dimmer Power Sensor template structure."},
    "compact_climate": {"fn": _recipe_compact_climate, "label": "Compact climate (marketplace style)", "domains": ["climate"], "description": "Marketplace 2×2-grid: icon + state/name + temp slider. Skip HVAC toggle on purpose — Card Builder × ESPHome integration filters out features for climate entities that lack SUPPORT_HVAC_MODE in supported_features."},
    "compact_cover": {"fn": _recipe_compact_cover, "label": "Compact cover (marketplace style)", "domains": ["cover"], "description": "Marketplace 2×2-grid: icon + state/name + position slider with coverControl=auto."},
    # Flow-layout (the old vertical-stack recipes — still useful for sensors)
    "tile_simple": {"fn": _recipe_tile_simple, "label": "Tile (simple)", "domains": [], "description": "Vertical-centered tile: icon + name + state. For any entity."},
    "tile_action": {"fn": _recipe_tile_action, "label": "Tile (with tap → toggle)", "domains": ["switch", "light", "input_boolean", "automation", "fan"], "description": "Same as tile_simple plus a 'tap' action slot pre-wired to toggle."},
    "climate_full": {"fn": _recipe_climate_full, "label": "Climate (full controls)", "domains": ["climate"], "description": "Header (icon + name/state) + HVAC mode toggle + target-temp slider."},
    "cover_panel": {"fn": _recipe_cover_panel, "label": "Cover panel", "domains": ["cover"], "description": "Header + open/close toggle + position slider."},
    "light_dimmer": {"fn": _recipe_light_dimmer, "label": "Light dimmer", "domains": ["light"], "description": "Header + power toggle + brightness slider (displayed as percent)."},
    "sensor_hero": {"fn": _recipe_sensor_hero, "label": "Sensor hero", "domains": ["sensor"], "description": "Uppercase label + huge bold value. Great for level / battery / yield sensors."},
    "media_panel": {"fn": _recipe_media_panel, "label": "Media player panel", "domains": ["media_player"], "description": "Album art (with mdi:music-circle fallback) + title/state + volume slider."},
    "weather_pretty": {"fn": _recipe_weather_pretty, "label": "Weather (animated)", "domains": ["weather"], "description": "Animated SVG weather background + condition + temperature."},
    "gauge_radial": {"fn": _recipe_gauge_radial, "label": "Gauge (faux radial)", "domains": ["sensor"], "description": "Pill-shaped hero — approximates a radial gauge. Build a true SVG gauge in the Card Builder UI for richer visuals."},
    "stat_compare": {"fn": _recipe_stat_compare, "label": "Stat (label + big number)", "domains": ["sensor"], "description": "Period-labelled stat: 'TODAY' uppercase + huge 2-decimal number. Pair multiple in a section for compare-at-a-glance grids."},
}


@mcp.tool(annotations=read("List built-in card templates"))
def list_card_templates(
    domain: Annotated[
        str | None,
        Field(description="HA entity domain to filter to, e.g. 'climate' or 'light'. Omit to list every template."),
    ] = None,
) -> list[dict]:
    """List built-in Card Builder recipe templates (`CARD_RECIPES`).

    Reads the module-level `CARD_RECIPES` constant; makes no call to Home
    Assistant.

    Use when: browsing available templates before previewing one with
    `card_builder_get_card_template` or instantiating it with
    `card_builder_make_template_card`.
    Returns: list of `{name, label, description, domains}` dicts.
    """
    out: list[dict] = []
    for name, info in CARD_RECIPES.items():
        if domain and info["domains"] and domain not in info["domains"]:
            continue
        out.append({"name": name, "label": info["label"], "description": info["description"], "domains": info["domains"]})
    return out


@mcp.tool(annotations=read("Preview a card template config"))
def get_card_template(
    name: Annotated[str, Field(description="Template name, e.g. 'sensor_hero', from card_builder_list_card_templates.")],
    slot: Annotated[
        str | None,
        Field(description="Override the template's default entity-slot name, e.g. 'main' instead of 'entity'. Omit to use the template's default."),
    ] = None,
) -> dict:
    """Resolve one built-in template into a full DocumentData config, without saving anything.

    Calls the template's recipe function and, if it returned a recipe
    shorthand rather than a full DocumentData, runs it through
    `card_builder_build_from_recipe`. Makes no call to Home Assistant.

    Use when: inspecting or hand-editing a template's config before
    calling `card_builder_create_card` yourself.
    Not for: saving the card directly — use
    `card_builder_make_template_card` to skip this step.
    Returns: the full DocumentData config dict for the template.
    Errors: `{"error": "unknown_template", "name": ..., "known": [...]}`
    when `name` isn't a known template.
    """
    entry = CARD_RECIPES.get(name)
    if not entry:
        return {"error": "unknown_template", "name": name, "known": list(CARD_RECIPES.keys())}
    output = entry["fn"](slot) if slot else entry["fn"]()
    # Compact builders return a full DocumentData directly (with rootId/version);
    # flow-layout recipes return a recipe shorthand that needs to be built.
    if "rootId" in output and "version" in output:
        return output
    return build_from_recipe(output)


@mcp.tool(annotations=write("Create card from template", idempotent=False))
def make_template_card(
    template: Annotated[str, Field(description="Template name, e.g. 'sensor_hero', from card_builder_list_card_templates.")],
    name: Annotated[str, Field(description="Display name for the new card, e.g. 'Battery Salon'.")],
    description: Annotated[
        str,
        Field(description="Card description. Empty string uses the template's own description instead."),
    ] = "",
    slot: Annotated[
        str | None,
        Field(description="Override the template's default entity-slot name, e.g. 'main' instead of 'entity'. Omit to use the template's default."),
    ] = None,
    tags: Annotated[
        list[str] | None,
        Field(description="Replacement tag list. Omit to default to ['nexus-template', template]."),
    ] = None,
    categories: Annotated[
        list[str] | None,
        Field(description="Replacement category list. Omit to default to the template's first domain, or 'tile' if it has none."),
    ] = None,
) -> dict:
    """Resolve a built-in template and save it as a new Card Builder card in one call.

    Resolves `template` the same way as `card_builder_get_card_template`,
    then passes the config straight to `card_builder_create_card` — which
    assigns a new id server-side, so repeating this call creates a second,
    distinct card.

    Use when: turning a built-in template into a saved card without the
    separate preview/create steps.
    Not for: previewing the config first without saving — use
    `card_builder_get_card_template`; a fully custom recipe — use
    `card_builder_build_from_recipe` and `card_builder_create_card`
    directly.
    Returns: the created card dict, including its new `id`. Use that `id`
    with `card_builder_renderer_card_config(card_id, slot_entities=
    {<slot>: ...})` to place the card on a dashboard.
    Errors: `{"error": "unknown_template", "template": ..., "known":
    [...]}` when `template` isn't a known template.
    """
    entry = CARD_RECIPES.get(template)
    if not entry:
        return {"error": "unknown_template", "template": template, "known": list(CARD_RECIPES.keys())}
    output = entry["fn"](slot) if slot else entry["fn"]()
    config = output if ("rootId" in output and "version" in output) else build_from_recipe(output)
    final_tags = list(tags) if tags else ["nexus-template", template]
    final_categories = list(categories) if categories else [entry["domains"][0] if entry["domains"] else "tile"]
    return create_card(
        name=name,
        config=config,
        description=description or entry["description"],
        tags=final_tags,
        categories=final_categories,
    )
