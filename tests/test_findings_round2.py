"""Regression tests for round-2 findings after the 0.21.0 contract migration
(``.claude/docs/nexus/findings-0.21.md`` items #4, #5, #6).

* #4 -- ``automations_validate_automation_references``: when
  ``ha.list_services()`` (or ``ha.get_states()``) raises, the old code
  swallowed the exception and fell back to an empty ``missing_services``
  (or, for entities, to reporting every literal reference as missing), so a
  fetch failure was indistinguishable from "nothing missing". The fix adds
  explicit ``services_check``/``entities_check`` flags plus an ``errors``
  list so a false negative (or false "everything is missing") is
  impossible.
* #5 -- ``automations_set_group``: ``entities`` and
  ``add_entities``/``remove_entities`` are documented as mutually exclusive
  but the old code let both through to ``group.set`` silently. The fix
  validates this up front and never calls the service when both are given.
* #6 -- ``entities_bulk_set_state``: a bare ``except Exception: str(e)`` per
  item made a validation mistake (missing entity_id/state) indistinguishable
  from an HA/network failure. The fix tags each failed item with an
  ``error_type`` of ``"validation"``, ``"http"`` or ``"unexpected"``.
"""
import httpx

import ha_client as ha
from tools import automations as automations_tools
from tools import entities as entities_tools


def _unwrap(tool):
    """FastMCP wraps decorated functions -- get the plain callable back."""
    return getattr(tool, "fn", tool)


# ---------------------------------------------------------------------------
# #4 -- automations_validate_automation_references
# ---------------------------------------------------------------------------

_YAML_ONE_AUTOMATION = """
alias: Test automation
trigger:
  - platform: state
    entity_id: light.kitchen
actions:
  - service: light.turn_on
    entity_id: light.kitchen
"""
# NB: top-level key is 'actions:' (not 'action:') so that
# tools.automations._extract_services actually recurses into the step list.
# _extract_services special-cases a dict key literally named "action" (or
# "service") and never recurses into its value when it isn't a string — a
# separate, pre-existing extraction bug (out of scope here, see the final
# report) that would otherwise make service_refs empty for every automation
# using the common top-level 'action:' key and mask the behaviour under test.


def test_services_fetch_failure_is_flagged_not_silently_empty(monkeypatch):
    """RED (pre-fix): ha.list_services() raising must not make it look like
    every referenced service exists (missing_services == [] with no other
    signal is indistinguishable from "checked, none missing")."""
    monkeypatch.setattr(ha, "get_states", lambda: [{"entity_id": "light.kitchen"}])

    def boom():
        raise RuntimeError("HA unreachable")

    monkeypatch.setattr(ha, "list_services", boom)

    result = _unwrap(automations_tools.validate_automation_references)(_YAML_ONE_AUTOMATION)

    assert result["missing_services"] == []
    # The old code stopped here -- nothing told the caller the check never
    # actually ran. The fix must expose that explicitly.
    assert result.get("services_check") == "unavailable", (
        "services_check must say 'unavailable' when ha.list_services() raised, "
        f"got: {result!r}"
    )
    assert any("service" in e.lower() for e in result.get("errors", [])), (
        f"errors must mention the failed service fetch, got: {result.get('errors')!r}"
    )
    # Entities were fetched fine, that check must still say so.
    assert result.get("entities_check") == "ok"


def test_entities_fetch_failure_is_flagged_not_reported_as_all_missing(monkeypatch):
    """RED (pre-fix): ha.get_states() raising made live_entity_ids == set(),
    so every literal entity_id in the YAML was reported as 'missing' -- a
    false positive that looks like real data instead of a fetch failure."""
    def boom():
        raise RuntimeError("HA unreachable")

    monkeypatch.setattr(ha, "get_states", boom)
    monkeypatch.setattr(ha, "list_services", lambda: [{"domain": "light", "services": {"turn_on": {}}}])

    result = _unwrap(automations_tools.validate_automation_references)(_YAML_ONE_AUTOMATION)

    assert result.get("entities_check") == "unavailable", (
        "entities_check must say 'unavailable' when ha.get_states() raised, "
        f"got: {result!r}"
    )
    # Must NOT silently report light.kitchen as missing -- that's the old,
    # misleading behaviour (false report dressed up as real data).
    assert result["missing_entities"] == []
    assert any("entit" in e.lower() for e in result.get("errors", []))
    assert result.get("services_check") == "ok"


def test_both_checks_ok_when_both_fetches_succeed(monkeypatch):
    """Regression guard: the happy path keeps reporting real missing refs
    and both checks say 'ok', with no spurious errors entry."""
    monkeypatch.setattr(ha, "get_states", lambda: [{"entity_id": "light.kitchen"}])
    monkeypatch.setattr(ha, "list_services", lambda: [{"domain": "light", "services": {"turn_on": {}}}])

    result = _unwrap(automations_tools.validate_automation_references)(_YAML_ONE_AUTOMATION)

    assert result["missing_entities"] == []
    assert result["missing_services"] == []
    assert result.get("services_check") == "ok"
    assert result.get("entities_check") == "ok"
    assert result.get("errors", []) == []


def test_missing_service_still_detected_when_fetch_succeeds(monkeypatch):
    """Regression guard: a genuinely missing service is still reported when
    the service list was fetched successfully (not confused with 'unavailable')."""
    monkeypatch.setattr(ha, "get_states", lambda: [{"entity_id": "light.kitchen"}])
    monkeypatch.setattr(ha, "list_services", lambda: [{"domain": "switch", "services": {"turn_on": {}}}])

    result = _unwrap(automations_tools.validate_automation_references)(_YAML_ONE_AUTOMATION)

    assert result["missing_services"] == ["light.turn_on"]
    assert result.get("services_check") == "ok"


# ---------------------------------------------------------------------------
# #5 -- automations_set_group
# ---------------------------------------------------------------------------


def test_set_group_rejects_entities_with_add_entities(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("group.set must not be called when entities/add_entities conflict")

    monkeypatch.setattr(ha, "call_service", explode)

    result = _unwrap(automations_tools.set_group)(
        group_id="living_room",
        entities=["light.a"],
        add_entities=["light.b"],
    )

    assert "error" in result


def test_set_group_rejects_entities_with_remove_entities(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("group.set must not be called when entities/remove_entities conflict")

    monkeypatch.setattr(ha, "call_service", explode)

    result = _unwrap(automations_tools.set_group)(
        group_id="living_room",
        entities=["light.a"],
        remove_entities=["light.b"],
    )

    assert "error" in result


def test_set_group_allows_entities_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: calls.append((a, k)))

    result = _unwrap(automations_tools.set_group)(group_id="living_room", entities=["light.a"])

    assert result == {"status": "set", "group_id": "group.living_room"}
    assert len(calls) == 1


def test_set_group_allows_add_and_remove_together(monkeypatch):
    """add_entities/remove_entities without entities is the documented,
    still-supported combination -- must keep working."""
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: calls.append((a, k)))

    result = _unwrap(automations_tools.set_group)(
        group_id="living_room", add_entities=["light.a"], remove_entities=["light.b"]
    )

    assert result == {"status": "set", "group_id": "group.living_room"}
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# #6 -- entities_bulk_set_state
# ---------------------------------------------------------------------------


def test_bulk_set_state_validation_error_is_tagged(monkeypatch):
    monkeypatch.setattr(ha, "set_state", lambda *a, **k: {"ok": True})

    result = _unwrap(entities_tools.bulk_set_state)([{"state": "on"}])

    assert result[0]["ok"] is False
    assert result[0].get("error_type") == "validation"


def test_bulk_set_state_http_error_is_tagged(monkeypatch):
    def boom(*a, **k):
        request = httpx.Request("POST", "http://ha.test:8123/api/states/light.a")
        response = httpx.Response(502, request=request)
        raise httpx.HTTPStatusError("bad gateway", request=request, response=response)

    monkeypatch.setattr(ha, "set_state", boom)

    result = _unwrap(entities_tools.bulk_set_state)([{"entity_id": "light.a", "state": "on"}])

    assert result[0]["ok"] is False
    assert result[0].get("error_type") == "http", f"expected 'http', got: {result[0]!r}"


def test_bulk_set_state_unexpected_error_is_tagged(monkeypatch):
    def boom(*a, **k):
        raise ValueError("something else entirely")

    monkeypatch.setattr(ha, "set_state", boom)

    result = _unwrap(entities_tools.bulk_set_state)([{"entity_id": "light.a", "state": "on"}])

    assert result[0]["ok"] is False
    assert result[0].get("error_type") == "unexpected"


def test_bulk_set_state_success_has_no_error_type(monkeypatch):
    monkeypatch.setattr(ha, "set_state", lambda *a, **k: {"ok": True})

    result = _unwrap(entities_tools.bulk_set_state)([{"entity_id": "light.a", "state": "on"}])

    assert result[0] == {"entity_id": "light.a", "ok": True}
