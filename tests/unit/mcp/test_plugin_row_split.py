"""A SwarmCloud batch is ONE summary row by default, or per-step rows split over runs (PLUGIN-ROW-SPLIT, #1809).

Claude Code runs at most min(16, CPU cores - 2) agents per workflow run, so a
`/sc attach` of an 18-step workflow on a 10-core machine showed 8 rows and
queued the other 10. Owner decision, 2026-10-21: a plugin setting `rows`.

* `summary` (the default): ONE follower row per SwarmCloud workflow (or per
  batch of single tasks), whose line sums up the step counts and changes only
  when they change; a step that fails or waits for a person is named on a line
  of its own.
* `per-step`: one row per unfinished step, split over several titled runs
  (`SC · <name> · N steps · rows 1-8`, `rows 9-16`, ...) so each run stays
  within the host's limit; the first copy alone reports the finished steps,
  and `max_live_rows` still caps the total.

The bridge half is called directly; run.js is run under node with the
workflow globals stubbed, by the harness in `test_plugin_agents_and_workflows`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_mcp import config, launch
from test_plugin_agents_and_workflows import _PLUGIN, _SPEC, _SUBMITTED, _run, _step

SESSION = "11111111-2222-4333-8444-555555555555"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_RUN_JS = _PLUGIN / "workflows" / "run.js"
_SUMMARY_AGENT = _PLUGIN / "agents" / "summary.md"


@pytest.fixture(autouse=True)
def _bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS", "SWARM_PLUGIN_ROWS"):
        monkeypatch.delenv(name, raising=False)
    # A 10-core host: Claude Code runs 8 agents per workflow run there.
    monkeypatch.setattr(launch.os, "cpu_count", lambda: 10)
    launch.forget_attached()


def _envelope(open_steps: int, done_steps: int = 0) -> dict:
    """A workflow of `done_steps` finished then `open_steps` unfinished steps,
    none depending on another, so the console's order is the list's."""
    steps, tasks = [], []
    for index in range(done_steps + open_steps):
        step_id = f"s{index + 1:02d}"
        state = "SUCCEEDED" if index < done_steps else ("RUNNING" if index % 2 else "QUEUED")
        steps.append({"step_id": step_id, "task_id": f"task_{step_id}", "depends_on": []})
        tasks.append({"id": f"task_{step_id}", "step_id": step_id, "state": state,
                      "metadata": {"title": "big", "claude_session": SESSION}})
    return {"workflow": {"workflow_id": "wf_big", "tenant_id": "eng", "steps": steps,
                         "claude_session": SESSION},
            "tasks": tasks}


class _Client:
    def __init__(self, envelope: dict) -> None:
        self.envelope = envelope
        self.tenant = None
        self.read = []

    def request(self, method, path, **_):
        if path.startswith("/v1/workflows/"):
            self.read.append(path.rsplit("/", 1)[-1])
            return self.envelope
        if path == "/v1/accounts":
            return {"accounts": []}
        raise RuntimeError(f"no {path} here")

    def tenants_mine(self):
        return []


def _shares(out: dict) -> list[list[str]]:
    return [launch_["args"]["row_share"]["steps"] for launch_ in out["launches"]]


# --------------------------------------------------------------------------
# The setting
# --------------------------------------------------------------------------


def test_the_rows_setting_is_summary_unless_set_and_reaches_the_bridge():
    assert config.rows_mode({}) == "summary"  # MUTATION: default to per-step -- "per-step".
    assert config.rows_mode({"SWARM_PLUGIN_ROWS": "per-step"}) == "per-step"  # MUTATION: never read the env -- "summary".
    # An unknown value, or one Claude Code left unexpanded, is the default.
    assert config.rows_mode({"SWARM_PLUGIN_ROWS": "rows"}) == "summary"  # MUTATION: pass any value through -- "rows".
    assert config.rows_mode({"SWARM_PLUGIN_ROWS": "${user_config.rows}"}) == "summary"  # MUTATION: no is_unset -- the reference.
    manifest = json.loads(_MANIFEST.read_text())
    option = manifest["userConfig"]["rows"]
    assert option["default"] == "summary"  # MUTATION: default per-step in the manifest -- "per-step".
    assert "summary" in option["description"] and "per-step" in option["description"]  # MUTATION: name one mode -- absent.
    env = manifest["mcpServers"]["swarmcloud"]["env"]
    assert env[config.plugin_env(config.PLUGIN_ROWS)] == "${user_config.rows}"  # MUTATION: drop the env entry -- KeyError.


def test_the_per_run_limit_is_cores_less_two_at_most_16_and_never_below_1(monkeypatch):
    for cores, limit in ((1, 1), (2, 1), (3, 1), (4, 2), (10, 8), (18, 16), (64, 16)):
        assert launch.rows_per_run(cores) == limit, cores  # MUTATION: drop max(1, ...) -- 0 or -1 at 1-2 cores.
    monkeypatch.setattr(launch.os, "cpu_count", lambda: None)
    assert launch.rows_per_run() == 1  # MUTATION: treat an unknown core count as 0 -- -2.
    monkeypatch.setattr(launch.os, "cpu_count", lambda: 10)
    assert launch.rows_per_run() == 8  # MUTATION: read a fixed number, not the host -- not 8.


# --------------------------------------------------------------------------
# summary: one copy, one row
# --------------------------------------------------------------------------


def test_the_default_attach_of_an_18_step_workflow_writes_one_copy_that_runs_the_summary_row():
    out = launch.for_attach(_Client(_envelope(18)), "wf_big")

    assert len(out["launches"]) == 1  # MUTATION: split the summary too -- 3.
    (copy,) = out["launches"]
    assert copy["args"]["rows"] == "summary"  # MUTATION: leave the mode out of the args -- KeyError.
    assert copy["args"]["attach"] == "wf_big"
    assert "row_share" not in copy["args"]  # MUTATION: hand the summary a share -- present.
    assert copy["title"] == "SC · big · 18 steps"  # MUTATION: add a rows suffix to a summary -- " · rows 1-18".
    assert Path(copy["script_path"]).is_file()
    # The reply's own fields are that one copy's, so a caller reading only
    # `script_path` still launches it.
    assert out["script_path"] == copy["script_path"] and out["args"] == copy["args"]


def test_the_single_task_batch_is_one_summary_row_by_default():
    entries = [{"task_id": f"task_{i}", "label": f"t{i}"} for i in range(12)]
    out = launch.for_tasks(entries, dispatched=False)
    assert out["args"]["rows"] == "summary"  # MUTATION: no mode on a task batch -- KeyError.
    assert len(out["args"]["attach_tasks"]) == 12


def test_attach_all_writes_one_summary_copy_per_workflow(monkeypatch):
    from swarm_mcp import sc

    listed = [{"workflow_id": wid, "workspace": "eng", "title": wid, "label": None, "steps_total": 18,
               "console": None, "claude_session": SESSION, "state": "RUNNING",
               "progress": {"done": 0, "total": 18, "age_seconds": 10}} for wid in ("wf_a", "wf_b")]
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": 2, "complete": True, "workflows": listed})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    out = launch.for_all(_Client(_envelope(18)))
    assert [l["args"]["rows"] for l in out["launches"]] == ["summary", "summary"]  # MUTATION: per-step by default -- split copies.


# --------------------------------------------------------------------------
# per-step: split over runs within the host's limit
# --------------------------------------------------------------------------


def test_per_step_18_rows_at_a_limit_of_8_writes_3_copies_titled_with_their_shares(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "per-step")
    out = launch.for_attach(_Client(_envelope(18, done_steps=2)), "wf_big")

    titles = [l["title"] for l in out["launches"]]
    assert titles == [  # MUTATION: one copy for all 18 -- a single title with no share.
        "SC · big · 20 steps · rows 1-8",
        "SC · big · 20 steps · rows 9-16",
        "SC · big · 20 steps · rows 17-18",
    ]
    shares = _shares(out)
    assert [len(s) for s in shares] == [8, 8, 2]  # MUTATION: split at 16, not the host's 8 -- [16, 2].
    assert sum(shares, []) == [f"s{i:02d}" for i in range(3, 21)]  # MUTATION: share the finished steps too -- s01, s02 in.
    for copy, share in zip(out["launches"], shares):
        assert copy["args"]["rows"] == "per-step"
        # Each copy knows the others' steps, so the first can report the
        # finished steps that are in no share, and only the first does.
        others = copy["args"]["row_share"]["others"]
        assert set(others) == set(sum(shares, [])) - set(share)  # MUTATION: no `others` -- the first reports shared steps.
        assert Path(copy["script_path"]).is_file()
    assert [l["args"]["row_share"]["from"] for l in out["launches"]] == [1, 9, 17]  # MUTATION: number every share from 1.
    assert out["not_followed"] == []


def test_attach_rows_per_step_overrides_the_summary_setting_for_one_attach():
    client = _Client(_envelope(18))
    out = launch.for_attach(client, "wf_big", rows="per-step")

    assert client.read == ["wf_big"]  # MUTATION: read something other than the id -- another read.
    assert [l["args"]["rows"] for l in out["launches"]] == ["per-step"] * 3  # MUTATION: ignore `rows` -- one summary copy.
    assert out["workflow_id"] == "wf_big"


def test_max_live_rows_still_caps_the_per_step_rows(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "per-step")
    monkeypatch.setenv("SWARM_PLUGIN_MAX_LIVE_ROWS", "10")
    out = launch.for_attach(_Client(_envelope(18)), "wf_big")

    assert [len(s) for s in _shares(out)] == [8, 2]  # MUTATION: split without the cap -- [8, 8, 2].
    left = [entry["step_id"] for entry in out["not_followed"]]
    assert left == [f"s{i:02d}" for i in range(11, 19)]  # MUTATION: drop the rest silently -- [].


def test_per_step_within_the_limit_is_one_copy_as_before(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "per-step")
    out = launch.for_attach(_Client(_envelope(5)), "wf_big")
    assert [l["title"] for l in out["launches"]] == ["SC · big · 5 steps"]  # MUTATION: always add a share -- " · rows 1-5".
    assert "row_share" not in out["args"]


# --------------------------------------------------------------------------
# run.js: the summary row
# --------------------------------------------------------------------------


_LINK = "https://console.example.test/workflows/wf_s"


def _attached(steps) -> dict:
    return {"workflow_id": "wf_s", "state": "RUNNING", "state_note": None, "title": "scan", "label": None,
            "console": _LINK, "bridge_version": "0.1.0", "follow_error": None, "error": None, "steps": steps}


_STEPS = [
    {"step_id": "a", "task_id": "task_a", "depends_on": [], "state": "SUCCEEDED", "console": None},
    {"step_id": "b", "task_id": "task_b", "depends_on": [], "state": "RUNNING", "console": None},
    {"step_id": "c", "task_id": "task_c", "depends_on": [], "state": "QUEUED", "console": None},
    {"step_id": "d", "task_id": "task_d", "depends_on": ["b"], "state": "QUEUED", "console": None},
]


def _followed(state: str, tasks: list[dict], last_error=None) -> dict:
    return {"state": state, "last_error": last_error, "tasks": tasks}


def _task(task_id, state, **extra) -> dict:
    return {"task_id": task_id, "state": state, "park_reason": None, "pr_url": None, "cost_usd": None,
            "questions": 0, "last_error": None, **extra}


def _summary_lines(logs: list[str]) -> list[str]:
    return [line for line in logs if line.startswith("4 steps: ")]


def _attach_summary(tmp_path, follower: dict, status_state="SUCCEEDED") -> dict:
    answers = {"ATTACH": _attached(_STEPS), "SUMMARY": follower,
               "STATUS": {"state": status_state, "state_note": None, "console": _LINK, "steps": []}}
    got = _run(tmp_path, {"attach": "wf_s", "title": "scan", "rows": "summary"}, answers)
    assert "error" not in got, got.get("error")
    return got


def test_a_summary_attach_starts_one_follower_row_and_no_step_rows(tmp_path):
    follower = _followed("FINISHED", [
        _task("task_b", "SUCCEEDED", pr_url="https://github.com/acme/w/pull/12", cost_usd=0.4),
        _task("task_c", "SUCCEEDED", cost_usd=0.1),
        _task("task_d", "SUCCEEDED"),
    ])
    got = _attach_summary(tmp_path, follower)

    kinds = [c["agentType"] for c in got["calls"]]
    assert kinds.count("sc:summary") == 1  # MUTATION: start a follower per step -- 3.
    assert "sc:step" not in kinds  # MUTATION: start step rows as well -- present.
    (row,) = [c for c in got["calls"] if c["agentType"] == "sc:summary"]
    assert row["label"] == "[SwarmCloud] scan · 4 steps · " + _LINK  # MUTATION: label it as setup -- "· setup ·".
    followed = [line for line in row["prompt"].split("\n") if line.startswith("task: ")]
    assert followed == ["task: task_b b", "task: task_c c", "task: task_d d"]  # MUTATION: hand it the finished step -- task_a in.
    # The opening line, from the attach read, with the workflow's link.
    assert _summary_lines(got["logs"])[0] == "4 steps: 1 running · 2 queued · 1 done · 0 failed · console: " + _LINK
    result = got["result"]
    assert result["state"] == "SUCCEEDED" and result["rows"] == "summary"  # MUTATION: return the follower's word -- FINISHED.
    by_step = {s["step_id"]: s for s in result["steps"]}
    assert by_step["b"]["pr_url"] == "https://github.com/acme/w/pull/12" and by_step["b"]["cost_usd"] == 0.4
    assert by_step["a"]["state"] == "SUCCEEDED"  # MUTATION: drop the steps finished before attach -- KeyError.
    # The closing line names the pull request.
    assert _summary_lines(got["logs"])[-1].startswith("4 steps: 0 running · 0 queued · 4 done · 0 failed · PR #12")


def test_the_summary_line_is_written_again_only_when_the_counts_change(tmp_path):
    # The follower reached its turn cap with b and c swapped: the same counts.
    same = _followed("running", [_task("task_b", "QUEUED"), _task("task_c", "RUNNING"), _task("task_d", "QUEUED")])
    got = _attach_summary(tmp_path, same, status_state="RUNNING")
    assert len(_summary_lines(got["logs"])) == 1  # MUTATION: write the line on every answer -- 2 or more.

    moved = _followed("running", [_task("task_b", "SUCCEEDED"), _task("task_c", "RUNNING"), _task("task_d", "QUEUED")])
    got = _attach_summary(tmp_path, moved, status_state="RUNNING")
    lines = _summary_lines(got["logs"])
    assert lines == [  # MUTATION: never write it again -- one line.
        "4 steps: 1 running · 2 queued · 1 done · 0 failed · console: " + _LINK,
        "4 steps: 1 running · 1 queued · 2 done · 0 failed · console: " + _LINK,
    ]
    # Still running when the follower stopped: the result says how to resume.
    assert "/sc attach wf_s" in (got["result"].get("resume") or "")  # MUTATION: no resume -- None.


def test_a_failed_or_person_waiting_step_is_named_on_its_own_line(tmp_path):
    follower = _followed("running", [
        _task("task_b", "FAILED", last_error="claude-code exited 1: boom"),
        _task("task_c", "PARKED", park_reason="AWAITING_INPUT"),
        _task("task_d", "SUCCEEDED", questions=2),
    ])
    got = _attach_summary(tmp_path, follower, status_state="RUNNING")
    logs = got["logs"]

    assert "b FAILED · claude-code exited 1: boom" in logs  # MUTATION: count it and name nothing -- absent.
    assert any(line.startswith("c waits for a person") for line in logs), logs  # MUTATION: only failures -- absent.
    assert "d · 2 question(s) for the owner" in logs  # MUTATION: drop the questions -- absent.
    # Named once each, however many times the follower is started again.
    assert sum(1 for line in logs if line.startswith("b FAILED")) == 1  # MUTATION: name it on every answer -- 2.


def test_a_summary_run_of_a_spec_follows_its_steps_with_one_row(tmp_path):
    answers = {"SUBMIT": _SUBMITTED,
               "SUMMARY": _followed("FINISHED", [_task(f"task_{i}", "SUCCEEDED") for i in range(1, 5)]),
               "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []}}
    got = _run(tmp_path, {"spec": _SPEC, "rows": "summary"}, answers)

    assert "error" not in got, got.get("error")
    kinds = [c["agentType"] for c in got["calls"]]
    assert kinds.count("sc:summary") == 1 and "sc:step" not in kinds  # MUTATION: summary only on attach -- 4 step rows.
    assert got["result"]["state"] == "SUCCEEDED"


def test_a_summary_batch_of_single_tasks_is_one_row(tmp_path):
    tasks = [{"task_id": f"task_{i}", "title": f"t{i}"} for i in range(5)]
    answers = {"SUMMARY": _followed("FINISHED", [_task(f"task_{i}", "SUCCEEDED") for i in range(5)])}
    got = _run(tmp_path, {"attach_tasks": tasks, "rows": "summary"}, answers)

    assert "error" not in got, got.get("error")
    kinds = [c["agentType"] for c in got["calls"]]
    assert kinds == ["sc:summary"]  # MUTATION: a row per task -- five sc:task rows.
    assert [line for line in got["logs"] if line.startswith("5 tasks: ")][-1].startswith(
        "5 tasks: 0 running · 0 queued · 5 done · 0 failed")
    assert {t["task_id"]: t["state"] for t in got["result"]["tasks"]} == {f"task_{i}": "SUCCEEDED" for i in range(5)}


def test_without_a_rows_mode_an_attach_runs_a_row_per_step_as_before(tmp_path):
    answers = {"ATTACH": _attached(_STEPS), "step:b": _step("SUCCEEDED"), "step:c": _step("SUCCEEDED"),
               "step:d": _step("SUCCEEDED"), "STATUS": {"state": "SUCCEEDED", "state_note": None, "steps": []}}
    got = _run(tmp_path, {"attach": "wf_s"}, answers)
    kinds = [c["agentType"] for c in got["calls"]]
    assert kinds.count("sc:step") == 3 and "sc:summary" not in kinds  # MUTATION: default run.js to summary -- 0 step rows.


# --------------------------------------------------------------------------
# run.js: per-step shares
# --------------------------------------------------------------------------


def _attach_share(tmp_path, share: dict) -> dict:
    answers = {"ATTACH": _attached(_STEPS), "step:b": _step("SUCCEEDED"), "step:c": _step("SUCCEEDED"),
               "step:d": _step("SUCCEEDED"), "STATUS": {"state": "SUCCEEDED", "state_note": None, "steps": []}}
    got = _run(tmp_path, {"attach": "wf_s", "title": "scan", "rows": "per-step", "row_share": share}, answers)
    assert "error" not in got, got.get("error")
    return got


def test_each_per_step_copy_follows_its_share_and_only_the_first_reports_the_finished_steps(tmp_path):
    first = _attach_share(tmp_path, {"from": 1, "to": 2, "steps": ["b", "c"], "others": ["d"]})
    second = _attach_share(tmp_path, {"from": 3, "to": 3, "steps": ["d"], "others": ["b", "c"]})

    def followed(got):
        return [line for c in got["calls"] if c["agentType"] == "sc:step"
                for line in c["prompt"].split("\n") if line.startswith("step_id: ")]

    assert followed(first) == ["step_id: b", "step_id: c"]  # MUTATION: follow every open step -- d too.
    assert followed(second) == ["step_id: d"]
    finished = "a SUCCEEDED · finished before attach"
    assert sum(1 for got in (first, second) for line in got["logs"] if line.startswith(finished)) == 1  # MUTATION: every copy reports it -- 2.
    assert any(line.startswith(finished) for line in first["logs"])  # MUTATION: the last copy reports it instead.
    assert [s["step_id"] for s in second["result"]["steps"]] == ["d"]  # MUTATION: return the finished rows in every copy.


# --------------------------------------------------------------------------
# The follower agent, the skill and the version
# --------------------------------------------------------------------------


def _js_states(name: str) -> list[str]:
    import re

    source = _RUN_JS.read_text()
    match = re.search(r"const " + name + r" = \[([^\]]*)\]", source)
    assert match, f"run.js has no {name}"
    return re.findall(r"'([A-Z_]+)'", match.group(1))


def test_the_follower_counts_states_into_the_same_buckets_as_run_js():
    text = " ".join(_SUMMARY_AGENT.read_text().split())
    for bucket, name in (("running", "SUMMARY_RUNNING"), ("done", "SUMMARY_DONE"), ("failed", "SUMMARY_FAILED")):
        states = _js_states(name)
        assert states, name
        # MUTATION: move CANCELLED from failed to done in run.js only -- the agent's list no longer matches.
        assert f"**{bucket}**: " + ", ".join(states) in text, (bucket, states)
    assert "AWAITING_INPUT" in text and "question(s) for the owner" in text


def test_the_skill_describes_both_modes_and_the_per_step_flag():
    flat = " ".join(_SKILL.read_text().split())
    assert "`rows`" in flat and "`summary`" in flat and "`per-step`" in flat  # MUTATION: no mode in the skill.
    assert "--per-step" in flat  # MUTATION: no override documented.
    assert "rows 9-16" in flat  # MUTATION: no word on the split copies.


def test_the_plugin_version_moved_one_patch_past_0_5_49_with_its_pin():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    # Past it, not equal to it: a later lane moves it again.
    assert version > (0, 5, 49)  # MUTATION: no bump -- 0.5.49.
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in pinned)  # MUTATION: bump the version, not the pin.
