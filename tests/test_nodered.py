"""`tools/nodered.py` — Node-RED add-on tools, against a fake Supervisor + fake
Node-RED Admin API (`httpx.MockTransport`, same pattern as `test_adguard.py`).

Covered: add-on lookup (not installed / stopped / not host-networked / missing
token), Node-RED transport errors (401, connection refused, 409 rev conflict),
read tools (`list_flows`, `get_flow` with credential/secret redaction,
`search_nodes`, `find_entity_usage`, `get_info`, `get_diagnostics`), write
tools (`create_flow`, `update_flow`, `delete_flow`, `set_flow_enabled`,
`restore_backup`) including backup-before-deploy, fail-closed validation
(unknown node types, id conflicts, placeholders in new nodes, failed backup)
and `list_backups`.
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from tools import nodered

_RealClient = httpx.Client
_TOKEN = "sup-token-do-not-leak"
_SLUG = "a0d7b954_nodered"
_SECRET = "s3cr3t-mqtt-pass"


def _unwrap(tool):
    return getattr(tool, "fn", tool)


list_flows = _unwrap(nodered.list_flows)
get_flow = _unwrap(nodered.get_flow)
search_nodes = _unwrap(nodered.search_nodes)
find_entity_usage = _unwrap(nodered.find_entity_usage)
get_info = _unwrap(nodered.get_info)
get_diagnostics = _unwrap(nodered.get_diagnostics)
list_backups = _unwrap(nodered.list_backups)
create_flow = _unwrap(nodered.create_flow)
update_flow = _unwrap(nodered.update_flow)
delete_flow = _unwrap(nodered.delete_flow)
set_flow_enabled = _unwrap(nodered.set_flow_enabled)
restore_backup = _unwrap(nodered.restore_backup)


def _initial_flows() -> list[dict]:
    return [
        {"id": "tab1", "type": "tab", "label": "Lights", "disabled": False, "info": "", "env": []},
        {"id": "tab2", "type": "tab", "label": "Alarm", "disabled": True, "info": "", "env": []},
        {"id": "sub1", "type": "subflow", "name": "Notify", "in": [], "out": []},
        {"id": "srv", "type": "server", "name": "Home Assistant", "addon": True},
        {
            "id": "broker",
            "type": "mqtt-broker",
            "name": "Mosquitto",
            "broker": f"mqtt://iot:{_SECRET}@core-mosquitto",
        },
        {
            "id": "n1",
            "type": "server-state-changed",
            "z": "tab1",
            "name": "Kitchen motion",
            "server": "srv",
            "entities": {"entity": ["binary_sensor.kitchen_motion"], "substring": [], "regex": []},
            "x": 100,
            "y": 80,
            "wires": [["n2"]],
        },
        {
            "id": "n2",
            "type": "api-call-service",
            "z": "tab1",
            "name": "Kitchen light",
            "server": "srv",
            "action": "light.turn_on",
            "target": {"entityId": ["light.kitchen"]},
            "x": 300,
            "y": 80,
            "wires": [[]],
        },
        {
            "id": "n3",
            "type": "http request",
            "z": "tab1",
            "name": "Webhook",
            "url": "https://example.test/hook",
            "apiKey": "abc123-api-key",
            "x": 300,
            "y": 160,
            "wires": [[]],
        },
        {
            "id": "n4",
            "type": "api-render-template",
            "z": "tab2",
            "name": "Alarm text",
            "template": "Door is {{ states('binary_sensor.front_door') }}",
            "x": 100,
            "y": 80,
            "wires": [[]],
        },
        {
            "id": "n5",
            "type": "trigger-state",
            "z": "tab2",
            "name": "Any door",
            "entities": {"entity": [], "substring": ["_door"], "regex": []},
            "x": 100,
            "y": 160,
            "wires": [[]],
        },
        {
            "id": "n6",
            "type": "poll-state",
            "z": "tab2",
            "name": "Old style",
            "entityId": "^sensor\\.temp_.*$",
            "entityIdType": "regex",
            "x": 100,
            "y": 240,
            "wires": [[]],
        },
        {"id": "s1", "type": "debug", "z": "sub1", "name": "inside subflow", "x": 1, "y": 1, "wires": []},
        {"id": "i1", "type": "subflow:sub1", "z": "tab1", "name": "notify me", "x": 500, "y": 80, "wires": []},
    ]


_NODE_SETS = [
    {
        "id": "node-red/inject",
        "module": "node-red",
        "version": "5.0.9",
        "enabled": True,
        "types": ["inject"],
    },
    {
        "id": "node-red/debug",
        "module": "node-red",
        "version": "5.0.9",
        "enabled": True,
        "types": ["debug"],
    },
    {
        "id": "node-red/mqtt",
        "module": "node-red",
        "version": "5.0.9",
        "enabled": True,
        "types": ["mqtt in", "mqtt out", "mqtt-broker"],
    },
    {
        "id": "node-red/httprequest",
        "module": "node-red",
        "version": "5.0.9",
        "enabled": True,
        "types": ["http request"],
    },
    {
        "id": "node-red-contrib-home-assistant-websocket/all",
        "module": "node-red-contrib-home-assistant-websocket",
        "version": "0.82.0",
        "enabled": True,
        "user": False,
        "types": ["server", "server-state-changed", "api-call-service", "trigger-state", "poll-state",
                  "api-render-template"],
    },
    {
        "id": "node-red-contrib-old/x",
        "module": "node-red-contrib-old",
        "version": "1.0.0",
        "enabled": False,
        "user": True,
        "types": ["old-node"],
    },
]


class FakeHA:
    """Supervisor + Node-RED Admin API behind one MockTransport."""

    def __init__(self) -> None:
        self.addons = [
            {"slug": "core_mosquitto", "state": "started"},
            {"slug": _SLUG, "state": "started"},
        ]
        self.info = {"slug": _SLUG, "state": "started", "host_network": True, "version": "22.2.2"}
        self.flows = _initial_flows()
        self.rev = "rev-1"
        self.rev_counter = 1
        self.status: dict[str, int] = {}
        self.refuses = False
        self.force_conflict = False
        self.requests: list[httpx.Request] = []
        self.posted: list[dict] = []
        self.stored_credentials: dict[str, dict] = {}
        self.credential_types = {"http-request", "mqtt-broker", "server"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "supervisor":
            return self._supervisor(request)
        if request.url.host == "127.0.0.1" and request.url.port == 46836:
            if self.refuses:
                raise httpx.ConnectError("Connection refused", request=request)
            return self._nodered(request)
        raise AssertionError(f"unexpected host {request.url}")

    def _supervisor(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == f"Bearer {_TOKEN}"
        if request.url.path == "/addons":
            return httpx.Response(200, json={"result": "ok", "data": {"addons": self.addons}})
        if request.url.path == f"/addons/{_SLUG}/info":
            return httpx.Response(200, json={"result": "ok", "data": self.info})
        return httpx.Response(404, text="not found")

    def _nodered(self, request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers, "Supervisor token must not reach Node-RED"
        path = request.url.path
        key = f"{request.method} {path}"
        if key in self.status:
            return httpx.Response(self.status[key], json={"code": "boom", "message": f"error on {path}"})
        if key == "GET /flows":
            assert request.headers.get("Node-RED-API-Version") == "v2"
            return httpx.Response(200, json={"rev": self.rev, "flows": copy.deepcopy(self.flows)})
        if key == "POST /flows":
            assert request.headers.get("Node-RED-API-Version") == "v2"
            assert request.headers.get("Node-RED-Deployment-Type") == "flows"
            body = json.loads(request.content)
            self.posted.append(body)
            # Mirrors @node-red/runtime lib/api/flows.js setFlows: rev check -> 409.
            if self.force_conflict or body.get("rev") != self.rev:
                return httpx.Response(409, json={"code": "version_mismatch", "message": "Error"})
            self.flows = body["flows"]
            self.rev_counter += 1
            self.rev = f"rev-{self.rev_counter}"
            return httpx.Response(200, json={"rev": self.rev})
        if key == "GET /nodes":
            assert request.headers.get("Accept") == "application/json"
            return httpx.Response(200, json=_NODE_SETS)
        if key == "GET /settings":
            return httpx.Response(
                200,
                json={
                    "httpNodeRoot": "/endpoint",
                    "version": "5.0.9",
                    "context": {"default": "memory", "stores": ["memory"]},
                    "flowEncryptionType": "user",
                    "libraries": [{"id": "local"}],
                    "editorTheme": {"theme": "dark"},
                },
            )
        if request.method == "GET" and path.startswith("/credentials/"):
            # @node-red/editor-api lib/editor/credentials.js -> runtime getNodeCredentials: the definition is
            # looked up by the exact type, but registered with spaces turned into dashes
            # (runtime/lib/nodes/credentials.js `register`: type.replace(/\s+/g, '-')). An unknown type
            # has no definition, so the answer is {} even when credentials are stored.
            _, _, node_type, node_id = path.split("/", 3)
            if node_type not in self.credential_types:
                return httpx.Response(200, json={})
            return httpx.Response(200, json=self.stored_credentials.get(node_id, {}))
        if key == "GET /flows/state":
            return httpx.Response(200, json={"state": "start"})
        if key == "GET /diagnostics":
            return httpx.Response(
                200,
                json={
                    "report": "diagnostics",
                    "runtime": {"version": "5.0.9", "settings": {"adminAuth": "UNSET", "uiPort": "SET"}},
                    "nodejs": {"version": "v22.0.0"},
                    "extra": {"secret_thing": "leak-me-not"},
                },
            )
        return httpx.Response(404, json={"code": "not_found"})

    def nodered_requests(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.url.host == "127.0.0.1"]


@pytest.fixture
def fake(monkeypatch, tmp_path) -> FakeHA:
    ha = FakeHA()
    transport = httpx.MockTransport(ha.handler)
    monkeypatch.setattr(nodered.httpx, "Client", lambda **kw: _RealClient(transport=transport, **kw))
    monkeypatch.setenv("SUPERVISOR_TOKEN", _TOKEN)
    monkeypatch.setenv("NEXUS_DATA_PATH", str(tmp_path))
    return ha


def _dump(result) -> str:
    return json.dumps(result, default=str)


def _backups(tmp_path) -> list:
    d = tmp_path / "nodered_backups"
    return sorted(d.glob("flows-*.json")) if d.is_dir() else []


# --- add-on lookup / transport ----------------------------------------------


def test_list_flows_counts_and_rev(fake):
    result = list_flows()

    assert result["addon_slug"] == _SLUG
    assert result["rev"] == "rev-1"
    assert result["flows"] == [
        {"id": "tab1", "label": "Lights", "disabled": False, "node_count": 4},
        {"id": "tab2", "label": "Alarm", "disabled": True, "node_count": 3},
    ]
    assert result["subflows"] == [{"id": "sub1", "name": "Notify", "node_count": 1, "instance_count": 1}]
    assert result["global_config_nodes"] == 2
    assert _TOKEN not in _dump(result)


def test_addon_not_installed_does_no_nodered_io(fake):
    fake.addons = [{"slug": "core_mosquitto", "state": "started"}]

    assert list_flows()["error"] == "nodered_not_found"
    assert fake.nodered_requests() == []


def test_addon_stopped(fake):
    fake.info["state"] = "stopped"

    result = list_flows()

    assert result["error"] == "nodered_not_running"
    assert result["state"] == "stopped"
    assert fake.nodered_requests() == []


def test_addon_without_host_network_is_refused(fake):
    fake.info["host_network"] = False

    assert list_flows()["error"] == "nodered_unsupported_addon"
    assert fake.nodered_requests() == []


def test_missing_supervisor_token_does_no_io(fake, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN")

    result = list_flows()

    assert "SUPERVISOR_TOKEN not set" in result["error"]
    assert fake.requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_admin_auth_enabled_is_reported(fake, status):
    fake.status["GET /flows"] = status

    assert list_flows()["error"] == "nodered_auth_required"


def test_connection_refused(fake):
    fake.refuses = True

    result = list_flows()

    assert result["error"] == "nodered_unreachable"
    assert "127.0.0.1:46836" in result["message"]


def test_http_error_carries_nodered_detail(fake):
    fake.status["GET /flows"] = 500

    result = list_flows()

    assert result["error"] == "HTTP 500"
    assert result["detail"]["code"] == "boom"


# --- get_flow / redaction -----------------------------------------------------


def test_get_flow_returns_members_with_secrets_redacted(fake):
    fake.flows[7]["credentials"] = {"user": "u", "password": "should-not-be-here"}

    result = get_flow("tab1")

    assert result["flow"]["label"] == "Lights"
    assert {n["id"] for n in result["nodes"]} == {"n1", "n2", "n3", "i1"}
    webhook = next(n for n in result["nodes"] if n["id"] == "n3")
    assert webhook["apiKey"] == nodered._REDACTED
    assert webhook["credentials"] == nodered._REDACTED
    assert webhook["url"] == "https://example.test/hook"
    paths = [f["path"] for f in result["redacted_fields"]]
    assert paths.count("[1][2].credentials") == 1
    assert "[1][2].apiKey" in paths
    dumped = _dump(result)
    assert "abc123-api-key" not in dumped
    assert "should-not-be-here" not in dumped


def test_get_flow_unknown_id(fake):
    result = get_flow("nope")

    assert result["error"] == "flow_not_found"


# --- search_nodes -------------------------------------------------------------


def test_search_nodes_by_type_and_name(fake):
    result = search_nodes(type="API-CALL", name="kitchen")

    assert result["count"] == 1
    hit = result["matches"][0]
    assert hit == {
        "id": "n2",
        "type": "api-call-service",
        "name": "Kitchen light",
        "flow_id": "tab1",
        "flow_label": "Lights",
        "in_subflow": False,
        "disabled": False,
    }
    assert "config" not in hit


def test_search_nodes_requires_a_filter_without_io(fake):
    assert search_nodes()["error"] == "no_filter"
    assert fake.requests == []


def test_search_nodes_limit_include_config_and_redaction(fake):
    result = search_nodes(name="o", include_config=True, limit=2)

    assert result["truncated"] is True
    assert len(result["matches"]) == 2
    assert all("config" in m for m in result["matches"])
    broker = search_nodes(type="mqtt-broker", include_config=True)["matches"][0]
    assert broker["config"]["broker"] == nodered._REDACTED
    assert broker["flow_id"] is None
    assert _SECRET not in _dump(broker)


def test_search_nodes_marks_disabled_flow_and_subflow(fake):
    alarm = search_nodes(name="any door")["matches"][0]
    inner = search_nodes(name="inside subflow")["matches"][0]

    assert alarm["disabled"] is True
    assert inner["in_subflow"] is True
    assert inner["flow_label"] == "Notify"


# --- find_entity_usage ----------------------------------------------------------


def test_find_entity_usage_exact_selector(fake):
    result = find_entity_usage("binary_sensor.kitchen_motion")

    assert [n["id"] for n in result["nodes"]] == ["n1"]
    assert result["nodes"][0]["match"] == ["exact"]
    assert result["nodes"][0]["fields"] == ["entities.entity[0]"]
    assert result["flows"] == [{"flow_id": "tab1", "flow_label": "Lights", "node_count": 1}]


def test_find_entity_usage_action_target(fake):
    result = find_entity_usage("light.kitchen")

    assert [n["id"] for n in result["nodes"]] == ["n2"]
    assert result["nodes"][0]["fields"] == ["target.entityId[0]"]


def test_find_entity_usage_template_text_and_substring_selector(fake):
    result = find_entity_usage("binary_sensor.front_door")

    by_id = {n["id"]: n for n in result["nodes"]}
    assert set(by_id) == {"n4", "n5"}
    assert by_id["n4"]["match"] == ["text"]
    assert by_id["n5"]["match"] == ["substring"]
    assert by_id["n4"]["disabled"] is True


def test_find_entity_usage_legacy_regex(fake):
    result = find_entity_usage("sensor.temp_living")

    assert [n["id"] for n in result["nodes"]] == ["n6"]
    assert result["nodes"][0]["match"] == ["regex"]


def test_find_entity_usage_respects_word_boundaries(fake):
    # `sensor.kitchen_motion` is not `binary_sensor.kitchen_motion`.
    assert find_entity_usage("sensor.kitchen_motion")["count"] == 0
    # `light.kitchen` must not match `light.kitchen_2`.
    fake.flows[6]["target"]["entityId"] = ["light.kitchen_2"]
    assert find_entity_usage("light.kitchen")["count"] == 0


@pytest.mark.parametrize("bad", ["", "kitchen", "Light.Kitchen", "light.kitchen/../x"])
def test_find_entity_usage_rejects_bad_entity_id_without_io(fake, bad):
    assert find_entity_usage(bad)["error"] == "invalid_entity_id"
    assert fake.requests == []


# --- get_info / get_diagnostics ---------------------------------------------------


def test_get_info_projects_settings_and_modules(fake):
    result = get_info()

    assert result["addon_version"] == "22.2.2"
    assert result["nodered_version"] == "5.0.9"
    assert "libraries" not in result["settings"]
    assert "editorTheme" not in result["settings"]
    assert result["settings"]["httpNodeRoot"] == "/endpoint"
    assert result["flows_state"] == {"state": "start"}
    modules = {m["module"]: m for m in result["modules"]}
    assert modules["node-red"]["types"] == 6
    assert modules["node-red-contrib-home-assistant-websocket"]["version"] == "0.82.0"
    assert modules["node-red-contrib-old"]["enabled"] is False
    assert modules["node-red-contrib-old"]["user_installed"] is True


def test_get_info_section_error_stays_local(fake):
    fake.status["GET /nodes"] = 500

    result = get_info()

    assert result["modules"]["error"] == "HTTP 500"
    assert result["nodered_version"] == "5.0.9"


def test_get_diagnostics_redacts(fake):
    result = get_diagnostics()

    assert result["report"]["runtime"]["version"] == "5.0.9"
    assert result["report"]["extra"]["secret_thing"] == nodered._REDACTED
    assert "leak-me-not" not in _dump(result)


# --- create_flow --------------------------------------------------------------------

_NEW_NODES = [
    {"id": "a1", "type": "inject", "x": 100, "y": 80, "wires": [["a2"]]},
    {"id": "a2", "type": "debug", "x": 300, "y": 80, "wires": []},
]


def test_create_flow_backs_up_then_deploys_with_rev(fake, tmp_path):
    before = copy.deepcopy(fake.flows)

    result = create_flow("Garage", copy.deepcopy(_NEW_NODES), info="test flow")

    assert result["created"] is True
    assert result["rev"] == "rev-2"
    flow_id = result["flow_id"]
    assert len(flow_id) == 16
    posted = fake.posted[-1]
    assert posted["rev"] == "rev-1"
    tab = next(n for n in posted["flows"] if n["id"] == flow_id)
    assert tab == {"id": flow_id, "type": "tab", "label": "Garage", "disabled": False, "info": "test flow", "env": []}
    assert {n["z"] for n in posted["flows"] if n["id"] in ("a1", "a2")} == {flow_id}
    files = _backups(tmp_path)
    assert [f.stem for f in files] == [result["backup_id"]]
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["flows"] == before
    assert saved["rev"] == "rev-1"
    assert "create_flow" in saved["reason"]


def test_create_flow_unknown_type_is_refused_before_backup(fake, tmp_path):
    nodes = [*copy.deepcopy(_NEW_NODES), {"id": "a3", "type": "not-installed", "x": 1, "y": 1, "wires": []}]

    result = create_flow("Garage", nodes)

    assert result["error"] == "unknown_node_types"
    assert result["types"] == ["not-installed"]
    assert fake.posted == []
    assert _backups(tmp_path) == []


def test_create_flow_disabled_module_type_and_missing_subflow_are_unknown(fake):
    nodes = [
        {"id": "a3", "type": "old-node", "x": 1, "y": 1, "wires": []},
        {"id": "a4", "type": "subflow:missing", "x": 1, "y": 1, "wires": []},
        {"id": "a5", "type": "subflow:sub1", "x": 1, "y": 1, "wires": []},
    ]

    result = create_flow("Garage", nodes)

    assert result["types"] == ["old-node", "subflow:missing"]


def test_create_flow_id_conflict_with_other_flow(fake):
    nodes = [{"id": "n1", "type": "debug", "x": 1, "y": 1, "wires": []}]

    result = create_flow("Garage", nodes)

    assert result["error"] == "id_conflict"
    assert result["ids"] == ["n1"]
    assert fake.posted == []


@pytest.mark.parametrize(
    "nodes",
    [
        [{"type": "debug"}],
        [{"id": "x", "type": ""}],
        [{"id": "x", "type": "tab"}],
        [{"id": "x", "type": "debug"}, {"id": "x", "type": "debug"}],
        ["not an object"],
    ],
)
def test_create_flow_invalid_nodes(fake, nodes):
    result = create_flow("Garage", nodes)

    assert result["error"] == "invalid_nodes"
    assert result["problems"]
    assert fake.posted == []


def test_create_flow_rejects_empty_label_without_io(fake):
    assert create_flow("   ", [])["error"] == "invalid_label"
    assert fake.requests == []


def test_create_flow_expected_rev_mismatch_writes_nothing(fake, tmp_path):
    result = create_flow("Garage", copy.deepcopy(_NEW_NODES), expected_rev="rev-0")

    assert result["error"] == "rev_conflict"
    assert result["current_rev"] == "rev-1"
    assert fake.posted == []
    assert _backups(tmp_path) == []


def test_create_flow_nodered_409_is_rev_conflict(fake):
    fake.force_conflict = True

    result = create_flow("Garage", copy.deepcopy(_NEW_NODES))

    assert result["error"] == "rev_conflict"
    assert fake.rev == "rev-1"


def test_failed_backup_aborts_before_deploy(fake, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("NEXUS_DATA_PATH", str(blocker))

    result = create_flow("Garage", copy.deepcopy(_NEW_NODES))

    assert result["error"] == "backup_failed"
    assert fake.posted == []


# --- update_flow ----------------------------------------------------------------------


def test_update_flow_round_trip_restores_placeholders(fake):
    current = get_flow("tab1")
    nodes = current["nodes"]
    webhook = next(n for n in nodes if n["id"] == "n3")
    assert webhook["apiKey"] == nodered._REDACTED
    webhook["name"] = "Webhook v2"
    nodes = [n for n in nodes if n["id"] != "i1"]
    nodes.append({"id": "b1", "type": "debug", "x": 1, "y": 1, "wires": []})

    result = update_flow("tab1", nodes, label="Lights v2", expected_rev=current["rev"])

    assert result["updated"] is True
    assert result["added"] == ["b1"]
    assert result["removed"] == ["i1"]
    assert result["kept"] == 3
    posted = {n["id"]: n for n in fake.posted[-1]["flows"]}
    assert posted["n3"]["apiKey"] == "abc123-api-key"
    assert posted["n3"]["name"] == "Webhook v2"
    assert "i1" not in posted
    assert posted["tab1"]["label"] == "Lights v2"
    assert posted["n4"]["z"] == "tab2"  # other flows untouched
    assert posted["broker"]["broker"].startswith("mqtt://iot:")


def test_update_flow_drops_credentials_placeholder(fake):
    nodes = copy.deepcopy([n for n in fake.flows if n.get("z") == "tab1"])
    nodes[0]["credentials"] = nodered._REDACTED

    assert update_flow("tab1", nodes)["updated"] is True
    n1 = next(n for n in fake.posted[-1]["flows"] if n["id"] == "n1")
    assert "credentials" not in n1


def test_update_flow_placeholder_in_new_node_is_refused(fake):
    nodes = [{"id": "new", "type": "debug", "token": nodered._REDACTED, "x": 1, "y": 1, "wires": []}]

    result = update_flow("tab1", nodes)

    assert result["error"] == "invalid_nodes"
    assert fake.posted == []


def test_update_flow_unknown_flow(fake):
    assert update_flow("nope", [])["error"] == "flow_not_found"
    assert fake.posted == []


# --- delete_flow / set_flow_enabled ------------------------------------------------------


def test_delete_flow_removes_tab_and_members_only(fake, tmp_path):
    result = delete_flow("tab1")

    assert result["deleted"] is True
    assert result["removed_nodes"] == 4
    ids = {n["id"] for n in fake.posted[-1]["flows"]}
    assert ids == {"tab2", "sub1", "srv", "broker", "n4", "n5", "n6", "s1"}
    assert len(_backups(tmp_path)) == 1


def test_set_flow_enabled_same_state_writes_nothing(fake, tmp_path):
    result = set_flow_enabled("tab1", True)

    assert result["changed"] is False
    assert result["backup_id"] is None
    assert fake.posted == []
    assert _backups(tmp_path) == []


def test_set_flow_enabled_flips_only_the_flag(fake):
    result = set_flow_enabled("tab2", True)

    assert result["changed"] is True
    posted = fake.posted[-1]["flows"]
    assert next(n for n in posted if n["id"] == "tab2")["disabled"] is False
    expected = _initial_flows()
    expected[1]["disabled"] = False
    assert posted == expected


# --- backups ------------------------------------------------------------------------------


def test_restore_backup_round_trip(fake, tmp_path):
    original = copy.deepcopy(fake.flows)
    deleted = delete_flow("tab1")
    assert {n["id"] for n in fake.flows}.isdisjoint({"tab1", "n1"})

    result = restore_backup(deleted["backup_id"])

    assert result["restored"] is True
    assert result["flow_count"] == 2
    assert fake.flows == original
    assert result["safety_backup_id"] != deleted["backup_id"]
    listing = list_backups()
    assert listing["count"] == 2
    assert listing["backups"][0]["backup_id"] == result["safety_backup_id"]
    assert listing["backups"][0]["flow_count"] == 1


@pytest.mark.parametrize("bad", ["../../etc/passwd", "flows-1", "", "flows-20261111T120000000000Z.json"])
def test_restore_backup_rejects_bad_id_without_io(fake, bad):
    assert restore_backup(bad)["error"] == "invalid_backup_id"
    assert fake.requests == []


def test_restore_backup_not_found(fake):
    assert restore_backup("flows-20261111T120000000000Z")["error"] == "backup_not_found"
    assert fake.posted == []


def test_backups_are_pruned_to_newest(fake, tmp_path):
    d = tmp_path / "nodered_backups"
    d.mkdir()
    for i in range(nodered._MAX_BACKUPS + 5):
        (d / f"flows-20260101T0000000000{i:02d}Z.json").write_text(json.dumps({"flows": []}))

    result = set_flow_enabled("tab1", False)

    files = _backups(tmp_path)
    assert len(files) == nodered._MAX_BACKUPS
    assert files[-1].stem == result["backup_id"]
    assert "flows-20260101T000000000000Z" not in {f.stem for f in files}


def test_list_backups_empty_and_unreadable(fake, tmp_path):
    assert list_backups() == {"backups": [], "count": 0}
    d = tmp_path / "nodered_backups"
    d.mkdir()
    (d / "flows-20260101T000000000000Z.json").write_text("{not json")

    assert list_backups()["backups"] == [
        {"backup_id": "flows-20260101T000000000000Z", "error": "backup_unreadable"}
    ]


def test_create_flow_through_fastmcp_call_tool(fake):
    """The MCP-facing schema (`list[dict]` nodes, `type` parameter name) round-trips."""
    import asyncio

    created = asyncio.run(
        nodered.mcp.call_tool("create_flow", {"label": "Via MCP", "nodes": copy.deepcopy(_NEW_NODES)})
    )
    found = asyncio.run(nodered.mcp.call_tool("search_nodes", {"type": "inject"}))

    assert created.structured_content["created"] is True
    assert found.structured_content["matches"][0]["id"] == "a1"


# --- annotations ---------------------------------------------------------------------------


def test_annotations_classify_reads_and_writes():
    import asyncio

    tools = {t.name: t for t in asyncio.run(nodered.mcp.list_tools())}
    reads = {n for n, t in tools.items() if t.annotations.readOnlyHint}
    assert reads == {
        "list_flows", "get_flow", "search_nodes", "find_entity_usage",
        "get_info", "get_diagnostics", "list_backups",
    }
    destructive = {n for n, t in tools.items() if t.annotations.destructiveHint}
    # security review 0.29.0 (L2): enabling a flow runs its code again, so set_flow_enabled is destructive too
    assert destructive == {"create_flow", "update_flow", "delete_flow", "restore_backup", "set_flow_enabled"}
    assert all(tools[n].annotations.openWorldHint for n in ("restore_backup", "set_flow_enabled"))


# --- security review 0.29.0: M1 redaction gaps ----------------------------------------------


def _flow_with(*extra_nodes: dict, tab_env: list | None = None) -> list[dict]:
    flows = _initial_flows()
    if tab_env is not None:
        flows[0]["env"] = tab_env
    flows.extend(extra_nodes)
    return flows


def test_redacts_authorization_header_and_keeps_header_keys(fake):
    fake.flows = _flow_with(
        {
            "id": "h1",
            "type": "http request",
            "z": "tab1",
            "headers": [
                {"keyType": "other", "keyValue": "Authorization", "valueType": "other", "valueValue": "Bearer abc.def.ghi"},
                {"keyType": "Accept", "keyValue": "", "valueType": "other", "valueValue": "application/json"},
            ],
            "x": 1,
            "y": 1,
            "wires": [[]],
        }
    )

    node = next(n for n in get_flow("tab1")["nodes"] if n["id"] == "h1")

    assert node["headers"][0]["valueValue"] == nodered._REDACTED
    assert node["headers"][0]["keyType"] == "other"
    assert node["headers"][0]["keyValue"] == "Authorization"
    assert node["headers"][1]["valueValue"] == "application/json"


def test_redacts_secret_env_values_on_tabs(fake):
    fake.flows = _flow_with(tab_env=[{"name": "API_TOKEN", "value": "tok-123", "type": "str"},
                                     {"name": "ROOM", "value": "kitchen", "type": "str"}])

    env = get_flow("tab1")["flow"]["env"]

    assert env[0]["value"] == nodered._REDACTED
    assert env[1]["value"] == "kitchen"


def test_redacts_bearer_values_and_secrets_inside_json_strings(fake):
    fake.flows = _flow_with(
        {"id": "c1", "type": "change", "z": "tab1",
         "rules": [{"t": "set", "p": "headers.x", "pt": "msg", "to": "Bearer xyz-123", "tot": "str"},
                   {"t": "set", "p": "topic", "pt": "msg", "to": "kitchen", "tot": "str"}],
         "x": 1, "y": 1, "wires": [[]]},
        {"id": "a1", "type": "api-call-service", "z": "tab1",
         "data": "{\"token\": \"t0k3n\", \"entity_id\": \"light.kitchen\"}", "x": 1, "y": 1, "wires": [[]]},
    )

    nodes = {n["id"]: n for n in get_flow("tab1")["nodes"]}

    assert nodes["c1"]["rules"][0]["to"] == nodered._REDACTED
    assert nodes["c1"]["rules"][1]["to"] == "kitchen"
    assert nodes["a1"]["data"] == nodered._REDACTED
    dumped = _dump(nodes)
    assert "xyz-123" not in dumped and "t0k3n" not in dumped


def test_redacted_header_round_trips_through_update(fake):
    fake.flows = _flow_with(
        {"id": "h1", "type": "http request", "z": "tab1",
         "headers": [{"keyType": "other", "keyValue": "Authorization", "valueType": "other",
                      "valueValue": "Bearer abc.def.ghi"}],
         "url": "https://example.test/hook", "x": 1, "y": 1, "wires": [[]]}
    )
    nodes = get_flow("tab1")["nodes"]

    assert update_flow("tab1", nodes)["updated"] is True
    h1 = next(n for n in fake.posted[-1]["flows"] if n["id"] == "h1")
    assert h1["headers"][0]["valueValue"] == "Bearer abc.def.ghi"


# --- M2: a restored secret must not be redirected ----------------------------------------------


def test_restored_secret_cannot_be_sent_to_another_url(fake):
    nodes = get_flow("tab1")["nodes"]
    webhook = next(n for n in nodes if n["id"] == "n3")
    webhook["url"] = "https://attacker.test/collect"

    result = update_flow("tab1", nodes)

    assert result["error"] == "secret_redirect_blocked"
    assert result["nodes"][0]["id"] == "n3"
    assert fake.posted == []


def test_restored_secret_cannot_change_node_type(fake):
    nodes = get_flow("tab1")["nodes"]
    webhook = next(n for n in nodes if n["id"] == "n3")
    webhook["type"] = "debug"

    assert update_flow("tab1", nodes)["error"] == "secret_redirect_blocked"
    assert fake.posted == []


def test_stored_credentials_block_destination_change(fake):
    fake.stored_credentials = {"n3": {"user": "", "has_password": True}}
    fake.flows[7].pop("apiKey")
    nodes = get_flow("tab1")["nodes"]
    next(n for n in nodes if n["id"] == "n3")["url"] = "https://attacker.test/collect"

    result = update_flow("tab1", nodes)

    assert result["error"] == "secret_redirect_blocked"
    assert any(r.startswith("GET /credentials/") for r in fake.nodered_requests())


def test_destination_change_allowed_with_explicit_credentials(fake):
    fake.stored_credentials = {"n3": {"has_password": True}}
    fake.flows[7].pop("apiKey")
    nodes = get_flow("tab1")["nodes"]
    webhook = next(n for n in nodes if n["id"] == "n3")
    webhook["url"] = "https://new.example.test/hook"
    webhook["credentials"] = {"user": "u", "password": "explicit-new-secret"}

    assert update_flow("tab1", nodes)["updated"] is True


def test_destination_change_without_any_secret_is_allowed(fake):
    fake.flows[7].pop("apiKey")
    nodes = get_flow("tab1")["nodes"]
    next(n for n in nodes if n["id"] == "n3")["url"] = "https://new.example.test/hook"

    assert update_flow("tab1", nodes)["updated"] is True


def test_credentials_lookup_failure_fails_closed(fake):
    fake.flows[7].pop("apiKey")
    fake.status["GET /credentials/http-request/n3"] = 500
    nodes = get_flow("tab1")["nodes"]
    next(n for n in nodes if n["id"] == "n3")["url"] = "https://new.example.test/hook"

    assert update_flow("tab1", nodes)["error"] == "secret_redirect_blocked"


# --- L1 / L3 / L4 --------------------------------------------------------------------------------


def test_backup_id_with_trailing_newline_is_rejected(fake):
    assert restore_backup("flows-20261111T120000000000Z\n")["error"] == "invalid_backup_id"


def test_restore_backup_checks_types_and_addon(fake, tmp_path):
    d = tmp_path / "nodered_backups"
    d.mkdir()
    bad_type = {"addon_slug": _SLUG, "rev": "x", "flows": [*_initial_flows(),
                {"id": "z9", "type": "not-installed", "z": "tab1", "x": 1, "y": 1, "wires": []}]}
    other = {"addon_slug": "other_nodered", "rev": "x", "flows": _initial_flows()}
    (d / "flows-20261111T120000000000Z.json").write_text(json.dumps(bad_type), encoding="utf-8")
    (d / "flows-20261111T120000000001Z.json").write_text(json.dumps(other), encoding="utf-8")

    assert restore_backup("flows-20261111T120000000000Z")["error"] == "unknown_node_types"
    assert restore_backup("flows-20261111T120000000001Z")["error"] == "backup_from_other_addon"
    assert fake.posted == []


def test_nodes_payload_size_limit(fake):
    big = [{"id": "big", "type": "debug", "x": 1, "y": 1, "wires": [], "info": "x" * (nodered._MAX_NODES_BYTES + 1)}]

    result = create_flow("Big", big)

    assert result["error"] == "invalid_nodes"
    assert fake.posted == []


def test_find_entity_usage_skips_dangerous_regex(fake):
    fake.flows = _flow_with(
        {"id": "r1", "type": "trigger-state", "z": "tab1",
         "entities": {"entity": [], "substring": [], "regex": ["^(a+)+$"]}, "x": 1, "y": 1, "wires": [[]]},
        {"id": "r2", "type": "trigger-state", "z": "tab1",
         "entities": {"entity": [], "substring": [], "regex": ["x" * 300]}, "x": 1, "y": 1, "wires": [[]]},
    )

    result = find_entity_usage("sensor." + "a" * 40)

    assert result["count"] == 0
    assert result["skipped_patterns"] == 2


def test_redacts_secret_header_value_without_a_scheme(fake):
    """`X-Api-Key: <raw token>` has no Bearer/Basic prefix; the header name alone decides."""
    fake.flows = _flow_with(
        {"id": "h2", "type": "http request", "z": "tab1",
         "headers": [{"keyType": "other", "keyValue": "X-Api-Key", "valueType": "other", "valueValue": "raw-token-42"}],
         "x": 1, "y": 1, "wires": [[]]}
    )

    node = next(n for n in get_flow("tab1")["nodes"] if n["id"] == "h2")

    assert node["headers"][0]["valueValue"] == nodered._REDACTED
    assert node["headers"][0]["keyValue"] == "X-Api-Key"


def test_credentials_are_looked_up_under_the_dashed_type(fake):
    """S1a: Node-RED registers `http request` credentials as `http-request`."""
    fake.stored_credentials = {"n3": {"has_password": True}}
    fake.flows[7].pop("apiKey")
    nodes = get_flow("tab1")["nodes"]
    next(n for n in nodes if n["id"] == "n3")["url"] = "https://attacker.test/collect"

    assert update_flow("tab1", nodes)["error"] == "secret_redirect_blocked"
    assert "GET /credentials/http-request/n3" in fake.nodered_requests()


def test_type_change_with_stored_credentials_is_blocked(fake):
    """S1b: no placeholder, no destination change, but stored credentials would move to another type."""
    fake.stored_credentials = {"n3": {"has_password": True}}
    fake.flows[7].pop("apiKey")
    nodes = get_flow("tab1")["nodes"]
    next(n for n in nodes if n["id"] == "n3")["type"] = "mqtt-broker"

    result = update_flow("tab1", nodes)

    assert result["error"] == "secret_redirect_blocked"
    assert fake.posted == []


def test_type_change_without_any_secret_is_allowed(fake):
    fake.flows[7].pop("apiKey")
    nodes = get_flow("tab1")["nodes"]
    next(n for n in nodes if n["id"] == "n3")["type"] = "debug"

    assert update_flow("tab1", nodes)["updated"] is True
