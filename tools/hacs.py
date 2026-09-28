"""HACS (Home Assistant Community Store) integration via WebSocket commands.

Requires HACS to be installed in Home Assistant. WS message names below were
re-verified 2026-09-28 against hacs/integration
`custom_components/hacs/websocket/repository.py` and `repositories.py`
(main branch) after `hacs/repository/install`, `hacs/repository/update` and
`hacs/repository/uninstall` were found to be non-existent commands — HACS
answered `{"code": "unknown_command"}` for all three when called live
against HA 2026.9.3. The real commands:

* `hacs/repository/info`      — field `repository_id` (not `repository`).
* `hacs/repository/download`  — field `repository` + optional `version`;
  this is both "install" (not yet installed) and "update in place"
  (already installed) in HACS's own model — there is no separate command
  for either.
* `hacs/repository/remove`    — field `repository`; uninstalls.
* `hacs/repository/refresh`   — field `repository`; re-fetches repo data
  and always returns `{}` (no version info in the response).
* `hacs/repositories/add`     — plural; fields `repository` (URL) +
  `category`; registers a *custom* repository (distinct from the singular,
  installed-repository commands above).
* `hacs/repositories/list`    — plural; unchanged, already correct.

If HACS changes these again, re-verify against
`/config/custom_components/hacs/websocket/` on a live instance.
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
    `repo_id`, passed as its `repository_id` field.

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
    return _safe_ws("hacs/repository/info", repository_id=repo_id)


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

    Calls the `hacs/repository/download` WebSocket command (HACS has no
    separate "install" command — `download` installs when not yet
    installed, or re-downloads in place when it is), which fetches the
    repository's release from its source (typically GitHub) — an outbound
    fetch beyond this HA instance and its host.

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
    return _safe_ws("hacs/repository/download", **kwargs)


@mcp.tool(annotations=destructive("Uninstall a HACS repository", idempotent=True))
def uninstall_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="HACS repository id to uninstall, e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Uninstall a HACS repository.

    Calls the `hacs/repository/remove` WebSocket command (HACS has no
    separate "uninstall" command), which removes the installed files for
    `repo_id`; the integration/plugin/theme stops working until
    reinstalled.

    Use when: removing a HACS-managed component that is no longer needed.
    Not for: only skipping an update while keeping it installed — there is
    no such option exposed here.
    Returns: HACS's remove result, or `{"error": ..., "hint": ...}` if
    the WebSocket call fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when `repo_id` is not installed or
    the WebSocket command fails.
    """
    return _safe_ws("hacs/repository/remove", repository=repo_id)


@mcp.tool(annotations=destructive("Update a HACS repository", idempotent=False, open_world=True))
def update_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="HACS repository id to update, e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Update an installed HACS repository to its latest available version.

    HACS has no single "update" WebSocket command. This performs the three
    calls HACS's own frontend makes for an update: `hacs/repository/refresh`
    (re-fetches repository data; always returns `{}`, no version info),
    then `hacs/repository/info` (reads the resulting `available_version`),
    then `hacs/repository/download` with that version — which downloads and
    installs the newest release over the network; the previous version is
    only recoverable from a backup taken beforehand.

    Use when: `pending_upgrade` is set for the repository (see
    `hacs_list_hacs_critical_updates`) and the new version should be
    applied.
    Not for: pinning a specific version instead of the latest — use
    `hacs_install_hacs_repository` with `version` set.
    Returns: HACS's download result, or `{"error": ..., "hint": ...}` if
    any of the three WebSocket calls fails.
    Errors: `{"error": str(exception), "hint": "HACS may not be installed,
    or the WS command name has changed"}` when the refresh or info call
    fails (the download call is then never made); `{"error": "HACS did not
    report an available_version for this repository", "raw": ...}` when
    info succeeds but has nothing to update to.
    Limits: downloads the new release over the network; no automatic
    rollback on failure; three sequential WebSocket round-trips instead of
    one.
    """
    refresh_result = _safe_ws("hacs/repository/refresh", repository=repo_id)
    if isinstance(refresh_result, dict) and "error" in refresh_result:
        return refresh_result

    info_result = _safe_ws("hacs/repository/info", repository_id=repo_id)
    if isinstance(info_result, dict) and "error" in info_result:
        return info_result

    available_version = info_result.get("available_version") if isinstance(info_result, dict) else None
    if not available_version:
        return {
            "error": "HACS did not report an available_version for this repository",
            "raw": info_result,
        }

    return _safe_ws("hacs/repository/download", repository=repo_id, version=available_version)


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

    Calls the `hacs/repositories/add` WebSocket command (plural — distinct
    from the singular `hacs/repository/*` commands used elsewhere in this
    namespace) with `url` as its `repository` field and `category`. This
    only registers the repository — it does not install it.

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
    return _safe_ws("hacs/repositories/add", repository=url, category=category)


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
