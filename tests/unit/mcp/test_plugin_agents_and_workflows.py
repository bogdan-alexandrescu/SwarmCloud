"""The plugin's agents and its workflow, checked against what Claude Code loads.

Owner decision, 2026-09-26: a Claude Code workflow shows as running while every
step executes in SwarmCloud. Two surfaces carry it, and both are files Claude
Code reads with rules that fail QUIETLY:

* `plugin/agents/*.md` -- `sc:remote`, `sc:step`, `sc:workflow`. A plugin
  agent whose frontmatter does not parse still loads, with EVERY field ignored
  -- no model pin, no tool restriction -- and `mcpServers`, `permissionMode`,
  `hooks` and `initialPrompt` are ignored in a plugin agent even when it
  parses (Claude Code's plugin components reference, "Frontmatter fields in
  plugin agents"). A `tools` entry that names nothing grants nothing, and an
  agent none of whose entries resolve does not start.
* `plugin/workflows/run.js` -- `/sc:run`. Its `meta` must be a pure literal or
  the command drops out of `/` autocomplete; the body runs with no filesystem,
  no imports, and `Date.now()` / `Math.random()` throwing.

So each rule is asserted here rather than proof-read, and `run.js` is also RUN
-- under node, with the workflow globals stubbed -- to prove what it does with
a spec: which agents it starts, under which phase and label, what it logs, and
that it derives no state SwarmCloud owns.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_mcp import server

from test_plugin_skills import _PREFIX, _scoped_prefix

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_AGENTS = sorted((_PLUGIN / "agents").glob("*.md"))
_RUN_JS = _PLUGIN / "workflows" / "run.js"
_REAL = {tool["name"] for tool in server.TOOLS}
_PREFIXES = (_PREFIX, _scoped_prefix())

#: What Claude Code reads from a PLUGIN agent's frontmatter -- the plugin
#: components reference's "Supported fields" list. `experimental` is there for
#: its `cacheTtl` key only.
SUPPORTED_KEYS = frozenset(
    {
        "name", "description", "model", "effort", "maxTurns", "tools",
        "disallowedTools", "skills", "memory", "background", "omitClaudeMd",
        "isolation", "color", "experimental",
    }
)

#: Ignored in a plugin agent. Listed so a failure says WHY the key is wrong: it
#: would parse, and do nothing.
IGNORED_IN_PLUGIN_AGENTS = frozenset({"permissionMode", "hooks", "mcpServers", "initialPrompt"})

#: The SwarmCloud tools each agent needs, and no more. `sc:step` only watches;
#: `sc:workflow` submits and reads; `sc:remote` is the one that dispatches.
EXPECTED_TOOLS = {
    "remote": {"swarm_dispatch", "swarm_follow"},
    "step": {"swarm_follow"},
    "workflow": {"swarm_workflow", "swarm_workflow_status"},
}

#: The globals a workflow script's body may use: the workflow runtime's own
#: (the workflow-authoring reference) and the plain JavaScript built-ins.
WORKFLOW_GLOBALS = frozenset({"agent", "pipeline", "parallel", "phase", "log", "args", "budget", "workflow"})
JS_BUILTINS = frozenset(
    {"JSON", "Math", "String", "Number", "Array", "Object", "Error", "Boolean", "isFinite", "isNaN",
     "parseInt", "parseFloat", "Promise", "Set", "Map", "undefined", "Infinity", "NaN"}
)
JS_KEYWORDS = frozenset(
    {"if", "else", "for", "of", "in", "const", "let", "var", "function", "return", "await", "async",
     "try", "catch", "finally", "throw", "new", "typeof", "instanceof", "true", "false", "null",
     "export", "while", "do", "break", "continue", "switch", "case", "default", "delete", "void"}
)


# --------------------------------------------------------------------------
# Frontmatter, parsed as strictly as the host parses it
# --------------------------------------------------------------------------


def _split(text: str) -> tuple[list[str], str]:
    lines = text.split("\n")
    assert lines and lines[0].strip() == "---", "frontmatter must open with ---"
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], "\n".join(lines[index + 1:])
    raise AssertionError("frontmatter opened with --- and never closed")


def _scalar(raw: str, key: str) -> object:
    """A YAML plain or quoted scalar, restricted to what cannot be misread.

    `: ` makes an unquoted scalar a mapping, and ` #` starts a comment, in a
    real YAML parser -- the host's. Either would load a DIFFERENT value there
    than here, and a plugin agent whose frontmatter fails to parse loads with
    every field ignored.
    """
    text = raw.strip()
    if text[:1] in "\"'":
        assert text[-1:] == text[:1] and len(text) >= 2, f"{key}: unterminated quote"
        return text[1:-1]
    assert ": " not in text, f"{key}: an unquoted scalar may not contain ': ' -- quote it"
    assert " #" not in text, f"{key}: ' #' starts a YAML comment -- quote it"
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _frontmatter(text: str) -> dict[str, object]:
    fields: dict[str, object] = {}
    key: str | None = None
    for raw in _split(text)[0]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")):
            item = raw.strip()
            assert item.startswith("- "), f"not a list item: {raw!r}"
            assert key is not None and isinstance(fields.get(key), list), f"list item without a list: {raw!r}"
            fields[key].append(_scalar(item[2:], key))
            continue
        assert ":" in raw, f"not `key: value`: {raw!r}"
        key, _, value = raw.partition(":")
        key = key.strip()
        assert key and key not in fields, f"empty or repeated key: {raw!r}"
        fields[key] = _scalar(value, key) if value.strip() else []
    return fields


def _load(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    return _frontmatter(text), _split(text)[1]


def _granted(entries) -> dict[str, set[str]]:
    return {
        prefix: {e[len(prefix):] for e in entries if isinstance(e, str) and e.startswith(prefix)}
        for prefix in _PREFIXES
    }


def _plugin_name() -> str:
    return json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())["name"]


# --------------------------------------------------------------------------
# The agents
# --------------------------------------------------------------------------


def test_the_plugin_ships_the_three_agents_the_workflows_name():
    assert {p.stem for p in _AGENTS} == set(EXPECTED_TOOLS), (
        f"expected agents {sorted(EXPECTED_TOOLS)} under {_PLUGIN / 'agents'}, found "
        f"{sorted(p.stem for p in _AGENTS)}"
    )


@pytest.mark.parametrize("path", _AGENTS, ids=lambda p: p.stem)
def test_an_agents_frontmatter_parses_with_only_keys_a_plugin_agent_honours(path):
    fields, body = _load(path)
    ignored = sorted(set(fields) & IGNORED_IN_PLUGIN_AGENTS)
    assert not ignored, f"{path.name} sets {ignored}, which Claude Code IGNORES in a plugin agent"
    unknown = sorted(set(fields) - SUPPORTED_KEYS)
    assert not unknown, f"{path.name} sets {unknown}, which a plugin agent does not read"
    assert fields.get("name") == path.stem, "name must match the file, which is the agent's scoped name"
    assert ":" not in str(fields["name"]), "':' is reserved for the plugin scope; the file would not load"
    assert isinstance(fields.get("description"), str) and len(fields["description"]) > 40
    assert body.strip(), "an agent with no body has no instructions"


@pytest.mark.parametrize("path", _AGENTS, ids=lambda p: p.stem)
def test_an_agent_is_pinned_to_haiku_at_low_effort(path):
    """A row that only relays a remote task needs no larger model. The pin is
    on the agent, so the workflow script names no model of its own."""
    fields, _ = _load(path)
    assert fields.get("model") == "haiku"
    assert fields.get("effort") == "low"
    assert fields.get("omitClaudeMd") is True, (
        "the user's CLAUDE.md is instructions for a session, not for a relay; loaded "
        "into every row it costs tokens on every call and can countermand the agent"
    )


@pytest.mark.parametrize("path", _AGENTS, ids=lambda p: p.stem)
def test_an_agents_tools_are_the_swarmcloud_ones_it_needs_under_both_names(path):
    """`tools` is matched, not resolved: a wrong spelling grants nothing, and an
    agent none of whose entries resolve does not start. The plugin's own server
    is scoped (`mcp__plugin_<plugin>_<server>__<tool>`, derived from
    plugin.json), the checkout's `.mcp.json` registers `mcp__swarmcloud__*`;
    both are listed so the agent starts under either."""
    fields, _ = _load(path)
    tools = fields.get("tools")
    assert isinstance(tools, list) and tools, f"{path.name} has no tools list"
    stray = sorted(
        t for t in tools
        if t != "StructuredOutput" and not any(str(t).startswith(p) for p in _PREFIXES)
    )
    assert not stray, (
        f"{path.name} grants {stray}: only SwarmCloud's tools, and StructuredOutput -- the "
        "channel a schema-bearing agent() call answers through -- are allowed"
    )
    per_prefix = _granted(tools)
    project, scoped = _PREFIXES
    assert per_prefix[project] == per_prefix[scoped], (
        f"{path.name}: only under {project}: {sorted(per_prefix[project] - per_prefix[scoped])}; "
        f"only under {scoped}: {sorted(per_prefix[scoped] - per_prefix[project])}"
    )
    assert per_prefix[scoped] <= _REAL, f"{path.name} names tools the bridge does not serve"
    assert per_prefix[scoped] == EXPECTED_TOOLS[path.stem]


@pytest.mark.parametrize("path", _AGENTS, ids=lambda p: p.stem)
def test_an_agents_instructions_name_only_tools_it_was_granted(path):
    fields, body = _load(path)
    named = set(re.findall(r"`(swarm_[a-z_]+)`", body))
    granted = _granted(fields["tools"])[_PREFIXES[1]]
    assert named <= granted, f"{path.name} tells the agent to call {sorted(named - granted)}, which it cannot"


def test_every_agent_type_the_workflow_names_is_an_agent_the_plugin_ships():
    source = _RUN_JS.read_text()
    named = set(re.findall(r"agentType:\s*'([^']+)'", source))
    assert named, "run.js names no agentType, so every row would run the session's own model and tools"
    shipped = {f"{_plugin_name()}:{p.stem}" for p in _AGENTS}
    assert named <= shipped, f"run.js starts {sorted(named - shipped)}, which the plugin does not ship"


# --------------------------------------------------------------------------
# run.js, read
# --------------------------------------------------------------------------


_PUNCT = ("...", "===", "!==", "=>", "==", "!=", "&&", "||", "<=", ">=", "++", "--")


def _tokens(source: str) -> list[tuple[str, str]]:
    """A JavaScript token stream: enough to tell code from strings and comments.

    Regular-expression literals are not recognised; `test_run_js_uses_no_regex_literal_or_template`
    holds the script to not having any, so a `/` here is always division.
    """
    out: list[tuple[str, str]] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c.isspace():
            i += 1
        elif source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
        elif source.startswith("/*", i):
            j = source.find("*/", i + 2)
            assert j >= 0, "unterminated block comment"
            i = j + 2
        elif c in "'\"":
            j = i + 1
            while source[j] != c:
                assert source[j] != "\n", "a string literal runs past the end of its line"
                j += 2 if source[j] == "\\" else 1
            out.append(("str", source[i:j + 1]))
            i = j + 1
        elif c == "`":
            j = source.find("`", i + 1)
            assert j >= 0, "unterminated template literal"
            out.append(("tpl", source[i:j + 1]))
            i = j + 1
        elif c.isalpha() or c in "_$":
            j = i
            while j < n and (source[j].isalnum() or source[j] in "_$"):
                j += 1
            out.append(("ident", source[i:j]))
            i = j
        elif c.isdigit():
            j = i
            while j < n and (source[j].isalnum() or source[j] == "."):
                j += 1
            out.append(("num", source[i:j]))
            i = j
        else:
            op = next((p for p in _PUNCT if source.startswith(p, i)), c)
            out.append(("punct", op))
            i += len(op)
    return out


def _meta_span(tokens: list[tuple[str, str]]) -> tuple[int, int]:
    head = [("ident", "export"), ("ident", "const"), ("ident", "meta"), ("punct", "="), ("punct", "{")]
    assert tokens[:5] == head, "`export const meta = {` must be the script's first statement"
    depth = 0
    for index in range(4, len(tokens)):
        kind, text = tokens[index]
        if kind == "punct" and text in "{[":
            depth += 1
        elif kind == "punct" and text in "}]":
            depth -= 1
            if depth == 0:
                return 4, index + 1
    raise AssertionError("meta's object literal never closes")


def _literal(tokens: list[tuple[str, str]]) -> object:
    """Evaluate a PURE object literal: strings, numbers, true/false/null, keys."""
    position = 0

    def value() -> object:
        nonlocal position
        kind, text = tokens[position]
        position += 1
        if kind == "punct" and text == "{":
            out: dict[str, object] = {}
            while tokens[position] != ("punct", "}"):
                key_kind, key = tokens[position]
                assert key_kind in ("ident", "str"), f"meta has a computed or spread key near {key!r}"
                position += 1
                assert tokens[position] == ("punct", ":"), f"meta key {key!r} is not `key: value`"
                position += 1
                out[key if key_kind == "ident" else ast.literal_eval(key)] = value()
                if tokens[position] == ("punct", ","):
                    position += 1
            position += 1
            return out
        if kind == "punct" and text == "[":
            items = []
            while tokens[position] != ("punct", "]"):
                items.append(value())
                if tokens[position] == ("punct", ","):
                    position += 1
            position += 1
            return items
        if kind == "str":
            return ast.literal_eval(text)
        if kind == "num":
            return float(text) if "." in text else int(text)
        if kind == "ident" and text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        raise AssertionError(f"meta is not a pure literal: it contains {kind} {text!r}")

    result = value()
    assert position == len(tokens), "meta holds more than one literal"
    return result


def _meta() -> dict:
    tokens = _tokens(_RUN_JS.read_text())
    start, end = _meta_span(tokens)
    meta = _literal(tokens[start:end])
    assert isinstance(meta, dict)
    return meta


def test_run_js_meta_is_a_pure_literal_naming_the_command():
    """Anything but literals in `meta` -- a variable, a call, a spread, a
    template -- and Claude Code drops the command from `/` autocomplete. The
    plugin's workflows run as /<plugin>:<meta.name>, so this is /sc:run."""
    meta = _meta()
    assert meta["name"] == _RUN_JS.stem == "run"
    assert _plugin_name() == "sc", "the command is documented as /sc:run"
    assert isinstance(meta["description"], str) and meta["description"].strip()
    titles = [phase["title"] for phase in meta.get("phases", [])]
    assert titles == ["Submit", "Result"], titles


def test_run_js_uses_only_the_workflow_globals():
    """No filesystem, no imports, no process, no clock and no randomness: the
    runtime provides the workflow globals and plain JavaScript, and makes
    `Date.now()` and `Math.random()` throw so a relaunched run repeats itself."""
    tokens = _tokens(_RUN_JS.read_text())
    _, end = _meta_span(tokens)
    body = tokens[end:]
    declared: set[str] = set()
    for index, (kind, text) in enumerate(body):
        if kind != "ident":
            continue
        if text in ("const", "let", "var", "function") and index + 1 < len(body):
            declared.add(body[index + 1][1])
        if text == "catch" and body[index + 1] == ("punct", "(") and body[index + 2][0] == "ident":
            declared.add(body[index + 2][1])
    for index, token in enumerate(body):
        # Parameters: the identifiers inside the parentheses before `=>`, or
        # after `function name`.
        closes = None
        if token == ("punct", "=>") and body[index - 1] == ("punct", ")"):
            closes = index - 1
        elif token == ("ident", "function"):
            closes = next(k for k in range(index, len(body)) if body[k] == ("punct", ")"))
        elif token == ("punct", "=>") and body[index - 1][0] == "ident":
            declared.add(body[index - 1][1])
        if closes is not None:
            depth, k = 0, closes
            while True:
                if body[k] == ("punct", ")"):
                    depth += 1
                elif body[k] == ("punct", "("):
                    depth -= 1
                    if depth == 0:
                        break
                elif body[k][0] == "ident":
                    declared.add(body[k][1])
                k -= 1
    free = set()
    for index, (kind, text) in enumerate(body):
        if kind != "ident" or text in JS_KEYWORDS or text in declared:
            continue
        previous = body[index - 1] if index else ("", "")
        following = body[index + 1] if index + 1 < len(body) else ("", "")
        if previous == ("punct", "."):
            continue  # a property, not a variable
        if following == ("punct", ":") and previous in (("punct", "{"), ("punct", ",")):
            continue  # an object key
        free.add(text)
    unknown = sorted(free - WORKFLOW_GLOBALS - JS_BUILTINS)
    assert not unknown, f"run.js reads globals a workflow script does not have: {unknown}"
    source = _RUN_JS.read_text()
    for banned in ("Date.now", "Math.random", "new Date(", "import(", "require(", "process."):
        assert banned not in source, f"run.js uses {banned!r}, which a workflow script cannot"


def test_run_js_uses_no_regex_literal_or_template():
    """The reader above cannot tell a regex from a division. The script has
    no need of either, and a template literal is where an interpolated value
    would slip into `meta` unseen."""
    tokens = _tokens(_RUN_JS.read_text())
    assert "tpl" not in {kind for kind, _ in tokens}, "run.js holds a template literal"
    # Every `/` must sit between two operands, which is division. A `/` after
    # an operator or an opening bracket starts a regex literal.
    for index, token in enumerate(tokens):
        if token != ("punct", "/"):
            continue
        before = tokens[index - 1]
        assert before[0] in ("ident", "num") or before[1] in (")", "]"), (
            f"run.js appears to hold a regex literal after {before[1]!r}"
        )


def test_every_literal_phase_the_script_starts_is_in_meta():
    """A `phase()` title with no meta entry gets a progress group of its own --
    right for the per-level groups, which cannot be known in advance, and wrong
    for the two fixed ones, which would appear twice."""
    source = _RUN_JS.read_text()
    literal = set(re.findall(r"phase\('([^']+)'\)", source)) | set(re.findall(r"phase: '([^']+)'", source))
    titles = {phase["title"] for phase in _meta()["phases"]}
    assert literal and literal <= titles, sorted(literal - titles)


# --------------------------------------------------------------------------
# run.js, run
# --------------------------------------------------------------------------


_HARNESS = r"""
const fs = require('fs')
const [, , scriptPath, fixturePath] = process.argv
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'))
const source = fs.readFileSync(scriptPath, 'utf8')
if (!source.startsWith('export const meta =')) throw new Error('meta must be the first statement')
const body = source.replace('export const meta =', 'const meta =')
const calls = []
const logs = []
const phases = []
Date.now = () => { throw new Error('Date.now() is unavailable in a workflow script') }
Math.random = () => { throw new Error('Math.random() is unavailable in a workflow script') }
async function agent(prompt, opts) {
  opts = opts || {}
  calls.push({ prompt, label: opts.label, phase: opts.phase, agentType: opts.agentType,
               model: opts.model || null, schema: opts.schema ? Object.keys(opts.schema.properties) : null })
  const key = opts.agentType === 'sc:step' ? 'step:' + opts.label : prompt.split('\n')[0]
  const answer = fixture.answers[key]
  if (answer === undefined) throw new Error('no fixture answer for ' + key)
  if (answer && answer.__throw) throw new Error(answer.__throw)
  return answer
}
async function pipeline(items, ...stages) {
  const out = []
  for (let i = 0; i < items.length; i++) {
    let value = items[i]
    for (const stage of stages) {
      try { value = await stage(value, items[i], i) } catch (error) { value = null; break }
    }
    out.push(value)
  }
  return out
}
async function parallel(thunks) { return Promise.all(thunks.map((t) => t().catch(() => null))) }
function phase(title) { phases.push(title) }
function log(message) { logs.push(message) }
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = new AsyncFunction('agent', 'pipeline', 'parallel', 'phase', 'log', 'args', 'budget', 'workflow', body)
run(agent, pipeline, parallel, phase, log, fixture.args,
    { total: null, spent: () => 0, remaining: () => Infinity },
    async () => { throw new Error('run.js must not nest a workflow') })
  .then((result) => process.stdout.write(JSON.stringify({ result, calls, logs, phases })))
  .catch((error) => process.stdout.write(JSON.stringify({ error: String((error && error.message) || error), calls, logs, phases })))
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("node is not on PATH in CI, so run.js was never run -- that is not a pass")
        pytest.skip("node is not installed; CI runs these")
    return node


def _run(tmp_path, args, answers) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_HARNESS)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run(
        [_node(), str(harness), str(_RUN_JS), str(fixture)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


_SPEC = {
    "label": "scan",
    "steps": [
        {"step_id": "scan-01", "prompt": "scan a"},
        {"step_id": "scan-02", "prompt": "scan b"},
        {"step_id": "join", "prompt": "join", "depends_on": ["scan-01", "scan-02"]},
        {"step_id": "report", "prompt": "report", "depends_on": ["join"], "stage": "Report"},
    ],
}

_SUBMITTED = {
    "workflow_id": "wf_1",
    "repository": "https://github.com/acme/widgets.git",
    "error": None,
    "steps": [
        {"step_id": "report", "task_id": "task_4", "depends_on": ["join"]},
        {"step_id": "join", "task_id": "task_3", "depends_on": ["scan-01", "scan-02"]},
        {"step_id": "scan-01", "task_id": "task_1", "depends_on": []},
        {"step_id": "scan-02", "task_id": "task_2", "depends_on": []},
    ],
}


def _step(state, **extra):
    return {"state": state, "answer_excerpt": None, "cost_usd": None, "duration_s": None,
            "pr_url": None, "artifacts": [], "last_error": None, **extra}


_ANSWERS = {
    "SUBMIT": _SUBMITTED,
    "step:scan-01": _step("SUCCEEDED", cost_usd=0.21, duration_s=252,
                          pr_url="https://github.com/acme/widgets/pull/231", artifacts=["a.md"]),
    "step:scan-02": _step("FAILED", duration_s=63, last_error="claude-code exited 1: boom"),
    "step:join": {"__throw": "StructuredOutput failed validation after 5 attempts"},
    "step:report": _step("SUCCEEDED", cost_usd=0.05, duration_s=40),
    "STATUS": {"state": "FAILED", "state_note": None, "steps": []},
}


def test_run_js_submits_once_then_starts_one_step_row_per_step(tmp_path):
    got = _run(tmp_path, _SPEC, _ANSWERS)
    assert "error" not in got, got.get("error")
    submit, *steps, status = got["calls"]

    assert (submit["agentType"], submit["phase"], submit["label"]) == ("sc:workflow", "Submit", "submit")
    lines = submit["prompt"].split("\n")
    assert lines[:2] == ["SUBMIT", "BEGIN SPEC"] and lines[-1] == "END SPEC"
    assert json.loads("\n".join(lines[2:-1])) == _SPEC, "the spec must reach the submitting agent verbatim"

    # One row per step, labelled by step id, grouped by DAG level -- or by the
    # stage the spec gives -- and started shallowest first.
    # The submission answered them deepest first; the rows start level 0 first.
    assert [(c["agentType"], c["label"], c["phase"]) for c in steps] == [
        ("sc:step", "scan-01", "Level 0"),
        ("sc:step", "scan-02", "Level 0"),
        ("sc:step", "join", "Level 1"),
        ("sc:step", "report", "Report"),
    ]
    by_label = {c["label"]: c["prompt"] for c in steps}
    assert "task_id: task_3" in by_label["join"] and "workflow_id: wf_1" in by_label["join"]
    assert "depends_on: scan-01, scan-02" in by_label["join"]

    # The agents pin the model; the script names none.
    assert all(c["model"] is None for c in got["calls"])
    assert (status["agentType"], status["phase"]) == ("sc:workflow", "Result")
    assert status["prompt"].startswith("STATUS\nworkflow_id: wf_1")


def test_run_js_narrates_each_step_as_one_line(tmp_path):
    logs = _run(tmp_path, _SPEC, _ANSWERS)["logs"]
    assert "wf_1 submitted · 4 step(s) · clones https://github.com/acme/widgets.git" in logs
    assert "scan-01 SUCCEEDED · 4m12s · $0.21 · PR #231" in logs
    # Null spend is not measured, never $0.00.
    assert "scan-02 FAILED · 1m03s · cost not recorded · claude-code exited 1: boom" in logs
    assert any(
        line.startswith("join · its row stopped before the task finished")
        and "the SwarmCloud task is unaffected" in line
        for line in logs
    ), logs
    assert logs[-1] == "wf_1 FAILED"


def test_run_js_returns_the_state_swarmcloud_derived_not_one_of_its_own(tmp_path):
    """Two steps SUCCEEDED, one FAILED, one row died: the script could guess.
    It must not -- the state is read back from swarm_workflow_status. The
    fixture answers a state no row implies, to show where it came from."""
    answers = {**_ANSWERS, "STATUS": {"state": "RUNNING", "state_note": None, "steps": []}}
    result = _run(tmp_path, _SPEC, answers)["result"]

    assert result["workflow_id"] == "wf_1"
    assert result["state"] == "RUNNING"
    rows = {row["step_id"]: row for row in result["steps"]}
    assert set(rows) == {"scan-01", "scan-02", "join", "report"}
    assert rows["scan-01"]["cost_usd"] == 0.21 and rows["scan-01"]["task_id"] == "task_1"
    assert rows["join"]["state"] is None and "validation" in rows["join"]["row_error"]


def test_run_js_starts_no_step_when_the_submission_is_refused(tmp_path):
    refused = {"workflow_id": None, "steps": [], "repository": None,
               "error": "branch 'lane/y' has no upstream: it has never been pushed"}
    got = _run(tmp_path, _SPEC, {"SUBMIT": refused})

    assert len(got["calls"]) == 1
    assert got["result"]["state"] == "NOT_SUBMITTED"
    assert got["result"]["error"] == refused["error"]
    assert got["logs"] == ["not submitted · " + refused["error"]]


def test_run_js_takes_the_spec_as_json_text_and_refuses_what_is_not_a_spec(tmp_path):
    as_text = _run(tmp_path, json.dumps(_SPEC), _ANSWERS)
    assert as_text["result"]["workflow_id"] == "wf_1"

    refused = _run(tmp_path, {"steps": []}, _ANSWERS)
    assert "non-empty `steps` list" in refused["error"]
    assert refused["calls"] == []
