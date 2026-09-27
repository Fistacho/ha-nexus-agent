"""ADR-0004 "Powiazany dlug" D-1/D-2: secret redaction and self-add-on guard
for `tools/supervisor.py`, hardened after the W3 Security review's NO-GO
(2026-09-27) — see that review's M1-M4/S1-S2 for the exact findings this
file's newer sections close.

D-1 (unchanged from the first W3 pass): `supervisor_get_addon` (`GET
/addons/<slug>/info`) comes back from Supervisor with `options` fully
unredacted whenever the caller holds the "manager"/"admin" hassio_role
(`supervisor/api/apps.py::APIApps.info_data`, verified against
`home-assistant/supervisor` @ `main`, read 2026-09-27) — nexus's own
`config.yaml` declares `hassio_role: manager`, so every installed add-on's
secrets come back in full on every call. Redaction combines three
independent signals, all recursive through nested option groups/lists:

- the add-on's own `schema` (`app.schema_ui`) marking a field `"format":
  "password"`;
- the option key's own name looking like a secret (M2 review: `password`,
  `passwd`, `pass`, `pwd`, `secret`, `token`, `api_key`/`apikey`,
  `credential`, `private`, `key` as a whole word/suffix — exact-token
  matched, so `passenger`/`bypass_cache` are NOT flagged);
- (M3 review) the value itself being a URL with inline credentials
  (`scheme://user:pass@host`), independent of key name or schema.

`redacted_fields` (review M3: now carries *why*) is a list of `{"path",
"reason"}` dicts, `reason` one of `schema_password`, `key_name_heuristic`,
`credentials_in_url`.

`supervisor_set_addon_options` performs a read-modify-write when the caller
echoes the placeholder back for a field. Review S1: a list containing the
placeholder is merged strictly by index, and a length mismatch against the
stored list raises rather than guesses. Review S2: once any placeholder was
resolved this way, a subsequent Supervisor error has its `detail` stripped,
since Supervisor can echo a rejected payload's values (the just-restored
secret included) straight back in that field.

D-2 (redesigned, review M1/M4): `supervisor_set_addon_options`,
`supervisor_uninstall_addon` and `supervisor_stop_addon` all refuse to
target nexus's own add-on via the shared `self_protection.is_own_addon`
guard (its real slug, cached at startup — see `self_protection.py`'s module
docstring for the three-state fail-closed contract now used instead of a
per-call `GET /addons/self/info` — or the literal `"self"` alias Supervisor
resolves to the calling add-on). `supervisor_restart_addon` stays
unblocked: restarting nexus with unchanged options is not privilege
escalation.
"""
from __future__ import annotations

import pytest

import self_protection
from tools import supervisor as supervisor_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


get_addon = _unwrap(supervisor_tools.get_addon)
set_addon_options = _unwrap(supervisor_tools.set_addon_options)
uninstall_addon = _unwrap(supervisor_tools.uninstall_addon)
stop_addon = _unwrap(supervisor_tools.stop_addon)
restart_addon = _unwrap(supervisor_tools.restart_addon)

_REDACTED = supervisor_tools._REDACTED

_OWN_SLUG = "5c53de3b_nexus"


@pytest.fixture(autouse=True)
def _reset_self_protection():
    """Every test starts from (and leaves) the uninitialized default —
    self_protection is process-wide global state, shared with
    `tests/test_self_protection.py` and any other module that imports it."""
    self_protection.reset_for_tests()
    yield
    self_protection.reset_for_tests()


# ---------------------------------------------------------------------------
# Helpers to build a fake `GET /addons/<slug>/info` response
# ---------------------------------------------------------------------------


def _info_envelope(data: dict) -> dict:
    return {"result": "ok", "data": data}


def _password_schema(name: str, *, multiple: bool = False) -> dict:
    node = {"name": name, "type": "string", "format": "password", "required": True}
    if multiple:
        node["multiple"] = True
    return node


def _plain_schema(name: str) -> dict:
    return {"name": name, "type": "string", "required": True}


def _nested_schema(name: str, children: list[dict], *, multiple: bool = False) -> dict:
    return {"name": name, "type": "schema", "optional": True, "multiple": multiple, "schema": children}


def _redacted_paths(result: dict) -> list[str]:
    return [f["path"] for f in result["redacted_fields"]]


def _redacted_reasons(result: dict) -> dict[str, str]:
    return {f["path"]: f["reason"] for f in result["redacted_fields"]}


# ---------------------------------------------------------------------------
# D-1a: schema-driven redaction (format: "password")
# ---------------------------------------------------------------------------


def test_get_addon_redacts_a_schema_password_field(monkeypatch):
    data = {
        "slug": "core_mosquitto",
        "options": {"logins": [], "customize": {}, "password": "hunter2"},
        "schema": [_password_schema("password")],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("core_mosquitto")

    assert result["data"]["options"]["password"] == _REDACTED
    assert result["redacted_fields"] == [{"path": "password", "reason": "schema_password"}]


def test_get_addon_leaves_non_password_schema_fields_alone(monkeypatch):
    data = {
        "slug": "core_mosquitto",
        "options": {"anonymous": True},
        "schema": [_plain_schema("anonymous")],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("core_mosquitto")

    assert result["data"]["options"]["anonymous"] is True
    assert result["redacted_fields"] == []


def test_get_addon_redacts_multiple_password_values_in_a_list(monkeypatch):
    data = {
        "slug": "some_addon",
        "options": {"api_keys": ["abc123", "def456"]},
        "schema": [_password_schema("api_keys", multiple=True)],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("some_addon")

    assert result["data"]["options"]["api_keys"] == [_REDACTED, _REDACTED]
    assert _redacted_paths(result) == ["api_keys[0]", "api_keys[1]"]
    assert set(_redacted_reasons(result).values()) == {"schema_password"}


# ---------------------------------------------------------------------------
# D-1b: heuristic key-name redaction (no/false/stale schema) — M2 review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password", "passwd", "pass", "pwd", "secret", "token", "api_key", "apikey",
        "credential", "private", "key", "mqtt_password", "db_pwd",
    ],
)
def test_get_addon_redacts_secret_looking_keys_without_schema_support(monkeypatch, key):
    data = {"slug": "eon_pl", "options": {key: "s3cr3t-value"}, "schema": None}
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"][key] == _REDACTED
    assert result["redacted_fields"] == [{"path": key, "reason": "key_name_heuristic"}]


def test_get_addon_redacts_camel_case_secret_key(monkeypatch):
    data = {"slug": "eon_pl", "options": {"apiKey": "s3cr3t"}, "schema": False}
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"]["apiKey"] == _REDACTED


@pytest.mark.parametrize("key", ["username", "host", "keyboard_layout", "port", "passenger", "bypass_cache"])
def test_get_addon_does_not_flag_unrelated_keys(monkeypatch, key):
    """M2 review regression: `pass`/`pwd` are exact-token matches, so
    `passenger`/`bypass_cache` — neither of which tokenizes to a bare
    `pass` — must NOT be flagged."""
    data = {"slug": "eon_pl", "options": {key: "plain-value"}, "schema": None}
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"][key] == "plain-value"
    assert result["redacted_fields"] == []


# ---------------------------------------------------------------------------
# D-1c: empty/unset secret values stay visibly empty
# ---------------------------------------------------------------------------


def test_get_addon_leaves_empty_password_value_empty(monkeypatch):
    data = {
        "slug": "core_mosquitto",
        "options": {"password": ""},
        "schema": [_password_schema("password")],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("core_mosquitto")

    assert result["data"]["options"]["password"] == ""
    assert result["redacted_fields"] == []


def test_get_addon_leaves_none_password_value_as_none(monkeypatch):
    data = {
        "slug": "core_mosquitto",
        "options": {"password": None},
        "schema": [_password_schema("password")],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("core_mosquitto")

    assert result["data"]["options"]["password"] is None
    assert result["redacted_fields"] == []


# ---------------------------------------------------------------------------
# D-1d: recursion through nested option groups (dict/list, ADR-0004 nesting)
# ---------------------------------------------------------------------------


def test_get_addon_redacts_password_nested_one_level_deep(monkeypatch):
    data = {
        "slug": "eon_pl",
        "options": {"mqtt": {"host": "core-mosquitto", "password": "hunter2"}},
        "schema": [_nested_schema("mqtt", [_plain_schema("host"), _password_schema("password")])],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"]["mqtt"]["password"] == _REDACTED
    assert result["data"]["options"]["mqtt"]["host"] == "core-mosquitto"
    assert result["redacted_fields"] == [{"path": "mqtt.password", "reason": "schema_password"}]


def test_get_addon_redacts_password_in_a_list_of_nested_groups(monkeypatch):
    data = {
        "slug": "eon_pl",
        "options": {
            "accounts": [
                {"user": "a", "password": "pw-a"},
                {"user": "b", "password": "pw-b"},
            ]
        },
        "schema": [
            _nested_schema(
                "accounts", [_plain_schema("user"), _password_schema("password")], multiple=True
            )
        ],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    accounts = result["data"]["options"]["accounts"]
    assert accounts[0] == {"user": "a", "password": _REDACTED}
    assert accounts[1] == {"user": "b", "password": _REDACTED}
    assert _redacted_paths(result) == ["accounts[0].password", "accounts[1].password"]


def test_get_addon_heuristic_recurses_into_nested_dict_without_schema(monkeypatch):
    data = {
        "slug": "eon_pl",
        "options": {"mqtt": {"host": "core-mosquitto", "password": "hunter2"}},
        "schema": None,
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"]["mqtt"]["password"] == _REDACTED
    assert result["data"]["options"]["mqtt"]["host"] == "core-mosquitto"


# ---------------------------------------------------------------------------
# D-1e: no options / errors pass through unchanged
# ---------------------------------------------------------------------------


def test_get_addon_passes_through_errors_unchanged(monkeypatch):
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: {"error": "HTTP 404", "detail": "no such addon"}
    )
    result = get_addon("missing")
    assert result == {"error": "HTTP 404", "detail": "no such addon"}


def test_get_addon_handles_missing_options_gracefully(monkeypatch):
    data = {"slug": "some_addon", "options": None, "schema": None}
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("some_addon")

    assert result["data"]["options"] is None
    assert result["redacted_fields"] == []


# ---------------------------------------------------------------------------
# M3 review: in-value URL-credentials redaction, independent of key/schema
# ---------------------------------------------------------------------------


def test_get_addon_redacts_a_url_with_inline_credentials_on_an_innocuous_key(monkeypatch):
    data = {
        "slug": "eon_pl",
        "options": {"broker": "mqtt://iot:s3cr3t@core-mosquitto:1883"},
        "schema": [_plain_schema("broker")],
    }
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"]["broker"] == _REDACTED
    assert result["redacted_fields"] == [{"path": "broker", "reason": "credentials_in_url"}]


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1/status",
        "mqtt://core-mosquitto:1883",
        "postgres://db-host:5432/dbname",
    ],
)
def test_get_addon_does_not_flag_a_url_without_inline_credentials(monkeypatch, url):
    data = {"slug": "eon_pl", "options": {"broker": url}, "schema": None}
    monkeypatch.setattr(supervisor_tools, "_supervisor_request", lambda *a, **k: _info_envelope(data))

    result = get_addon("eon_pl")

    assert result["data"]["options"]["broker"] == url
    assert result["redacted_fields"] == []


def test_classify_scalar_priority_schema_beats_url_scan():
    """Priority order (`_classify_scalar`): schema wins over the URL scan
    when both could apply — not user-observable in practice (a
    schema-declared password field holding a bare URL is unusual), but
    pins the documented priority order."""
    reason = supervisor_tools._classify_scalar(
        "mqtt://iot:s3cr3t@host", {"format": "password"}, key_should_redact=False
    )
    assert reason == "schema_password"


def test_classify_scalar_priority_key_heuristic_beats_url_scan():
    reason = supervisor_tools._classify_scalar(
        "mqtt://iot:s3cr3t@host", {}, key_should_redact=True
    )
    assert reason == "key_name_heuristic"


# ---------------------------------------------------------------------------
# D-1f: `set_addon_options` read-modify-write for the redaction placeholder
# ---------------------------------------------------------------------------


def test_set_addon_options_preserves_real_secret_when_marker_is_echoed_back(monkeypatch):
    stored = {"slug": "core_mosquitto", "options": {"password": "hunter2", "anonymous": False}}
    posted = {}

    def fake_request(method, path, json=None):
        if method == "GET":
            return _info_envelope(stored)
        posted["method"] = method
        posted["path"] = path
        posted["json"] = json
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("core_mosquitto", {"password": _REDACTED, "anonymous": True})

    assert result == {"result": "ok"}
    assert posted["path"] == "/addons/core_mosquitto/options"
    assert posted["json"] == {"options": {"password": "hunter2", "anonymous": True}}


def test_set_addon_options_skips_the_options_read_when_no_marker_present(monkeypatch):
    """No self_protection lookup at all (it's a pure in-memory check now,
    see `self_protection.py`) and no merge-read GET when there's no
    placeholder to resolve — a single POST, nothing else."""
    calls = []

    def fake_request(method, path, json=None):
        calls.append((method, path))
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("core_mosquitto", {"anonymous": True})

    assert result == {"result": "ok"}
    assert calls == [("POST", "/addons/core_mosquitto/options")]


def test_set_addon_options_surfaces_read_error_instead_of_writing_marker_literally(monkeypatch):
    def fake_request(method, path, json=None):
        if method == "GET":
            return {"error": "HTTP 404", "detail": "no such addon"}
        raise AssertionError("must not POST after a failed read-modify-write GET")

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("core_mosquitto", {"password": _REDACTED})

    assert result == {"error": "HTTP 404", "detail": "no such addon"}


def test_set_addon_options_preserves_marker_nested_one_level_deep(monkeypatch):
    stored = {"slug": "eon_pl", "options": {"mqtt": {"host": "core-mosquitto", "password": "hunter2"}}}
    posted = {}

    def fake_request(method, path, json=None):
        if method == "GET":
            return _info_envelope(stored)
        posted["json"] = json
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    set_addon_options("eon_pl", {"mqtt": {"host": "new-host", "password": _REDACTED}})

    assert posted["json"] == {"options": {"mqtt": {"host": "new-host", "password": "hunter2"}}}


# ---------------------------------------------------------------------------
# S1 review: list-length mismatch on a placeholder list is refused
# ---------------------------------------------------------------------------


def test_set_addon_options_refuses_mismatched_length_placeholder_list(monkeypatch):
    stored = {"slug": "eon_pl", "options": {"api_keys": ["k1", "k2", "k3"]}}

    def fake_request(method, path, json=None):
        if method == "GET":
            return _info_envelope(stored)
        raise AssertionError("must not POST when the placeholder list length can't be resolved")

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("eon_pl", {"api_keys": [_REDACTED, "k2"]})

    assert result["error"] == "redaction_marker_list_length_mismatch"


def test_set_addon_options_allows_same_length_placeholder_list(monkeypatch):
    stored = {"slug": "eon_pl", "options": {"api_keys": ["k1", "k2", "k3"]}}
    posted = {}

    def fake_request(method, path, json=None):
        if method == "GET":
            return _info_envelope(stored)
        posted["json"] = json
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("eon_pl", {"api_keys": [_REDACTED, "k2-edited", _REDACTED]})

    assert result == {"result": "ok"}
    assert posted["json"] == {"options": {"api_keys": ["k1", "k2-edited", "k3"]}}


# ---------------------------------------------------------------------------
# S2 review: sanitized `detail` once a placeholder was resolved
# ---------------------------------------------------------------------------


def test_set_addon_options_sanitizes_detail_on_post_failure_after_marker_resolution(monkeypatch):
    stored = {"slug": "eon_pl", "options": {"password": "hunter2"}}

    def fake_request(method, path, json=None):
        if method == "GET":
            return _info_envelope(stored)
        return {"error": "HTTP 400", "detail": "invalid config: password 'hunter2' too short"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("eon_pl", {"password": _REDACTED})

    assert result["error"] == "HTTP 400"
    assert "hunter2" not in result["detail"]


def test_set_addon_options_keeps_detail_when_no_marker_was_involved(monkeypatch):
    """No placeholder in the request at all — nothing was reconstituted, so
    the ordinary (unsanitized) Supervisor error detail is still useful."""

    def fake_request(method, path, json=None):
        return {"error": "HTTP 400", "detail": "invalid config: 'port' out of range"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("eon_pl", {"port": 999999})

    assert result == {"error": "HTTP 400", "detail": "invalid config: 'port' out of range"}


# ---------------------------------------------------------------------------
# D-2 (redesigned, M1/M4 review): self-add-on guard via `self_protection`
# ---------------------------------------------------------------------------


def test_set_addon_options_blocks_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = set_addon_options(_OWN_SLUG, {"api_key": "new-value"})

    assert result["error"] == "self_addon_options_blocked"


def test_set_addon_options_blocks_literal_self_alias(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = set_addon_options("self", {"api_key": "new-value"})

    assert result["error"] == "self_addon_options_blocked"


def test_set_addon_options_does_not_block_other_addons(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    posted = {}

    def fake_request(method, path, json=None):
        posted["method"] = method
        posted["path"] = path
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("core_mosquitto", {"anonymous": True})

    assert result == {"result": "ok"}
    assert posted == {"method": "POST", "path": "/addons/core_mosquitto/options"}


def test_set_addon_options_fails_closed_when_own_slug_unknown_in_addon_mode(monkeypatch):
    """M4 review: add-on mode confirmed (`set_own_slug(None)`) but the slug
    itself unresolved — every slug is refused, not just a literal match."""
    self_protection.set_own_slug(None)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = set_addon_options("core_mosquitto", {"anonymous": True})

    assert result["error"] == "self_addon_options_blocked"


def test_set_addon_options_is_inert_outside_addon_mode(monkeypatch):
    """Standalone (self_protection never initialized): the guard does
    nothing — there is no "own add-on" to protect."""
    posted = {}

    def fake_request(method, path, json=None):
        posted["method"] = method
        posted["path"] = path
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = set_addon_options("core_mosquitto", {"anonymous": True})

    assert result == {"result": "ok"}


def test_uninstall_addon_confirm_false_needs_no_supervisor_call_even_for_self(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)

    def _boom(*a, **k):
        raise AssertionError("must not call Supervisor before the confirm=False gate")

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", _boom)

    result = uninstall_addon(_OWN_SLUG, confirm=False)

    assert result["error"] == "self_addon_uninstall_blocked"


def test_uninstall_addon_confirm_false_for_a_foreign_slug_still_asks_for_confirmation(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)

    def _boom(*a, **k):
        raise AssertionError("must not call Supervisor before the confirm=False gate")

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", _boom)

    result = uninstall_addon("core_mosquitto", confirm=False)

    assert result["error"] == "confirmation_required"


def test_uninstall_addon_blocks_own_slug_once_confirmed(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = uninstall_addon(_OWN_SLUG, confirm=True)

    assert result["error"] == "self_addon_uninstall_blocked"


def test_uninstall_addon_blocks_literal_self_alias_once_confirmed(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = uninstall_addon("self", confirm=True)

    assert result["error"] == "self_addon_uninstall_blocked"


def test_uninstall_addon_still_uninstalls_other_addons_once_confirmed(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    posted = {}

    def fake_request(method, path, json=None):
        posted["method"] = method
        posted["path"] = path
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = uninstall_addon("core_mosquitto", confirm=True)

    assert result == {"result": "ok"}
    assert posted == {"method": "POST", "path": "/addons/core_mosquitto/uninstall"}


def test_stop_addon_blocks_own_slug(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = stop_addon(_OWN_SLUG)

    assert result["error"] == "self_addon_stop_blocked"


def test_stop_addon_blocks_literal_self_alias(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    monkeypatch.setattr(
        supervisor_tools, "_supervisor_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no I/O"))
    )

    result = stop_addon("self")

    assert result["error"] == "self_addon_stop_blocked"


def test_stop_addon_still_stops_other_addons(monkeypatch):
    self_protection.set_own_slug(_OWN_SLUG)
    posted = {}

    def fake_request(method, path, json=None):
        posted["method"] = method
        posted["path"] = path
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = stop_addon("core_mosquitto")

    assert result == {"result": "ok"}
    assert posted == {"method": "POST", "path": "/addons/core_mosquitto/stop"}


def test_restart_addon_is_deliberately_not_blocked_for_self(monkeypatch):
    """ADR-0004 recommendation: restarting nexus with unchanged options is
    not privilege escalation, so `supervisor_restart_addon` stays unblocked
    even against nexus's own slug — unlike stop/uninstall/set_options."""
    self_protection.set_own_slug(_OWN_SLUG)
    posted = {}

    def fake_request(method, path, json=None):
        posted["method"] = method
        posted["path"] = path
        return {"result": "ok"}

    monkeypatch.setattr(supervisor_tools, "_supervisor_request", fake_request)

    result = restart_addon(_OWN_SLUG)

    assert result == {"result": "ok"}
    assert posted == {"method": "POST", "path": f"/addons/{_OWN_SLUG}/restart"}


# ---------------------------------------------------------------------------
# Helper unit tests (heuristic + merge/contains-marker)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("password", True),
        ("passwd", True),
        ("pass", True),
        ("pwd", True),
        ("api_key", True),
        ("apikey", True),
        ("apiKey", True),
        ("API_KEY", True),
        ("key", True),
        ("public_key", True),
        ("secret_token", True),
        ("credential", True),
        ("private_data", True),
        ("mqtt_pass", True),
        ("db_pwd", True),
        ("credentials", True),
        ("secrets", True),
        ("tokens", True),
        ("passwords", True),
        ("api_keys", True),
        ("username", False),
        ("host", False),
        ("keyboard_layout", False),
        ("port", False),
        ("passenger", False),
        ("bypass_cache", False),
    ],
)
def test_looks_like_secret_key_heuristic(key, expected):
    assert supervisor_tools._looks_like_secret_key(key) is expected


def test_contains_redaction_marker_detects_nested_marker():
    assert supervisor_tools._contains_redaction_marker({"a": {"b": [_REDACTED]}}) is True
    assert supervisor_tools._contains_redaction_marker({"a": {"b": ["x"]}}) is False


def test_merge_redaction_markers_keeps_new_edits_and_restores_marker_leaves():
    new = {"a": _REDACTED, "b": "edited", "c": [_REDACTED, "kept"]}
    old = {"a": "real-secret", "b": "stale", "c": ["old-0", "old-1"]}

    merged = supervisor_tools._merge_redaction_markers(new, old)

    assert merged == {"a": "real-secret", "b": "edited", "c": ["old-0", "kept"]}


def test_merge_redaction_markers_raises_on_list_length_mismatch():
    new = {"a": [_REDACTED, "edited"]}
    old = {"a": ["x", "y", "z"]}

    with pytest.raises(supervisor_tools.RedactionMergeError):
        supervisor_tools._merge_redaction_markers(new, old)


def test_merge_redaction_markers_allows_list_without_marker_even_if_length_differs():
    """Only a list that actually contains the placeholder is
    length-checked — a plain, fully-specified replacement list is a normal
    edit, not an ambiguous positional merge."""
    new = {"a": ["only-one-now"]}
    old = {"a": ["x", "y", "z"]}

    merged = supervisor_tools._merge_redaction_markers(new, old)

    assert merged == {"a": ["only-one-now"]}
