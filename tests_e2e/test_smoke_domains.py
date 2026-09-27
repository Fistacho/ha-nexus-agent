"""End-to-end smoke tests: a handful of read-only tool calls against a *real*,
freshly onboarded Home Assistant container (see `scripts/e2e_onboard_ha.py`,
`.github/workflows/e2e.yml`, and `tests_e2e/conftest.py`).

This complements `tests/` (which mocks every HTTP/WS call) by proving the request
shapes nexus's tools build actually match what a real, current Home Assistant
accepts and returns — the class of bug a mock can't catch by construction. Kept
deliberately small: a few representative, read-only ("safe on prod") domains, not
a full sweep of ~320 tools.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from tools import areas as areas_tools
from tools import entities as entities_tools
from tools import system as system_tools

_ROOT = Path(__file__).resolve().parents[1]


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back.

    (Same helper as tests/test_audit_websocket_system.py — duplicated rather than
    imported because tests_e2e/ must not depend on anything under tests/, to keep
    the two suites' isolation and collection independent of each other.)
    """
    return getattr(tool, "fn", tool)


def test_testpaths_excludes_e2e_dir():
    """Guard the isolation this whole suite depends on: a bare `pytest` from the
    repo root must not also collect tests_e2e/ (it needs a live HA instance and
    would otherwise fail/hang in every normal CI/local run of `tests/`)."""
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths == ["tests"], testpaths


def test_ping_ha_reaches_the_real_instance():
    result = _unwrap(system_tools.ping_ha)()
    assert result["reachable"] is True


def test_check_config_returns_a_verdict():
    result = _unwrap(system_tools.check_config)()
    assert result.get("result") in ("valid", "invalid"), result


def test_list_areas_returns_a_list():
    result = _unwrap(areas_tools.list_areas)()
    assert isinstance(result, list)


def test_list_entities_returns_the_default_areas_and_fields():
    """A freshly onboarded instance has no user entities yet, but onboarding's
    UserOnboardingView does create a handful of default areas/entities (e.g. the
    person entity for the admin user) — so `sun.sun` at minimum should exist."""
    result = _unwrap(entities_tools.list_entities)()
    assert isinstance(result, list)
    entity_ids = {row["entity_id"] for row in result}
    assert "sun.sun" in entity_ids, entity_ids
    assert all({"entity_id", "state", "friendly_name"} <= row.keys() for row in result)
