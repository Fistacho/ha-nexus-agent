"""`service_guard.py` — ADR-0006 "confirm w generycznym wywolaniu uslug".

Covers: `validate_service_name` (D3, F2-F4), the closed `GUARDED_SERVICES`
list (D2), `CONFIRM_TOOL_EQUIVALENTS`'s completeness against the live
`confirm`-tool set on `server.mcp`, and the `confirm_gate` helper
`services_call_service` uses (D4).
"""
from __future__ import annotations

import asyncio

import pytest

import service_guard as sg


# ---------------------------------------------------------------------------
# D3 — validate_service_name (F2-F4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["light", "turn_on", "homeassistant", "restart", "input_boolean", "A", "a1_2", "HASSIO", "ADDON_STOP"],
)
def test_validate_service_name_accepts_ascii_word_chars(name):
    assert sg.validate_service_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "addon_stop?x",
        "../hassio/addon_stop",
        "hassio/addon_stop",
        "addon.stop",
        "addon stop",
        "addon-stop",
        "addon_stop#frag",
        "café",
    ],
)
def test_validate_service_name_rejects_anything_else(name):
    assert sg.validate_service_name(name) is False


# ---------------------------------------------------------------------------
# D2 — GUARDED_SERVICES: exactly these eight
# ---------------------------------------------------------------------------


def test_guarded_services_is_exactly_the_adr_list():
    assert sg.GUARDED_SERVICES == frozenset(
        {
            sg.GuardedService("homeassistant", "restart"),
            sg.GuardedService("homeassistant", "stop"),
            sg.GuardedService("hassio", "host_reboot"),
            sg.GuardedService("hassio", "host_shutdown"),
            sg.GuardedService("hassio", "restore_full"),
            sg.GuardedService("hassio", "restore_partial"),
            sg.GuardedService("update", "install"),
            sg.GuardedService("group", "remove"),
        }
    )
    assert len(sg.GUARDED_SERVICES) == 8


@pytest.mark.parametrize(
    "domain,service",
    [
        ("hassio", "backup_full"),
        ("hassio", "addon_stop"),
        ("backup", "create"),
        ("recorder", "purge"),
        ("light", "turn_on"),
    ],
)
def test_is_guarded_false_for_deliberately_out_of_scope_services(domain, service):
    assert sg.is_guarded(domain, service) is False


@pytest.mark.parametrize(
    "domain,service",
    [
        ("homeassistant", "restart"),
        ("HOMEASSISTANT", "RESTART"),
        ("homeassistant", "stop"),
        ("hassio", "host_reboot"),
        ("hassio", "host_shutdown"),
        ("hassio", "restore_full"),
        ("hassio", "restore_partial"),
        ("update", "install"),
        ("UPDATE", "Install"),
        ("group", "remove"),
    ],
)
def test_is_guarded_true_case_insensitively(domain, service):
    assert sg.is_guarded(domain, service) is True


# ---------------------------------------------------------------------------
# D2 — CONFIRM_TOOL_EQUIVALENTS completeness
# ---------------------------------------------------------------------------


def _confirm_tool_names() -> set[str]:
    """Every tool name in `server.mcp.list_tools()` whose schema declares a
    `confirm` parameter — the live, ground-truth set `CONFIRM_TOOL_EQUIVALENTS`
    must match exactly."""
    import server

    tools = asyncio.run(server.mcp.list_tools())
    names = set()
    for tool in tools:
        props = (tool.parameters or {}).get("properties", {})
        if "confirm" in props:
            names.add(tool.name)
    return names


def test_confirm_tool_equivalents_keys_match_live_confirm_tools():
    live = _confirm_tool_names()
    mapped = set(sg.CONFIRM_TOOL_EQUIVALENTS)
    assert mapped == live, (
        f"CONFIRM_TOOL_EQUIVALENTS keys != live confirm-tool set.\n"
        f"In map but not live: {sorted(mapped - live)}\n"
        f"Live but not in map: {sorted(live - mapped)}"
    )


def test_confirm_tool_equivalents_pairs_are_all_guarded_services():
    for tool_name, equivalents in sg.CONFIRM_TOOL_EQUIVALENTS.items():
        if isinstance(equivalents, str):
            continue
        for pair in equivalents:
            assert pair in sg.GUARDED_SERVICES, (
                f"{tool_name} maps to {pair!r}, which is not in GUARDED_SERVICES"
            )


def test_confirm_tool_equivalents_non_tuple_values_are_nonempty_strings():
    for tool_name, equivalents in sg.CONFIRM_TOOL_EQUIVALENTS.items():
        if isinstance(equivalents, tuple):
            continue
        assert isinstance(equivalents, str) and equivalents.strip(), (
            f"{tool_name}'s CONFIRM_TOOL_EQUIVALENTS value must be a nonempty "
            "justification string when it isn't a tuple of GuardedService pairs"
        )


def test_services_call_service_maps_to_every_guarded_service():
    assert set(sg.CONFIRM_TOOL_EQUIVALENTS["services_call_service"]) == sg.GUARDED_SERVICES


# ---------------------------------------------------------------------------
# dedicated_tool_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain,service,expected",
    [
        ("homeassistant", "restart", "system_restart_ha"),
        ("HOMEASSISTANT", "RESTART", "system_restart_ha"),
        ("homeassistant", "stop", "system_stop_ha"),
        ("hassio", "host_reboot", "supervisor_restart_host"),
        ("hassio", "restore_full", "supervisor_restore_backup"),
        ("group", "remove", "automations_remove_group"),
        ("update", "install", "esphome_upload_device"),
    ],
)
def test_dedicated_tool_for_known_pairs(domain, service, expected):
    assert sg.dedicated_tool_for(domain, service) == expected


@pytest.mark.parametrize(
    "domain,service",
    [("hassio", "host_shutdown"), ("hassio", "restore_partial"), ("light", "turn_on")],
)
def test_dedicated_tool_for_returns_none_when_no_dedicated_tool_exists(domain, service):
    assert sg.dedicated_tool_for(domain, service) is None


# ---------------------------------------------------------------------------
# D4 — confirm_gate
# ---------------------------------------------------------------------------


def test_confirm_gate_none_for_unguarded_service_regardless_of_confirm():
    assert sg.confirm_gate("light", "turn_on", {"entity_id": "light.x"}, confirm=False) is None
    assert sg.confirm_gate("light", "turn_on", {"entity_id": "light.x"}, confirm=True) is None


def test_confirm_gate_none_when_guarded_and_confirmed():
    assert sg.confirm_gate("homeassistant", "restart", None, confirm=True) is None


def test_confirm_gate_denies_guarded_service_without_confirm():
    result = sg.confirm_gate("homeassistant", "restart", None, confirm=False)
    assert result["error"] == "confirmation_required"
    assert isinstance(result["message"], str) and "system_restart_ha" in result["message"]
    assert "confirm=True" in result["action"]


def test_confirm_gate_denies_case_insensitively():
    result = sg.confirm_gate("HOMEASSISTANT", "RESTART", None, confirm=False)
    assert result["error"] == "confirmation_required"


def test_confirm_gate_message_has_no_dedicated_tool_pointer_when_none_exists():
    result = sg.confirm_gate("hassio", "host_shutdown", None, confirm=False)
    assert result["error"] == "confirmation_required"
    assert "Prefer the dedicated tool" not in result["message"]
