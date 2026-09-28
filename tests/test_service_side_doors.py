"""ADR-0006 D6: the two identified "side doors" around service-name
validation (F5), plus a static AST guard against a future one going
unreviewed.

- `areas_control_area`'s `action` used to be passed through unchecked into
  `<domain>.<action>` — an MCP client could reach any service on `domain`
  under cover of "controlling an area".
- `services_send_notification`'s `target`, when it contained a dot, became
  the `domain.service` pair verbatim — reachable as an arbitrary service
  call under cover of "sending a notification".

The AST test below is the broader defense: every `ha.call_service(...)`
call site anywhere in `tools/` whose `domain`/`service` argument is not a
string literal must be in the explicit allowlist declared here, so a new one
can't be added without a reviewer noticing and deciding whether it needs the
same kind of validation these two got.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ha_client as ha
import self_protection
from tools import areas as areas_tools
from tools import services as services_tools

control_area = getattr(areas_tools.control_area, "fn", areas_tools.control_area)
send_notification = getattr(services_tools.send_notification, "fn", services_tools.send_notification)

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


@pytest.fixture(autouse=True)
def _reset_self_protection():
    self_protection.reset_for_tests()
    yield
    self_protection.reset_for_tests()


# ---------------------------------------------------------------------------
# areas_control_area — action must be turn_on/turn_off/toggle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["turn_on", "turn_off", "toggle"])
def test_control_area_allows_the_three_valid_actions(monkeypatch, action):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [{"ok": True}])

    result = control_area(area_id="living_room", action=action, domain="light")

    assert result == [{"ok": True}]
    assert calls == [("light", action, {"area_id": "living_room"})]


@pytest.mark.parametrize("action", ["install", "restart", "delete", "addon_stop", "turn_on; rm -rf"])
def test_control_area_rejects_any_other_action_without_io(monkeypatch, action):
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    with pytest.raises(ValueError):
        control_area(area_id="living_room", action=action, domain="update")


# ---------------------------------------------------------------------------
# services_send_notification — a dotted target must name the notify domain
# ---------------------------------------------------------------------------


def test_send_notification_bare_target_uses_notify_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [])

    result = send_notification(message="hi", target="mobile_app_phone")

    assert result == []
    assert calls == [("notify", "mobile_app_phone", {"message": "hi"})]


def test_send_notification_notify_dotted_target_is_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [])

    result = send_notification(message="hi", target="notify.mobile_app_phone")

    assert result == []
    assert calls == [("notify", "mobile_app_phone", {"message": "hi"})]


def test_send_notification_omitted_target_defaults_to_plain_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(ha, "call_service", lambda d, s, data: calls.append((d, s, data)) or [])

    send_notification(message="hi")

    assert calls == [("notify", "notify", {"message": "hi"})]


def test_send_notification_rejects_non_notify_dotted_target_without_io(monkeypatch):
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    result = send_notification(message="hi", target="hassio.addon_stop")

    assert result["error"] == "invalid_notify_target"
    assert "notify" in result["message"]


def test_send_notification_rejects_guarded_service_disguised_as_target(monkeypatch):
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    result = send_notification(message="hi", target="homeassistant.restart")

    assert result["error"] == "invalid_notify_target"


def test_send_notification_rejects_multi_dot_notify_target_as_invalid_service_name(monkeypatch):
    """ADR-0006 follow-up (Security review, 2026-09-28, SHOULD): a target
    with more than one dot (e.g. 'notify.a.b') names the 'notify' domain but
    an invalid service name ('a.b') -- must come back as a clean
    `{"error": "invalid_service_name"}`, not an uncaught `ValueError`
    escaping from `ha_client.call_service`'s own D3 validation."""
    monkeypatch.setattr(ha, "call_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HA call")))

    result = send_notification(message="hi", target="notify.a.b")

    assert result == {"error": "invalid_service_name"}


# ---------------------------------------------------------------------------
# Static AST guard: every non-literal ha.call_service(domain, service, ...)
# call site in tools/ must be on this explicit, reviewed allowlist.
# ---------------------------------------------------------------------------

# (relative filename under tools/, enclosing function name): why it's safe
# despite domain and/or service not being a string literal at the call site.
_ALLOWED_NON_LITERAL_SERVICE_CALLS: dict[tuple[str, str], str] = {
    ("entities.py", "turn_on"): "domain parsed from entity_id, service fixed 'turn_on'",
    ("entities.py", "turn_off"): "domain parsed from entity_id, service fixed 'turn_off'",
    ("entities.py", "toggle"): "domain parsed from entity_id, service fixed 'toggle'",
    ("entities.py", "bulk_control"): (
        "domain parsed from each entity_id, action validated to turn_on/turn_off/toggle"
    ),
    ("helpers.py", "set_input_boolean"): (
        "domain fixed 'input_boolean', service picked from a 2-way literal ternary"
    ),
    ("helpers.py", "reload_helpers"): (
        "domain iterates a fixed literal list of helper domains, service fixed 'reload'"
    ),
    ("services.py", "call_service"): (
        "the generic dispatcher itself -- domain/service are its own documented, "
        "now name-validated and confirm-gated parameters (ADR-0006 D3/D4)"
    ),
    ("services.py", "reload_config"): "domain is the tool's own parameter, service fixed 'reload'",
    ("services.py", "send_notification"): (
        "a dotted target's domain is validated to 'notify' before use (ADR-0006 D6)"
    ),
    ("areas.py", "control_area"): (
        "domain is the tool's own parameter, action validated to turn_on/turn_off/toggle (ADR-0006 D6)"
    ),
}


def _is_str_literal(node: ast.expr | None) -> bool:
    return node is not None and isinstance(node, ast.Constant) and isinstance(node.value, str)


def _service_call_args(call: ast.Call) -> tuple[ast.expr | None, ast.expr | None]:
    """(domain_node, service_node) for a `*.call_service(...)` call, from
    positional args first, falling back to `domain=`/`service=` keywords."""
    domain_node = call.args[0] if len(call.args) > 0 else None
    service_node = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "domain" and domain_node is None:
            domain_node = kw.value
        if kw.arg == "service" and service_node is None:
            service_node = kw.value
    return domain_node, service_node


class _ServiceCallVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self._func_stack: list[str] = []
        self.violations: list[str] = []

    def _visit_func(self, node):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "call_service":
            domain_node, service_node = _service_call_args(node)
            if not (_is_str_literal(domain_node) and _is_str_literal(service_node)):
                func_name = self._func_stack[-1] if self._func_stack else "<module>"
                key = (self.filename, func_name)
                if key not in _ALLOWED_NON_LITERAL_SERVICE_CALLS:
                    self.violations.append(
                        f"{self.filename}:{node.lineno} in {func_name}(): "
                        "non-literal domain/service passed to call_service(), "
                        "and not in _ALLOWED_NON_LITERAL_SERVICE_CALLS"
                    )
        self.generic_visit(node)


def test_every_non_literal_call_service_site_is_on_the_reviewed_allowlist():
    violations: list[str] = []
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _ServiceCallVisitor(path.name)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    assert not violations, "\n".join(violations)


def test_allowlist_has_no_stale_entries():
    """Every allowlist entry must correspond to an actual non-literal
    call_service() call site still present in the named function, so the
    allowlist can't silently grow stale (a fixed/removed call site staying
    listed forever)."""
    found: set[tuple[str, str]] = set()
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _ServiceCallVisitor(path.name)
        visitor.visit(tree)

        class _Collector(ast.NodeVisitor):
            def __init__(self):
                self._stack: list[str] = []

            def _visit_func(self, node):
                self._stack.append(node.name)
                self.generic_visit(node)
                self._stack.pop()

            visit_FunctionDef = _visit_func
            visit_AsyncFunctionDef = _visit_func

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Attribute) and node.func.attr == "call_service":
                    domain_node, service_node = _service_call_args(node)
                    if not (_is_str_literal(domain_node) and _is_str_literal(service_node)):
                        func_name = self._stack[-1] if self._stack else "<module>"
                        found.add((path.name, func_name))
                self.generic_visit(node)

        _Collector().visit(tree)

    stale = set(_ALLOWED_NON_LITERAL_SERVICE_CALLS) - found
    assert not stale, f"allowlist entries with no matching call site: {sorted(stale)}"
