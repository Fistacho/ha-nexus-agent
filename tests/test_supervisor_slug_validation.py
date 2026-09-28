"""ADR-0006 follow-up (Security review, 2026-09-28, "same class as F3"):
every `tools/supervisor.py` tool that takes an add-on or backup `slug`
splices it, unvalidated until now, into an f-string Supervisor REST path
(`_supervisor_request`/`_supervisor_get_text`) -- the identical structural
bug ADR-0006 F3 closed for `services_call_service`. `_validate_slug`
(`service_guard.path_segment(slug, "identifier")`) closes it here: a
malicious slug is refused with `{"error": "invalid_slug", ...}` and zero
Supervisor requests, checked *before* self_protection/confirm so it can't
be bypassed by either.
"""
from __future__ import annotations

import pytest

from tools import supervisor as supervisor_tools


def _unwrap(tool):
    return getattr(tool, "fn", tool)


get_addon = _unwrap(supervisor_tools.get_addon)
install_addon = _unwrap(supervisor_tools.install_addon)
uninstall_addon = _unwrap(supervisor_tools.uninstall_addon)
start_addon = _unwrap(supervisor_tools.start_addon)
stop_addon = _unwrap(supervisor_tools.stop_addon)
restart_addon = _unwrap(supervisor_tools.restart_addon)
update_addon = _unwrap(supervisor_tools.update_addon)
get_addon_logs = _unwrap(supervisor_tools.get_addon_logs)
set_addon_options = _unwrap(supervisor_tools.set_addon_options)
get_addon_stats = _unwrap(supervisor_tools.get_addon_stats)
restore_backup = _unwrap(supervisor_tools.restore_backup)
delete_backup = _unwrap(supervisor_tools.delete_backup)

_MALICIOUS_SLUGS = [
    "../host/shutdown",
    "../../hassio/addon_stop",
    "core_mosquitto?x=1",
    "core_mosquitto#frag",
    "..",
    "a/b",
    "%2e%2e",
    "%2e%2e%2fhost%2fshutdown",
]


def _boom(*_args, **_kwargs):
    raise AssertionError("no Supervisor request should happen for a malicious slug")


@pytest.fixture(autouse=True)
def _forbid_supervisor_io(monkeypatch):
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", _boom)
    monkeypatch.setattr(supervisor_tools, "_supervisor_get_text", _boom)


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_get_addon_rejects_malicious_slug_without_io(slug):
    result = get_addon(slug)
    assert result["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_install_addon_rejects_malicious_slug_without_io(slug):
    assert install_addon(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_uninstall_addon_rejects_malicious_slug_without_io_even_with_confirm(slug):
    """Slug validation runs before self_protection/confirm -- a malicious
    slug is refused regardless of `confirm`."""
    assert uninstall_addon(slug, confirm=True)["error"] == "invalid_slug"
    assert uninstall_addon(slug, confirm=False)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_start_addon_rejects_malicious_slug_without_io(slug):
    assert start_addon(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_stop_addon_rejects_malicious_slug_without_io(slug):
    assert stop_addon(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_restart_addon_rejects_malicious_slug_without_io(slug):
    assert restart_addon(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_update_addon_rejects_malicious_slug_without_io(slug):
    assert update_addon(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_get_addon_logs_rejects_malicious_slug_without_io(slug):
    assert get_addon_logs(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_set_addon_options_rejects_malicious_slug_without_io(slug):
    assert set_addon_options(slug, {"foo": "bar"})["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_get_addon_stats_rejects_malicious_slug_without_io(slug):
    assert get_addon_stats(slug)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_restore_backup_rejects_malicious_slug_without_io_even_with_confirm(slug):
    assert restore_backup(slug, confirm=True)["error"] == "invalid_slug"
    assert restore_backup(slug, confirm=False)["error"] == "invalid_slug"


@pytest.mark.parametrize("slug", _MALICIOUS_SLUGS)
def test_delete_backup_rejects_malicious_slug_without_io_even_with_confirm(slug):
    assert delete_backup(slug, confirm=True)["error"] == "invalid_slug"
    assert delete_backup(slug, confirm=False)["error"] == "invalid_slug"


# ---------------------------------------------------------------------------
# Regression: legitimate slugs are unaffected
# ---------------------------------------------------------------------------


def test_legitimate_slug_still_reaches_supervisor_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda method, path, json=None: calls.append((method, path)) or {"data": {}}
    )
    result = get_addon("core_mosquitto")
    assert "error" not in result
    assert calls == [("GET", "/addons/core_mosquitto/info")]


def test_legitimate_backup_slug_still_reaches_supervisor_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda method, path, json=None: calls.append((method, path)) or {"ok": True}
    )
    result = delete_backup("abcd1234", confirm=True)
    assert result == {"ok": True}
    assert calls == [("DELETE", "/backups/abcd1234")]
