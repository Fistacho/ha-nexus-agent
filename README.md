# Nexus Agent — MCP for Home Assistant

[![Release](https://img.shields.io/github/v/release/Fistacho/ha-nexus-agent?style=flat-square&label=latest)](https://github.com/Fistacho/ha-nexus-agent/releases/latest)
[![HA Add-on](https://img.shields.io/badge/Add--on-available-41BDF5?style=flat-square&logo=home-assistant)](https://github.com/Fistacho/ha-nexus-agent#installation--home-assistant-add-on-recommended)
[![Custom Repository](https://img.shields.io/badge/Home%20Assistant-custom_repository-41BDF5?style=flat-square&logo=home-assistant)](https://github.com/Fistacho/ha-nexus-agent#manual-installation-add-on-store)
[![License](https://img.shields.io/github/license/Fistacho/ha-nexus-agent?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Fistacho/ha-nexus-agent?style=flat-square)](https://github.com/Fistacho/ha-nexus-agent/stargazers)

**Give AI assistants full control over your smart home.** **323 tools across 29 domains** — entities, automations & scripts (CRUD + traces + linter + live reference validator), **scene CRUD**, dashboards + screenshot + resource management, energy, **long-term statistics** (sum/mean/min/max by day/week/month), voice pipelines, blueprints, calendar, HACS, Supervisor, **ESPHome** (list devices, compile, OTA, logs, **LVGL display UI management**), themes, **self-documenting Card Builder** (visual cards, recipe builder, embedded block schema, upstream sync), **aggregated snapshot** (one-call context), **BM25 tool search**, HA-aware YAML validation, git versioning, and more.

Works with **Claude Code**, **Claude Desktop**, **VS Code**, **Cursor**, **Windsurf**, **OpenAI Codex CLI**, **Gemini CLI**.

---

## What can you ask?

Once connected, just talk to your AI assistant:

- *"Turn off all lights in the house"*
- *"Create an automation: alert me when the front door opens after 10 PM"*
- *"Why is my bedroom sensor showing unavailable?"*
- *"Take a screenshot of my main dashboard"*
- *"Show all pending Home Assistant updates"*
- *"Install Mushroom Cards from HACS"*
- *"Commit my config changes to git with a summary of what changed"*
- *"Build me a Lovelace card for the living room with temperature and humidity"*

---

## What's New in v0.23.0

- **ESPHome compile/validate/OTA upload work in the add-on** — routed through the
  ESPHome Device Builder add-on's own trusted-peer ingress site, the same path Home
  Assistant Core itself uses to reach it. Standalone installs need
  `ESPHOME_DASHBOARD_URL` set explicitly; the add-on discovers it automatically via
  the Supervisor.
- **The add-on now uses `host_network: true`** so it can reach that trusted-peer site
  at all — see [Network (add-on)](#network-add-on) below for what this means for your
  Network tab settings, and note the add-on's security rating drops by one point as a
  result.
- **Stable `esphome_*` error codes** (`esphome_not_configured`, `esphome_unreachable`,
  `esphome_auth_required`, `esphome_protocol_error`, `esphome_command_failed`,
  `esphome_unsupported_option`, `timeout`) with a `diagnosis` block on
  connectivity/auth failures instead of ad-hoc messages.
- **`esphome_upload_device` requires `confirm=True`** — OTA-flashing a device is not
  undoable the way a config edit is; the first call without it does no I/O.
- **SSRF guard on `card_builder_upload_image_from_url`** — resolves the hostname once,
  rejects loopback/link-local/reserved destinations, and pins every redirect hop to the
  already-resolved IP, closing the DNS-rebinding window. More relevant than before:
  under `host_network`, a loopback request from this add-on now looks like it came from
  Home Assistant Core to some ingress-fronted add-ons.
- Full details in [CHANGELOG.md](CHANGELOG.md#0230).

## What's New in v0.22.1

Includes everything from 0.22.0 below — 0.22.0 could not be built by the real Home
Assistant Supervisor (a `build.yaml` regression) and should not be used; install
0.22.1 directly. **Fixed:** the add-on image failed to build under the Supervisor
(`update.install` error) — reverted to a literal `FROM python:3.12-alpine` in the
Dockerfile (no `ARG BUILD_FROM`) and removed `build.yaml`, matching every release
through 0.21.0. Full details in [CHANGELOG.md](CHANGELOG.md#0221).

## What's New in v0.22.0

- **`read_only` and `disabled_namespaces` add-on options** — a serverside policy that hides
  *and refuses* non-read-only tools (or whole namespaces) at the MCP protocol level, not
  just from `tools/list`. See [Add-on Options](#add-on-options) / [Security](#security).
- **`confirm` gate on `supervisor_delete_backup` and `files_delete_config_file`** — the
  first call without `confirm=True` now does no I/O and returns a confirmation prompt
  instead of deleting immediately.
- **Safer section-by-section dashboard edits** — `expected_config_hash`/`config_hash` on
  the dashboard section tools detect a stale read before a write can land on the wrong
  card.
- **`GET /health` trimmed to `{"status": "ok"}`** — it no longer leaks `ha_url`, the tool
  count or the active policy to unauthenticated LAN callers.
- **ESPHome fixes** — `esphome_clean_mqtt` now works through HA's own `mqtt` integration;
  device online/offline status no longer depends on a `binary_sensor.*_api_connection_status`
  entity that isn't guaranteed to exist; an unreachable dashboard now comes with a
  Supervisor-based diagnosis instead of a bare connection error.
- Full details in [CHANGELOG.md](CHANGELOG.md#0220).

## What's New in v0.20.0

- **Section-aware Lovelace editing** — `dashboards_get_view_sections`, `dashboards_add_card_to_section`,
  `dashboards_update_card_in_section`, `dashboards_remove_card_from_section`, `dashboards_add_section_to_view`.
  A view with `type: sections` renders **only** `view.sections`; cards written to `view.cards` show up
  exclusively in edit mode as "imported cards" (`hui-sections-view.ts`), so `dashboards_add_card_to_view`
  could never place a card on a modern dashboard. These tools do the read-modify-write server-side, so a
  16-view / 140 kB config never has to round-trip through `dashboards_save_dashboard_config`.
  `url_path` accepts a view path (`'lukasz2'`), and non-`sections` views are rejected with a clear message.
- **Tool count corrected** — the manifest claimed 325 while 317 were registered; the real number is now
  measured from the registry (322 with the five new tools).
- **Audit fixes** — a follow-up review checked every registered tool against the live Home Assistant API
  and fixed ~20 of them that called nonexistent endpoints or held stale logic: WebSocket
  `render_template`/`listen_state_changes`, backup service selection, `integrations_*` config/options
  flow via REST, entity exposure, light `color_temp_kelvin`, zones, energy preferences, calendar event
  deletion, ESPHome add-on slug discovery + compile/upload/validate, energy statistics units. Full list
  in [CHANGELOG.md](CHANGELOG.md).
- **Security hardening** — path containment for `files_*`/`esphome_*`/`git_rollback_file`, SVG upload
  sanitization and stricter media upload limits in `card_builder_*`.
- **New `blueprints_save_blueprint`** — persists blueprint YAML fetched by `import_blueprint`, which only
  ever downloaded and validated. Tool count corrected to **323**.

## What's New in v0.16.0

- **`system_get_updates`** — list pending HA updates (core, add-ons, HACS, custom components) with version info and release URLs
- **`system_get_system_health`** — health check of all HA subsystems (recorder, network, cloud, etc.)
- **`system_get_repairs`** — list active repair issues that require attention
- **Entity groups CRUD** — `automations_list_groups`, `automations_set_group`, `automations_remove_group` — create/update/delete `group.*` entities via `group.set`
- **Live automation reference validator** — `automations_validate_automation_references` cross-checks every `entity_id` and `service` in your YAML against the live HA registry; template values skipped automatically
- **Lovelace resource management** — `dashboards_add_dashboard_resource`, `dashboards_remove_dashboard_resource`, `dashboards_update_dashboard_resource` — manage custom JS/CSS resources without touching YAML

## What's New in v0.15.0

- **Pagination + field selection** — `list_entities` and `get_snapshot` now accept `limit`, `offset`, and `fields`/`state_fields`
- **Confirmation gates** on all destructive operations — `restart_ha`, `stop_ha`, `git_rollback_*`, `delete_automation`, `delete_script`, `remove_integration` all require `confirm=True`
- **Automation best-practice linter** — `automations_validate_best_practices` statically checks YAML for 7 common mistakes
- **Dashboard screenshot** via Puppet engine — `dashboards_screenshot` renders any Lovelace view to PNG

---

## Installation — Home Assistant Add-on (Recommended)

[![Open your Home Assistant instance and add the repository.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FFistacho%2Fha-nexus-agent)

1. Click the **Open Add-on Repository on MY** button above
2. Find **Nexus Agent** in the Add-on Store → **Install** → **Start**
3. Open **Web UI** — copy your MCP URL and paste it into your AI client

---

## Manual Installation (Add-on Store)

If the MY button does not work for your setup:

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add:

   ```text
   https://github.com/Fistacho/ha-nexus-agent
   ```

4. Find **Nexus Agent** → **Install** → **Start** → **Open Web UI**

The web UI shows your API key and generates ready-to-paste config for every MCP client.

---

## Standalone (outside HA)

```bash
git clone https://github.com/Fistacho/ha-nexus-agent
cd ha-nexus-agent
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set HA_URL and HA_TOKEN
python server.py
```

Open <http://localhost:7123> to get your API key and MCP client configs.

### Getting a Home Assistant token

1. In HA: **Profile → Security → Long-Lived Access Tokens**
2. **Create Token** → name it `nexus`
3. Paste as `HA_TOKEN` in `.env`

---

## Connecting MCP Clients

Open Nexus with the **Open Web UI** button on the add-on's page (Settings → Add-ons → Nexus Agent) after starting it — that page shows your real API key and the exact, ready-to-paste config for every client below. This works over HA's ingress proxy whether or not you've also enabled **Show in sidebar** on that same add-on page — the sidebar entry is just a shortcut to the same URL. Outside of ingress (or without a valid `Authorization: Bearer` header), the same page hides the key.

The MCP endpoint is a **Streamable HTTP** transport (not SSE) at:

```text
http://your-ha-ip:7123/mcp
```

Authenticate with an `Authorization: Bearer YOUR_API_KEY` header wherever the client supports it — **prefer this over the `?token=` query string**, which is kept only for backward compatibility, ends up in plaintext in HTTP access logs and shell history, and will be removed entirely in nexus **1.0.0** (using it logs a one-time deprecation warning per add-on start, without ever logging the key itself).

### Claude Code CLI

```bash
claude mcp add nexus --transport http "http://your-ha-ip:7123/mcp" --header "Authorization: Bearer YOUR_API_KEY" --scope user
```

(`--transport http`, not `sse` — Nexus speaks Streamable HTTP. Syntax per the [Claude Code MCP docs](https://code.claude.com/docs/en/mcp).)

### Gemini CLI

```bash
gemini mcp add --transport http --header "Authorization: Bearer YOUR_API_KEY" nexus "http://your-ha-ip:7123/mcp"
```

### OpenAI Codex CLI

```bash
codex mcp add nexus --url "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY"
```

> Codex's `mcp add` has no `--header` flag for HTTP servers, so this falls back to the query-token URL (logged in plaintext). For header-based auth instead, edit `~/.codex/config.toml` directly, e.g.:
>
> ```toml
> [mcp_servers.nexus]
> url = "http://your-ha-ip:7123/mcp"
> bearer_token_env_var = "NEXUS_API_KEY"
> ```

### VS Code

Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "nexus": {
      "type": "http",
      "url": "http://your-ha-ip:7123/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "url": "http://your-ha-ip:7123/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "url": "http://your-ha-ip:7123/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### Claude Desktop

Add to `%APPDATA%/Claude/claude_desktop_config.json` (Win) or `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python",
      "args": ["server.py"],
      "cwd": "/path/to/ha-nexus-agent",
      "env": {
        "HA_URL": "http://homeassistant.local:8123",
        "HA_TOKEN": "your_ha_token_here"
      }
    }
  }
}
```

> **Tip:** Copy the exact config with your real key from the **Open Web UI** button on the add-on's page (ingress). The same URL outside of ingress hides the key — see [Security](#security).

---

## Tools

323 tools across 29 categories:

| Category | Tools | Highlights |
| --- | --- | --- |
| `entities_*` | 18 | list (paginated + field selection), turn on/off/toggle, **bulk_control**, voice expose, set_value |
| `services_*` | 19 | call_service, notify, light color, camera snapshot/record, media controls |
| `automations_*` | 31 | CRUD + full YAML, traces, scripts, scenes, **scene CRUD** (`get/set/delete_scene_config`), **validate_best_practices** (static linter), **validate_automation_references** (live registry check), **list/set/remove groups**, confirm gates on delete |
| `blueprints_*` | 5 | list, import from URL, **save** (persist imported YAML), delete, instantiate |
| `areas_*` | 8 | list, create, get_states, **control_area** |
| `devices_*` | 4 | list, update (rename/move/disable), remove |
| `calendar_*` | 4 | list calendars/events, create/delete event |
| `todo_*` | 5 | list, add/update/remove items |
| `helpers_*` | 11 | input_boolean/number/text/select/datetime, timers, counters |
| `history_*` | 5 | state history, logbook, error log, system info |
| `system_*` | 12 | check_config, backup, **restart/stop** (confirm gate), **get_updates**, **get_system_health**, **get_repairs** |
| `dashboards_*` | 15 | get/save config, add cards/views, **section-aware editing** of `type: sections` views (list sections, add/update/remove cards in a section, add section), **screenshot** (Puppet), **add/remove/update resources** (JS/CSS) |
| `files_*` | 6 | read/write config files, YAML validation (`!include`, `!secret`) |
| `git_*` | 11 | commit, **rollback** (confirm gate), log, **safe_write_with_checkpoint** |
| `ws_*` | 7 | listen state changes, events, subscribe_trigger, render_template |
| `supervisor_*` | 20 | add-on install/start/stop/update/logs/stats, backups, core/host info |
| `hacs_*` | 7 | list/install/uninstall/update HACS repos, critical updates |
| `energy_*` | 9 | grid, solar, battery sources, energy preferences |
| `zones_*` | 8 | create/update/delete zones, person location |
| `labels_*` | 14 | labels, categories, assign to entities/devices |
| `search_*` | 7 | fuzzy search, orphan devices, unused entities, deep_search |
| `integrations_*` | 13 | **config_flow** (install like in UI), options flow CRUD, enable/disable, **remove** (confirm gate) |
| `voice_*` | 10 | Assist pipelines CRUD, STT/TTS/wake-word engines |
| `themes_*` | 8 | list/create/update/delete Lovelace themes |
| `card_builder_*` | 38 | Cards CRUD, style presets, CSS properties, media, renderer config, **embedded block schema** (`list_block_types`, `get_block_schema`, `list_button_toggle_features`), **embedded styles knowledge** (`list_style_categories`, `list_style_targets`, `list_style_snippets`, `build_styles`), **10 turnkey templates** (`make_template_card`), **`build_from_recipe`** high-level builder, **`validate_config`**, **`check_schema_sync`**, upload SVG/media/image-from-url, design patterns, design principles |
| `snapshot_*` | 2 | **Aggregated one-call context** — states + areas + devices + entities + integrations, domain/area/field filters, pagination |
| `esphome_*` | 18 | list devices + online status, **read / write** config YAML, get entities, **compile / validate / OTA upload** via Dashboard API, add-on logs, ping; **LVGL**: list LVGL devices, get pages/widgets/styles, **client-side validate** (unique IDs, page refs), add/delete widgets |
| `statistics_*` | 4 | **long-term recorder statistics** — list IDs, get sum/mean/min/max by hour/day/week/month, **`get_energy_statistics`** (auto-discovers kWh/m³ sensors) |
| `discover_*` | 4 | **BM25 tool search** — query the tool catalogue, list namespaces, fetch full docstrings |

---

## Features

- **323 MCP tools** across 29 categories — the most complete HA MCP server available
- **Built-in tool search** — `discover_tool_search("query")` finds the right tool without flooding the AI's context
- **Confirmation gates** — all destructive operations require `confirm=True`; without it they return the exact command to re-run
- **Automation linter** — `automations_validate_best_practices` catches 7 common YAML mistakes before they cause issues
- **Live reference validator** — `automations_validate_automation_references` cross-checks every entity_id and service call against the running HA instance
- **Updates monitor** — `system_get_updates` lists all pending updates across core, add-ons and HACS
- **Repair issues** — `system_get_repairs` surfaces active issues from HA's repair centre
- **Lovelace resources** — add/remove/update custom JS modules and CSS without editing YAML
- **Dashboard screenshot** — render any Lovelace view to PNG via the Puppet engine (see below)
- **Real-time WebSocket** — subscribe to state changes, events and triggers live
- **Git versioning** — every config change auto-committed, instant rollback, `safe_write_with_checkpoint`
- **YAML validation** before writing any config file (`!include`, `!secret` aware)
- **Setup web UI** — generates ready-to-use MCP config for every client
- **HA Add-on native** — one-click install, no manual token setup
- **API key auth** — `/mcp` requires the key via `Authorization: Bearer` (recommended) or the legacy `?token=` query string
- **Ingress-gated Setup UI** — the API key and one-click client config are only shown to Home Assistant's ingress proxy or a valid Bearer token; see [Security](#security)

---

## Security

- **Setup UI (`GET /`) and `POST /regenerate`** only reveal the API key / accept a key reset from callers Home Assistant has already authenticated:
  - via the **HA ingress proxy** (the **Open Web UI** button on the add-on's page, or the Nexus sidebar entry if you've enabled **Show in sidebar**) — this is the intended way to open Nexus;
  - via a valid `Authorization: Bearer <API_KEY>` header — for scripted/CLI access;
  - in **standalone mode only** (no Supervisor, e.g. `.env` deployment) — from `localhost`, since there is no ingress proxy to authenticate the caller there.

  Any other caller gets the page with the key, tool count and active policy omitted (or `403` for `/regenerate`).
- **`GET /health` is intentionally unauthenticated and minimal** — it returns only `{"status": "ok"}`, nothing else, because the Supervisor watchdog that polls it only needs a 200 response. The tool count, `ha_url` and active `read_only`/`disabled_namespaces` policy used to leak here to anyone on the LAN; they now live only on the ingress-gated Setup UI page above.
  - The Supervisor **Watchdog** itself is **off by default** and must be turned on with the **Watchdog** toggle on the add-on's **Info** tab — listing `watchdog:` in the add-on's own manifest does not enable it by itself.
- **`/mcp` accepts `Authorization: Bearer <API_KEY>`** (recommended) or `?token=<API_KEY>` in the query string (kept for backward compatibility only, **removed in nexus 1.0.0** — it ends up in HTTP access logs and shell/browser history; prefer the header wherever your client supports it, see [Connecting MCP Clients](#connecting-mcp-clients)). Using the query-string form logs one deprecation warning per add-on start (never the key itself).
- **Access logs never contain the raw key** — the add-on redacts `?token=...` in uvicorn's access log before it's written.
- **Startup logs never print the raw key** — only the path of the file it's stored in (`/config/.nexus_api_key`).
- **Tool-exposure policy** (`read_only`, `disabled_namespaces`) — see [Add-on Options](#add-on-options) — narrows what a client can see/call at the MCP protocol level itself: a hidden tool is refused at `tools/call`, not merely omitted from `tools/list`. `read_only` does not restrict what an already-visible read tool can read (e.g. `files_read_config_file`, `git_log`, `supervisor_get_addon_logs`); combine with `disabled_namespaces` to also narrow that.
- Found a vulnerability? See [SECURITY.md](SECURITY.md) for how to report it privately.

---

## Add-on Options

| Option | Default | Description |
| --- | --- | --- |
| `port` | `7123` | TCP port for the MCP endpoint and Setup UI |
| `log_level` | `info` | Add-on's own log verbosity (`debug`\|`info`\|`warning`\|`error`) |
| `git_versioning_auto` | `true` | Auto-commit `/config` changes made through nexus's file/git tools |
| `max_backups` | `30` | Supervisor backups nexus keeps before pruning the oldest |
| `api_key` | *(auto-generated)* | Pin the MCP API key instead of letting nexus generate one |
| `read_only` | `false` | Hide and refuse every tool that is not read-only (ADR-0003), and every MCP prompt. See [Security](#security) for what it does *not* restrict. Requires a restart. |
| `disabled_namespaces` | `[]` | List of tool namespaces (e.g. `["supervisor", "git"]`) to hide and refuse entirely. An unknown namespace name refuses to start with a readable error in the add-on log. Requires a restart. |

`read_only` and `disabled_namespaces` combine as a union — a tool hidden by either one is hidden. Both are evaluated once at startup; there is no per-session or per-client variant.

## Network (add-on)

Since 0.23.0 the add-on runs with `host_network: true` (ADR-0004) — the only way it can
reach the ESPHome Device Builder add-on's trusted-peer ingress site at
`127.0.0.1:<its ingress_port>`, the same path Home Assistant Core itself uses to reach
it. This is a deliberate trade-off, not a default anyone should ignore: the add-on's
Supervisor security rating drops by one point as a result, since a host-networked
container can reach the host's own loopback interface, not just its own network
namespace.

Practically, this changes how the `port` option is enforced. Docker no longer publishes
`ports:` from `config.yaml` — nexus reconstructs your choice itself at startup from the
add-on's **Network** tab (Configuration tab, or Info tab → Network):

- **Default (`7123`)** — nexus binds `0.0.0.0:7123`; MCP + Setup UI are reachable on the
  LAN at that port, same as before 0.23.0.
- **Remapped to another port** — nexus binds the LAN socket there instead;
  `ingress_port` stays fixed at `7123` regardless, so Home Assistant's own ingress
  ("Open Web UI") keeps working either way.
- **Disabled/unmapped** — no LAN socket at all; only Home Assistant ingress can reach
  nexus. This is the only way to turn the LAN endpoint off entirely.

Do not try to work around any of this by editing the `port` *option's* value below —
it is scheduled for removal in 1.0.0 and only ever controls the LAN socket described
above.

## Dashboard Screenshots

`dashboards_screenshot` renders any Lovelace view to a base64-encoded PNG by delegating to the **Puppet** headless Chromium add-on. Nexus itself contains no browser dependencies — this approach works on every architecture (amd64, aarch64, armv7, armhf).

### Setup

1. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**  
   Add: `https://github.com/balloob/home-assistant-addons`
2. Install **Puppet**, set its `access_token` option to a HA long-lived access token, then start it
3. Done — Nexus discovers Puppet automatically via the Supervisor

**Docker / standalone:**

```bash
# Run the Puppet container as a sidecar, then point Nexus at it:
NEXUS_SCREENSHOT_ENGINE_URL=http://puppet:10000
```

### Usage

```python
dashboards_screenshot(url_path="caly-dom", width=1280, height=800, wait_ms=3000)
dashboards_screenshot(url_path="lovelace/0", full_page=True)   # full scrollable page
```

Returns `{"image_base64": "...", "format": "png", "size_bytes": ...}`.

---

## Git Versioning

Nexus keeps a git history of your HA config directory. Before every risky change, use `git_safe_write_with_checkpoint` — it commits current state first, then applies the change.

All rollback operations require `confirm=True` to prevent accidental data loss:

```python
git_init_config()
git_safe_write_with_checkpoint("automations.yaml", new_content)
git_rollback_file("automations.yaml", confirm=True)        # undo single file
git_rollback_to_commit("abc1234", confirm=True)            # full rollback
git_log(limit=10)                                          # see history
```

---

## Automation Linter

`automations_validate_best_practices` checks YAML against 7 rules before you save:

| Rule | Severity | What it catches |
| --- | --- | --- |
| `state_trigger_no_for` | ⚠️ warning | State trigger without `for:` duration — fires on every flicker |
| `no_alias` | ⚠️ warning | Missing `alias:` — hard to find in logs |
| `missing_mode` | ℹ️ info | No `mode:` declared — silently defaults to `single` |
| `triggers_without_ids` | ℹ️ info | Multiple triggers without `id:` — breaks `trigger.id` conditions |
| `deprecated_service_key` | ℹ️ info | `service:` in actions — use `action:` (HA 2024.8+) |
| `no_description` | ℹ️ info | No `description:` field |
| `restart_mode_caution` | ℹ️ info | `mode: restart` — can cause mid-run side-effects |

```python
automations_validate_best_practices(yaml_content="""
alias: Turn off lights
trigger:
  - platform: state
    entity_id: binary_sensor.motion
    to: "off"
action:
  - service: light.turn_off
    target:
      entity_id: light.living_room
""")
# → {"warnings": 2, "infos": 1, "issues": [...]}
```

---

## Environment Variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HA_URL` | Yes | `http://homeassistant.local:8123` | Home Assistant URL |
| `HA_TOKEN` | Standalone only | — | Long-lived access token |
| `SUPERVISOR_TOKEN` | Add-on only | auto-injected | Set automatically by HA |
| `HA_CONFIG_PATH` | For git/file tools | `/config` | Path to HA config directory |
| `NEXUS_API_KEY` | No | auto-generated | Pin to a specific API key |
| `NEXUS_PORT` | No | `7123` | HTTP server port |
| `NEXUS_SCREENSHOT_ENGINE_URL` | No | auto-discovered | Explicit URL to Puppet engine (Docker/standalone) |
| `ESPHOME_DASHBOARD_URL` | Standalone only, for `esphome_*` compile/validate/upload | — | ESPHome Device Builder's trusted-peer ingress URL. The add-on discovers this automatically via the Supervisor and does not need it set; standalone installs must set it explicitly or those tools return `esphome_not_configured`. |
| `NEXUS_READ_ONLY` | No | `false` | Standalone equivalent of the `read_only` add-on option — see [Add-on Options](#add-on-options) |
| `NEXUS_DISABLED_NAMESPACES` | No | *(empty)* | Standalone equivalent of `disabled_namespaces`, comma-separated (e.g. `supervisor,git`) |

---

## Changelog

See [Releases](https://github.com/Fistacho/ha-nexus-agent/releases) for full history.
