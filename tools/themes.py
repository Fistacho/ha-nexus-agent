"""Lovelace theme management.

Themes live in `<config>/themes/<name>.yaml` and are loaded via the
`frontend:` integration. Listing/active selection is done over WS/services.
Creating/editing writes a YAML file and triggers `frontend.reload_themes`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

load_dotenv()

mcp = FastMCP("themes")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))
_THEMES_DIR = _CONFIG_PATH / "themes"


def _theme_path(name: str) -> Path:
    if "/" in name or "\\" in name or name.startswith(".") or not name.strip():
        raise ValueError(f"Invalid theme name: {name!r}")
    return (_THEMES_DIR / f"{name}.yaml").resolve()


@mcp.tool(annotations=read("List themes"))
def list_themes() -> dict:
    """List all Lovelace themes registered with the frontend.

    Calls WS `frontend/get_themes` and returns Home Assistant's response
    unchanged.

    Use when: you need every theme's name plus which one is active before
    switching or editing.
    Returns: dict with `themes` (name to variable-map), `default_theme`,
    and `default_dark_theme`."""
    return ha._ws_call("frontend/get_themes")


@mcp.tool(annotations=read("Get theme variables"))
def get_theme(
    name: Annotated[str, Field(description="Theme name to look up, matching a key in themes_list_themes' `themes` map.")],
) -> dict:
    """Get one theme's CSS variable map by name.

    Calls WS `frontend/get_themes` and looks up `name` in the result.

    Use when: inspecting a single theme's variables rather than the whole
    registry from `themes_list_themes`.
    Returns: dict with `name` and `variables` (the CSS variable map).
    Errors: `{"error": "Theme '<name>' not found", "available": [...]}` if
    the name isn't registered."""
    data = ha._ws_call("frontend/get_themes")
    themes = data.get("themes") or {}
    if name not in themes:
        return {"error": f"Theme {name!r} not found", "available": list(themes.keys())}
    return {"name": name, "variables": themes[name]}


@mcp.tool(annotations=write("Set active theme", idempotent=True))
def set_active_theme(
    name: Annotated[str, Field(description="Registered theme name to activate, e.g. 'default' or a custom theme name; defaults to 'default'.")] = "default",
    mode: Annotated[str | None, Field(description="Optional 'light' or 'dark' to also pin the color mode; any other value is ignored; omit to leave the mode as-is.")] = None,
) -> dict:
    """Set the active Lovelace frontend theme.

    Calls service `frontend.set_theme` with `name`, and `mode` when it is
    'light' or 'dark'; any other `mode` value is silently omitted from the
    call.

    Use when: switching which theme the frontend displays.
    Returns: the raw result of the `frontend.set_theme` service call.
    Limits: `mode` values other than 'light'/'dark' are dropped rather than
    rejected."""
    payload: dict = {"name": name}
    if mode in {"light", "dark"}:
        payload["mode"] = mode
    return ha.call_service("frontend", "set_theme", payload)


@mcp.tool(annotations=write("Reload themes from disk", idempotent=True))
def reload_themes() -> dict:
    """Reload theme files from the themes directory.

    Calls service `frontend.reload_themes`, making Home Assistant re-read
    every YAML file under `<config>/themes/`.

    Use when: after `themes_create_theme`, `themes_update_theme` or
    `themes_delete_theme` writes a file with `reload=False`, or after
    editing a theme file outside nexus.
    Returns: dict with `status: 'reloaded'`."""
    ha.call_service("frontend", "reload_themes")
    return {"status": "reloaded"}


@mcp.tool(annotations=destructive("Create theme file", idempotent=True))
def create_theme(
    name: Annotated[str, Field(description="Theme name, becomes the filename themes/<name>.yaml without the extension.")],
    variables: Annotated[dict, Field(description="Inner theme variable map to write, e.g. {'primary-color': '#03a9f4'} or a 'modes' block with 'light'/'dark' keys; not wrapped in the theme name.")],
    overwrite: Annotated[bool, Field(description="If true, replaces an existing theme file instead of failing; defaults to false.")] = False,
    reload: Annotated[bool, Field(description="If true, calls frontend.reload_themes after writing the file; defaults to true.")] = True,
) -> dict:
    """Create a new Lovelace theme file.

    Writes `themes/<name>.yaml` as `{<name>: variables}`. The file is only
    loaded by the frontend if configuration.yaml has `frontend: themes:
    !include_dir_merge_named themes`. Fails instead of overwriting unless
    `overwrite=True`.

    Use when: adding a brand-new theme.
    Not for: changing an existing theme in place — use
    `themes_update_theme`.
    Returns: dict with `success`, `path`, and `reloaded` (present when
    `reload=True`).
    Errors: `{"success": false, "error": "Theme '<name>' already exists.
    Pass overwrite=True to replace."}` when the file exists and
    `overwrite` is false; `{"success": false, "error":
    "invalid_theme_name", "detail": ...}` when `name` contains '/' or a
    backslash, starts with '.', or is blank.
    Limits: `name` must not contain '/' or a backslash, start with '.', or
    be blank."""
    try:
        path = _theme_path(name)
    except ValueError as e:
        return {"success": False, "error": "invalid_theme_name", "detail": str(e)}
    if path.exists() and not overwrite:
        return {"success": False, "error": f"Theme {name!r} already exists. Pass overwrite=True to replace."}

    _THEMES_DIR.mkdir(parents=True, exist_ok=True)
    document = {name: variables}
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")

    result = {"success": True, "path": str(path)}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool(annotations=destructive("Update theme file", idempotent=False))
def update_theme(
    name: Annotated[str, Field(description="Theme name to update; obtain it from themes_list_themes or themes_list_theme_files.")],
    variables: Annotated[dict, Field(description="Variable map to apply; merged into or replacing the existing set depending on `merge`.")],
    merge: Annotated[bool, Field(description="If true, shallow-merges variables into the existing top-level keys; if false, replaces the whole variable set; defaults to true.")] = True,
    reload: Annotated[bool, Field(description="If true, calls frontend.reload_themes after writing the file; defaults to true.")] = True,
) -> dict:
    """Update an existing Lovelace theme file.

    With `merge=True` (default), shallow-merges the given `variables` into
    the existing top-level keys — a passed `modes` key replaces the whole
    `modes` block rather than merging inside it; with `merge=False`,
    replaces the entire variable set. Fails if the theme file doesn't
    exist yet.

    Use when: changing some or all variables on a theme that already
    exists.
    Not for: a theme that doesn't exist yet — use `themes_create_theme`.
    Returns: dict with `success`, `path`, `merged`, and `reloaded` (present
    when `reload=True`).
    Errors: `{"success": false, "error": "Theme '<name>' not found at
    <path>"}` when the file doesn't exist; `{"success": false, "error":
    "invalid_theme_name", "detail": ...}` when `name` contains '/' or a
    backslash, starts with '.', or is blank."""
    try:
        path = _theme_path(name)
    except ValueError as e:
        return {"success": False, "error": "invalid_theme_name", "detail": str(e)}
    if not path.exists():
        return {"success": False, "error": f"Theme {name!r} not found at {path}"}

    if merge:
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        current_vars = existing.get(name, {}) or {}
        current_vars.update(variables)
        document = {name: current_vars}
    else:
        document = {name: variables}

    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")

    result = {"success": True, "path": str(path), "merged": merge}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool(annotations=destructive("Delete theme file", idempotent=True))
def delete_theme(
    name: Annotated[str, Field(description="Theme name to delete; obtain it from themes_list_themes or themes_list_theme_files.")],
    reload: Annotated[bool, Field(description="If true, calls frontend.reload_themes after deleting the file; defaults to true.")] = True,
) -> dict:
    """Delete a theme file.

    Removes `themes/<name>.yaml` from disk if it exists.

    Use when: a theme is no longer needed.
    Returns: dict with `success` and `deleted` (the removed path), plus
    `reloaded` when `reload=True`.
    Errors: `{"success": false, "error": "Theme '<name>' not found at
    <path>"}` when the file doesn't exist; `{"success": false, "error":
    "invalid_theme_name", "detail": ...}` when `name` contains '/' or a
    backslash, starts with '.', or is blank."""
    try:
        path = _theme_path(name)
    except ValueError as e:
        return {"success": False, "error": "invalid_theme_name", "detail": str(e)}
    if not path.exists():
        return {"success": False, "error": f"Theme {name!r} not found at {path}"}
    path.unlink()
    result = {"success": True, "deleted": str(path)}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool(annotations=read("List theme files on disk"))
def list_theme_files() -> list[str]:
    """List YAML theme files present on disk under themes/.

    Scans `<config>/themes/` recursively for `.yaml`/`.yml` files,
    returning their paths relative to the config directory; returns an
    empty list if the directory doesn't exist yet.

    Use when: a theme exists on disk but isn't showing up in
    `themes_list_themes` (not yet loaded).
    Returns: list of file paths relative to the Home Assistant config
    directory."""
    if not _THEMES_DIR.exists():
        return []
    return [
        str(p.relative_to(_CONFIG_PATH))
        for p in _THEMES_DIR.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml"}
    ]
