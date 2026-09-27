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
def add_dashboard_resource(url: str, resource_type: str = "module") -> dict:
    """Add a custom resource (JS module or CSS) to Lovelace.

    Resources load on every dashboard page — use this to register custom cards,
    themes, or scripts stored in /local/ or an external CDN.

    Args:
        url: Resource URL, e.g. '/local/my-card.js' or '/hacsfiles/mini-graph-card/mini-graph-card-bundle.js'.
        resource_type: 'module' (default, ES module JS), 'js' (legacy script), or 'css'.
    """
    if resource_type not in ("module", "js", "css"):
        return {"error": "resource_type must be 'module', 'js', or 'css'"}
    result = ha._ws_call("lovelace/resources/create", res_type=resource_type, url=url)
    return {"status": "added", "url": url, "resource_type": resource_type, "result": result}


@mcp.tool()
def remove_dashboard_resource(resource_id: int, confirm: bool = False) -> dict:
    """Remove a Lovelace resource by its numeric ID. Set confirm=True to proceed.

    Use get_dashboard_resources() to find the resource ID.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will remove Lovelace resource #{resource_id}. Call again with confirm=True.",
            "action": f"remove_dashboard_resource(resource_id={resource_id}, confirm=True)",
        }
    result = ha._ws_call("lovelace/resources/delete", resource_id=resource_id)
    return {"status": "removed", "resource_id": resource_id, "result": result}


@mcp.tool()
def update_dashboard_resource(resource_id: int, url: str, resource_type: str = "module") -> dict:
    """Update the URL or type of an existing Lovelace resource.

    Use get_dashboard_resources() to find the resource ID.

    Args:
        resource_id: Numeric ID of the resource to update.
        url: New resource URL.
        resource_type: 'module', 'js', or 'css'.
    """
    if resource_type not in ("module", "js", "css"):
        return {"error": "resource_type must be 'module', 'js', or 'css'"}
    result = ha._ws_call(
        "lovelace/resources/update",
        resource_id=resource_id,
        res_type=resource_type,
        url=url,
    )
    return {"status": "updated", "resource_id": resource_id, "url": url, "resource_type": resource_type, "result": result}


@mcp.tool()
def get_dashboard_config(url_path: str | None = None) -> dict:
    """Get Lovelace dashboard config. `url_path` can be a dashboard (e.g. 'map') or a view path inside
    the default dashboard (e.g. 'living-room' from /lovelace/living-room). Omit for the full default
    dashboard. For a view path the result is a read-only excerpt {view, source, path} that must not
    be passed to save_dashboard_config.
    """
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
    """Replace the ENTIRE config of one storage-mode dashboard (`url_path` = dashboard url_path; omit
    for the default dashboard). `config` must be the full dashboard object with a `views` list, as
    returned by get_dashboard_config for a dashboard path — NOT the {view, source, path} excerpt it
    returns for a view path; saving that would delete every view. No validation, backup or undo:
    keep the previous config to restore. For single-card or section edits prefer the
    add/update/remove_card_in_section tools.
    """
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
    """Append a card to view.cards of one view and save the whole dashboard. `url_path` must be a
    dashboard url_path, not a view path. On `type: sections` views cards in view.cards are visible
    only in edit mode — use dashboards_add_card_to_section there.
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


# ---------------------------------------------------------------------------
# Section-aware editing (views with `type: sections`)
#
# A sections view renders ONLY `view.sections`. Cards written to `view.cards`
# appear exclusively in edit mode, in an "imported cards" block — see
# home-assistant/frontend `src/panels/lovelace/views/hui-sections-view.ts`.
# So `add_card_to_view` cannot place a card on such a view, and the tools below
# do the read-modify-write server-side instead of making the caller round-trip
# a whole (often huge) dashboard config through `save_dashboard_config`.
# ---------------------------------------------------------------------------
def _load_view(
    url_path: str | None,
    view_index: int | None = None,
) -> tuple[dict, str | None, int, dict]:
    """Resolve a view for editing.

    Returns `(config, save_url_path, view_index, view)` where `config` is the
    dashboard that must be saved back and `save_url_path` is what
    `lovelace/config/save` needs (None = default dashboard).

    `url_path` may name a dashboard ('map') or a view inside the default
    dashboard ('living-room'); in the latter case `view_index` is inferred.
    """
    config: dict | None = None
    save_url_path: str | None = None
    view_path: str | None = None

    if url_path and url_path != "lovelace":
        try:
            config = ha._ws_call("lovelace/config", url_path=url_path)
            save_url_path = url_path
        except RuntimeError as e:
            if "config_not_found" not in str(e):
                raise
            # Not a dashboard — treat it as a view path in the default dashboard.
            view_path = url_path

    if config is None:
        config = ha._ws_call("lovelace/config")

    views = config.get("views") or []
    if not views:
        raise ValueError(f"Dashboard '{save_url_path or 'lovelace'}' has no views")

    idx = view_index
    if idx is None and view_path:
        idx = next((i for i, v in enumerate(views) if v.get("path") == view_path), None)
        if idx is None:
            raise ValueError(
                f"No view with path '{view_path}' in the default dashboard. "
                "Call get_dashboard_config() to list view paths."
            )
    if idx is None:
        raise ValueError(
            "Specify which view: pass view_index, or pass the view's path as url_path "
            "(e.g. url_path='living-room')."
        )
    if idx < 0 or idx >= len(views):
        raise ValueError(f"View index {idx} out of range (dashboard has {len(views)} views)")

    config["views"] = views
    return config, save_url_path, idx, views[idx]


def _require_sections(view: dict, view_index: int) -> list[dict]:
    """Return the view's section list, refusing views that cannot render sections."""
    if view.get("type") != "sections":
        raise ValueError(
            f"View {view_index} ('{view.get('title')}') is not a 'sections' view "
            f"(type={view.get('type') or 'masonry'}). Cards belong in view.cards there — "
            "use add_card_to_view instead."
        )
    return view.setdefault("sections", [])


def _require_section(sections: list[dict], section_index: int, view_index: int) -> dict:
    if section_index < 0 or section_index >= len(sections):
        raise ValueError(
            f"Section index {section_index} out of range — view {view_index} "
            f"has {len(sections)} sections. Call get_view_sections to list them."
        )
    section = sections[section_index]
    section.setdefault("cards", [])
    return section


def _require_card(section: dict, card_index: int, section_index: int) -> dict:
    cards = section["cards"]
    if card_index < 0 or card_index >= len(cards):
        raise ValueError(
            f"Card index {card_index} out of range — section {section_index} "
            f"has {len(cards)} cards. Call get_view_sections to inspect it."
        )
    return cards[card_index]


def _section_heading(section: dict) -> str | None:
    """Best-effort human label: the section's heading card, or its title."""
    for card in section.get("cards") or []:
        if card.get("type") == "heading":
            return card.get("heading")
    return section.get("title")


def _save(config: dict, save_url_path: str | None) -> None:
    kwargs: dict = {"config": config}
    if save_url_path:
        kwargs["url_path"] = save_url_path
    ha._ws_call("lovelace/config/save", **kwargs)


def _result(status: str, save_url_path: str | None, view_index: int, view: dict, **extra) -> dict:
    return {
        "status": status,
        "url_path": save_url_path or "lovelace",
        "view_index": view_index,
        "view_title": view.get("title"),
        **extra,
    }


@mcp.tool()
def get_view_sections(url_path: str | None = None, view_index: int | None = None) -> dict:
    """List the sections of a `type: sections` view with their indexes, headings and card counts.

    Read this before add_card_to_section / update_card_in_section so the indexes are real.
    `url_path` may be a dashboard ('map') or a view path in the default dashboard ('living-room').
    """
    _config, save_url_path, idx, view = _load_view(url_path, view_index)
    sections = _require_sections(view, idx)
    return _result(
        "ok",
        save_url_path,
        idx,
        view,
        view_path=view.get("path"),
        sections=[
            {
                "section_index": i,
                "heading": _section_heading(s),
                "card_count": len(s.get("cards") or []),
            }
            for i, s in enumerate(sections)
        ],
    )


@mcp.tool()
def add_card_to_section(
    url_path: str | None,
    section_index: int,
    card_config: dict,
    view_index: int | None = None,
    position: int | None = None,
) -> dict:
    """Add a card to one section of a `type: sections` view, leaving every other view intact.

    This is the only way to make a card visible on a sections view —
    add_card_to_view writes to view.cards, which such a view renders only in edit mode.

    Args:
        url_path: Dashboard url_path, or a view path in the default dashboard ('living-room').
        section_index: Target section — see get_view_sections.
        card_config: Lovelace card, e.g. {'type': 'tile', 'entity': 'sensor.power'}.
        view_index: Needed only when url_path names a dashboard rather than a view.
        position: Insert at this index instead of appending.
    """
    if not isinstance(card_config, dict) or not card_config.get("type"):
        raise ValueError("card_config must be a dict with a 'type' key, e.g. {'type': 'tile', ...}")

    config, save_url_path, idx, view = _load_view(url_path, view_index)
    sections = _require_sections(view, idx)
    section = _require_section(sections, section_index, idx)

    cards = section["cards"]
    at = len(cards) if position is None else max(0, min(position, len(cards)))
    cards.insert(at, card_config)
    _save(config, save_url_path)

    return _result(
        "added",
        save_url_path,
        idx,
        view,
        section_index=section_index,
        card_index=at,
        cards_in_section=len(cards),
    )


@mcp.tool()
def update_card_in_section(
    url_path: str | None,
    section_index: int,
    card_index: int,
    card_config: dict,
    view_index: int | None = None,
    merge: bool = True,
) -> dict:
    """Change one card inside a section of a `type: sections` view.

    Args:
        merge: True (default) merges the given keys into the existing card;
            False replaces the card wholesale.
    """
    if not isinstance(card_config, dict) or not card_config:
        raise ValueError("card_config must be a non-empty dict")
    if not merge and not card_config.get("type"):
        raise ValueError("A replacement card_config needs a 'type' key")

    config, save_url_path, idx, view = _load_view(url_path, view_index)
    sections = _require_sections(view, idx)
    section = _require_section(sections, section_index, idx)
    existing = _require_card(section, card_index, section_index)

    before = dict(existing)
    if merge:
        existing.update(card_config)
        new_card = existing
    else:
        new_card = card_config
        section["cards"][card_index] = new_card
    _save(config, save_url_path)

    return _result(
        "updated",
        save_url_path,
        idx,
        view,
        section_index=section_index,
        card_index=card_index,
        card_before=before,
        card_after=new_card,
    )


@mcp.tool()
def remove_card_from_section(
    url_path: str | None,
    section_index: int,
    card_index: int,
    view_index: int | None = None,
) -> dict:
    """Delete one card from a section of a `type: sections` view.

    The removed card is returned, so it can be re-added with add_card_to_section.
    """
    config, save_url_path, idx, view = _load_view(url_path, view_index)
    sections = _require_sections(view, idx)
    section = _require_section(sections, section_index, idx)
    _require_card(section, card_index, section_index)

    removed = section["cards"].pop(card_index)
    _save(config, save_url_path)

    return _result(
        "removed",
        save_url_path,
        idx,
        view,
        section_index=section_index,
        card_index=card_index,
        removed_card=removed,
        cards_in_section=len(section["cards"]),
    )


@mcp.tool()
def add_section_to_view(
    url_path: str | None,
    section_config: dict | None = None,
    view_index: int | None = None,
    position: int | None = None,
) -> dict:
    """Add a section to a `type: sections` view.

    Args:
        section_config: Section body, e.g.
            {'cards': [{'type': 'heading', 'heading': 'Living Room'}]}.
            Defaults to an empty grid section.
        position: Insert at this index instead of appending.
    """
    section = dict(section_config or {})
    if not isinstance(section.get("cards", []), list):
        raise ValueError("section_config['cards'] must be a list of card configs")
    section.setdefault("type", "grid")
    section.setdefault("cards", [])

    config, save_url_path, idx, view = _load_view(url_path, view_index)
    sections = _require_sections(view, idx)

    at = len(sections) if position is None else max(0, min(position, len(sections)))
    sections.insert(at, section)
    _save(config, save_url_path)

    return _result(
        "added",
        save_url_path,
        idx,
        view,
        section_index=at,
        sections_in_view=len(sections),
    )


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

    Requires the **Puppet** add-on (balloob's repo), discovered via Supervisor
    or `NEXUS_SCREENSHOT_ENGINE_URL` (Docker/standalone: point it at
    `http://<puppet-host>:10000`). If the engine isn't available, the returned
    error includes install/start instructions — no need to duplicate them here.

    Args:
        url_path: Frontend route to capture. A registered dashboard's own
            `url_path` (e.g. 'dashbord-parter', optionally with a view path
            appended: 'dashbord-parter/testy-parter') is used as-is — Home
            Assistant serves every non-default dashboard at its own top-level
            route (`/<dashboard_url_path>/<view>`), never under
            `/lovelace/...`. A path that already starts with 'lovelace' (e.g.
            'lovelace/0') is also used as-is. Anything else is treated as a
            view of the *default* dashboard and gets 'lovelace/' prepended
            (e.g. 'caly-dom' -> 'lovelace/caly-dom'). Omit for the default
            dashboard's root ('lovelace').
            Telling these cases apart costs one `lovelace/dashboards/list` WS
            call (skipped when the path already starts with 'lovelace'); if
            that call fails, this tool returns
            `{"error": "dashboard_list_unavailable"}` instead of guessing —
            silently mis-routing to the wrong dashboard is worse than failing
            loudly. Use `list_dashboards()` to inspect registered dashboards
            up front if you expect this to matter.
        width: Viewport width in pixels (default 1280).
        height: Viewport height in pixels (default 800). Ignored when full_page=True.
        wait_ms: Settle time in ms after load — increase for heavy chart cards (default 2500).
        full_page: Use a fixed 4096px-tall viewport instead of `height`. The engine
            does not measure the actual page height, so this only approximates a
            full-scroll capture — it can crop very tall pages or leave blank space
            on short ones.
        zoom: Zoom factor (default 1.0).

    Returns the PNG as a base64 string (`image_base64`) — this payload can be
    large, especially with `full_page=True`. The returned `url_path` echoes the
    frontend route actually requested (after resolution above), not the raw
    argument.
    """
    raw = url_path.strip("/") if url_path else "lovelace"

    if raw == "lovelace" or raw.startswith("lovelace/"):
        path = raw
    else:
        first_segment = raw.split("/", 1)[0]
        try:
            registered = {
                d.get("url_path") for d in ha._ws_call("lovelace/dashboards/list")
            }
        except Exception as e:
            return {
                "error": "dashboard_list_unavailable",
                "detail": f"Could not list dashboards to resolve url_path='{url_path}': {e}",
                "hint": "Retry, or pass an already-qualified path: 'lovelace/<view>' "
                        "for a default-dashboard view, or the dashboard's registered "
                        "url_path if you already know it names a dashboard.",
            }
        path = raw if first_segment in registered else f"lovelace/{raw}"

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
