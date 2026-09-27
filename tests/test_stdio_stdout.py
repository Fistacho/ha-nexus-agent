"""Regression test: nexus's stdio transport must never write anything to
stdout that isn't a valid MCP message.

Per the MCP spec ("Transports", 2025-11-25): "The server MUST NOT write
anything to its stdout that is not a valid MCP message." `server.main()`'s
stdio branch used to print its startup banner (`Nexus starting (stdio)`,
`API key stored in ...`) via bare `print(line)` — no `file=` argument, so it
landed on stdout — which corrupts the JSON-RPC stream for any stdio client
(Claude Desktop, etc.) reading nexus's stdout as MCP messages.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import server

_ROOT = Path(__file__).resolve().parents[1]


def test_stdio_startup_writes_nothing_to_stdout(monkeypatch, capsys):
    """`server.main()` in stdio mode (no `SUPERVISOR_TOKEN`, no `NEXUS_HTTP`)
    must print its startup banner to stderr only.

    `mcp.run()` is monkeypatched to a no-op so this never blocks on stdin and
    never depends on `mcp.run()`'s own transport behaviour (FastMCP's banner
    already goes to stderr via `rich.Console(stderr=True)`; this test is only
    about the two lines nexus prints itself, in `server._startup_log_lines`,
    before `mcp.run()` is even called).
    """
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_HTTP", raising=False)
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: None)

    server.main()

    captured = capsys.readouterr()
    assert captured.out == "", f"stdio startup wrote to stdout: {captured.out!r}"
    assert "Nexus starting (stdio)" in captured.err
    assert "API key stored in" in captured.err


def test_stdio_subprocess_first_stdout_line_is_valid_jsonrpc(tmp_path):
    """End-to-end check: a real `python server.py` child process, launched
    the same way Claude Desktop (or any stdio MCP client) would launch it —
    standalone env, no `NEXUS_HTTP`/`SUPERVISOR_TOKEN` — must have its very
    first stdout line parse as a JSON-RPC response to `initialize`, not a
    startup banner line. Complements
    `test_stdio_startup_writes_nothing_to_stdout` (which only exercises
    `server.main()` in-process) by also covering anything module import,
    `load_dotenv()`, or FastMCP itself might print before `main()` even runs.
    """
    env = dict(os.environ)
    env.pop("NEXUS_HTTP", None)
    env.pop("SUPERVISOR_TOKEN", None)
    env["HA_URL"] = "http://ha.test:8123"
    env["HA_TOKEN"] = "test-ha-token"
    env["NEXUS_API_KEY"] = "test-api-key"
    env["HA_CONFIG_PATH"] = str(tmp_path)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-stdio-client", "version": "0"},
        },
    }

    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
    )
    try:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        first_line = proc.stdout.readline()
    finally:
        proc.kill()
        proc.communicate(timeout=10)

    assert first_line, "child process closed stdout before writing any line"
    message = json.loads(first_line)
    assert message.get("jsonrpc") == "2.0"
    assert message.get("id") == 1
    assert "result" in message
