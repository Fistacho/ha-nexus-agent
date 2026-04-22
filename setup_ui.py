from fastapi import Request
from fastapi.responses import HTMLResponse
import os
from auth import API_KEY

_PORT = int(os.getenv("NEXUS_PORT", "7123"))


def _ha_url() -> str:
    return os.getenv("HA_URL", "http://homeassistant.local:8123")


async def setup_page(request: Request):
    host_header = request.headers.get("host", f"homeassistant.local:{_PORT}")
    hostname = host_header.split(":")[0]
    ha_url = _ha_url()
    mcp_url = f"http://{hostname}:{_PORT}/mcp?token={API_KEY}"
    api_key = API_KEY
    cwd = os.getcwd().replace("\\", "/")

    configs = {
        "claude-code":    f"claude mcp add nexus --transport sse {mcp_url}",
        "codex":          f"codex mcp add nexus --url {mcp_url}",
        "gemini":         f"gemini mcp add nexus --url {mcp_url}",
        "claude-desktop": '{{\n  "mcpServers": {{\n    "nexus": {{\n      "command": "python",\n      "args": ["server.py"],\n      "cwd": "{cwd}",\n      "env": {{\n        "HA_URL": "{ha_url}",\n        "NEXUS_API_KEY": "{api_key}"\n      }}\n    }}\n  }}\n}}'.format(cwd=cwd, ha_url=ha_url, api_key=api_key),
        "vscode":         '{{\n  "servers": {{\n    "nexus": {{\n      "type": "sse",\n      "url": "{mcp_url}"\n    }}\n  }}\n}}'.format(mcp_url=mcp_url),
        "cursor":         '{{\n  "mcpServers": {{\n    "nexus": {{\n      "url": "{mcp_url}",\n      "type": "sse"\n    }}\n  }}\n}}'.format(mcp_url=mcp_url),
        "windsurf":       '{{\n  "mcpServers": {{\n    "nexus": {{\n      "url": "{mcp_url}",\n      "type": "sse"\n    }}\n  }}\n}}'.format(mcp_url=mcp_url),
    }

    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Nexus</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;max-width:820px;margin:40px auto;padding:0 24px 60px;background:#0f172a;color:#e2e8f0}
  h1{color:#38bdf8;font-size:2rem;margin-bottom:4px}
  .sub{color:#64748b;margin-bottom:32px;font-size:.9rem}
  h2{color:#7dd3fc;border-bottom:1px solid #1e3a5f;padding-bottom:6px;margin-top:32px;font-size:1rem}
  .code-wrap{position:relative;margin:6px 0 16px}
  pre{background:#1e293b;padding:14px 50px 14px 16px;border-radius:8px;overflow-x:auto;font-size:12.5px;border:1px solid #334155;margin:0;white-space:pre-wrap;word-break:break-all}
  .copy-btn{position:absolute;top:8px;right:8px;background:#334155;border:none;color:#94a3b8;padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer}
  .copy-btn:hover{background:#475569;color:#e2e8f0}
  .copy-btn.ok{background:#166534;color:#bbf7d0}
  .key{background:#1e293b;border:1px solid #0ea5e9;border-radius:6px;padding:10px 16px;font-family:monospace;font-size:13px;color:#38bdf8;word-break:break-all;margin-bottom:6px}
  .badge{display:inline-block;background:#166534;color:#bbf7d0;padding:2px 10px;border-radius:12px;font-size:12px;margin-left:8px;vertical-align:middle}
  .tip{background:#0c2a1a;border:1px solid #166634;border-radius:6px;padding:8px 14px;color:#86efac;font-size:12.5px;margin-bottom:10px}
  .warn{background:#422006;border:1px solid #92400e;border-radius:6px;padding:10px 16px;color:#fcd34d;font-size:13px;margin-top:24px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  p{color:#94a3b8;font-size:.875rem;margin:4px 0 6px}
  code{background:#1e293b;padding:2px 6px;border-radius:4px;font-size:11px}
  a{color:#38bdf8}
</style>
</head>
<body>
<h1>Nexus <span class="badge">running</span></h1>
<p class="sub">MCP server for Home Assistant &nbsp;&middot;&nbsp; 100 tools &nbsp;&middot;&nbsp; <a href="health">health check</a></p>

<h2>MCP URL (with token)</h2>
<div class="key" id="mcp-url">""" + mcp_url + """</div>
<p>This URL includes your API key &mdash; paste it directly into any MCP client.</p>

<h2>API Key</h2>
<div class="key">""" + api_key + """</div>
<p>Stored in <code>/config/.nexus_api_key</code>.</p>

<h2>&#9889; Claude Code CLI &mdash; one command</h2>
<div class="tip">No config files needed. Just run this once in your terminal.</div>
<div class="code-wrap">
  <pre id="claude-code">""" + configs["claude-code"] + """</pre>
  <button class="copy-btn" onclick="copyBlock('claude-code')">copy</button>
</div>

<div class="grid">
<section>
<h2>OpenAI Codex CLI</h2>
<p>Run once in terminal:</p>
<div class="code-wrap"><pre id="codex">""" + configs["codex"] + """</pre><button class="copy-btn" onclick="copyBlock('codex')">copy</button></div>
</section>
<section>
<h2>Gemini CLI</h2>
<p>Run once in terminal:</p>
<div class="code-wrap"><pre id="gemini">""" + configs["gemini"] + """</pre><button class="copy-btn" onclick="copyBlock('gemini')">copy</button></div>
</section>
</div>

<h2>Claude Desktop</h2>
<p>Paste into <code>%APPDATA%/Claude/claude_desktop_config.json</code> (Win) or <code>~/Library/Application Support/Claude/claude_desktop_config.json</code> (Mac):</p>
<div class="code-wrap"><pre id="claude-desktop">""" + configs["claude-desktop"] + """</pre><button class="copy-btn" onclick="copyBlock('claude-desktop')">copy</button></div>

<div class="grid">
<section>
<h2>VS Code</h2>
<p>Create <code>.vscode/mcp.json</code>:</p>
<div class="code-wrap"><pre id="vscode">""" + configs["vscode"] + """</pre><button class="copy-btn" onclick="copyBlock('vscode')">copy</button></div>
</section>
<section>
<h2>Cursor</h2>
<p>Paste into <code>~/.cursor/mcp.json</code>:</p>
<div class="code-wrap"><pre id="cursor">""" + configs["cursor"] + """</pre><button class="copy-btn" onclick="copyBlock('cursor')">copy</button></div>
</section>
</div>

<h2>Windsurf</h2>
<p>Paste into <code>~/.codeium/windsurf/mcp_config.json</code>:</p>
<div class="code-wrap"><pre id="windsurf">""" + configs["windsurf"] + """</pre><button class="copy-btn" onclick="copyBlock('windsurf')">copy</button></div>

<h2>Home Assistant</h2>
<p>Connected to: <code>""" + ha_url + """</code></p>
<div class="warn">Never share your API key. Delete <code>/config/.nexus_api_key</code> and restart to regenerate.</div>

<script>
function copyBlock(id) {
  var text = document.getElementById(id).textContent;
  var btn = event.target;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function() { flash(btn); });
  } else {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand('copy'); flash(btn); } catch(e) {}
    document.body.removeChild(ta);
  }
}
function flash(btn) {
  btn.textContent = 'copied!';
  btn.classList.add('ok');
  setTimeout(function() { btn.textContent = 'copy'; btn.classList.remove('ok'); }, 1500);
}
</script>
</body>
</html>""")


async def health():
    return {"status": "ok", "ha_url": _ha_url(), "tools": 100}
