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
import yaml

from swarm_mcp import progress, server, workflows

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
    # `sc:wait` is the pause between two tries of a row whose bridge was down
    # (a workflow script has no timer): no SwarmCloud tool at all, only Bash
    # for its one `sleep` (HOST_TOOLS).
    "wait": set(),
    "step": {"swarm_follow"},
    # `sc:task` is `sc:step` for a task in NO workflow (#830): it follows
    # without `step_id`, so it watches and nothing else, like a step row.
    "task": {"swarm_follow"},
    # `swarm_workflow_spec` reads a spec FILE for /sc:run (epic #227): a workflow
    # script has no filesystem, so the bridge reads it and digests it.
    # `swarm_follow` is the probe (owner decision, 2026-10-01): after a submit
    # or an attach it makes the rows' own progress follow once, so a bridge
    # that refuses it fails the workflow instead of every row falling back.
    # `swarm_workflows` is the LIST job (owner decision, 2026-10-02):
    # `{attach: "all"}` lists the tenant's running workflows before attaching each.
    "workflow": {
        "swarm_workflow", "swarm_workflow_spec", "swarm_workflow_status", "swarm_workflows", "swarm_follow",
    },
}

#: The non-SwarmCloud tools an agent may hold beside StructuredOutput, and why.
#: `sc:wait` makes one `sleep` and nothing else.
HOST_TOOLS = {"wait": {"Bash"}}

#: The globals a workflow script's body may use: the workflow runtime's own
#: (the workflow-authoring reference) and the plain JavaScript built-ins.
WORKFLOW_GLOBALS = frozenset({"agent", "pipeline", "parallel", "phase", "log", "args", "budget", "workflow"})
JS_BUILTINS = frozenset(
    {"JSON", "Math", "String", "Number", "Array", "Object", "Error", "Boolean", "isFinite", "isNaN",
     "parseInt", "parseFloat", "Promise", "Set", "Map", "undefined", "Infinity", "NaN"}
)
#: Host globals run.js may read behind a `typeof` guard. None: the runtime has
#: no timer, so a lost row's retries are spaced by an `sc:wait` row instead.
GUARDED_HOST_GLOBALS: frozenset[str] = frozenset()
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


def _yaml(path: Path) -> object:
    """The frontmatter as a REAL YAML parser reads it (PyYAML, safe loader)."""
    return yaml.safe_load("\n".join(_split(path.read_text())[0]))


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


def test_the_plugin_ships_the_agents_the_workflows_name():
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
def test_an_agents_frontmatter_reads_the_same_under_a_real_yaml_parser(path):
    """The strict reader above proves the file matches THIS test's grammar.
    That is not the host's: a real YAML parser reads `a: b` inside a scalar as
    a mapping, `#` as a comment, `yes` as a boolean. So the same text goes
    through PyYAML too, and the two must agree on every key and value.
    (Claude Code's own parser is a JavaScript one; it is not run here.)"""
    strict, _ = _load(path)
    real = _yaml(path)
    assert isinstance(real, dict), f"{path.name}: a YAML parser reads the frontmatter as {type(real).__name__}"
    assert real == strict, (
        f"{path.name}: a YAML parser reads different values: "
        f"{ {k: real.get(k) for k in set(real) | set(strict) if real.get(k) != strict.get(k)} }"
    )


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


@pytest.mark.parametrize("name", ["remote", "step", "task"])
def test_a_proxy_row_is_capped_well_under_claude_codes_own_turn_limit(name):
    """Owner decision, 2026-09-26 (proxy cost bound): a haiku row that only
    relays a remote task must not be able to poll `swarm_follow` for hours
    before Claude Code itself intervenes. `maxTurns: 60` is the bound; `sc:
    workflow` submits once and reads state once, so it keeps its own smaller
    cap."""
    fields, _ = _load(_PLUGIN / "agents" / f"{name}.md")
    assert fields.get("maxTurns") == 60, f"{name}.md must cap at 60 turns, found {fields.get('maxTurns')!r}"


@pytest.mark.parametrize("name", ["remote", "step", "task"])
def test_a_proxy_rows_own_follow_call_cap_is_sixty_not_twenty(name):
    """Owner decision (#230 comment, 2026-09-26): a proxy row's OWN count of
    its `swarm_follow` calls -- distinct from Claude Code's `maxTurns: 60` --
    is 56 calls of up to 300s each (4 of its 60 turns are kept for dispatch and report), not 20. 20 calls stopped a row reporting
    `running` well before a real remote task -- which can take hours -- had a
    chance to finish, on a turn budget that had room for more polling."""
    _, body = _load(_PLUGIN / "agents" / f"{name}.md")
    flat = " ".join(body.split())
    assert "the 56th one" in flat, (
        f"{name}.md must count its own swarm_follow calls to 56, not fewer"
    )
    assert "56-call limit" in flat, (
        f"{name}.md must name its own follow-call cap as 56"
    )
    assert "20th one" not in flat and "20-call limit" not in flat, (
        f"{name}.md still names the old 20-call follow cap"
    )


@pytest.mark.parametrize("name", ["remote", "step", "task"])
def test_a_proxy_row_at_its_turn_cap_reports_running_not_silence(name):
    """Claude Code's own `maxTurns` is a hard kill with no chance to answer.
    A row must stop ASKING before that -- well inside its 60-turn budget --
    and answer with a state Claude Code can read, plus how to see the rest:
    `swarm follow <task_id>` is the terminal command every other tail/follow
    instruction in this file spells out too."""
    _, body = _load(_PLUGIN / "agents" / f"{name}.md")
    flat = " ".join(body.split())
    assert "running" in flat.lower()
    assert "swarm follow <task_id>" in flat, (
        f"{name}.md must tell a capped row to say `swarm follow <task_id>` to resume"
    )


@pytest.mark.parametrize("path", _AGENTS, ids=lambda p: p.stem)
def test_an_agents_tools_are_the_swarmcloud_ones_it_needs_from_the_plugins_server_only(path):
    """`tools` is matched, not resolved: a wrong spelling grants nothing, and
    Claude Code refuses to launch an agent none of whose entries resolve.

    ONLY the plugin's scoped server (`mcp__plugin_<plugin>_<server>__<tool>`,
    derived from plugin.json). In this repository's checkout the project
    `.mcp.json` server loads as well -- Claude Code merges two servers only
    when their commands are identical, and these are not -- and it is a
    DIFFERENT program: the checked-out branch, in developer mode, against the
    deployment in the tfvars. Granted both, an agent calls either, per call:
    one row submits through one deployment and another follows through the
    other and reads 404, and a stale checkout bridge drops arguments it does
    not know. A plugin agent runs where its plugin's server runs, or not at
    all."""
    fields, _ = _load(path)
    tools = fields.get("tools")
    assert isinstance(tools, list) and tools, f"{path.name} has no tools list"
    project, scoped = _PREFIXES
    checkout_names = sorted(t for t in tools if str(t).startswith(project))
    assert not checkout_names, (
        f"{path.name} grants the checkout's server too: {checkout_names}. A plugin agent must "
        f"reach only its own plugin's server, {scoped}*"
    )
    allowed = {"StructuredOutput"} | HOST_TOOLS.get(path.stem, set())
    stray = sorted(t for t in tools if t not in allowed and not str(t).startswith(scoped))
    assert not stray, (
        f"{path.name} grants {stray}: only the plugin's SwarmCloud tools, and StructuredOutput "
        "-- the channel a schema-bearing agent() call answers through -- are allowed"
    )
    granted = _granted(tools)[scoped]
    assert granted <= _REAL, f"{path.name} names tools the bridge does not serve"
    assert granted == EXPECTED_TOOLS[path.stem]
    assert len(tools) == len(set(tools)), f"{path.name} lists a tool twice"


@pytest.mark.parametrize("path", _AGENTS, ids=lambda p: p.stem)
def test_an_agent_says_what_to_answer_when_its_server_is_not_connected(path):
    """With only the plugin's server granted, that server being down leaves an
    agent with `StructuredOutput` at most. Its instructions must say what to
    answer then -- a failure naming the cause -- rather than leave a haiku row
    to improvise one."""
    _, body = _load(path)
    assert "is not connected in this session" in " ".join(body.split()), (
        f"{path.name} does not say what to do when the sc plugin's SwarmCloud server is missing"
    )


_NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def test_the_step_agent_states_the_bridges_read_failure_limit():
    """`sc:step` is told when the bridge will stop it on a task it cannot
    read. The number is the bridge's; written as a word for a haiku reader, it
    is a mirrored value (docs/mirrored-values.md) and is held to the constant."""
    word = _NUMBER_WORDS[progress.READ_FAILURE_LIMIT]
    _, body = _load(_PLUGIN / "agents" / "step.md")
    assert f"other failures after {word} calls in a row" in " ".join(body.split()), (
        f"step.md must say the bridge stops after {word} calls in a row (READ_FAILURE_LIMIT)"
    )


@pytest.mark.parametrize("name", ["remote", "step", "task"])
def test_a_following_agent_stops_on_the_bridges_stop(name):
    """`all_finished` never becomes true for a task that cannot be read or is
    the wrong step; an agent that waited for it would poll to its turn limit."""
    _, body = _load(_PLUGIN / "agents" / f"{name}.md")
    flat = " ".join(body.split())
    assert "`stop` is `true`" in flat, f"{name}.md does not stop on the reply's `stop`"
    assert "abandoned_because" in flat, f"{name}.md does not hand back why the row gave up"


def test_the_step_agent_passes_its_step_id_to_the_bridge():
    _, body = _load(_PLUGIN / "agents" / "step.md")
    assert '`step_id: "<step_id>"`' in " ".join(body.split())


def test_remote_infers_with_infer_true_not_repo_or_ref():
    """Owner decision, 2026-09-26: repository inference is opt-in now, so
    `sc:remote` must ask for it explicitly with `infer: true` -- passing
    nothing gets nothing, exactly like a plain `swarm_dispatch` call."""
    _, body = _load(_PLUGIN / "agents" / "remote.md")
    flat = " ".join(body.split())
    assert "Do NOT pass `repo` or `ref`" in flat
    assert "`infer: true`" in flat
    assert "commit" in flat.lower(), "remote.md must say the repository is pinned at a commit"


def test_workflow_submits_with_infer_true_and_forwards_repository_notes():
    _, body = _load(_PLUGIN / "agents" / "workflow.md")
    flat = " ".join(body.split())
    assert '"infer": true' in flat.lower() or "infer: true" in flat.lower()
    assert "repository_notes" in flat


def test_a_schema_mode_failure_is_documented_as_a_throw_and_never_filled_in():
    """Claude Code's workflow docs: with a schema, a subagent whose output
    still fails validation after five attempts makes the `agent()` call FAIL
    WITH AN ERROR. The README once promised `null`; a workflow written against
    that has no catch, and a relay pressed five times for an object may build
    one -- `{counts: {}}` reads as "no TODOs"."""
    readme = " ".join((_PLUGIN / "README.md").read_text().split())
    assert "receives `null` for that step" not in readme, "the README still promises null for a failed schema-mode step"
    assert "the call can THROW" in readme
    assert ".catch(" in readme, "the README's schema-mode sc:remote example must catch the call"
    _, body = _load(_PLUGIN / "agents" / "remote.md")
    flat = " ".join(body.split())
    assert "do NOT call `StructuredOutput` — not the first time, and not when you are asked again" in flat
    assert "an object built to validate is an invented answer" in flat


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
    plugin's workflows run as /<plugin>:<meta.name>, so this is /sc:swarmcloud.

    Owner decision, 2026-10-01: the workflow is not called `run` -- it reads as
    SwarmCloud in /workflows, and its rows carry a `[SwarmCloud]` prefix."""
    meta = _meta()
    assert meta["name"] == "swarmcloud", "the command is lowercase: /sc:swarmcloud (owner, 2026-10-01)"
    assert _plugin_name() == "sc", "the command is documented as /sc:swarmcloud"
    assert "SwarmCloud" in meta["description"]
    assert isinstance(meta["description"], str) and meta["description"].strip()
    titles = [phase["title"] for phase in meta.get("phases", [])]
    # `Tasks` is the {attach_tasks} run (#830): one sc:task row per single task.
    assert titles == ["Submit", "Attach", "Tasks", "Result"], titles


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
    unknown = sorted(free - WORKFLOW_GLOBALS - JS_BUILTINS - GUARDED_HOST_GLOBALS)
    source = _RUN_JS.read_text()
    for name in GUARDED_HOST_GLOBALS:
        uses = source.count(name)
        assert uses == source.count(f"typeof {name} !== 'function'") * 2, (
            f"run.js reads {name} other than as `if (typeof {name} !== 'function') ...` and one call after it"
        )
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
    for the three fixed ones, which would appear twice."""
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
  const stepLine = prompt.split('\n').find((line) => line.startsWith('step_id: '))
  const key = opts.agentType === 'sc:step' ? 'step:' + (stepLine || '').slice('step_id: '.length) : prompt.split('\n')[0]
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

def _bridge_digest(spec) -> str | None:
    """`workflows.spec_digest`, looked up when called: a bridge without it
    fails the tests that need it by name, not this module's collection."""
    digest = getattr(workflows, "spec_digest", None)
    return digest(spec) if digest is not None else None


_SUBMITTED = {
    "workflow_id": "wf_1",
    "repository": "https://github.com/acme/widgets.git",
    # The bridge's digest of the spec it received -- Python's, so every run.js
    # test that starts a row also proves the script's digest agrees with it.
    "spec_digest": _bridge_digest(_SPEC),
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

    assert (submit["agentType"], submit["phase"], submit["label"]) == (
        "sc:workflow", "Submit", "[SwarmCloud] scan · 4 steps · setup · submit")
    lines = submit["prompt"].split("\n")
    assert lines[:3] == ["SUBMIT", "spec_digest: " + workflows.spec_digest(_SPEC), "BEGIN SPEC"]
    assert lines[-1] == "END SPEC"
    assert json.loads("\n".join(lines[3:-1])) == _SPEC, "the spec must reach the submitting agent verbatim"

    # One row per step, labelled `[SwarmCloud] <workflow> · stage <n> · <step>`
    # (owner decision, 2026-10-01) with the CONSOLE'S stage, the DAG level plus
    # one (owner, 2026-10-04: the spec's own `stage` never reaches the console,
    # so it is not shown here either), grouped under 'Stage N', shallowest first.
    # The submission answered them deepest first; the rows start level 0 first.
    assert [(c["agentType"], c["label"], c["phase"]) for c in steps] == [
        ("sc:step", "[SwarmCloud] scan · stage 1 · scan-01", "Stage 1"),
        ("sc:step", "[SwarmCloud] scan · stage 1 · scan-02", "Stage 1"),
        ("sc:step", "[SwarmCloud] scan · stage 2 · join", "Stage 2"),
        ("sc:step", "[SwarmCloud] scan · stage 3 · report", "Stage 3"),
    ]
    by_label = {c["label"].rsplit(" · ", 1)[-1]: c["prompt"] for c in steps}
    assert "task_id: task_3" in by_label["join"] and "workflow_id: wf_1" in by_label["join"]
    assert "depends_on: scan-01, scan-02" in by_label["join"]
    # The row's own step id, which it hands to swarm_follow as the check that
    # the task it follows is this step.
    assert "step_id: join" in by_label["join"]

    # The agents pin the model; the script names none.
    assert all(c["model"] is None for c in got["calls"])
    assert (status["agentType"], status["phase"], status["label"]) == (
        "sc:workflow", "Result", "[SwarmCloud] scan · 4 steps · wf_1 · setup · result")
    assert status["prompt"].startswith("STATUS\nworkflow_id: wf_1")


def test_run_js_narrates_each_step_as_one_line(tmp_path):
    logs = _run(tmp_path, _SPEC, _ANSWERS)["logs"]
    assert "wf_1 submitted · 4 step(s) · clones https://github.com/acme/widgets.git" in logs
    # What the step produced, by name (#143): a row that ends without saying so
    # sends the reader to the console to find out.
    assert "scan-01 SUCCEEDED · 4m12s · $0.21 · PR #231 · produced a.md" in logs
    # A step that produced nothing says so, rather than leaving it to be
    # inferred from a missing clause.
    assert "report SUCCEEDED · 40s · $0.05 · produced no artifacts" in logs
    # Null spend is not measured, never $0.00.
    assert "scan-02 FAILED · 1m03s · cost not recorded · claude-code exited 1: boom" in logs
    assert any(
        line.startswith("join · its row stopped before the task finished")
        and "the SwarmCloud task is unaffected" in line
        for line in logs
    ), logs
    assert logs[-1] == "wf_1 FAILED"


def test_the_submit_schema_carries_repository_notes(tmp_path):
    """Owner decision, 2026-09-26: every inference note (uncommitted changes
    not visible, the branch's upstream, the checkout path) must reach the
    calling workflow -- it cannot, unless `sc:workflow`'s answer schema has
    somewhere to put it."""
    got = _run(tmp_path, _SPEC, _ANSWERS)
    submit = got["calls"][0]
    assert "repository_notes" in submit["schema"]


def test_run_js_logs_the_bridges_repository_inference_notes(tmp_path):
    submitted = {**_SUBMITTED, "repository_notes": [
        "inferred from the checkout at /work/widgets: origin/lane/x, pushed at abc123",
        "3 uncommitted change(s) in this checkout are NOT visible to the remote agent",
    ]}
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "SUBMIT": submitted})

    assert "error" not in got, got.get("error")
    assert any("3 uncommitted change(s)" in line for line in got["logs"]), got["logs"]
    assert any("origin/lane/x" in line for line in got["logs"]), got["logs"]


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
    refused = {"workflow_id": None, "steps": [], "repository": None, "spec_digest": None,
               "error": "branch 'lane/y' is not on origin under its own name"}
    got = _run(tmp_path, _SPEC, {"SUBMIT": refused})

    assert len(got["calls"]) == 1
    assert got["result"]["state"] == "NOT_SUBMITTED"
    assert got["result"]["error"] == refused["error"]
    assert got["logs"] == ["not submitted · " + refused["error"]]


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (None, "stopped before it answered"),
        ({"__throw": "StructuredOutput failed validation after 5 attempts"}, "failed validation"),
        ({"workflow_id": None, "steps": [], "repository": None, "spec_digest": None, "error": None},
         "neither a workflow id nor an error"),
    ],
    ids=["stopped", "threw", "said-nothing"],
)
def test_run_js_reports_a_submission_it_cannot_see_as_unknown_not_as_not_submitted(tmp_path, answer, why):
    """`agent()` resolves to null when the row is stopped and throws when
    validation runs out -- both possibly AFTER swarm_workflow created the
    workflow. Claude Code re-runs a failed or stopped agent on relaunch and the
    API has no idempotency key: calling this NOT_SUBMITTED invites a second
    copy of every task."""
    got = _run(tmp_path, _SPEC, {"SUBMIT": answer})

    assert "error" not in got, got.get("error")
    assert len(got["calls"]) == 1, "no row may start when the submission cannot be seen"
    result = got["result"]
    assert result["state"] == "SUBMISSION_UNKNOWN"
    assert why in result["error"] and "second copy" in result["error"]
    assert "label scan" in result["error"], "the spec's label is how the workflow is found again"
    assert got["logs"][0].startswith("submission outcome unknown · ")


def _unverified(tmp_path, submitted) -> dict:
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "SUBMIT": submitted})
    assert "error" not in got, got.get("error")
    assert [c["agentType"] for c in got["calls"]] == ["sc:workflow"], "a row started on an unverified reply"
    assert got["result"]["state"] == "SUBMITTED_UNVERIFIED"
    assert got["result"]["workflow_id"] == "wf_1", "the workflow exists; its id must be handed back"
    assert "swarm_workflow_cancel" in got["result"]["error"]
    return got


def test_run_js_starts_no_row_when_the_reply_drops_a_step(tmp_path):
    """The relay retyped the reply and lost a step. Its task still runs."""
    dropped = {**_SUBMITTED, "steps": [s for s in _SUBMITTED["steps"] if s["step_id"] != "scan-02"]}
    got = _unverified(tmp_path, dropped)
    assert "wf_1 · step scan-02 is in the spec and missing from the reply" in got["logs"], got["logs"]


def test_run_js_starts_no_row_when_the_reply_changes_a_dependency_or_reuses_a_task(tmp_path):
    steps = [dict(s) for s in _SUBMITTED["steps"]]
    by_id = {s["step_id"]: s for s in steps}
    by_id["join"]["depends_on"] = ["scan-01"]
    by_id["scan-02"]["task_id"] = "task_1"
    got = _unverified(tmp_path, {**_SUBMITTED, "steps": steps})
    error = got["result"]["error"]
    assert "step join depends on [scan-01] in the reply and on [scan-01, scan-02] in the spec" in error, error
    assert "task task_1 is named for both" in error, error


def test_run_js_starts_no_row_when_the_digest_is_not_the_specs(tmp_path):
    """The spec SwarmCloud received is not the spec given: a dropped or
    reworded step would run under this workflow's name."""
    got = _unverified(tmp_path, {**_SUBMITTED, "spec_digest": "fnv1a32:00000000"})
    assert "the relay changed it on the way" in got["result"]["error"]

    missing = _unverified(tmp_path, {**_SUBMITTED, "spec_digest": None})
    assert "carries no spec digest" in missing["result"]["error"]


def test_run_js_keeps_every_rows_result_when_the_result_row_fails(tmp_path):
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "STATUS": {"__throw": "StructuredOutput failed validation after 5 attempts"}})

    assert "error" not in got, got.get("error")
    result = got["result"]
    assert {row["step_id"] for row in result["steps"]} == {"scan-01", "scan-02", "join", "report"}
    assert result["state"] is None
    assert "the row reading the workflow state failed" in result["state_note"]
    assert got["logs"][-1].startswith("wf_1 state not read · ")


def test_run_js_digests_a_spec_exactly_as_the_bridge_does(tmp_path):
    """run.js cannot import Python, so its digest is a second implementation
    (docs/mirrored-values.md). A mismatch would make swarm_workflow refuse
    EVERY /sc:run submission as a changed spec. Held here over the cases where
    two JSON writers differ: non-ASCII, astral characters, control characters,
    unsorted and nested keys, an integral float."""
    spec = {
        "label": "scän 🚀 日本",
        "steps": [
            {"step_id": "b", "prompt": "tab\there, bell\u0007, quote \" and backslash \\", "timeout_seconds": 60.0,
             "inputs": {"z": [1, 2.5, None, True], "a": {"y": "é", "x": ""}}},
            {"step_id": "a", "prompt": "line\nbreak\r\n", "depends_on": ["b"]},
        ],
        "priority": 3,
    }
    got = _run(tmp_path, spec, {"SUBMIT": None})
    submit = got["calls"][0]["prompt"].split("\n")
    assert submit[1] == "spec_digest: " + workflows.spec_digest(spec), (
        f"run.js digested the spec as {submit[1]!r}; the bridge as {workflows.spec_digest(spec)!r}"
    )
    # The control shows the comparison could fail: a one-character change moves it.
    changed = json.loads(json.dumps(spec))
    changed["steps"][1]["prompt"] = "line\nbreak\r"
    assert workflows.spec_digest(changed) != workflows.spec_digest(spec)


def test_run_js_takes_the_spec_as_json_text_and_refuses_what_is_not_a_spec(tmp_path):
    as_text = _run(tmp_path, json.dumps(_SPEC), _ANSWERS)
    assert as_text["result"]["workflow_id"] == "wf_1"

    refused = _run(tmp_path, {"steps": []}, _ANSWERS)
    assert "non-empty `steps` list" in refused["error"]
    assert refused["calls"] == []


# --------------------------------------------------------------------------
# run.js: a spec FILE (epic #227). The script has no filesystem, so the bridge
# reads the file (`swarm_workflow_spec`) and hands back its digest; the script
# checks the relayed spec against that digest before submitting it.
# --------------------------------------------------------------------------


def _read(spec, digest=None, **extra):
    """What the READ SPEC row relays: the bridge's ref, digest and outline -- never the spec."""
    outline = {"title": spec.get("title"), "label": spec.get("label"), "steps": [
        {"step_id": s["step_id"], "depends_on": s.get("depends_on", []), "stage": s.get("stage")} for s in spec["steps"]
    ]}
    return {"path": "/work/widgets/specs/scan.json", "spec_ref": "spec_00112233aabbccdd",
            "spec_digest": _bridge_digest(spec) if digest is None else digest, "outline": outline,
            "error": None, **extra}


def test_run_js_takes_a_spec_files_path_and_submits_what_the_bridge_read(tmp_path):
    got = _run(tmp_path, "specs/scan.json", {**_ANSWERS, "READ SPEC": _read(_SPEC)})

    assert "error" not in got, got.get("error")
    read, submit = got["calls"][0], got["calls"][1]
    assert (read["agentType"], read["phase"], read["label"]) == (
        "sc:workflow", "Submit", "[SwarmCloud] specs/scan.json · setup · read spec")
    assert read["prompt"] == "READ SPEC\npath: specs/scan.json"
    # By the ref the bridge holds: the submit row never sees the spec.
    assert submit["prompt"] == "SUBMIT\nspec_digest: " + workflows.spec_digest(_SPEC) + "\nspec_ref: spec_00112233aabbccdd"
    assert got["result"]["workflow_id"] == "wf_1"
    assert "read the spec from /work/widgets/specs/scan.json" in got["logs"][0], got["logs"]


def test_run_js_submits_nothing_when_the_read_row_drops_the_ref_or_the_outline(tmp_path):
    for broken in (_read(_SPEC, spec_ref=None), _read(_SPEC, outline=None), _read(_SPEC, digest="")):
        got = _run(tmp_path, "specs/scan.json", {**_ANSWERS, "READ SPEC": broken})
        assert [c["prompt"].split("\n")[0] for c in got["calls"]] == ["READ SPEC"], "it submitted without a ref"
        assert got["result"]["state"] == "NOT_SUBMITTED"


def test_run_js_starts_no_row_when_the_reply_disagrees_with_the_files_outline(tmp_path):
    """A relay that changed the outline or the reply: the two no longer agree,
    so no row starts -- the same check a spec object gets."""
    got = _run(tmp_path, "specs/scan.json", {**_ANSWERS, "READ SPEC": _read(_SPEC),
                                             "SUBMIT": {**_SUBMITTED, "spec_digest": "fnv1a32:00000000"}})
    assert got["result"]["state"] == "SUBMITTED_UNVERIFIED"


def test_run_js_submits_nothing_when_the_bridge_could_not_read_the_file(tmp_path):
    refused = {"path": None, "spec_ref": None, "spec_digest": None, "outline": None,
               "error": "specs/scan.json does not exist in /work/widgets"}
    got = _run(tmp_path, "specs/scan.json", {**_ANSWERS, "READ SPEC": refused})

    assert len(got["calls"]) == 1
    assert got["result"]["state"] == "NOT_SUBMITTED"
    assert got["result"]["error"] == refused["error"]


def test_run_js_still_reports_json_text_that_does_not_parse_as_json(tmp_path):
    """Text that begins like JSON is JSON with a mistake in it, not a path."""
    got = _run(tmp_path, '{"steps": [', _ANSWERS)
    assert "is not JSON" in got["error"], got
    assert got["calls"] == []


# --------------------------------------------------------------------------
# Rows that are cheap to watch, and named for SwarmCloud (owner decisions,
# 2026-10-01)
# --------------------------------------------------------------------------


def test_every_row_label_carries_the_swarmcloud_prefix_and_a_step_row_its_stage(tmp_path):
    got = _run(tmp_path, _SPEC, _ANSWERS)
    labels = [c["label"] for c in got["calls"]]
    assert labels and all(label.startswith("[SwarmCloud] ") for label in labels), labels
    for call in got["calls"]:
        if call["agentType"] == "sc:step":
            assert " · stage " in call["label"], call["label"]


def test_a_spec_without_a_label_names_its_rows_by_workflow_id(tmp_path):
    spec = {k: v for k, v in _SPEC.items() if k != "label"}
    submitted = {**_SUBMITTED, "spec_digest": _bridge_digest(spec)}
    got = _run(tmp_path, spec, {**_ANSWERS, "SUBMIT": submitted})
    assert "error" not in got, got.get("error")
    steps = [c for c in got["calls"] if c["agentType"] == "sc:step"]
    assert steps[0]["label"] == "[SwarmCloud] wf_1 · stage 1 · scan-01", steps[0]["label"]


def test_a_long_workflow_label_is_cut_so_the_stage_and_step_survive(tmp_path):
    spec = {**_SPEC, "label": "Wave 1 B2: quota data path " + "and a very long tail " * 8}
    submitted = {**_SUBMITTED, "spec_digest": _bridge_digest(spec)}
    got = _run(tmp_path, spec, {**_ANSWERS, "SUBMIT": submitted})
    assert "error" not in got, got.get("error")
    for call in got["calls"]:
        assert len(call["label"]) <= 100, call["label"]
        assert call["label"].startswith("[SwarmCloud] Wave 1 B2"), call["label"]
    join = [c["label"] for c in got["calls"] if c["agentType"] == "sc:step"][2]
    assert join.endswith("… · stage 2 · join"), join


def test_the_step_result_shape_is_unchanged(tmp_path):
    """Callers of the workflow read these seven fields per step; the slim
    follow format must not change them. `console` (owner decision 2026-10-01)
    is added after them: the step's console link, as the API served it.

    The ROW answers `{state, result}` (owner decision 2026-10-05, B2): it
    copies the bridge's null-free `result` rather than retyping each field,
    and run.js reads that back into the same seven fields."""
    got = _run(tmp_path, _SPEC, _ANSWERS)
    steps = [c for c in got["calls"] if c["agentType"] == "sc:step"]
    assert steps and all(c["schema"] == ["state", "result"] for c in steps), steps[0]["schema"]
    source = _RUN_JS.read_text()
    fields = source[source.index("const STEP_FIELDS = {"):source.index("const STEP_RESULT = {")]
    for name in ("state", "answer_excerpt", "cost_usd", "duration_s", "pr_url", "artifacts", "last_error", "console"):
        assert f"    {name}: " in fields, name
    scan = got["result"]["steps"][0]
    assert {k: scan[k] for k in ("state", "cost_usd", "duration_s", "pr_url", "artifacts")} == {
        "state": "SUCCEEDED", "cost_usd": 0.21, "duration_s": 252,
        "pr_url": "https://github.com/acme/widgets/pull/231", "artifacts": ["a.md"],
    }


def test_the_step_row_follows_the_progress_format_and_never_streams():
    """Measured 2026-10-01: rows following `format: "lines"` cost 4.0M tokens
    for one row and ~22M for six three-step workflows. A row follows
    `format: "progress"`, holds every call for the bridge's maximum (#448),
    and writes nothing while nothing changed."""
    _, body = _load(_PLUGIN / "agents" / "step.md")
    flat = " ".join(body.split())
    assert '`format: "progress"`' in flat, "step.md must follow the progress format"
    assert '"lines"' not in flat, "step.md must not ask for the streamed log"
    assert "`changed` is `false`" in flat and "write nothing" in flat.lower()
    assert "`wait_seconds: 1800`" in flat, "every call holds for the maximum"
    assert "`wait_seconds: 120`" not in flat and "`wait_seconds: 600`" not in flat
    for field in ("state", "answer_excerpt", "cost_usd", "duration_s", "pr_url", "artifacts", "last_error"):
        assert f"`{field}`" in flat, f"step.md no longer answers with {field}"


_STATUS_NAMING_JOIN = {
    "state": "SUCCEEDED",
    "state_note": None,
    "steps": [
        {"step_id": "scan-01", "state": "SUCCEEDED"},
        {"step_id": "scan-02", "state": "SUCCEEDED"},
        {"step_id": "join", "state": "SUCCEEDED"},
        {"step_id": "report", "state": "SUCCEEDED"},
    ],
}


def test_a_failed_relay_row_takes_its_state_from_the_workflow_status(tmp_path):
    """#285: the join row's relay failed to retype the answer into its
    StructuredOutput five times, but the task SUCCEEDED. The workflow status
    read after the rows names the step's state, so the row reports it -- with
    the row's own error kept beside it and where the state came from."""
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "STATUS": _STATUS_NAMING_JOIN})

    assert "error" not in got, got.get("error")
    rows = {row["step_id"]: row for row in got["result"]["steps"]}
    join = rows["join"]
    assert join["state"] == "SUCCEEDED"
    assert "validation" in join["row_error"], "the row's own failure is kept beside the state"
    assert "workflow status" in join["state_from"]
    # Only a recovered row carries the field: a row that answered keeps the
    # STEP_RESULT shape, and its own state, not the status's.
    assert "state_from" not in rows["scan-01"] and "state_from" not in rows["scan-02"]
    assert rows["scan-02"]["state"] == "FAILED"
    assert any(
        line.startswith("join SUCCEEDED (from the workflow status; its row failed: ")
        and "validation" in line
        for line in got["logs"]
    ), got["logs"]
    assert got["logs"][-1] == "wf_1 SUCCEEDED"


def test_a_failed_relay_row_stays_null_when_the_status_read_fails_too(tmp_path):
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "STATUS": {"__throw": "StructuredOutput failed validation after 5 attempts"}})

    assert "error" not in got, got.get("error")
    join = {row["step_id"]: row for row in got["result"]["steps"]}["join"]
    assert join["state"] is None and "validation" in join["row_error"]
    assert "state_from" not in join


def test_a_failed_relay_row_stays_null_when_the_status_gives_its_step_no_state(tmp_path):
    steps = [{"step_id": "join", "state": None}, {"step_id": "scan-01", "state": "SUCCEEDED"}]
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "STATUS": {**_STATUS_NAMING_JOIN, "steps": steps}})

    join = {row["step_id"]: row for row in got["result"]["steps"]}["join"]
    assert join["state"] is None and "state_from" not in join


def test_the_step_relay_copies_the_answer_excerpt_verbatim_and_never_retypes_it():
    """#285: the relay retyped the raw excerpt into JSON and failed five times
    on backticks, quotes, masks, paths and backslashes. The bridge now serves
    it JSON-safe; the relay copies it, and on a refused StructuredOutput sends
    it empty rather than rewriting it, so the state still arrives."""
    _, body = _load(_PLUGIN / "agents" / "step.md")
    flat = " ".join(body.split()).lower()
    assert "verbatim" in flat and "character for character" in flat
    assert "json-safe" in flat
    assert "never retype" in flat
    # B2 (owner, 2026-10-05): the fallback is an EMPTY excerpt, not a null --
    # every one of the 14 refused StructuredOutput calls that day was a null.
    assert "`result.answer_excerpt` set to `\"\"`" in flat
