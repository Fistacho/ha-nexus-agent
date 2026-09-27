from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
import os
from auth import API_KEY, can_access_ui
from policy import ToolPolicy

_PORT = int(os.getenv("NEXUS_PORT", "7123"))

# Wired in by server._build_app() once the FastMCP instance exists, so the
# Setup UI can report the live tool count instead of a hard-coded number
# that silently goes stale (was "100" while the registry held 323).
_mcp = None

# Wired in by server.main() *after* policy.apply_policy() has run (ADR-0003
# P1/P2). Anything that only calls `server._build_app()` directly (most of
# tests/test_audit_http_auth.py) never calls set_policy(), so this default —
# the same as 0.21.0's unconditional behaviour — is exactly right for those
# callers too.
_policy: ToolPolicy = ToolPolicy()


def set_mcp(instance) -> None:
    global _mcp
    _mcp = instance


def set_policy(policy: ToolPolicy) -> None:
    global _policy
    _policy = policy


async def _tool_count() -> int:
    if _mcp is None:
        return 0
    tools = await _mcp.list_tools()
    return len(tools)


def _ha_url() -> str:
    return os.getenv("HA_URL", "http://homeassistant.local:8123")


_PAGE_STYLE = """
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
  .deprecated{background:#3f1d1d;border:1px solid #7f1d1d;border-radius:6px;padding:8px 14px;color:#fca5a5;font-size:12.5px;margin:10px 0}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  p{color:#94a3b8;font-size:.875rem;margin:4px 0 6px}
  code{background:#1e293b;padding:2px 6px;border-radius:4px;font-size:11px}
  a{color:#38bdf8}
"""

_COPY_SCRIPT = """
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
function regenerateKey() {
  var btn = document.getElementById('regen-btn');
  var msg = document.getElementById('regen-msg');
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  fetch('regenerate', {method:'POST'}).then(function(r){ return r.json(); }).then(function(d){
    if (d.ok) {
      msg.style.color = '#86efac';
      msg.textContent = 'Key deleted — restart add-on to activate new key.';
      btn.textContent = 'Done';
    } else {
      msg.style.color = '#fca5a5';
      msg.textContent = d.error || 'Error';
      btn.disabled = false;
      btn.textContent = 'Regenerate key';
    }
  });
}
function flash(btn) {
  btn.textContent = 'copied!';
  btn.classList.add('ok');
  setTimeout(function() { btn.textContent = 'copy'; btn.classList.remove('ok'); }, 1500);
}
"""


def _build_configs(mcp_url_bare: str, mcp_url_with_token: str, api_key: str, ha_url: str, cwd: str) -> dict:
    """Per-client ready-to-paste config. Prefers the `Authorization: Bearer`
    header over the `?token=` query string wherever the client supports it —
    the query string ends up in server access logs and shell history.

    Client header support confirmed against each project's own docs (2026-09):
    - Claude Code CLI: `claude mcp add --transport http ... --header "Authorization: Bearer ..."`
      (code.claude.com/docs/en/mcp)
    - Gemini CLI: `gemini mcp add --transport http --header "Authorization: Bearer ..."`
      (geminicli.com/docs/tools/mcp-server)
    - VS Code / Cursor / Windsurf: `headers` object in their JSON server config.
    - OpenAI Codex CLI: `codex mcp add` has NO --header flag for HTTP servers;
      headers require manually editing config.toml (`bearer_token_env_var` /
      `http_headers`). The one-liner below therefore keeps the query-token
      form for Codex, with an explicit warning in the page.
    """
    return {
        "claude-code":    f'claude mcp add nexus --transport http "{mcp_url_bare}" --header "Authorization: Bearer {api_key}" --scope user',
        "codex":          f'codex mcp add nexus --url "{mcp_url_with_token}"',
        "gemini":         f'gemini mcp add --transport http --header "Authorization: Bearer {api_key}" nexus "{mcp_url_bare}"',
        "claude-desktop": '{{\n  "mcpServers": {{\n    "nexus": {{\n      "command": "python",\n      "args": ["server.py"],\n      "cwd": "{cwd}",\n      "env": {{\n        "HA_URL": "{ha_url}",\n        "NEXUS_API_KEY": "{api_key}"\n      }}\n    }}\n  }}\n}}'.format(cwd=cwd, ha_url=ha_url, api_key=api_key),
        "vscode":         '{{\n  "servers": {{\n    "nexus": {{\n      "type": "http",\n      "url": "{mcp_url}",\n      "headers": {{\n        "Authorization": "Bearer {api_key}"\n      }}\n    }}\n  }}\n}}'.format(mcp_url=mcp_url_bare, api_key=api_key),
        "cursor":         '{{\n  "mcpServers": {{\n    "nexus": {{\n      "url": "{mcp_url}",\n      "type": "http",\n      "headers": {{\n        "Authorization": "Bearer {api_key}"\n      }}\n    }}\n  }}\n}}'.format(mcp_url=mcp_url_bare, api_key=api_key),
        "windsurf":       '{{\n  "mcpServers": {{\n    "nexus": {{\n      "url": "{mcp_url}",\n      "type": "http",\n      "headers": {{\n        "Authorization": "Bearer {api_key}"\n      }}\n    }}\n  }}\n}}'.format(mcp_url=mcp_url_bare, api_key=api_key),
    }


def _locked_page_html() -> str:
    """Unauthenticated response. Deliberately omits the tool count, `ha_url`
    and active policy — those are addon-internal facts, shown only on the
    `can_access_ui`-gated page below (ADR-0003 P: same reasoning as why
    `/health` was trimmed to `{"status": "ok"}`)."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Nexus</title>
<style>""" + _PAGE_STYLE + """</style>
</head>
<body>
<h1>Nexus <span class="badge">running</span></h1>
<p class="sub">MCP server for Home Assistant &nbsp;&middot;&nbsp; <a href="health">health check</a></p>

<h2>Configuration is not shown here</h2>
<p>This page only reveals your API key and client configuration to callers Home
Assistant has already authenticated.</p>
<div class="tip">Open Nexus with the <strong>Open Web UI</strong> button on the add-on's Info page to see your API key and ready-to-paste client configs. The Home Assistant sidebar only shows Nexus if you additionally enable "Show in sidebar" on that same page.</div>
<p>Running the standalone (non-add-on) build? This page only shows the key to requests
from <code>localhost</code>, or with a valid <code>Authorization: Bearer &lt;API_KEY&gt;</code> header.</p>
</body>
</html>"""


def _policy_section_html(policy: ToolPolicy) -> str:
    """Active tool-exposure policy (ADR-0003 P1/P2) — shown only here, behind
    `can_access_ui`, next to the watchdog reminder Supervisor operators
    otherwise miss (enabling it in `config.yaml` alone does not turn the
    add-on's watchdog on)."""
    if policy.disabled_namespaces:
        namespaces_html = ", ".join(f"<code>{ns}</code>" for ns in sorted(policy.disabled_namespaces))
    else:
        namespaces_html = "<em>none</em>"

    read_only_html = "<strong>on</strong> — only read-only tools are visible/callable" if policy.read_only else "off"

    return """
<h2>Active Policy</h2>
<p>read_only: """ + read_only_html + """<br>
disabled_namespaces: """ + namespaces_html + """<br>
tool_mode: <code>""" + policy.tool_mode + """</code></p>
<div class="tip">Changing these requires an add-on restart — see the add-on's Configuration tab. The Supervisor <strong>Watchdog</strong> toggle on the add-on's Info tab must be turned on separately; listing <code>watchdog:</code> in this add-on's own manifest does not enable it by itself.</div>
"""


def _full_page_html(configs: dict, mcp_url_with_token: str, api_key: str, ha_url: str, tool_count: int, policy: ToolPolicy) -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Nexus</title>
<style>""" + _PAGE_STYLE + """</style>
</head>
<body>
<h1>Nexus <span class="badge">running</span></h1>
<p class="sub">MCP server for Home Assistant &nbsp;&middot;&nbsp; """ + str(tool_count) + """ tools &nbsp;&middot;&nbsp; <a href="health">health check</a></p>
""" + _policy_section_html(policy) + """
<h2>API Key</h2>
<div class="key">""" + api_key + """</div>
<p>Stored in <code>/config/.nexus_api_key</code>. Prefer the <code>Authorization: Bearer</code> header over the URL query string below where your client supports it — query strings end up in access logs and shell history.</p>

<h2>MCP URL</h2>
<div class="key" id="mcp-url">""" + mcp_url_with_token + """</div>
<div class="deprecated">The <code>?token=</code> query string is supported for backward compatibility only and is logged in plaintext by the HTTP access log. Use the <code>Authorization: Bearer</code> header wherever the client below supports it.</div>

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
<div class="deprecated">Codex's <code>mcp add</code> has no <code>--header</code> flag for HTTP servers, so this uses the query-token URL — visible in shell history and access logs. For header-based auth instead, edit <code>~/.codex/config.toml</code> directly and add <code>bearer_token_env_var</code> under <code>[mcp_servers.nexus]</code>.</div>
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
<div class="warn">
  Never share your API key.
  <button onclick="regenerateKey()" style="margin-left:12px;background:#92400e;border:1px solid #fcd34d;color:#fcd34d;padding:4px 14px;border-radius:4px;font-size:12px;cursor:pointer" id="regen-btn">Regenerate key</button>
  <span id="regen-msg" style="margin-left:10px;font-size:12px"></span>
</div>

<script>""" + _COPY_SCRIPT + """</script>
</body>
</html>"""


async def setup_page(request: Request):
    if not can_access_ui(request):
        return HTMLResponse(_locked_page_html())

    host_header = request.headers.get("host", f"homeassistant.local:{_PORT}")
    hostname = host_header.split(":")[0]
    ha_url = _ha_url()
    tool_count = await _tool_count()

    mcp_url_bare = f"http://{hostname}:{_PORT}/mcp"
    mcp_url_with_token = f"{mcp_url_bare}?token={API_KEY}"
    cwd = os.getcwd().replace("\\", "/")
    configs = _build_configs(mcp_url_bare, mcp_url_with_token, API_KEY, ha_url, cwd)

    return HTMLResponse(_full_page_html(configs, mcp_url_with_token, API_KEY, ha_url, tool_count, _policy))


async def health():
    """Public, unauthenticated — the Supervisor watchdog only needs a 200.

    Deliberately returns nothing else: `ha_url`, the live tool count and the
    active policy used to leak here to any caller on the LAN before ingress
    auth was even checked. They now live on the `can_access_ui`-gated Setup
    UI page instead (see `_full_page_html`/`_policy_section_html`).
    """
    return {"status": "ok"}


async def regenerate(request: Request):
    if not can_access_ui(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    from auth import delete_api_key
    delete_api_key()
    return JSONResponse({"ok": True})
