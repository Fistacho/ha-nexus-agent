"""Regression tests for `tools/hacs.py` — WebSocket command names/fields.

`hacs_update_hacs_repository` and `hacs_install_hacs_repository` were
confirmed live (2026-09-28, HACS on HA 2026.9.3) to answer
`{'code': 'unknown_command'}` — they send WebSocket command names that do
not exist in HACS's own websocket API.

Verified against hacs/integration (`custom_components/hacs/websocket/`):

* `hacs/repository/info`         — field is `repository_id`, not `repository`
  (repository.py, `hacs_repository_info`)
* `hacs/repository/download`     — `repository` (required) + `version`
  (optional); installs when not yet installed, updates in place when
  already installed (repository.py, `hacs_repository_download`)
* `hacs/repository/remove`       — `repository` (required); uninstalls an
  already-installed repository (repository.py, `hacs_repository_remove`)
* `hacs/repository/refresh`      — `repository` (required); re-fetches repo
  data and always returns `{}` — no version info (repository.py,
  `hacs_repository_refresh`)
* `hacs/repositories/add`        — plural; `repository` (URL) + `category`,
  registers a *custom* repository (repositories.py, `hacs_repositories_add`)
* `hacs/repositories/list`       — plural; unchanged, already correct
  (repositories.py, `hacs_repositories_list`)

There is no `hacs/repository/install`, `hacs/repository/uninstall`,
`hacs/repository/update` or singular `hacs/repository/add` command in HACS
at all — every call to one of those previously returned `unknown_command`.
"""
from __future__ import annotations

import pytest

import ha_client as ha
from tools import hacs as hacs_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


# --- get_hacs_repository: repository_id, not repository ----------------------


def test_get_hacs_repository_uses_repository_id_field(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {"repository_id": "123"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.get_hacs_repository)("123")

    assert calls == [("hacs/repository/info", {"repository_id": "123"})]


# --- install_hacs_repository: download, not install ---------------------------


def test_install_hacs_repository_uses_download_command_with_version(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.install_hacs_repository)("123", version="1.2.3")

    assert calls == [("hacs/repository/download", {"repository": "123", "version": "1.2.3"})]


def test_install_hacs_repository_without_version_omits_version_field(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.install_hacs_repository)("123")

    assert calls == [("hacs/repository/download", {"repository": "123"})]


# --- uninstall_hacs_repository: remove, not uninstall --------------------------


def test_uninstall_hacs_repository_uses_remove_command(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.uninstall_hacs_repository)("123")

    assert calls == [("hacs/repository/remove", {"repository": "123"})]


# --- add_custom_repository: plural hacs/repositories/add ------------------------


def test_add_custom_repository_uses_plural_repositories_add(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return {}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.add_custom_repository)("https://github.com/foo/bar", "integration")

    assert calls == [
        ("hacs/repositories/add", {"repository": "https://github.com/foo/bar", "category": "integration"})
    ]


# --- update_hacs_repository: refresh, then info, then download(available_version) ---


def test_update_hacs_repository_sends_refresh_then_download_with_available_version(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        if msg_type == "hacs/repository/refresh":
            return {}
        if msg_type == "hacs/repository/info":
            return {"available_version": "2.0.0", "installed_version": "1.0.0"}
        if msg_type == "hacs/repository/download":
            return {}
        raise AssertionError(f"unexpected msg_type {msg_type!r}")

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.update_hacs_repository)("123")

    assert calls == [
        ("hacs/repository/refresh", {"repository": "123"}),
        ("hacs/repository/info", {"repository_id": "123"}),
        ("hacs/repository/download", {"repository": "123", "version": "2.0.0"}),
    ]


def test_update_hacs_repository_stops_if_refresh_fails(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        raise RuntimeError("boom")

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(hacs_tools.update_hacs_repository)("123")

    assert calls == ["hacs/repository/refresh"]
    assert "error" in result


def test_update_hacs_repository_errors_if_no_available_version_reported(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append(msg_type)
        if msg_type == "hacs/repository/refresh":
            return {}
        if msg_type == "hacs/repository/info":
            return {"installed_version": "1.0.0"}  # no available_version
        raise AssertionError(f"unexpected msg_type {msg_type!r}")

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(hacs_tools.update_hacs_repository)("123")

    assert calls == ["hacs/repository/refresh", "hacs/repository/info"]
    assert "error" in result


# --- list_hacs_repositories: unchanged, already-correct plural list command ---


def test_list_hacs_repositories_uses_plural_list_command(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return []

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.list_hacs_repositories)(category="integration")

    assert calls == [("hacs/repositories/list", {"categories": ["integration"]})]


def test_list_hacs_repositories_without_category_omits_categories_field(monkeypatch):
    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        return []

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(hacs_tools.list_hacs_repositories)()

    assert calls == [("hacs/repositories/list", {})]


# --- list_hacs_critical_updates: unchanged, still filters hacs/repositories/list ---


def test_list_hacs_critical_updates_filters_pending_upgrade(monkeypatch):
    def fake_ws_call(msg_type, **kwargs):
        assert msg_type == "hacs/repositories/list"
        return [
            {"repository_id": "1", "pending_upgrade": True},
            {"repository_id": "2", "pending_upgrade": False},
        ]

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(hacs_tools.list_hacs_critical_updates)()

    assert result == [{"repository_id": "1", "pending_upgrade": True}]
