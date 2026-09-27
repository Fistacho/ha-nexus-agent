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
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("integrations")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("List installable integration domains"))
def list_flow_handlers() -> list[str]:
    """List every integration domain that supports a config flow.

    Calls `GET /api/config/config_entries/flow_handlers` over HTTP for the
    domains HA can set up through the UI-style flow (as opposed to
    YAML-only integrations).

    Use when: checking whether a domain can be installed via
    `integrations_start_config_flow` before attempting it.
    Not for: domains already configured — use `system_list_integrations`.
    Returns: list of domain strings, e.g. `["shelly", "hue", ...]`.
    Errors: raises `httpx.HTTPStatusError` if the HA API request fails.
    """
    with ha._client() as c:
        r = c.get("/api/config/config_entries/flow_handlers")
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=read("List in-progress config flows"))
def list_flows_in_progress() -> list[dict]:
    """List config flows that were started but not yet finished.

    Calls the `config_entries/flow/progress` WebSocket command.

    Use when: finding a `flow_id` left over from an earlier
    `integrations_start_config_flow` call, e.g. one waiting on discovery.
    Not for: flows already completed into a config entry — use
    `system_list_integrations`.
    Returns: list of in-progress flow dicts (`flow_id`, `handler`,
    `step_id`, context, and related fields).
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. auth or WS-level error).
    """
    return ha._ws_call("config_entries/flow/progress")


@mcp.tool(annotations=write("Start an integration config flow", idempotent=False))
def start_config_flow(
    handler: Annotated[
        str,
        Field(description="Integration domain to configure, e.g. 'shelly'; from `integrations_list_flow_handlers`."),
    ],
    show_advanced_options: Annotated[
        bool,
        Field(
            description=(
                "Whether to include the integration's advanced-options fields "
                "in the returned form. False (default) hides them."
            )
        ),
    ] = False,
) -> dict:
    """Start a new config flow for an integration domain.

    Calls `POST /api/config/config_entries/flow` with `{"handler":
    handler, "show_advanced_options": show_advanced_options}`; HA has no
    WebSocket command for this. Each call creates a new, distinct flow.

    Use when: beginning to set up a new integration through the same
    step-by-step flow the HA UI uses.
    Not for: editing an already-configured entry — use
    `integrations_start_options_flow`.
    Returns: the first step: `{flow_id, step_id, data_schema, errors,
    ...}`. Pass `flow_id` into `integrations_submit_config_flow_step` with
    the user's answers.
    Errors: raises `httpx.HTTPStatusError` if `handler` is not a valid
    flow-handler domain.
    """
    with ha._client() as c:
        r = c.post(
            "/api/config/config_entries/flow",
            json={"handler": handler, "show_advanced_options": show_advanced_options},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=destructive("Submit a config flow step", idempotent=False, open_world=True))
def submit_config_flow_step(
    flow_id: Annotated[
        str,
        Field(description="Flow id from `integrations_start_config_flow` or `integrations_list_flows_in_progress`."),
    ],
    user_input: Annotated[
        dict,
        Field(
            description=(
                "Answers for the current step, matching the fields in the "
                "flow's `data_schema` (e.g. host, API key, chosen device)."
            )
        ),
    ],
) -> dict:
    """Submit user input for the current step of a config flow.

    Calls `POST /api/config/config_entries/flow/{flow_id}` with
    `user_input`. What this does depends entirely on the integration domain
    driving the flow: it may just advance to the next form, or it may
    validate credentials against the integration's own device/cloud service
    and create or overwrite a real config entry that HA loads. HA does not
    signal in advance which step is the last one, so any submitted step —
    not only a visibly "final" one — can be the one that creates the entry.

    Use when: providing the values a config-flow step asked for.
    Not for: reading the current step without submitting anything — use
    `integrations_get_config_flow`; cancelling a flow before it finishes —
    use `integrations_abort_config_flow`.
    Returns: the next step (form), a step with `errors`, or the finished
    config entry. The returned step's `last_step` field is `True` only
    when the integration explicitly declares this is the final step, and
    `None` otherwise — most integrations leave it `None` even on their
    actual last step, so `None` does not mean more steps remain.
    Errors: raises `httpx.HTTPStatusError` if `flow_id` is unknown or
    expired.
    Limits: the integration handling this domain may reach an external
    device or cloud service while validating `user_input`; a config entry
    can be created without warning on any step.
    """
    with ha._client() as c:
        r = c.post(f"/api/config/config_entries/flow/{flow_id}", json=user_input)
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=write("Abort a config flow", idempotent=True))
def abort_config_flow(
    flow_id: Annotated[
        str,
        Field(description="Flow id from `integrations_start_config_flow` or `integrations_list_flows_in_progress`."),
    ],
) -> dict:
    """Abort a config flow that has not been finished yet.

    Calls `DELETE /api/config/config_entries/flow/{flow_id}`. Nothing was
    persisted by an unfinished flow, so this discards only the flow's
    in-progress state, not a config entry.

    Use when: cancelling a setup that was started by mistake or is no
    longer wanted.
    Not for: removing a completed integration — use
    `integrations_remove_integration`.
    Returns: HA's abort response for the flow.
    Errors: raises `httpx.HTTPStatusError` if `flow_id` is unknown or
    already finished.
    """
    with ha._client() as c:
        r = c.delete(f"/api/config/config_entries/flow/{flow_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=read("Get config flow state"))
def get_config_flow(
    flow_id: Annotated[
        str,
        Field(description="Flow id from `integrations_start_config_flow` or `integrations_list_flows_in_progress`."),
    ],
) -> dict:
    """Get the current state of a config flow without advancing it.

    Calls `GET /api/config/config_entries/flow/{flow_id}`.

    Use when: checking a flow's current step/schema again, e.g. after
    losing the earlier response.
    Not for: providing the step's answers — use
    `integrations_submit_config_flow_step`.
    Returns: the flow's current step payload, same shape as
    `integrations_start_config_flow`'s result.
    Errors: raises `httpx.HTTPStatusError` if `flow_id` is unknown or
    expired.
    """
    with ha._client() as c:
        r = c.get(f"/api/config/config_entries/flow/{flow_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=destructive("Remove a config entry", idempotent=True))
def remove_integration(
    entry_id: Annotated[
        str,
        Field(description="Config entry ID to remove, from `system_list_integrations` or `system_get_all_integrations`."),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Set true to actually remove the integration. False (default) "
                "performs no action and instead returns a confirmation prompt."
            )
        ),
    ] = False,
) -> dict:
    """Remove (uninstall) an integration's config entry after an explicit confirmation.

    Calls `DELETE /api/config/config_entries/entry/{entry_id}` — there is no
    `config_entries/remove` WebSocket command. HA unloads the integration
    and deletes its entry; any devices/entities it owned typically go with
    it. Without `confirm=True` nothing is removed.

    Use when: an integration is no longer wanted and should be fully
    uninstalled, not just disabled.
    Not for: keeping the entry but pausing it — use
    `integrations_disable_integration`.
    Returns: HA's removal result when confirmed, otherwise a confirmation
    prompt.
    Errors: returns `{"error": "confirmation_required", "message": ...,
    "action": ...}` when `confirm` is false; raises
    `httpx.HTTPStatusError` if `entry_id` does not exist.
    Limits: requires `confirm=True`; deletes the entry and its
    devices/entities with no separate backup step.
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


@mcp.tool(annotations=write("Disable a config entry", idempotent=True))
def disable_integration(
    entry_id: Annotated[
        str,
        Field(description="Config entry ID to disable, from `system_list_integrations` or `system_get_all_integrations`."),
    ],
) -> dict:
    """Disable an integration's config entry without removing it.

    Calls the `config_entries/disable` WebSocket command with
    `disabled_by="user"`. The entry and its devices/entities stay
    registered but stop loading until re-enabled.

    Use when: pausing an integration temporarily while keeping its
    configuration and registry entries intact.
    Not for: permanently deleting the entry — use
    `integrations_remove_integration`; to bring it back — use
    `integrations_enable_integration`.
    Returns: the `config_entries/disable` result.
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. `entry_id` unknown).
    """
    return ha._ws_call(
        "config_entries/disable",
        entry_id=entry_id,
        disabled_by="user",
    )


@mcp.tool(annotations=write("Enable a config entry", idempotent=True))
def enable_integration(
    entry_id: Annotated[
        str,
        Field(description="Config entry ID to re-enable, from `system_list_integrations` or `system_get_all_integrations`."),
    ],
) -> dict:
    """Re-enable a previously disabled integration's config entry.

    Calls the `config_entries/disable` WebSocket command with
    `disabled_by=None`, which HA treats as clearing the disabled state.

    Use when: bringing back an integration disabled via
    `integrations_disable_integration`.
    Not for: setting up a brand-new integration — use
    `integrations_start_config_flow`.
    Returns: the `config_entries/disable` result.
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. `entry_id` unknown).
    """
    return ha._ws_call(
        "config_entries/disable",
        entry_id=entry_id,
        disabled_by=None,
    )


@mcp.tool(annotations=write("Start an options flow", idempotent=False))
def start_options_flow(
    entry_id: Annotated[
        str,
        Field(description="Config entry ID to edit, from `system_list_integrations` or `system_get_all_integrations`."),
    ],
    show_advanced_options: Annotated[
        bool,
        Field(
            description=(
                "Whether to include the integration's advanced-options fields "
                "in the returned form. False (default) hides them."
            )
        ),
    ] = False,
) -> dict:
    """Start the options flow for an existing config entry.

    Calls `POST /api/config/config_entries/options/flow`, passing
    `entry_id` as `handler` — HA's options-flow API reuses the config-flow
    request shape and documents `handler` as "the entry_id" in this
    context. Each call creates a new, distinct flow.

    Use when: changing an already-configured integration's settings through
    its own options UI/schema.
    Not for: configuring a brand-new integration — use
    `integrations_start_config_flow`.
    Returns: the first step: `{flow_id, step_id, data_schema, errors,
    ...}`. Pass `flow_id` into `integrations_submit_options_flow_step`.
    Errors: raises `httpx.HTTPStatusError` if `entry_id` does not exist or
    the integration has no options flow.
    """
    with ha._client() as c:
        r = c.post(
            "/api/config/config_entries/options/flow",
            json={"handler": entry_id, "show_advanced_options": show_advanced_options},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=destructive("Submit an options flow step", idempotent=False, open_world=True))
def submit_options_flow_step(
    flow_id: Annotated[
        str,
        Field(description="Flow id from `integrations_start_options_flow`."),
    ],
    user_input: Annotated[
        dict,
        Field(
            description=(
                "Answers for the current step, matching the fields in the "
                "flow's `data_schema`."
            )
        ),
    ],
) -> dict:
    """Submit a step of an options flow.

    Calls `POST /api/config/config_entries/options/flow/{flow_id}` with
    `user_input`. What this does depends entirely on the integration
    domain: an intermediate step just advances the form, while the step
    that finishes the flow applies the new options to the existing config
    entry, replacing its previous options. HA does not signal in advance
    which step is the last one, so any submitted step — not only a
    visibly "final" one — can be the one that overwrites the options.

    Use when: providing the values an options-flow step asked for.
    Not for: reading the current step without submitting — there is no
    separate "get options flow" tool; call
    `integrations_start_options_flow` again to restart it; cancelling a
    flow before it finishes — use `integrations_abort_options_flow`.
    Returns: the next step (form), a step with `errors`, or the entry's
    updated result. The returned step's `last_step` field is `True` only
    when the integration explicitly declares this is the final step, and
    `None` otherwise — most integrations leave it `None` even on their
    actual last step, so `None` does not mean more steps remain.
    Errors: raises `httpx.HTTPStatusError` if `flow_id` is unknown or
    expired.
    Limits: any submitted step, not only a visibly "final" one, may
    overwrite the entry's previous options; the integration may reach an
    external device or cloud service while validating the submitted
    values.
    """
    with ha._client() as c:
        r = c.post(f"/api/config/config_entries/options/flow/{flow_id}", json=user_input)
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=write("Abort an options flow", idempotent=True))
def abort_options_flow(
    flow_id: Annotated[
        str,
        Field(description="Flow id from `integrations_start_options_flow`."),
    ],
) -> dict:
    """Abort an options flow that has not been finished yet.

    Calls `DELETE /api/config/config_entries/options/flow/{flow_id}`. The
    entry's existing options are untouched, since nothing was applied by an
    unfinished flow.

    Use when: cancelling an options edit started by mistake or no longer
    wanted.
    Not for: cancelling a new-integration setup flow instead — use
    `integrations_abort_config_flow`.
    Returns: HA's abort response for the flow.
    Errors: raises `httpx.HTTPStatusError` if `flow_id` is unknown or
    already finished.
    """
    with ha._client() as c:
        r = c.delete(f"/api/config/config_entries/options/flow/{flow_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool(annotations=destructive("Update config entry properties", idempotent=True))
def update_integration_entry(
    entry_id: Annotated[
        str,
        Field(description="Config entry ID to update, from `system_list_integrations` or `system_get_all_integrations`."),
    ],
    title: Annotated[
        str | None,
        Field(description="New display title for the entry. Omit to leave the current title unchanged."),
    ] = None,
    pref_disable_new_entities: Annotated[
        bool | None,
        Field(
            description=(
                "Whether newly discovered entities from this integration should "
                "start disabled. Omit to leave the current preference unchanged."
            )
        ),
    ] = None,
    pref_disable_polling: Annotated[
        bool | None,
        Field(
            description=(
                "Whether HA should skip polling for this integration's "
                "entities. Omit to leave the current preference unchanged."
            )
        ),
    ] = None,
) -> dict:
    """Update selected properties of an existing config entry.

    Calls the `config_entries/update` WebSocket command with only the
    fields given here (`title`, `pref_disable_new_entities`,
    `pref_disable_polling`) — omitted fields keep their previous stored
    value, but any field that is given replaces the previous one, which is
    then lost.

    Use when: renaming an integration's entry or changing its
    entity-discovery/polling preferences without going through its options
    flow.
    Not for: settings specific to the integration itself (its actual
    options schema) — use `integrations_start_options_flow`.
    Returns: the `config_entries/update` result.
    Errors: raises `RuntimeError` if the underlying WebSocket call fails
    (e.g. `entry_id` unknown).
    """
    payload: dict = {"entry_id": entry_id}
    if title is not None:
        payload["title"] = title
    if pref_disable_new_entities is not None:
        payload["pref_disable_new_entities"] = pref_disable_new_entities
    if pref_disable_polling is not None:
        payload["pref_disable_polling"] = pref_disable_polling
    return ha._ws_call("config_entries/update", **payload)
