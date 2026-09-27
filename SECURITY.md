# Security Policy

## Supported Versions

Only the latest published release of `ha-nexus-agent` (Nexus) is supported with
security fixes. Older versions do not receive backports — please update to the
latest release (Home Assistant add-on store, or `git pull` for standalone
installs) before reporting an issue, in case it is already fixed.

| Version | Supported |
| --- | --- |
| latest release | ✅ |
| anything older | ❌ |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

This repository supports **GitHub Private Vulnerability Reporting**. To report a
vulnerability privately:

1. Go to the repository's **Security** tab → **Advisories** → **Report a vulnerability**
   (or use the direct URL: `https://github.com/Fistacho/ha-nexus-agent/security/advisories/new`).
2. Describe the issue: affected version, steps to reproduce, and impact.
3. GitHub will notify the repository maintainer privately; no other users or
   search engines can see the report until it is published as an advisory.

> **Note for the repository owner:** Private Vulnerability Reporting must be
> enabled once under **Settings → Security → Private vulnerability reporting**
> before the flow above works. If it is not enabled yet, please enable it and
> update this note.

If you are unable to use GitHub's private reporting flow, contact:

> **TODO(owner): add a private contact channel (e.g. a dedicated security
> email address) here.** Do not guess or invent one — leave this placeholder
> until the maintainer fills it in.

### What to expect

- **Acknowledgement:** TODO(owner) — target response time (e.g. "within 3
  business days") once decided.
- **Status updates:** TODO(owner) — target cadence while a fix is in progress.
- **Disclosure:** coordinated disclosure once a fix is released; credit given
  to the reporter unless they prefer to stay anonymous.

## Scope

In scope: the `nexus` MCP server itself (`server.py`, `setup_ui.py`, `auth.py`,
`ha_client.py`, `tools/*`) — authentication, path traversal, injection,
unsafe file/media handling, and anything that lets a caller bypass the
add-on's trust boundary (Home Assistant ingress / API key).

Out of scope: vulnerabilities in Home Assistant Core, the Supervisor, or
third-party MCP clients — please report those upstream instead.
