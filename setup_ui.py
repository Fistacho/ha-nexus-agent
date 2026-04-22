"""
Setup web UI — shows connection instructions for all MCP clients.
Accessible at http://<ha-ip>:7123 after starting Nexus.
"""
from fastapi.responses import HTMLResponse
import os
from auth import API_KEY

_PORT = int(os.getenv("NEXUS_PORT", "7123"))


def _ha_url() -> str:
    return os.getenv("HA_URL", "http://homeassistant.local:8123")


def _mcp_url() -> str:
    host = os.getenv("HA_IP", "homeassistant.local")
    return f"http://{host}:{_PORT}/mcp"


async def setup_page():
    ha_url = _ha_url()
    mcp_url = _mcp_url()
    api_key = API_KEY
    cwd = os.getcwd().replace("\\", "/")

    claude_desktop = f"""{{
  "mcpServers": {{
    "nexus": {{
      "command": "python",
      "args": ["server.py"],
      "cwd": "{cwd}",
      "env": {{
        "HA_URL": "{ha_url}",
        "NEXUS_API_KEY": "{api_key}"
      }}
    }}
  }}
}}"""

    claude_code_cmd = f"claude mcp add nexus --transport sse {mcp_url}"

    vscode_config = f"""{{
  "servers": {{
    "nexus": {{
      "type": "sse",
      "url": "{mcp_url}"
    }}
  }}
}}"""

    cursor_config = f"""{{
  "mcpServers": {{
    "nexus": {{
      "url": "{mcp_url}",
      "type": "sse"
    }}
  }}
}}"""

    windsurf_config = cursor_config

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Nexus — Setup</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 24px 60px; background: #0f172a; color: #e2e8f0; }}
  h1 {{ color: #38bdf8; font-size: 2rem; margin-bottom: 4px; }}
  .sub {{ color: #64748b; margin-bottom: 32px; font-size: 0.95rem; }}
  h2 {{ color: #7dd3fc; border-bottom: 1px solid #1e3a5f; padding-bottom: 6px; margin-top: 36px; }}
  h3 {{ color: #bae6fd; margin-bottom: 6px; font-size: 0.95rem; }}
  pre {{ background: #1e293b; padding: 14px 16px; border-radius: 8px; overflow-x: auto; font-size: 12.5px; border: 1px solid #334155; margin: 0 0 20px; }}
  .key {{ background: #1e293b; border: 1px solid #0ea5e9; border-radius: 6px; padding: 10px 16px; font-family: monospace; font-size: 14px; color: #38bdf8; word-break: break-all; margin-bottom: 8px; }}
  .badge {{ display: inline-block; background: #166534; color: #bbf7d0; padding: 2px 10px; border-radius: 12px; font-size: 12px; margin-left: 8px; vertical-align: middle; }}
  .warn {{ background: #422006; border: 1px solid #92400e; border-radius: 6px; padding: 10px 16px; color: #fcd34d; font-size: 13px; margin-top: 24px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  p {{ color: #94a3b8; font-size: 0.9rem; margin: 4px 0 12px; }}
  code {{ background: #1e293b; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
  .note {{ color: #64748b; font-size: 12px; margin-top: -12px; margin-bottom: 16px; }}
</style>
</head>
<body>
<h1>Nexus <span class="badge">running</span></h1>
<p class="sub">MCP server for Home Assistant &nbsp;·&nbsp; 100 tools &nbsp;·&nbsp; <a href="/health" style="color:#38bdf8">health</a></p>

<h2>API Key</h2>
<div class="key">{api_key}</div>
<p>Stored in <code>/config/.nexus_api_key</code>. Keep it secret.</p>

<h2>MCP URL (HTTP clients)</h2>
<div class="key">{mcp_url}</div>
<p class="note">Use this URL for all HTTP-based clients below.</p>

<h2>Claude Desktop</h2>
<p>Paste into <code>%APPDATA%\Claude\claude_desktop_config.json</code> (Windows) or <code>~/Library/Application Support/Claude/claude_desktop_config.json</code> (macOS):</p>
<pre>{claude_desktop}</pre>

<h2>Claude Code (CLI)</h2>
<p>Run once in terminal:</p>
<pre>{claude_code_cmd}</pre>

<div class="grid">
  <div>
    <h2>VS Code</h2>
    <p>Create <code>.vscode/mcp.json</code> in your workspace:</p>
    <pre>{vscode_config}</pre>
  </div>
  <div>
    <h2>Cursor</h2>
    <p>Paste into <code>~/.cursor/mcp.json</code>:</p>
    <pre>{cursor_config}</pre>
  </div>
</div>

<h2>Windsurf</h2>
<p>Paste into <code>~/.codeium/windsurf/mcp_config.json</code>:</p>
<pre>{windsurf_config}</pre>

<h2>Home Assistant</h2>
<p>Connected to: <code>{ha_url}</code></p>
<div class="warn">Never share your API key. Delete <code>/config/.nexus_api_key</code> and restart to regenerate.</div>
</body>
</html>""")


async def health():
    return {"status": "ok", "ha_url": _ha_url(), "mcp_url": _mcp_url(), "tools": 100}
