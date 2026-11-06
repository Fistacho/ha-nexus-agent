"""HACS / Marketplace repositories via WebSocket commands.

From Home Assistant 2026.11 HACS is built into core as the **Marketplace**
(`homeassistant/components/marketplace`). Every `hacs/*` WebSocket command
then answers `{"code": "unknown_command"}` (confirmed live on HA 2026.11.0,
2026-11-06); the commands are `marketplace/*`. Before 2026.11 the
`custom_components/hacs` integration serves `hacs/*`. Tool names and
parameters stay `hacs_*` — they are public API for models.

Backend detection: each tool tries the `marketplace/*` command first and
falls back to `hacs/*` only when HA answers `unknown_command`. An unknown
command is rejected before any handler runs, so the probe has no side
effects. This needs no HA-version parsing (dev/beta versions, custom
builds) and keeps working when nexus outlives a core upgrade (no cached
backend). Cost: one extra round-trip per tool call on HA before 2026.11.

Commands, verified against core tag 2026.11.0
(`homeassistant/components/marketplace/websocket/`) and, for HACS,
hacs/integration `custom_components/hacs/websocket/` (2026-09-28):

| operation | Marketplace (HA >= 2026.11)       | HACS (older HA)              |
|-----------|-----------------------------------|------------------------------|
| list      | `marketplace/repositories/list`   | `hacs/repositories/list`     |
| info      | `marketplace/repository/info`     | `hacs/repository/info`       |
| install   | `marketplace/repository/install`  | `hacs/repository/download`   |
| uninstall | `marketplace/repository/uninstall`| `hacs/repository/remove`     |
| refresh   | `marketplace/repository/refresh`  | `hacs/repository/refresh`    |
| add       | `marketplace/repositories/add`    | `hacs/repositories/add`      |

Field names are the same on both (`categories`, `repository_id`,
`repository`, `version`, `category`). Marketplace-only rules: install and
add answer `warning_not_accepted` until the token's user accepted the
first-run warning in Settings -> Marketplace (per user, nexus never accepts
it; as an add-on nexus is the Supervisor system user, which cannot accept it
in the UI — see `_ADDON_WARNING_HINT`); add also needs a connected GitHub account (`github_not_connected`);
an install that replaces a built-in integration answers `replaces_built_in`
(the confirm flag is not exposed here — install it from the UI).
"""
from __future__ import annotations

import json
from typing import Annotated, Any

import websockets.exceptions
from fastmcp import FastMCP
from pydantic import Field

import auth
import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("hacs")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

MARKETPLACE = "marketplace"
HACS = "hacs"
_BACKENDS: tuple[str, ...] = (MARKETPLACE, HACS)

_COMMANDS: dict[str, dict[str, str]] = {
    "list": {MARKETPLACE: "marketplace/repositories/list", HACS: "hacs/repositories/list"},
    "info": {MARKETPLACE: "marketplace/repository/info", HACS: "hacs/repository/info"},
    "install": {MARKETPLACE: "marketplace/repository/install", HACS: "hacs/repository/download"},
    "uninstall": {MARKETPLACE: "marketplace/repository/uninstall", HACS: "hacs/repository/remove"},
    "refresh": {MARKETPLACE: "marketplace/repository/refresh", HACS: "hacs/repository/refresh"},
    "add": {MARKETPLACE: "marketplace/repositories/add", HACS: "hacs/repositories/add"},
}

_MARKETPLACE_UI = "Settings -> Marketplace (/marketplace) in Home Assistant"

# Hints for the error codes the Marketplace answers with
# (marketplace/websocket/decorators.py, repository.py, repositories.py).
_MARKETPLACE_HINTS: dict[str, str] = {
    "warning_not_accepted": (
        f"Open {_MARKETPLACE_UI}, signed in as the user that owns nexus's access "
        "token, read the first-run warning and accept it. Until then the "
        "Marketplace refuses installs, updates and new custom repositories. "
        "Nexus never accepts the warning for you."
    ),
    "github_not_connected": (
        f"Connect a GitHub account in {_MARKETPLACE_UI} first; adding a custom "
        "repository needs it."
    ),
    "not_loaded": (
        "The Marketplace integration is not loaded. Check Settings -> Devices & "
        "services -> Marketplace (disabled or failed to set up), then retry."
    ),
    "replaces_built_in": (
        f"This repository replaces a built-in integration. Install it from "
        f"{_MARKETPLACE_UI}, where the replacement can be confirmed; this tool "
        "does not confirm it."
    ),
    "repository_in_use": (
        "Delete the integration's config entries first (Settings -> Devices & "
        "services, or `integrations_remove_integration`), then uninstall."
    ),
    "github_rate_limited": (
        f"GitHub rate limit reached. Try again later, or connect a GitHub "
        f"account in {_MARKETPLACE_UI}."
    ),
    "repository_not_found": (
        "Pass the repository's `id` from `hacs_list_hacs_repositories` (a "
        "numeric string), not its name."
    ),
    "repository_busy": "The repository is being installed right now; retry when that finishes.",
    "unauthorized": "Marketplace commands need an admin user; nexus's access token belongs to a non-admin user.",
}

# As an add-on nexus reaches HA through the Supervisor proxy with
# SUPERVISOR_TOKEN, so HA sees the Supervisor system user (supervisor
# homeassistant/api.py:254-279, core http/auth.py:189-210), never the owner.
# The warning acceptance is stored per user (marketplace/base.py:483-485) and
# no one can accept it as that user in the UI, so the standalone hint above
# cannot be followed. What does work: the owner acting in the UI, or an
# automation calling `update.install` — automations run without a user
# (automation/entity.py:486-487) and then any acceptance counts
# (marketplace/update.py:121-127). A script keeps the caller's user, so it
# does not help.
_ADDON_WARNING_HINT = (
    "Nexus runs as an add-on and reaches Home Assistant as the Supervisor "
    "system user, not as you. The Marketplace stores the first-run warning "
    "acceptance per user and that user cannot accept it in the UI, so "
    "installs, updates and new custom repositories through nexus are refused "
    f"even after you accepted it. Do them yourself in {_MARKETPLACE_UI} "
    "(updates also under Settings -> Updates). An installed repository can "
    "also be updated by an automation whose action is `update.install` on its "
    "`update.*` entity: automations run without a user, so your acceptance "
    "counts. Nexus prepares or runs such an automation only when the owner "
    "explicitly asks for it, never on its own after a `warning_not_accepted` "
    "answer. Nexus never accepts the warning for you."
)

_HACS_HINT = "HACS may not be installed, or the WS command name has changed"

# What `ha_client` raises when HA gives no usable answer: its own
# RuntimeErrors (handshake/auth/greeting), OSError (refused, DNS,
# TimeoutError), websockets' protocol/closed errors and a bad JSON frame.
# Anything else (KeyError, TypeError, ...) is a bug and is not masked as
# "No answer". `HAWebSocketError` (a RuntimeError) is handled before this.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    RuntimeError,
    OSError,
    websockets.exceptions.WebSocketException,
    json.JSONDecodeError,
)

# Install/update answer only once the download is written and the integration
# reloaded; a Marketplace download may be up to 100 MB (marketplace/const.py:36).
# `ha_client._ws_call`'s 10 s bound (`_WS_HANDSHAKE_TIMEOUT`) would routinely
# report "No answer" while HA keeps installing, so these wait longer. Uninstall
# too: with GitHub connected the Marketplace first re-fetches the repository
# (marketplace/websocket/repository.py:372-379). Every other command keeps
# the default.
_INSTALL_TIMEOUT = 120.0

_NOT_AVAILABLE = {
    "error": (
        "Neither the Marketplace (Home Assistant 2026.11 and later) nor HACS "
        "answers on this instance"
    ),
    "code": "unknown_command",
    "hint": (
        "On Home Assistant 2026.11+ the built-in Marketplace replaces HACS; "
        "check that its integration is set up (Settings -> Devices & services). "
        "On older versions install HACS."
    ),
}


_NO_ANSWER_HINT = (
    "Home Assistant gave no answer (timeout or connection error), so the "
    "command may still be running or may already have finished there. Check "
    "the repository's state with `hacs_get_hacs_repository` (`installed`, "
    "`installed_version`) before retrying."
)


def _is_error(result: Any) -> bool:
    return isinstance(result, dict) and "error" in result


def _error_result(backend: str | None, exc: Exception) -> dict:
    """Internal: a failed call as `{"error", "code", "backend", "hint"}`.

    Only an `HAWebSocketError` is an answer from HA (a real rejection by
    that backend). Anything else (timeout, refused connection) means HA
    never answered: no code, no claim of rejection, and `backend` only when
    the caller had already established it.
    """
    if not isinstance(exc, ha.HAWebSocketError):
        return {
            "error": f"No answer from Home Assistant: {exc}",
            "code": None,
            "backend": backend,
            "hint": _NO_ANSWER_HINT,
        }
    code = exc.code
    if backend == HACS:
        return {"error": str(exc), "code": code, "backend": HACS, "hint": _HACS_HINT}
    message = exc.message or str(exc)
    if code == "warning_not_accepted" and auth.is_addon_mode():
        hint = _ADDON_WARNING_HINT
    else:
        hint = _MARKETPLACE_HINTS.get(code or "") or (
            f"The Marketplace rejected the command; see `error`. Details are in {_MARKETPLACE_UI}."
        )
    return {"error": f"Marketplace: {message}", "code": code, "backend": MARKETPLACE, "hint": hint}


def _call(
    op: str,
    backends: tuple[str, ...] = _BACKENDS,
    *,
    ws_timeout: float | None = None,
    **kwargs,
) -> tuple[str | None, Any]:
    """Internal: run `op` on the first backend that knows the command.

    Returns `(backend, result)`; on failure `result` is an error dict and
    `backend` is the backend that answered, or None when no backend knows
    the command or HA did not answer before the backend was established
    (a single entry in `backends` means the caller already established it).
    Only `unknown_command` moves on to the next backend — after a timeout
    the command may still be running, so it is never re-sent elsewhere.
    `ws_timeout` replaces `ha_client`'s default bound for this call.
    """
    pinned = len(backends) == 1
    for backend in backends:
        try:
            command = _COMMANDS[op][backend]
            if ws_timeout is None:
                return backend, ha._ws_call(command, **kwargs)
            return backend, ha._ws_call_timeout(command, ws_timeout, **kwargs)
        except ha.HAWebSocketError as e:
            if e.code == "unknown_command":
                continue
            return backend, _error_result(backend, e)
        except _TRANSPORT_ERRORS as e:
            known = backend if pinned else None
            return known, _error_result(known, e)
    return None, dict(_NOT_AVAILABLE)


def _write_result(backend: str | None, result: Any) -> dict | list:
    """Internal: give a successful write a JSON object, the same on both backends.

    The Marketplace answers install/uninstall/refresh/add with `result: null`
    and HACS with `{}`. A bare `None` fails FastMCP's output validation for
    `-> dict | list` ("Invalid structured content") after HA already did the
    work, so success is reported as `{"status": "ok", "backend": ...}`; a
    non-empty payload, should HA ever send one, is kept under `result`.
    """
    if backend is None or _is_error(result):
        return result
    ok: dict = {"status": "ok", "backend": backend}
    if result not in (None, {}):
        ok["result"] = result
    return ok


@mcp.tool(annotations=read("List HACS repositories"))
def list_hacs_repositories(
    category: Annotated[
        str | None,
        Field(
            description=(
                "Filter to one category: integration, plugin, theme or "
                "template (HACS before HA 2026.11 also has appdaemon and "
                "python_script). Omit to list every category."
            )
        ),
    ] = None,
) -> dict | list:
    """List the HACS / Marketplace repositories known to this Home Assistant instance.

    On HA 2026.11+ calls the built-in Marketplace's
    `marketplace/repositories/list`; on older HA falls back to HACS's
    `hacs/repositories/list`. Optionally scoped to one `category`.

    Use when: browsing or checking the state of repositories already known
    (installed or not); `id` from here is the `repo_id` other tools take.
    Not for: repositories not added yet — use `hacs_add_custom_repository`
    first.
    Returns: list of repository dicts as reported by HA (`id`, `full_name`,
    `category`, `installed`, `installed_version`, `available_version`,
    `pending_upgrade`, ...), or an error dict.
    Errors: `{"error", "code", "backend", "hint"}` when the command fails;
    `code` `unknown_command` with a hint when neither the Marketplace nor
    HACS is available.
    """
    kwargs: dict = {}
    if category:
        kwargs["categories"] = [category]
    return _call("list", **kwargs)[1]


@mcp.tool(annotations=read("Get HACS repository details"))
def get_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="Repository id (the `id` field), e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Get details about a single HACS / Marketplace repository.

    Calls `marketplace/repository/info` on HA 2026.11+ or
    `hacs/repository/info` on older HA, passing `repo_id` as the
    `repository_id` field.

    Use when: checking one repository's installed/available version or
    metadata before installing or updating it.
    Not for: browsing many repositories at once — use
    `hacs_list_hacs_repositories`.
    Returns: HA's raw repository-info dict, or an error dict.
    Errors: `{"error", "code", "backend", "hint"}` when `repo_id` is unknown
    (`repository_not_found`) or the command fails; `code` `unknown_command`
    when neither the Marketplace nor HACS is available.
    """
    return _call("info", repository_id=repo_id)[1]


@mcp.tool(annotations=write("Install a HACS repository", idempotent=True, open_world=True))
def install_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="Repository id to install (the `id` field), e.g. from `hacs_list_hacs_repositories`."),
    ],
    version: Annotated[
        str | None,
        Field(description="Specific version/tag to pin. Omit to install the latest available version."),
    ] = None,
) -> dict | list:
    """Install a HACS / Marketplace repository, optionally pinning a version.

    Calls `marketplace/repository/install` on HA 2026.11+ or
    `hacs/repository/download` on older HA (installs, or re-downloads in
    place when already installed). Fetches the release from its source
    (typically GitHub) — an outbound fetch beyond this HA instance.

    Use when: adding an integration/plugin/theme that is already known (via
    the default store or `hacs_add_custom_repository`).
    Not for: a repository not known yet — use `hacs_add_custom_repository`
    first.
    Returns: `{"status": "ok", "backend": "marketplace"|"hacs"}`, or an
    error dict.
    Errors: `{"error", "code", "backend", "hint"}`; on the Marketplace
    `warning_not_accepted` until the first-run warning is accepted in
    Settings -> Marketplace (nexus never accepts it; as an add-on see
    `hint`), `replaces_built_in`
    for a repository that replaces a built-in integration (install it from
    the UI), `github_rate_limited`; `unknown_command` when neither backend
    is available; `code` null on a timeout (check with
    `hacs_get_hacs_repository` before retrying).
    Limits: waits up to 120 s for HA's answer (an MCP client with a shorter
    timeout may give up first — then check with `hacs_get_hacs_repository`
    before retrying); most categories need a restart of the component.
    """
    kwargs: dict = {"repository": repo_id}
    if version:
        kwargs["version"] = version
    return _write_result(*_call("install", ws_timeout=_INSTALL_TIMEOUT, **kwargs))


@mcp.tool(annotations=destructive("Uninstall a HACS repository", idempotent=True))
def uninstall_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="Repository id to uninstall (the `id` field), e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Uninstall a HACS / Marketplace repository.

    Calls `marketplace/repository/uninstall` on HA 2026.11+ or
    `hacs/repository/remove` on older HA, which removes the installed files
    for `repo_id`; the integration/plugin/theme stops working until
    reinstalled.

    Use when: removing a component that is no longer needed.
    Not for: only skipping an update while keeping it installed — there is
    no such option exposed here.
    Returns: `{"status": "ok", "backend": "marketplace"|"hacs"}`, or an
    error dict.
    Errors: `{"error", "code", "backend", "hint"}` when `repo_id` is not
    installed or the command fails; the Marketplace refuses an integration
    that still has config entries (`repository_in_use` — delete them first);
    `unknown_command` when neither backend is available.
    Limits: waits up to 120 s (with GitHub connected the Marketplace first
    re-fetches the repository); an MCP client with a shorter timeout may give
    up first — then check with `hacs_get_hacs_repository` before retrying.
    """
    return _write_result(*_call("uninstall", ws_timeout=_INSTALL_TIMEOUT, repository=repo_id))


@mcp.tool(annotations=destructive("Update a HACS repository", idempotent=False, open_world=True))
def update_hacs_repository(
    repo_id: Annotated[
        str,
        Field(description="Repository id to update (the `id` field), e.g. from `hacs_list_hacs_repositories`."),
    ],
) -> dict | list:
    """Update an installed HACS / Marketplace repository to its latest available version.

    Neither backend has a single update command. Runs `repository/refresh`
    (re-fetch), `repository/info` (read `available_version`), then installs
    that version (`marketplace/repository/install` on HA 2026.11+,
    `hacs/repository/download` on older HA). The previous version is only
    recoverable from a backup taken beforehand.

    Use when: `pending_upgrade` is set (see `hacs_list_hacs_critical_updates`)
    and the new version should be applied.
    Not for: pinning a specific version — use `hacs_install_hacs_repository`
    with `version` set.
    Returns: `{"status": "ok", "backend": "marketplace"|"hacs"}`, or an
    error dict.
    Errors: `{"error", "code", "backend", "hint"}` from the first failing
    call (later calls are not made); on the Marketplace
    `warning_not_accepted` until the first-run warning is accepted in
    Settings -> Marketplace (as an add-on see `hint`); `{"error": "No available_version reported for
    this repository", "raw": ...}` when info has nothing to update to;
    `code` null on a timeout (check with `hacs_get_hacs_repository` before
    retrying).
    Limits: downloads over the network; the install step waits up to 120 s
    (an MCP client with a shorter timeout may give up first — then check with
    `hacs_get_hacs_repository`); no automatic rollback.
    """
    backend, refresh_result = _call("refresh", repository=repo_id)
    if backend is None or _is_error(refresh_result):
        return refresh_result

    _, info_result = _call("info", backends=(backend,), repository_id=repo_id)
    if _is_error(info_result):
        return info_result

    available_version = info_result.get("available_version") if isinstance(info_result, dict) else None
    if not available_version:
        return {"error": "No available_version reported for this repository", "raw": info_result}

    return _write_result(
        *_call(
            "install",
            backends=(backend,),
            ws_timeout=_INSTALL_TIMEOUT,
            repository=repo_id,
            version=available_version,
        )
    )


@mcp.tool(annotations=write("Add a custom HACS repository", idempotent=True, open_world=True))
def add_custom_repository(
    url: Annotated[
        str,
        Field(description="Full URL of the repository to register, e.g. a GitHub repo URL."),
    ],
    category: Annotated[
        str,
        Field(
            description=(
                "Category for the repository: integration, plugin, theme or "
                "template (HACS before HA 2026.11 also accepts appdaemon and "
                "python_script)."
            )
        ),
    ],
) -> dict | list:
    """Register a custom repository with HACS / the Marketplace so it becomes installable.

    Calls `marketplace/repositories/add` on HA 2026.11+ or
    `hacs/repositories/add` on older HA, with `url` as its `repository`
    field and `category`. This only registers the repository — it does not
    install it.

    Use when: a repository is not in the default store and needs adding
    before it can be installed.
    Not for: installing an already-registered repository — use
    `hacs_install_hacs_repository`.
    Returns: `{"status": "ok", "backend": "marketplace"|"hacs"}`, or an
    error dict.
    Errors: `{"error", "code", "backend", "hint"}` when `url`/`category` is
    invalid or the command fails; the Marketplace needs the first-run
    warning accepted (`warning_not_accepted`) and a connected GitHub account
    (`github_not_connected`), both in Settings -> Marketplace (as an add-on
    see `hint`);
    `unknown_command` when neither backend is available.
    """
    return _write_result(*_call("add", repository=url, category=category))


@mcp.tool(annotations=read("List HACS pending upgrades"))
def list_hacs_critical_updates() -> dict | list:
    """List installed HACS / Marketplace repositories that have a pending upgrade available.

    Calls the same list command as `hacs_list_hacs_repositories`
    (`marketplace/repositories/list` on HA 2026.11+,
    `hacs/repositories/list` on older HA) and keeps entries whose
    `pending_upgrade` flag is set, despite this tool's name — there is no
    separate "critical" severity here.

    Use when: finding what needs `hacs_update_hacs_repository` without
    filtering the full list manually.
    Not for: every repository regardless of upgrade state — use
    `hacs_list_hacs_repositories`.
    Returns: list of repository dicts with `pending_upgrade` truthy, or an
    error dict.
    Errors: `{"error", "code", "backend", "hint"}` when the command fails;
    `{"error": "Unexpected response shape", "raw": result}` when it does not
    return a list.
    """
    _, result = _call("list")
    if _is_error(result):
        return result
    if not isinstance(result, list):
        return {"error": "Unexpected response shape", "raw": result}
    return [r for r in result if isinstance(r, dict) and r.get("pending_upgrade")]
