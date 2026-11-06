"""The code graph as a skill on demand, with a nudge (the owner's pick, 3 November 2026; part of #2688).

Measured before this: with the ten graph tools deferred behind tool search
(batch one) agents never fetched them, 0 calls in 45 steps; loaded up front
(batch two, about 1,900 tokens on every turn) they made 2 calls in 12 steps,
the first an ambiguity error after which the agent never came back; and
subagents were never told about the graph. So:

1. a claude-code step's graph is a `code-graph` skill, a ~100-token
   description naming the moments it pays off, written under the CLI's HOME;
2. one scoped query behind it, `slice`: files or symbols, 1-3 hops, what to
   include, a bounded and paged answer; the server lists it alone;
3. a PreToolUse hint when the agent greps for an identifier-shaped name, one
   line, at most `MAX_HINTS` a step, only when the graph is there;
4. an ambiguous name answers its best match and lists the others;
5. the skill's line passed on to every subagent prompt;
6. the workflow switch `graph_shape` says which shape a step gets -- skill,
   loaded tools or off -- so the experiment can compare them.

Offline: no CLI, no cloud, no emulator.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import graphstage
from agent_worker.graphmcp import server as graph_server
from agent_worker.graphmcp import tools
from agent_worker.runners import claude_code, graph_hint_hook

from test_agents_use_tools import _graph_env, _run, _served_env, _servers
from test_dispatch_switches_worker import _arm_task, _prepare, _stage_record
from test_graphmcp_server import FETCH, GET_USER, LOAD_USER, TEST_LOAD, ask, rows, snapshot  # noqa: F401
from test_graphmcp_server import built  # noqa: F401 -- a fixture `snapshot` needs
from test_graphstage import TENANT, _FakeTicket, _promote, clone  # noqa: F401

FLAG = claude_code.APPEND_SYSTEM_PROMPT_FLAG


def _tokens(text: str) -> int:
    """The repository's schema-budget estimate: 3 bytes a token (§7.7)."""
    return -(-len(text.encode()) // 3)


# --------------------------------------------------------------------------
# (4) an ambiguous name is answered, never an error
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool, arguments", [
    ("context", {"symbol": "render"}),
    ("impact", {"symbol": "render"}),
    ("call_graph", {"symbol": "render"}),
    ("neighbours", {"symbol": "render"}),
])
def test_every_symbol_tool_answers_an_ambiguous_name_with_its_best_match(
        snapshot, tool, arguments):  # noqa: F811
    answer, failed = ask(snapshot, tool, arguments)
    # MUTATION: raise on two exact matches again and this is an error answer.
    assert not failed, answer
    assert {answer["symbol"], *answer["also_matched"]} == {"lib/a.py#render", "lib/b.py#render"}


def test_resolve_still_refuses_for_the_context_packs_seeds(snapshot):  # noqa: F811
    from agent_worker.graphmcp.snapshot import Snapshot

    snap = Snapshot(snapshot)
    with pytest.raises(tools._Unresolved):
        tools.resolve(snap, "render")
    best, others = tools.resolve_best(snap, "load_user")
    assert (best, others) == (LOAD_USER, [])


def test_a_name_nothing_matches_is_still_unknown(snapshot):  # noqa: F811
    answer, failed = ask(snapshot, "context", {"symbol": "nothing_named_this"})
    assert failed and "no symbol named" in answer["error"]


# --------------------------------------------------------------------------
# (2) the one scoped query
# --------------------------------------------------------------------------

def test_slice_of_a_symbol_gives_callers_and_covering_tests_by_default(snapshot):  # noqa: F811
    answer, failed = ask(snapshot, "slice", {"symbols": ["load_user"]})
    assert not failed, answer
    assert answer["symbol"] == LOAD_USER and answer["depth"] == 1
    assert answer["include"] == ["callers", "tests"]
    assert [r["symbol"] for r in rows(answer, "caller")] == [GET_USER]
    assert TEST_LOAD in {r["test"] for r in rows(answer, "test")}
    # Tests are tests, not callers (as in `impact`).
    assert all(not r["symbol"].startswith("tests/") for r in rows(answer, "caller"))
    assert not rows(answer, "callee")


def test_slice_depth_walks_further_and_include_picks_the_sections(snapshot):  # noqa: F811
    one, _ = ask(snapshot, "slice", {"symbols": [FETCH], "include": ["callers"]})
    two, _ = ask(snapshot, "slice", {"symbols": [FETCH], "depth": 2, "include": ["callers"]})
    assert GET_USER not in {r["symbol"] for r in rows(one, "caller")}
    assert {r["symbol"]: r["depth"] for r in rows(two, "caller")}[GET_USER] == 2
    assert not rows(two, "test")
    down, _ = ask(snapshot, "slice", {"symbols": [LOAD_USER], "include": ["callees"]})
    assert FETCH in {r["symbol"] for r in rows(down, "callee")}


def test_slice_of_a_file_covers_every_symbol_it_defines(snapshot):  # noqa: F811
    answer, failed = ask(snapshot, "slice", {"files": ["src/pkg/users.py"],
                                             "include": ["callers", "tests", "docs"]})
    assert not failed, answer
    assert answer["file"] == "src/pkg/users.py" and answer["roots"] > 1
    assert "docs" in answer


def test_slice_answers_an_ambiguous_name_with_the_others_listed(snapshot):  # noqa: F811
    answer, failed = ask(snapshot, "slice", {"symbols": ["render"]})
    assert not failed
    best, *others = answer["also_matched"]["render"]
    assert best == answer["symbol"] and others


@pytest.mark.parametrize("arguments", [
    {}, {"files": ["../outside.py"]}, {"symbols": ["load_user"], "depth": 4},
    {"symbols": ["load_user"], "include": ["everything"]}, {"symbols": ["x"], "image": "y"},
])
def test_slice_refuses_what_it_does_not_take(snapshot, arguments):  # noqa: F811
    answer, failed = ask(snapshot, "slice", arguments)
    assert failed and answer["error"]


def test_slice_pages_under_the_cap(snapshot, monkeypatch):  # noqa: F811
    whole, _ = ask(snapshot, "slice", {"files": ["src/pkg/users.py", "src/pkg/store.py"],
                                       "depth": 3, "include": list(tools.SLICE_INCLUDES)})
    monkeypatch.setattr(tools, "ANSWER_CAP", len(tools.dumps({**whole, "rows": whole["rows"][:2]})))
    seen: list[dict] = []
    cursor = None
    while True:
        page, _ = ask(snapshot, "slice", {"files": ["src/pkg/users.py", "src/pkg/store.py"],
                                          "depth": 3, "include": list(tools.SLICE_INCLUDES),
                                          **({"cursor": cursor} if cursor else {})})
        seen += page["rows"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == whole["rows"]


# --------------------------------------------------------------------------
# what each shape's server lists
# --------------------------------------------------------------------------

def test_the_skill_shape_lists_the_slice_alone_and_the_tools_shape_the_ten():
    assert tools.listed("skill") == [tools.SLICE_TOOL]
    assert tools.listed("tools") == tools.TOOLS
    assert tools.listed(None) == tools.TOOLS
    assert tools.SLICE_TOOL["name"] not in tools.TOOL_NAMES


def test_the_skill_shapes_server_answers_tools_list_with_the_slice_over_stdio(built):  # noqa: F811
    _repo, snap, _document = built
    argv = graphstage.server_argv(str(snap), None, graphstage.SHAPE_SKILL)
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "slice", "arguments": {"symbols": ["load_user"]}}}]
    done = subprocess.run([sys.executable, *argv[1:]] if argv[0] == sys.executable else argv,
                          input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, timeout=60)
    answers = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert answers[0]["result"]["instructions"] == graph_server.SKILL_INSTRUCTIONS
    assert [t["name"] for t in answers[1]["result"]["tools"]] == ["slice"], done.stderr
    assert answers[2]["result"]["isError"] is False


# --------------------------------------------------------------------------
# (1) the skill, (6) the shape, in the runner
# --------------------------------------------------------------------------

def _skill(tmp_path: Path) -> Path:
    return tmp_path / "work" / claude_code.GRAPH_SKILL_DIR / "SKILL.md"


def _system(seen: dict) -> str | None:
    args = seen["args"]
    return args[args.index(FLAG) + 1] if FLAG in args else None


def _settings(seen: dict) -> dict[str, Any]:
    args = seen["args"]
    return json.loads(Path(args[args.index("--settings") + 1]).read_text())


def _hint_hooks(settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in settings["hooks"]["PreToolUse"]
            if any(str(claude_code.GRAPH_HINT_HOOK) in h["command"] for h in entry["hooks"])]


@pytest.mark.parametrize("env_of", [_graph_env, _served_env], ids=["staged", "served"])
@pytest.mark.parametrize("chosen", [None, graphstage.SHAPE_SKILL], ids=["default", "switch"])
def test_a_graph_on_step_gets_the_skill_not_the_ten_tools(monkeypatch, tmp_path, env_of, chosen):
    env = env_of(tmp_path)
    if chosen:
        env[graphstage.SHAPE_ENV] = chosen
    seen = _run(monkeypatch, tmp_path, env)
    text = _skill(tmp_path).read_text()
    front = text.split("---\n")[1]
    assert f"name: {claude_code.GRAPH_SKILL_NAME}\n" in front
    assert f"description: {claude_code.GRAPH_SKILL_DESCRIPTION}\n" in front
    for moment in ("before changing", "before finishing", "spans modules"):
        assert moment in claude_code.GRAPH_SKILL_DESCRIPTION
    assert claude_code.GRAPH_SLICE_TOOL in text
    entry = _servers(seen)[graphstage.SERVER_NAME]
    # MUTATION: keep `alwaysLoad` and the step pays for the schema every turn.
    assert claude_code.ALWAYS_LOAD not in entry
    assert entry["args"][-2:] == ["--shape", graphstage.SHAPE_SKILL]
    assert claude_code.GRAPH_FIRST_NOTE not in seen["prompt"]
    # An implement step: the lookup line names the skill, in place of the
    # exploration line's clause (BGD, 2026-11-06).
    assert claude_code.GRAPH_LOOKUP_SKILL in seen["prompt"]
    assert claude_code.EXPLORATION_SKILL_CLAUSE not in seen["prompt"]
    system = _system(seen)
    assert system == claude_code.GRAPH_SKILL_SYSTEM_NOTE
    assert len(_hint_hooks(_settings(seen))) == 1


def test_the_tools_shape_is_batch_two_exactly(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {**_graph_env(tmp_path),
                                        graphstage.SHAPE_ENV: graphstage.SHAPE_TOOLS})
    entry = _servers(seen)[graphstage.SERVER_NAME]
    assert entry[claude_code.ALWAYS_LOAD] is True and "--shape" not in entry["args"]
    assert claude_code.GRAPH_FIRST_NOTE in seen["prompt"]
    assert _system(seen) == claude_code.GRAPH_SYSTEM_NOTE
    assert not _skill(tmp_path).exists()
    assert _hint_hooks(_settings(seen)) == []


def test_a_graph_off_step_gets_no_skill_and_no_hint_and_clears_an_old_one(monkeypatch, tmp_path):
    stale = _skill(tmp_path)
    stale.parent.mkdir(parents=True)
    stale.write_text("left by an earlier attempt\n")
    seen = _run(monkeypatch, tmp_path, {graphstage.SHAPE_ENV: graphstage.SHAPE_SKILL})
    assert not stale.parent.exists()
    assert _hint_hooks(_settings(seen)) == []
    assert _system(seen) is None


def test_the_skill_costs_a_fraction_of_the_loaded_tools_every_turn():
    """The first-turn prefix each shape adds, at the budget's 3 bytes a token."""
    loaded = (_tokens(json.dumps({"tools": tools.TOOLS}, separators=(",", ":")))
              + _tokens(graph_server.INSTRUCTIONS) + _tokens(claude_code.GRAPH_FIRST_NOTE)
              + _tokens(claude_code.GRAPH_SYSTEM_NOTE)
              + _tokens(claude_code.EXPLORATION_GRAPH_CLAUSE))
    skill = (_tokens(claude_code.GRAPH_SKILL_NAME + claude_code.GRAPH_SKILL_DESCRIPTION)
             + _tokens(graph_server.SKILL_INSTRUCTIONS)
             + _tokens(claude_code.GRAPH_SKILL_SYSTEM_NOTE)
             + _tokens(claude_code.EXPLORATION_SKILL_CLAUSE))
    assert _tokens(claude_code.GRAPH_SKILL_DESCRIPTION) <= 110
    assert skill * 4 < loaded, (skill, loaded)


# --------------------------------------------------------------------------
# (3) the nudge and (5) the subagent line
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pattern, name", [
    ("cancel_workflow", "cancel_workflow"), ("GraphServer", "GraphServer"),
    ("def handle_row(", "handle_row"), (r"\bresolveBest\b", "resolveBest"),
    ("class TicketFile", "TicketFile"), ("_private_helper", "_private_helper"),
])
def test_an_identifier_shaped_pattern_is_a_symbol(pattern, name):
    assert graph_hint_hook.symbol_in(pattern) == name


@pytest.mark.parametrize("pattern", [
    "TODO", "error", "SHOWN_CANDIDATES", "foo|bar", "def ", "a_b", "some phrase here",
    "cancel_.*", "",
])
def test_a_word_regex_constant_or_phrase_is_not(pattern):
    assert graph_hint_hook.symbol_in(pattern) is None


@pytest.mark.parametrize("command", [
    "grep -rn cancel_workflow apps/", "rg -n 'cancel_workflow' apps", "git grep -n cancel_workflow",
    "cd apps && grep -rn --include=*.py -e cancel_workflow .", "timeout 5 rg cancel_workflow | head",
])
def test_a_grep_like_command_is_read_for_its_pattern(command):
    tail = command.split("timeout 5 ")[-1]
    assert graph_hint_hook.searched_symbol("Bash", {"command": tail}) == "cancel_workflow"


def _hook(state: Path, call: dict[str, Any]) -> dict[str, Any] | None:
    done = subprocess.run([sys.executable, "-I", str(claude_code.GRAPH_HINT_HOOK), str(state)],
                          input=json.dumps(call), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"] if done.stdout.strip() else None


def test_the_hint_is_one_line_once_a_name_and_at_most_three_a_step(tmp_path):
    state = tmp_path / "graph-hint-state.json"
    first = _hook(state, {"tool_name": "Grep", "tool_input": {"pattern": "cancel_workflow"}})
    assert first is not None and "permissionDecision" not in first
    line = first["additionalContext"]
    assert "\n" not in line and "cancel_workflow" in line and claude_code.GRAPH_SLICE_TOOL in line
    assert _hook(state, {"tool_name": "Grep", "tool_input": {"pattern": "cancel_workflow"}}) is None
    for name in ("load_user", "GraphServer"):
        assert _hook(state, {"tool_name": "Bash",
                             "tool_input": {"command": f"grep -rn {name} ."}}) is not None
    # MUTATION: drop the cap and every search carries a line.
    assert _hook(state, {"tool_name": "Grep", "tool_input": {"pattern": "fourth_name"}}) is None
    assert graph_hint_hook.MAX_HINTS == 3


def test_a_subagent_prompt_carries_the_skills_line_once(tmp_path):
    state = tmp_path / "graph-hint-state.json"
    tool_input = {"description": "explore", "prompt": "Find the callers.", "subagent_type": "x"}
    out = _hook(state, {"tool_name": "Task", "tool_input": tool_input})
    assert out is not None and "permissionDecision" not in out
    updated = out["updatedInput"]
    assert updated["prompt"] == f"Find the callers.\n\n{graph_hint_hook.SUBAGENT_LINE}"
    assert {k: v for k, v in updated.items() if k != "prompt"} == {
        "description": "explore", "subagent_type": "x"}
    assert _hook(state, {"tool_name": "Agent", "tool_input": updated}) is None


def test_the_hook_fails_open(tmp_path):
    done = subprocess.run([sys.executable, "-I", str(claude_code.GRAPH_HINT_HOOK),
                           str(tmp_path / "state.json")], input="not json",
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stdout == ""


def test_the_hint_and_the_skill_name_the_same_tool():
    assert graph_hint_hook.SLICE_TOOL == claude_code.GRAPH_SLICE_TOOL
    assert graph_hint_hook.SKILL == claude_code.GRAPH_SKILL_NAME


# --------------------------------------------------------------------------
# (6) the switch, in the worker
# --------------------------------------------------------------------------

def test_shape_of_reads_the_three_words_and_nothing_else():
    for word in ("skill", "tools", "off"):
        assert graphstage.shape_of({"graph_shape": word}) == word
    for other in ("on", "SKILL", None, 1):
        assert graphstage.shape_of({"graph_shape": other}) is None
    assert graphstage.graph_switch({"graph_shape": "off", "code_intelligence": "on"}) == "off"
    assert graphstage.graph_switch({"graph_shape": "tools", "code_intelligence": "on"}) == "on"
    assert graphstage.shape_env(None) == {} and graphstage.shape_env("off") == {}
    assert graphstage.shape_env("tools") == {graphstage.SHAPE_ENV: "tools"}


@pytest.mark.parametrize("shape", ["skill", "tools"])
def test_a_chosen_shape_reaches_the_runner_and_stage_json(db, store, clone, shape):  # noqa: F811
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "ab"}})
    env = _prepare(db, store, checkout, second, ticket=_FakeTicket(), task_id=_arm_task("A"),
                   dispatch={"code_intelligence": "on", "graph_shape": shape})
    assert graphstage.SNAPSHOT_ENV in env and env[graphstage.SHAPE_ENV] == shape
    line = _stage_record(checkout)[graphstage.SWITCHES_FIELD]["graph_shape"]
    assert line == {"value": shape, "shape": shape, "source": "workflow switch"}
    summary = graphstage.switches_summary(checkout.parent, {"graph_shape": shape})
    assert summary is not None and summary["graph_shape"] == line


def test_graph_shape_off_stages_nothing(db, store, clone):  # noqa: F811
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {
        "context": "on", "context_ab": "off"}})
    ticket = _FakeTicket()
    env = _prepare(db, store, checkout, second, ticket=ticket,
                   dispatch={"code_intelligence": "on", "graph_shape": "off"})
    assert ticket.started == []
    for name in (graphstage.SNAPSHOT_ENV, graphstage.PIN_ENV, graphstage.SHAPE_ENV):
        assert name not in env
    record = _stage_record(checkout)[graphstage.SWITCHES_FIELD]
    assert record["graph_shape"] == {"value": "off", "shape": None, "source": "workflow switch"}
    assert record["code_intelligence"]["graph"] is False


def test_no_shape_chosen_hands_the_runner_todays_variables(db, store, clone):  # noqa: F811
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "on"}})
    env = _prepare(db, store, checkout, second, ticket=_FakeTicket(), dispatch=None)
    assert graphstage.SNAPSHOT_ENV in env and graphstage.SHAPE_ENV not in env
    assert claude_code.graph_shape(env) == graphstage.DEFAULT_SHAPE == graphstage.SHAPE_SKILL
