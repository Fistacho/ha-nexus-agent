"""Home Assistant Supervisor API — add-on lifecycle, backups, host/core management.

Requires SUPERVISOR_TOKEN env var (auto-set when running as HA add-on).
config.yaml must have `hassio_api: true` and `hassio_role: manager`.
"""
from __future__ import annotations

import os
from typing import Annotated

import httpx
from fastmcp import FastMCP
from pydantic import Field

from tools._contract import destructive, read, write

mcp = FastMCP("supervisor")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_BASE_URL = "http://supervisor"


def _supervisor_request(method: str, path: str, json: dict | None = None) -> dict:
    """Internal: call Supervisor REST API with bearer token from env."""
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return {"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA add-on for Supervisor API"}
    try:
        with httpx.Client(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as c:
            if method.upper() == "GET":
                r = c.request(method, path)
            else:
                r = c.request(method, path, json=json or {})
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text}
    except Exception as e:
        return {"error": str(e)}


def _supervisor_get_text(path: str) -> str:
    """Internal: GET a text endpoint (e.g. logs) instead of JSON."""
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return ""
    with httpx.Client(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as c:
        r = c.get(path)
        r.raise_for_status()
        return r.text


# --- Add-on lifecycle ---

@mcp.tool(annotations=read("List installed add-ons"))
def list_addons() -> dict:
    """List every installed add-on with its slug, name, state and version.

    Calls Supervisor's `GET /addons` and projects each entry down to slug,
    name, state, version, `version_latest` and `update_available`.

    Use when: getting an overview of installed add-ons and which have
    updates pending.
    Not for: full add-on detail (options, ports, boot mode) — use
    `supervisor_get_addon`.
    Returns: `{"addons": [{slug, name, state, version, version_latest,
    update_available}, ...]}`.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` on a Supervisor API error.
    """
    resp = _supervisor_request("GET", "/addons")
    if "error" in resp:
        return resp
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    addons = data.get("addons", []) if isinstance(data, dict) else []
    return {
        "addons": [
            {
                "slug": a.get("slug"),
                "name": a.get("name"),
                "state": a.get("state"),
                "version": a.get("version"),
                "version_latest": a.get("version_latest"),
                "update_available": a.get("update_available", False),
            }
            for a in addons
        ]
    }


@mcp.tool(annotations=read("Get add-on details"))
def get_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Get full Supervisor info for one add-on.

    Calls `GET /addons/<slug>/info`, returning every field Supervisor stores
    for that add-on (options schema, current options, ports, boot mode,
    version, state and more).

    Use when: the full add-on record is needed, e.g. before editing its
    options.
    Not for: a quick overview across all add-ons — use
    `supervisor_list_addons`.
    Returns: Supervisor's raw add-on info payload.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug does not exist.
    """
    return _supervisor_request("GET", f"/addons/{slug}/info")


@mcp.tool(annotations=write("Install an add-on", idempotent=True, open_world=True))
def install_addon(
    slug: Annotated[
        str,
        Field(
            description=(
                "Slug of an add-on already visible in a registered repository "
                "(e.g. from `hacs_list_hacs_repositories`-style store listings "
                "or Supervisor's own add-on store)."
            )
        ),
    ],
) -> dict:
    """Install an add-on from its slug.

    Calls `POST /addons/<slug>/install`. The add-on must already belong to a
    repository Supervisor knows about; this does not add repositories.
    Installing downloads the add-on's image, which reaches outside the HA
    instance and its host.

    Use when: adding a new add-on that is already visible in an installed
    repository.
    Not for: starting/stopping an already-installed add-on — use
    `supervisor_start_addon`/`supervisor_stop_addon`.
    Returns: Supervisor's install-job result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown.
    Limits: downloads the add-on image over the network; can take a while
    on a slow connection.
    """
    return _supervisor_request("POST", f"/addons/{slug}/install")


@mcp.tool(annotations=destructive("Uninstall an add-on", idempotent=True))
def uninstall_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to uninstall, e.g. from `supervisor_list_addons`."),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually uninstall the add-on and its data. "
                "False (default) performs no action."
            )
        ),
    ] = False,
) -> dict:
    """Uninstall an add-on and its persistent data after an explicit confirmation.

    Calls `POST /addons/<slug>/uninstall`. Without `confirm=True` nothing is
    removed; the call only returns an error asking for confirmation.

    Use when: permanently removing an add-on that is no longer needed.
    Not for: temporarily stopping it while keeping its data and config — use
    `supervisor_stop_addon`.
    Returns: Supervisor's uninstall-job result when confirmed.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "HTTP <status>",
    "detail": ...}` on a Supervisor API error.
    Limits: WARNING: deletes the add-on's data with no separate backup step;
    requires `confirm=True`.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                f"This will uninstall add-on '{slug}' and delete its persistent data. "
                "Call again with confirm=True to proceed."
            ),
            "action": f"uninstall_addon(slug={slug!r}, confirm=True)",
        }
    return _supervisor_request("POST", f"/addons/{slug}/uninstall")


@mcp.tool(annotations=write("Start an add-on", idempotent=True))
def start_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to start, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Start an installed, stopped add-on.

    Calls `POST /addons/<slug>/start`.

    Use when: bringing an installed add-on online after it was stopped.
    Not for: installing a new add-on — use `supervisor_install_addon`; for
    stopping it — use `supervisor_stop_addon`.
    Returns: Supervisor's start-job result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown or
    already running.
    """
    return _supervisor_request("POST", f"/addons/{slug}/start")


@mcp.tool(annotations=destructive("Stop an add-on", idempotent=True))
def stop_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to stop, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Stop a running add-on.

    Calls `POST /addons/<slug>/stop`. The add-on — which could be this nexus
    add-on itself, or e.g. an MQTT broker other integrations depend on —
    becomes unavailable until started again.

    Use when: taking one add-on offline without uninstalling it.
    Not for: removing it entirely — use `supervisor_uninstall_addon`; for
    bringing it back — use `supervisor_start_addon`.
    Returns: Supervisor's stop-job result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown.
    Limits: interrupts the add-on's availability, and anything depending on
    it, until it is started again.
    """
    return _supervisor_request("POST", f"/addons/{slug}/stop")


@mcp.tool(annotations=destructive("Restart an add-on", idempotent=True))
def restart_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to restart, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Restart an add-on (stop, then start).

    Calls `POST /addons/<slug>/restart`. The add-on — which could be this
    nexus add-on itself — is briefly unavailable during the restart.

    Use when: an add-on needs to pick up new options or recover from a
    stuck state.
    Not for: a one-way stop — use `supervisor_stop_addon`; for restarting
    Core instead — use `supervisor_restart_core`.
    Returns: Supervisor's restart-job result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug is unknown.
    Limits: briefly interrupts the add-on's availability.
    """
    return _supervisor_request("POST", f"/addons/{slug}/restart")


@mcp.tool(annotations=destructive("Update an add-on", idempotent=False, open_world=True))
def update_addon(
    slug: Annotated[
        str,
        Field(description="Add-on slug to update, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Update an add-on to its latest available version.

    Calls `POST /addons/<slug>/update`, which downloads and installs the new
    version over the network; the previous version is only recoverable from
    a backup taken beforehand.

    Use when: `update_available` is true for the add-on and the new version
    should be applied.
    Not for: reverting an update — restore a backup via
    `supervisor_restore_backup` instead; there is no dedicated rollback
    call.
    Returns: Supervisor's update-job result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` on a failed update.
    Limits: downloads the new version over the network; no automatic
    rollback on failure.
    """
    return _supervisor_request("POST", f"/addons/{slug}/update")


@mcp.tool(annotations=read("Get add-on log lines"))
def get_addon_logs(
    slug: Annotated[
        str,
        Field(description="Add-on slug to fetch logs for, e.g. from `supervisor_list_addons`."),
    ],
    lines: Annotated[
        int,
        Field(
            description=(
                "Maximum number of most-recent log lines to return. 100 by "
                "default; 0 or negative returns the full fetched log text."
            )
        ),
    ] = 100,
) -> dict:
    """Get the most recent log lines from one add-on.

    Calls `GET /addons/<slug>/logs`, which returns plain text, and keeps
    only the last `lines` lines.

    Use when: debugging one add-on's runtime behaviour.
    Not for: the ESPHome add-on's per-device compile/upload logs — use
    `esphome_get_addon_logs` if that distinction matters to the caller.
    Returns: `{"logs": "<joined log lines>"}`.
    Errors: `{"error": "SUPERVISOR_TOKEN not set or empty response"}` when
    the token is missing; `{"error": "HTTP <status>", "detail": ...}` on a
    Supervisor API error.
    """
    try:
        text = _supervisor_get_text(f"/addons/{slug}/logs")
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detail": e.response.text}
    except Exception as e:
        return {"error": str(e)}
    if not text:
        return {"error": "SUPERVISOR_TOKEN not set or empty response"}
    log_lines = text.splitlines()
    if lines > 0:
        log_lines = log_lines[-lines:]
    return {"logs": "\n".join(log_lines)}


@mcp.tool(annotations=destructive("Set add-on options", idempotent=True))
def set_addon_options(
    slug: Annotated[
        str,
        Field(description="Add-on slug whose options to set, e.g. from `supervisor_list_addons`."),
    ],
    options: Annotated[
        dict,
        Field(
            description=(
                "Full options object to store, validated by Supervisor against "
                "the add-on's schema. Any field read via `supervisor_get_addon` "
                "and left out here is not preserved automatically — include the "
                "whole dict, not just changed keys."
            )
        ),
    ],
) -> dict:
    """Replace an add-on's stored options with the given object.

    Calls `POST /addons/<slug>/options` as `{"options": options}`. Read
    current values via `supervisor_get_addon`, modify them, and send the
    full dict back — this overwrites the previous options entirely, so
    fields omitted here are lost. Takes effect after
    `supervisor_restart_addon`.

    Use when: changing an add-on's configuration programmatically instead
    of through the HA UI.
    Not for: reading the current options first — use
    `supervisor_get_addon`.
    Returns: Supervisor's options-update result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if `options` fails schema
    validation.
    Limits: does not apply until the add-on is restarted.
    """
    return _supervisor_request("POST", f"/addons/{slug}/options", json={"options": options})


@mcp.tool(annotations=read("Get add-on resource stats"))
def get_addon_stats(
    slug: Annotated[
        str,
        Field(description="Add-on slug to fetch stats for, e.g. from `supervisor_list_addons`."),
    ],
) -> dict:
    """Get an add-on's current runtime resource usage.

    Calls `GET /addons/<slug>/stats` for CPU, memory, network and disk I/O
    figures at the moment of the call.

    Use when: checking whether an add-on is consuming unexpected resources.
    Not for: historical usage over time — this returns only a snapshot.
    Returns: Supervisor's raw stats payload (CPU percent, memory
    usage/limit, network and block I/O counters).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the add-on is not
    running.
    """
    return _supervisor_request("GET", f"/addons/{slug}/stats")


# --- Supervisor self / Core / Host ---

@mcp.tool(annotations=read("Get Supervisor info"))
def get_supervisor_info() -> dict:
    """Get info about the Supervisor component itself.

    Calls `GET /supervisor/info` for its version, update channel and health
    flag.

    Use when: checking the Supervisor's own version/channel/health, as
    opposed to Core or the host.
    Not for: Home Assistant Core details — use `supervisor_get_core_info`;
    for the host OS — use `supervisor_get_host_info`.
    Returns: Supervisor's raw info payload (version, channel, `healthy`,
    and related fields).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/supervisor/info")


@mcp.tool(annotations=read("Get HA Core info"))
def get_core_info() -> dict:
    """Get info about the Home Assistant Core container.

    Calls `GET /core/info` for its version, CPU architecture and machine
    type.

    Use when: checking Core's version/arch, as opposed to the Supervisor or
    host.
    Not for: HA's own location/units/component config — use
    `history_get_ha_config`.
    Returns: Supervisor's raw Core info payload (version, arch, machine).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/core/info")


@mcp.tool(annotations=read("Get host OS info"))
def get_host_info() -> dict:
    """Get info about the host operating system.

    Calls `GET /host/info` for Home Assistant OS version, hostname, kernel and related
    host-level fields.

    Use when: checking the underlying OS/hardware, as opposed to Core or the
    Supervisor.
    Not for: Supervisor's own version — use
    `supervisor_get_supervisor_info`.
    Returns: Supervisor's raw host info payload (Home Assistant OS version, hostname,
    kernel, and related fields).
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/host/info")


@mcp.tool(annotations=destructive("Restart HA Core", idempotent=True))
def restart_core(
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually restart Core. False (default) performs "
                "no action."
            )
        ),
    ] = False,
) -> dict:
    """Restart Home Assistant Core through the Supervisor after an explicit confirmation.

    Calls `POST /core/restart`. WARNING: causes downtime for HA Core (and
    everything depending on it) until it comes back up. Without
    `confirm=True` nothing is restarted.

    Use when: Core needs restarting on a Supervisor install specifically.
    Not for: the equivalent call via HA's own service — use
    `system_restart_ha`; for rebooting the whole host — use
    `supervisor_restart_host`.
    Returns: Supervisor's restart-job result when confirmed.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "SUPERVISOR_TOKEN
    not set — Nexus must run as HA add-on for Supervisor API"}` when the
    token env var is missing.
    Limits: requires `confirm=True`; interrupts Core until it restarts.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                "This will restart Home Assistant Core via Supervisor (brief downtime). "
                "Call again with confirm=True to proceed."
            ),
            "action": "restart_core(confirm=True)",
        }
    return _supervisor_request("POST", "/core/restart")


@mcp.tool(annotations=destructive("Reboot the host", idempotent=True))
def restart_host(
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually reboot the host. False (default) performs "
                "no action."
            )
        ),
    ] = False,
) -> dict:
    """Reboot the whole host machine after an explicit confirmation.

    Calls `POST /host/reboot`. WARNING: Home Assistant, every add-on
    (including this nexus add-on) and the host OS go offline for minutes.
    Without `confirm=True` nothing is rebooted.

    Use when: a host-level change (e.g. a Home Assistant OS update) requires a full reboot.
    Not for: restarting only Core — use `supervisor_restart_core`; for
    restarting one add-on — use `supervisor_restart_addon`.
    Returns: Supervisor's reboot-job result when confirmed.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "SUPERVISOR_TOKEN
    not set — Nexus must run as HA add-on for Supervisor API"}` when the
    token env var is missing.
    Limits: requires `confirm=True`; takes the whole host, including this
    add-on, offline for minutes — the call itself may not receive a reply.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                "This will reboot the whole host machine, taking Home Assistant, every "
                "add-on and the host OS offline for minutes. Call again with confirm=True "
                "to proceed."
            ),
            "action": "restart_host(confirm=True)",
        }
    return _supervisor_request("POST", "/host/reboot")


# --- Backups ---

@mcp.tool(annotations=read("List Supervisor backups"))
def list_backups() -> dict:
    """List every backup Supervisor manages.

    Calls `GET /backups` for the full backup catalogue Supervisor tracks.

    Use when: finding a backup's slug before restoring or deleting it, or
    checking whether a scheduled backup succeeded.
    Not for: HA Core's own `backup.create`/`backup.create_automatic` backups
    when they were not made through Supervisor — those may not appear here
    under the same view.
    Returns: Supervisor's raw backups payload.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing.
    """
    return _supervisor_request("GET", "/backups")


@mcp.tool(annotations=write("Create a Supervisor backup", idempotent=False))
def create_backup(
    name: Annotated[
        str,
        Field(description="Human-readable name to store on the new backup."),
    ],
    addons: Annotated[
        list[str] | None,
        Field(
            description=(
                "Add-on slugs to include for a partial backup. Omit together "
                "with `folders` for a full backup of everything."
            )
        ),
    ] = None,
    folders: Annotated[
        list[str] | None,
        Field(
            description=(
                "Folder names to include for a partial backup, e.g. 'share', "
                "'ssl', 'media', 'addons/local'. Omit together with `addons` "
                "for a full backup of everything."
            )
        ),
    ] = None,
    password: Annotated[
        str | None,
        Field(
            description=(
                "Password to encrypt the backup with. Omit for an unencrypted "
                "backup."
            )
        ),
    ] = None,
) -> dict:
    """Create a new Supervisor backup, full or partial.

    Calls `POST /backups/new/full` when neither `addons` nor `folders` is
    given, otherwise `POST /backups/new/partial` with those slugs/folders.
    Unlike `system_create_backup`, this never deletes older backups — it
    only adds a new one.

    Use when: a named, partial and/or password-protected backup is needed on
    a Supervisor install.
    Not for: a quick backup using the instance's own automatic-backup
    settings (which may prune old ones) — use `system_create_backup`.
    Returns: Supervisor's backup-job result, including the new backup's
    slug on success.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` on a timeout or failure —
    check `supervisor_list_backups` before retrying.
    Limits: a full backup can take minutes; on a timeout error, check
    `supervisor_list_backups` before retrying rather than assuming failure.
    """
    payload: dict = {"name": name}
    if password:
        payload["password"] = password
    if addons is not None or folders is not None:
        if addons is not None:
            payload["addons"] = addons
        if folders is not None:
            payload["folders"] = folders
        return _supervisor_request("POST", "/backups/new/partial", json=payload)
    return _supervisor_request("POST", "/backups/new/full", json=payload)


@mcp.tool(annotations=destructive("Restore a full backup", idempotent=True))
def restore_backup(
    slug: Annotated[
        str,
        Field(description="Backup slug to restore, from `supervisor_list_backups`."),
    ],
    password: Annotated[
        str | None,
        Field(description="Password for an encrypted backup. Omit for an unencrypted one."),
    ] = None,
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually restore the backup. False (default) "
                "performs no action."
            )
        ),
    ] = False,
) -> dict:
    """Restore a full backup after an explicit confirmation.

    Calls `POST /backups/<slug>/restore/full`, which replaces the HA
    configuration, every add-on with its data, and the backed-up folders
    with the backup's contents; anything changed since the backup was taken
    is lost. Core and this nexus add-on restart as part of the restore, so
    the call may drop the connection before it can reply. Without
    `confirm=True` nothing is restored.

    Use when: reverting to a known-good state after a failed update or
    misconfiguration.
    Not for: restoring only some add-ons/folders — Supervisor's partial
    restore is not exposed by this tool.
    Returns: Supervisor's restore-job result when confirmed; the reply may
    never arrive because the add-on restarts mid-call.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; `{"error": "HTTP <status>",
    "detail": ...}` if the slug or password is wrong.
    Limits: WARNING: discards every change made since the backup; requires
    `confirm=True`.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": (
                f"This will restore backup '{slug}', discarding every change made since it "
                "was taken. Call again with confirm=True to proceed."
            ),
            "action": f"restore_backup(slug={slug!r}, confirm=True)",
        }
    payload: dict = {}
    if password:
        payload["password"] = password
    return _supervisor_request("POST", f"/backups/{slug}/restore/full", json=payload)


@mcp.tool(annotations=destructive("Delete a Supervisor backup", idempotent=True))
def delete_backup(
    slug: Annotated[
        str,
        Field(description="Backup slug to delete, from `supervisor_list_backups`."),
    ],
) -> dict:
    """Permanently delete one backup.

    Calls `DELETE /backups/<slug>`. Unlike most other destructive tools in
    this module, this call has no `confirm` parameter — there is no
    confirmation step and no undo once the backup file is gone.

    Use when: cleaning up a backup that is confirmed to be no longer
    needed.
    Not for: any situation where the backup might still be needed — there
    is no recovery after this call.
    Returns: Supervisor's delete result.
    Errors: `{"error": "SUPERVISOR_TOKEN not set — Nexus must run as HA
    add-on for Supervisor API"}` when the token env var is missing;
    `{"error": "HTTP <status>", "detail": ...}` if the slug does not exist.
    Limits: WARNING: no `confirm` parameter and no undo — the backup is gone
    immediately.
    """
    return _supervisor_request("DELETE", f"/backups/{slug}")
