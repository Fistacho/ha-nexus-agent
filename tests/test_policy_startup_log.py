"""The startup log must say whether read_only (and the rest of the policy) is on.

`server._apply_tool_policy_or_exit()` reported the applied policy through
`logging.getLogger(__name__).info(...)`, but nexus never configures the root
logger, so Python's last-resort handler (WARNING and above) dropped that
line in both stdio and HTTP mode -- the 0.29.0 image started with
`NEXUS_READ_ONLY=true`/`READ_ONLY=true` and printed nothing about it. The
line goes to stderr (never stdout: in stdio mode stdout is the MCP stream)
and carries no secrets -- only booleans, namespace names and counts.
"""
from __future__ import annotations

from fastmcp import FastMCP

import server


def _clear_policy_env(monkeypatch):
    for key in (
        "NEXUS_READ_ONLY",
        "NEXUS_DISABLED_NAMESPACES",
        "NEXUS_TOOL_MODE",
        "NEXUS_NODERED_WRITE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


def test_policy_summary_goes_to_stderr_when_read_only_on(monkeypatch, capsys):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("NEXUS_READ_ONLY", "true")
    # A throwaway root: applying read_only to the session-wide server.mcp
    # would hide tools from every other test sharing this worker.
    monkeypatch.setattr(server, "mcp", FastMCP("test-policy-log"))

    server._apply_tool_policy_or_exit()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "read_only=True" in captured.err
    assert "tool policy" in captured.err.lower()


def test_policy_summary_reports_read_only_off(monkeypatch, capsys):
    _clear_policy_env(monkeypatch)
    monkeypatch.setattr(server, "mcp", FastMCP("test-policy-log-off"))

    server._apply_tool_policy_or_exit()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "read_only=False" in captured.err
