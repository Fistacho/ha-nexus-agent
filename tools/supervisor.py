"""Home Assistant Supervisor API — add-on lifecycle, backups, host/core management.

Requires SUPERVISOR_TOKEN env var (auto-set when running as HA add-on).
config.yaml must have `hassio_api: true` and `hassio_role: manager`.
"""
import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("supervisor")

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

@mcp.tool()
def list_addons() -> dict:
    """List all installed add-ons with slug, name, state, version, update_available."""
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


@mcp.tool()
def get_addon(slug: str) -> dict:
    """Get full info for a specific add-on by slug."""
    return _supervisor_request("GET", f"/addons/{slug}/info")


@mcp.tool()
def install_addon(slug: str) -> dict:
    """Install an add-on from its slug (must already be in a registered repository)."""
    return _supervisor_request("POST", f"/addons/{slug}/install")


@mcp.tool()
def uninstall_addon(slug: str, confirm: bool = False) -> dict:
    """Uninstall an add-on. DANGEROUS — requires confirm=True."""
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    return _supervisor_request("POST", f"/addons/{slug}/uninstall")


@mcp.tool()
def start_addon(slug: str) -> dict:
    """Start an installed add-on."""
    return _supervisor_request("POST", f"/addons/{slug}/start")


@mcp.tool()
def stop_addon(slug: str) -> dict:
    """Stop a running add-on."""
    return _supervisor_request("POST", f"/addons/{slug}/stop")


@mcp.tool()
def restart_addon(slug: str) -> dict:
    """Restart an add-on (stop + start)."""
    return _supervisor_request("POST", f"/addons/{slug}/restart")


@mcp.tool()
def update_addon(slug: str) -> dict:
    """Update an add-on to the latest available version."""
    return _supervisor_request("POST", f"/addons/{slug}/update")


@mcp.tool()
def get_addon_logs(slug: str, lines: int = 100) -> dict:
    """Get the last N log lines from an add-on (returns text wrapped in {logs: ...})."""
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


@mcp.tool()
def set_addon_options(slug: str, options: dict) -> dict:
    """Send an add-on's options object (POST as {"options": options}); the Supervisor validates it
    against the add-on schema and stores it as the new options. Read current values via
    supervisor_get_addon, modify, and send the full dict. Takes effect after
    supervisor_restart_addon.
    """
    return _supervisor_request("POST", f"/addons/{slug}/options", json={"options": options})


@mcp.tool()
def get_addon_stats(slug: str) -> dict:
    """Get runtime resource stats (CPU, memory, network, IO) for an add-on."""
    return _supervisor_request("GET", f"/addons/{slug}/stats")


# --- Supervisor self / Core / Host ---

@mcp.tool()
def get_supervisor_info() -> dict:
    """Get info about the Supervisor itself (version, channel, healthy)."""
    return _supervisor_request("GET", "/supervisor/info")


@mcp.tool()
def get_core_info() -> dict:
    """Get info about Home Assistant Core (version, arch, machine)."""
    return _supervisor_request("GET", "/core/info")


@mcp.tool()
def get_host_info() -> dict:
    """Get info about the host OS (HAOS version, hostname, kernel, etc.)."""
    return _supervisor_request("GET", "/host/info")


@mcp.tool()
def restart_core(confirm: bool = False) -> dict:
    """Restart Home Assistant Core. DANGEROUS — requires confirm=True."""
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    return _supervisor_request("POST", "/core/restart")


@mcp.tool()
def restart_host(confirm: bool = False) -> dict:
    """Reboot the whole host: Home Assistant, all add-ons and this nexus add-on go offline for minutes.
    Requires confirm=True.
    """
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    return _supervisor_request("POST", "/host/reboot")


# --- Backups ---

@mcp.tool()
def list_backups() -> dict:
    """List all backups managed by Supervisor."""
    return _supervisor_request("GET", "/backups")


@mcp.tool()
def create_backup(
    name: str,
    addons: list[str] | None = None,
    folders: list[str] | None = None,
    password: str | None = None,
) -> dict:
    """Create a Supervisor backup named `name`: full when neither `addons` nor `folders` is given,
    otherwise partial with those add-on slugs and folders (e.g. 'share', 'ssl', 'media',
    'addons/local'). `password` encrypts it. Full backups can take minutes; on a timeout error check
    supervisor_list_backups before retrying.
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


@mcp.tool()
def restore_backup(slug: str, password: str | None = None, confirm: bool = False) -> dict:
    """Restore a FULL backup (POST /backups/<slug>/restore/full): replaces HA configuration, all add-
    ons with their data, and folders with the backup contents; everything changed since then is
    lost. Core and this nexus add-on restart, so the call may drop before replying. `password` is
    needed for encrypted backups. Without confirm=True it returns {error} and does nothing.
    """
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    payload: dict = {}
    if password:
        payload["password"] = password
    return _supervisor_request("POST", f"/backups/{slug}/restore/full", json=payload)


@mcp.tool()
def delete_backup(slug: str) -> dict:
    """Permanently delete a backup (slug from supervisor_list_backups). No confirmation, no undo."""
    return _supervisor_request("DELETE", f"/backups/{slug}")
