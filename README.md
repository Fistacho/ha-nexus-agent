# ha-nexus-agent

MCP server for Home Assistant — gives AI assistants (Claude, Cursor, VS Code) full control over your smart home through **100 tools** covering entities, automations, scripts, dashboards, helpers, areas, history, system management, YAML config files, git-based versioning and real-time WebSocket events.

## Installation — Home Assistant Add-on (recommended)

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) in the top right → **Repositories**
3. Add this URL:

   ```text
   https://github.com/Fistacho/ha-nexus-agent
   ```

4. Find **Nexus Agent** in the store and click **Install**
5. Go to the add-on **Configuration** tab and set your options (port, log level, etc.)
6. Click **Start**
7. Click **Open Web UI** — you'll see your API key and ready-to-paste config for your MCP client

That's it. No token setup needed — the add-on gets access to Home Assistant automatically.

## Installation — Standalone (outside HA)

```bash
git clone https://github.com/Fistacho/ha-nexus-agent
cd ha-nexus-agent
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set HA_URL and HA_TOKEN
python server.py
```

Open **http://localhost:7123** — copy the generated config into your MCP client.

### Getting a Home Assistant token (standalone only)

1. In HA go to **Profile → Security → Long-Lived Access Tokens**
2. Click **Create Token**, give it a name (e.g. `nexus`)
3. Paste the token as `HA_TOKEN` in your `.env`

## Connecting to your MCP client

After starting Nexus, open **<http://localhost:7123>** — the setup UI shows the exact config snippet to paste into your client.

### Claude Desktop

Paste into `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python",
      "args": ["server.py"],
      "cwd": "/path/to/ha-nexus-agent",
      "env": {
        "HA_URL": "http://homeassistant.local:8123",
        "HA_TOKEN": "your_token_here"
      }
    }
  }
}
```

### Cursor / VS Code

```
[mcp]
name = nexus
url = http://localhost:7123/mcp
api_key = YOUR_API_KEY
```

## Features

- **100 MCP tools** across 11 categories
- **Real-time WebSocket** — listen for state changes, events and triggers live
- **Git versioning** — every config change auto-committed with rollback to any point
- **YAML validation** before saving any config file
- **Setup web UI** — auto-generates MCP client config, shows API key
- **HA add-on native** — installs from Add-on Store, no manual token setup
- Works with Claude Desktop, Cursor, VS Code, Gemini CLI

## Tools overview

| Category | Count | Examples |
|---|---|---|
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
| `ws_*` | 7 | ws_listen_state_changes, ws_listen_events, ws_subscribe_trigger |

## Git versioning

Nexus keeps a git history of your HA config directory. Before every risky change, use `git_safe_write_with_checkpoint` — it commits the current state first, then applies the change. If something breaks, roll back instantly.

```python
git_init_config()                          # run once to initialize
safe_write_with_checkpoint("automations.yaml", new_content)
git_rollback_file("automations.yaml")      # undo single file
git_rollback_to_commit("abc1234")          # full rollback
git_log(limit=10)                          # see history
```

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HA_URL` | Yes | `http://homeassistant.local:8123` | Home Assistant URL |
| `HA_TOKEN` | Standalone only | — | Long-lived access token |
| `SUPERVISOR_TOKEN` | Add-on only | auto-injected | Set automatically by HA |
| `HA_CONFIG_PATH` | For git tools | `/config` | Path to HA config directory |
| `NEXUS_API_KEY` | No | auto-generated | Pin to a specific key |
| `NEXUS_PORT` | No | `7123` | Setup UI and server port |
