# ha-nexus-agent

MCP server for Home Assistant — gives AI assistants full control over your smart home through **100 tools** covering entities, automations, scripts, dashboards, helpers, areas, history, system management, YAML config files, git-based versioning and real-time WebSocket events.

Works with **Claude Code CLI**, **Claude Desktop**, **VS Code**, **Cursor**, **Windsurf**, **OpenAI Codex CLI**, **Gemini CLI**.

---

## Installation — Home Assistant Add-on (recommended)

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add:

   ```text
   https://github.com/Fistacho/ha-nexus-agent
   ```

4. Find **Nexus Agent** and click **Install**
5. Click **Start**
6. Click **Open Web UI**

The web UI shows your API key and generates ready-to-paste config for every MCP client. No manual token setup — the add-on connects to Home Assistant automatically.

---

## Installation — Standalone (outside HA)

```bash
git clone https://github.com/Fistacho/ha-nexus-agent
cd ha-nexus-agent
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set HA_URL and HA_TOKEN
python server.py
```

Open **<http://localhost:7123>** to get your API key and MCP client configs.

### Getting a Home Assistant token

1. In HA go to **Profile → Security → Long-Lived Access Tokens**
2. Click **Create Token**, name it `nexus`
3. Paste as `HA_TOKEN` in `.env`

---

## Connecting MCP clients

Open **<http://your-ha-ip:7123>** after starting Nexus. The setup page generates the exact command or config snippet for each client — just copy and paste.

All SSE-based clients connect to:

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

Paste into `~/.cursor/mcp.json`:

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

Paste into `~/.codeium/windsurf/mcp_config.json`:

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

### Claude Desktop (standalone, subprocess mode)

Paste into `%APPDATA%/Claude/claude_desktop_config.json` (Win) or `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

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

> **Tip:** Copy the exact config (with your real paths and key) from the Nexus web UI at `http://your-ha-ip:7123`.

---

## Features

- **100 MCP tools** across 11 categories
- **Real-time WebSocket** — subscribe to state changes, events and triggers live
- **Git versioning** — every config change auto-committed with instant rollback
- **YAML validation** before writing any config file
- **Setup web UI** — auto-generates ready-to-use MCP config for every client
- **HA add-on native** — one-click install from Add-on Store, no manual token setup
- **API key auth** — MCP endpoint protected, token passed via URL query parameter

---

## Tools overview

| Category | Count | Examples |
| --- | --- | --- |
| `entities_*` | 14 | list_entities, turn_on, turn_off, toggle, assign_to_area |
| `services_*` | 15 | call_service, send_notification, set_light_color, set_climate_mode |
| `automations_*` | 13 | list_automations, trigger_automation, run_script, activate_scene |
| `areas_*` | 8 | list_areas, create_area, get_area_states, control_area |
| `helpers_*` | 12 | set_input_boolean, set_input_number, start_timer, increment_counter |
| `history_*` | 5 | get_state_history, get_logbook, get_error_log |
| `system_*` | 9 | check_config, create_backup, restart_ha, list_integrations |
| `dashboards_*` | 6 | get_dashboard_config, add_card_to_view, add_view_to_dashboard |
| `files_*` | 6 | read_config_file, write_config_file, validate_yaml_content |
| `git_*` | 11 | git_commit_all, git_rollback_file, git_log, safe_write_with_checkpoint |
| `ws_*` | 7 | listen_state_changes, listen_events, subscribe_trigger |

---

## Git versioning

Nexus keeps a git history of your HA config directory. Before every risky change, use `git_safe_write_with_checkpoint` — it commits the current state first, then applies the change. Roll back instantly if something breaks.

```python
git_init_config()                                    # run once
git_safe_write_with_checkpoint("automations.yaml", new_content)
git_rollback_file("automations.yaml")                # undo single file
git_rollback_to_commit("abc1234")                    # full rollback
git_log(limit=10)                                    # see history
```

---

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HA_URL` | Yes | `http://homeassistant.local:8123` | Home Assistant URL |
| `HA_TOKEN` | Standalone only | — | Long-lived access token |
| `SUPERVISOR_TOKEN` | Add-on only | auto-injected | Set automatically by HA |
| `HA_CONFIG_PATH` | For git tools | `/config` | Path to HA config directory |
| `NEXUS_API_KEY` | No | auto-generated | Pin to a specific API key |
| `NEXUS_PORT` | No | `7123` | HTTP server port |
