# Nexus Agent — Documentation

Supervisor renders this file as the add-on's **Documentation** tab. It is a
short operational reference; the full tool catalogue, feature list and every
MCP client's exact config snippet live in
[README.md](https://github.com/Fistacho/ha-nexus-agent#readme) — this page
links to the relevant section instead of duplicating it.

## Install

- **Add-on (recommended):** Settings → Add-ons → Add-on Store → ⋮ →
  Repositories → add `https://github.com/Fistacho/ha-nexus-agent` → install
  **Nexus Agent** → Start → **Open Web UI**. See
  [README § Installation](https://github.com/Fistacho/ha-nexus-agent#installation--home-assistant-add-on-recommended).
- **Standalone / HA Container (no Supervisor):** run the published image
  with Docker, as a stdio server your MCP client starts itself or as a
  long-running HTTP server — see
  [Running without the add-on](#running-without-the-add-on-docker--ha-container).
- **MCP Registry / MCP clients that resolve servers by name:** `server.json`
  in this repo declares the same `ghcr.io/fistacho/nexus-agent` OCI package for
  the [Model Context Protocol Registry](https://modelcontextprotocol.io/registry)
  (publication to the live registry starts at nexus 1.0.0 — see
  `.github/workflows/mcp-registry.yml` and ADR-0005; the image itself is
  pullable from every tagged release regardless).

## Running without the add-on (Docker / HA Container)

For Home Assistant Container (or any HA you reach over the network) there is
no Supervisor, so nexus runs from the same published image,
`ghcr.io/fistacho/nexus-agent:<version>` (amd64 and aarch64), configured with
environment variables instead of the add-on's Configuration tab. You need a
Home Assistant **long-lived access token**: HA → your profile → **Security**
→ **Long-Lived Access Tokens** → **Create token**.

The container picks its mode at start. No `/data/options.json` (the
Supervisor's marker) means standalone, and then:

- **no `NEXUS_HTTP`** → **stdio**: the MCP client starts the container
  itself and talks to it over stdin/stdout (`docker run -i`). Nothing but MCP
  messages is written to stdout; logs go to stderr.
- **`NEXUS_HTTP=1`** → **HTTP**: Streamable HTTP on port 7123 (`/mcp`),
  protected by an API key, for clients on other machines or several clients
  at once.

Without `-i` and without `NEXUS_HTTP=1` the container reads an empty stdin
and exits at once — the usual reason a `docker run -d` "does nothing".

### stdio (simplest: one client on the same machine)

Try it by hand first (the server waits for MCP messages; `Ctrl+C` stops it):

```bash
docker run -i --rm \
  -e HA_URL=http://192.168.1.10:8123 \
  -e HA_TOKEN=your_long_lived_token \
  ghcr.io/fistacho/nexus-agent:<version>
```

Use an HA address that is reachable **from inside the container**
(`homeassistant.local` often does not resolve there; an IP address works).

Claude Desktop (`claude_desktop_config.json`); VS Code (`.vscode/mcp.json`,
top-level key `servers` instead of `mcpServers`) and most other clients take
the same `command`/`args`/`env` shape:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
               "-e", "HA_URL", "-e", "HA_TOKEN", "-e", "NEXUS_READ_ONLY",
               "ghcr.io/fistacho/nexus-agent:<version>"],
      "env": {
        "HA_URL": "http://192.168.1.10:8123",
        "HA_TOKEN": "your_long_lived_token",
        "NEXUS_READ_ONLY": "true"
      }
    }
  }
}
```

`-e NAME` without a value passes the variable through from `env`, so the
token is not part of the `args` list. Claude Code:

```bash
claude mcp add nexus \
  -e HA_URL=http://192.168.1.10:8123 -e HA_TOKEN=your_long_lived_token -e NEXUS_READ_ONLY=true \
  -- docker run -i --rm -e HA_URL -e HA_TOKEN -e NEXUS_READ_ONLY ghcr.io/fistacho/nexus-agent:<version>
```

Each client session starts its own short-lived container. stdio mode uses
no API key: the pipe is reachable only by the client that started it.

### HTTP (long-running server)

```bash
docker run -d --name nexus --restart unless-stopped \
  -p 7123:7123 \
  -e NEXUS_HTTP=1 \
  -e HA_URL=http://192.168.1.10:8123 \
  -e HA_TOKEN=your_long_lived_token \
  -e NEXUS_API_KEY=a_long_random_string \
  ghcr.io/fistacho/nexus-agent:<version>
```

Connect clients to `http://<docker-host>:7123/mcp` with
`Authorization: Bearer <NEXUS_API_KEY>` (per-client snippets:
[README § Connecting MCP Clients](https://github.com/Fistacho/ha-nexus-agent#connecting-mcp-clients)).
Generate the key with e.g. `openssl rand -hex 32` and set it yourself. The
Setup UI shows the key only to a request from localhost, and a request
through Docker's port mapping does not come from localhost. An
auto-generated key is readable only with
`docker exec nexus cat /config/.nexus_api_key` and changes whenever the
container is recreated. `GET /health` answers `{"status":"ok"}` without
auth, for health checks.

### Optional: the HA config folder

`files_*`, `git_*` and the theme file tools read and write Home Assistant's
config folder directly. Mount it if you want them (on HA Container, the
folder you mount into the HA container as `/config`):

```bash
  -v /path/to/homeassistant/config:/config
```

Without the mount those tools see an empty folder inside the container.
Tools that need the Supervisor (`supervisor_*`, and the add-on discovery
behind `esphome_*`, `nodered_*` and `adguard_*`) return an error in standalone mode;
`esphome_*` works if you set `ESPHOME_DASHBOARD_URL`.

### Without Docker: `uvx`

With [uv](https://docs.astral.sh/uv/) installed, nexus also runs straight
from the repository (stdio). Set `HA_CONFIG_PATH`, otherwise nexus uses
`/config` (on Windows `C:\config`) for its key file and file tools:

```bash
HA_URL=http://192.168.1.10:8123 HA_TOKEN=your_long_lived_token HA_CONFIG_PATH=$HOME/.nexus \
  uvx --from git+https://github.com/Fistacho/ha-nexus-agent@v<version> nexus
```

nexus is not a PyPI package yet (ADR-0005), so this installs its modules at
the top level of uv's throwaway environment. Pin a tag (`@v<version>`)
rather than the default branch.

### Environment variables (standalone)

| Variable | Default | Purpose |
| --- | --- | --- |
| `HA_URL` | `http://homeassistant.local:8123` | Home Assistant base URL, reachable from the container |
| `HA_TOKEN` | — (required) | Long-lived access token. A secret: pass it via `env` / `-e NAME`, never commit it |
| `NEXUS_HTTP` | unset (stdio) | `1` = serve Streamable HTTP on `NEXUS_PORT` instead of stdio |
| `NEXUS_API_KEY` | auto-generated | API key for HTTP mode (`Authorization: Bearer ...`) |
| `NEXUS_PORT` | `7123` | HTTP port inside the container |
| `HA_CONFIG_PATH` | `/config` | HA config folder for file/git tools and nexus's key file |
| `NEXUS_READ_ONLY` | `false` | Read-only mode, see below |
| `NEXUS_DISABLED_NAMESPACES` | empty | Comma-separated namespaces to hide, e.g. `supervisor,git` |
| `NEXUS_TOOL_MODE` | `full` | `search` = a small `tools/list` with lookup and call-by-name tools |
| `NEXUS_NODERED_WRITE_ENABLED` | `false` | Allow the `nodered_*` write tools (full trust, ADR-0007) |
| `ESPHOME_DASHBOARD_URL` | — | ESPHome Device Builder URL for `esphome_*` without a Supervisor |

The unprefixed `READ_ONLY`, `DISABLED_NAMESPACES`, `TOOL_MODE` and
`NODERED_WRITE_ENABLED` still work as fallbacks. If both spellings are set,
the safer one wins: read-only is on if either says so, Node-RED writes are
off if either says so, and the two namespace lists are joined.

Verify the image's signature before running it in anything you care about:

```bash
cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/Fistacho/ha-nexus-agent/' \
  ghcr.io/fistacho/nexus-agent:<version>
```

## Read-only mode

The safest way to try nexus: the model can look at everything and change
nothing. Turn it on with the add-on option `read_only: true` (Configuration
tab, then restart) or `NEXUS_READ_ONLY=true` in standalone mode.

- Every tool that is not annotated read-only is **hidden** from `tools/list`
  **and refused** if a client calls it by name anyway. The server enforces
  this, not the client. All MCP prompts are hidden too.
- What stays: tools that only read — states, history, logbook, statistics,
  registries, automation/script/dashboard configs and traces, logs, config
  validation, search and `discover_*` (169 of 354 tools in 0.29.0).
- What goes: everything that can change something — entity control and
  `set_*` tools, the generic `services_call_service`, `ws_call_service` and
  `services_fire_event`, creating, editing or deleting automations,
  dashboards, files, git commits, helpers, areas and labels, add-ons,
  backups, HACS, ESPHome compile/flash, Node-RED and NeonPlan writes,
  notifications, reloads and restarts.
- It fails closed: a tool counts as read-only only if its annotations say
  `readOnlyHint=true`; anything else is hidden. The exact list is pinned and
  reviewed in `tests/contract/read_only_tools.json`, so a new tool cannot
  join it unnoticed.
- The startup log states the active policy, e.g.
  `Tool policy applied: read_only=True ... -> 169/354 tools visible`.
- Read-only limits **changes, not reading**. Visible tools can still read
  `/config` files, git history and add-on logs; add `disabled_namespaces`
  (e.g. `files,git,supervisor`) to narrow what can be read. The setting
  covers the whole process, not one client: to give one client write access
  and another read-only, run two instances.

## Configuring options

Every option below is set on the add-on's **Configuration** tab; changes to
any of them (except `port`, which nexus picks up live) need a **restart** to
take effect. Full descriptions:
[README § Add-on Options](https://github.com/Fistacho/ha-nexus-agent#add-on-options).

| Option | Purpose |
| --- | --- |
| `read_only` | Hide and refuse every tool/prompt that isn't read-only (ADR-0003). Does **not** restrict what an already-visible read tool can read — pair with `disabled_namespaces` for that. |
| `disabled_namespaces` | Hide and refuse entire tool namespaces (e.g. `["supervisor", "git"]`). An unknown name refuses to start with a log error instead of silently no-op'ing. |
| `nodered_write_enabled` | Off by default. Allows the `nodered_*` tools that change flows. **Full trust:** a flow runs any code inside the Node-RED add-on, which holds a Supervisor token with the `manager` role and Node-RED's node secrets (ADR-0007). |
| `tool_mode` | `full` (default) or `search` — `search` swaps `tools/list` for a handful of lookup + call-by-name-proxy tools, for context-constrained clients. Everything it hides still works if called by name and stays subject to `read_only`/`disabled_namespaces`. |
| `api_key` | Pin the MCP API key instead of letting nexus auto-generate one. |
| `git_versioning_auto` | Auto-commit `/config` changes made through nexus's file/git tools. |
| `max_backups` | Supervisor backups nexus keeps before pruning the oldest. |
| `port` | LAN-facing port for `/mcp` + the Setup UI (see **Network**, below). Scheduled for removal in 1.0.0. |
| `log_level` | Add-on's own log verbosity — does not affect what an MCP client sees. |

## Connecting an MCP client (Bearer auth)

Open the add-on's **Web UI** (ingress) to get your real API key and a
ready-to-paste config block per client (Claude Code, Claude Desktop, Gemini
CLI, Codex CLI, VS Code, Cursor, Windsurf). The endpoint is Streamable HTTP at
`http://<host>:7123/mcp`, authenticated with an `Authorization: Bearer
<API_KEY>` header. The `?token=` query parameter was removed in 1.0.0; a
request that still uses it gets `401` with a hint. Codex CLI:
`codex mcp add nexus --url http://<host>:7123/mcp --bearer-token-env-var
NEXUS_API_KEY` (the key is read from that environment variable). Full
per-client snippets:
[README § Connecting MCP Clients](https://github.com/Fistacho/ha-nexus-agent#connecting-mcp-clients).

## Security

- **`host_network: true` (ADR-0004):** required so nexus can reach the
  ESPHome Device Builder add-on's trusted-peer ingress site the same way HA
  Core itself does. This drops the add-on's Supervisor security rating by one
  point — a host-networked container can reach the host's own loopback
  interface, not just its own network namespace. See
  [README § Network (add-on)](https://github.com/Fistacho/ha-nexus-agent#network-add-on).
- **Images are cosign-signed** (keyless, GitHub OIDC) from
  `.github/workflows/publish-image.yml` — verify with the `cosign verify`
  command above before trusting a pulled image.
- **AppArmor:** a custom profile is drafted (`apparmor.txt.draft` in this
  repo) but **not yet active** — see that file's own header for why, and
  ADR-0005 for the planned two-step rollout (test on a live instance first,
  then rename it to `apparmor.txt` in a later release).
- **Setup UI / API key exposure, `/mcp` auth, redacted logs:** see
  [README § Security](https://github.com/Fistacho/ha-nexus-agent#security).
- Found a vulnerability? See [SECURITY.md](SECURITY.md) — private GitHub
  advisory reporting, not a public issue.

## ESPHome

`esphome_*` tools reach the **ESPHome Device Builder** add-on's own
trusted-peer ingress site (`127.0.0.1:<its ingress_port>`), the same path HA
Core uses — this is why `host_network: true` is required (above). Install
ESPHome Device Builder from the Add-on Store; nexus discovers it
automatically via the Supervisor. Standalone/no-Supervisor installs must set
`ESPHOME_DASHBOARD_URL` explicitly instead. Full tool list:
[README § Tools](https://github.com/Fistacho/ha-nexus-agent#tools) (`esphome_*`
row).
