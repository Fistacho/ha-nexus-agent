# ha-nexus-agent

MCP server for Home Assistant — gives AI assistants (Claude, Cursor, VS Code) full control over your smart home through 93 tools covering entities, automations, scripts, dashboards, helpers, areas, history, system management, YAML config files, and git-based versioning.

## Features

- **93 MCP tools** across 10 categories
- **Git versioning** — every config change can be auto-committed, with rollback to any point
- **YAML validation** before saving any config file
- **Setup web UI** — shows your API key and ready-to-paste config for Claude Desktop / Cursor / VS Code
- **HA add-on ready** — runs natively inside Home Assistant (auto token via `SUPERVISOR_TOKEN`)
- Works with any MCP client: Claude Desktop, Cursor, VS Code, Gemini CLI

## Quick start (standalone)

```bash
git clone https://github.com/Fistacho/ha-nexus-agent
cd ha-nexus-agent
pip install -r requirements.txt
cp .env.example .env
# edit .env — set HA_URL and HA_TOKEN
python server.py
```

Open **http://localhost:7123** — copy the generated config into your MCP client.

## Getting a Home Assistant token

1. In HA go to **Profile → Security → Long-Lived Access Tokens**
2. Click **Create Token**, give it a name (e.g. `nexus`)
3. Copy the token to `HA_TOKEN` in your `.env`

## Tools overview

| Category | Tools | Examples |
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

## Claude Desktop config

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

Or just open **http://localhost:7123** after starting the server — it generates the config for you.

## Git versioning

Nexus keeps a git history of your HA config directory. Before every risky change, use `git_safe_write_with_checkpoint` — it commits the current state first, then applies the change. If something breaks, roll back with `git_rollback_file` or `git_rollback_to_commit`.

```
# Initialize (run once)
git_init_config()

# Safe write with auto-checkpoint
safe_write_with_checkpoint("automations.yaml", new_content)

# Rollback single file
git_rollback_file("automations.yaml")

# See history
git_log(limit=10)
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `HA_URL` | Yes | `http://homeassistant.local:8123` | Home Assistant URL |
| `HA_TOKEN` | Yes* | — | Long-lived access token |
| `SUPERVISOR_TOKEN` | Add-on only | auto | Injected by HA when running as add-on |
| `HA_CONFIG_PATH` | For git tools | `/config` | Path to HA config directory |
| `NEXUS_API_KEY` | No | auto-generated | Pin API key to specific value |
| `NEXUS_PORT` | No | `7123` | Setup UI port |

*Not needed when running as HA add-on (`SUPERVISOR_TOKEN` takes priority).
