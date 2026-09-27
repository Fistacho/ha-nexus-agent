"""HACS (Home Assistant Community Store) integration via WebSocket commands.

Requires HACS to be installed in Home Assistant. WS message names below are based
on the public HACS API as of 2024+. If HACS changes them, the exact strings may
need to be re-verified against /config/custom_components/hacs/websocket/.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("hacs")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


def _safe_ws(msg_type: str, **kwargs) -> dict | list:
    """Internal: wrap _ws_call so HACS-not-installed surfaces as {"error": ...}."""
    try:
        return ha._ws_call(msg_type, **kwargs)
    except Exception as e:
        return {"error": str(e), "hint": "HACS may not be installed, or the WS command name has changed"}


@mcp.tool(annotations=read("List HACS repositories"))
def list_hacs_repositories(
    category: Annotated[
        str | None,
        Field(
            description=(
                "Filter to one HACS category: integration, plugin, theme, "
                "template, appdaemon or python_script. Omit to list every "
                "category."
            )
        ),
    ] = None,
) -> dict | list:
    """List HACS repositories known to this Home Assistant instance.

    Calls the `hacs/repositories/list` WebSocket command, optionally scoped
    to one `category`.

    Use when: browsing or checking the state of repositories HACS already
    knows about (installed or not).
    Not for: repositories not yet added to HACS at all — use
    `hacs_add_custom_repository` first.
    Returns: list of HACS repository dicts as reported by HACS, or
    `{"error": ..., "hint": ...}` if the WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when the WebSocket command fails,
    e.g. because HACS is not installed.
    """
    kwargs: dict = {}
    if category:
        kwargs["categories"] = [category]
    return _safe_ws("hacs/repositories/list", **kwargs)


@mcp.tool(annotations=read("Get HACS repository details"))
def get_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="HACS repository id, e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Get details about a single HACS repository.

    Calls the `hacs/repository/info` WebSocket command for the given
    `repo_id`.

    Use when: checking one repository's installed/available version or
    metadata before installing or updating it.
    Not for: browsing many repositories at once — use
    `hacs_list_hacs_repositories`.
    Returns: HACS's raw repository-info dict, or `{"error": ..., "hint":
    ...}` if the WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when `repo_id` is unknown or the
    WebSocket command fails.
    """
    return _safe_ws("hacs/repository/info", repository=repo_id)


@mcp.tool(annotations=write("Install a HACS repository", idempotent=True, open_world=True))
def install_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="HACS repository id to install, e.g. from `hacs_list_hacs_repositories`."),
    ],
    version: Annotated[
        str | None,
        Field(description="Specific version/tag to pin. Omit to install the latest available version."),
    ] = None,
) -> dict | list:
    """Install a HACS repository, optionally pinning a version.

    Calls the `hacs/repository/install` WebSocket command, which downloads
    the repository's release from its source (typically GitHub) — an
    outbound fetch beyond this HA instance and its host.

    Use when: adding a HACS-managed integration/plugin/theme that is already
    known to HACS (via the default store or `hacs_add_custom_repository`).
    Not for: adding a repository HACS does not know about yet — use
    `hacs_add_custom_repository` first.
    Returns: HACS's install result, or `{"error": ..., "hint": ...}` if the
    WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when `repo_id`/`version` is
    invalid or the WebSocket command fails.
    Limits: downloads the release over the network; requires a restart of
    the affected component for most categories.
    """
    kwargs: dict = {"repository": repo_id}
    if version:
        kwargs["version"] = version
    return _safe_ws("hacs/repository/install", **kwargs)


@mcp.tool(annotations=destructive("Uninstall a HACS repository", idempotent=True))
def uninstall_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="HACS repository id to uninstall, e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Uninstall a HACS repository.

    Calls the `hacs/repository/uninstall` WebSocket command, which removes
    the installed files for `repo_id`; the integration/plugin/theme stops
    working until reinstalled.

    Use when: removing a HACS-managed component that is no longer needed.
    Not for: only skipping an update while keeping it installed — there is
    no such option exposed here.
    Returns: HACS's uninstall result, or `{"error": ..., "hint": ...}` if
    the WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when `repo_id` is not installed or
    the WebSocket command fails.
    """
    return _safe_ws("hacs/repository/uninstall", repository=repo_id)


@mcp.tool(annotations=destructive("Update a HACS repository", idempotent=False, open_world=True))
def update_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="HACS repository id to update, e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Update an installed HACS repository to its latest available version.

    Calls the `hacs/repository/update` WebSocket command, which downloads
    and installs the newest release over the network; the previous version
    is only recoverable from a backup taken beforehand.

    Use when: `pending_upgrade` is set for the repository (see
    `hacs_list_hacs_critical_updates`) and the new version should be
    applied.
    Not for: pinning a specific version instead of the latest — use
    `hacs_install_hacs_repository` with `version` set.
    Returns: HACS's update result, or `{"error": ..., "hint": ...}` if the
    WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when `repo_id` is not installed or
    the WebSocket command fails.
    Limits: downloads the new release over the network; no automatic
    rollback on failure.
    """
    return _safe_ws("hacs/repository/update", repository=repo_id)


@mcp.tool(annotations=write("Add a custom HACS repository", idempotent=True, open_world=True))
def add_custom_repository(
    url: Annotated[
        str,
        Field(description="Full URL of the repository to register with HACS, e.g. a GitHub repo URL."),
    ],
    category: Annotated[
        str,
        Field(
            description=(
                "HACS category for the repository: integration, plugin, theme, "
                "template, appdaemon or python_script."
            )
        ),
    ],
) -> dict | list:
    """Register a custom repository with HACS so it becomes installable.

    Calls the `hacs/repository/add` WebSocket command with `url` and
    `category`. This only registers the repository — it does not install
    it.

    Use when: a repository is not in HACS's default store and needs adding
    before it can be installed.
    Not for: installing an already-registered repository — use
    `hacs_install_hacs_repository`.
    Returns: HACS's add result, or `{"error": ..., "hint": ...}` if the
    WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when `url`/`category` is invalid
    or the WebSocket command fails.
    """
    return _safe_ws("hacs/repository/add", repository=url, category=category)


@mcp.tool(annotations=read("List HACS pending upgrades"))
def list_hacs_critical_updates() -> dict | list:
    """List installed HACS repositories that have a pending upgrade available.

    Calls the same `hacs/repositories/list` WebSocket command as
    `hacs_list_hacs_repositories` and filters to entries whose
    `pending_upgrade` flag is set, despite this tool's name — HACS itself
    has no separate "critical" severity here.

    Use when: finding what needs `hacs_update_hacs_repository` without
    filtering the full list manually.
    Not for: every repository regardless of upgrade state — use
    `hacs_list_hacs_repositories`.
    Returns: list of repository dicts with `pending_upgrade` truthy, or
    `{"error": ...}` if the underlying call fails or returns an unexpected
    shape.
    Errors: `{"error": str(exception), "hint": ...}` when the WebSocket call
    fails; `{"error": "Unexpected HACS response shape", "raw": result}` when
    it succeeds but does not return a list.
    """
    result = _safe_ws("hacs/repositories/list")
    if isinstance(result, dict) and "error" in result:
        return result
    if not isinstance(result, list):
        return {"error": "Unexpected HACS response shape", "raw": result}
    return [r for r in result if isinstance(r, dict) and r.get("pending_upgrade")]
