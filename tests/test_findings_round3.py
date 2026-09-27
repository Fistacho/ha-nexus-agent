"""Regression tests for round-3 finding in ``tools/automations.py``.

* #1 -- ``tools.automations._extract_services`` special-cased the literal
  dict keys ``"service"``/``"action"``: when the value was a plain
  "domain.service" string it was recorded, but for ANY other shape (a list
  or a dict) the function did nothing -- crucially, it did NOT recurse into
  that value. This is fatal for the most common automation shape: the
  legacy top-level key is spelled ``action:`` (singular) and holds a LIST
  OF STEP DICTS, not a service string:

      action:
        - service: light.turn_on
          entity_id: light.kitchen

  Because the top-level key is literally ``"action"``, the function's own
  ``k in ("service", "action")`` branch matched it, saw a ``list`` (not a
  ``str``), and silently swallowed the entire steps list -- so
  ``automations_validate_automation_references`` almost never found any
  service reference to check for a classic automation using the classic
  ``action:`` key (see ``tests/test_findings_round2.py``, which had to use
  ``actions:`` instead of ``action:`` specifically to dodge this bug).

  The fix: recurse into the value whenever it is not a literal
  "domain.service" string, regardless of the key name, so both the legacy
  shape (``action:``/``condition:`` holding a list of step dicts, each
  step's own call spelled ``service:``) and the current shape (``actions:``
  holding step dicts, each step's call spelled ``action:`` as a string) are
  covered, including arbitrarily nested ``choose``/``if``/``then``/``else``/
  ``sequence``/``parallel``/``repeat`` blocks.

``tools.automations._extract_entity_ids`` was checked for the same shape of
bug (its own ``k == "entity_id"`` branch also has an implicit "else: do
nothing" for non-str/non-list values) and does NOT have it: unlike
``action``/``service``, the literal key ``"entity_id"`` never denotes a
container of further steps in the HA automation schema -- it is always a
terminal string or list of strings inside a trigger/condition/target dict,
and every non-``entity_id`` key already recurses unconditionally. The tests
below include a regression case proving nested extraction already works.
"""
from tools import automations as automations_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions -- get the plain callable back."""
    return getattr(tool, "fn", tool)


_extract_services = automations_tools._extract_services
_extract_entity_ids = automations_tools._extract_entity_ids


# ---------------------------------------------------------------------------
# _extract_services -- legacy `action:` (singular) holding a list of steps
# ---------------------------------------------------------------------------

def test_extract_services_legacy_action_list_of_steps():
    """RED (pre-fix): the classic shape -- top-level 'action:' is a LIST of
    step dicts, each using 'service:' -- must yield every service."""
    automation = {
        "alias": "Legacy automation",
        "trigger": [{"platform": "state", "entity_id": "light.kitchen"}],
        "action": [
            {"service": "light.turn_on", "entity_id": "light.kitchen"},
            {"service": "notify.mobile_app", "data": {"message": "hi"}},
        ],
    }
    found: set = set()
    _extract_services(automation, found)
    assert found == {"light.turn_on", "notify.mobile_app"}


def test_extract_services_new_syntax_actions_list_with_action_string():
    """Current syntax -- top-level 'actions:' holding steps whose own call
    is spelled 'action:' as a plain string -- must also work."""
    automation = {
        "alias": "New-style automation",
        "triggers": [{"trigger": "state", "entity_id": "light.kitchen"}],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}},
            {"action": "script.goodnight"},
        ],
    }
    found: set = set()
    _extract_services(automation, found)
    assert found == {"light.turn_on", "script.goodnight"}


def test_extract_services_nested_choose_if_sequence_parallel_repeat_mixed_keys():
    """Deeply nested choose/if/then/else/sequence/parallel/repeat blocks,
    mixing the legacy 'service:' key and the new 'action:' string key,
    must all be collected."""
    automation = {
        "alias": "Nested",
        "action": [
            {
                "choose": [
                    {
                        "conditions": [{"condition": "state", "entity_id": "sensor.x", "state": "on"}],
                        "sequence": [{"service": "light.turn_on"}],
                    }
                ],
                "default": [{"action": "light.turn_off"}],
            },
            {
                "if": [{"condition": "state", "entity_id": "sensor.y", "state": "on"}],
                "then": [{"service": "cover.open_cover"}],
                "else": [{"action": "cover.close_cover"}],
            },
            {
                "parallel": [
                    {"service": "switch.turn_on"},
                    {"sequence": [{"action": "fan.turn_on"}]},
                ]
            },
            {
                "repeat": {
                    "count": 3,
                    "sequence": [{"service": "light.toggle"}],
                }
            },
        ],
    }
    found: set = set()
    _extract_services(automation, found)
    assert found == {
        "light.turn_on",
        "light.turn_off",
        "cover.open_cover",
        "cover.close_cover",
        "switch.turn_on",
        "fan.turn_on",
        "light.toggle",
    }


def test_extract_services_skips_templates_and_non_dotted_strings():
    automation = {
        "action": [
            {"service": "{{ 'light.turn_on' }}"},
            {"action": "not_a_service"},
            {"service": "light.turn_on"},
        ],
    }
    found: set = set()
    _extract_services(automation, found)
    assert found == {"light.turn_on"}


def test_extract_services_empty_and_none_values_do_not_crash():
    automation = {"alias": "Empty", "action": None, "trigger": []}
    found: set = set()
    _extract_services(automation, found)
    assert found == set()


# ---------------------------------------------------------------------------
# _extract_entity_ids -- checked for the same shape of bug (see module
# docstring); this is a regression guard proving it already recurses
# correctly through nested action/choose/repeat blocks.
# ---------------------------------------------------------------------------

def test_extract_entity_ids_recurses_through_nested_target_and_repeat():
    automation = {
        "alias": "Nested entity_id",
        "trigger": [{"platform": "state", "entity_id": ["light.a", "light.b"]}],
        "action": [
            {
                "repeat": {
                    "sequence": [
                        {
                            "service": "light.toggle",
                            "target": {"entity_id": "light.c"},
                        }
                    ]
                }
            },
            {
                "choose": [
                    {
                        "conditions": [{"condition": "state", "entity_id": "sensor.x", "state": "on"}],
                        "sequence": [{"service": "light.turn_on", "entity_id": ["light.d"]}],
                    }
                ]
            },
        ],
    }
    found: set = set()
    _extract_entity_ids(automation, found)
    assert found == {"light.a", "light.b", "light.c", "sensor.x", "light.d"}


def test_extract_entity_ids_skips_templates():
    automation = {"action": [{"service": "light.turn_on", "entity_id": "{{ target }}"}]}
    found: set = set()
    _extract_entity_ids(automation, found)
    assert found == set()


# ---------------------------------------------------------------------------
# Integration: automations_validate_automation_references with the legacy
# `action:` (singular) shape must actually detect a missing service instead
# of silently reporting "nothing missing" because service_refs stayed empty.
# ---------------------------------------------------------------------------

def test_validate_automation_references_detects_missing_service_in_legacy_action_list(monkeypatch):
    import ha_client as ha

    monkeypatch.setattr(ha, "get_states", lambda: [{"entity_id": "light.kitchen"}])
    monkeypatch.setattr(ha, "list_services", lambda: [{"domain": "light", "services": {"turn_on": {}}}])

    yaml_content = """
alias: Legacy automation
trigger:
  - platform: state
    entity_id: light.kitchen
action:
  - service: notify.mobile_app
    data:
      message: hi
"""
    result = _unwrap(automations_tools.validate_automation_references)(yaml_content)

    assert result["services_checked"] == 1, (
        f"expected the legacy 'action:' step list to be walked and yield one "
        f"service reference, got: {result!r}"
    )
    assert result["missing_services"] == ["notify.mobile_app"]
    assert result["services_check"] == "ok"
