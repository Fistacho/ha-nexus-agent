"""`ha_client.call_service` — ADR-0006 D3 defense in depth.

Every call site in `tools/` already passes a hardcoded literal or a name
validated one layer up (`service_guard.validate_service_name`), so this is
the last line of defense: a malformed `domain`/`service` must never reach
`httpx`'s own URL-joining (`?`/`#`/`..` semantics that let F2/F3 through
before this change) at all.
"""
from __future__ import annotations

import pytest

import ha_client as ha


class _BoomClient:
    def __enter__(self):
        raise AssertionError("no HTTP client should be constructed for an invalid service name")

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "domain,service",
    [
        ("light", "turn_on?x"),
        ("../hassio", "addon_stop"),
        ("hassio", "../addon_stop"),
        ("light", "turn.on"),
        ("light", "turn on"),
        ("light", ""),
        ("", "turn_on"),
        ("light", "turn_on#frag"),
    ],
)
def test_call_service_rejects_malformed_names_without_io(monkeypatch, domain, service):
    monkeypatch.setattr(ha, "_client", _BoomClient)
    with pytest.raises(ValueError):
        ha.call_service(domain, service, {})


def test_call_service_accepts_well_formed_names(monkeypatch):
    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"ok": True}]

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json):
            calls.append((path, json))
            return _FakeResponse()

    monkeypatch.setattr(ha, "_client", _FakeClient)

    result = ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"})

    assert result == [{"ok": True}]
    assert calls == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"})]
