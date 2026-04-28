from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("dashboards")


@mcp.tool()
def list_dashboards() -> list[dict]:
    """List all Lovelace dashboards."""
    return ha._ws_call("lovelace/dashboards/list")


@mcp.tool()
def get_dashboard_config(url_path: str | None = None) -> dict:
    """Get Lovelace dashboard config. `url_path` can be a dashboard (e.g. 'map') or a view path
    inside the default dashboard (e.g. 'lukasz' from /lovelace/lukasz). Omit for the full default dashboard."""
    if not url_path or url_path == "lovelace":
        return ha._ws_call("lovelace/config")
    try:
        return ha._ws_call("lovelace/config", url_path=url_path)
    except RuntimeError as e:
        if "config_not_found" not in str(e):
            raise
    # Fallback — url_path is a view inside the default dashboard
    default = ha._ws_call("lovelace/config")
    for view in default.get("views", []):
        if view.get("path") == url_path:
            return {"view": view, "source": "default_dashboard_view", "path": url_path}
    raise RuntimeError(f"No dashboard or view found for url_path='{url_path}'")


@mcp.tool()
def save_dashboard_config(config: dict, url_path: str | None = None) -> dict:
    """Save Lovelace dashboard config. Overwrites existing config. Use get_dashboard_config first, then modify and save."""
    kwargs = {"config": config}
    if url_path and url_path != "lovelace":
        kwargs["url_path"] = url_path
    ha._ws_call("lovelace/config/save", **kwargs)
    return {"status": "saved", "url_path": url_path or "lovelace"}


@mcp.tool()
def get_dashboard_resources() -> list[dict]:
    """Get Lovelace resources (custom cards, CSS)."""
    return ha._ws_call("lovelace/resources")


@mcp.tool()
def add_card_to_view(url_path: str, view_index: int, card_config: dict) -> dict:
    """Add a card to a specific view in a dashboard.
    Example card_config: {'type': 'entities', 'entities': ['light.living_room']}
    """
    config = get_dashboard_config(url_path)
    views = config.get("views", [])
    if view_index >= len(views):
        raise ValueError(f"View index {view_index} out of range (dashboard has {len(views)} views)")
    if "cards" not in views[view_index]:
        views[view_index]["cards"] = []
    views[view_index]["cards"].append(card_config)
    config["views"] = views
    return save_dashboard_config(config, url_path)


@mcp.tool()
def add_view_to_dashboard(url_path: str, view_config: dict) -> dict:
    """Add a new view to a dashboard.
    Example view_config: {'title': 'Bedroom', 'icon': 'mdi:bed', 'cards': []}
    """
    config = get_dashboard_config(url_path)
    views = config.get("views", [])
    views.append(view_config)
    config["views"] = views
    return save_dashboard_config(config, url_path)
