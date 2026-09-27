"""Regression tests for confirmed findings in the 2026-09-27 prompt audit
(C:\\GIT\\ha\\.claude\\docs\\audits\\2026-09-27-prompt-audit\\REPORT.md).

Covers:
* (e) tools/files.py `_safe_path` — prefix-string containment bypass.
* F25  tools/card_builder.py `upload_media_from_path` — arbitrary local file read.
* F25  tools/card_builder.py `upload_image_from_url` — scheme/content-type/size bypass.
* F39  tools/statistics.py `get_energy_statistics` — 'W' (power) treated as an energy unit.

Security panel review (2026-09-27, second pass):
* BLOCKER tools/card_builder.py `upload_svg` / `upload_media_from_path` /
  `upload_image_from_url` / `upload_media` — stored XSS via unsanitized SVG
  written under www/card_builder/ (served unauthenticated at /local/).
* tools/files.py `list_config_files` — `subdirectory` boundary bypass.
* tools/git_ops.py `git_rollback_file` — path boundary bypass (defense in depth).

ADR-0004 S4 (2026-09-27, partition D): with `host_network: true`, a request
from this add-on to loopback/link-local has the same peer identity as HA
Core/Supervisor towards some ingress-fronted add-ons (e.g. the ESPHome
Device Builder trusts `127.0.0.1` without authentication) — so
`upload_image_from_url` must refuse to become an SSRF proxy into the host's
own network, on the initial URL and on every redirect hop.
"""
from __future__ import annotations

import base64
import socket

import httpx
import pytest

import ha_client as ha
from tools import card_builder, files, git_ops, statistics


def _unwrap(tool):
    """FastMCP wraps decorated functions — get the plain callable back."""
    return getattr(tool, "fn", tool)


# --- (e) tools/files.py: path containment bypass ----------------------------

def test_safe_path_rejects_sibling_directory_with_shared_prefix(tmp_path, monkeypatch):
    """`_safe_path` used `str(path).startswith(str(config_root))`, which also matches
    a sibling directory that merely starts with the same characters
    (e.g. '.../config' vs '.../config_old'). A relative_path that resolves outside
    /config via such a sibling must be rejected.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    sibling_dir = tmp_path / "config_old"
    sibling_dir.mkdir()
    (sibling_dir / "leak.yaml").write_text("leaked: true", encoding="utf-8")

    monkeypatch.setattr(files, "_CONFIG_PATH", config_dir)

    with pytest.raises(PermissionError):
        files._safe_path("../config_old/leak.yaml")


def test_safe_path_still_allows_files_inside_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(files, "_CONFIG_PATH", config_dir)

    path = files._safe_path("automations.yaml")

    assert path == (config_dir / "automations.yaml").resolve()


# --- F25 tools/card_builder.py: upload_media_from_path ----------------------

def test_upload_media_from_path_rejects_file_outside_media_roots(tmp_path, monkeypatch):
    """Must not read arbitrary files reachable by the process, e.g. secrets.yaml."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    secret = config_dir / "secrets.yaml"
    secret.write_text("ha_token: super-secret", encoding="utf-8")

    monkeypatch.setenv("HA_CONFIG_PATH", str(config_dir))

    def explode(*a, **k):
        raise AssertionError("must not upload a file outside www/media")

    monkeypatch.setattr(ha, "_ws_call", explode)

    result = _unwrap(card_builder.upload_media_from_path)(str(secret))

    assert "error" in result


def test_upload_media_from_path_rejects_disallowed_extension_even_inside_media_root(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    www_dir = config_dir / "www"
    www_dir.mkdir(parents=True)
    script = www_dir / "payload.py"
    script.write_text("print('nope')", encoding="utf-8")

    monkeypatch.setenv("HA_CONFIG_PATH", str(config_dir))

    def explode(*a, **k):
        raise AssertionError("must not upload a non-media file")

    monkeypatch.setattr(ha, "_ws_call", explode)

    result = _unwrap(card_builder.upload_media_from_path)(str(script))

    assert "error" in result


def test_upload_media_from_path_accepts_file_inside_www(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    www_dir = config_dir / "www"
    www_dir.mkdir(parents=True)
    image = www_dir / "background.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    monkeypatch.setenv("HA_CONFIG_PATH", str(config_dir))

    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["msg_type"] = msg_type
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/background.png"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_media_from_path)(str(image))

    assert "error" not in result
    assert calls["kwargs"]["filename"] == "background.png"


# --- F25 tools/card_builder.py: upload_image_from_url -----------------------
#
# ADR-0004 S4 SSRF guard: the download path now goes through httpx (manual
# redirect handling + per-hop DNS validation), not urllib. `_FakeHttpxClient`
# stands in for `httpx.Client()` the same way `FakeClient` does in
# `tests/test_audit_discover_esphome_blueprints.py` — a context manager
# recording every `.stream()` call and replaying queued responses in order.


class _FakeStreamResponse:
    """Stand-in for the `httpx.Response` yielded by `Client.stream(...)`."""

    def __init__(self, status_code: int, headers: dict, chunks: list[bytes]):
        self.status_code = status_code
        # Real httpx.Response headers are case-insensitive (e.g. `Location`
        # must be readable via `.get("location")`) — mirror that here so the
        # fake doesn't mask a lookup bug the real thing would tolerate.
        self.headers = httpx.Headers(headers)
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield from self._chunks


class _FakeHttpxClient:
    """Stand-in for `httpx.Client()` — records every `.stream()` call
    (method, resolved URL, headers, extensions) and returns the queued
    `_FakeStreamResponse`s in order, one per call.
    """

    def __init__(self, responses: list[_FakeStreamResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, *, headers=None, extensions=None, follow_redirects=False):
        assert follow_redirects is False, "redirects must be handled manually so each hop is re-validated"
        self.calls.append({
            "method": method,
            "url": str(url),
            "headers": dict(headers or {}),
            "extensions": extensions,
        })
        if not self._responses:
            raise AssertionError("no more fake responses queued — unexpected extra request")
        return self._responses.pop(0)


def _install_fake_client(monkeypatch, responses: list[_FakeStreamResponse]) -> _FakeHttpxClient:
    fake_client = _FakeHttpxClient(responses)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake_client)
    return fake_client


def _no_http_client(*_a, **_k):
    raise AssertionError("must not open any HTTP connection for a rejected target")


def _fake_getaddrinfo(mapping: dict[str, str]):
    """`socket.getaddrinfo` stand-in for hostnames that need a mocked public
    IP (no real DNS lookup in tests). IP literals and 'localhost' resolve
    for real — `getaddrinfo` on those is local-only, no network involved,
    so using the real resolver there is the more faithful test.
    """

    def fake(host, *args, **kwargs):
        if host not in mapping:
            raise AssertionError(f"unexpected getaddrinfo({host!r}) — add it to the test's mapping")
        ip = mapping[host]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    return fake


def test_upload_image_from_url_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _no_http_client)
    monkeypatch.setattr(ha, "_ws_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not upload")))

    result = _unwrap(card_builder.upload_image_from_url)("file:///etc/passwd")

    assert result["error"] == "scheme_not_allowed"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/x",
    ],
    ids=["ipv4-loopback", "localhost", "ipv6-loopback", "ipv4-mapped-ipv6-loopback", "link-local", "unspecified"],
)
def test_upload_image_from_url_rejects_internal_targets(url, monkeypatch):
    """127.0.0.1, localhost, ::1, ::ffff:127.0.0.1, 169.254.x and 0.0.0.0 must
    all be refused before any HTTP connection is attempted — this add-on's
    own loopback is a trusted peer for other ingress-fronted add-ons under
    `host_network: true` (ADR-0004 S4).
    """
    monkeypatch.setattr(httpx, "Client", _no_http_client)
    monkeypatch.setattr(ha, "_ws_call", _explode)

    result = _unwrap(card_builder.upload_image_from_url)(url)

    assert result["error"] == "target_not_allowed"


def test_upload_image_from_url_rejects_redirect_to_loopback(monkeypatch):
    """A public-looking URL that redirects to an internal address must be
    refused, and the internal target must never actually be connected to.
    """
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"public.example": "93.184.216.34", "127.0.0.1": "127.0.0.1"}),
    )
    fake_client = _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(302, {"Location": "http://127.0.0.1/secret"}, [])],
    )
    monkeypatch.setattr(ha, "_ws_call", _explode)

    result = _unwrap(card_builder.upload_image_from_url)("https://public.example/redirector")

    assert result["error"] == "target_not_allowed"
    # Exactly one request was made — to the public host. The internal
    # redirect target was validated and rejected before a second connection
    # was ever attempted.
    assert len(fake_client.calls) == 1
    assert "93.184.216.34" in fake_client.calls[0]["url"]


def test_upload_image_from_url_allows_public_url(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"example.com": "93.184.216.34"})
    )
    content = b"\x89PNG\r\n\x1a\nfake"
    fake_client = _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(200, {"Content-Type": "image/png"}, [content])],
    )

    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/photo.png"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_image_from_url)("https://example.com/photo.png")

    assert "error" not in result
    assert calls["kwargs"]["filename"] == "photo.png"
    assert fake_client.calls[0]["headers"]["Host"] == "example.com"
    assert fake_client.calls[0]["extensions"] == {"sni_hostname": "example.com"}


def test_upload_image_from_url_allows_lan_address(monkeypatch):
    """RFC1918 LAN addresses stay allowed — users legitimately fetch images
    from devices on their own network (a camera snapshot, a local NAS, ...).
    """
    content = b"\x89PNG\r\n\x1a\nfake"
    fake_client = _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(200, {"Content-Type": "image/png"}, [content])],
    )

    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/photo.png"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_image_from_url)("http://192.168.1.50/photo.png")

    assert "error" not in result
    assert calls["kwargs"]["filename"] == "photo.png"
    assert fake_client.calls[0]["url"].startswith("http://192.168.1.50")


def test_upload_image_from_url_rejects_non_image_content_type(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"example.com": "93.184.216.34"})
    )
    _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(200, {"Content-Type": "text/html"}, [b"<html>not an image</html>"])],
    )
    monkeypatch.setattr(ha, "_ws_call", _explode)

    result = _unwrap(card_builder.upload_image_from_url)("https://example.com/looks-like-image")

    assert result["error"] == "content_type_not_allowed"


def test_upload_image_from_url_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"example.com": "93.184.216.34"})
    )
    oversized = b"x" * (card_builder._MAX_URL_IMAGE_BYTES + 1)
    _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(200, {"Content-Type": "image/png"}, [oversized])],
    )
    monkeypatch.setattr(ha, "_ws_call", _explode)

    result = _unwrap(card_builder.upload_image_from_url)("https://example.com/huge.png")

    assert result["error"] == "file_too_large"


def test_upload_image_from_url_accepts_valid_image(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"example.com": "93.184.216.34"})
    )
    content = b"\x89PNG\r\n\x1a\nfake"
    _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(200, {"Content-Type": "image/png"}, [content])],
    )

    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/photo.png"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_image_from_url)("https://example.com/photo.png")

    assert "error" not in result
    assert calls["kwargs"]["filename"] == "photo.png"


# --- F39 tools/statistics.py: 'W' is power, not energy ----------------------

def test_get_energy_statistics_excludes_power_watts(monkeypatch):
    """'W' is instantaneous power, not an energy unit — it must not be swept into
    the energy-consumption aggregation (recorder sum statistics don't make sense
    for a power sensor)."""
    list_result = [
        {"statistic_id": "sensor.house_power", "has_sum": True, "unit_of_measurement": "W"},
        {"statistic_id": "sensor.house_energy", "has_sum": True, "unit_of_measurement": "kWh"},
    ]

    calls = []

    def fake_ws_call(msg_type, **kwargs):
        calls.append((msg_type, kwargs))
        if msg_type == "recorder/list_statistic_ids":
            return list_result
        return {}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    _unwrap(statistics.get_energy_statistics)(days=7)

    period_call = next(c for c in calls if c[0] == "recorder/statistics_during_period")
    assert "sensor.house_power" not in period_call[1]["statistic_ids"]
    assert "sensor.house_energy" in period_call[1]["statistic_ids"]


# --- BLOCKER tools/card_builder.py: stored XSS via unsanitized SVG ----------
#
# /config/www is served by HA at /local/... with NO authentication, in the
# same origin as the HA frontend. An SVG opened directly executes embedded
# <script>, on*= handlers and javascript: URIs — a stored-XSS path into an
# authenticated admin session. Every SVG written under www/card_builder/
# must go through card_builder.sanitize_svg().

_SVG_WITH_ONLOAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)" width="10" height="10">'
    '<rect width="10" height="10" fill="red"/></svg>'
)
_SVG_WITH_SCRIPT = (
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
    '<rect width="1" height="1"/></svg>'
)
_SVG_WITH_JS_HREF = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<a xlink:href="javascript:alert(1)"><rect width="1" height="1"/></a></svg>'
)
_SVG_WITH_FOREIGN_OBJECT = (
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject>'
    '<div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>'
    '<rect width="1" height="1"/></svg>'
)
_SVG_WITH_DOCTYPE_ENTITY = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    '<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>'
)
_SVG_WITH_EXTERNAL_USE_HREF = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<use xlink:href="https://evil.example/x.svg#a"/></svg>'
)
_SVG_WITH_STYLE_IMPORT = (
    '<svg xmlns="http://www.w3.org/2000/svg">'
    "<style>@import url(javascript:alert(1));</style>"
    '<rect width="1" height="1" fill="blue"/></svg>'
)
_SVG_BENIGN_GRADIENT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    '<defs><linearGradient id="g"><stop offset="0" stop-color="red"/>'
    '<stop offset="1" stop-color="blue"/></linearGradient></defs>'
    '<path d="M0 0 L10 10" fill="url(#g)"/></svg>'
)


def test_sanitize_svg_strips_onload_handler():
    out, err = card_builder.sanitize_svg(_SVG_WITH_ONLOAD)
    assert err is None
    assert "onload" not in out
    assert "alert(1)" not in out
    assert "<rect" in out  # graphic content preserved


def test_sanitize_svg_removes_script_element():
    out, err = card_builder.sanitize_svg(_SVG_WITH_SCRIPT)
    assert err is None
    assert "<script" not in out
    assert "alert(1)" not in out


def test_sanitize_svg_neutralizes_javascript_href():
    out, err = card_builder.sanitize_svg(_SVG_WITH_JS_HREF)
    assert err is None
    assert "javascript:" not in out


def test_sanitize_svg_removes_foreign_object():
    out, err = card_builder.sanitize_svg(_SVG_WITH_FOREIGN_OBJECT)
    assert err is None
    assert "foreignObject" not in out
    assert "<div" not in out
    assert "<rect" in out


def test_sanitize_svg_rejects_doctype_with_entity():
    out, err = card_builder.sanitize_svg(_SVG_WITH_DOCTYPE_ENTITY)
    assert out is None
    assert err is not None
    assert err["error"] == "unsafe_svg"


def test_sanitize_svg_strips_external_use_href():
    out, err = card_builder.sanitize_svg(_SVG_WITH_EXTERNAL_USE_HREF)
    assert err is None
    assert "evil.example" not in out
    assert "<use" in out  # element kept, only the unsafe href is dropped


def test_sanitize_svg_removes_style_element_with_import():
    out, err = card_builder.sanitize_svg(_SVG_WITH_STYLE_IMPORT)
    assert err is None
    assert "@import" not in out
    assert "javascript:" not in out
    assert "<style" not in out
    assert "<rect" in out


def test_sanitize_svg_keeps_benign_gradient_and_path():
    """Regression: legitimate card backgrounds (gradients, paths) must still render."""
    out, err = card_builder.sanitize_svg(_SVG_BENIGN_GRADIENT)
    assert err is None
    assert "linearGradient" in out
    assert 'stop-color="red"' in out
    assert 'd="M0 0 L10 10"' in out
    assert 'fill="url(#g)"' in out


def test_upload_svg_sanitizes_before_upload(monkeypatch):
    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/bad.svg"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_svg)(_SVG_WITH_SCRIPT, "bad")

    assert "error" not in result
    uploaded = base64.b64decode(calls["kwargs"]["content"]).decode("utf-8")
    assert "<script" not in uploaded
    assert "alert(1)" not in uploaded


def test_upload_svg_rejects_doctype_entity(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not upload before sanitization rejects DOCTYPE/ENTITY")

    monkeypatch.setattr(ha, "_ws_call", explode)

    result = _unwrap(card_builder.upload_svg)(_SVG_WITH_DOCTYPE_ENTITY, "evil")

    assert result["error"] == "unsafe_svg"


def test_upload_media_from_path_sanitizes_svg(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    www_dir = config_dir / "www"
    www_dir.mkdir(parents=True)
    svg_file = www_dir / "bad.svg"
    svg_file.write_text(_SVG_WITH_ONLOAD, encoding="utf-8")

    monkeypatch.setenv("HA_CONFIG_PATH", str(config_dir))

    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/bad.svg"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_media_from_path)(str(svg_file))

    assert "error" not in result
    uploaded = base64.b64decode(calls["kwargs"]["content"]).decode("utf-8")
    assert "onload" not in uploaded


def test_upload_image_from_url_sanitizes_svg_response(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"example.com": "93.184.216.34"})
    )
    _install_fake_client(
        monkeypatch,
        [_FakeStreamResponse(200, {"Content-Type": "image/svg+xml"}, [_SVG_WITH_SCRIPT.encode("utf-8")])],
    )

    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/photo.svg"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    result = _unwrap(card_builder.upload_image_from_url)("https://example.com/photo.svg")

    assert "error" not in result
    uploaded = base64.b64decode(calls["kwargs"]["content"]).decode("utf-8")
    assert "<script" not in uploaded


def test_upload_media_sanitizes_svg_by_filename(monkeypatch):
    """`upload_media` is the raw/generic upload path — no extension allowlist at
    all, so an SVG smuggled in through it must still be sanitized."""
    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/bad.svg"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)

    b64 = base64.b64encode(_SVG_WITH_ONLOAD.encode("utf-8")).decode("ascii")
    result = _unwrap(card_builder.upload_media)("bad.svg", b64)

    assert "error" not in result
    uploaded = base64.b64decode(calls["kwargs"]["content"]).decode("utf-8")
    assert "onload" not in uploaded


# --- BLOCKER (2026-09-27 follow-up) tools/card_builder.py: `upload_media`
# had NO extension allowlist at all — any `filename` (evil.html, evil.js,
# ...) was written verbatim under www/card_builder/, served unauthenticated
# at /local/, with only the `.svg` case sanitized. Same class of stored-XSS
# as the SVG findings above, just for a different content-type. Also had no
# upload size cap and no filename/path traversal guard.

def _explode(*_a, **_k):
    raise AssertionError("must not reach ha._ws_call for a rejected upload")


def test_upload_media_rejects_html_extension(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    b64 = base64.b64encode(b"<html><body onload=alert(1)></body></html>").decode("ascii")

    result = _unwrap(card_builder.upload_media)("evil.html", b64)

    assert result["error"] == "extension_not_allowed"


def test_upload_media_rejects_js_extension(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    b64 = base64.b64encode(b"alert(1)").decode("ascii")

    result = _unwrap(card_builder.upload_media)("evil.js", b64)

    assert result["error"] == "extension_not_allowed"


def test_upload_media_rejects_uppercase_html_extension(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    b64 = base64.b64encode(b"<html></html>").decode("ascii")

    result = _unwrap(card_builder.upload_media)("EVIL.HTML", b64)

    assert result["error"] == "extension_not_allowed"


def test_upload_media_rejects_double_extension_disguised_as_image(monkeypatch):
    """`a.png.html` — the *last* suffix (`.html`) is what counts, not the fact
    that '.png' appears somewhere in the name."""
    monkeypatch.setattr(ha, "_ws_call", _explode)
    b64 = base64.b64encode(b"<html></html>").decode("ascii")

    result = _unwrap(card_builder.upload_media)("a.png.html", b64)

    assert result["error"] == "extension_not_allowed"


def test_upload_media_accepts_png(monkeypatch):
    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/photo.png"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)
    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")

    result = _unwrap(card_builder.upload_media)("photo.png", b64)

    assert "error" not in result
    assert calls["kwargs"]["filename"] == "photo.png"


def test_upload_media_accepts_webp(monkeypatch):
    calls = {}

    def fake_ws_call(msg_type, **kwargs):
        calls["kwargs"] = kwargs
        return {"reference": "cb-media://local/card_builder/photo.webp"}

    monkeypatch.setattr(ha, "_ws_call", fake_ws_call)
    b64 = base64.b64encode(b"RIFF....WEBPfake").decode("ascii")

    result = _unwrap(card_builder.upload_media)("photo.webp", b64)

    assert "error" not in result
    assert calls["kwargs"]["filename"] == "photo.webp"


def test_upload_media_rejects_filename_with_path_traversal(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")

    result = _unwrap(card_builder.upload_media)("../../etc/passwd.png", b64)

    assert "error" in result


def test_upload_media_rejects_path_arg_with_traversal(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")

    result = _unwrap(card_builder.upload_media)("photo.png", b64, path="../../../etc")

    assert "error" in result


def test_upload_media_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    oversized = b"\x89PNG" + b"x" * card_builder._MAX_MEDIA_UPLOAD_BYTES
    b64 = base64.b64encode(oversized).decode("ascii")

    result = _unwrap(card_builder.upload_media)("big.png", b64)

    assert result["error"] == "file_too_large"


def test_upload_svg_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(ha, "_ws_call", _explode)
    huge_id = "a" * card_builder._MAX_MEDIA_UPLOAD_BYTES
    svg_content = f'<svg xmlns="http://www.w3.org/2000/svg"><rect id="{huge_id}" width="1" height="1"/></svg>'

    result = _unwrap(card_builder.upload_svg)(svg_content, "big")

    assert result["error"] == "file_too_large"


def test_upload_svg_rejects_element_count_bomb(monkeypatch):
    """Cheap defense-in-depth: cap total SVG element count after parsing,
    independent of the DOCTYPE/ENTITY-based XXE guard, since a flat/deeply
    nested element bomb doesn't need any entity declaration at all."""
    monkeypatch.setattr(ha, "_ws_call", _explode)
    many_rects = "".join('<rect width="1" height="1"/>' for _ in range(card_builder._SVG_MAX_ELEMENTS + 10))
    svg_content = f'<svg xmlns="http://www.w3.org/2000/svg">{many_rects}</svg>'

    result = _unwrap(card_builder.upload_svg)(svg_content, "bomb")

    assert "error" in result


# --- tools/files.py: list_config_files subdirectory boundary bypass ---------

def test_list_config_files_rejects_subdirectory_escaping_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leak.yaml").write_text("leaked: true", encoding="utf-8")

    monkeypatch.setattr(files, "_CONFIG_PATH", config_dir)

    result = _unwrap(files.list_config_files)("../outside")

    assert result == []


def test_list_config_files_still_lists_files_inside_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    sub_dir = config_dir / "sub"
    sub_dir.mkdir(parents=True)
    (sub_dir / "a.yaml").write_text("a: 1", encoding="utf-8")

    monkeypatch.setattr(files, "_CONFIG_PATH", config_dir)

    result = _unwrap(files.list_config_files)("sub")

    assert len(result) == 1
    assert result[0].replace("\\", "/") == "sub/a.yaml"


# --- tools/git_ops.py: git_rollback_file path boundary (defense in depth) ---

class _FakeGitCmd:
    def __init__(self):
        self.calls: list[tuple] = []

    def checkout(self, *args):
        self.calls.append(args)


class _FakeRepo:
    def __init__(self):
        self.git = _FakeGitCmd()


def test_git_rollback_file_rejects_path_outside_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(git_ops, "_CONFIG_PATH", config_dir)

    def explode():
        raise AssertionError("must not touch the repo for a path outside /config")

    monkeypatch.setattr(git_ops, "_repo", explode)

    result = _unwrap(git_ops.git_rollback_file)("../outside/secrets.yaml", confirm=True)

    assert "error" in result


def test_git_rollback_file_still_restores_path_inside_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(git_ops, "_CONFIG_PATH", config_dir)

    fake_repo = _FakeRepo()
    monkeypatch.setattr(git_ops, "_repo", lambda: fake_repo)

    result = _unwrap(git_ops.git_rollback_file)("automations.yaml", confirm=True)

    assert result["status"] == "restored"
    assert fake_repo.git.calls == [("HEAD", "--", "automations.yaml")]
