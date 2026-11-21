"""Smoke-test the nexus image as a stdio MCP server (ADR-0005 conditions (a) and (b)).

Runs the image the way `server.json` declares it for the MCP Registry
(`docker run -i --rm -e HA_URL -e HA_TOKEN <image>`), sends `initialize`,
`notifications/initialized` and `tools/list` over stdin, and checks:

- every stdout line is a JSON-RPC message (MCP spec, stdio transport:
  nothing else may appear on stdout);
- `initialize` and `tools/list` both answer;
- the number of tools in `tools/list`, when `--expect-tools` is given;
- a substring on stderr, when `--expect-stderr` is given (e.g. the
  "Tool policy applied: read_only=True" startup line).

Extra `docker run` arguments go after `--`, e.g. to start in add-on mode:

    python scripts/smoke_stdio_image.py nexus:test --expect-stderr read_only=True \
        -- -v "$PWD/options.json:/data/options.json:ro"

Uses fake credentials only: nothing here reaches a Home Assistant instance.
Standard library only, so CI can run it without installing nexus.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "nexus-smoke", "version": "0"},
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--expect-tools", type=int)
    parser.add_argument("--expect-stderr", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=60.0)
    args, extra = parser.parse_known_args()
    extra = [a for a in extra if a != "--"]

    cmd = [
        "docker", "run", "-i", "--rm",
        "-e", "HA_URL=http://127.0.0.1:9",
        "-e", "HA_TOKEN=smoke-fake-token",
        *extra,
        args.image,
    ]
    # S603: argv list, no shell; the image and extra docker args come from
    # whoever runs this smoke test (CI workflow or a developer), not from untrusted input.
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
    responses: dict[int, dict] = {}
    bad_lines: list[str] = []
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                bad_lines.append(line)
                continue
            if isinstance(msg, dict) and "id" in msg:
                responses[msg["id"]] = msg

    def read_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    def wait_for(msg_id: int) -> bool:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and msg_id not in responses and proc.poll() is None:
            time.sleep(0.2)
        return msg_id in responses

    try:
        send(_INIT)
        ok_init = wait_for(1)
        if ok_init:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        ok_list = ok_init and wait_for(2)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(0.5)

    failures: list[str] = []
    if bad_lines:
        failures.append(f"non-JSON-RPC lines on stdout: {bad_lines[:3]!r}")
    if not ok_init:
        failures.append("no response to initialize")
    if not ok_list:
        failures.append("no response to tools/list")
    tool_count = None
    if ok_list:
        tool_count = len(responses[2].get("result", {}).get("tools", []))
        if args.expect_tools is not None and tool_count != args.expect_tools:
            failures.append(f"tools/list returned {tool_count} tools, expected {args.expect_tools}")
    stderr_text = "\n".join(stderr_lines)
    for needle in args.expect_stderr:
        if needle not in stderr_text:
            failures.append(f"stderr does not contain {needle!r}")

    if failures:
        print("stdio smoke FAILED:", *failures, sep="\n  ")
        print("stderr:\n" + stderr_text[-3000:])
        return 1
    print(f"stdio smoke OK: initialize + tools/list answered, {tool_count} tools, stdout clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
