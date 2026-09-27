"""Regression test for GitHub issue #3.

`dashboards_screenshot` used to unconditionally prepend `lovelace/` to
`url_path` before asking the Puppet engine for a screenshot. That's wrong for
any custom (non-default) dashboard: Home Assistant serves a registered
dashboard at its own top-level route (`/<dashboard_url_path>/<view>`), and
`/lovelace/...` only ever resolves to views of the *default* dashboard. The
old code silently mangled `dashbord-parter/testy-parter` into
`lovelace/dashbord-parter/testy-parter`, which HA falls back to the default
"Overview" dashboard for — both calls in the issue returned a screenshot of
the wrong page with HTTP 200, no error at all.

The fix resolves the first path segment against the registered dashboards
(`lovelace/dashboards/list`, already exposed by `list_dashboards`):

- first segment matches a registered dashboard's `url_path` -> use as-is
- path already starts with `lovelace` -> use as-is
- otherwise -> treat as a view of the default dashboard, prepend `lovelace/`
  (pre-existing behaviour, e.g. `caly-dom` -> `lovelace/caly-dom`)
- dashboard list unavailable (WS error) -> fail loudly with
  `{"error": "dashboard_list_unavailable"}` instead of silently prepending
"""
import copy

import pytest

import ha_client as ha
from tools import dashboards as dash


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


class _FakeResponse:
    def __init__(self, content: bytes = b"PNGDATA", status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.text = ""


class _FakeHttpxClient:
    """Stand-in for httpx.Client — records the URL it was asked to GET."""

    last_url = None
    last_params = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        _FakeHttpxClient.last_url = url
        _FakeHttpxClient.last_params = params
        return _FakeResponse()


@pytest.fixture
def engine(monkeypatch):
    """Bypass Supervisor discovery and fake the Puppet engine HTTP call."""
    monkeypatch.setenv("NEXUS_SCREENSHOT_ENGINE_URL", "http://engine.test:10000")
    monkeypatch.setattr(dash.httpx, "Client", _FakeHttpxClient)
    _FakeHttpxClient.last_url = None
    return _FakeHttpxClient


@pytest.fixture
def registered_dashboards(monkeypatch):
    """Fake `lovelace/dashboards/list` — one custom dashboard registered."""
    dashboards = [
        {"id": "abc123", "url_path": "dashbord-parter", "title": "Parter", "mode": "storage"},
    ]

    def fake_ws_call(msg_type, **kwargs):
        if msg_type == "lovelace/dashboards/list":
            return copy.deepcopy(dashboards)
        raise AssertionError(f"unexpected WS command: {msg_type}")

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)
    return dashboards


def test_registered_dashboard_path_is_used_without_lovelace_prefix(engine, registered_dashboards):
    result = _unwrap(dash.screenshot)(url_path="dashbord-parter/testy-parter")

    assert result["url_path"] == "dashbord-parter/testy-parter"
    assert engine.last_url == "http://engine.test:10000/dashbord-parter/testy-parter"


def test_registered_dashboard_root_path_is_used_without_lovelace_prefix(engine, registered_dashboards):
    result = _unwrap(dash.screenshot)(url_path="dashbord-parter")

    assert result["url_path"] == "dashbord-parter"


def test_path_already_prefixed_with_lovelace_is_untouched(engine, registered_dashboards):
    result = _unwrap(dash.screenshot)(url_path="lovelace/0")

    assert result["url_path"] == "lovelace/0"


def test_unregistered_path_is_treated_as_default_dashboard_view(engine, registered_dashboards):
    """'caly-dom' is not a registered dashboard -> pre-existing behaviour."""
    result = _unwrap(dash.screenshot)(url_path="caly-dom")

    assert result["url_path"] == "lovelace/caly-dom"


def test_missing_url_path_defaults_to_lovelace_root(engine, registered_dashboards):
    result = _unwrap(dash.screenshot)()

    assert result["url_path"] == "lovelace"


def test_dashboard_list_unavailable_fails_loudly_instead_of_guessing(engine, monkeypatch):
    def failing_ws_call(msg_type, **kwargs):
        raise RuntimeError("websocket connection closed")

    monkeypatch.setattr(ha, "_ws_call", failing_ws_call)

    result = _unwrap(dash.screenshot)(url_path="dashbord-parter")

    assert result["error"] == "dashboard_list_unavailable"
    assert engine.last_url is None  # never reached the engine
