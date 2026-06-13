import base64
import os
import httpx
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("dashboards")

# ---------------------------------------------------------------------------
# Screenshot engine helpers (Puppet add-on — https://github.com/balloob/home-assistant-addons)
# ---------------------------------------------------------------------------
_PUPPET_PORT = 10000
_PUPPET_SLUG_SUFFIXES = ("_puppet", "_ha_mcp_screenshot")


def _resolve_screenshot_engine() -> str:
    """Return the base URL of the Puppet screenshot engine or raise RuntimeError."""
    explicit = os.environ.get("NEXUS_SCREENSHOT_ENGINE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        raise RuntimeError(
            "No screenshot engine configured. "
            "Install the 'Puppet' add-on from https://github.com/balloob/home-assistant-addons "
            "and set its access_token, OR set NEXUS_SCREENSHOT_ENGINE_URL to the engine URL."
        )

    headers = {"Authorization": f"Bearer {supervisor_token}"}
    try:
        with httpx.Client(base_url="http://supervisor", headers=headers, timeout=15) as sup:
            resp = sup.get("/addons")
            resp.raise_for_status()
            addons = resp.json().get("data", {}).get("addons", [])

        matches = [a for a in addons if str(a.get("slug", "")).endswith(_PUPPET_SLUG_SUFFIXES)]
        if not matches:
            raise RuntimeError(
                "Puppet screenshot engine add-on not found. "
                "Add balloob's repository in Settings → Add-ons → Add-on Store → Repositories, "
                "then install 'Puppet' and configure its access_token."
            )

        with httpx.Client(base_url="http://supervisor", headers=headers, timeout=15) as sup:
            for addon in matches:
                slug = addon["slug"]
                info = sup.get(f"/addons/{slug}/info").json().get("data", {})
                if info.get("state") == "started":
                    host = info.get("hostname") or info.get("ip_address")
                    if host:
                        return f"http://{host}:{_PUPPET_PORT}"

        raise RuntimeError(
            "Puppet add-on is installed but not started. "
            "Go to Settings → Add-ons → Puppet, set access_token, and start it."
        )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not discover screenshot engine via Supervisor: {e}")


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


@mcp.tool()
def screenshot(
    url_path: str | None = None,
    width: int = 1280,
    height: int = 800,
    wait_ms: int = 2500,
    full_page: bool = False,
    zoom: float = 1.0,
) -> dict:
    """Capture a PNG screenshot of a Lovelace dashboard view via the Puppet engine.

    Requires the **Puppet** add-on (balloob's repo):
      1. In HA: Settings → Add-ons → Add-on Store → ⋮ → Repositories
         Add: https://github.com/balloob/home-assistant-addons
      2. Install 'Puppet', set its 'access_token' option to a long-lived HA token, start it.

    On Docker/standalone: set NEXUS_SCREENSHOT_ENGINE_URL=http://<puppet-host>:10000

    Args:
        url_path: Lovelace path, e.g. 'caly-dom' or 'lovelace/0'. Omit for default.
        width: Viewport width in pixels (default 1280).
        height: Viewport height in pixels (default 800).
        wait_ms: Settle time in ms after load — increase for heavy chart cards (default 2500).
        full_page: Capture full scrollable page instead of just the viewport.
        zoom: Zoom factor (default 1.0).
    """
    path = url_path.strip("/") if url_path else "lovelace"
    if not path.startswith("lovelace"):
        path = f"lovelace/{path}"

    # Reject unsafe paths
    forbidden = ("://", "//", "..", "@", "\\", "?", "#")
    if any(bit in path for bit in forbidden):
        return {"error": "invalid_path", "detail": f"Unsafe characters in path: '{url_path}'"}

    try:
        engine = _resolve_screenshot_engine()
    except RuntimeError as e:
        return {"error": "engine_not_available", "detail": str(e)}

    effective_height = 4096 if full_page else height
    params = {
        "viewport": f"{width}x{effective_height}",
        "zoom": str(zoom),
        "wait": str(int(wait_ms)),
        "format": "png",
    }
    engine_url = f"{engine}/{path}"

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(engine_url, params=params)
    except httpx.HTTPError as e:
        return {
            "error": "engine_unreachable",
            "detail": str(e),
            "engine_url": engine_url,
            "hint": "Verify Puppet add-on is started and access_token is set.",
        }

    if resp.status_code >= 400:
        return {
            "error": f"engine_http_{resp.status_code}",
            "detail": resp.text[:300],
            "engine_url": engine_url,
            "hint": "Check that the dashboard path exists and the Puppet access_token is valid.",
        }

    if not resp.content:
        return {"error": "empty_response", "engine_url": engine_url}

    return {
        "url_path": path,
        "engine": engine,
        "format": "png",
        "width": width,
        "height": effective_height,
        "image_base64": base64.b64encode(resp.content).decode("ascii"),
        "size_bytes": len(resp.content),
    }
