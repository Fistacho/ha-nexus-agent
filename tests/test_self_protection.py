"""`self_protection.py` — single source of truth for nexus's own add-on slug
and the D-2 guard shared by `tools/supervisor.py`, `tools/services.py` and
`tools/websocket.py` (ADR-0004 "Powiazany dlug" D-2, hardened after the W3
Security review's NO-GO: M1 closes the `hassio.*` service bypass, M4 makes
identity resolution fail-closed).
"""
from __future__ import annotations

import pytest

import self_protection as sp


@pytest.fixture(autouse=True)
def _reset():
    """Every test starts from (and leaves) the uninitialized default state."""
    sp.reset_for_tests()
    yield
    sp.reset_for_tests()


# ---------------------------------------------------------------------------
# State 1: never initialized — inert
# ---------------------------------------------------------------------------


def test_uninitialized_is_inert_for_any_slug():
    assert sp.is_initialized() is False
    assert sp.is_own_addon("5c53de3b_nexus") is False
    assert sp.is_own_addon("self") is False
    assert sp.is_own_addon("core_mosquitto") is False


def test_uninitialized_blocked_hassio_service_call_never_blocks():
    assert sp.blocked_hassio_service_call("hassio", "addon_stop", {"addon": "self"}) is None


# ---------------------------------------------------------------------------
# State 2: initialized with a real slug
# ---------------------------------------------------------------------------


def test_known_slug_matches_itself_and_the_self_alias():
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.is_own_addon("5c53de3b_nexus") is True
    assert sp.is_own_addon("self") is True


def test_known_slug_does_not_match_a_foreign_slug():
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.is_own_addon("core_mosquitto") is False


def test_get_own_slug_returns_the_recorded_value():
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.get_own_slug() == "5c53de3b_nexus"
    assert sp.is_initialized() is True


# ---------------------------------------------------------------------------
# State 3: initialized but slug unknown — fail-closed for every slug (M4)
# ---------------------------------------------------------------------------


def test_initialized_with_none_blocks_every_slug():
    sp.set_own_slug(None)
    assert sp.is_initialized() is True
    assert sp.get_own_slug() is None
    assert sp.is_own_addon("core_mosquitto") is True
    assert sp.is_own_addon("some_other_addon") is True
    assert sp.is_own_addon("self") is True


def test_initialized_with_none_then_learning_the_slug_narrows_back_down():
    sp.set_own_slug(None)
    assert sp.is_own_addon("core_mosquitto") is True
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.is_own_addon("core_mosquitto") is False
    assert sp.is_own_addon("5c53de3b_nexus") is True


# ---------------------------------------------------------------------------
# `blocked_hassio_service_call` — M1
# ---------------------------------------------------------------------------


def test_non_hassio_domain_is_never_blocked():
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.blocked_hassio_service_call("light", "turn_on", {"entity_id": "light.x"}) is None


@pytest.mark.parametrize("service,key", [("addon_stop", "addon"), ("app_stop", "app"), ("addon_stdin", "addon"), ("app_stdin", "app")])
def test_blocked_services_refuse_own_slug(service, key):
    sp.set_own_slug("5c53de3b_nexus")
    data = {key: "5c53de3b_nexus"}
    if key == "addon" and service == "addon_stdin":
        data["input"] = "hello"
    if key == "app" and service == "app_stdin":
        data["input"] = "hello"
    result = sp.blocked_hassio_service_call("hassio", service, data)
    assert result is not None
    assert result["error"] == "self_addon_hassio_service_blocked"


@pytest.mark.parametrize("service,key", [("addon_stop", "addon"), ("app_stop", "app"), ("addon_stdin", "addon"), ("app_stdin", "app")])
def test_blocked_services_refuse_literal_self_alias(service, key):
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("hassio", service, {key: "self"})
    assert result is not None
    assert result["error"] == "self_addon_hassio_service_blocked"


@pytest.mark.parametrize("service,key", [("addon_stop", "addon"), ("app_stop", "app"), ("addon_stdin", "addon"), ("app_stdin", "app")])
def test_blocked_services_allow_other_addons(service, key):
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("hassio", service, {key: "core_mosquitto"})
    assert result is None


@pytest.mark.parametrize("service,key", [("addon_start", "addon"), ("app_start", "app"), ("addon_restart", "addon"), ("app_restart", "app")])
def test_start_and_restart_are_never_blocked_even_against_own_slug(service, key):
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("hassio", service, {key: "5c53de3b_nexus"})
    assert result is None


@pytest.mark.parametrize("service", ["host_reboot", "host_shutdown"])
def test_host_services_are_out_of_scope_for_this_guard(service):
    """No add-on/app slug field exists on these — never this guard's job."""
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.blocked_hassio_service_call("hassio", service, {}) is None


@pytest.mark.parametrize(
    "service,data",
    [
        ("backup_full", {"name": "nightly"}),
        ("backup_partial", {"addons": ["5c53de3b_nexus"]}),
        ("restore_full", {"slug": "abcd1234"}),
        ("mount_reload", {"device_id": "abc"}),
    ],
)
def test_non_addon_targeted_services_are_out_of_scope(service, data):
    """`backup_partial`'s own `addons` list names backup *contents*, not an
    action against nexus's own add-on identity — deliberately not this
    guard's concern (see module docstring)."""
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.blocked_hassio_service_call("hassio", service, data) is None


def test_blocks_when_own_slug_appears_anywhere_in_a_list_of_slugs():
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call(
        "hassio", "addon_stop", {"addon": ["core_mosquitto", "5c53de3b_nexus"]}
    )
    assert result is not None
    assert result["error"] == "self_addon_hassio_service_blocked"


def test_allows_a_list_of_slugs_with_no_self_match():
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("hassio", "addon_stop", {"addon": ["core_mosquitto", "core_ssh"]})
    assert result is None


def test_missing_data_key_is_not_blocked():
    sp.set_own_slug("5c53de3b_nexus")
    assert sp.blocked_hassio_service_call("hassio", "addon_stop", {}) is None
    assert sp.blocked_hassio_service_call("hassio", "addon_stop", None) is None


def test_unknown_slug_state_blocks_every_hassio_service_call_against_any_slug():
    """State 3 (M4 fail-closed): with the slug unresolved, even an
    ostensibly-foreign target can't be proven not-self."""
    sp.set_own_slug(None)
    result = sp.blocked_hassio_service_call("hassio", "addon_stop", {"addon": "core_mosquitto"})
    assert result is not None
    assert result["error"] == "self_addon_hassio_service_blocked"


# ---------------------------------------------------------------------------
# ADR-0006 D3 (F4 fix): domain/service compared case-insensitively
# ---------------------------------------------------------------------------


def test_blocked_hassio_service_call_matches_uppercase_domain_and_service():
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("HASSIO", "ADDON_STOP", {"addon": "5c53de3b_nexus"})
    assert result is not None
    assert result["error"] == "self_addon_hassio_service_blocked"


def test_blocked_hassio_service_call_matches_mixed_case_domain_and_service():
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("Hassio", "Addon_Stop", {"addon": "self"})
    assert result is not None
    assert result["error"] == "self_addon_hassio_service_blocked"


def test_blocked_hassio_service_call_case_insensitive_still_allows_other_addons():
    sp.set_own_slug("5c53de3b_nexus")
    result = sp.blocked_hassio_service_call("HASSIO", "ADDON_STOP", {"addon": "core_mosquitto"})
    assert result is None
