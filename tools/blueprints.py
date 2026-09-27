from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("blueprints")


@mcp.tool()
def list_blueprints(domain: str = "automation") -> dict:
    """List all installed blueprints for a domain ('automation' or 'script')."""
    return ha._ws_call("blueprint/list", domain=domain)


@mcp.tool()
def import_blueprint(url: str, domain: str = "automation") -> dict:
    """Fetch and validate a blueprint from a URL (e.g. GitHub raw or community forum).

    This only downloads and validates — it does NOT save anything to disk. Returns
    `suggested_filename`, `raw_data` (the blueprint YAML text), `blueprint.metadata`,
    `validation_errors` and `exists` (whether a blueprint already sits at that path).
    To actually persist it under /config/blueprints/<domain>/..., pass `raw_data` and
    `suggested_filename` to `blueprints_save_blueprint`.
    """
    return ha._ws_call("blueprint/import", domain=domain, url=url)


@mcp.tool()
def save_blueprint(
    path: str,
    yaml_content: str,
    domain: str = "automation",
    source_url: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Save blueprint YAML to /config/blueprints/<domain>/<path>.

    `path` is the relative filename (e.g. 'author/blueprint_name.yaml') — typically the
    `suggested_filename` from `blueprints_import_blueprint`. `yaml_content` is the raw
    blueprint YAML text (e.g. `raw_data` from `blueprints_import_blueprint`).
    Does NOT overwrite an existing file unless `overwrite=True`.
    """
    kwargs: dict = {"domain": domain, "path": path, "yaml": yaml_content, "allow_override": overwrite}
    if source_url:
        kwargs["source_url"] = source_url
    return ha._ws_call("blueprint/save", **kwargs)


@mcp.tool()
def delete_blueprint(path: str, domain: str = "automation") -> dict:
    """Delete an installed blueprint by its relative path (e.g. 'author/blueprint_name.yaml')."""
    return ha._ws_call("blueprint/delete", domain=domain, path=path)


@mcp.tool()
def substitute_blueprint(path: str, input: dict, domain: str = "automation") -> dict:
    """Render a blueprint with the given inputs and return the resulting automation/script YAML as a dict."""
    return ha._ws_call("blueprint/substitute", domain=domain, path=path, input=input)
