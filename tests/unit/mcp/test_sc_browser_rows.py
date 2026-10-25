"""The pane layout's toggle, its one quiet row per workflow, and the pane's reads (SC-BROWSER-BUILD, #2318).

BGD picked proposal B on 2026-10-25 (PR 2328): beside the SwarmCloud pane,
each workflow keeps ONE /workflows row whose only job is to notice its finish,
and a toggle switches between today's layout and the new one. What this file
holds:

* THE TOGGLE. `/sc layout rows|pane` (the `swarm_layout` tool) saves the choice
  per project, in a file under the bridge's config directory, so a new session
  -- a new bridge process -- starts in the layout left; a saved choice outranks
  the plugin's `layout` option; every launch reads it, so it switches without
  restarting the session.
* ONE ROW PER WORKFLOW, NOT PER STEP. With the pane layout every copy says
  `rows: "pane"`, and run.js starts ONE sc:finish row for a fifteen-step
  workflow where `per-step` starts fifteen sc:step rows.
* FAR CHEAPER THAN A STEP ROW. `swarm_follow` `format: "stage"` holds through
  every step change that leaves the workflow's stage where it was and answers
  when the stage changes; the row writes a line only then. run.js's stage
  line and the bridge's are one rule (held equal here).
* THE PANE'S READS. `swarm_browse`'s four levels, against the real API.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_mcp import browse, compact, config, launch, layout, progress, server
from swarm_mcp.client import SwarmError

from test_follow_cursor import World, swarm, world  # noqa: F401 - fixtures
from test_plugin_agents_and_workflows import _PLUGIN, _run
from test_plugin_row_split import SESSION, _Client, _envelope

_RUN_JS = _PLUGIN / "workflows" / "run.js"
_FINISH = _PLUGIN / "agents" / "finish.md"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_COMMAND = _PLUGIN / "commands" / "sc.md"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"


@pytest.fixture(autouse=True)
def _bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "project").mkdir()
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "SWARM_PLUGIN_ROWS", "SWARM_PLUGIN_LAYOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(launch.os, "cpu_count", lambda: 10)
    launch.forget_attached()
    progress.HANDLES.clear()


def _layout_tool(**args) -> dict:
    return json.loads(server._call(None, "swarm_layout", args))


# --------------------------------------------------------------------------
# The toggle
# --------------------------------------------------------------------------


def test_the_layout_is_todays_rows_until_someone_chooses():
    got = _layout_tool()
    assert got["layout"] == "rows" and got["source"] == "the default"  # MUTATION: default to pane -- every project switches.


def test_sc_layout_pane_is_saved_for_the_project_and_read_back_by_a_new_bridge(tmp_path, monkeypatch):
    saved = _layout_tool(layout="pane")
    assert saved["layout"] == "pane" and "keep going" in saved["note"]
    files = list((tmp_path / "config" / "sc-layout").glob("*.json"))
    assert len(files) == 1  # MUTATION: keep it in memory -- the next session forgets it.
    # A new session is a new bridge process: nothing but the file carries it.
    assert json.loads(files[0].read_text())["layout"] == "pane"
    assert layout.current()["layout"] == "pane"
    # Another project is another file.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    assert layout.current()["layout"] == "rows"  # MUTATION: one file for every project -- pane here too.


def test_a_saved_layout_outranks_the_plugins_option_and_switches_back(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_LAYOUT", "pane")
    assert layout.current()["layout"] == "pane" and layout.current()["source"] == "the plugin's layout option"
    _layout_tool(layout="rows")
    assert layout.current()["layout"] == "rows"  # MUTATION: the option wins -- the toggle cannot switch back.
    with pytest.raises(SwarmError, match="not a layout"):
        _layout_tool(layout="tree")


def test_the_layout_picks_each_launchs_row_mode_without_a_restart():
    assert layout.row_mode(None) == "summary"
    _layout_tool(layout="pane")
    assert layout.row_mode(None) == "pane"  # MUTATION: read the layout once at start-up -- still summary.
    assert layout.row_mode("per-step") == "per-step"  # a launch's own `rows` still wins
    _layout_tool(layout="rows")
    assert layout.row_mode(None) == "summary"


def test_the_layout_option_reaches_the_bridge_and_the_version_moved_with_its_pin():
    manifest = json.loads(_MANIFEST.read_text())
    assert manifest["userConfig"]["layout"]["default"] == "rows"
    assert manifest["mcpServers"]["swarmcloud"]["env"]["SWARM_PLUGIN_LAYOUT"] == "${user_config.layout}"
    assert config.PLUGIN_LAYOUT in config.PLUGIN_KEYS
    version = tuple(int(p) for p in manifest["version"].split("."))
    assert version >= (0, 5, 68)  # MUTATION: no bump -- installed copies never see the pane.
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in manifest["mcpServers"]["swarmcloud"]["args"])
    assert manifest["types"] == "./types/index.d.ts"


def test_the_skill_and_the_command_describe_both_layouts_and_the_toggle():
    skill = " ".join(_SKILL.read_text().split())
    command = " ".join(_COMMAND.read_text().split())
    for text in (skill, command):
        assert "/sc layout rows" in text and "/sc layout pane" in text  # MUTATION: no toggle documented.
        assert "swarm_layout" in text
    assert "/sc-pane" in skill and "sc:finish" in skill and "no model tokens" in skill


# --------------------------------------------------------------------------
# One row per workflow
# --------------------------------------------------------------------------


def test_an_attach_in_the_pane_layout_is_one_copy_whose_rows_say_pane():
    _layout_tool(layout="pane")
    out = json.loads(server._call(_Client(_envelope(18)), "swarm_workflow_launch", {"attach": "wf_big"}))
    assert [l["args"]["rows"] for l in out["launches"]] == ["pane"]  # MUTATION: ignore the layout -- "summary".
    assert "notices its finish" in Path(out["launches"][0]["script_path"]).read_text().split("\n", 3)[2]


def test_rows_pane_is_taken_for_one_attach_and_a_task_batch_keeps_its_summary_row():
    out = json.loads(server._call(_Client(_envelope(3)), "swarm_workflow_launch", {"attach": "wf_big", "rows": "pane"}))
    assert out["launches"][0]["args"]["rows"] == "pane"
    _layout_tool(layout="pane")
    batch = launch.for_tasks([{"task_id": "task_1", "label": "one"}], None, dispatched=False)
    assert batch["args"]["rows"] == "summary"  # MUTATION: hand a task batch the stage row -- it has no stages.


_STAGE_STEPS = (
    [{"step_id": "plan", "task_id": "task_plan", "depends_on": [], "state": "SUCCEEDED", "console": None}]
    + [{"step_id": f"s{i:02d}", "task_id": f"task_s{i:02d}", "depends_on": ["plan"],
        "state": "RUNNING" if i % 2 else "SUCCEEDED", "console": None} for i in range(13)]
    + [{"step_id": "join", "task_id": "task_join", "depends_on": [f"s{i:02d}" for i in range(13)], "state": "QUEUED",
        "console": None}]
)


def _attach(tmp_path, rows: str, answers: dict) -> dict:
    attached = {"workflow_id": "wf_c", "state": "RUNNING", "state_note": None, "title": "COUNTS-REVIEW", "label": None,
                "console": None, "bridge_version": "0.1.0", "follow_error": None, "error": None, "steps": _STAGE_STEPS}
    base = {"ATTACH": attached, "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []}}
    return _run(tmp_path, {"attach": "wf_c", "title": "COUNTS-REVIEW", "rows": rows}, {**base, **answers})


def test_the_pane_layout_starts_one_row_per_workflow_not_per_step(tmp_path):
    finished = {"state": "FINISHED", "last_error": None, "tasks": []}
    got = _attach(tmp_path, "pane", {"FINISH": finished})
    rows = [c for c in got["calls"] if c["agentType"] in ("sc:finish", "sc:step", "sc:summary")]
    assert [c["agentType"] for c in rows] == ["sc:finish"]  # MUTATION: per-step rows -- seven sc:step.
    assert rows[0]["label"] == "SC · COUNTS-REVIEW"
    assert rows[0]["phase"] == "Finish"
    prompt = rows[0]["prompt"].split("\n")
    stage_of = json.loads(next(l for l in prompt if l.startswith("stage_of: "))[len("stage_of: "):])
    assert stage_of["stages"]["task_plan"] == 1 and stage_of["stages"]["task_s05"] == 2
    assert stage_of["stages"]["task_join"] == 3  # MUTATION: the spec's order, not the DAG -- join is 1.
    assert stage_of["key"] == "stage 2"
    # The unfinished steps only, each followed: six running in stage 2 and the join.
    assert len([l for l in prompt if l.startswith("task: ")]) == 7
    # Today's per-step layout, for contrast: a row for every unfinished step.
    step_answers = {f"step:{s['step_id']}": {"state": "SUCCEEDED", "last_error": None}
                    for s in _STAGE_STEPS}
    per_step = _attach(tmp_path, "per-step", step_answers)
    assert len([c for c in per_step["calls"] if c["agentType"] == "sc:step"]) == 7


def test_the_row_writes_a_line_only_when_the_stage_changes(tmp_path):
    # The row reports stage 2 finished, the join still waiting: one new line.
    tasks = [{"task_id": f"task_s{i:02d}", "state": "SUCCEEDED"} for i in range(13)]
    got = _attach(tmp_path, "pane", {"FINISH": {"state": "FINISHED", "last_error": None, "tasks": tasks}})
    stage_lines = [l for l in got["logs"] if l.startswith("stage ") or l.startswith("succeeded")]
    assert stage_lines == ["stage 2 · 6 running", "stage 3 · 1 waiting"]  # MUTATION: a line per poll -- repeats.
    assert got["result"]["rows"] == "pane"


def _js_list(name: str) -> list[str]:
    match = re.search(r"const " + name + r" = \[([^\]]*)\]", _RUN_JS.read_text())
    assert match, f"run.js has no {name}"
    return re.findall(r"'([a-z]+)'", match.group(1))


_CASES = [
    ({"a": 1, "b": 1, "c": 2}, {"a": "SUCCEEDED", "b": "RUNNING", "c": "QUEUED"}),
    ({"a": 1, "b": 2, "c": 2, "d": 2}, {"a": "SUCCEEDED", "b": "FAILED", "c": "PARKED", "d": "LEASED"}),
    ({"a": 1, "b": 2}, {"a": "SUCCEEDED", "b": "SUCCEEDED"}),
    ({"a": 1, "b": 2}, {"a": "SUCCEEDED", "b": "CANCELLED"}),
    ({"a": 1, "b": 1}, {"a": None, "b": "READY"}),
]


def test_run_js_and_the_bridge_build_one_stage_line(tmp_path):
    assert _js_list("STAGE_WORDS") == list(compact.STAGE_WORDS)  # MUTATION: reorder the bridge's words.
    source = _RUN_JS.read_text()
    start = source.index("function stageWord(")
    end = source.index("function stageItems(")
    program = (
        "const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED']\n"
        "const SUMMARY_RUNNING = ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING']\n"
        "const SUMMARY_DONE = ['SUCCEEDED']\nconst SUMMARY_FAILED = ['FAILED', 'CANCELLED', 'DEAD_LETTERED']\n"
        f"const STAGE_WORDS = {json.dumps(list(compact.STAGE_WORDS))}\n"
        + source[start:end]
        + f"\nprocess.stdout.write(JSON.stringify({json.dumps(_CASES)}.map(([s, t]) => stageLine(s, t))))"
    )
    script = tmp_path / "stage.cjs"
    script.write_text(program)
    import subprocess

    from test_plugin_agents_and_workflows import _node

    done = subprocess.run([_node(), str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    js = json.loads(done.stdout)
    py = [dict(zip(("key", "line"), compact.stage_line(s, t))) for s, t in _CASES]
    assert js == py  # MUTATION: count PARKED as waiting in one of the two.
    assert py[1] == {"key": "stage 2", "line": "stage 2 · 1 failed · 1 running · 1 parked"}
    assert py[2] == {"key": "done", "line": "succeeded · 2/2 stages"}
    assert py[3] == {"key": "done", "line": "finished · 1 failed · 2/2 stages"}


def test_the_finish_row_follows_with_format_stage_and_writes_only_on_a_stage_change():
    text = " ".join(_FINISH.read_text().split())
    assert '`format: "stage"`' in text and "`stage_of`" in text  # MUTATION: keep the row on summary.
    assert "stage_changed" in text and "verbatim" in text
    assert "`wait_seconds: 1800`" in text
    assert "NEVER `step_id`, `parents` or `summary_of`" in text


# --------------------------------------------------------------------------
# The stage follow: held through step changes, answered on a stage change
# --------------------------------------------------------------------------


def _stage_of() -> dict:
    return {"stages": {"task_a": 1, "task_b": 1, "task_c": 1, "task_d": 2}, "states": {}, "key": "stage 1"}


def test_the_stage_follow_holds_through_step_changes_and_answers_on_the_stage_change(swarm, world):
    for task_id in ("task_a", "task_b", "task_c"):
        world.task(task_id, state="RUNNING")
    world.task("task_d", state="QUEUED")
    ids = ["task_a", "task_b", "task_c", "task_d"]
    first = compact.watch_stage(swarm, ids, _stage_of())
    assert first["key"] == "stage 1" and first["stage_changed"] is False  # the prompt's key: not news

    now = [0.0]
    moves = iter([("task_a", "SUCCEEDED"), ("task_b", "SUCCEEDED"), ("task_c", "SUCCEEDED")])
    slept = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds
        task_id, state = next(moves)
        world.set_state(task_id, state)

    reply = compact.watch_stage(swarm, ids, _stage_of(), since=first["since"], wait_seconds=1800,
                                sleep=sleep, clock=lambda: now[0])
    # Two steps finished inside stage 1 without an answer; the third ended it.
    assert len(slept) == 3  # MUTATION: answer on any state change, as `summary` does -- one sleep.
    assert reply["key"] == "stage 2" and reply["stage_changed"] is True
    assert reply["line"] == "stage 2 · 1 waiting"
    again = compact.watch_stage(swarm, ids, _stage_of(), since=reply["since"], wait_seconds=0)
    assert again["stage_changed"] is False  # MUTATION: compare with the prompt's key every time -- True.


def test_swarm_follow_takes_format_stage_and_needs_stage_of(swarm, world):
    world.task("task_a", state="RUNNING")
    reply = json.loads(server._call(swarm, "swarm_follow", {
        "task_ids": ["task_a"], "format": "stage", "stage_of": {"stages": {"task_a": 1}, "key": ""}}))
    assert reply["key"] == "stage 1" and reply["line"] == "stage 1 · 1 running"
    assert re.fullmatch(r"r[0-9a-f]{5}", reply["since"])  # the short handle every row format gets
    with pytest.raises(SwarmError, match="stage_of"):  # MUTATION: follow without the map -- no stage to compare.
        server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "format": "stage"})


# --------------------------------------------------------------------------
# The pane's reads: swarm_browse
# --------------------------------------------------------------------------


def test_swarm_browse_and_swarm_layout_are_advertised_read_only():
    names = {t["name"]: t for t in server.TOOLS}
    assert names["swarm_browse"]["inputSchema"]["properties"]["level"]["enum"] == list(browse.LEVELS)
    assert "read-only" in names["swarm_browse"]["description"]
    assert "Reads and writes nothing in SwarmCloud" in names["swarm_layout"]["description"]


def test_swarm_browse_refuses_an_unknown_level_and_missing_ids():
    for args in ({"level": "tree"}, {"level": "stages"}, {"level": "agents"}, {"level": "agent"}):
        with pytest.raises(SwarmError, match="Nothing was read"):
            browse.read(None, args)


class _Session:
    """A client answering what Level 1 and Level 2 read, and nothing else."""

    tenant = None

    def __init__(self, sessions: dict[str, str]) -> None:
        self.sessions = sessions
        self.asked: list[dict] = []

    def workflows(self, **kwargs):
        self.asked.append(kwargs)
        return {"workflows": [{"workflow_id": w} for w in self.sessions], "filter": {"claude_session": SESSION}}

    def request(self, method, path, **_):
        workflow_id = path.rsplit("/", 1)[-1]
        owner = self.sessions[workflow_id]
        tasks = [
            {"id": "t1", "step_id": "a", "state": "SUCCEEDED", "completed_at": "2026-10-25T10:30:00Z",
             "metadata": {"title": workflow_id.upper(), "claude_session": owner},
             "result_summary": {"runner": {"usage": {"input_tokens": 1000, "output_tokens": 200,
                                                     "cache_read_input_tokens": 50_000}}},
             "cost_usd_total": 1.25},
            {"id": "t2", "step_id": "b", "state": "RUNNING", "metadata": {"claude_session": owner},
             "result_summary": {"runner": {"usage": {"input_tokens": 999_999}}}},
        ]
        return {"workflow": {"workflow_id": workflow_id, "tenant_id": "eng", "state": "RUNNING",
                             "state_source": "derived", "created_at": "2026-10-25T10:00:00Z",
                             "claude_session": owner,
                             "steps": [{"step_id": "a", "task_id": "t1", "depends_on": []},
                                       {"step_id": "b", "task_id": "t2", "depends_on": ["a"]}]},
                "tasks": tasks}


def test_level_1_lists_only_this_sessions_workflows_with_finished_steps_tokens(monkeypatch):
    from datetime import datetime, timezone

    from swarm_mcp import sc

    monkeypatch.setattr(sc, "running_tasks", lambda client, **_: {"count": 0, "tasks": []})
    client = _Session({"wf_mine": SESSION, "wf_theirs": "99999999-2222-4333-8444-555555555555"})
    got = browse.workflows_level(client, SESSION, now=datetime(2026, 10, 25, 10, 42, 7, tzinfo=timezone.utc))
    assert client.asked == [{"claude_session": SESSION, "limit": browse.SESSION_PAGE}]  # finished ones too: no state filter
    assert [w["workflow_id"] for w in got["workflows"]] == ["wf_mine"]  # MUTATION: trust the filter -- theirs too.
    row = got["workflows"][0]
    assert row["name"] == "WF_MINE"
    assert row["tokens"] == 51_200  # MUTATION: count the running step's earlier attempt -- 1,051,199.
    assert row["tokens_note"] == "finished steps only"
    assert row["cost_usd"] == 1.25
    assert row["duration_s"] == 42 * 60 + 7
    assert row["steps"] == [{"step_id": "a", "task_id": "t1", "depends_on": [], "state": "SUCCEEDED"},
                            {"step_id": "b", "task_id": "t2", "depends_on": ["a"], "state": "RUNNING"}]
    assert got["read_at"] == "2026-10-25T10:42:07Z"


def test_level_1_without_a_session_id_says_why_it_lists_nothing():
    got = browse.workflows_level(None, None)
    assert got["workflows"] == [] and "not known" in got["scope_because"]  # MUTATION: list the tenant's -- someone else's work.


def test_levels_3_and_4_read_the_stage_and_the_agent(swarm, world):
    world.task("task_a", state="RUNNING")
    world.task("task_b", state="PARKED")
    world.db.docs["tasks/task_b"]["park_reason"] = "PROVIDER_QUOTA_EXHAUSTED"
    got = json.loads(server._call(swarm, "swarm_browse", {"level": "agents", "task_ids": ["task_a", "task_b"]}))
    assert [a["task_id"] for a in got["agents"]] == ["task_a", "task_b"]
    assert got["agents"][1]["note"] == "quota, holds nothing"  # MUTATION: a parked step reads as running.
    one = json.loads(server._call(swarm, "swarm_browse", {"level": "agent", "task_id": "task_a"}))
    assert one["state"] == "RUNNING" and one["task_id"] == "task_a"
    assert one["tokens"] is None and one["tokens_because"]  # MUTATION: unknown as 0.
    assert one["cost_usd"] is None
    for key in ("attempts", "duration_s", "last_error", "pr_url", "artifacts"):
        assert key in one
