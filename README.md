# Nexus Agent — MCP for Home Assistant

[![Release](https://img.shields.io/github/v/release/Fistacho/ha-nexus-agent?style=flat-square&label=latest)](https://github.com/Fistacho/ha-nexus-agent/releases/latest)
[![HACS](https://img.shields.io/badge/HACS-default-41BDF5?style=flat-square&logo=home-assistant-community-store)](https://hacs.xyz)
[![HA Add-on](https://img.shields.io/badge/Add--on-available-41BDF5?style=flat-square&logo=home-assistant)](https://github.com/Fistacho/ha-nexus-agent#installation--home-assistant-add-on-recommended)
[![License](https://img.shields.io/github/license/Fistacho/ha-nexus-agent?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Fistacho/ha-nexus-agent?style=flat-square)](https://github.com/Fistacho/ha-nexus-agent/stargazers)

**Give AI assistants full control over your smart home.** **302 tools across 28 domains** — entities, automations & scripts (CRUD + traces + linter + live reference validator), dashboards + screenshot + resource management, energy, voice pipelines, blueprints, calendar, HACS, Supervisor, **ESPHome** (list devices, compile, OTA, logs), themes, **self-documenting Card Builder** (visual cards, recipe builder, embedded block schema, upstream sync), **aggregated snapshot** (one-call context), **BM25 tool search**, HA-aware YAML validation, git versioning, and more.

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

Open <http://your-ha-ip:7123> after starting Nexus. The setup page generates the exact config for each client.

All clients connect to:

```text
http://your-ha-ip:7123/mcp?token=YOUR_API_KEY
```

### Claude Code CLI

```bash
claude mcp add nexus --transport sse "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY" --scope user
```

### OpenAI Codex CLI

```bash
codex mcp add nexus --url "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY"
```

### Gemini CLI

```bash
gemini mcp add nexus --url "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY"
```

### VS Code

Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "nexus": {
      "type": "sse",
      "url": "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY"
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
      "url": "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY",
      "type": "sse"
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
      "url": "http://your-ha-ip:7123/mcp?token=YOUR_API_KEY",
      "type": "sse"
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

> **Tip:** Copy the exact config with your real key from the Nexus web UI at `http://your-ha-ip:7123`.

---

## Tools

302 tools across 28 categories:

| Category | Tools | Highlights |
| --- | --- | --- |
| `entities_*` | 18 | list (paginated + field selection), turn on/off/toggle, **bulk_control**, voice expose, set_value |
| `services_*` | 19 | call_service, notify, light color, camera snapshot/record, media controls |
| `automations_*` | 28 | CRUD + full YAML, traces, scripts, scenes, **validate_best_practices** (static linter), **validate_automation_references** (live registry check), **list/set/remove groups**, confirm gates on delete |
| `blueprints_*` | 4 | list, import from URL, delete, instantiate |
| `areas_*` | 8 | list, create, get_states, **control_area** |
| `devices_*` | 4 | list, update (rename/move/disable), remove |
| `calendar_*` | 4 | list calendars/events, create/delete event |
| `todo_*` | 5 | list, add/update/remove items |
| `helpers_*` | 11 | input_boolean/number/text/select/datetime, timers, counters |
| `history_*` | 5 | state history, logbook, error log, system info |
| `system_*` | 12 | check_config, backup, **restart/stop** (confirm gate), **get_updates**, **get_system_health**, **get_repairs** |
| `dashboards_*` | 10 | get/save config, add cards/views, **screenshot** (Puppet), **add/remove/update resources** (JS/CSS) |
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
| `esphome_*` | 10 | list devices + online status, read config YAML, get entities, **compile / validate / OTA upload** via Dashboard API, add-on logs, ping |
| `discover_*` | 4 | **BM25 tool search** — query the tool catalogue, list namespaces, fetch full docstrings |

---

## Features

- **302 MCP tools** across 28 categories — the most complete HA MCP server available
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
- **API key auth** — MCP endpoint protected, token via URL or Bearer header

---

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
| `ESPHOME_DASHBOARD_URL` | No | `http://homeassistant.local:6052` | ESPHome dashboard URL for compile/OTA tools |

---

## Changelog

See [Releases](https://github.com/Fistacho/ha-nexus-agent/releases) for full history.
