"""The sc plugin's third layout, the band above the prompt (SC-BAND, part of #2318).

BGD decided on 2026-10-26 (about 16:00Z): the SwarmCloud view moves from the
right-docked pane into a HORIZONTAL STRIP ABOVE THE PROMPT, with a one-line
summary BELOW the prompt, and in that layout the plugin starts no /workflows
rows at all. What this file holds, with no engine, no network and no
credentials:

* THE LAYOUT. `swarm_layout {layout: "band"}` saves and reads back per project,
  and every launch then reads `band` as its row mode.
* NO ROWS. In `band`, `swarm_workflow_launch` answers an attach, `attach:
  "all"` and a dispatch's rows with `launches: []`, `rows: "band"` and
  `followed_by: "band"`, and reads nothing to build a row; a spec's copy still
  submits, and run.js starts no follow row after it.
* THE STRIP'S MODEL (plugin/hooks/pane/model.ts, under plain node): the summary
  line and the line under the prompt, the band's frame at narrow and wide
  widths and folded, and `changes` -- a finished workflow is exactly one turn,
  an unchanged read none, the first read none.
* SESSION START. In `band` the SessionStart hook asks for nothing else: it runs
  no bridge listing, and still re-arms a remembered watch.
* THE ENGINE ITSELF, where the claude CLI is installed: test_sc_browser_pane's
  `claude plugin test` runs plugin/hooks/band.test.ts beside pane.test.ts.

WHAT THIS CANNOT PROVE: how a live fullscreen session lays the strip out, and
the PR counts and owner-question notices against a live bridge -- swarm_browse
does not serve `pull_requests`, `owner_questions` or `park_phase` on its
workflow rows yet (a question in the PR); the model reads them when served.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_mcp import launch, layout, progress, server
from swarm_mcp.client import SwarmError

from test_auto_attach import _hook
from test_plugin_agents_and_workflows import _PLUGIN, _SPEC, _SUBMITTED, _run
from test_plugin_row_split import SESSION, _Client, _envelope
from test_sc_browser_pane import AGENTS, READ_AT, WORKFLOW, _js, _state

_HOOK_SCRIPT = _PLUGIN / "hooks" / "session-start.sh"


@pytest.fixture(autouse=True)
def _bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "project").mkdir(exist_ok=True)
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "SWARM_PLUGIN_ROWS", "SWARM_PLUGIN_LAYOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(launch.os, "cpu_count", lambda: 10)
    launch.forget_attached()
    progress.HANDLES.clear()


def _layout_tool(**args) -> dict:
    return json.loads(server._call(None, "swarm_layout", args))


def _launch(client, **args) -> dict:
    return json.loads(server._call(client, "swarm_workflow_launch", args))


class _NoReads(_Client):
    """A client that fails any read: in `band` an attach builds no row, so it reads nothing."""

    def request(self, method, path, **_):
        raise AssertionError(f"read {path} to build a row the band layout never starts")


# --------------------------------------------------------------------------
# The layout
# --------------------------------------------------------------------------


def test_layout_band_is_saved_for_the_project_and_read_back(tmp_path):
    saved = _layout_tool(layout="band")
    assert saved["layout"] == "band" and saved["source"] == "saved for this project"  # MUTATION: no `band` -- "not a layout".
    files = list((tmp_path / "config" / "sc-layout").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["layout"] == "band"
    assert _layout_tool()["layout"] == "band"  # MUTATION: saved() drops band -- "rows", the default.
    assert layout.row_mode(None) == "band"  # MUTATION: no band row mode -- "summary", and rows start.
    assert layout.row_mode("per-step") == "per-step"  # a launch's own `rows` still wins
    _layout_tool(layout="rows")
    assert layout.row_mode(None) == "summary"


def test_the_layout_option_may_name_band_and_a_wrong_name_names_all_three(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_LAYOUT", "band")
    assert layout.current() == {**layout.current(), "layout": "band", "source": "the plugin's layout option"}
    with pytest.raises(SwarmError, match="`band`"):
        _layout_tool(layout="strip")


# --------------------------------------------------------------------------
# No rows in band
# --------------------------------------------------------------------------


def _no_rows(out: dict) -> None:
    assert out["launches"] == []  # MUTATION: launch as before -- one copy per workflow.
    assert out["rows"] == "band" and out["followed_by"] == "band"
    assert "strip above the prompt" in out["note"]
    assert "script_path" not in out


def test_an_attach_in_band_launches_no_row_and_says_why():
    _layout_tool(layout="band")
    out = _launch(_NoReads(_envelope(3)), attach="wf_big")
    _no_rows(out)
    assert out["attach"] == "wf_big"
    assert out["arm_watch"] is not None  # arming the /sc check watch is unchanged for an attach


@pytest.mark.parametrize("project", [False, True])
def test_attach_all_in_band_launches_no_row_and_reads_nothing(project):
    _layout_tool(layout="band")
    out = _launch(_NoReads(_envelope(3)), attach="all", **({"project": True} if project else {}))
    _no_rows(out)


def test_a_dispatchs_rows_in_band_are_none_and_the_dispatch_is_still_recorded(tmp_path):
    _layout_tool(layout="band")
    out = launch.for_tasks([{"task_id": "task_1", "label": "one"}, {"task_id": "task_2", "label": "two"}])
    _no_rows(out)
    assert out["task_ids"] == ["task_1", "task_2"]
    record = (tmp_path / "project" / ".sc-swarmcloud-runs" / "launched.jsonl").read_text()
    assert record.count('"dispatch"') == 2  # MUTATION: return before recording -- the build board loses them.


def test_a_spec_in_band_is_still_submitted_by_its_run_and_followed_by_the_band():
    _layout_tool(layout="band")
    out = _launch(None, spec=_SPEC)
    assert Path(out["script_path"]).exists()  # the run that submits it
    assert out["args"]["rows"] == "band" and out["rows"] == "band"  # MUTATION: summary -- run.js starts a row.
    assert out["followed_by"] == "band"
    assert "starts no row" in Path(out["script_path"]).read_text().split("\n", 3)[2]


def test_run_js_submits_a_band_spec_and_starts_no_follow_row(tmp_path):
    got = _run(tmp_path, {"spec": _SPEC, "rows": "band"}, {"SUBMIT": _SUBMITTED})
    assert "error" not in got, got.get("error")
    kinds = [c["agentType"] for c in got["calls"]]
    assert len(kinds) == 1 and got["calls"][0]["prompt"].startswith("SUBMIT")  # MUTATION: oneRow(band) -- a summary row.
    result = got["result"]
    assert (result["workflow_id"], result["state"], result["followed_by"]) == ("wf_1", "SUBMITTED", "band")
    assert any("strip above the prompt" in line for line in got["logs"])


def test_run_js_attaches_nothing_for_a_band_copy(tmp_path):
    got = _run(tmp_path, {"attach": "wf_c", "title": "COUNTS-REVIEW", "rows": "band"}, {})
    assert got["calls"] == []  # MUTATION: read the workflow and start its row -- an ATTACH call.
    assert got["result"]["followed_by"] == "band"
    tasks = _run(tmp_path, {"attach_tasks": [{"task_id": "task_1", "title": "one"}], "rows": "band"}, {})
    assert tasks["calls"] == [] and tasks["result"]["followed_by"] == "band"


# --------------------------------------------------------------------------
# The strip's model, under node
# --------------------------------------------------------------------------

_PULL = "https://github.com/acme/app/pull/2463"
_WAITING = {"workflow_id": "wf_ask", "name": "ASK-OWNER", "state": "RUNNING",
            "steps": [{"step_id": "ask", "task_id": "task_ask", "depends_on": [], "state": "PARKED",
                       "park_reason": "AWAITING_INPUT"}]}


def _band(tmp_path, expression: str, **values) -> object:
    names = "".join(f"const {k} = {json.dumps(v)}\n" for k, v in values.items())
    return _js(tmp_path, names + f"process.stdout.write(JSON.stringify({expression}))")


def test_the_summary_and_the_line_under_the_prompt(tmp_path):
    running = _state(workflows=[WORKFLOW, _WAITING, {**WORKFLOW, "workflow_id": "wf_done", "state": "SUCCEEDED",
                                                      "pull_requests": [_PULL, _PULL + "0"]}],
                     singles=[{"task_id": "task_x", "state": "RUNNING"}], listReadAt=READ_AT)
    got = _band(tmp_path, "[m.summaryText(s), m.statusText(s), m.anyRunning(s)]", s=running)
    # Three running (two workflows and a single task), one step waiting, two pull requests.
    assert got == ["SwarmCloud · 3 running · 1 waits on you · 2 PRs", "SC 3 running · 1 waits on you · 2 PRs", True]
    done = _state(workflows=[{**WORKFLOW, "state": "SUCCEEDED"}], listReadAt=READ_AT)
    got = _band(tmp_path, "[m.summaryText(s), m.statusText(s), m.anyRunning(s)]", s=done)
    assert got == ["SwarmCloud · nothing running", None, False]  # MUTATION: a line for finished work -- never cleared.
    unread = _state()
    assert _band(tmp_path, "[m.summaryText(s), m.statusText(s)]", s=unread) == ["SwarmCloud · not read yet", None]


def test_a_finished_workflow_is_exactly_one_turn_and_an_unchanged_read_none(tmp_path):
    before = _state(workflows=[WORKFLOW, _WAITING], listReadAt=READ_AT)
    after = _state(workflows=[{**WORKFLOW, "state": "SUCCEEDED", "pull_requests": [_PULL]}, _WAITING],
                   listReadAt=READ_AT)
    got = _band(tmp_path, """(() => {
  const first = m.changes(null, before)
  const same = m.changes(first.seen, before)
  const done = m.changes(same.seen, after)
  const again = m.changes(done.seen, after)
  return [first.turns, same.turns, done.turns, again.turns]
})()""", before=before, after=after)
    assert got[0] == []  # MUTATION: notice on the first read -- the waiting step is a turn at start-up.
    assert got[1] == []  # MUTATION: notice every fact every read -- a turn a minute.
    assert got[2] == ["status wf_counts · finished: succeeded · PR #2463"]  # MUTATION: no notice -- nobody learns of it.
    assert got[3] == []


def test_waits_merges_and_owner_questions_are_turns_and_nothing_else_is(tmp_path):
    steps = WORKFLOW["steps"]
    moved = {**WORKFLOW, "steps": [{**s, "state": "SUCCEEDED"} if s["step_id"] == "counts-h" else s for s in steps]}
    parked = {**WORKFLOW, "steps": [{**s, "state": "PARKED", "park_reason": "HUMAN_REQUIRED", "park_phase": "owner_questions"}
                                    if s["step_id"] == "integrate" else s for s in steps]}
    asked = {**parked, "owner_questions": 2}
    fewer = {**parked, "owner_questions": 1}
    got = _band(tmp_path, """(() => {
  const read = (w) => ({ ...base, workflows: [w] })
  let seen = m.changes(null, read(rows[0])).seen
  const out = []
  for (const w of rows.slice(1)) { const r = m.changes(seen, read(w)); seen = r.seen; out.push(r.turns) }
  return out
})()""", base=_state(listReadAt=READ_AT), rows=[WORKFLOW, moved, parked, asked, fewer, _WAITING])
    assert got[0] == []  # a step moved: not one of the four
    assert got[1] == ["status wf_counts · merge parked on owner questions (integrate)"]
    assert got[2] == ["status wf_counts · new owner questions: 2"]
    assert got[3] == []  # MUTATION: any change of count -- an answered question is a turn.
    assert got[4] == ["status wf_ask · ask waits for a person"]


@pytest.mark.parametrize("cols", [60, 100, 180])
def test_the_band_fits_its_columns_at_every_level_and_folds_to_one_line(tmp_path, cols):
    narrow = cols < 70
    levels = [
        _state(workflows=[WORKFLOW, _WAITING], listReadAt=READ_AT, project=True),
        _state(level="stages", workflows=[WORKFLOW], workflowId="wf_counts", workflow=WORKFLOW, workflowReadAt=READ_AT),
        _state(level="agents", workflows=[WORKFLOW], workflowId="wf_counts", workflow=WORKFLOW, workflowReadAt=READ_AT,
               stage=2, agents=AGENTS, agentsReadAt=READ_AT),
    ]
    got = _band(tmp_path, """levels.map((s) => [
  m.bandFrame(s, m.initialBand(), { cols, rows: 40, narrow }).map((l) => l.map((g) => g.text).join('')),
  m.bandFrame(s, { ...m.initialBand(), folded: true }, { cols, rows: 40, narrow }).map((l) => l.map((g) => g.text).join('')),
  m.bandFrame(s, m.initialBand(), { cols, rows: 1, narrow }).length,
])""", levels=levels, cols=cols, narrow=narrow)
    for open_lines, folded, one_row in got:
        assert all(len(line) <= cols for line in open_lines), open_lines  # MUTATION: draw at the pane's width -- overflow.
        assert len(open_lines[0]) <= cols - 4  # the engine's [-] keeps the top right
        assert "SwarmCloud ·" in open_lines[0] and open_lines[0].rstrip().endswith("[Fold]")
        assert len(open_lines) > 1
        assert len(folded) == 1 and folded[0].rstrip().endswith("[Open]")  # MUTATION: fold draws the tree -- many lines.
        assert one_row == 1
    # Two columns where there is room; one at a time on a narrow terminal.
    assert any("│" in line for line in got[2][0]) is (not narrow)


def test_a_narrow_band_clips_the_summary_never_the_control(tmp_path):
    crowded = _state(workflows=[WORKFLOW] * 3 + [_WAITING] * 12, listReadAt=READ_AT)
    head = _band(tmp_path, "m.bandFrame(s, m.initialBand(), { cols: 30, rows: 1, narrow: true })[0]", s=crowded)
    assert head[-1]["text"] == "[Fold]" and head[-1]["press"] == "band:fold"  # MUTATION: clip the line -- no Fold.
    assert sum(len(seg["text"]) for seg in head) == 26


def test_the_settings_frame_offers_the_band_and_saving_it_is_one_call(tmp_path):
    got = _js(tmp_path, f"""
const s = {{ ...m.initialState(), level: 'settings', layout: 'band' }}
const lines = m.frame(s, {{ cols: 66, narrow: false, now: '' }}).map((l) => l.map((g) => g.text).join(''))
const calls = []
const after = await m.act(s, 'layout:band', async (tool, args) => {{ calls.push({{ tool, args }}); return {{ ok: true, data: {{ layout: 'band' }} }} }}, () => 'T')
process.stdout.write(JSON.stringify([lines, calls, after.layout]))""")
    lines, calls, chosen = got
    assert any("● Band above the prompt" in line for line in lines)
    assert calls == [{"tool": "swarm_layout", "args": {"layout": "band"}}]  # MUTATION: layout:band reads -- {}.
    assert chosen == "band"


# --------------------------------------------------------------------------
# Session start
# --------------------------------------------------------------------------

_RECORD = 'printf "%s\\n" "$@" >> "$SWARM_ARGS_LOG"'
_CONTEXT = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": "Run `/sc attach --all --project` now."}})


def _start(tmp_path, saved: str | None, **env: str):
    config_dir = tmp_path / "hook-config"
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    if saved is not None:
        layout.save(saved, {"SWARM_CONFIG_DIR": str(config_dir), "CLAUDE_PROJECT_DIR": str(project)})
    return _hook(tmp_path, f"{_RECORD}\ncat <<'EOF'\n{_CONTEXT}\nEOF", SWARM_CONFIG_DIR=str(config_dir), **env)


def test_in_band_session_start_asks_for_nothing_and_runs_no_listing(tmp_path):
    done = _start(tmp_path, "band")
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""  # MUTATION: ask for the attach as in rows -- the context names /sc attach.
    assert not (tmp_path / "uv-args").exists()  # MUTATION: run the listing anyway -- a bridge call at every start.


def test_the_layout_option_band_is_read_by_the_hook_too(tmp_path):
    done = _start(tmp_path, None, CLAUDE_PLUGIN_OPTION_LAYOUT="band")
    assert done.stdout == "" and not (tmp_path / "uv-args").exists()


def test_a_saved_rows_outranks_the_option_and_the_hook_still_asks(tmp_path):
    done = _start(tmp_path, "rows", CLAUDE_PLUGIN_OPTION_LAYOUT="band")
    assert "/sc attach --all --project" in done.stdout  # MUTATION: the option wins -- nothing asked.
    assert "workflows" in (tmp_path / "uv-args").read_text()


def test_in_band_a_remembered_watch_is_still_rearmed(tmp_path):
    config_dir = tmp_path / "hook-config"
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    from swarm_mcp import watch

    path = watch.state_path(watch.project_dir({"CLAUDE_PROJECT_DIR": str(project)}),
                            {"SWARM_CONFIG_DIR": str(config_dir)})
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"interval_minutes": 30}))
    done = _start(tmp_path, "band")
    context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "/sc watch 30m" in context and "attach" not in context
