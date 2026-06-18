# Changelog

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
