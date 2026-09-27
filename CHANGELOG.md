# Changelog

## 0.22.0

**Added**

- New add-on options `read_only` and `disabled_namespaces` (ADR-0003 P1/P2), enforced
  server-side through FastMCP's own Visibility mechanism: a hidden tool is refused at
  `tools/call`, not merely omitted from `tools/list`. `read_only` hides and blocks every
  tool whose annotations don't declare `readOnlyHint=True` — today 169 of 323 tools, 154
  stay visible — and hides all MCP prompts (they carry no annotations, so there's no way
  to prove one only ever reads). `disabled_namespaces` hides and blocks every tool/prompt
  under a given namespace prefix (e.g. `["supervisor", "git"]`). Both are read once at
  startup; a malformed value (unknown namespace, non-boolean `read_only`) stops the
  add-on from starting instead of silently applying a narrower or wider policy than
  configured. Standalone (non-add-on) equivalents: `NEXUS_READ_ONLY` /
  `NEXUS_DISABLED_NAMESPACES`.
- `expected_config_hash` / `config_hash` on the dashboard section tools
  (`dashboards_get_view_sections`, `_add_card_to_section`, `_update_card_in_section`,
  `_remove_card_from_section`, `_add_section_to_view`): optimistic concurrency for the
  whole dashboard, since `card_index`/`section_index` are positions, not stable ids.
  Pass the `config_hash` from a read back as `expected_config_hash` on the next write;
  a stale hash returns `{"error": "config_changed", ...}` and saves nothing instead of
  editing the wrong card. Omitting it keeps today's behaviour.
- `timeout` (1–300 s) on `ws_get_states` and `ws_call_service`, alongside the existing
  `ws_render_template` / `ws_listen_state_changes` / `ws_listen_events` /
  `ws_subscribe_trigger`, which now enforce the same 1–300 s bounds.

**Changed**

- `supervisor_delete_backup` and `files_delete_config_file` now require `confirm=True`:
  the first call without it does no I/O at all and returns
  `{"error": "confirmation_required", "message", "action"}` instead of deleting
  immediately. Both are one-shot, unrecoverable operations (a backup is itself the undo
  mechanism for everything else in this add-on; a config file has no trash) that
  previously had no safety net.
- `automations_delete_scene`'s refusal without `confirm=True` now uses the same
  `{"error": "confirmation_required", "message", "action"}` shape as every other
  `confirm`-gated tool, instead of its own `{"error": "set confirm=True to delete", ...}`.
- `GET /health` now returns only `{"status": "ok"}`. It previously also returned `ha_url`
  and the live tool count — addon-internal facts that leaked to any unauthenticated
  caller on the LAN, since the Supervisor watchdog only needs a 200. Those, plus the new
  `read_only`/`disabled_namespaces` policy, now show only on the ingress-gated Setup UI
  page.
- `build.yaml`'s `build_from:` map now names `python:3.12-alpine` explicitly for all 5
  architectures, matching what `Dockerfile` actually built from all along (it hardcoded
  `FROM python:3.12-alpine`, making the previous `ghcr.io/home-assistant/<arch>-base-python`
  entries dead). The Dockerfile now uses `ARG BUILD_FROM` / `FROM ${BUILD_FROM}` fed by
  build.yaml, so the two files can no longer silently disagree. The image that ships to
  users is unchanged.

**Fixed**

- The WebSocket `auth_required` → `auth` → `auth_ok` handshake in `tools/websocket.py`
  now honours the caller's `timeout` — previously an add-on/HA instance that sent
  `auth_required` and then never answered left the call blocked indefinitely, with no
  way to bound it.
- `esphome_clean_mqtt` now clears retained MQTT discovery topics through HA's own `mqtt`
  integration (`mqtt/device/debug_info` plus an empty retained `mqtt.publish`) instead of
  the ESPHome dashboard, which has no equivalent command.
- `esphome_list_devices` / `esphome_get_device_entities` no longer rely on a
  `binary_sensor.*_api_connection_status` entity that isn't guaranteed to exist. Connected
  status now comes from whether any of a device's own registered entities report a state
  other than `"unavailable"` (ESPHome entities share one per-config-entry availability
  flag), and entity matching uses the device registry's `device_id` when a device-registry
  entry is found, falling back to the previous slug-in-entity_id match otherwise.
- ESPHome dashboard tools (`esphome_compile_device`, `esphome_validate_config`, and others
  behind `_dash_ws_command`/`_dash_ws_spawn`) now attach a `diagnosis` block to a
  `"Cannot connect to ESPHome dashboard"` error, built from the Supervisor add-on's own
  `/addons/<slug>/info` (port-6052 mapping, ingress-only detection) with concrete advice
  instead of a bare connection error.

**Security**

- `GET /health` (see Changed above) no longer exposes `ha_url`, the tool count or the
  active tool-exposure policy to unauthenticated callers.
- Authenticating via `?token=<API_KEY>` in the query string now logs one deprecation
  warning per add-on start (never the key itself) and is documented as **removed in
  nexus 1.0.0** — prefer the `Authorization: Bearer` header.
- The Setup UI page and its README/Security docs now say explicitly that the Supervisor
  **Watchdog** toggle on the add-on's **Info** tab must be turned on separately —
  listing `watchdog:` in `config.yaml` does not enable it by itself, so an add-on that
  silently stopped restarting itself is now called out on the same protected page.

**Internal**

- New GitHub Actions CI (`.github/workflows/ci.yml`): `ruff check .` (required), `pytest`
  on Python 3.12 and 3.14, and a build-and-smoke-test job that builds the amd64 image
  from this repo's own `Dockerfile`/`build.yaml` and polls `/health` for HTTP 200.
- New E2E workflow (`.github/workflows/e2e.yml`, informational only): onboards a real,
  freshly started Home Assistant container and runs `tests_e2e` against it. Not required
  for merge yet — added as a required check only once observed green a few times in a row.
- `Dockerfile` now reads its base image from `build.yaml` via `ARG BUILD_FROM` for every
  architecture instead of hardcoding it (see Changed above); the runtime image itself
  (`python:3.12-alpine`) is unchanged.
- `ruff==0.16.1` pinned as a dev dependency; `ruff check .` is clean across the whole
  repo (tests/tests_e2e excluded from lint — see `pyproject.toml`'s `[tool.ruff]` comment).

## 0.21.0

**Added**

- MCP tool annotations on all 323 tools (`readOnlyHint`/`destructiveHint`/`idempotentHint`/
  `openWorldHint` + `title`) so clients can auto-approve reads and ask before destructive
  calls; every parameter now carries a schema description (`Field(description=...)`); tool
  descriptions rewritten to a single contract template (what it does, when to use / not
  for with the alternative tool, returns, errors, limits).
- `discover_get_tool_doc` also returns `input_schema` and `annotations` alongside the
  description, so a client can inspect a tool's full parameter schema and MCP hints
  without a separate `tools/list` round trip.
- Tool contract test suite (`tests/test_tool_contract.py`, `tests/test_discover_contract.py`,
  `tests/contract/`) — a golden API surface snapshot, tool/domain counts, and a per-tool
  lint guarding descriptions, annotations and overlap cross-references, so future changes
  to the tool surface fail loudly instead of drifting silently.

**Changed**

- Dependencies pinned to tested majors: `fastmcp>=3.2.4,<4`, `mcp>=1.27,<2`,
  `pydantic>=2,<3`, and upper bounds added for `fastapi`, `uvicorn`, `httpx`, `websockets`,
  `pyyaml` and `python-dotenv` — the add-on builds its image at install time, so an
  unpinned major meant every new install could silently run untested code.
- `helpers_reload_helpers` now returns `{"reloaded": [...], "failed": {domain: message}}`
  instead of a bare list, so a failed domain reload is visible instead of swallowed.
- Supervisor destructive tools (reboot/shutdown/restart-core and friends) return the
  common `{"error": "confirmation_required", "message": ..., "action": ...}` shape when
  called without `confirm=True`, instead of each tool inventing its own refusal format.

**Fixed**

- `automations_validate_automation_references` checked almost no services for classic
  automations — the legacy top-level `action:` list was skipped — and silently reported
  "nothing missing" when fetching live services/entities failed. It now reports
  `entities_check`/`services_check` (`"ok"`/`"unavailable"`) and `errors[]` naming which
  fetch failed.
- `automations_set_group` rejects `entities` combined with `add_entities`/
  `remove_entities` instead of calling `group.set` with an ambiguous mix.
- `entities_bulk_set_state` tags per-item failures with `error_type`
  (`validation`/`http`/`unexpected`) instead of a single undifferentiated error string.
- `themes_*` tools return `{"success": false, "error": "invalid_theme_name", ...}` for a
  bad theme name instead of raising.

**Removed**

- Dead `ha_client.read_config_file` (raised `NotImplementedError`, unused).

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
