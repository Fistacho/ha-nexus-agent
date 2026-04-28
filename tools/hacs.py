"""HACS (Home Assistant Community Store) integration via WebSocket commands.

Requires HACS to be installed in Home Assistant. WS message names below are based
on the public HACS API as of 2024+. If HACS changes them, the exact strings may
need to be re-verified against /config/custom_components/hacs/websocket/.
"""
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("hacs")


def _safe_ws(msg_type: str, **kwargs) -> dict | list:
    """Internal: wrap _ws_call so HACS-not-installed surfaces as {"error": ...}."""
    try:
        return ha._ws_call(msg_type, **kwargs)
    except Exception as e:
        return {"error": str(e), "hint": "HACS may not be installed, or the WS command name has changed"}


@mcp.tool()
def list_hacs_repositories(category: str | None = None) -> dict | list:
    """List HACS repositories, optionally filtered by category (integration, plugin, theme, template, appdaemon, python_script)."""
    kwargs: dict = {}
    if category:
        kwargs["categories"] = [category]
    return _safe_ws("hacs/repositories/list", **kwargs)


@mcp.tool()
def get_hacs_repository(repo_id: str) -> dict | list:
    """Get details about a single HACS repository by id."""
    return _safe_ws("hacs/repository/info", repository=repo_id)


@mcp.tool()
def install_hacs_repository(repo_id: str, version: str | None = None) -> dict | list:
    """Install a HACS repository, optionally pinning a specific version."""
    kwargs: dict = {"repository": repo_id}
    if version:
        kwargs["version"] = version
    return _safe_ws("hacs/repository/install", **kwargs)


@mcp.tool()
def uninstall_hacs_repository(repo_id: str) -> dict | list:
    """Uninstall a HACS repository by id."""
    return _safe_ws("hacs/repository/uninstall", repository=repo_id)


@mcp.tool()
def update_hacs_repository(repo_id: str) -> dict | list:
    """Update a HACS repository to its latest available version."""
    return _safe_ws("hacs/repository/update", repository=repo_id)


@mcp.tool()
def add_custom_repository(url: str, category: str) -> dict | list:
    """Add a custom HACS repository by URL + category (integration, plugin, theme, template, appdaemon, python_script)."""
    return _safe_ws("hacs/repository/add", repository=url, category=category)


@mcp.tool()
def list_hacs_critical_updates() -> dict | list:
    """List HACS repositories that have a pending upgrade available."""
    result = _safe_ws("hacs/repositories/list")
    if isinstance(result, dict) and "error" in result:
        return result
    if not isinstance(result, list):
        return {"error": "Unexpected HACS response shape", "raw": result}
    return [r for r in result if isinstance(r, dict) and r.get("pending_upgrade")]
