"""ADR-0006 follow-up (Security review, 2026-09-28): "MUST z tej samej klasy
co F3". `service_guard.valid_entity_id`/`path_segment` are the shared
building blocks `ha_client.py` and `tools/supervisor.py` both use to stop a
caller-supplied string from being spliced, unvalidated, into an HTTP path
segment — the same class of bug ADR-0006 F3 closed for `services_call_service`
(a `?`/`#`-suffixed value, or a `..`-segment path escape, reaching a
different HA/Supervisor endpoint than the one the caller named).
"""
from __future__ import annotations

import pytest

import service_guard as sg


# ---------------------------------------------------------------------------
# valid_entity_id — must match home-assistant/core's own `valid_entity_id`
# (homeassistant/core.py, verified against home-assistant/core@dev, read
# 2026-09-28): `^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity_id",
    [
        "light.kitchen",
        "binary_sensor.motion",
        "a.b",
        "light.a1_2",
        "input_boolean.guest_mode",
    ],
)
def test_valid_entity_id_accepts_real_shapes(entity_id):
    assert sg.valid_entity_id(entity_id) is True


@pytest.mark.parametrize(
    "entity_id",
    [
        "",
        "light",
        "light.",
        ".kitchen",
        "light..kitchen",
        "light.kitchen.extra",
        "_light.kitchen",
        "light.kitchen_",
        "light_.kitchen",
        "light.__kitchen",
        "li__ght.kitchen",
        "Light.Kitchen",
        "light.Kitchen",
        "light.kitchen/../../hassio/addon_stop",
        "light.kitchen?x=1",
        "light.kitchen#frag",
        "../states/light.kitchen",
        "light.kitchen ",
    ],
)
def test_valid_entity_id_rejects_anything_else(entity_id):
    assert sg.valid_entity_id(entity_id) is False


# ---------------------------------------------------------------------------
# path_segment — the shared helper ha_client.py/tools/supervisor.py apply
# before splicing a value into an f-string URL path
# ---------------------------------------------------------------------------


def test_path_segment_entity_id_returns_value_unchanged_when_valid():
    assert sg.path_segment("light.kitchen", "entity_id") == "light.kitchen"


@pytest.mark.parametrize(
    "value",
    ["../services/hassio/addon_stop", "light.kitchen/../x", "light.kitchen?x", "light.kitchen#x", "LIGHT.KITCHEN"],
)
def test_path_segment_entity_id_raises_for_invalid(value):
    with pytest.raises(ValueError):
        sg.path_segment(value, "entity_id")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("kitchen_sensor", "kitchen_sensor"),
        ("automation.1733294456585", "automation.1733294456585"),
        ("core_mosquitto", "core_mosquitto"),
        ("5c53de3b_nexus", "5c53de3b_nexus"),
    ],
)
def test_path_segment_identifier_passes_through_safe_characters(value, expected):
    assert sg.path_segment(value, "identifier") == expected


def test_path_segment_identifier_percent_encodes_a_harmless_special_character():
    """A value with no path-structural character still round-trips through
    `quote()` for anything outside the unreserved set (space here) --
    proves encoding, not just a pass-through, actually runs."""
    assert sg.path_segment("my event", "identifier") == "my%20event"


@pytest.mark.parametrize(
    "value",
    [
        "../services/hassio/host_shutdown",
        "../hassio/addon_stop",
        "light.kitchen/../../hassio/addon_stop",
        "addon_stop?x=1",
        "addon_stop#frag",
        "..",
        "a/b",
        "a\\b",
        "%2e%2e",
        "%2e%2e%2fhassio%2faddon_stop",
        "%2f%2e%2e",
    ],
)
def test_path_segment_identifier_raises_for_path_structural_payloads(value):
    """Rejected outright (ValueError, zero HTTP) rather than silently
    encoded-through: a `/`, `?`, `#`, literal `..`, or a percent-encoded
    disguise of the same (checked by decoding once and re-testing) in an
    id/event-type/timestamp is never a legitimate value in practice, and
    failing loud here matches the "confirmation_required"-style
    fail-before-I/O convention the rest of ADR-0006 already established,
    rather than relying solely on the encoding in the non-raising branch.
    """
    with pytest.raises(ValueError):
        sg.path_segment(value, "identifier")


@pytest.mark.parametrize("value", ["", None, 123])
def test_path_segment_identifier_rejects_empty_or_non_string(value):
    with pytest.raises(ValueError):
        sg.path_segment(value, "identifier")


def test_path_segment_rejects_unknown_kind():
    with pytest.raises(ValueError):
        sg.path_segment("light.kitchen", "bogus_kind")
