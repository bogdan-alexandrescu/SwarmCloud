"""The sc plugin's SwarmCloud pane draws proposal B and reads only on a click (SC-BROWSER-BUILD, #2318).

BGD picked proposal B, Two columns, for every frame on 2026-10-25 (PR 2328).
The pane is a Claude Code mod (plugin/hooks/pane.tsx); everything it draws and
everything a click reads is plugin/hooks/pane/model.ts, which these tests run
under plain node -- Node 24 strips its types -- with no engine, no network and
no credentials. What this file holds:

* STAGES ARE THE CONSOLE'S. The pane's Level 2 stages are dag.ts's own
  `levelsOf`, RUN over the same steps (test_plugin_rows_match_console's
  `_console_levels`), by name (`Stage N`) and by count.
* ONE CALL PER CLICK, NONE OTHERWISE. Opening the pane, a workflow, a stage or
  an agent, Refresh and Retry each make exactly one `swarm_browse` call to the
  sc plugin's server; going back makes none; drawing makes none (`frame` has
  no way to call); the module starts no timer and reaches no model.
* B'S FRAMES. The empty state, the unreachable state, Levels 1-4 and the
  60-column frame, drawn at B's widths, equal B's text cell for cell
  (fixtures/sc_browser_b_frames.json), the key hints aside: in Claude Code a
  focused pane's arrows scroll and Tab moves the focus, so the pane says Tab
  where B drew `↑↓`.
* WITH THE CLAUDE CLI, THE ENGINE ITSELF: `claude plugin validate` and
  `claude plugin test` (plugin/hooks/pane.test.ts) run where the CLI is
  installed. CI's runner has none, so there they skip and say so.

WHAT THIS CANNOT PROVE: how a live Claude Code session seats the pane (dock or
inline, at which width), that `/mcp` names the plugin's server
`plugin:sc:swarmcloud`, or that function hooks are on in a person's build
(early access: CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1). A live session is the
measurement for those.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_plugin_agents_and_workflows import _PLUGIN, _node
from test_plugin_rows_match_console import _FIXTURE, _console_levels

_MODEL = _PLUGIN / "hooks" / "pane" / "model.ts"
_PANE = _PLUGIN / "hooks" / "pane.tsx"
_HOOKS = _PLUGIN / "hooks" / "hooks.json"
_B = json.loads((Path(__file__).parent / "fixtures" / "sc_browser_b_frames.json").read_text())

READ_AT = "2026-10-25T10:42:07Z"
FAILED_AT = "2026-10-25T10:48:12Z"


def _js(tmp_path: Path, body: str) -> object:
    """Run `body` as an ES module with `m` the pane's model; it prints JSON."""
    script = tmp_path / f"probe{len(list(tmp_path.glob('probe*.mjs')))}.mjs"
    script.write_text(f"const m = await import({json.dumps(_MODEL.as_uri())})\n{body}\n")
    done = subprocess.run([_node(), str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --------------------------------------------------------------------------
# B's COUNTS-REVIEW, as swarm_browse serves it
# --------------------------------------------------------------------------

_MIDDLE_STATES = ["SUCCEEDED"] * 5 + ["FAILED", "SUCCEEDED", "RUNNING", "RUNNING", "RUNNING", "PARKED",
                                      "SUCCEEDED", "SUCCEEDED"]
_MIDDLE = [{"step_id": f"counts-{c}", "task_id": f"task_counts-{c}", "depends_on": ["plan"], "state": s}
           for c, s in zip("abcdefghijklm", _MIDDLE_STATES)]
_STEPS = ([{"step_id": "plan", "task_id": "task_plan", "depends_on": [], "state": "SUCCEEDED"}] + _MIDDLE
          + [{"step_id": "integrate", "task_id": "task_integrate", "depends_on": [s["step_id"] for s in _MIDDLE],
              "state": "PARKED", "park_reason": "DEPENDENCY_INCOMPLETE"}])
WORKFLOW = {"workflow_id": "wf_counts", "name": "COUNTS-REVIEW", "state": "RUNNING", "steps": _STEPS,
            "duration_s": 2 * 3600 + 5 * 60, "tokens": 7_600_000, "cost_usd": 30.11}
_NOTES = {"counts-a": "PR #2290", "counts-b": "PR #2291", "counts-c": "PR #2292", "counts-d": "PR #2293",
          "counts-e": "PR #2294", "counts-f": "vitest: 2 failed", "counts-g": "PR #2296",
          "counts-h": "rendering shots 8 of 14", "counts-i": "vitest 3 of 6 files", "counts-j": "reading the brief",
          "counts-k": "quota, holds nothing", "counts-l": "PR #2298", "counts-m": "PR #2299"}
AGENTS = [{"task_id": s["task_id"], "step_id": s["step_id"], "state": s["state"], "note": _NOTES[s["step_id"]]}
          for s in _MIDDLE]
AGENT = {"task_id": "task_counts-f", "step_id": "counts-f", "state": "FAILED", "attempts": 2, "max_attempts": 2,
         "duration_s": 38 * 60, "cost_usd": 4.12, "tokens": 1_210_000,
         "last_error": "exit 1 · vitest: 2 failed in counts.table.test.tsx",
         "pr_url": "https://github.com/acme/app/pull/2295",
         "artifacts": ["review.md", "shots/ (14 files)", "pr-body.md", "vitest.log"],
         "console": "https://swarm.example.com/agents/task_7d227b6f"}


def _state(**fields) -> dict:
    """A PaneState as model.initialState() spells it, with `fields` over it."""
    base = {"level": "workflows", "workflows": None, "singles": [], "listReadAt": None, "workflowId": None,
            "workflow": None, "workflowReadAt": None, "stage": None, "agents": None, "agentsReadAt": None,
            "taskId": None, "agent": None, "agentReadAt": None, "agentFrom": "agents", "layout": None,
            "layoutNote": None, "failure": None, "calls": 0}
    return {**base, **fields}


def _frame(tmp_path, state: dict, cols: int, narrow: bool = False) -> list[str]:
    return _js(tmp_path, f"""
const lines = m.frame({json.dumps(state)}, {{ cols: {cols}, narrow: {json.dumps(narrow)}, now: '' }})
process.stdout.write(JSON.stringify(lines.map((l) => l.map((s) => s.text).join(''))))""")


def _same_but_hint(got: list[str], want: list[str]) -> None:
    """Every line equal but the last (the key hint), which keeps B's width."""
    assert len(got) == len(want), "\n".join(got)
    for index, (line, expected) in enumerate(zip(got[:-1], want[:-1])):
        assert line == expected, f"line {index}:\n got {line!r}\nwant {expected!r}"
    assert len(got[-1]) == len(want[-1])


# --------------------------------------------------------------------------
# Stages are the console's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("steps", [_FIXTURE["chain"]["steps"], _FIXTURE["parallel"]["steps"], _STEPS],
                         ids=["chain", "parallel", "counts-review"])
def test_the_panes_level_2_stages_are_the_consoles_levels_by_name_and_count(tmp_path, steps):
    served = [{"step_id": s["step_id"], "task_id": "task_" + s["step_id"],
               "depends_on": list(s.get("depends_on") or []), "state": s.get("state")} for s in steps]
    console = _console_levels(served)
    pane = _js(tmp_path, f"""
const stages = m.stagesOf({json.dumps(served)})
process.stdout.write(JSON.stringify(stages.map((s) => [s.name, s.steps.map((x) => x.step_id)])))""")
    # MUTATION: number stages from the spec's `stage` key -- "Report"/"Scans" differ from the DAG.
    assert [name for name, _ in pane] == [f"Stage {i + 1}" for i in range(len(console))]
    assert [ids for _, ids in pane] == console  # MUTATION: depth from the first parent -- the join moves.
    assert [len(ids) for _, ids in pane] == [len(level) for level in console]


def test_a_stages_census_is_the_consoles_band_failures_first_then_biggest(tmp_path):
    got = _js(tmp_path, f"""
const stage = m.stagesOf({json.dumps(_STEPS)})[1]
process.stdout.write(JSON.stringify([m.shapeText(m.stagesOf({json.dumps(_STEPS)})), stage.census]))""")
    assert got[0] == "1 → 13 → 1"
    assert got[1] == [{"word": "failed", "n": 1}, {"word": "succeeded", "n": 8},  # MUTATION: biggest first only -- succeeded leads.
                      {"word": "running", "n": 3}, {"word": "parked", "n": 1}]


# --------------------------------------------------------------------------
# One call per click, none otherwise
# --------------------------------------------------------------------------

_ACT = """
const calls = []
const call = async (tool, args) => { calls.push({ tool, args }); return { ok: true, data: REPLY } }
const after = await m.act(STATE, ACTION, call, () => '2026-10-25T10:42:07Z')
process.stdout.write(JSON.stringify({ calls, after }))
"""


def _act(tmp_path, state: dict, action: str, reply: dict | None = None) -> dict:
    body = (_ACT.replace("STATE", json.dumps(state)).replace("ACTION", json.dumps(action))
            .replace("REPLY", json.dumps(reply or {})))
    return _js(tmp_path, body)


def _at_level_2() -> dict:
    return _state(level="stages", workflowId="wf_counts", workflow=WORKFLOW, workflowReadAt=READ_AT,
                  workflows=[WORKFLOW], listReadAt=READ_AT)


def test_opening_a_stage_makes_exactly_one_mcp_call_and_nothing_else(tmp_path):
    out = _act(tmp_path, _at_level_2(), "stage:2", {"read_at": READ_AT, "agents": AGENTS})
    assert out["calls"] == [{"tool": "swarm_browse", "args": {  # MUTATION: one call per agent -- 13 calls.
        "level": "agents", "task_ids": [s["task_id"] for s in _MIDDLE]}}]
    assert out["after"]["level"] == "agents" and out["after"]["stage"] == 2
    assert out["after"]["calls"] == 1  # MUTATION: do not count it -- the empty state's footer would lie.


@pytest.mark.parametrize(("state", "action", "args"), [
    (_state(), "open", {"level": "workflows"}),
    (_state(workflows=[WORKFLOW]), "open:wf_counts", {"level": "stages", "workflow_id": "wf_counts"}),
    (_at_level_2(), "agent:task_counts-f", {"level": "agent", "task_id": "task_counts-f"}),
    (_at_level_2(), "refresh", {"level": "stages", "workflow_id": "wf_counts"}),
], ids=["open the pane", "open a workflow", "open an agent", "refresh level 2"])
def test_every_click_that_reads_makes_exactly_one_swarm_browse_call(tmp_path, state, action, args):
    out = _act(tmp_path, state, action, {"read_at": READ_AT, "workflows": [], "workflow": WORKFLOW})
    assert out["calls"] == [{"tool": "swarm_browse", "args": args}]  # MUTATION: re-read the list on every open -- two calls.


@pytest.mark.parametrize("action", ["back:workflows", "back:stages", "back:agents", "nonsense"])
def test_going_back_reads_nothing(tmp_path, action):
    state = _state(level="agents", workflowId="wf_counts", workflow=WORKFLOW, stage=2, agents=AGENTS)
    assert _act(tmp_path, state, action)["calls"] == []  # MUTATION: refresh on back -- a call.


def test_drawing_reads_nothing_and_the_module_starts_no_timer_and_reaches_no_model():
    model = _MODEL.read_text()
    pane = _PANE.read_text()
    # `frame` takes no way to call anything: drawing cannot read.
    assert re.search(r"export function frame\(state: PaneState, view: View\): Line\[\]", model)
    for source in (model, pane):
        for forbidden in ("$.clock.every", "$.clock.after", "$.clock.sleep", "setTimeout", "setInterval",
                          "$.model.", "$.agent.", "$.prompt.submit", "$.tool."):
            assert forbidden not in source, f"{forbidden} would read on a timer or reach the model"  # MUTATION: a 30 s refresh timer.
    # The one command it runs is the re-attach after the pane's Layout control
    # saves (SC-BROWSER-FOLLOWUP, test_sc_browser_followup.py): a chat turn the
    # person's switch asked for, never a timer's.
    assert pane.count("$.command.run(") <= 1
    # The only MCP calls are the click runner's and /sc layout's.
    assert pane.count("$.mcp.call(") == 2
    # `run` is called from a press and from the two commands that open the pane -- never at session start.
    start = pane.index("on('session.start'")
    assert "run(" not in pane[start:pane.index("})", start)]  # MUTATION: read the list when the session starts.
    assert re.findall(r"run\(\$, ([a-z']+)\)", pane) == ["'open'", "'open'", "press"]


def test_the_hooks_json_loads_the_pane_module_beside_the_session_start_hook():
    hooks = json.loads(_HOOKS.read_text())
    assert hooks["modules"] == ["./pane.tsx"]  # MUTATION: no module -- the pane never loads.
    assert "SessionStart" in hooks["hooks"]


def test_retry_repeats_the_read_that_failed(tmp_path):
    failed = _state(workflows=[WORKFLOW], listReadAt=READ_AT,
                    failure={"tool": "swarm_browse", "reason": "x", "at": FAILED_AT, "action": "open:wf_counts"})
    out = _act(tmp_path, failed, "retry", {"read_at": READ_AT, "workflow": WORKFLOW})
    assert out["calls"] == [{"tool": "swarm_browse", "args": {"level": "stages", "workflow_id": "wf_counts"}}]  # MUTATION: retry the level -- the list.
    assert out["after"]["failure"] is None and out["after"]["level"] == "stages"


def test_a_call_that_throws_or_errors_is_the_unreachable_state_with_the_bridges_reason(tmp_path):
    got = _js(tmp_path, """
const thrown = await m.act(m.initialState(), 'open', async () => { throw new Error('connect: connection refused') }, () => 'T')
const refused = m.parseResult({ content: [{ type: 'text', text: 'GET /v1/workflows answered 503' }], isError: true })
const parsed = m.parseResult({ content: [{ type: 'text', text: '{"a": 1}' }], isError: false })
process.stdout.write(JSON.stringify([thrown.failure, refused, parsed]))""")
    assert got[0] == {"tool": "swarm_browse", "at": "T", "action": "open", "reason": (
        "the sc plugin's SwarmCloud server is not connected in this session (connect: connection refused).")}
    assert got[1] == {"ok": False, "reason": "GET /v1/workflows answered 503"}  # MUTATION: drop the bridge's words.
    assert got[2] == {"ok": True, "data": {"a": 1}}


# --------------------------------------------------------------------------
# B's frames
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cols", ["66", "53"])
def test_the_empty_state_draws_as_b(tmp_path, cols):
    state = _state(workflows=[], listReadAt=READ_AT, calls=1)
    assert _frame(tmp_path, state, int(cols)) == _B["empty"][cols]  # MUTATION: a one-line "nothing here".


@pytest.mark.parametrize("cols", ["66", "53"])
def test_the_unreachable_state_draws_as_b(tmp_path, cols):
    state = {**_at_level_2(), "failure": {
        # B's mock-up names the read it imagined, swarm_workflow_status; the
        # pane names whichever tool failed, so the state carries B's name here.
        "tool": "swarm_workflow_status", "at": FAILED_AT, "action": "refresh",
        "reason": "the sc plugin's SwarmCloud server is not connected in this session (connect: connection refused)."}}
    # MUTATION: wrap the reason at the column's full width -- the lines move.
    assert _frame(tmp_path, state, int(cols)) == _B["unreachable"][cols]


@pytest.mark.parametrize("cols", ["66", "53"])
def test_level_2_draws_as_b(tmp_path, cols):
    _same_but_hint(_frame(tmp_path, _at_level_2(), int(cols)), _B["level2"][cols])


@pytest.mark.parametrize("cols", ["66", "53"])
def test_level_3_draws_as_b_one_line_each_or_two_where_one_does_not_fit(tmp_path, cols):
    state = {**_at_level_2(), "level": "agents", "stage": 2, "agents": AGENTS, "agentsReadAt": READ_AT}
    _same_but_hint(_frame(tmp_path, state, int(cols)), _B["level3"][cols])  # MUTATION: never split -- the 53 frame overflows.


@pytest.mark.parametrize("cols", ["66", "53"])
def test_level_4_draws_as_b(tmp_path, cols):
    state = {**_at_level_2(), "level": "agent", "stage": 2, "agents": AGENTS, "taskId": "task_counts-f",
             "agent": AGENT, "agentReadAt": READ_AT}
    got, want = _frame(tmp_path, state, int(cols)), list(_B["level4"][cols])
    # B marks the pull request `(draft)`; swarm_browse serves no draft flag, so the pane says the number alone.
    want = [line.replace("#2295 (draft)", "#2295        ") for line in want]
    _same_but_hint(got, want)


@pytest.mark.parametrize("cols", ["66", "53"])
def test_level_1_lists_the_workflows_as_b(tmp_path, cols):
    rows = [WORKFLOW,
            {"workflow_id": "wf_live", "name": "LIVE-NOMASK", "state": "RUNNING", "duration_s": 3600 + 42 * 60,
             "tokens": 2_600_000, "steps": [{"step_id": f"s{i}", "task_id": f"t{i}", "depends_on": [f"s{i - 1}"] if i else [],
                                             "state": "SUCCEEDED" if i < 3 else "RUNNING"} for i in range(4)]}]
    got = _frame(tmp_path, _state(workflows=rows, listReadAt=READ_AT), int(cols))
    want = _B["level1"][cols]
    assert got[:5] == want[:5]  # MUTATION: one column fewer -- the rows shift.


def test_the_narrow_terminal_draws_one_column_as_b(tmp_path):
    state = {**_at_level_2(), "level": "agents", "stage": 2, "agents": AGENTS, "agentsReadAt": READ_AT}
    _same_but_hint(_frame(tmp_path, state, 60, narrow=True), _B["narrow"]["60"])  # MUTATION: two columns at 60.


def test_narrow_is_the_terminal_not_the_pane(tmp_path):
    got = _js(tmp_path, "process.stdout.write(JSON.stringify([m.isNarrow(60, 60), m.isNarrow(80, 53), m.isNarrow(144, 66)]))")
    assert got == [True, False, False]  # MUTATION: judge by the pane's width -- 53 is narrow.


# --------------------------------------------------------------------------
# The engine itself, where the claude CLI is installed
# --------------------------------------------------------------------------


def _claude() -> str:
    found = shutil.which("claude")
    if found is None:
        pytest.skip("the claude CLI is not installed here; `claude plugin validate` and `claude plugin test` "
                    "need it (CI's runner has none)")
    return found


def test_claude_plugin_validate_passes_and_lists_only_the_calls_the_pane_needs():
    done = subprocess.run([_claude(), "plugin", "validate", str(_PLUGIN)], capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    calls = next(line for line in done.stdout.splitlines() if "./pane.tsx calls:" in line)
    assert "$.mcp.call" in calls
    assert "$.clock.every" not in calls and "$.model" not in calls and "$.prompt" not in calls


def test_claude_plugin_test_runs_the_pane_against_the_engine(tmp_path):
    claude = _claude()
    copy = tmp_path / "plugin"
    shutil.copytree(_PLUGIN, copy)
    manifest = copy / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text())
    # `claude plugin test` reads no settings, so a REQUIRED option has no
    # value there and the engine refuses to load the module; an install always
    # has the deployment URL (it is asked for at install).
    data["userConfig"]["deployment_url"].pop("required", None)
    manifest.write_text(json.dumps(data))
    env = {**os.environ, "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"}
    done = subprocess.run([claude, "plugin", "test", str(copy)], capture_output=True, text=True, timeout=300,
                          check=False, env=env, cwd=copy)
    assert done.returncode == 0, done.stdout + done.stderr
    # SC-BROWSER-FOLLOWUP's seven beside the four: the re-attach, and the pane at session start.
    assert re.search(r"\b11 pass\b", done.stdout + done.stderr), done.stdout + done.stderr
