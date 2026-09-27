#!/usr/bin/env python3
"""Onboard a freshly started, un-configured Home Assistant container over its REST
API and write out a long-enough-lived access token for the e2e smoke tests
(`tests_e2e/`) to use against it.

Home Assistant's first-boot "onboarding" wizard is normally driven by the frontend;
this script drives the same REST endpoints directly so CI can run headless. The
exact request/response shapes below are taken from the Home Assistant Core source
at the version pinned in `.github/workflows/e2e.yml`
(`homeassistant/components/onboarding/views.py` and
`homeassistant/components/auth/__init__.py`), not guessed:

1. ``POST /api/onboarding/users`` (no auth) creates the first (admin) user and
   returns an OAuth2 ``auth_code``.
2. ``POST /auth/token`` (form-encoded, grant_type=authorization_code) exchanges
   that code for an ``access_token`` + ``refresh_token``.
3. ``POST /api/onboarding/core_config``, ``.../integration`` and
   ``.../analytics`` (all Bearer-authenticated) finish the remaining onboarding
   steps; once all four are done, HA leaves "onboarding" mode and the normal
   `/api/*` surface nexus's tools call is fully available.

The OAuth ``access_token`` (~30 min lifetime) is used as-is rather than minting a
long-lived access token — a single CI job's smoke run comfortably fits inside that
window, and minting an LLAT would require an extra authenticated WebSocket round
trip for no real benefit here.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _request(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    form_body: dict | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = None

    # Fixed CI-controlled URL (localhost HA container / GITHUB_ACTIONS runner), never
    # user-supplied input — the request target is `--base-url` from the CI workflow.
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read()
        return json.loads(body) if body else {}


def wait_for_onboarding_api(base_url: str, *, attempts: int = 60, delay: float = 2.0) -> None:
    """Poll GET /api/onboarding until Home Assistant answers (or give up)."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            _request("GET", f"{base_url}/api/onboarding")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(
        f"Home Assistant at {base_url} never answered GET /api/onboarding "
        f"after {attempts * delay:.0f}s: {last_error}"
    )


def onboard(base_url: str) -> dict:
    """Run the full onboarding flow. Returns {'url': ..., 'token': ...}."""
    base_url = base_url.rstrip("/")
    client_id = f"{base_url}/"

    status = _request("GET", f"{base_url}/api/onboarding")
    if any(step.get("step") == "user" and step.get("done") for step in status):
        raise RuntimeError(
            "Home Assistant at "
            f"{base_url} is already onboarded (the 'user' step is done) — "
            "this script only knows how to onboard a *fresh* container; start a "
            "new one instead of reusing an already-onboarded instance."
        )

    password = secrets.token_urlsafe(24)
    user_step = _request(
        "POST",
        f"{base_url}/api/onboarding/users",
        json_body={
            "client_id": client_id,
            "name": "e2e",
            "username": "e2e",
            "password": password,
            "language": "en",
        },
    )
    auth_code = user_step["auth_code"]

    token_response = _request(
        "POST",
        f"{base_url}/auth/token",
        form_body={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": auth_code,
        },
    )
    access_token = token_response["access_token"]

    _request("POST", f"{base_url}/api/onboarding/core_config", token=access_token)
    _request(
        "POST",
        f"{base_url}/api/onboarding/integration",
        json_body={"client_id": client_id, "redirect_uri": client_id},
        token=access_token,
    )
    _request("POST", f"{base_url}/api/onboarding/analytics", token=access_token)

    return {"url": base_url, "token": access_token}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8123")
    parser.add_argument(
        "--output",
        default="e2e_ha_credentials.json",
        help="Path to write {'url': ..., 'token': ...} JSON to.",
    )
    parser.add_argument(
        "--wait-attempts",
        type=int,
        default=60,
        help="How many times to poll /api/onboarding before giving up.",
    )
    parser.add_argument("--wait-delay", type=float, default=2.0, help="Seconds between polls.")
    args = parser.parse_args(argv)

    wait_for_onboarding_api(args.base_url, attempts=args.wait_attempts, delay=args.wait_delay)
    credentials = onboard(args.base_url)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(credentials, f)

    print(f"Onboarded {args.base_url}; credentials written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
