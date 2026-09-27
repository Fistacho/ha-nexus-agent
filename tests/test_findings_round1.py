"""TDD coverage for findings-0.21.md items #1-#3 (round 1: themes, supervisor,
helpers) plus the dead-code removal in ha_client.py (item #4).

Each item below is written RED-first: the test documents/exercises the
behaviour described in `.claude/docs/nexus/findings-0.21.md` and only turns
GREEN once the corresponding fix lands in `tools/themes.py`,
`tools/supervisor.py`, or `tools/helpers.py`.
"""
from __future__ import annotations

import importlib

import pytest

import ha_client as ha
from tools import helpers, supervisor, themes

# ---------------------------------------------------------------------------
# #1 — themes: _theme_path() ValueError must not escape create/update/delete
# ---------------------------------------------------------------------------

_BAD_NAMES = ["../evil", "a/b", "a\\b", ".hidden", "", "   "]


@pytest.mark.parametrize("bad_name", _BAD_NAMES)
def test_create_theme_rejects_invalid_name_without_raising(bad_name, ha_config_dir):
    result = themes.create_theme(bad_name, {"primary-color": "#000"})
    assert result == {
        "success": False,
        "error": "invalid_theme_name",
        "detail": f"Invalid theme name: {bad_name!r}",
    }


@pytest.mark.parametrize("bad_name", _BAD_NAMES)
def test_update_theme_rejects_invalid_name_without_raising(bad_name, ha_config_dir):
    result = themes.update_theme(bad_name, {"primary-color": "#000"})
    assert result == {
        "success": False,
        "error": "invalid_theme_name",
        "detail": f"Invalid theme name: {bad_name!r}",
    }


@pytest.mark.parametrize("bad_name", _BAD_NAMES)
def test_delete_theme_rejects_invalid_name_without_raising(bad_name, ha_config_dir):
    result = themes.delete_theme(bad_name)
    assert result == {
        "success": False,
        "error": "invalid_theme_name",
        "detail": f"Invalid theme name: {bad_name!r}",
    }


def test_create_theme_still_works_for_a_valid_name(tmp_path, monkeypatch):
    # themes.py resolves _THEMES_DIR at import time from HA_CONFIG_PATH, so
    # setting the env var alone (as `ha_config_dir` does) is too late here —
    # patch the module-level path directly instead.
    monkeypatch.setattr(themes, "_CONFIG_PATH", tmp_path)
    monkeypatch.setattr(themes, "_THEMES_DIR", tmp_path / "themes")
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: {"status": "ok"})
    result = themes.create_theme("valid_name", {"primary-color": "#fff"})
    assert result["success"] is True
    assert result["path"].endswith("valid_name.yaml")


# ---------------------------------------------------------------------------
# #2 — supervisor: confirm=False shape must match system.py's
#      {"error": "confirmation_required", "message": ..., "action": ...}
# ---------------------------------------------------------------------------

_CONFIRM_GUARDED = [
    ("uninstall_addon", {"slug": "some_addon"}),
    ("restart_core", {}),
    ("restart_host", {}),
    ("restore_backup", {"slug": "some_backup"}),
]


@pytest.mark.parametrize("fn_name,kwargs", _CONFIRM_GUARDED, ids=[c[0] for c in _CONFIRM_GUARDED])
def test_supervisor_confirm_guard_matches_system_shape(fn_name, kwargs):
    fn = getattr(supervisor, fn_name)
    result = fn(**kwargs, confirm=False)
    assert result["error"] == "confirmation_required"
    assert "message" in result and isinstance(result["message"], str) and result["message"]
    assert "action" in result and isinstance(result["action"], str) and "confirm=True" in result["action"]
    # the old shape must be gone
    assert result != {"error": "set confirm=True to proceed"}


@pytest.mark.parametrize("fn_name,kwargs", _CONFIRM_GUARDED, ids=[c[0] for c in _CONFIRM_GUARDED])
def test_supervisor_confirm_guard_performs_no_request(fn_name, kwargs, monkeypatch):
    """Without confirm=True, no Supervisor HTTP request is made."""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call Supervisor API when confirm is false")

    monkeypatch.setattr(supervisor, "_supervisor_request", _boom)
    fn = getattr(supervisor, fn_name)
    fn(**kwargs, confirm=False)
    assert called["n"] == 0


def test_supervisor_restart_core_proceeds_when_confirmed(monkeypatch):
    monkeypatch.setattr(supervisor, "_supervisor_request", lambda *a, **k: {"result": "ok"})
    result = supervisor.restart_core(confirm=True)
    assert result == {"result": "ok"}


# ---------------------------------------------------------------------------
# #3 — helpers_reload_helpers: per-domain failures must be surfaced, not
#      swallowed by a bare `except Exception: pass`.
# ---------------------------------------------------------------------------


def test_reload_helpers_reports_which_domain_failed(monkeypatch):
    def fake_call_service(domain, service, data=None):
        if domain == "input_text":
            raise RuntimeError("boom: input_text reload failed")
        return [{"entity_id": f"{domain}.dummy", "state": "on"}]

    monkeypatch.setattr(helpers.ha, "call_service", fake_call_service)
    result = helpers.reload_helpers()

    assert isinstance(result, dict)
    assert "failed" in result, "caller must be able to see which domain failed"
    assert result["failed"].get("input_text") == "boom: input_text reload failed"
    # domains that succeeded are still reported
    reloaded_ids = {s["entity_id"] for s in result["reloaded"]}
    assert "input_boolean.dummy" in reloaded_ids
    assert "input_text" not in {eid.split(".")[0] for eid in reloaded_ids}


def test_reload_helpers_all_success_has_empty_failed(monkeypatch):
    monkeypatch.setattr(
        helpers.ha, "call_service", lambda domain, service, data=None: [{"entity_id": f"{domain}.x", "state": "on"}]
    )
    result = helpers.reload_helpers()
    assert result["failed"] == {}
    assert len(result["reloaded"]) == 7  # one per reloaded domain


# ---------------------------------------------------------------------------
# #4 — ha_client.read_config_file is dead code (NotImplementedError, unused
#      anywhere in the repo including tests) and must be removed.
# ---------------------------------------------------------------------------


def test_ha_client_has_no_dead_read_config_file():
    importlib.reload(ha)
    assert not hasattr(ha, "read_config_file"), (
        "ha_client.read_config_file is dead code (raises NotImplementedError, unused) — remove it"
    )
