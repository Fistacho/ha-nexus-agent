from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read

mcp = FastMCP("blueprints")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("List installed blueprints"))
def list_blueprints(
    domain: Annotated[
        str,
        Field(
            description=(
                "Blueprint domain folder under /config/blueprints/ to list, "
                "'automation' or 'script'. Defaults to 'automation'."
            )
        ),
    ] = "automation",
) -> dict:
    """List all installed blueprints for a domain over the WebSocket API.

    Calls the `blueprint/list` WebSocket command via `ha._ws_call` for the
    given `domain` and returns Home Assistant's response unchanged; nothing
    is read from disk directly here.

    Use when: enumerating what blueprints exist under
    /config/blueprints/<domain> before importing, substituting or deleting
    one.
    Returns: dict keyed by blueprint path, each value the blueprint's
    metadata (name, description, domain, source_url) as returned by HA.
    Limits: blocks for the WebSocket round trip; the default 10 second
    response wait in `ha._ws_call` applies.
    """
    return ha._ws_call("blueprint/list", domain=domain)


@mcp.tool(annotations=read("Import and validate a blueprint from a URL", open_world=True))
def import_blueprint(
    url: Annotated[
        str,
        Field(
            description=(
                "HTTP(S) URL to fetch the blueprint YAML from, e.g. a GitHub "
                "raw file link or a community forum export link."
            )
        ),
    ],
    domain: Annotated[
        str,
        Field(
            description=(
                "Blueprint domain the fetched blueprint belongs to, "
                "'automation' or 'script'. Defaults to 'automation'."
            )
        ),
    ] = "automation",
) -> dict:
    """Fetch a blueprint from a URL and validate it without saving it.

    Calls the `blueprint/import` WebSocket command, which has Home Assistant
    download `url` and validate its YAML. This only downloads and
    validates — it does NOT save anything to disk.

    Use when: previewing a blueprint (its metadata and validation errors)
    before deciding to persist it.
    Not for: persisting the fetched blueprint — pass its `raw_data` and
    `suggested_filename` to `blueprints_save_blueprint`.
    Returns: dict with `suggested_filename`, `raw_data` (the blueprint YAML
    text), `blueprint.metadata`, `validation_errors` and `exists` (whether a
    blueprint already sits at that path).
    Limits: blocks for the WebSocket round trip plus the remote download
    time; the default 10 second response wait in `ha._ws_call` applies.
    """
    return ha._ws_call("blueprint/import", domain=domain, url=url)


@mcp.tool(annotations=destructive("Save a blueprint file", idempotent=True))
def save_blueprint(
    path: Annotated[
        str,
        Field(
            description=(
                "Blueprint path relative to /config/blueprints/<domain>/, "
                "e.g. 'author/blueprint_name.yaml'. Typically the "
                "`suggested_filename` from `blueprints_import_blueprint`."
            )
        ),
    ],
    yaml_content: Annotated[
        str,
        Field(
            description=(
                "Raw blueprint YAML text to persist, e.g. the `raw_data` "
                "field returned by `blueprints_import_blueprint`."
            )
        ),
    ],
    domain: Annotated[
        str,
        Field(
            description=(
                "Blueprint domain folder to save under, 'automation' or "
                "'script'. Defaults to 'automation'."
            )
        ),
    ] = "automation",
    source_url: Annotated[
        str | None,
        Field(
            description=(
                "Origin URL to record as the blueprint's source_url "
                "metadata. Omit to save without one."
            )
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        Field(
            description=(
                "If True, replace an existing file at `path`. If False "
                "(default), Home Assistant refuses the save when a file "
                "already exists there."
            )
        ),
    ] = False,
) -> dict:
    """Save blueprint YAML to /config/blueprints/<domain>/<path>.

    Calls the `blueprint/save` WebSocket command with `yaml_content` as the
    payload and `overwrite` mapped to HA's `allow_override` flag; with
    `overwrite=False` (default) it does not replace an existing file at
    `path`, and with `overwrite=True` it does, losing the previous content.

    Not for: fetching a blueprint from the internet first — use
    `blueprints_import_blueprint` to get `raw_data`/`suggested_filename`.
    Returns: Home Assistant's `blueprint/save` result (typically an empty
    success payload).
    Errors: Home Assistant raises a WebSocket error, surfaced by
    `ha._ws_call` as a `RuntimeError`, when `overwrite=False` and a file
    already exists at `path`.
    Limits: blocks for the WebSocket round trip; the default 10 second
    response wait in `ha._ws_call` applies. No `confirm` parameter — the
    only overwrite guard is the `overwrite` flag itself.
    """
    kwargs: dict = {"domain": domain, "path": path, "yaml": yaml_content, "allow_override": overwrite}
    if source_url:
        kwargs["source_url"] = source_url
    return ha._ws_call("blueprint/save", **kwargs)


@mcp.tool(annotations=destructive("Delete an installed blueprint", idempotent=True))
def delete_blueprint(
    path: Annotated[
        str,
        Field(
            description=(
                "Blueprint path relative to /config/blueprints/<domain>/ to "
                "delete, e.g. 'author/blueprint_name.yaml'."
            )
        ),
    ],
    domain: Annotated[
        str,
        Field(
            description=(
                "Blueprint domain folder `path` lives under, 'automation' "
                "or 'script'. Defaults to 'automation'."
            )
        ),
    ] = "automation",
) -> dict:
    """Delete an installed blueprint by its relative path.

    Calls the `blueprint/delete` WebSocket command for `domain`/`path`; the
    file under /config/blueprints/<domain>/ is removed and its content is
    lost, with no backup taken by this tool.

    Not for: automations or scripts generated from this blueprint — they
    keep running with their already-substituted config; use
    `automations_delete_automation` or `automations_delete_script` for
    those.
    Returns: Home Assistant's `blueprint/delete` result (typically an empty
    success payload).
    Limits: blocks for the WebSocket round trip; the default 10 second
    response wait in `ha._ws_call` applies. No `confirm` parameter guards
    this deletion.
    """
    return ha._ws_call("blueprint/delete", domain=domain, path=path)


@mcp.tool(annotations=read("Render a blueprint with given inputs"))
def substitute_blueprint(
    path: Annotated[
        str,
        Field(
            description=(
                "Blueprint path relative to /config/blueprints/<domain>/ to "
                "render, e.g. 'author/blueprint_name.yaml'."
            )
        ),
    ],
    input: Annotated[
        dict,
        Field(
            description=(
                "Input values keyed by the input name declared in the "
                "blueprint's own `input:` section."
            )
        ),
    ],
    domain: Annotated[
        str,
        Field(
            description=(
                "Blueprint domain folder `path` lives under, 'automation' "
                "or 'script'. Defaults to 'automation'."
            )
        ),
    ] = "automation",
) -> dict:
    """Render a blueprint with given inputs into a plain automation or script.

    Calls the `blueprint/substitute` WebSocket command, which resolves
    `input` against the blueprint's `input:` placeholders and returns the
    resulting config; nothing is saved or reloaded by this tool.

    Use when: previewing the concrete triggers/conditions/actions a
    blueprint would produce before saving it via
    `automations_set_automation_config` or `automations_set_script_config`.
    Returns: dict with the substituted automation/script YAML content as a
    dict.
    Limits: blocks for the WebSocket round trip; the default 10 second
    response wait in `ha._ws_call` applies.
    """
    return ha._ws_call("blueprint/substitute", domain=domain, path=path, input=input)
