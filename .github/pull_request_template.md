## What and why

<!-- What does this change and which problem does it solve? Link the issue or discussion. -->

## Type

- [ ] Bug fix
- [ ] New tool / namespace
- [ ] Change to an existing tool (name, parameters or behaviour)
- [ ] Docs only
- [ ] Build / CI

## Checklist

- [ ] `python -m pytest -q -n auto` passes
- [ ] `ruff check .` passes
- [ ] Bug fix: a test that failed before the fix is included
- [ ] New or changed tool: follows the tool contract in CONTRIBUTING.md, `tests/contract/tool_surface.json` regenerated, tool counts and the README table updated
- [ ] Writes or deletes: hidden in `read_only` mode, `confirm=True` gate considered for anything hard to undo
- [ ] Tried against a real Home Assistant (version: ____ )
- [ ] No tokens, API keys, IP addresses or personal data in code, tests, logs or screenshots
- [ ] Version numbers and CHANGELOG.md left to the maintainer (see CONTRIBUTING.md)
