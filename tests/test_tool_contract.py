"""Contract test for the MCP tool surface (ADR-0002).

.claude/docs/nexus/adr/0002-kontrakt-narzedzi-mcp.md is the source of truth;
this docstring only summarises what's enforced and how to opt a module in.

Marker convention (ADR-0002 D4)
--------------------------------
A module `tools/<ns>.py` opts into the T3 checks below by declaring, at
module level::

    TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

- `long_ok`: ``{"<full tool name>": "<why the description must exceed 1500
  chars>"}`` — exempts a tool from the T3h upper bound.
- `heuristic_exceptions`: ``{"<full tool name>": "<why the real annotation
  differs from the name-based guess>"}`` — exempts a tool from T3c.

The marker is meant to stay in the module permanently as the place for these
documented exceptions — it is not a temporary flag removed after migration.

Batch Z (ADR-0002 D4/T4): the marker is now mandatory for every module.
`_require_marker()` calls `pytest.fail("contract pending: tools/<ns>.py —
opt in via TOOL_CONTRACT, see ADR-0002 D4")` for any module still missing
it — there is no more skip path and no env var to dry-run the old,
pre-batch-Z behaviour.

What's checked
--------------
Always on, for all 323 tools, regardless of marker:

- T1 (`test_t1_tool_surface_matches_golden`): the live tool surface (names +
  parameter names/types/defaults/required, `description` stripped) equals
  `tests/contract/tool_surface.json`. Any diff fails with the added/removed
  tool names and a per-tool schema diff. Regenerate only after a deliberate,
  reviewed API change: `python tests/contract/generate_tool_surface.py`.
- T2 (`test_t2_tool_and_namespace_counts`): `len(list_tools())` and the
  number of distinct namespaces mounted in `server.py` (`mcp.mount(...,
  namespace=...)`) agree with each other and with every "N tools across M
  {domains,categories}" mention in `pyproject.toml`, `config.yaml` and
  `README.md`.

Required per module (batch Z: a missing `TOOL_CONTRACT` marker fails, no
longer skips):

- T3a: `annotations` present; all four hints (`readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`) are explicit `bool`;
  `title` non-empty and <=60 chars.
- T3b: `readOnlyHint=True` implies `destructiveHint=False` and
  `idempotentHint=True`.
- T3c: cross-check against a name heuristic (`list_`/`get_`/... expected
  read-only; `delete_`/`remove_`/... expected destructive), overridable via
  `TOOL_CONTRACT["heuristic_exceptions"]`.
- T3d: every `inputSchema.properties.*` has a `description` of >=10 chars
  that isn't just the parameter name.
- T3e: `tool.description == inspect.getdoc(fn)` — nothing was silently
  dropped by FastMCP's docstring parser.
- T3f: docstring has no parameter section left (`Args:`, `Arguments:`,
  `Params:`, `Parameters:`, `Keyword Args:`, `:param:`, NumPy underline).
- T3g: first paragraph (discover._summarise's unit) is one sentence and
  <=140 chars.
- T3h: description length in [120, 1500], unless listed in `long_ok`.
- T3i: `Returns:` present; `Errors:` present when the function raises or
  returns `{"error": ...}`.
- T3k: tools in one of the ADR-0002 overlap groups mention each other's full
  name in their description (`Not for`), checked only once both tools'
  modules carry the marker.

T3j (denylist: marketing words, "Step N", private examples, ALL-CAPS
emphasis outside WARNING/DANGEROUS) is folded into `_check_t3j_denylist` and
run from `test_t3j_denylist`, same marker requirement as the others.

Every T3x test is parametrized over all 323 tool names (`ids=tool name`), so
a single failing tool shows up as a single failing test id, e.g.
`test_t3d_parameter_descriptions[supervisor_delete_backup]`.
"""
from __future__ import annotations

import importlib
import inspect
import json
import re
from difflib import unified_diff
from pathlib import Path
from typing import Any

import pytest
from contract import generate_tool_surface as golden

_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN_FILE = Path(__file__).resolve().parent / "contract" / "tool_surface.json"

# --- T4 switch -------------------------------------------------------------
# Batch Z (ADR-0002 D4/T4, final flip): every module must carry the
# TOOL_CONTRACT marker now — a missing marker is a hard failure, not a skip.
# Unconditional per the ADR ("brak zależności od env"); the old
# `NEXUS_CONTRACT_ENFORCE_ALL=1` dry-run env var is now permanently baked in
# and no longer read.
_T4_ENFORCE_ALL_MODULES = True

# --- collect the live tool surface once, at collection time ----------------
# (pytest.mark.parametrize needs the tool-name list up front.)
_SERVER_TOOLS: dict[str, Any] = golden.collect_server_tools()
_MODULE_NAMESPACE: dict[str, str] = golden.module_namespace_map()
_NAMESPACE_MODULE: dict[str, str] = {ns: mod for mod, ns in _MODULE_NAMESPACE.items()}
_NAMESPACES_BY_LEN = sorted(_NAMESPACE_MODULE, key=len, reverse=True)

_ALL_NAMES: list[str] = sorted(_SERVER_TOOLS)


def _namespace_for(tool_name: str) -> str:
    for ns in _NAMESPACES_BY_LEN:
        if tool_name.startswith(ns + "_"):
            return ns
    raise AssertionError(f"tool '{tool_name}' matches no known namespace prefix {sorted(_NAMESPACE_MODULE)}")


def _module_function_tools(module_name: str) -> dict[str, Any]:
    """{local tool name: FunctionTool} for one `tools/<module>.py`, incl. `.fn`."""
    import asyncio

    module = importlib.import_module(f"tools.{module_name}")
    tools = asyncio.run(module.mcp.list_tools())
    return {t.name: t for t in tools}


_MODULE_OBJS: dict[str, Any] = {
    mod: importlib.import_module(f"tools.{mod}") for mod in _MODULE_NAMESPACE
}
_MODULE_TOOLS: dict[str, dict[str, Any]] = {
    mod: _module_function_tools(mod) for mod in _MODULE_NAMESPACE
}


def _fn_for(tool_name: str) -> Any:
    ns = _namespace_for(tool_name)
    module_name = _NAMESPACE_MODULE[ns]
    local_name = tool_name[len(ns) + 1 :]
    return _MODULE_TOOLS[module_name][local_name].fn


def _marker_for(tool_name: str) -> dict | None:
    ns = _namespace_for(tool_name)
    module_name = _NAMESPACE_MODULE[ns]
    return getattr(_MODULE_OBJS[module_name], "TOOL_CONTRACT", None)


def _require_marker(tool_name: str) -> dict:
    """Return the module's TOOL_CONTRACT marker, or skip/fail per T4."""
    marker = _marker_for(tool_name)
    if marker is not None:
        return marker
    ns = _namespace_for(tool_name)
    module_name = _NAMESPACE_MODULE[ns]
    reason = (
        f"contract pending: tools/{module_name}.py has no TOOL_CONTRACT marker "
        "(opt-in per ADR-0002 D4)"
    )
    if _T4_ENFORCE_ALL_MODULES:
        pytest.fail(reason)
    pytest.skip(reason)


# ---------------------------------------------------------------------------
# T1 — golden file
# ---------------------------------------------------------------------------


def _format_mismatches(mismatches: list[tuple[str, Any, Any]]) -> str:
    lines = []
    for name, golden_schema, current_schema in mismatches:
        golden_text = json.dumps(golden_schema, indent=2, sort_keys=True).splitlines()
        current_text = json.dumps(current_schema, indent=2, sort_keys=True).splitlines()
        diff = unified_diff(golden_text, current_text, fromfile="golden", tofile="current", lineterm="")
        lines.append(f"--- {name} ---")
        lines.extend(diff)
    return "\n".join(lines)


def test_t1_tool_surface_matches_golden():
    golden_data = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))
    golden_tools = golden_data["tools"]

    current_names = set(_SERVER_TOOLS)
    golden_names = set(golden_tools)
    added = sorted(current_names - golden_names)
    removed = sorted(golden_names - current_names)
    assert not added and not removed, (
        "Tool surface changed vs the golden file "
        f"{_GOLDEN_FILE}.\nAdded: {added}\nRemoved: {removed}\n"
        "Names/parameters are public API (workspace CLAUDE.md). If this is a "
        "deliberate, reviewed change, regenerate with:\n"
        "  python tests/contract/generate_tool_surface.py"
    )

    mismatches = []
    for name in sorted(current_names):
        current_schema = golden.strip_descriptions(_SERVER_TOOLS[name].inputSchema)
        golden_schema = golden_tools[name]["inputSchema"]
        if current_schema != golden_schema:
            mismatches.append((name, golden_schema, current_schema))
    assert not mismatches, (
        "Parameter schema changed (types/defaults/required) vs the golden file "
        "for the tools below. If deliberate and reviewed, regenerate with "
        "`python tests/contract/generate_tool_surface.py`:\n" + _format_mismatches(mismatches)
    )


# ---------------------------------------------------------------------------
# T2 — tool/namespace count consistency
# ---------------------------------------------------------------------------

_COUNT_MENTION_RE = re.compile(r"(\d+)\s+(?:MCP\s+)?tools\b[^\d]{0,40}?(\d+)\s+(?:domains|categories)", re.IGNORECASE)


def test_t2_tool_and_namespace_counts():
    tool_count = len(_SERVER_TOOLS)
    namespace_count = len(set(_MODULE_NAMESPACE.values()))
    server_py_text = (_ROOT / "server.py").read_text(encoding="utf-8")
    literal_mount_count = golden.mount_call_count(server_py_text)

    assert namespace_count == literal_mount_count, (
        f"tools.<ns>.py -> namespace mapping found {namespace_count} distinct namespaces "
        f"but server.py has {literal_mount_count} `mcp.mount(` calls — a namespace is "
        "reused or the regex in generate_tool_surface.module_namespace_map() is stale."
    )
    assert tool_count == 323, (
        f"list_tools() returned {tool_count} tools, expected 323 "
        "(update this literal alongside pyproject.toml/config.yaml/README.md "
        "in the same reviewed change, then regenerate tests/contract/tool_surface.json)."
    )
    assert namespace_count == 29, f"expected 29 namespaces, found {namespace_count}"

    files = [
        _ROOT / "pyproject.toml",
        _ROOT / "config.yaml",
        _ROOT / "README.md",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        mentions = _COUNT_MENTION_RE.findall(text)
        assert mentions, f"{path.name}: found no 'N tools across M {{domains,categories}}' mention to check"
        for n_tools, n_ns in mentions:
            assert int(n_tools) == tool_count, (
                f"{path.name} says {n_tools} tools, but list_tools() returns {tool_count}"
            )
            assert int(n_ns) == namespace_count, (
                f"{path.name} says {n_ns} domains/categories, but server.py mounts {namespace_count}"
            )


# ---------------------------------------------------------------------------
# T3 — per-tool checks, opt-in via TOOL_CONTRACT marker
# ---------------------------------------------------------------------------

_R1_PREFIXES = ("list_", "get_", "search_", "find_", "render_", "validate_", "check_", "ping_")
_DESTRUCTIVE_NAME_RE = re.compile(r"^(delete_|remove_|uninstall_|restore_|rollback|restart_|stop_|save_|write_|update_|set_\w+_config)")

_PARAM_SECTION_RE = re.compile(
    r"^[ \t]*(Args|Arguments|Params|Parameters|Keyword Args)\s*:\s*$"
    r"|:param\s+\w+:"
    r"|^[ \t]*Parameters[ \t]*\n[ \t]*-{3,}[ \t]*$",
    re.MULTILINE,
)

_MID_SENTENCE_RE = re.compile(r"\.\s+[A-Z]")

_MARKETING_RE = re.compile(r"\b(powerful|best|most complete|seamless|awesome)\b", re.IGNORECASE)
_STEP_RE = re.compile(r"\bStep\s+\d+\b", re.IGNORECASE)
_PRIVATE_NAME_RE = re.compile(r"\blukasz\b", re.IGNORECASE)
_LAN_IP_RE = re.compile(r"\b192\.168\.")
_CAPS_WORD_RE = re.compile(r"\b[A-Z]{4,}\b")
# Technical acronyms are not "emphasis" in the sense the ADR denylist targets
# (marketing/shouting) — only WARNING/DANGEROUS are contractual per D3; this
# allowlist exists purely to keep T3j from flagging normal HA/MCP vocabulary.
_ALLOWED_CAPS = {
    "WARNING", "DANGEROUS", "HTTP", "HTTPS", "URL", "URLS", "JSON", "YAML",
    "HACS", "MQTT", "TTS", "STT", "LVGL", "CSS", "SVG", "HTML", "API", "APIS",
    "ID", "IDS", "WS", "BM25", "UI", "UUID", "OAUTH", "GET", "POST", "PUT",
    "DELETE", "PATCH", "CRUD", "TODO", "LTS", "SQL", "MCP", "HA", "OTA",
    "LAN", "ESPHOME", "KELVIN",
}

_OVERLAP_GROUPS: list[tuple[str, ...]] = [
    ("services_call_service", "ws_call_service"),
    ("services_render_template", "ws_render_template"),
    ("system_list_integrations", "system_get_all_integrations"),
    ("history_get_ha_config", "ws_get_config"),
    ("history_get_system_info", "system_ping_ha"),
    ("system_create_backup", "supervisor_create_backup"),
    ("system_restart_ha", "supervisor_restart_core"),
    ("files_write_config_file", "git_safe_write_with_checkpoint", "esphome_write_config"),
    ("entities_bulk_control", "entities_bulk_set_state"),
    ("esphome_get_addon_logs", "supervisor_get_addon_logs"),
    ("areas_list_devices", "devices_list_devices", "search_search_devices", "devices_list_devices_in_area"),
    ("ws_get_states", "entities_list_entities"),
    ("statistics_get_statistics", "history_get_state_history"),
    ("statistics_list_statistic_ids", "history_get_state_history"),
    ("snapshot_get_area_snapshot", "areas_get_area_entities", "areas_get_area_states"),
    # Batch Z (task 4): added because the code already carries reciprocal
    # "Not for" mentions on both sides (verified in tools/dashboards.py before
    # adding). Other candidate pairs from the same review were one-sided in
    # the code today and are deliberately NOT added here — see the batch Z
    # report instead of editing tools/ from this test file.
    ("dashboards_add_card_to_view", "dashboards_add_card_to_section"),
    # areas_control_area <-> entities_bulk_control: reciprocal "Not for"
    # mentions verified in tools/areas.py::control_area (line ~162) and
    # tools/entities.py::bulk_control (line ~406) before adding this pair.
    ("areas_control_area", "entities_bulk_control"),
]
_OVERLAP_PARTNERS: dict[str, list[str]] = {}
for _group in _OVERLAP_GROUPS:
    for _name in _group:
        _OVERLAP_PARTNERS.setdefault(_name, []).extend(n for n in _group if n != _name)


def _first_paragraph(description: str) -> str:
    # Same split discover._summarise uses (first blank-line-separated block,
    # newlines collapsed) — kept in sync deliberately, see T3g.
    first = (description or "").strip().split("\n\n", 1)[0]
    return first.replace("\n", " ").strip()


# Some modules give (some of) their tools' local names a "subgroup" prefix
# that repeats before the verb: `git_ops` mounts as namespace "git", but every
# local name is already `git_<verb>_...` (e.g. `git_rollback_file`, full tool
# name `git_git_rollback_file`); `esphome`'s `lvgl_*` tools carry an explicit
# `lvgl_` subgroup unrelated to the "esphome" namespace (e.g.
# `lvgl_delete_widget`, full tool name `esphome_lvgl_delete_widget`). A verb
# prefix like `list_`/`delete_`/`rollback` then never lands at the start of
# `local_name`, and `_heuristic_expectation` alone silently says nothing.
# `git_` is also covered generically below (it equals the namespace), but is
# kept here too so the intent is explicit and doesn't rely on that coincidence.
_KNOWN_SUBGROUP_PREFIXES = ("git_", "lvgl_")


def _heuristic_expectation(local_name: str) -> str | None:
    if any(local_name.startswith(p) for p in _R1_PREFIXES):
        return "read_only"
    if _DESTRUCTIVE_NAME_RE.match(local_name):
        return "destructive"
    return None


def _subgroup_stripped_name(local_name: str, ns: str) -> str | None:
    """`local_name` with a known subgroup prefix removed, or None if none applies.

    Tries the namespace's own name as a prefix (the `git_ops` case) and the
    explicit `_KNOWN_SUBGROUP_PREFIXES` list (the `lvgl_` case), in that
    order. Never returns an empty string (a local name that IS just the
    prefix, e.g. hypothetical `git_git`, has nothing left to match a verb
    against).
    """
    for prefix in (f"{ns}_", *_KNOWN_SUBGROUP_PREFIXES):
        if local_name.startswith(prefix):
            rest = local_name[len(prefix):]
            if rest:
                return rest
    return None


def _heuristic_expectation_with_subgroups(local_name: str, ns: str) -> str | None:
    """`_heuristic_expectation`, falling back to a subgroup-prefix-stripped name.

    The plain name is tried first so a tool whose full verb already matches
    (e.g. `list_...`) isn't reinterpreted through a coincidental subgroup
    strip.
    """
    expectation = _heuristic_expectation(local_name)
    if expectation is not None:
        return expectation
    stripped = _subgroup_stripped_name(local_name, ns)
    if stripped is None:
        return None
    return _heuristic_expectation(stripped)


def check_t3a_annotations(tool) -> list[str]:
    problems: list[str] = []
    ann = tool.annotations
    if ann is None:
        return ["annotations is None, expected an explicit ToolAnnotations (see tools/_contract.py presets)"]
    for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        val = getattr(ann, hint, None)
        if not isinstance(val, bool):
            problems.append(f"{hint} is {val!r}, expected an explicit bool")
    title = tool.title or getattr(ann, "title", None)
    if not title:
        problems.append("title is empty")
    elif len(title) > 60:
        problems.append(f"title is {len(title)} chars, must be <=60")
    return problems


def check_t3b_readonly_consistency(tool) -> list[str]:
    ann = tool.annotations
    if ann is None:
        return []  # already reported by T3a
    problems: list[str] = []
    if ann.readOnlyHint is True:
        if ann.destructiveHint is not False:
            problems.append("readOnlyHint=True but destructiveHint != False")
        if ann.idempotentHint is not True:
            problems.append("readOnlyHint=True but idempotentHint != True")
    return problems


def check_t3c_heuristic(tool_name: str, local_name: str, ns: str, tool, marker: dict | None) -> list[str]:
    ann = tool.annotations
    if ann is None:
        return []  # already reported by T3a
    expectation = _heuristic_expectation_with_subgroups(local_name, ns)
    if expectation is None:
        return []
    exceptions = (marker or {}).get("heuristic_exceptions", {})
    if tool_name in exceptions:
        return []
    problems: list[str] = []
    if expectation == "read_only" and ann.readOnlyHint is not True:
        problems.append(
            f"name '{local_name}' matches the read-only heuristic but readOnlyHint={ann.readOnlyHint!r}; "
            f"add TOOL_CONTRACT['heuristic_exceptions']['{tool_name}'] if intentional"
        )
    if expectation == "destructive" and ann.destructiveHint is not True:
        problems.append(
            f"name '{local_name}' matches the destructive heuristic but destructiveHint={ann.destructiveHint!r}; "
            f"add TOOL_CONTRACT['heuristic_exceptions']['{tool_name}'] if intentional"
        )
    return problems


def check_t3d_param_descriptions(tool) -> list[str]:
    problems: list[str] = []
    props = (tool.inputSchema or {}).get("properties", {})
    for pname, pschema in props.items():
        desc = pschema.get("description")
        if not desc or len(desc.strip()) < 10:
            problems.append(f"parameter '{pname}' has no description (or <10 chars)")
        elif desc.strip().lower().rstrip(".") == pname.lower():
            problems.append(f"parameter '{pname}' description just repeats the name")
    return problems


def check_t3e_description_matches_docstring(tool, fn) -> list[str]:
    doc = inspect.getdoc(fn) or ""
    if tool.description != doc:
        return [
            (
                "tool.description != inspect.getdoc(fn) — FastMCP's docstring parser dropped/altered "
                "text (a leftover Args:/Parameters: section is the usual cause, see T3f)"
            )
        ]
    return []


def check_t3f_no_param_section(fn) -> list[str]:
    doc = inspect.getdoc(fn) or ""
    if _PARAM_SECTION_RE.search(doc):
        return [
            (
                "docstring still has a parameter section (Args:/Arguments:/Params:/Parameters:/"
                "Keyword Args:/:param:/NumPy underline) — move it into Annotated[..., Field(description=...)]"
            )
        ]
    return []


def check_t3g_summary_sentence(tool) -> list[str]:
    para = _first_paragraph(tool.description or "")
    problems: list[str] = []
    if len(para) > 140:
        problems.append(
            f"first paragraph is {len(para)} chars, must be <=140 "
            "(discover._summarise truncates it silently in tool_search otherwise)"
        )
    body = para.removesuffix(".")
    if _MID_SENTENCE_RE.search(body):
        problems.append("first paragraph reads like more than one sentence (embedded '. X')")
    return problems


def check_t3h_length(tool_name: str, tool, marker: dict | None) -> list[str]:
    n = len(tool.description or "")
    if n < 120:
        return [f"description is {n} chars, must be >=120"]
    long_ok = (marker or {}).get("long_ok", {})
    if n > 1500 and tool_name not in long_ok:
        return [
            (
                f"description is {n} chars, must be <=1500 "
                f"(or add TOOL_CONTRACT['long_ok']['{tool_name}'] with a justification)"
            )
        ]
    return []


def check_t3i_returns_errors(tool, fn) -> list[str]:
    doc = tool.description or ""
    problems: list[str] = []
    if "Returns:" not in doc:
        problems.append("docstring has no 'Returns:' section")
    try:
        src = inspect.getsource(fn)
    except OSError:
        src = ""
    needs_errors = bool(re.search(r"\braise\b|[\"']error[\"']\s*:", src))
    if needs_errors and "Errors:" not in doc:
        problems.append("function raises or returns {'error': ...} but docstring has no 'Errors:' section")
    return problems


def check_t3j_denylist(tool) -> list[str]:
    doc = tool.description or ""
    problems: list[str] = []
    if _MARKETING_RE.search(doc):
        problems.append("marketing language found (powerful/best/most complete/seamless/awesome)")
    if _STEP_RE.search(doc):
        problems.append("multi-step choreography found ('Step N')")
    if _PRIVATE_NAME_RE.search(doc):
        problems.append("private example name found ('lukasz')")
    if _LAN_IP_RE.search(doc):
        problems.append("private LAN IP example found (192.168.*)")
    for word in _CAPS_WORD_RE.findall(doc):
        if word not in _ALLOWED_CAPS:
            problems.append(f"ALL-CAPS emphasis word '{word}' outside the WARNING/DANGEROUS allowlist")
    return problems


def check_t3k_overlap_not_for(tool_name: str, tool, ready_partners: list[str]) -> list[str]:
    doc = tool.description or ""
    problems: list[str] = []
    for partner in ready_partners:
        if partner not in doc:
            problems.append(f"overlap group: description must mention '{partner}' in a 'Not for' line")
    return problems


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3a_annotations_are_explicit(tool_name):
    _require_marker(tool_name)
    problems = check_t3a_annotations(_SERVER_TOOLS[tool_name])
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3b_readonly_implies_flags(tool_name):
    _require_marker(tool_name)
    problems = check_t3b_readonly_consistency(_SERVER_TOOLS[tool_name])
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3c_matches_name_heuristic(tool_name):
    marker = _require_marker(tool_name)
    ns = _namespace_for(tool_name)
    local_name = tool_name[len(ns) + 1 :]
    problems = check_t3c_heuristic(tool_name, local_name, ns, _SERVER_TOOLS[tool_name], marker)
    assert not problems, "; ".join(problems)


# --- T3c subgroup-prefix gap: regression coverage (batch Z, task 3) --------
#
# `git_ops` mounts as namespace "git", but every local tool name already
# starts with "git_" (`git_rollback_file`, full tool name
# `git_git_rollback_file`); `esphome`'s `lvgl_*` tools carry an explicit
# `lvgl_` subgroup unrelated to the "esphome" namespace (`lvgl_delete_widget`,
# full tool name `esphome_lvgl_delete_widget`). In both cases the verb
# (`rollback`, `delete_`) never lands at the start of `local_name`, so the
# plain heuristic used to say nothing about them — a wrong annotation would
# have gone undetected. These tests prove the gap existed and that
# `_heuristic_expectation_with_subgroups` closes it.


def test_plain_heuristic_missed_git_rollback_file_before_this_batch():
    """Documents the gap this batch closes: the plain, non-subgroup-aware
    heuristic returns None for `git_rollback_file` — 'rollback' is buried
    after the repeated 'git_' prefix — so it would not have caught a wrong
    annotation on `git_git_rollback_file`."""
    assert _heuristic_expectation("git_rollback_file") is None


def test_plain_heuristic_missed_lvgl_delete_widget_before_this_batch():
    assert _heuristic_expectation("lvgl_delete_widget") is None


def test_extended_heuristic_recovers_git_rollback_file_as_destructive():
    assert _heuristic_expectation_with_subgroups("git_rollback_file", "git") == "destructive"


def test_extended_heuristic_recovers_lvgl_delete_widget_as_destructive():
    assert _heuristic_expectation_with_subgroups("lvgl_delete_widget", "esphome") == "destructive"


def test_extended_heuristic_still_prefers_a_directly_matching_verb():
    """A local name that already matches a verb prefix on its own must not be
    reinterpreted through a coincidental subgroup strip."""
    assert _heuristic_expectation_with_subgroups("list_devices", "git") == "read_only"


def test_t3c_extended_heuristic_catches_git_rollback_file_misannotated_as_read(monkeypatch):
    """RED-by-design proof: `git_git_rollback_file` is correctly annotated
    destructive today (sanity-checked below). Monkeypatch its *real* live
    annotations to a wrong read-only classification (simulating the kind of
    regression T3c exists to catch) and confirm `check_t3c_heuristic` now
    fails it — which it would NOT have done before this batch, since the
    plain heuristic is silent on `git_rollback_file` (see the tests above)."""
    tool = _SERVER_TOOLS["git_git_rollback_file"]
    assert tool.annotations.destructiveHint is True  # sanity: real annotation is correct today
    assert tool.annotations.readOnlyHint is False

    monkeypatch.setattr(tool.annotations, "readOnlyHint", True)
    monkeypatch.setattr(tool.annotations, "destructiveHint", False)
    monkeypatch.setattr(tool.annotations, "idempotentHint", True)

    problems = check_t3c_heuristic("git_git_rollback_file", "git_rollback_file", "git", tool, marker=None)

    assert problems, "extended heuristic must flag git_git_rollback_file mis-annotated as read-only"


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3d_parameter_descriptions(tool_name):
    _require_marker(tool_name)
    problems = check_t3d_param_descriptions(_SERVER_TOOLS[tool_name])
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3e_description_matches_docstring(tool_name):
    _require_marker(tool_name)
    problems = check_t3e_description_matches_docstring(_SERVER_TOOLS[tool_name], _fn_for(tool_name))
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3f_no_parameter_section_in_docstring(tool_name):
    _require_marker(tool_name)
    problems = check_t3f_no_param_section(_fn_for(tool_name))
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3g_summary_is_one_short_sentence(tool_name):
    _require_marker(tool_name)
    problems = check_t3g_summary_sentence(_SERVER_TOOLS[tool_name])
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3h_description_length(tool_name):
    marker = _require_marker(tool_name)
    problems = check_t3h_length(tool_name, _SERVER_TOOLS[tool_name], marker)
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3i_returns_and_errors_sections(tool_name):
    _require_marker(tool_name)
    problems = check_t3i_returns_errors(_SERVER_TOOLS[tool_name], _fn_for(tool_name))
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", _ALL_NAMES, ids=_ALL_NAMES)
def test_t3j_denylist(tool_name):
    _require_marker(tool_name)
    problems = check_t3j_denylist(_SERVER_TOOLS[tool_name])
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tool_name", sorted(_OVERLAP_PARTNERS), ids=sorted(_OVERLAP_PARTNERS))
def test_t3k_overlap_groups_cross_reference(tool_name):
    _require_marker(tool_name)
    ready_partners = [p for p in _OVERLAP_PARTNERS[tool_name] if _marker_for(p) is not None]
    if not ready_partners:
        pytest.skip(f"no partner of '{tool_name}' has a TOOL_CONTRACT marker yet")
    problems = check_t3k_overlap_not_for(tool_name, _SERVER_TOOLS[tool_name], ready_partners)
    assert not problems, "; ".join(problems)
