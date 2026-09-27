# Changelog

## 0.20.0

Cards could not be added to any modern (`type: sections`) dashboard view. A sections
view renders only `view.sections` — cards placed in `view.cards` appear exclusively in
edit mode, inside an "imported cards" block (verified in home-assistant/frontend
`src/panels/lovelace/views/hui-sections-view.ts`). `dashboards_add_card_to_view` writes
exactly there, and the only alternative, `dashboards_save_dashboard_config`, overwrites
the whole dashboard, so editing one section meant round-tripping a full config (140 kB /
16 views on a real instance) through the model.

- **New `dashboards_get_view_sections`**: lists a sections view's sections with their
  index, heading and card count — read it before addressing sections by index.
- **New `dashboards_add_card_to_section`**: inserts a card into one section (optional
  `position`), preserving every other view, section and view-level key.
- **New `dashboards_update_card_in_section`**: merges keys into a card (default) or
  replaces it (`merge=False`); returns the card before and after.
- **New `dashboards_remove_card_from_section`**: deletes a card and returns it, so the
  change can be undone with `add_card_to_section`.
- **New `dashboards_add_section_to_view`**: appends (or inserts) a section, defaulting to
  `{type: grid, cards: []}`.
- All five accept a **view path as `url_path`** (e.g. `'lukasz2'`) and then save the
  default dashboard — passing a view path to `lovelace/config/save` would otherwise create
  a stray dashboard. Views that are not `type: sections` are refused with a message
  pointing at `add_card_to_view`.
- **Fix documented tool count**: the manifest, README and `pyproject.toml` all claimed 325
  tools while the registry held 317 (the README's own per-namespace table summed to 317).
  The count is now measured from the FastMCP registry: **322**.
- **Tests**: `tests/test_dashboards_sections.py` — 12 tests covering section targeting,
  view-path resolution, non-sections rejection, index guards, position, merge/replace and
  removal.

A follow-up audit reviewed every registered tool against the live Home Assistant API and
fixed roughly 20 of them that called nonexistent endpoints, returned stale or wrong data,
or had gaps in path/input handling. **`blueprints_save_blueprint`** was added to close the
blueprint import → save gap, bringing the total to **323 tools across 29 domains**.

**Fixed**

- `ws_render_template` now waits for the follow-up WebSocket event and returns the
  rendered value instead of always `null`.
- `ws_listen_state_changes` counts only events of the target `entity_id`.
- `system_create_backup` picks the backup service that exists (`create` on
  Core/Container, `create_automatic` on Supervisor).
- `integrations_*` config/options flow tools and `remove_integration` use the REST
  config-entries API instead of non-existent WebSocket commands.
- `entities_get_entity_exposure` / `entities_list_exposed_entities` use
  `homeassistant/expose_entity/list`.
- `entities_turn_on` and `services_set_light_color` send `color_temp_kelvin` (`mireds`
  removed in HA 2026.3); deprecated `color_temp` is converted; `entities_turn_on` rejects
  light-only options for non-light domains.
- `zones_list_persons_in_zone` reads the zone's `persons` attribute (`zone.home` always
  returned empty).
- `zones_create/update/delete_zone` use `zone/create|update|delete`.
- `energy_save_energy_prefs` no longer sends `currency`/`energy_per_unit` (not accepted by
  `energy/save_prefs`); passing them now returns an error.
- `calendar_delete_event` uses `calendar/event/delete` (with optional
  `recurrence_id`/`recurrence_range`).
- `discover` namespaces now match the real mount prefixes (`card_builder` was indexed as
  `card`).
- **`esphome_*`**: the add-on slug is discovered dynamically (previously missed installs
  like `5c53de3b_esphome`); `compile`/`upload` use the Device Builder-compatible WebSocket
  spawn protocol; `validate_config` uses Device Builder's `/ws devices/validate`;
  `lvgl_add/delete_widget` write a `.bak` backup before rewriting YAML.
- `statistics_get_energy_statistics` no longer treats W (power) as an energy unit.
- **New `blueprints_save_blueprint`**: persists blueprint YAML fetched by
  `import_blueprint`, which until now only ever downloaded and validated;
  `import_blueprint`'s docstring no longer claims to save.
- `/health` and the Setup UI report the live tool count instead of a hard-coded 100.
- `dashboards_screenshot` no longer prepends `lovelace/` to custom dashboards' paths; it
  resolves the first path segment against registered dashboards and fails with
  `dashboard_list_unavailable` instead of guessing (#3).
- Add-on now declares a Supervisor `watchdog` on `/health`, so a crashed server is
  restarted automatically (mitigation for #4).

**Security**

- `files_*` path containment uses `Path.is_relative_to` (sibling directories like
  `config_old` could previously escape `/config`); `list_config_files` no longer leaks
  paths via an exception.
- `esphome` device-name parameters reject path traversal.
- `card_builder`: `upload_media_from_path` only reads media files under `<config>/www` or
  `/media`; `upload_image_from_url` accepts only `http(s)`, `image/*` and ≤15 MiB; SVG
  uploads (`upload_svg`/`upload_media_from_path`/`upload_image_from_url`/`upload_media`)
  are sanitized with an allowlist against stored XSS under the unauthenticated `/local/`
  path; `upload_media` restricts file types and size.
- `git_rollback_file` rejects paths outside `/config`.
- **Rotate your API key after upgrading.** On 0.19.x, the Setup UI (`GET /`) and
  `POST /regenerate` exposed the API key and accepted a key reset from any
  unauthenticated LAN caller — anyone on the local network could read the key at
  `http://<host>:7123/`, and it was also written to the add-on log at startup. Both
  are now restricted to HA ingress (`172.30.32.2` + `X-Ingress-Path`), a valid
  `Authorization: Bearer` header, or (standalone mode only) `localhost`.
- The access log redacts `?token=...` to `?token=***`; the API key is no longer
  printed at startup.
- `Authorization: Bearer` is now the documented, preferred auth for all clients that
  support headers; README examples fixed to Streamable HTTP (`--transport http`)
  instead of SSE.
- Added `SECURITY.md` (private vulnerability reporting).

**Docs**

- Tool descriptions rewritten for ~40 tools to match actual behaviour (e.g. automation
  config `id` vs `entity_id`, `set_group` being runtime-only, `git_diff`/`--stat`,
  backup/restore consequences, overlapping-tool boundaries); personal examples removed.

**Known issues**

- `esphome_clean_mqtt` has no confirmed equivalent in ESPHome Device Builder ≥2026.6.
- ESPHome dashboard tools need a reachable dashboard: Device Builder exposes only ingress
  by default (port 6052 disabled) — set `ESPHOME_DASHBOARD_URL` / enable the port.

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
