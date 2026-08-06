# Changelog

## 0.19.1

Three diagnostic tools called Home Assistant endpoints that do not exist, so
every one of them failed on a live instance. Verified against `home-assistant/core`.

- **Fix `system_get_repairs`**: sent the WebSocket command `repairs/list`, which HA
  rejects with `unknown_command`. The registered command is **`repairs/list_issues`**
  (`components/repairs/websocket_api.py`).
- **Fix `system_get_system_health`**: called `GET /api/system_health`, which 404s —
  `system_health` registers no REST view, only the **`system_health/info`** WebSocket
  subscription. That command answers with an empty `result`, then streams an `initial`
  snapshot, one `update` per slow value and a final `finish`, so a plain request/response
  call could never read it. Added `ha_client._ws_collect_events` (generic subscription
  collector, returns partial data on timeout rather than failing) plus
  `merge_system_health_events` to fold the stream into one dict.
- **Fix `history_get_error_log`**: called `GET /api/error_log`, which HA registers
  **only when it logs to a file** (`if DATA_LOGGING in hass.data`). Supervisor installs
  default to `duplicate_log_file: false`, so the endpoint is absent and the tool 404'd.
  It now falls back to the `system_log/list` WebSocket command and renders those records
  as log-file-like text via `ha_client.format_system_log_entries`.
- **Tests**: first `pytest` suite in the add-on (`tests/`), covering all three fixes plus
  the Supervisor add-on options path. `pytest` added as an optional `dev` dependency.

## 0.19.0

- **LVGL display tools** (`esphome_*`): 7 new tools for AI-driven LVGL UI management on ESPHome devices — `lvgl_list_devices` (find LVGL-capable devices), `lvgl_get_pages` (list pages + widget counts), `lvgl_get_page_widgets` (inspect widgets with types/IDs/positions), `lvgl_get_styles` (theme + style definitions), `lvgl_validate` (client-side validation: unique IDs, page references — no Dashboard needed), `lvgl_add_widget` (add widget to page + save), `lvgl_delete_widget` (delete widget by id + save)
- `!lambda` / `!secret` / `!include` tags are preserved on YAML round-trip (NUL-encoded during parse, restored on dump)
- 325 tools across 29 domains

## 0.18.1

- **Fix `esphome_list_devices`**: `ha_devices` was always empty — detection now uses three strategies: config entry domain lookup, identifiers field, and manufacturer name (`Espressif` / `esphome`) as fallback. All AC units and Level sensor now appear correctly.

## 0.18.0

- **Scene CRUD** (`automations_*`): `get_scene_config`, `set_scene_config` (create/overwrite + auto-reload), `delete_scene` (confirm gate)
- **`esphome_write_config`**: write ESPHome YAML to `/config/esphome/` with `!secret`-aware validation
- **New `statistics_*` namespace** (4 tools): `list_statistic_ids`, `get_statistics` (sum/mean/min/max by hour/day/week/month), `get_energy_statistics` (auto-discovers kWh/m³ sensors), `get_statistics_metadata`
- 318 tools across 29 domains

## 0.17.0

- **New `esphome_*` namespace** (10 tools): `list_devices` (configs + HA registry + online status), `get_config`, `get_device_entities`, `compile_device`, `validate_config`, `upload_device` (OTA), `clean_mqtt`, `get_addon_info`, `get_addon_logs`, `ping_dashboard`
- Dashboard URL configurable via `ESPHOME_DASHBOARD_URL` env var
- 302 tools across 28 domains

## 0.16.0

- **`system_get_updates`**: list pending updates for core, add-ons, HACS, custom components
- **`system_get_system_health`**: health check of all HA subsystems
- **`system_get_repairs`**: active repair issues from HA repair centre
- **`automations_validate_automation_references`**: live cross-check of every entity_id and service in automation YAML against the running HA instance
- **`automations_list/set/remove_group`**: group entity CRUD
- **`dashboards_add/remove/update_dashboard_resource`**: Lovelace JS/CSS resource management
- 292 tools across 27 domains

## 0.15.0

- Pagination and field projection on list tools (`page`, `page_size`, `fields`)
- Confirmation gates on all destructive operations (`confirm=True`)
- **`automations_validate_best_practices`**: static linter (7 rules: missing modes, empty conditions, deprecated keys, etc.)
- **`dashboards_screenshot`**: render any Lovelace view to PNG via Puppet engine
- 285 tools across 27 domains

## 0.14.0

- Card Builder UX layer: `design_principles`, `list_design_patterns`, `get_design_pattern`, `design_for_intent`
- MCP prompts for guided card creation workflows

## 0.13.0

- Card Builder schema sync against upstream Mushroom/custom-cards 2.3.0
- `check_schema_sync` tool to detect drift

## 0.12.0

- Card Builder: compact marketplace-style recipes (grid + absolute layouts)
- `build_from_recipe` high-level builder

## 0.11.0

- Card Builder canvas root + in-session media generation (SVG, PNG upload)
- 276 tools

## 0.10.0

- Card Builder: 10 turnkey card templates (`make_template_card`)
- 274 tools

## 0.9.0

- Card Builder: full styles knowledge embedded (`list_style_categories`, `list_style_targets`, `list_style_snippets`, `build_styles`)
- 270 tools

## 0.8.0

- Self-documenting Card Builder: `list_block_types`, `get_block_schema`, `list_button_toggle_features`
- 264 tools

## 0.7.0

- BM25 full-text tool search (`discover_tool_search`)
- 259 tools across 27 namespaces

## 0.6.0

- Snapshot tools (`snapshot_get_snapshot`, `snapshot_get_area_snapshot`)
- Last-trace helpers (`get_last_automation_trace`, `get_last_script_trace`)
- HA custom YAML tag support in `files_validate_yaml_content` (`!include`, `!secret`, `!env_var`)
- Bulk voice exposure (`entities_bulk_set_entity_exposure`)
- 248 tools

## 0.5.0

- Card Builder integration (38 tools): CRUD, style presets, CSS custom properties, media upload, renderer config
- 244 tools across 25 namespaces

## 0.4.0

- Config flows (install integrations like in the UI)
- Voice pipelines CRUD
- Themes management
- 227 tools

## 0.3.0

- Initial public release: 202 tools across 21 namespaces
