from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("dashboards")


@mcp.tool()
def list_dashboards() -> list[dict]:
    """List all Lovelace dashboards."""
    with ha._client() as c:
        r = c.get("/api/lovelace/dashboards")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_dashboard_config(url_path: str = "lovelace") -> dict:
    """Get raw Lovelace dashboard config (views, cards). url_path: 'lovelace' for default."""
    with ha._client() as c:
        endpoint = "/api/lovelace/config" if url_path == "lovelace" else f"/api/lovelace/config/{url_path}"
        r = c.get(endpoint)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def save_dashboard_config(config: dict, url_path: str = "lovelace") -> dict:
    """Save Lovelace dashboard config. Overwrites existing config. Use get_dashboard_config first, then modify and save."""
    with ha._client() as c:
        endpoint = "/api/lovelace/config" if url_path == "lovelace" else f"/api/lovelace/config/{url_path}"
        r = c.post(endpoint, json=config)
        r.raise_for_status()
        return {"status": "saved", "url_path": url_path}


@mcp.tool()
def get_dashboard_resources() -> list[dict]:
    """Get Lovelace resources (custom cards, CSS)."""
    with ha._client() as c:
        r = c.get("/api/lovelace/resources")
        r.raise_for_status()
        return r.json()


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
