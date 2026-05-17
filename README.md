# Nexus Agent — MCP for Home Assistant

[![Release](https://img.shields.io/github/v/release/Fistacho/ha-nexus-agent?style=flat-square&label=latest)](https://github.com/Fistacho/ha-nexus-agent/releases/latest)
[![HACS](https://img.shields.io/badge/HACS-default-41BDF5?style=flat-square&logo=home-assistant-community-store)](https://hacs.xyz)
[![HA Add-on](https://img.shields.io/badge/Add--on-available-41BDF5?style=flat-square&logo=home-assistant)](https://github.com/Fistacho/ha-nexus-agent#installation--home-assistant-add-on-recommended)
[![License](https://img.shields.io/github/license/Fistacho/ha-nexus-agent?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Fistacho/ha-nexus-agent?style=flat-square)](https://github.com/Fistacho/ha-nexus-agent/stargazers)

**Give AI assistants full control over your smart home.** **244 tools across 25 domains** — entities, automations, scripts, dashboards, energy, voice pipelines, blueprints, calendar, HACS, Supervisor, themes, **Card Builder** (visual drag-and-drop cards), git versioning, and more.

Works with **Claude Code**, **Claude Desktop**, **VS Code**, **Cursor**, **Windsurf**, **OpenAI Codex CLI**, **Gemini CLI**.

---

## Option 1: Installation via Home Assistant (Recommended)

Click the button below to add the Nexus Agent repository to your Home Assistant instance, then install the add-on:

[![Open your Home Assistant instance and add the repository.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FFistacho%2Fha-nexus-agent)

1. Click the **Open Add-on Repository on MY** button above
2. Find **Nexus Agent** in the Add-on Store → **Install** → **Start**
3. Open **Web UI** — copy your MCP URL and paste it into your AI client

---

## Option 2: Manual Installation (Add-on Store)

If the MY button does not work for your setup:

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add:

   ```text
   https://github.com/Fistacho/ha-nexus-agent
   ```

4. Find **Nexus Agent** → **Install** → **Start** → **Open Web UI**

The web UI shows your API key and generates ready-to-paste config for every MCP client. No manual token setup.

## Option 3: Standalone (outside HA)

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

244 tools across 25 categories:

| Category | Count | Highlights |
| --- | --- | --- |
| `entities_*` | 17 | list, turn on/off/toggle, **bulk_control**, voice expose |
| `services_*` | 19 | call_service, notify, light color, camera snapshot/record |
| `automations_*` | 21 | CRUD + full YAML, traces, scripts, scenes |
| `blueprints_*` | 4 | list, import from URL, delete, instantiate |
| `areas_*` | 8 | list, create, get_states, control_area |
| `devices_*` | 4 | list, update (rename/move/disable), remove |
| `calendar_*` | 4 | list calendars/events, create/delete event |
| `todo_*` | 5 | list, add/update/remove items |
| `helpers_*` | 11 | input_boolean/number/text/select, timers, counters |
| `history_*` | 5 | state history, logbook, error log |
| `system_*` | 9 | check_config, backup, restart, list integrations |
| `dashboards_*` | 6 | get/save dashboard config, add cards/views |
| `files_*` | 6 | read/write config files, YAML validation |
| `git_*` | 11 | commit, rollback, log, safe_write_with_checkpoint |
| `ws_*` | 7 | listen state changes, events, subscribe_trigger |
| `supervisor_*` | 20 | add-on install/start/stop/update, backups, core/host info |
| `hacs_*` | 7 | list/install/uninstall/update HACS repos |
| `energy_*` | 9 | grid, solar, battery sources, energy preferences |
| `zones_*` | 8 | create/update/delete zones, person location |
| `labels_*` | 13 | labels, categories, assign to entities/devices |
| `search_*` | 7 | fuzzy search, orphan devices, unused entities |
| `integrations_*` | 12 | **config_flow** (install like in UI), options flow CRUD, enable/disable |
| `voice_*` | 9 | Assist pipelines CRUD, STT/TTS/wake-word engines |
| `themes_*` | 8 | list/create/update/delete Lovelace themes |
| `card_builder_*` | 17 | Card Builder cards CRUD, style presets, CSS properties, media manager, renderer config |

---

## Features

- **244 MCP tools** across 25 categories — the most complete HA MCP server
- **Real-time WebSocket** — subscribe to state changes, events and triggers live
- **Git versioning** — every config change auto-committed, instant rollback
- **YAML validation** before writing any config file
- **Setup web UI** — generates ready-to-use MCP config for every client
- **HA Add-on native** — one-click install, no manual token setup
- **API key auth** — MCP endpoint protected, token via URL or Bearer header

---

## Git Versioning

Nexus keeps a git history of your HA config directory. Before every risky change, use `git_safe_write_with_checkpoint` — it commits current state first, then applies the change.

```python
git_init_config()
git_safe_write_with_checkpoint("automations.yaml", new_content)
git_rollback_file("automations.yaml")       # undo single file
git_rollback_to_commit("abc1234")           # full rollback
git_log(limit=10)                           # see history
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

---

## Changelog

See [Releases](https://github.com/Fistacho/ha-nexus-agent/releases) for full history.
