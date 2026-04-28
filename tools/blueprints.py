from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("blueprints")


@mcp.tool()
def list_blueprints(domain: str = "automation") -> dict:
    """List all installed blueprints for a domain ('automation' or 'script')."""
    return ha._ws_call("blueprint/list", domain=domain)


@mcp.tool()
def import_blueprint(url: str, domain: str = "automation") -> dict:
    """Import a blueprint from a URL (e.g. GitHub raw or community forum) into /config/blueprints/<domain>/..."""
    return ha._ws_call("blueprint/import", domain=domain, url=url)


@mcp.tool()
def delete_blueprint(path: str, domain: str = "automation") -> dict:
    """Delete an installed blueprint by its relative path (e.g. 'author/blueprint_name.yaml')."""
    return ha._ws_call("blueprint/delete", domain=domain, path=path)


@mcp.tool()
def substitute_blueprint(path: str, input: dict, domain: str = "automation") -> dict:
    """Render a blueprint with the given inputs and return the resulting automation/script YAML as a dict."""
    return ha._ws_call("blueprint/substitute", domain=domain, path=path, input=input)
