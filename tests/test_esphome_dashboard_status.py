"""Tests for ADR-0004 partition B (`tools/esphome.py`'s dashboard-facing tools).

Supersedes this file's earlier D4 section (nexus 0.22.0 coordinator decisions),
which tested a Supervisor-port-6052 diagnosis (`_dashboard_unreachable_diagnosis`,
`_dash`, `_sup_json`-only wiring) that never actually worked in add-on mode — see
ADR-0004's own "Kontekst" section. All dashboard I/O now goes through
`esphome_dashboard.DashboardClient` (ADR-0004 partition A, tested in
`tests/test_esphome_dashboard_client.py`); this file covers only what
`tools/esphome.py` itself is responsible for:

- the process-wide `DashboardLocator`/`DashboardClient` singleton and its test reset;
- the `_supervisor_get` adapter (`_sup_json`'s `{"error": ...}` convention -> raise);
- `_dashboard_error_response`'s mapping from every `DashboardError` subclass to this
  module's `{"error": ...}` tool-result shape, including the reshaped `diagnosis`
  (`{"mode", "dashboard_url", "slug", "addon_state", "ingress", "ingress_port",
  "advice"}`) attached only for `esphome_unreachable`/`esphome_auth_required`;
- `ping_dashboard`'s `reachable`/`url`/`mode`/`result` shape;
- `compile_device`'s `only_generate=True` rejection (D4) and timeout's
  `job_may_still_be_running`;
- `upload_device`'s `confirm` gate (D5);
- D-3: `secrets`/`secrets.yaml` blocked (case-insensitively) for every device-name
  parameter in this module, and excluded from `list_devices`.

Item-3 (device connected status via the entity registry, unrelated to ADR-0004)
tests are unchanged from the pre-ADR-0004 version of this file.
"""
from __future__ import annotations

import inspect

import pytest

import esphome_dashboard as dash
from tools import esphome as esphome_tools


def _unwrap(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _reset_dashboard_singleton():
    """Every test starts from (and leaves) a clean locator/client singleton."""
    esphome_tools._reset_dashboard_client()
    yield
    esphome_tools._reset_dashboard_client()


class _StubClient:
    """Drop-in replacement for `DashboardClient` — one canned result/exception per method."""

    def __init__(self, **outcomes):
        self._outcomes = outcomes
        self.calls: list[tuple[str, tuple, dict]] = []

    def _run(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        outcome = self._outcomes.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            raise AssertionError(f"_StubClient got an unexpected call to {name}()")
        return outcome

    def ping(self, *, timeout=5.0):
        return self._run("ping", timeout=timeout)

    def validate(self, configuration, *, timeout=60.0, tail_lines=200):
        return self._run("validate", configuration, timeout=timeout, tail_lines=tail_lines)

    def compile(self, configuration, *, timeout=180.0, tail_lines=200):
        return self._run("compile", configuration, timeout=timeout, tail_lines=tail_lines)

    def upload(self, configuration, port, *, timeout=240.0, tail_lines=200):
        return self._run("upload", configuration, port, timeout=timeout, tail_lines=tail_lines)


def _install_stub_client(monkeypatch, **outcomes) -> _StubClient:
    stub = _StubClient(**outcomes)
    monkeypatch.setattr(esphome_tools, "_get_dashboard_client", lambda: stub)
    return stub


def _forbid_dashboard_client(monkeypatch) -> None:
    """Fail loudly if a tool reaches `_get_dashboard_client()` at all."""
    def _boom():
        raise AssertionError("must not build/call the dashboard client")
    monkeypatch.setattr(esphome_tools, "_get_dashboard_client", _boom)
    monkeypatch.setattr(esphome_tools, "_get_dashboard_locator", _boom)


# === singleton wiring ================================================================

def test_get_dashboard_client_is_a_process_wide_singleton():
    first = esphome_tools._get_dashboard_client()
    second = esphome_tools._get_dashboard_client()
    assert first is second


def test_get_dashboard_locator_is_a_process_wide_singleton():
    first = esphome_tools._get_dashboard_locator()
    second = esphome_tools._get_dashboard_locator()
    assert first is second


def test_reset_dashboard_client_forces_a_fresh_singleton():
    first = esphome_tools._get_dashboard_client()
    esphome_tools._reset_dashboard_client()
    second = esphome_tools._get_dashboard_client()
    assert first is not second


def test_dashboard_client_singleton_is_built_from_dashboard_locator_singleton():
    client = esphome_tools._get_dashboard_client()
    locator = esphome_tools._get_dashboard_locator()
    assert client._locator is locator


# === _supervisor_get adapter (raises instead of returning {"error": ...}) ============

def test_supervisor_get_returns_data_on_success(monkeypatch):
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {"data": {"ok": True}},
    )
    assert esphome_tools._supervisor_get("/addons/x/info") == {"data": {"ok": True}}


def test_supervisor_get_raises_on_sup_json_error_dict(monkeypatch):
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {"error": "SUPERVISOR_TOKEN not set"},
    )
    with pytest.raises(Exception, match="SUPERVISOR_TOKEN not set"):
        esphome_tools._supervisor_get("/addons/x/info")


# === _diagnose_dashboard_unreachable shape (ADR-0004 D3) =============================

_DIAGNOSIS_KEYS = {"mode", "dashboard_url", "slug", "addon_state", "ingress", "ingress_port", "advice"}


def test_diagnosis_shape_is_exactly_the_adr0004_keys_in_explicit_mode(monkeypatch):
    monkeypatch.setenv("ESPHOME_DASHBOARD_URL", "http://example.local:6052")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert set(diag) == _DIAGNOSIS_KEYS
    assert diag["mode"] == "explicit"
    assert diag["dashboard_url"] == "http://example.local:6052"
    assert diag["slug"] is None and diag["addon_state"] is None
    assert diag["ingress"] is None and diag["ingress_port"] is None


def test_diagnosis_shape_is_unknown_with_nothing_configured(monkeypatch):
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert set(diag) == _DIAGNOSIS_KEYS
    assert diag["mode"] == "unknown"
    assert diag["dashboard_url"] is None


def test_diagnosis_supervisor_ingress_addon_not_found(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(esphome_tools, "_discover_esphome_slug", lambda: None)

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert set(diag) == _DIAGNOSIS_KEYS
    assert diag["mode"] == "supervisor_ingress"
    assert diag["slug"] is None
    assert "not found" in diag["advice"].lower()


def test_diagnosis_supervisor_ingress_addon_stopped(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(esphome_tools, "_discover_esphome_slug", lambda: "5c53de3b_esphome")
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {
            "data": {"state": "stopped", "ingress": True, "ingress_port": 65490}
        },
    )

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert diag["slug"] == "5c53de3b_esphome"
    assert diag["addon_state"] == "stopped"
    assert "stopped" in diag["advice"]


def test_diagnosis_supervisor_ingress_addon_without_ingress(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(esphome_tools, "_discover_esphome_slug", lambda: "5c53de3b_esphome")
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {
            "data": {"state": "started", "ingress": False, "ingress_port": None}
        },
    )

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert diag["ingress"] is False
    assert diag["dashboard_url"] is None
    assert "ingress" in diag["advice"].lower()


def test_diagnosis_supervisor_ingress_healthy_addon_still_unreachable(monkeypatch):
    """Add-on looks fine on paper (running, ingress enabled, valid port) but the
    connection still failed -- e.g. nexus itself lacks host_network (ADR-0004 D1)."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(esphome_tools, "_discover_esphome_slug", lambda: "5c53de3b_esphome")
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {
            "data": {"state": "started", "ingress": True, "ingress_port": 65490}
        },
    )

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert diag["dashboard_url"] == "http://127.0.0.1:65490"
    assert diag["addon_state"] == "started"
    assert diag["ingress"] is True
    assert diag["ingress_port"] == 65490


def test_diagnosis_supervisor_unreachable_itself(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(esphome_tools, "_discover_esphome_slug", lambda: "5c53de3b_esphome")
    monkeypatch.setattr(
        esphome_tools, "_sup_json",
        lambda method, path, body=None, timeout=30: {"error": "SUPERVISOR_TOKEN not set"},
    )

    diag = esphome_tools._diagnose_dashboard_unreachable()

    assert diag["slug"] == "5c53de3b_esphome"
    assert "SUPERVISOR_TOKEN not set" in diag["advice"]


def test_diagnosis_has_no_stale_port_6052_fields(monkeypatch):
    monkeypatch.setenv("ESPHOME_DASHBOARD_URL", "http://example.local:9999")
    diag = esphome_tools._diagnose_dashboard_unreachable()
    assert "port_6052_mapped" not in diag
    assert "ingress_only" not in diag
    assert "6052" not in diag["advice"]
    assert "authentication enabled" not in diag["advice"]
    assert "leave_front_door_open" not in diag["advice"]


# === _dashboard_error_response mapping ===============================================

def test_dashboard_error_response_not_configured_has_no_diagnosis():
    e = dash.NotConfiguredError("nope")
    result = esphome_tools._dashboard_error_response(e, device="d", action="compile")
    assert result["error"] == "esphome_not_configured"
    assert "diagnosis" not in result
    assert result["device"] == "d" and result["action"] == "compile"


def test_dashboard_error_response_unreachable_has_diagnosis(monkeypatch):
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    e = dash.UnreachableError("refused")
    result = esphome_tools._dashboard_error_response(e)
    assert result["error"] == "esphome_unreachable"
    assert set(result["diagnosis"]) == _DIAGNOSIS_KEYS


def test_dashboard_error_response_auth_required_has_diagnosis(monkeypatch):
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    e = dash.AuthRequiredError("needs a login")
    result = esphome_tools._dashboard_error_response(e)
    assert result["error"] == "esphome_auth_required"
    assert "diagnosis" in result


def test_dashboard_error_response_command_failed_carries_dashboard_code():
    e = dash.CommandFailedError("boom", dashboard_error_code="not_found", details="no such file")
    result = esphome_tools._dashboard_error_response(e, device="d", action="validate")
    assert result["error"] == "esphome_command_failed"
    assert result["dashboard_error_code"] == "not_found"
    assert result["details"] == "no such file"


def test_dashboard_error_response_timeout_job_running_gets_do_not_retry_message():
    e = dash.DashboardTimeoutError(
        "timed out", timeout=180.0, log_tail=["a", "b"], log_lines=2, job_may_still_be_running=True,
    )
    result = esphome_tools._dashboard_error_response(e, device="d", action="compile")
    assert result["error"] == "timeout"
    assert result["timeout"] == 180.0
    assert result["log_tail"] == ["a", "b"]
    assert result["log_lines"] == 2
    assert result["job_may_still_be_running"] is True
    assert "do not retry" in result["message"].lower()
    assert "esphome_get_addon_logs" in result["message"]


def test_dashboard_error_response_timeout_no_job_running_has_plain_message():
    e = dash.DashboardTimeoutError(
        "timed out", timeout=60.0, log_tail=[], log_lines=0, job_may_still_be_running=False,
    )
    result = esphome_tools._dashboard_error_response(e, device="d", action="validate")
    assert result["job_may_still_be_running"] is False
    assert "do not retry" not in result["message"].lower()


# === ping_dashboard ===================================================================

def test_ping_dashboard_success_reports_url_and_mode(monkeypatch):
    monkeypatch.setenv("ESPHOME_DASHBOARD_URL", "http://example.local:6052")
    _install_stub_client(monkeypatch, ping={"ok": True})

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result == {
        "reachable": True, "url": "http://example.local:6052", "mode": "explicit", "result": {"ok": True},
    }


def test_ping_dashboard_not_configured(monkeypatch):
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result["reachable"] is False
    assert result["error"] == "esphome_not_configured"
    assert "diagnosis" not in result
    assert "url" not in result and "mode" not in result


def test_ping_dashboard_unreachable_has_diagnosis(monkeypatch):
    monkeypatch.setenv("ESPHOME_DASHBOARD_URL", "http://example.local:6052")
    _install_stub_client(monkeypatch, ping=dash.UnreachableError("refused"))

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result["reachable"] is False
    assert result["error"] == "esphome_unreachable"
    assert result["url"] == "http://example.local:6052"
    assert result["mode"] == "explicit"
    assert set(result["diagnosis"]) == _DIAGNOSIS_KEYS


def test_ping_dashboard_auth_required_has_diagnosis(monkeypatch):
    monkeypatch.setenv("ESPHOME_DASHBOARD_URL", "http://example.local:6052")
    _install_stub_client(monkeypatch, ping=dash.AuthRequiredError("nope"))

    result = _unwrap(esphome_tools.ping_dashboard)()

    assert result["error"] == "esphome_auth_required"
    assert "diagnosis" in result


# === compile_device ====================================================================

def test_compile_device_only_generate_true_is_rejected_with_no_io(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.compile_device)("livingroom", only_generate=True)

    assert result == {
        "device": "livingroom",
        "action": "compile",
        "error": "esphome_unsupported_option",
        "message": result["message"],
    }
    assert "only_generate" in result["message"]


def test_compile_device_success_passthrough(monkeypatch):
    stub = _install_stub_client(monkeypatch, compile={"success": True, "exit_code": 0, "log_tail": [], "log_lines": 0})

    result = _unwrap(esphome_tools.compile_device)("livingroom")

    assert result["device"] == "livingroom"
    assert result["action"] == "compile"
    assert result["success"] is True
    assert stub.calls == [("compile", ("livingroom.yaml",), {"timeout": 180, "tail_lines": 200})]


def test_compile_device_command_failed_maps_dashboard_error_code(monkeypatch):
    _install_stub_client(
        monkeypatch, compile=dash.CommandFailedError("boom", dashboard_error_code="build_failed", details="oops"),
    )

    result = _unwrap(esphome_tools.compile_device)("livingroom")

    assert result["error"] == "esphome_command_failed"
    assert result["dashboard_error_code"] == "build_failed"
    assert result["details"] == "oops"


def test_compile_device_timeout_marks_job_may_still_be_running(monkeypatch):
    _install_stub_client(
        monkeypatch,
        compile=dash.DashboardTimeoutError(
            "timed out", timeout=180.0, log_tail=["building"], log_lines=1, job_may_still_be_running=True,
        ),
    )

    result = _unwrap(esphome_tools.compile_device)("livingroom")

    assert result["error"] == "timeout"
    assert result["job_may_still_be_running"] is True
    assert result["log_tail"] == ["building"]


def test_compile_device_path_traversal_rejected_before_any_dashboard_call(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.compile_device)("../../etc/passwd")

    assert "error" in result


# === validate_config ===================================================================

def test_validate_config_success_passthrough(monkeypatch):
    stub = _install_stub_client(monkeypatch, validate={"success": True, "result": {}, "log_tail": [], "log_lines": 0})

    result = _unwrap(esphome_tools.validate_config)("livingroom")

    assert result["success"] is True
    assert stub.calls == [("validate", ("livingroom.yaml",), {"timeout": 60, "tail_lines": 200})]


def test_validate_config_auth_required_has_diagnosis(monkeypatch):
    _install_stub_client(monkeypatch, validate=dash.AuthRequiredError("nope"))

    result = _unwrap(esphome_tools.validate_config)("livingroom")

    assert result["error"] == "esphome_auth_required"
    assert "diagnosis" in result


def test_validate_config_timeout_job_not_running(monkeypatch):
    _install_stub_client(
        monkeypatch,
        validate=dash.DashboardTimeoutError(
            "timed out", timeout=60.0, log_tail=[], log_lines=0, job_may_still_be_running=False,
        ),
    )

    result = _unwrap(esphome_tools.validate_config)("livingroom")

    assert result["error"] == "timeout"
    assert result["job_may_still_be_running"] is False


# === upload_device: confirm gate (ADR-0004 D5) =========================================

def test_upload_device_without_confirm_returns_common_shape_with_zero_io(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.upload_device)("livingroom")

    assert result == {
        "error": "confirmation_required",
        "message": result.get("message"),
        "action": result.get("action"),
    }
    assert isinstance(result["message"], str) and result["message"]
    assert "confirm=True" in result["action"]


def test_upload_device_confirm_false_explicit_still_zero_io(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.upload_device)("livingroom", confirm=False)

    assert result["error"] == "confirmation_required"


def test_upload_device_confirm_true_success(monkeypatch):
    stub = _install_stub_client(monkeypatch, upload={"success": True, "exit_code": 0, "log_tail": [], "log_lines": 0})

    result = _unwrap(esphome_tools.upload_device)("livingroom", confirm=True)

    assert result["success"] is True
    assert stub.calls == [("upload", ("livingroom.yaml", "OTA"), {"timeout": 240, "tail_lines": 200})]


def test_upload_device_confirm_true_unreachable_has_diagnosis(monkeypatch):
    _install_stub_client(monkeypatch, upload=dash.UnreachableError("refused"))

    result = _unwrap(esphome_tools.upload_device)("livingroom", confirm=True)

    assert result["error"] == "esphome_unreachable"
    assert "diagnosis" in result


def test_upload_device_confirm_true_timeout_marks_job_may_still_be_running(monkeypatch):
    _install_stub_client(
        monkeypatch,
        upload=dash.DashboardTimeoutError(
            "timed out", timeout=240.0, log_tail=[], log_lines=0, job_may_still_be_running=True,
        ),
    )

    result = _unwrap(esphome_tools.upload_device)("livingroom", confirm=True)

    assert result["job_may_still_be_running"] is True


def test_upload_device_schema_confirm_is_optional_boolean_default_false():
    import asyncio

    tools = {t.name: t for t in asyncio.run(esphome_tools.mcp.list_tools())}
    schema = tools["upload_device"].parameters or {}
    props = schema.get("properties", {})
    assert props["confirm"]["type"] == "boolean"
    assert props["confirm"]["default"] is False
    assert "confirm" not in (schema.get("required") or [])


# === D-3: secrets/secrets.yaml blocked for every device-name parameter ===============

@pytest.mark.parametrize("blocked_name", ["secrets", "Secrets", "SECRETS", "secrets.yaml", "Secrets.YAML"])
def test_safe_filename_rejects_secrets_case_insensitively(blocked_name):
    with pytest.raises(ValueError):
        esphome_tools._safe_filename(blocked_name)


def test_get_config_rejects_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    (tmp_path / "secrets.yaml").write_text("api_password: hunter2\n", encoding="utf-8")

    result = _unwrap(esphome_tools.get_config)("secrets")

    assert "error" in result
    assert "content" not in result


def test_write_config_rejects_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)

    result = _unwrap(esphome_tools.write_config)("secrets", "api_password: hunter2\n")

    assert result["success"] is False
    assert not (tmp_path / "secrets.yaml").exists()


def test_compile_device_rejects_secrets(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.compile_device)("secrets")

    assert "error" in result


def test_validate_config_rejects_secrets(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.validate_config)("SECRETS.yaml")

    assert "error" in result


def test_upload_device_rejects_secrets_after_confirm(monkeypatch):
    _forbid_dashboard_client(monkeypatch)

    result = _unwrap(esphome_tools.upload_device)("secrets", confirm=True)

    assert "error" in result


def test_lvgl_add_widget_rejects_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)

    result = _unwrap(esphome_tools.lvgl_add_widget)("secrets", "home", "label", {"id": "lbl1"})

    assert "error" in result


def test_list_devices_excludes_secrets_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    (tmp_path / "secrets.yaml").write_text("api_password: hunter2\n", encoding="utf-8")
    (tmp_path / "Secrets.yaml").write_text("api_password: hunter2\n", encoding="utf-8")
    (tmp_path / "kuchnia.yaml").write_text("esphome: {}\n", encoding="utf-8")
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)

    result = _unwrap(esphome_tools.list_devices)()

    assert result["yaml_configs"] == ["kuchnia"]


# === static source checks: no stale port-6052/auth advice left (ADR-0004 D6) =========

def test_source_has_no_stale_port_6052_or_leave_front_door_open_mentions():
    import inspect

    source = inspect.getsource(esphome_tools)
    for banned in ("6052", "authentication enabled", "leave_front_door_open", "ESPHOME_USERNAME", "ESPHOME_PASSWORD"):
        assert banned not in source, f"stale reference to {banned!r} still present in tools/esphome.py"


# === Item 3: device connected status from entity registry + states (unchanged) ======

def _entity(entity_id, device_id, platform="esphome"):
    return {"entity_id": entity_id, "device_id": device_id, "platform": platform}


def test_list_devices_connected_true_when_an_entity_is_available(monkeypatch):
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [_entity("sensor.kitchen_sensor_temperature", "d1")],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [{"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"}],
    )

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is True
    assert result["online"] == 1
    assert result["offline"] == 0


def test_list_devices_connected_false_when_all_entities_unavailable(monkeypatch):
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [
            _entity("sensor.kitchen_sensor_temperature", "d1"),
            _entity("sensor.kitchen_sensor_humidity", "d1"),
        ],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [
            {"entity_id": "sensor.kitchen_sensor_temperature", "state": "unavailable"},
            {"entity_id": "sensor.kitchen_sensor_humidity", "state": "unavailable"},
        ],
    )

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is False
    assert result["online"] == 0
    assert result["offline"] == 1


def test_list_devices_connected_none_when_device_has_no_known_entities(monkeypatch):
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(esphome_tools.ha, "get_entity_registry", list)
    monkeypatch.setattr(esphome_tools.ha, "get_states", list)

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is None
    assert result["online"] == 0
    assert result["offline"] == 0


def test_list_devices_does_not_rely_on_api_connection_status_binary_sensor(monkeypatch):
    """RED against the pre-fix code: a `binary_sensor.*_api_connection_status`
    entity that isn't linked to the device via `device_id` (e.g. it doesn't
    exist at all, matching the live fact) must not affect the result, and a
    real entity that IS linked must still correctly report connected."""
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [{"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"}],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [_entity("sensor.kitchen_sensor_temperature", "d1")],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [
            {"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"},
            {"entity_id": "binary_sensor.kitchen_sensor_api_connection_status", "state": "off"},
        ],
    )

    result = _unwrap(esphome_tools.list_devices)()

    assert result["ha_devices"][0]["connected"] is True


def test_get_device_entities_matches_by_device_id_not_substring(monkeypatch):
    """RED against the pre-fix code: slug substring matching would also match
    'sensor' from an unrelated device whose slug happens to contain it."""
    monkeypatch.setattr(
        esphome_tools.ha, "get_device_registry",
        lambda: [
            {"id": "d1", "name": "Kitchen Sensor", "manufacturer": "espressif"},
            {"id": "d2", "name": "Kitchen Sensor 2", "manufacturer": "espressif"},
        ],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [
            _entity("sensor.kitchen_sensor_temperature", "d1"),
            _entity("sensor.kitchen_sensor_2_temperature", "d2"),
        ],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [
            {"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"},
            {"entity_id": "sensor.kitchen_sensor_2_temperature", "state": "22.0"},
        ],
    )

    result = _unwrap(esphome_tools.get_device_entities)("Kitchen Sensor")

    assert [e["entity_id"] for e in result] == ["sensor.kitchen_sensor_temperature"]


def test_get_device_entities_falls_back_to_slug_match_without_device_registry(monkeypatch):
    monkeypatch.setattr(esphome_tools.ha, "get_device_registry", list)
    monkeypatch.setattr(
        esphome_tools.ha, "get_entity_registry",
        lambda: [_entity("sensor.kitchen_sensor_temperature", None)],
    )
    monkeypatch.setattr(
        esphome_tools.ha, "get_states",
        lambda: [{"entity_id": "sensor.kitchen_sensor_temperature", "state": "21.5"}],
    )

    result = _unwrap(esphome_tools.get_device_entities)("kitchen_sensor")

    assert len(result) == 1
    assert result[0]["entity_id"] == "sensor.kitchen_sensor_temperature"


# === write_config: remote external_components/packages detection (task: ADR-0004
# follow-up, Security review pre-0.23.0) ==============================================
#
# `esphome_write_config` only ever validated YAML syntax. A config whose
# `external_components:`/`packages:` names a remote source is fetched AND
# EXECUTED by Device Builder at `esphome_compile_device` time (ADR-0004 gave
# nexus a working `compile`). Detection is static (parsed YAML structure
# only) — no network I/O — and never blocks the write; it only adds
# `warning`/`external_sources` to the success response.

def _write_config_ok(tmp_path, monkeypatch, content: str) -> dict:
    monkeypatch.setattr(esphome_tools, "_ESPHOME_DIR", tmp_path)
    result = _unwrap(esphome_tools.write_config)("device", content)
    assert result["success"] is True, result
    return result


def test_write_config_external_components_github_shorthand_warns(tmp_path, monkeypatch):
    content = (
        "external_components:\n"
        "  - source: github://esphome/esphome@dev\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" in result
    assert result["external_sources"] == ["github://esphome/esphome@dev"]


def test_write_config_external_components_git_dict_source_warns(tmp_path, monkeypatch):
    content = (
        "external_components:\n"
        "  - source:\n"
        "      type: git\n"
        "      url: https://github.com/example/malicious\n"
        "      ref: main\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" in result
    assert result["external_sources"] == ["https://github.com/example/malicious"]


def test_write_config_external_components_local_type_no_warning(tmp_path, monkeypatch):
    content = (
        "external_components:\n"
        "  - source:\n"
        "      type: local\n"
        "      path: my_components\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" not in result
    assert "external_sources" not in result


def test_write_config_external_components_local_path_shorthand_no_warning(tmp_path, monkeypatch):
    content = (
        "external_components:\n"
        "  - source: my_components\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" not in result
    assert "external_sources" not in result


def test_write_config_packages_github_shorthand_warns(tmp_path, monkeypatch):
    content = (
        "packages:\n"
        "  remote_package: github://example/repo/file.yml@main\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" in result
    assert result["external_sources"] == ["github://example/repo/file.yml@main"]


def test_write_config_packages_url_dict_warns(tmp_path, monkeypatch):
    content = (
        "packages:\n"
        "  remote_package_files:\n"
        "    url: https://github.com/esphome/repository\n"
        "    files: [file1.yml]\n"
        "    ref: main\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" in result
    assert result["external_sources"] == ["https://github.com/esphome/repository"]


def test_write_config_packages_local_include_no_warning(tmp_path, monkeypatch):
    content = (
        "packages:\n"
        "  wifi_config: !include common/wifi.yaml\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" not in result
    assert "external_sources" not in result


def test_write_config_packages_list_form_remote_warns(monkeypatch, tmp_path):
    content = (
        "packages:\n"
        "  - github://example/repo/file.yml@main\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert "warning" in result
    assert result["external_sources"] == ["github://example/repo/file.yml@main"]


def test_write_config_no_external_sections_no_warning_keys(tmp_path, monkeypatch):
    content = "esphome:\n  name: livingroom\n"
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert set(result) == {"success", "name", "path", "bytes"}


def test_write_config_secret_and_include_tags_still_work_with_detection(tmp_path, monkeypatch):
    """!secret/!include pass through unchanged (pre-existing behaviour) even
    when external_components/packages detection runs on the parsed doc."""
    content = (
        "wifi:\n"
        "  ssid: !secret wifi_ssid\n"
        "  password: !secret wifi_password\n"
        "external_components:\n"
        "  - source: github://esphome/esphome@dev\n"
    )
    result = _write_config_ok(tmp_path, monkeypatch, content)

    assert result["external_sources"] == ["github://esphome/esphome@dev"]


def test_write_config_docstring_mentions_external_sources_in_returns():
    doc = inspect.getdoc(_unwrap(esphome_tools.write_config)) or ""
    assert "external_sources" in doc
    assert "Returns:" in doc


