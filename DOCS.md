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
  directly —

  ```bash
  docker run -d --name nexus \
    -p 7123:7123 \
    -e HA_URL=http://homeassistant.local:8123 \
    -e HA_TOKEN=your_long_lived_token \
    ghcr.io/fistacho/nexus-agent:<version>
  ```

  `run.sh` detects the absence of `/data/options.json` (the Supervisor marker)
  and falls back to these environment variables — see
  [README § Standalone](https://github.com/Fistacho/ha-nexus-agent#standalone-outside-ha)
  for getting a token and connecting a client. Verify the image's signature
  before running it in anything you care about:

  ```bash
  cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github\.com/Fistacho/ha-nexus-agent/' \
    ghcr.io/fistacho/nexus-agent:<version>
  ```
- **MCP Registry / MCP clients that resolve servers by name:** `server.json`
  in this repo declares the same `ghcr.io/fistacho/nexus-agent` OCI package for
  the [Model Context Protocol Registry](https://modelcontextprotocol.io/registry)
  (publication to the live registry starts at nexus 1.0.0 — see
  `.github/workflows/mcp-registry.yml` and ADR-0005; the image itself is
  pullable from every tagged release regardless).

## Configuring options

Every option below is set on the add-on's **Configuration** tab; changes to
any of them (except `port`, which nexus picks up live) need a **restart** to
take effect. Full descriptions:
[README § Add-on Options](https://github.com/Fistacho/ha-nexus-agent#add-on-options).

| Option | Purpose |
| --- | --- |
| `read_only` | Hide and refuse every tool/prompt that isn't read-only (ADR-0003). Does **not** restrict what an already-visible read tool can read — pair with `disabled_namespaces` for that. |
| `disabled_namespaces` | Hide and refuse entire tool namespaces (e.g. `["supervisor", "git"]`). An unknown name refuses to start with a log error instead of silently no-op'ing. |
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
`http://<host>:7123/mcp`, authenticated with `Authorization: Bearer
<API_KEY>` (preferred) or a legacy `?token=` query parameter (deprecated,
removed in 1.0.0 — logged once at startup if used, never logs the key
itself). Full per-client snippets:
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
