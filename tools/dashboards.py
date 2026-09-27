from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Annotated

import httpx
from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("dashboards")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

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
        raise RuntimeError(f"Could not discover screenshot engine via Supervisor: {e}") from e


@mcp.tool(annotations=read("List Lovelace dashboards"))
def list_dashboards() -> list[dict]:
    """List every registered Lovelace dashboard.

    Calls `lovelace/dashboards/list` and returns the raw collection: each
    entry has `id`, `url_path`, `title`, `mode` (`storage` or `yaml`), and
    related flags.

    Use when: discovering a dashboard's `url_path` before calling
    `dashboards_get_dashboard_config` or `dashboards_screenshot`.
    Returns: list of dashboard dicts, unfiltered. The default dashboard is
    not included — it has no `url_path` entry here.
    """
    return ha._ws_call("lovelace/dashboards/list")


@mcp.tool(annotations=write("Add Lovelace resource", idempotent=False))
def add_dashboard_resource(
    url: Annotated[
        str,
        Field(description="Resource URL, e.g. '/local/my-card.js' or '/hacsfiles/mini-graph-card/mini-graph-card-bundle.js'."),
    ],
    resource_type: Annotated[
        str,
        Field(description="Resource kind: 'module' (ES module JS), 'js' (legacy script), or 'css'. Defaults to 'module'."),
    ] = "module",
) -> dict:
    """Register a custom JS or CSS resource that loads on every Lovelace dashboard page.

    Sends `url`/`resource_type` to `lovelace/resources/create`, which
    stores a new resource entry with a server-generated id — repeating
    this call registers a second, distinct resource rather than updating
    one. The tool itself only stores the URL string; it never fetches
    `url` — the browser loads it later, from `/local/`, `/hacsfiles/`, or
    any external CDN the URL names.

    Use when: registering a custom card, theme, or script so it becomes
    available on every dashboard.
    Not for: changing an existing resource's URL/type — use
    `dashboards_update_dashboard_resource`.
    Returns: `{"status": "added", "url": ..., "resource_type": ...,
    "result": ...}` on success.
    Errors: `{"error": "resource_type must be 'module', 'js', or 'css'"}`
    when `resource_type` is anything else.
    """
    if resource_type not in ("module", "js", "css"):
        return {"error": "resource_type must be 'module', 'js', or 'css'"}
    result = ha._ws_call("lovelace/resources/create", res_type=resource_type, url=url)
    return {"status": "added", "url": url, "resource_type": resource_type, "result": result}


@mcp.tool(annotations=destructive("Remove Lovelace resource", idempotent=True))
def remove_dashboard_resource(
    resource_id: Annotated[int, Field(description="Numeric resource id, from dashboards_get_dashboard_resources.")],
    confirm: Annotated[
        bool,
        Field(description="Must be true to actually remove the resource; false (the default) returns a confirmation prompt instead."),
    ] = False,
) -> dict:
    """Remove a registered Lovelace resource by its numeric id.

    Requires `confirm=True`; without it, returns a confirmation prompt and
    makes no change. With `confirm=True`, calls `lovelace/resources/delete`,
    which drops the resource from every dashboard page that loaded it —
    any custom card/theme it provided stops rendering until the resource
    is re-added.

    Use when: removing a resource that is no longer needed (e.g. after
    uninstalling a HACS frontend card).
    Returns: `{"status": "removed", "resource_id": ..., "result": ...}` on
    success.
    Errors: `{"error": "confirmation_required", "message": ..., "action":
    ...}` when `confirm` is false.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will remove Lovelace resource #{resource_id}. Call again with confirm=True.",
            "action": f"remove_dashboard_resource(resource_id={resource_id}, confirm=True)",
        }
    result = ha._ws_call("lovelace/resources/delete", resource_id=resource_id)
    return {"status": "removed", "resource_id": resource_id, "result": result}


@mcp.tool(annotations=destructive("Update Lovelace resource", idempotent=True))
def update_dashboard_resource(
    resource_id: Annotated[int, Field(description="Numeric resource id to update, from dashboards_get_dashboard_resources.")],
    url: Annotated[str, Field(description="New resource URL, replacing the current one.")],
    resource_type: Annotated[
        str,
        Field(description="Resource kind: 'module' (ES module JS), 'js' (legacy script), or 'css'. Defaults to 'module'."),
    ] = "module",
) -> dict:
    """Replace the URL and type of an existing Lovelace resource.

    Sends `resource_id`, `url` and `resource_type` to
    `lovelace/resources/update`, replacing both fields of that resource
    entry — the previous URL/type are not recoverable afterwards. Like
    `dashboards_add_dashboard_resource`, this tool never fetches `url`
    itself.

    Use when: repointing an existing resource entry at a new file or
    changing its declared type.
    Not for: registering a brand-new resource — use
    `dashboards_add_dashboard_resource`.
    Returns: `{"status": "updated", "resource_id": ..., "url": ...,
    "resource_type": ..., "result": ...}` on success.
    Errors: `{"error": "resource_type must be 'module', 'js', or 'css'"}`
    when `resource_type` is anything else.
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


@mcp.tool(annotations=read("Get Lovelace dashboard config"))
def get_dashboard_config(
    url_path: Annotated[
        str | None,
        Field(
            description=(
                "Dashboard url_path (e.g. 'map', from dashboards_list_dashboards) or a view "
                "path inside the default dashboard (e.g. 'living-room', from "
                "/lovelace/living-room). Omit for the full default dashboard."
            )
        ),
    ] = None,
) -> dict:
    """Get one Lovelace dashboard's config, or a read-only excerpt of one view.

    Calls `lovelace/config` for `url_path` as a dashboard; if that raises
    `config_not_found`, re-fetches the default dashboard and returns the
    matching view instead, since `url_path` may instead be that view's own
    `path`.

    Use when: reading a dashboard's `views`/`sections` before editing it,
    or inspecting one view without the surrounding dashboard.
    Not for: passing the view-path result back into
    `dashboards_save_dashboard_config` — that excerpt is not a full
    dashboard config and would delete every other view.
    Returns: the full dashboard dict (with `views`) for a dashboard
    `url_path`; `{"view": ..., "source": "default_dashboard_view", "path":
    ...}` for a view path in the default dashboard.
    Errors: raises `RuntimeError` when `url_path` matches neither a
    dashboard nor a view in the default dashboard.
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


@mcp.tool(annotations=destructive("Save Lovelace dashboard config", idempotent=True))
def save_dashboard_config(
    config: Annotated[
        dict,
        Field(
            description=(
                "Full dashboard object with a 'views' list, as returned by "
                "dashboards_get_dashboard_config for a dashboard path — never the "
                "{view, source, path} excerpt it returns for a view path."
            )
        ),
    ],
    url_path: Annotated[
        str | None,
        Field(description="Dashboard url_path to save, from dashboards_list_dashboards. Omit for the default dashboard."),
    ] = None,
) -> dict:
    """Replace the entire config of one storage-mode Lovelace dashboard.

    Sends `config` as-is to `lovelace/config/save`, fully overwriting that
    dashboard's stored `views`/`sections` with no validation, backup, or
    undo — keep the previous config (e.g. from
    `dashboards_get_dashboard_config`) if you need to restore it.

    Use when: writing back a dashboard object built or fetched in full.
    Not for: a single card or section change — use
    `dashboards_add_card_to_section`/`dashboards_update_card_in_section`/
    `dashboards_remove_card_from_section` (sections views) or
    `dashboards_add_card_to_view` (masonry/panel views), which read-modify-
    write the config for you.
    Returns: `{"status": "saved", "url_path": ...}`.
    """
    kwargs = {"config": config}
    if url_path and url_path != "lovelace":
        kwargs["url_path"] = url_path
    ha._ws_call("lovelace/config/save", **kwargs)
    return {"status": "saved", "url_path": url_path or "lovelace"}


@mcp.tool(annotations=read("Get Lovelace resources"))
def get_dashboard_resources() -> list[dict]:
    """List every registered Lovelace resource (custom cards, CSS).

    Calls `lovelace/resources` and returns the raw collection: each entry
    has `id`, `type` (`module`/`js`/`css`), and `url`.

    Use when: finding a resource's numeric id before calling
    `dashboards_update_dashboard_resource` or
    `dashboards_remove_dashboard_resource`.
    Returns: list of resource dicts, unfiltered.
    """
    return ha._ws_call("lovelace/resources")


@mcp.tool(annotations=write("Add card to dashboard view", idempotent=False))
def add_card_to_view(
    url_path: Annotated[str, Field(description="Dashboard url_path (not a view path), from dashboards_list_dashboards.")],
    view_index: Annotated[int, Field(description="Zero-based index of the target view within the dashboard's 'views' list.")],
    card_config: Annotated[
        dict,
        Field(description="Lovelace card config, e.g. {'type': 'entities', 'entities': ['light.living_room']}."),
    ],
) -> dict:
    """Append a card to one view's `cards` list and save the whole dashboard.

    Reads the dashboard via `dashboards_get_dashboard_config`, appends
    `card_config` to `views[view_index]["cards"]` (creating that key if
    absent), and writes the whole dashboard back with
    `dashboards_save_dashboard_config` — every other view and card is
    round-tripped unchanged.

    Use when: adding a card to a `masonry`/`panel` view.
    Not for: a `type: sections` view — there, cards in `view.cards` render
    only in edit mode; use `dashboards_add_card_to_section` instead.
    Returns: `{"status": "saved", "url_path": ...}` from the underlying
    save.
    Errors: raises `ValueError` when `view_index` is out of range for the
    dashboard's view count.
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


@mcp.tool(annotations=write("Add view to dashboard", idempotent=False))
def add_view_to_dashboard(
    url_path: Annotated[str, Field(description="Dashboard url_path to add the view to, from dashboards_list_dashboards.")],
    view_config: Annotated[
        dict,
        Field(description="Lovelace view config, e.g. {'title': 'Bedroom', 'icon': 'mdi:bed', 'cards': []}."),
    ],
) -> dict:
    """Append a new view to a dashboard and save the whole dashboard.

    Reads the dashboard via `dashboards_get_dashboard_config`, appends
    `view_config` to its `views` list, and writes the whole dashboard back
    with `dashboards_save_dashboard_config` — every existing view and card
    is round-tripped unchanged.

    Use when: adding a whole new tab/view to a dashboard.
    Not for: adding a card to an existing view — use
    `dashboards_add_card_to_view` or `dashboards_add_card_to_section`.
    Returns: `{"status": "saved", "url_path": ...}` from the underlying
    save.
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


def _config_hash(config: dict) -> str:
    """Stable short hash of a whole dashboard config (ADR-0003 D2).

    Canonical JSON (`sort_keys=True`, `separators=(",", ":")`,
    `ensure_ascii=False`) so key order/whitespace never change the hash,
    then the first 16 hex chars of its sha256. Shared by every reader/
    writer below — never computed ad hoc elsewhere.
    """
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _read_config(save_url_path: str | None) -> dict:
    """Fetch the dashboard config HA currently stores, the same way `_load_view` does.

    Used to re-read after a save (see `_save_and_hash`) — HA may normalize
    what it persists, so the post-save hash must come from a fresh read,
    never from the locally mutated dict.
    """
    if save_url_path:
        return ha._ws_call("lovelace/config", url_path=save_url_path)
    return ha._ws_call("lovelace/config")


def _save_and_hash(config: dict, save_url_path: str | None) -> str:
    """Save `config`, then return `_config_hash` of what HA actually stored.

    Re-reads via `_read_config` after the save instead of hashing the local
    `config` dict, so the returned hash matches what the next
    `dashboards_get_view_sections` call will report even if HA normalizes
    the saved data.
    """
    _save(config, save_url_path)
    return _config_hash(_read_config(save_url_path))


def _check_expected_hash(config: dict, expected_config_hash: str | None) -> dict | None:
    """Return a `config_changed` error dict if `expected_config_hash` is stale, else None.

    Hashes `config` as loaded, before any local mutation — call this right
    after `_load_view()` and before touching `sections`/`cards`, and return
    its result immediately (no save) when it isn't None.
    """
    if expected_config_hash is None:
        return None
    current = _config_hash(config)
    if current == expected_config_hash:
        return None
    return {
        "error": "config_changed",
        "message": (
            "The dashboard changed since expected_config_hash was read; "
            "section_index/card_index positions may now point at different cards."
        ),
        "expected_config_hash": expected_config_hash,
        "current_config_hash": current,
        "action": "Call dashboards_get_view_sections again and re-derive indexes.",
    }


def _result(status: str, save_url_path: str | None, view_index: int, view: dict, **extra) -> dict:
    return {
        "status": status,
        "url_path": save_url_path or "lovelace",
        "view_index": view_index,
        "view_title": view.get("title"),
        **extra,
    }


@mcp.tool(annotations=read("List sections of a dashboard view"))
def get_view_sections(
    url_path: Annotated[
        str | None,
        Field(description="Dashboard url_path (e.g. 'map') or a view path in the default dashboard (e.g. 'living-room'). Omit for the default dashboard's first view."),
    ] = None,
    view_index: Annotated[
        int | None,
        Field(description="Zero-based view index; required when url_path names a dashboard rather than a view path. Omit when url_path already identifies the view."),
    ] = None,
) -> dict:
    """List the sections of one `type: sections` view, with real indexes, headings and card counts.

    Resolves the view via `_load_view` (dashboard config or default-
    dashboard view path), requires it to be a `sections` view, and reports
    each section's position, a fallback-derived heading (its `heading`
    card, or `title`), and card count.

    Use when: reading current section indexes before calling
    `dashboards_add_card_to_section` / `dashboards_update_card_in_section`
    / `dashboards_remove_card_from_section`, or before re-sending
    `expected_config_hash` to one of them after their previous call
    changed it.
    Returns: `{status, url_path, view_index, view_title, view_path,
    sections: [{section_index, heading, card_count}, ...], config_hash}` —
    pass `config_hash` back as `expected_config_hash` to
    `dashboards_add_card_to_section` / `_update_card_in_section` /
    `_remove_card_from_section` / `_add_section_to_view` to detect a
    dashboard change before your write lands.
    Errors: raises `ValueError` when the view can't be resolved (bad
    index/path) or is not a `type: sections` view.
    """
    config, save_url_path, idx, view = _load_view(url_path, view_index)
    config_hash = _config_hash(config)
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
        config_hash=config_hash,
    )


@mcp.tool(annotations=write("Add card to dashboard section", idempotent=False))
def add_card_to_section(
    url_path: Annotated[
        str | None,
        Field(description="Dashboard url_path, or a view path in the default dashboard (e.g. 'living-room')."),
    ],
    section_index: Annotated[int, Field(description="Zero-based target section index, from dashboards_get_view_sections.")],
    card_config: Annotated[
        dict,
        Field(description="Lovelace card config with a 'type' key, e.g. {'type': 'tile', 'entity': 'sensor.power'}."),
    ],
    view_index: Annotated[
        int | None,
        Field(description="Zero-based view index; needed only when url_path names a dashboard rather than a view path."),
    ] = None,
    position: Annotated[
        int | None,
        Field(description="Zero-based index within the section to insert at. Omit to append at the end."),
    ] = None,
    expected_config_hash: Annotated[
        str | None,
        Field(
            description=(
                "Optional guard: the config_hash from a recent dashboards_get_view_sections "
                "or section-tool response. If the dashboard's current hash differs, this call "
                "returns 'config_changed' and saves nothing, since section_index/position are "
                "positions, not stable ids, and a change since your read may target the wrong "
                "section. Omit to skip the check (today's behaviour)."
            )
        ),
    ] = None,
) -> dict:
    """Insert a card into one section of a `type: sections` view, leaving every other view intact.

    Resolves the view via `_load_view`, requires it to be a `sections`
    view, inserts `card_config` into that section's `cards` list at
    `position` (or the end), and saves the whole dashboard back. This is
    the only way to make a card visible on a sections view —
    `dashboards_add_card_to_view` writes to `view.cards`, which such a
    view renders only in edit mode. When `expected_config_hash` is given,
    it is checked against the dashboard's hash right after the read and
    before any change, so a stale caller never overwrites the wrong
    section.

    Use when: adding a card to a specific section of a `sections` view.
    Pass `expected_config_hash` whenever `section_index`/`position` came
    from an earlier read that might now be stale.
    Not for: a `masonry`/`panel` view — use
    `dashboards_add_card_to_view`.
    Returns: `{status: "added", url_path, view_index, view_title,
    section_index, card_index, cards_in_section, config_hash}` —
    `config_hash` is re-read from HA after the save (not the locally built
    dict), ready to chain into the next call's `expected_config_hash`.
    Errors: raises `ValueError` when `card_config` has no `type` key, the
    view can't be resolved, isn't a `sections` view, or `section_index` is
    out of range. Returns `{"error": "config_changed", "expected_config_hash",
    "current_config_hash", "action"}` without saving when
    `expected_config_hash` doesn't match.
    """
    if not isinstance(card_config, dict) or not card_config.get("type"):
        raise ValueError("card_config must be a dict with a 'type' key, e.g. {'type': 'tile', ...}")

    config, save_url_path, idx, view = _load_view(url_path, view_index)
    mismatch = _check_expected_hash(config, expected_config_hash)
    if mismatch is not None:
        return mismatch
    sections = _require_sections(view, idx)
    section = _require_section(sections, section_index, idx)

    cards = section["cards"]
    at = len(cards) if position is None else max(0, min(position, len(cards)))
    cards.insert(at, card_config)
    new_hash = _save_and_hash(config, save_url_path)

    return _result(
        "added",
        save_url_path,
        idx,
        view,
        section_index=section_index,
        card_index=at,
        cards_in_section=len(cards),
        config_hash=new_hash,
    )


@mcp.tool(annotations=destructive("Update card in dashboard section", idempotent=False))
def update_card_in_section(
    url_path: Annotated[
        str | None,
        Field(description="Dashboard url_path, or a view path in the default dashboard (e.g. 'living-room')."),
    ],
    section_index: Annotated[int, Field(description="Zero-based section index, from dashboards_get_view_sections.")],
    card_index: Annotated[int, Field(description="Zero-based card index within the section, from dashboards_get_view_sections/inspection.")],
    card_config: Annotated[
        dict,
        Field(description="Card fields to apply. With merge=true, a partial update (only these keys change); with merge=false, the full replacement card (needs a 'type' key)."),
    ],
    view_index: Annotated[
        int | None,
        Field(description="Zero-based view index; needed only when url_path names a dashboard rather than a view path."),
    ] = None,
    merge: Annotated[
        bool,
        Field(description="If true (default), merge card_config's keys into the existing card. If false, replace the card wholesale."),
    ] = True,
    expected_config_hash: Annotated[
        str | None,
        Field(
            description=(
                "Optional guard: the config_hash from a recent dashboards_get_view_sections "
                "or section-tool response. If the dashboard's current hash differs, this call "
                "returns 'config_changed' and saves nothing, since section_index/card_index are "
                "positions, not stable ids, and a change since your read may target the wrong "
                "card. Omit to skip the check (today's behaviour)."
            )
        ),
    ] = None,
) -> dict:
    """Change one existing card inside a section of a `type: sections` view.

    Resolves the view and target card via `_load_view`/`_require_card`,
    then merges `card_config`'s keys into the existing card (`merge=True`,
    the default) or replaces it outright (`merge=False`), and saves the
    whole dashboard back. The previous card is only recoverable via
    `card_before` in this call's own response — storage itself keeps no
    history. With `expected_config_hash`, the dashboard's hash is checked
    right after the read and before any change, so a stale caller can't
    edit the wrong card.

    Use when: tweaking or replacing one card already placed in a section.
    Pass `expected_config_hash` when `section_index`/`card_index` came
    from a read that might now be stale.
    Not for: adding a new card — use
    `dashboards_add_card_to_section`; removing one — use
    `dashboards_remove_card_from_section`.
    Returns: `{status: "updated", url_path, view_index, view_title,
    section_index, card_index, card_before, card_after, config_hash}` —
    `config_hash` is re-read from HA after the save, for the next call's
    `expected_config_hash`.
    Errors: raises `ValueError` when `card_config` is empty, `merge=False`
    lacks a `type` key, or the view/section/card can't be resolved.
    Returns `{"error": "config_changed", ...}` without saving when
    `expected_config_hash` doesn't match.
    """
    if not isinstance(card_config, dict) or not card_config:
        raise ValueError("card_config must be a non-empty dict")
    if not merge and not card_config.get("type"):
        raise ValueError("A replacement card_config needs a 'type' key")

    config, save_url_path, idx, view = _load_view(url_path, view_index)
    mismatch = _check_expected_hash(config, expected_config_hash)
    if mismatch is not None:
        return mismatch
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
    new_hash = _save_and_hash(config, save_url_path)

    return _result(
        "updated",
        save_url_path,
        idx,
        view,
        section_index=section_index,
        card_index=card_index,
        card_before=before,
        card_after=new_card,
        config_hash=new_hash,
    )


@mcp.tool(annotations=destructive("Remove card from dashboard section", idempotent=False))
def remove_card_from_section(
    url_path: Annotated[
        str | None,
        Field(description="Dashboard url_path, or a view path in the default dashboard (e.g. 'living-room')."),
    ],
    section_index: Annotated[int, Field(description="Zero-based section index, from dashboards_get_view_sections.")],
    card_index: Annotated[int, Field(description="Zero-based card index within the section, from dashboards_get_view_sections/inspection.")],
    view_index: Annotated[
        int | None,
        Field(description="Zero-based view index; needed only when url_path names a dashboard rather than a view path."),
    ] = None,
    expected_config_hash: Annotated[
        str | None,
        Field(
            description=(
                "Optional guard: the config_hash from a recent dashboards_get_view_sections "
                "or section-tool response. If the dashboard's current hash differs, this call "
                "returns 'config_changed' and removes nothing — this is what makes a repeated "
                "call with a now-stale hash safe instead of deleting whatever shifted into that "
                "position. Omit to skip the check (today's behaviour)."
            )
        ),
    ] = None,
) -> dict:
    """Delete one card from a section of a `type: sections` view.

    Resolves the view and section via `_load_view`/`_require_section`,
    pops the card at `card_index` from that section's `cards` list, and
    saves the whole dashboard back. `card_index` is a **position**, not a
    stable id — repeating this call with the same `section_index`/
    `card_index` after a first successful removal deletes whatever card
    has shifted into that position next, not a no-op, unless
    `expected_config_hash` is also passed and now points at the pre-removal
    hash, in which case the repeat is rejected with `config_changed`
    instead of deleting the wrong card.

    Use when: removing a card that's no longer wanted on a sections view.
    Pass `expected_config_hash` whenever `section_index`/`card_index` came
    from an earlier read that might now be stale.
    Returns: `{status: "removed", url_path, view_index, view_title,
    section_index, card_index, removed_card, cards_in_section,
    config_hash}` — `removed_card` can be passed back to
    `dashboards_add_card_to_section` to restore it; `config_hash` is
    re-read from HA after the save, ready to chain into the next call's
    `expected_config_hash`.
    Errors: raises `ValueError` when the view/section/card can't be
    resolved (bad index/path). Returns `{"error": "config_changed",
    "expected_config_hash", "current_config_hash", "action"}` without
    saving when `expected_config_hash` doesn't match.
    """
    config, save_url_path, idx, view = _load_view(url_path, view_index)
    mismatch = _check_expected_hash(config, expected_config_hash)
    if mismatch is not None:
        return mismatch
    sections = _require_sections(view, idx)
    section = _require_section(sections, section_index, idx)
    _require_card(section, card_index, section_index)

    removed = section["cards"].pop(card_index)
    new_hash = _save_and_hash(config, save_url_path)

    return _result(
        "removed",
        save_url_path,
        idx,
        view,
        section_index=section_index,
        card_index=card_index,
        removed_card=removed,
        cards_in_section=len(section["cards"]),
        config_hash=new_hash,
    )


@mcp.tool(annotations=write("Add section to dashboard view", idempotent=False))
def add_section_to_view(
    url_path: Annotated[
        str | None,
        Field(description="Dashboard url_path, or a view path in the default dashboard (e.g. 'living-room')."),
    ],
    section_config: Annotated[
        dict | None,
        Field(description="Section body, e.g. {'cards': [{'type': 'heading', 'heading': 'Living Room'}]}. Omit for an empty grid section."),
    ] = None,
    view_index: Annotated[
        int | None,
        Field(description="Zero-based view index; needed only when url_path names a dashboard rather than a view path."),
    ] = None,
    position: Annotated[
        int | None,
        Field(description="Zero-based index within the view to insert the section at. Omit to append at the end."),
    ] = None,
    expected_config_hash: Annotated[
        str | None,
        Field(
            description=(
                "Optional guard: the config_hash from a recent dashboards_get_view_sections "
                "or section-tool response. If the dashboard's current hash differs, this call "
                "returns 'config_changed' and saves nothing, since position is a plain index "
                "and a change since your read may insert the new section somewhere unintended. "
                "Omit to skip the check (today's behaviour)."
            )
        ),
    ] = None,
) -> dict:
    """Insert a new section into a `type: sections` view.

    Resolves the view via `_load_view`, requires it to be a `sections`
    view, defaults `section_config` to `{"type": "grid", "cards": []}`
    when omitted, inserts it into that view's `sections` list at
    `position` (or the end), and saves the whole dashboard back. When
    `expected_config_hash` is given, it is checked against the dashboard's
    hash right after the read and before any change.

    Use when: adding a whole new section (a card group with its own grid)
    to a sections view, before populating it with
    `dashboards_add_card_to_section`. Pass `expected_config_hash` whenever
    `position` came from an earlier read that might now be stale.
    Returns: `{status: "added", url_path, view_index, view_title,
    section_index, sections_in_view, config_hash}` — `config_hash` is
    re-read from HA after the save, ready to chain into the next call's
    `expected_config_hash`.
    Errors: raises `ValueError` when `section_config["cards"]` isn't a
    list, or the view can't be resolved/isn't a `sections` view. Returns
    `{"error": "config_changed", "expected_config_hash",
    "current_config_hash", "action"}` without saving when
    `expected_config_hash` doesn't match.
    """
    section = dict(section_config or {})
    if not isinstance(section.get("cards", []), list):
        # ValueError (not TypeError) matches this file's established, tested
        # convention: every input-validation failure in dashboards.py raises
        # ValueError (see e.g. the "not a 'sections' view" check covered by
        # tests/test_dashboards_sections.py), and the docstring above publicly
        # documents `raises ValueError` as this function's contract.
        raise ValueError("section_config['cards'] must be a list of card configs")  # noqa: TRY004
    section.setdefault("type", "grid")
    section.setdefault("cards", [])

    config, save_url_path, idx, view = _load_view(url_path, view_index)
    mismatch = _check_expected_hash(config, expected_config_hash)
    if mismatch is not None:
        return mismatch
    sections = _require_sections(view, idx)

    at = len(sections) if position is None else max(0, min(position, len(sections)))
    sections.insert(at, section)
    new_hash = _save_and_hash(config, save_url_path)

    return _result(
        "added",
        save_url_path,
        idx,
        view,
        section_index=at,
        sections_in_view=len(sections),
        config_hash=new_hash,
    )


@mcp.tool(annotations=read("Screenshot a Lovelace dashboard view"))
def screenshot(
    url_path: Annotated[
        str | None,
        Field(
            description=(
                "Frontend route to capture. A registered dashboard's own url_path "
                "(e.g. 'dashboard-porch', optionally with a view path appended: "
                "'dashboard-porch/porch-tests') is used as-is. A path already "
                "starting with 'lovelace' (e.g. 'lovelace/0') is also used as-is. "
                "Anything else is treated as a view of the default dashboard and "
                "gets 'lovelace/' prepended (e.g. 'living-room' -> "
                "'lovelace/living-room'). Omit for the default dashboard's root."
            )
        ),
    ] = None,
    width: Annotated[int, Field(description="Viewport width in pixels. Defaults to 1280.")] = 1280,
    height: Annotated[
        int,
        Field(description="Viewport height in pixels. Ignored when full_page=True. Defaults to 800."),
    ] = 800,
    wait_ms: Annotated[
        int,
        Field(description="Settle time in milliseconds after page load before capturing — increase for heavy chart cards. Defaults to 2500."),
    ] = 2500,
    full_page: Annotated[
        bool,
        Field(description="If true, use a fixed 4096px-tall viewport instead of height, approximating a full-scroll capture. Defaults to false."),
    ] = False,
    zoom: Annotated[float, Field(description="Browser zoom factor applied by the engine. Defaults to 1.0 (no zoom).")] = 1.0,
) -> dict:
    """Capture a PNG screenshot of one Lovelace dashboard view via the Puppet add-on engine.

    Resolves `url_path` to a concrete frontend route (see the parameter
    description), rejects paths containing unsafe characters, then calls
    the Puppet engine (discovered via Supervisor or
    `NEXUS_SCREENSHOT_ENGINE_URL`) over HTTP with the resolved viewport/
    zoom/wait parameters and returns its PNG response as base64. Deciding
    between a dashboard route and a default-dashboard view path costs one
    `lovelace/dashboards/list` call (skipped when `url_path` already
    starts with `lovelace`). Reaches only a local/LAN add-on, not the
    public internet.

    Use when: visually verifying a dashboard or view after editing it.
    Returns: `{url_path, engine, format: "png", width, height,
    image_base64, size_bytes}` on success — `url_path` echoes the
    resolved route, not the raw argument, and `image_base64` can be large,
    especially with `full_page=True` (fixed 4096px height).
    Errors: `{"error": "dashboard_list_unavailable" | "invalid_path" |
    "engine_not_available" | "engine_unreachable" | "engine_http_<code>" |
    "empty_response", "detail": ..., ...}` when the dashboard list can't
    be fetched, the path is unsafe, the Puppet engine isn't installed/
    started, or the engine request itself fails.
    Limits: 60-second engine timeout; `full_page=True` only approximates
    full-scroll capture since the engine does not measure actual page
    height, so it can crop tall pages or leave blank space on short ones.
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
