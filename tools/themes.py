"""Lovelace theme management.

Themes live in `<config>/themes/<name>.yaml` and are loaded via the
`frontend:` integration. Listing/active selection is done over WS/services.
Creating/editing writes a YAML file and triggers `frontend.reload_themes`.
"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP

import ha_client as ha

load_dotenv()

mcp = FastMCP("themes")

_CONFIG_PATH = Path(os.getenv("HA_CONFIG_PATH", "/config"))
_THEMES_DIR = _CONFIG_PATH / "themes"


def _theme_path(name: str) -> Path:
    if "/" in name or "\\" in name or name.startswith(".") or not name.strip():
        raise ValueError(f"Invalid theme name: {name!r}")
    return (_THEMES_DIR / f"{name}.yaml").resolve()


@mcp.tool()
def list_themes() -> dict:
    """List all themes registered with the frontend, plus the active default theme.

    Returns: {"themes": {name: {...}}, "default_theme": "...", "default_dark_theme": "..."}
    """
    return ha._ws_call("frontend/get_themes")


@mcp.tool()
def get_theme(name: str) -> dict:
    """Get a single theme's variable map (CSS variables) by name."""
    data = ha._ws_call("frontend/get_themes")
    themes = data.get("themes") or {}
    if name not in themes:
        return {"error": f"Theme {name!r} not found", "available": list(themes.keys())}
    return {"name": name, "variables": themes[name]}


@mcp.tool()
def set_active_theme(name: str = "default", mode: str | None = None) -> dict:
    """Set the active frontend theme. `mode` can be 'light' or 'dark'."""
    payload: dict = {"name": name}
    if mode in {"light", "dark"}:
        payload["mode"] = mode
    return ha.call_service("frontend", "set_theme", payload)


@mcp.tool()
def reload_themes() -> dict:
    """Reload themes from `<config>/themes/`. Call after creating or editing theme files."""
    ha.call_service("frontend", "reload_themes")
    return {"status": "reloaded"}


@mcp.tool()
def create_theme(
    name: str,
    variables: dict,
    overwrite: bool = False,
    reload: bool = True,
) -> dict:
    """Create a new theme file at `themes/<name>.yaml` with the given CSS variables.

    The HA frontend stores themes as a dict keyed by theme name. Standard variables
    include `primary-color`, `accent-color`, `text-primary-color`, etc.
    For dark/light variants use modes: `{"name": {"modes": {"light": {...}, "dark": {...}}}}`.

    Set `overwrite=True` to replace an existing theme. By default reload_themes is called.
    """
    path = _theme_path(name)
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


@mcp.tool()
def update_theme(name: str, variables: dict, merge: bool = True, reload: bool = True) -> dict:
    """Update an existing theme file.

    `merge=True` (default) merges new variables into existing ones; `merge=False`
    replaces the whole variable set. Calls reload_themes unless reload=False.
    """
    path = _theme_path(name)
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


@mcp.tool()
def delete_theme(name: str, reload: bool = True) -> dict:
    """Delete a theme file from `themes/<name>.yaml`."""
    path = _theme_path(name)
    if not path.exists():
        return {"success": False, "error": f"Theme {name!r} not found at {path}"}
    path.unlink()
    result = {"success": True, "deleted": str(path)}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool()
def list_theme_files() -> list[str]:
    """List YAML files under `themes/`. Useful when a theme exists on disk but isn't loaded yet."""
    if not _THEMES_DIR.exists():
        return []
    return [
        str(p.relative_to(_CONFIG_PATH))
        for p in _THEMES_DIR.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml"}
    ]
