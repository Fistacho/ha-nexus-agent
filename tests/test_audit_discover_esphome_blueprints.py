"""Regression tests for audit findings F13, F23, F24, F26.

F13 `tools/discover.py`: namespace was derived from `name.split("_", 1)[0]`, so every
    `card_builder_*` tool (the only real multi-word namespace) was misfiled under "card".
F23 `tools/esphome.py`: compile/validate/upload/clean_mqtt sent HTTP POST to dashboard
    endpoints that are actually WebSocket-only (esphome/dashboard/web_server.py,
    `EsphomeCommandWebSocket` subclasses — protocol confirmed against the esphome PyPI
    wheel: client sends {"type": "spawn", ...}, server streams {"event": "line"/"exit"}).
    Follow-up (2026-09-27): the pip `esphome` dashboard was replaced by the standalone
    "ESPHome Device Builder" add-on between esphome 2026.5.0 and 2026.7.0. Re-verified
    against github.com/esphome/device-builder: /compile and /upload keep the exact same
    spawn WS protocol (api/legacy.py, read on GitHub) so compile_device/upload_device are
    unchanged; /validate and /clean-mqtt have NO legacy WS route anymore. validate_config
    now uses Device Builder's newer multiplexed /ws command protocol (docs/API.md:
    `devices/validate`); clean_mqtt has no confirmed equivalent at all (open risk, see its
    docstring). The ESPHome add-on's Supervisor slug is also no longer a hard-coded
    2-item list — `_discover_esphome_slug` finds it from the live `/addons` listing, since
    the installed add-on's real slug (`5c53de3b_esphome`) wasn't in that hard-coded list.
    Also fixed: every device-name parameter in this module (get/write_config, compile/
    validate/upload/clean_mqtt, lvgl_add_widget/lvgl_delete_widget) accepted `../` path
    traversal — `_device_path`/`_safe_filename` now reject it.
F24 `tools/esphome.py` lvgl_add_widget/lvgl_delete_widget: `yaml.dump` rewrites the whole
    device file (comments/anchors lost) with no backup.
F26 `tools/blueprints.py` import_blueprint: docstring claimed it saves to
    /config/blueprints/..., but HA's `blueprint/import` WS command only fetches +
    validates; persisting requires the separate `blueprint/save` command.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest

import ha_client as ha
from tools import blueprints as blueprints_tools
from tools import discover as discover_mod
from tools import esphome as esphome_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


# --- F13: discover namespace resolution --------------------------------------

class _FakeTool:
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description


class _FakeRoot:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return self._tools


@pytest.fixture(autouse=True)
def _reset_discover_state():
    """Every test starts from a clean discover.py module-level cache."""
    discover_mod._ROOT = None
    discover_mod._INDEX = None
    discover_mod._KNOWN_NAMESPACES = None
    yield
    discover_mod._ROOT = None
    discover_mod._INDEX = None
    discover_mod._KNOWN_NAMESPACES = None


def test_known_namespaces_includes_card_builder_as_one_prefix():
    """server.py mounts card_builder_mcp with namespace='card_builder' — a single,
    two-word prefix. It must come out of the real source parse intact, not split."""
    namespaces = discover_mod._known_namespaces()

    assert "card_builder" in namespaces
    assert "card" not in namespaces


def test_namespace_for_resolves_multiword_prefix_not_naive_split():
    """RED (pre-fix): `name.split('_', 1)[0]` gives 'card' for every card_builder_* tool."""
    assert discover_mod._namespace_for("card_builder_create_card") == "card_builder"
    assert discover_mod._namespace_for("card_builder_list_templates") == "card_builder"
    # Unaffected single-word namespaces keep working exactly as before.
    assert discover_mod._namespace_for("entities_get_entity") == "entities"


def test_list_namespaces_groups_card_builder_tools_together():
    tools = [
        _FakeTool("card_builder_create_card", "Create a mushroom card"),
        _FakeTool("card_builder_list_templates", "List card templates"),
        _FakeTool("entities_get_entity", "Get one entity"),
    ]
    discover_mod.bind_root(_FakeRoot(tools))

    namespaces = {row["namespace"]: row["count"] for row in _unwrap(discover_mod.list_namespaces)()}

    assert namespaces.get("card_builder") == 2
    assert "card" not in namespaces


def test_tool_search_namespace_filter_finds_card_builder_tools():
    """RED (pre-fix): `namespace="card_builder"` matched nothing — every entry was
    filed under "card" instead, per the F13 finding."""
    tools = [
        _FakeTool("card_builder_create_card", "Create a mushroom card layout"),
        _FakeTool("entities_get_entity", "Get one entity state"),
    ]
    discover_mod.bind_root(_FakeRoot(tools))

    hits = _unwrap(discover_mod.tool_search)("card", namespace="card_builder")

    assert len(hits) == 1
    assert hits[0]["name"] == "card_builder_create_card"
    assert hits[0]["namespace"] == "card_builder"


def test_tool_search_docstring_has_no_stale_tool_count():
    assert "~250-tool" not in _unwrap(discover_mod.tool_search).__doc__


# --- F23: ESPHome compile/validate/upload/clean_mqtt must use the dashboard WS API ---

class _FakeWebSocket:
    def __init__(self, events: list[dict]):
        self._events = list(events)
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return json.dumps(self._events.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConnect:
    """Stand-in for `websockets.connect` — records the URL and websocket it opened.

    (Assigning to `instance.__call__` would not intercept `instance(...)` — dunder
    lookups go through the type, not the instance — so instances are recorded here.)
    """

    def __init__(self, events: list[dict]):
        self._events = events
        self.urls: list[str] = []
        self.instances: list[_FakeWebSocket] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        ws = _FakeWebSocket(self._events)
        self.instances.append(ws)
        return ws


def _no_http(*a, **k):
    raise AssertionError("must not fall back to a plain HTTP POST for /compile & co.")


def test_compile_device_sends_spawn_message_with_configuration(monkeypatch):
    import websockets

    fake_connect = _FakeConnect([{"event": "exit", "code": 0}])
    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(httpx, "Client", _no_http)

    result = _unwrap(esphome_tools.compile_device)("livingroom")

    assert result["success"] is True
    (ws,) = fake_connect.instances
    assert ws.sent == [{"type": "spawn", "configuration": "livingroom.yaml"}]


def test_upload_device_defaults_port_to_ota(monkeypatch):
    import websockets

    fake_connect = _FakeConnect([{"event": "exit", "code": 0}])
    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(httpx, "Client", _no_http)

    result = _unwrap(esphome_tools.upload_device)("livingroom")

    assert result["success"] is True
    (ws,) = fake_connect.instances
    assert ws.sent == [{"type": "spawn", "configuration": "livingroom.yaml", "port": "OTA"}]


def test_compile_device_reports_failure_exit_code(monkeypatch):
    import websockets

    fake_connect = _FakeConnect([
        {"event": "line", "data": "error: xyz"},
        {"event": "exit", "code": 1},
    ])
    monkeypatch.setattr(websockets, "connect", fake_connect)

    result = _unwrap(esphome_tools.compile_device)("livingroom")

    assert result["exit_code"] == 1
    assert result["success"] is False
    assert result["log_tail"] == ["error: xyz"]


def test_dash_ws_spawn_times_out_gracefully(monkeypatch):
    """A process that never sends 'exit' must not hang the tool call forever."""
    import asyncio as _asyncio

    class _NeverExitsWS(_FakeWebSocket):
        async def recv(self) -> str:
            await _asyncio.sleep(0)
            raise _asyncio.TimeoutError

    import websockets

    def connect(url, **kwargs):
        return _NeverExitsWS([])

    monkeypatch.setattr(websockets, "connect", connect)

    result = esphome_tools._dash_ws_spawn("/compile", {"configuration": "x.yaml"}, timeout=0.01)

    assert result["error"] == "timeout"


def test_ping_dashboard_still_uses_plain_http_get(monkeypatch):
    """/ping is a regular tornado GET handler (not a websocket) — must stay on httpx."""
    calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, path, json=None):
            calls.append((method, path))
            request = httpx.Request(method, f"http://homeassistant.local:6052{path}")
            return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx, "Client", lambda **k: FakeClient())

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert calls == [("GET", "/ping")]
    assert result["reachable"] is True


# --- Follow-up to F23: Device Builder dropped the legacy /validate & /clean-mqtt WS
# routes (confirmed on GitHub 2026-09-27: esphome/device-builder's api/legacy.py keeps
# only /devices, /ping, /json-config, /encryption-key, /compile, /upload). validate_config
# must now use the newer multiplexed /ws command protocol (docs/API.md: `devices/validate`).

class _FakeMultiplexWebSocket:
    """Stand-in for the `/ws` multiplexed protocol: first recv is the ServerInfoMessage,
    then one command is sent, then queued command/result/event frames are replayed."""

    def __init__(self, server_info: dict, events: list[dict]):
        self._queue = [json.dumps(server_info)] + [json.dumps(e) for e in events]
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return self._queue.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeMultiplexConnect:
    def __init__(self, server_info: dict, events: list[dict]):
        self._server_info = server_info
        self._events = events
        self.urls: list[str] = []
        self.instances: list[_FakeMultiplexWebSocket] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        ws = _FakeMultiplexWebSocket(self._server_info, self._events)
        self.instances.append(ws)
        return ws


def test_validate_config_uses_ws_multiplexed_devices_validate_command(monkeypatch):
    """RED (post-research): the legacy dashboard's dedicated /validate WebSocket route
    is gone from Device Builder — validate must go through /ws's `devices/validate`
    command/response protocol instead of the old per-endpoint spawn protocol."""
    import websockets

    fake_connect = _FakeMultiplexConnect(
        {"requires_auth": False},
        [
            {"message_id": "1", "event": "output", "data": "Checking configuration...\n"},
            {"message_id": "1", "event": "result", "data": {"success": True, "code": 0}},
        ],
    )
    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(httpx, "Client", _no_http)

    result = _unwrap(esphome_tools.validate_config)("livingroom")

    assert fake_connect.urls == ["ws://homeassistant.local:6052/ws"]
    (ws,) = fake_connect.instances
    assert ws.sent == [
        {"command": "devices/validate", "message_id": "1", "args": {"configuration": "livingroom.yaml"}}
    ]
    assert result["success"] is True
    assert "Checking configuration...\n" in result["log_tail"]


def test_validate_config_surfaces_ws_error_code(monkeypatch):
    import websockets

    fake_connect = _FakeMultiplexConnect(
        {"requires_auth": False},
        [{"message_id": "1", "error_code": "not_found", "details": "no such file"}],
    )
    monkeypatch.setattr(websockets, "connect", fake_connect)

    result = _unwrap(esphome_tools.validate_config)("missing")

    assert result["error"] == "not_found"


def test_validate_config_refuses_when_dashboard_requires_auth(monkeypatch):
    """No ESPHOME_USERNAME/ESPHOME_PASSWORD wiring exists — fail fast and clearly
    rather than hang or guess credentials."""
    import websockets

    fake_connect = _FakeMultiplexConnect({"requires_auth": True}, [])
    monkeypatch.setattr(websockets, "connect", fake_connect)

    result = _unwrap(esphome_tools.validate_config)("livingroom")

    assert "error" in result
    assert fake_connect.instances[0].sent == []


def test_validate_config_rejects_path_traversal_before_opening_any_socket(monkeypatch):
    import websockets

    def _must_not_connect(url, **kwargs):
        raise AssertionError("must not open a websocket for an invalid device name")

    monkeypatch.setattr(websockets, "connect", _must_not_connect)

    result = _unwrap(esphome_tools.validate_config)("../../etc/passwd")

    assert "error" in result


def test_compile_device_docstring_confirms_device_builder_compat():
    doc = _unwrap(esphome_tools.compile_device).__doc__
    assert "device-builder" in doc.lower()


def test_upload_device_docstring_confirms_device_builder_compat():
    doc = _unwrap(esphome_tools.upload_device).__doc__
    assert "device-builder" in doc.lower()


def test_validate_config_docstring_documents_protocol_change():
    doc = _unwrap(esphome_tools.validate_config).__doc__
    assert "devices/validate" in doc
    assert "/ws" in doc


def test_clean_mqtt_docstring_documents_ha_mqtt_mechanism_not_dashboard():
    """Superseded by nexus 0.22.0 D3 (see coordinator decision + ADR follow-up):
    clean_mqtt no longer calls the ESPHome dashboard at all (Device Builder's
    `/ws` API has no MQTT-topic-clearing command, confirmed against
    docs/API.md) — it goes through HA's own `mqtt` integration instead
    (`mqtt/device/debug_info` WS command when a matching HA device exists,
    else a short `mqtt/subscribe` wildcard window; `mqtt.publish` with an
    empty retained payload to clear each topic). This replaces the prior
    "WARNING: unverified / likely broken" caveat, which described the
    pre-D3 state where clean_mqtt still (uselessly) called the dashboard's
    removed `/clean-mqtt` route."""
    doc = _unwrap(esphome_tools.clean_mqtt).__doc__

    assert "mqtt/device/debug_info" in doc
    assert re.search(r"mqtt/subscribe|wildcard_subscribe", doc)
    assert "mqtt.publish" in doc
    assert re.search(r"no.{0,15}mqtt.{0,15}section", doc, re.IGNORECASE), (
        "docstring must state the no-mqtt-section skip path"
    )


# --- ESPHome add-on slug: discover dynamically instead of hard-coded guesses --------

def test_discover_esphome_slug_finds_addon_by_suffix_pattern(monkeypatch):
    def fake_sup_json(method, path, body=None, timeout=30):
        assert (method, path) == ("GET", "/addons")
        return {"data": {"addons": [
            {"slug": "core_mosquitto", "state": "started"},
            {"slug": "5c53de3b_esphome", "name": "ESPHome Device Builder", "state": "started"},
        ]}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    assert esphome_tools._discover_esphome_slug() == "5c53de3b_esphome"


def test_discover_esphome_slug_prefers_started_over_stopped(monkeypatch):
    def fake_sup_json(method, path, body=None, timeout=30):
        return {"data": {"addons": [
            {"slug": "a0d7b954_esphome", "state": "stopped"},
            {"slug": "5c53de3b_esphome", "state": "started"},
        ]}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    assert esphome_tools._discover_esphome_slug() == "5c53de3b_esphome"


def test_discover_esphome_slug_stable_order_when_several_started(monkeypatch):
    def fake_sup_json(method, path, body=None, timeout=30):
        return {"data": {"addons": [
            {"slug": "z_esphome", "state": "started"},
            {"slug": "a_esphome", "state": "started"},
        ]}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    assert esphome_tools._discover_esphome_slug() == "a_esphome"


def test_discover_esphome_slug_returns_none_when_addons_list_unavailable(monkeypatch):
    monkeypatch.setattr(
        esphome_tools, "_sup_json", lambda *a, **k: {"error": "SUPERVISOR_TOKEN not set"}
    )

    assert esphome_tools._discover_esphome_slug() is None


def test_esphome_slug_candidates_falls_back_to_hardcoded_list_when_discovery_fails(monkeypatch):
    monkeypatch.setattr(esphome_tools, "_discover_esphome_slug", lambda: None)

    assert esphome_tools._esphome_slug_candidates() == esphome_tools._ESPHOME_SLUGS


def test_get_addon_info_uses_dynamically_discovered_slug(monkeypatch):
    def fake_sup_json(method, path, body=None, timeout=30):
        if path == "/addons":
            return {"data": {"addons": [{"slug": "5c53de3b_esphome", "state": "started"}]}}
        assert path == "/addons/5c53de3b_esphome/info"
        return {"data": {"name": "ESPHome Device Builder", "state": "started", "version": "2026.9.0"}}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    result = _unwrap(esphome_tools.get_addon_info)()

    assert result["slug"] == "5c53de3b_esphome"
    assert result["version"] == "2026.9.0"


def test_get_addon_info_falls_back_to_hardcoded_slugs_when_addons_list_unavailable(monkeypatch):
    def fake_sup_json(method, path, body=None, timeout=30):
        if path == "/addons":
            return {"error": "SUPERVISOR_TOKEN not set"}
        if path == "/addons/a0d7b954_esphome/info":
            return {"data": {"name": "ESPHome", "state": "started", "version": "2026.5.0"}}
        return {"error": "not found"}

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)

    result = _unwrap(esphome_tools.get_addon_info)()

    assert result["slug"] == "a0d7b954_esphome"


def test_get_addon_logs_uses_dynamically_discovered_slug(monkeypatch):
    def fake_sup_json(method, path, body=None, timeout=30):
        return {"data": {"addons": [{"slug": "5c53de3b_esphome", "state": "started"}]}}

    def fake_sup_text(path, timeout=30):
        assert path == "/addons/5c53de3b_esphome/logs"
        return "line1\nline2\n"

    monkeypatch.setattr(esphome_tools, "_sup_json", fake_sup_json)
    monkeypatch.setattr(esphome_tools, "_sup_text", fake_sup_text)

    result = _unwrap(esphome_tools.get_addon_logs)()

    assert result["slug"] == "5c53de3b_esphome"
    assert "line1" in result["logs"]


# --- Security: device-name path traversal (get_config/write_config/compile/validate/
# upload/clean_mqtt/lvgl_*) must be rejected, not resolved against _ESPHOME_DIR -------

def test_device_path_rejects_forward_slash_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    with pytest.raises(ValueError):
        esphome_tools._device_path("../../../etc/cron.d/evil")


def test_device_path_rejects_backslash_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    with pytest.raises(ValueError):
        esphome_tools._device_path("..\\..\\windows\\win.ini")


def test_device_path_rejects_leading_dot(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    with pytest.raises(ValueError):
        esphome_tools._device_path(".hidden")


def test_device_path_rejects_blank_name(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    with pytest.raises(ValueError):
        esphome_tools._device_path("   ")


def test_device_path_accepts_plain_name_with_and_without_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    expected = (tmp_path / "kuchnia.yaml").resolve()
    assert esphome_tools._device_path("kuchnia") == expected
    assert esphome_tools._device_path("kuchnia.yaml") == expected


def test_get_config_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path / "esphome")
    secret = tmp_path / "secret.yaml"
    secret.write_text("top secret", encoding="utf-8")

    result = _unwrap(esphome_tools.get_config)("../secret")

    assert "error" in result
    assert "content" not in result


def test_get_config_valid_name_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    (tmp_path / "kuchnia.yaml").write_text("esphome: {}\n", encoding="utf-8")

    result = _unwrap(esphome_tools.get_config)("kuchnia")

    assert result["content"] == "esphome: {}\n"


def test_write_config_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path / "esphome")

    result = _unwrap(esphome_tools.write_config)("../../cron.d/evil", "esphome: {}\n")

    assert result["success"] is False
    assert "error" in result
    assert not (tmp_path / "cron.d").exists()


def test_write_config_rejects_absolute_path_name(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path / "esphome")

    result = _unwrap(esphome_tools.write_config)("/etc/cron.d/evil", "esphome: {}\n")

    assert result["success"] is False
    assert "error" in result


def test_write_config_valid_name_still_works(tmp_path, monkeypatch):
    esphome_dir = tmp_path / "esphome"
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", esphome_dir)

    result = _unwrap(esphome_tools.write_config)("kuchnia.yaml", "esphome: {}\n")

    assert result["success"] is True
    assert (esphome_dir / "kuchnia.yaml").read_text(encoding="utf-8") == "esphome: {}\n"


def test_compile_device_rejects_path_traversal(monkeypatch):
    import websockets

    def _must_not_connect(url, **kwargs):
        raise AssertionError("must not open a websocket for an invalid device name")

    monkeypatch.setattr(websockets, "connect", _must_not_connect)

    result = _unwrap(esphome_tools.compile_device)("../../etc/passwd")

    assert "error" in result


def test_lvgl_add_widget_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)

    result = _unwrap(esphome_tools.lvgl_add_widget)(
        "../outside", "home", "label", {"id": "lbl1"}
    )

    assert "error" in result


# --- F24: lvgl_add_widget / lvgl_delete_widget must back up before rewriting ---------

def _write_device_yaml(esphome_dir, name: str, content: str):
    esphome_dir.mkdir(parents=True, exist_ok=True)
    path = esphome_dir / f"{name}.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_lvgl_add_widget_writes_backup_before_rewriting(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    original = (
        "# hand-written comment that yaml.dump will NOT preserve\n"
        "lvgl:\n"
        "  pages:\n"
        "    - id: home\n"
        "      widgets: []\n"
    )
    path = _write_device_yaml(tmp_path, "display1", original)

    result = _unwrap(esphome_tools.lvgl_add_widget)(
        "display1", "home", "label", {"id": "lbl1", "text": "Hello"}
    )

    assert result["added"] == "label"
    backup_path = path.with_name(path.name + ".bak")
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == original
    # The live file was rewritten (comment gone) — that's the documented trade-off.
    assert "hand-written comment" not in path.read_text(encoding="utf-8")


def test_lvgl_delete_widget_writes_backup_before_rewriting(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    original = (
        "lvgl:\n"
        "  pages:\n"
        "    - id: home\n"
        "      widgets:\n"
        "        - label:\n"
        "            id: lbl1\n"
        "            text: Hello\n"
    )
    path = _write_device_yaml(tmp_path, "display1", original)

    result = _unwrap(esphome_tools.lvgl_delete_widget)("display1", "home", "lbl1")

    assert result["deleted"] == "lbl1"
    backup_path = path.with_name(path.name + ".bak")
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == original


def test_lvgl_add_widget_docstring_warns_about_full_file_rewrite():
    doc = _unwrap(esphome_tools.lvgl_add_widget).__doc__
    assert "rewrites the entire device YAML file" in doc
    assert ".bak" in doc


# --- F26: blueprint import must not claim it saves; blueprint/save must persist -----

def test_import_blueprint_only_fetches_and_validates_does_not_save(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        return {
            "suggested_filename": "author/my_blueprint.yaml",
            "raw_data": "blueprint:\n  name: My blueprint\n",
            "exists": False,
        }

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(blueprints_tools.import_blueprint)("https://example.com/bp.yaml")

    assert calls == ["blueprint/import"]
    assert "blueprint/save" not in calls
    assert result["suggested_filename"] == "author/my_blueprint.yaml"


def test_import_blueprint_docstring_does_not_claim_it_saves_to_disk():
    doc = _unwrap(blueprints_tools.import_blueprint).__doc__
    assert "does NOT save" in doc
    assert "into /config/blueprints" not in doc


def test_save_blueprint_calls_blueprint_save_with_expected_fields(monkeypatch):
    seen = {}

    def fake_ws_call(msg_type, **kwargs):
        seen["type"] = msg_type
        seen["kwargs"] = kwargs
        return {"overrides_existing": False}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(blueprints_tools.save_blueprint)(
        "author/my_blueprint.yaml", "blueprint:\n  name: My blueprint\n"
    )

    assert seen["type"] == "blueprint/save"
    assert seen["kwargs"]["domain"] == "automation"
    assert seen["kwargs"]["path"] == "author/my_blueprint.yaml"
    assert seen["kwargs"]["yaml"] == "blueprint:\n  name: My blueprint\n"
    assert seen["kwargs"]["allow_override"] is False
    assert result["overrides_existing"] is False


def test_save_blueprint_does_not_overwrite_by_default_but_can_opt_in(monkeypatch):
    seen = {}
    monkeypatch.setattr(ha, "_ws_call", lambda t, **k: seen.update(kwargs=k) or {})

    _unwrap(blueprints_tools.save_blueprint)("p.yaml", "yaml text", overwrite=True)

    assert seen["kwargs"]["allow_override"] is True
