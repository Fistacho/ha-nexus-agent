"""Integration setup via config_flow.

Mirrors the HA UI flow: pick a domain, start a flow, walk through steps,
and finalize. Also exposes options-flow for editing existing entries.

Config/options flow and config-entry removal are REST-only in Home
Assistant — there is no WebSocket equivalent (confirmed against
home-assistant/core: `helpers/data_entry_flow.py`
(`FlowManagerIndexView`/`FlowManagerResourceView`) and
`components/config/config_entries.py`). `config_entries/get`,
`config_entries/update`, `config_entries/disable` and
`config_entries/flow/progress` remain WebSocket commands and are used as such.
"""
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("integrations")


@mcp.tool()
def list_flow_handlers() -> list[str]:
    """List all integration domains that support config_flow (i.e. can be installed via UI)."""
    with ha._client() as c:
        r = c.get("/api/config/config_entries/flow_handlers")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def list_flows_in_progress() -> list[dict]:
    """List config flows currently in progress (started but not finished)."""
    return ha._ws_call("config_entries/flow/progress")


@mcp.tool()
def start_config_flow(handler: str, show_advanced_options: bool = False) -> dict:
    """Start a new config flow for the given integration domain.

    Returns the first step (form, schema, errors). Pass `flow_id` from the
    result into `submit_config_flow_step` to provide user input.

    Uses REST `POST /api/config/config_entries/flow` — HA has no WebSocket
    command for this.

    Example: start_config_flow("shelly") → {flow_id, step_id: "user", data_schema: [...]}
    """
    with ha._client() as c:
        r = c.post(
            "/api/config/config_entries/flow",
            json={"handler": handler, "show_advanced_options": show_advanced_options},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
def submit_config_flow_step(flow_id: str, user_input: dict) -> dict:
    """Submit user input for the current step of a config flow.

    Returns either the next step (form), an error, or the final created entry.
    Uses REST `POST /api/config/config_entries/flow/{flow_id}`.
    """
    with ha._client() as c:
        r = c.post(f"/api/config/config_entries/flow/{flow_id}", json=user_input)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def abort_config_flow(flow_id: str) -> dict:
    """Abort a config flow in progress.

    Uses REST `DELETE /api/config/config_entries/flow/{flow_id}`.
    """
    with ha._client() as c:
        r = c.delete(f"/api/config/config_entries/flow/{flow_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_config_flow(flow_id: str) -> dict:
    """Get the current state of a config flow (without advancing).

    Uses REST `GET /api/config/config_entries/flow/{flow_id}`.
    """
    with ha._client() as c:
        r = c.get(f"/api/config/config_entries/flow/{flow_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def remove_integration(entry_id: str, confirm: bool = False) -> dict:
    """Remove (uninstall) an integration config entry. Set confirm=True to proceed.

    Uses REST `DELETE /api/config/config_entries/entry/{entry_id}` — there is
    no `config_entries/remove` WebSocket command in Home Assistant.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will permanently remove integration '{entry_id}'. Call again with confirm=True.",
            "action": f"remove_integration(entry_id='{entry_id}', confirm=True)",
        }
    with ha._client() as c:
        r = c.delete(f"/api/config/config_entries/entry/{entry_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def disable_integration(entry_id: str) -> dict:
    """Disable an integration config entry without removing it."""
    return ha._ws_call(
        "config_entries/disable",
        entry_id=entry_id,
        disabled_by="user",
    )


@mcp.tool()
def enable_integration(entry_id: str) -> dict:
    """Re-enable a previously disabled integration config entry."""
    return ha._ws_call(
        "config_entries/disable",
        entry_id=entry_id,
        disabled_by=None,
    )


@mcp.tool()
def start_options_flow(entry_id: str, show_advanced_options: bool = False) -> dict:
    """Start the options flow for an existing config entry (edit settings).

    Uses REST `POST /api/config/config_entries/options/flow` — the entry_id
    is passed as `handler` (HA's options-flow API reuses the config-flow
    request shape and documents `handler` as "the entry_id").
    """
    with ha._client() as c:
        r = c.post(
            "/api/config/config_entries/options/flow",
            json={"handler": entry_id, "show_advanced_options": show_advanced_options},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
def submit_options_flow_step(flow_id: str, user_input: dict) -> dict:
    """Submit a step in an options flow.

    Uses REST `POST /api/config/config_entries/options/flow/{flow_id}`.
    """
    with ha._client() as c:
        r = c.post(f"/api/config/config_entries/options/flow/{flow_id}", json=user_input)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def abort_options_flow(flow_id: str) -> dict:
    """Abort an options flow in progress.

    Uses REST `DELETE /api/config/config_entries/options/flow/{flow_id}`.
    """
    with ha._client() as c:
        r = c.delete(f"/api/config/config_entries/options/flow/{flow_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def update_integration_entry(
    entry_id: str,
    title: str | None = None,
    pref_disable_new_entities: bool | None = None,
    pref_disable_polling: bool | None = None,
) -> dict:
    """Update properties of an existing config entry (title, polling preferences)."""
    payload: dict = {"entry_id": entry_id}
    if title is not None:
        payload["title"] = title
    if pref_disable_new_entities is not None:
        payload["pref_disable_new_entities"] = pref_disable_new_entities
    if pref_disable_polling is not None:
        payload["pref_disable_polling"] = pref_disable_polling
    return ha._ws_call("config_entries/update", **payload)
