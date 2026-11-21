# Contributing to nexus

Thanks for helping! nexus is a single-maintainer hobby project, so small, focused pull requests
with tests get merged fastest.

- **Questions, ideas, "how do I…":** [GitHub Discussions](https://github.com/Fistacho/ha-nexus-agent/discussions)
- **Bugs and feature requests:** [issues](https://github.com/Fistacho/ha-nexus-agent/issues/new/choose)
- **Security problems:** never in a public issue — see [SECURITY.md](SECURITY.md)

## Development setup

Requirements: Python 3.11+ (CI runs 3.12 and 3.14), git, and a Home Assistant instance for manual
testing (a throwaway one is best).

```bash
git clone https://github.com/Fistacho/ha-nexus-agent
cd ha-nexus-agent
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"         # runtime deps + pytest, pytest-xdist, ruff
```

Run the server against your Home Assistant:

```bash
cp .env.example .env            # set HA_URL and HA_TOKEN (a long-lived access token)
python server.py                # stdio MCP server, for clients that start it themselves
NEXUS_HTTP=1 python server.py   # Streamable HTTP on :7123 + Setup UI at http://localhost:7123
```

Never commit `.env`, tokens or API keys — `.gitignore` already excludes `.env`.

## Tests and lint

```bash
python -m pytest -q -n auto               # full unit suite, same as CI
python -m pytest -q -m "not slow"         # fast loop: skips tests that use real sockets
ruff check .                              # required in CI (ruff 0.16.1, config in pyproject.toml)
```

`tests/` mock Home Assistant; nothing there needs a live instance. `tests_e2e/` boot a real Home
Assistant container in CI (`.github/workflows/e2e.yml`) — you normally do not run them locally.

Rules that keep the suite meaningful:

- Fixing a bug? Add a test that fails without the fix first.
- Don't skip, delete or weaken a test to make CI green — if a test looks wrong, say so in the PR.
- Don't silence checks (`# noqa`, `# type: ignore`, `--no-verify`) without a written reason next to it.

## Adding a tool

Every tool is a function in `tools/<namespace>.py`. Each module has its own `FastMCP` instance that
`server.py` mounts with `mcp.mount(<ns>_mcp, namespace="<ns>")`, so `def list_plans()` in
`tools/neonplan.py` is the MCP tool `neonplan_list_plans`. Tool names are a public API for
models: renaming one is a breaking change.

1. **Find a neighbour.** Copy the style of a similar tool in the same module (`tools/snapshot.py` is
   a good reference). One tool = one action.
2. **Follow the tool contract** (ADR-0002, enforced by `tests/test_tool_contract.py`):

   ```python
   from typing import Annotated
   from pydantic import Field
   from tools._contract import read, write, destructive

   # hypothetical example: tools/widgets.py, exposed as widgets_list_widgets
   @mcp.tool(annotations=read("List widgets"))
   def list_widgets(
       limit: Annotated[int, Field(description="Maximum number of widgets to return; 0 means all.")] = 0,
   ) -> dict:
       """List every widget with its id, name and state.

       Behaviour: what the code really does. Use when: … Not for: … — use <other_tool>.

       Returns: the result shape.
       Errors: when it returns {"error": ...}.
       """
   ```

   - Annotations only via the `read` / `write` / `destructive` presets, chosen by what the code
     actually does.
   - Every parameter is `Annotated[T, Field(description=...)]`; no `Args:` section in the docstring.
   - The first paragraph is one sentence of at most 140 characters (it is what `discover_tool_search`
     shows).
   - Tools are synchronous `def`, do I/O only through `import ha_client as ha`, return JSON-serialisable
     data and report failures as `{"error": ...}`. Never log tokens or personal data.
   - A new module needs the `TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}`
     marker.
3. **New namespace?** Create `tools/<ns>.py` with `mcp = FastMCP("<ns>")`, import and mount it in
   `server.py` next to the others, and check that `python server.py` starts.
4. **Update the API surface deliberately:**

   ```bash
   python tests/contract/generate_tool_surface.py   # regenerate the golden tool surface
   python -m pytest -q tests/test_tool_contract.py
   ```

   The tool and namespace counts are checked in `tests/test_tool_contract.py` (T2) and must match every
   "N tools across M domains" mention in `pyproject.toml`, `config.yaml` and `README.md`. Update them
   together, and add the tool to the table in `README.md` § Tools.
5. **Try it on a live instance** (through your MCP client or by calling the function) before opening
   the PR.

Tools that write or delete: think about `read_only` mode (ADR-0003 — non-read tools are hidden there),
whether a `confirm=True` gate is needed for anything hard to undo, and whether a backup or conflict
check belongs before the write.

## Versions and changelog

Leave version numbers and `CHANGELOG.md` alone in a PR — describe the user-visible change in the PR
description and the maintainer writes the changelog entry and bumps the version at release time.
For reference, a release keeps these in sync (checked by `tests/test_release_consistency.py`, so a
changelog heading without a matching version bump fails CI):

- `version` in `pyproject.toml`
- `version` in `config.yaml` (what the Supervisor compares for "update available")
- the first `## <version>` heading in `CHANGELOG.md`
- `version` and the image tag in `server.json` (MCP Registry)

## Pull requests

- One topic per PR; explain *why* in the description and link the issue or discussion.
- Run `python -m pytest -q -n auto` and `ruff check .` before pushing — CI runs both, plus an image
  build smoke test and the end-to-end suite.
- Code comments and tests refer to design decisions as `ADR-000N` (for example ADR-0002, the tool
  contract, and ADR-0003, read-only mode). If your change goes against one of them, open a
  discussion first.
- By contributing you agree your work is released under the [MIT License](LICENSE).
