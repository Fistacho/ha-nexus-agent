"""
Setup web UI — shows connection instructions for Claude Desktop / Cursor / VS Code.
Accessible at http://localhost:7123 after starting Nexus.
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os
from auth import API_KEY, get_ha_token

app = FastAPI(title="Nexus Setup", docs_url=None, redoc_url=None)

_PORT = int(os.getenv("NEXUS_PORT", "7123"))


def _ha_url() -> str:
    return os.getenv("HA_URL", "http://homeassistant.local:8123")


@app.get("/", response_class=HTMLResponse)
async def setup_page():
    ha_url = _ha_url()
    api_key = API_KEY

    claude_config = f"""{{
  "mcpServers": {{
    "nexus": {{
      "command": "python",
      "args": ["server.py"],
      "cwd": "{os.getcwd().replace(chr(92), '/')}",
      "env": {{
        "HA_URL": "{ha_url}",
        "NEXUS_API_KEY": "{api_key}"
      }}
    }}
  }}
}}"""

    cursor_config = f"""[mcp]
name = nexus
url = http://localhost:{_PORT}/mcp
api_key = {api_key}"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Nexus — Setup</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; background: #0f172a; color: #e2e8f0; }}
  h1 {{ color: #38bdf8; font-size: 2rem; margin-bottom: 4px; }}
  .sub {{ color: #64748b; margin-bottom: 32px; }}
  h2 {{ color: #7dd3fc; border-bottom: 1px solid #1e3a5f; padding-bottom: 6px; }}
  pre {{ background: #1e293b; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 13px; border: 1px solid #334155; }}
  .key {{ background: #1e293b; border: 1px solid #0ea5e9; border-radius: 6px; padding: 10px 16px; font-family: monospace; font-size: 14px; color: #38bdf8; word-break: break-all; }}
  .badge {{ display: inline-block; background: #166534; color: #bbf7d0; padding: 2px 10px; border-radius: 12px; font-size: 12px; margin-left: 8px; }}
  .warn {{ background: #422006; border: 1px solid #92400e; border-radius: 6px; padding: 10px 16px; color: #fcd34d; font-size: 13px; }}
  p {{ color: #94a3b8; }}
</style>
</head>
<body>
<h1>Nexus <span class="badge">running</span></h1>
<p class="sub">MCP server for Home Assistant · 93 tools</p>

<h2>Your API Key</h2>
<div class="key">{api_key}</div>
<p>Keep this secret. It's stored in <code>/config/.nexus_api_key</code>.</p>

<h2>Claude Desktop</h2>
<p>Add to <code>~/Library/Application Support/Claude/claude_desktop_config.json</code> (macOS) or <code>%APPDATA%\\Claude\\claude_desktop_config.json</code> (Windows):</p>
<pre>{claude_config}</pre>

<h2>Cursor / VS Code</h2>
<pre>{cursor_config}</pre>

<h2>Connected to Home Assistant</h2>
<p>URL: <code>{ha_url}</code></p>
<div class="warn">Never share your API key. Restart Nexus to regenerate it (delete <code>/config/.nexus_api_key</code>).</div>
</body>
</html>""")


@app.get("/health")
async def health():
    return {"status": "ok", "ha_url": _ha_url(), "tools": 93}


def start_ui():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=_PORT, log_level="warning")
