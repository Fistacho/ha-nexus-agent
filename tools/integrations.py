"""Integration setup via config_flow.

Mirrors the HA UI flow: pick a domain, start a flow, walk through steps,
and finalize. Also exposes options-flow for editing existing entries.
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

    Example: start_config_flow("shelly") → {flow_id, step_id: "user", data_schema: [...]}
    """
    return ha._ws_call(
        "config_entries/flow/init",
        handler=handler,
        show_advanced_options=show_advanced_options,
    )


@mcp.tool()
def submit_config_flow_step(flow_id: str, user_input: dict) -> dict:
    """Submit user input for the current step of a config flow.

    Returns either the next step (form), an error, or the final created entry.
    """
    return ha._ws_call(
        "config_entries/flow/configure",
        flow_id=flow_id,
        user_input=user_input,
    )


@mcp.tool()
def abort_config_flow(flow_id: str) -> dict:
    """Abort a config flow in progress."""
    return ha._ws_call("config_entries/flow/abort", flow_id=flow_id)


@mcp.tool()
def get_config_flow(flow_id: str) -> dict:
    """Get the current state of a config flow (without advancing)."""
    return ha._ws_call("config_entries/flow/get", flow_id=flow_id)


@mcp.tool()
def remove_integration(entry_id: str) -> dict:
    """Remove (uninstall) an integration config entry."""
    return ha._ws_call("config_entries/remove", entry_id=entry_id)


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
    """Start the options flow for an existing config entry (edit settings)."""
    return ha._ws_call(
        "config_entries/options/flow/init",
        handler=entry_id,
        show_advanced_options=show_advanced_options,
    )


@mcp.tool()
def submit_options_flow_step(flow_id: str, user_input: dict) -> dict:
    """Submit a step in an options flow."""
    return ha._ws_call(
        "config_entries/options/flow/configure",
        flow_id=flow_id,
        user_input=user_input,
    )


@mcp.tool()
def abort_options_flow(flow_id: str) -> dict:
    """Abort an options flow in progress."""
    return ha._ws_call("config_entries/options/flow/abort", flow_id=flow_id)


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
