"""A real `rows` argument, and a summary line the bridge builds (PLUGIN-ROW-SPLIT-2, #1809).

PLUGIN-ROW-SPLIT (#1811) shipped the `rows` setting and left four questions;
the owner's answers, 2026-10-21 under the overnight pre-authorisation:

1. `swarm_workflow_launch` takes `rows: "summary" | "per-step"`, passed to
   `launch.for_attach` and `launch.for_all`, so `attach --all --per-step`
   works too. The `--per-step` suffix in the `attach` value was read for
   one release and is refused since SC-ATTACH-SUFFIX-DROP (0.5.53). The tool's description says what is true now: split copies,
   a summary row, a cap of 100 within 1-200.
2. The bridge computes the summary row's count line: `swarm_follow` `format:
   "summary"` returns it, sorted into the buckets run.js uses, and the
   sc:summary row writes it verbatim instead of counting.
4. The README counts six agents.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_mcp import launch, progress, server
from swarm_mcp.client import SwarmError

from test_follow_cursor import World, swarm, world  # noqa: F401 - fixtures
from test_plugin_agents_and_workflows import _PLUGIN, _run
from test_plugin_row_split import _RUN_JS, _SUMMARY_AGENT, SESSION, _Client, _envelope, _shares

_README = _PLUGIN / "README.md"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"


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
    # Handles are per process: a follower another test answered would resume.
    progress.HANDLES.clear()


def _listed(monkeypatch, *workflow_ids: str) -> None:
    from swarm_mcp import sc

    listed = [{"workflow_id": wid, "workspace": "eng", "title": wid, "label": None, "steps_total": 18,
               "console": None, "claude_session": SESSION, "state": "RUNNING",
               "progress": {"done": 0, "total": 18, "age_seconds": 10}} for wid in workflow_ids]
    monkeypatch.setattr(sc, "running_workflows",
                        lambda client, **_: {"count": len(listed), "complete": True, "workflows": listed})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})


def _launch(client, **args) -> dict:
    return json.loads(server._call(client, "swarm_workflow_launch", args))


# --------------------------------------------------------------------------
# 1. The `rows` argument
# --------------------------------------------------------------------------


def test_rows_per_step_on_attach_all_writes_split_copies(monkeypatch):
    _listed(monkeypatch, "wf_big")
    out = _launch(_Client(_envelope(18)), attach="all", rows="per-step")

    assert out["rows"] == "per-step"  # MUTATION: never pass `rows` to for_all -- "summary", the setting.
    assert [len(s) for s in _shares(out)] == [8, 8, 2]  # MUTATION: one summary copy -- KeyError on row_share.
    assert [l["title"] for l in out["launches"]] == [  # MUTATION: no split on attach-all -- one title.
        "SC · wf_big · 18 steps · rows 1-8",
        "SC · wf_big · 18 steps · rows 9-16",
        "SC · wf_big · 18 steps · rows 17-18",
    ]


def test_rows_summary_on_attach_all_outranks_a_per_step_setting(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "per-step")
    _listed(monkeypatch, "wf_big")
    out = _launch(_Client(_envelope(18)), attach="all", rows="summary")
    assert [l["args"]["rows"] for l in out["launches"]] == ["summary"]  # MUTATION: the setting wins -- 3 per-step copies.


def test_rows_reaches_the_single_task_batch_of_attach_all(monkeypatch):
    from swarm_mcp import sc

    _listed(monkeypatch)
    tasks = [{"task_id": f"task_{i}", "label": f"t{i}", "workspace": "eng", "claude_session": SESSION}
             for i in range(3)]
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 3, "tasks": tasks})
    out = launch.for_all(_Client(_envelope(0)), rows="per-step")
    (batch,) = [l for l in out["launches"] if "attach_tasks" in l["args"]]
    assert batch["args"]["rows"] == "per-step"  # MUTATION: for_tasks reads the setting, not `rows` -- "summary".


def test_rows_per_step_on_one_attach_splits_it():
    client = _Client(_envelope(18))
    out = _launch(client, attach="wf_big", rows="per-step")
    assert client.read == ["wf_big"]
    assert [l["args"]["rows"] for l in out["launches"]] == ["per-step"] * 3  # MUTATION: drop `rows` for one id -- one summary copy.


@pytest.mark.parametrize("suffix", ["--per-step", "--summary"])
def test_the_attach_suffix_is_refused_and_the_refusal_names_rows(suffix):
    """SC-ATTACH-SUFFIX-DROP: the suffix 0.5.50 read was kept one release and
    is gone; `rows` is the one way to pick a mode for one attach."""
    client = _Client(_envelope(18))
    with pytest.raises(SwarmError) as refused:  # MUTATION: keep reading the suffix -- a per-step attach, no error.
        _launch(client, attach=f"wf_big {suffix}")
    message = str(refused.value)
    assert "`rows`" in message  # MUTATION: refuse without naming the argument that replaced it -- absent.
    assert '"per-step"' in message and '"summary"' in message  # MUTATION: name `rows` but not its values -- absent.
    assert "Nothing was written" in message  # MUTATION: refuse after a read -- the house refusal's tail missing.
    assert client.read == []  # MUTATION: fetch "wf_big" before refusing -- ["wf_big"].


def test_two_ids_are_still_refused_and_all_with_a_suffix_too():
    client = _Client(_envelope(18))
    with pytest.raises(SwarmError, match="one workflow id"):  # MUTATION: take the first word -- no error.
        _launch(client, attach="wf_a wf_b")
    # `all --per-step` is not `all`: it reaches the one-id path and is refused there.
    with pytest.raises(SwarmError, match="`rows`"):  # MUTATION: strip the suffix from "all" -- an attach-all.
        _launch(client, attach="all --per-step")
    assert client.read == []  # MUTATION: read before refusing -- an id fetched.


def test_the_suffix_reader_and_its_release_promise_are_gone():
    source = Path(launch.__file__).read_text(encoding="utf-8")
    assert not hasattr(launch, "ATTACH_FLAGS")  # MUTATION: keep the flag table -- present.
    assert not hasattr(launch, "attach_target")  # MUTATION: keep the suffix reader -- present.
    # MUTATION: keep "drop it in plugin 0.5.52" beside the code -- a release named for a drop that happened.
    assert not re.search(r"0\.5\.5[0-9]", source)
    flat = " ".join(_SKILL.read_text().split())
    assert "is still read until" not in flat  # MUTATION: keep the skill's deprecation line -- present.
    assert '{attach: "<wf_id> --per-step"}' not in flat  # MUTATION: keep the old attach value in the skill -- present.


def test_a_suffix_beside_rows_is_refused_and_an_unknown_rows_too(monkeypatch):
    with pytest.raises(SwarmError, match="rows"):  # MUTATION: read the suffix and let one silently win -- no error.
        server._call(_Client(_envelope(18)), "swarm_workflow_launch", {"attach": "wf_big --per-step", "rows": "summary"})
    with pytest.raises(SwarmError, match="rows"):  # MUTATION: pass any value through -- the setting's shape.
        server._call(_Client(_envelope(18)), "swarm_workflow_launch", {"attach": "wf_big", "rows": "split"})
    with pytest.raises(SwarmError, match="rows"):  # MUTATION: accept and ignore `rows` with a spec -- no error.
        server._call(_Client(_envelope(18)), "swarm_workflow_launch", {"spec": {"steps": []}, "rows": "per-step"})


def _tool(name: str) -> dict:
    (tool,) = [t for t in server.TOOLS if t["name"] == name]
    return tool


def test_the_launch_tool_offers_rows_and_describes_what_is_true_now():
    tool = _tool("swarm_workflow_launch")
    rows = tool["inputSchema"]["properties"]["rows"]
    assert rows["enum"] == ["summary", "per-step"]  # MUTATION: no `rows` in the schema -- KeyError.
    text = " ".join(tool["description"].split())
    assert "1-200" in text and "100" in text  # MUTATION: keep "30 unless set; 1-50" -- absent.
    assert "1-50" not in text and "30 unless set" not in text  # MUTATION: leave the stale range -- present.
    assert "one copy each" not in text  # MUTATION: leave the stale shape -- present.
    assert "rows 1-8" in text and "summary" in text  # MUTATION: say nothing of split copies or the summary row.


# --------------------------------------------------------------------------
# 2. The summary line, built by the bridge
# --------------------------------------------------------------------------


def _summary_world(world) -> None:
    for task_id, state in (("task_r1", "RUNNING"), ("task_r2", "LEASED"), ("task_q1", "QUEUED"),
                           ("task_q2", "PARKED"), ("task_d1", "SUCCEEDED"), ("task_f1", "CANCELLED")):
        world.task(task_id, state=state)
    world.db.docs["tasks/task_d1"]["result_summary"] = {
        "git": {"pull_request": {"url": "https://github.com/acme/w/pull/31", "number": 31}}}


_FOLLOWED = ["task_r1", "task_r2", "task_q1", "task_q2", "task_d1", "task_f1"]
_LINK = "https://console.example.test/workflows/wf_s"


def _summary_of(**extra) -> dict:
    return {"noun": "step", "total": 9, "counted": {"running": 0, "queued": 1, "done": 2, "failed": 0},
            "prs": ["PR #12"], "console": _LINK, "line": "", **extra}


def _follow_summary(client, **args) -> dict:
    return json.loads(server._call(client, "swarm_follow", {
        "task_ids": _FOLLOWED, "format": "summary", "summary_of": _summary_of(), **args}))


def test_swarm_follow_summary_returns_the_exact_count_line(swarm, world):
    _summary_world(world)
    reply = _follow_summary(swarm)

    # 2 running (RUNNING, LEASED), 2+1 queued (QUEUED, PARKED, one counted),
    # 1+2 done, 1 failed (CANCELLED); the counted PR first, then the new one.
    assert reply["line"] == (  # MUTATION: count PARKED as running -- "3 running · 2 queued".
        "9 steps: 2 running · 3 queued · 3 done · 1 failed · PR #12, #31 · console: " + _LINK)
    assert reply["line_changed"] is True  # MUTATION: never say it changed -- False.
    assert "progress" not in reply  # MUTATION: hand the summary row the per-task lines too -- present.
    by_id = {row["task_id"]: row for row in reply["tasks"]}
    assert by_id["task_d1"]["outcome"]["pr_url"].endswith("/pull/31")  # MUTATION: no outcome -- KeyError.


def test_the_summary_line_is_said_changed_only_when_it_changes(swarm, world):
    _summary_world(world)
    first = _follow_summary(swarm)
    again = _follow_summary(swarm, since=first["since"], wait_seconds=0)
    assert again["line"] == first["line"]
    assert again["line_changed"] is False  # MUTATION: compare with the prompt's line every time -- True.
    # The finished tasks are not read again, and their PR stays on the line.
    world.set_state("task_q1", "RUNNING")
    moved = _follow_summary(swarm, since=again["since"], wait_seconds=0)
    assert moved["line"].startswith("9 steps: 3 running · 2 queued · 3 done · 1 failed · PR #12, #31")  # MUTATION: forget a finished task's PR -- "PR #12 ·".
    assert moved["line_changed"] is True
    assert {row["task_id"] for row in moved["tasks"]} == {"task_r1", "task_r2", "task_q1", "task_q2"}  # MUTATION: re-read the finished -- d1, f1 in.


def test_the_first_summary_line_equal_to_the_prompts_is_not_news(swarm, world):
    _summary_world(world)
    line = "9 steps: 2 running · 3 queued · 3 done · 1 failed · PR #12, #31 · console: " + _LINK
    reply = json.loads(server._call(swarm, "swarm_follow", {
        "task_ids": _FOLLOWED, "format": "summary", "summary_of": _summary_of(line=line)}))
    assert reply["line"] == line and reply["line_changed"] is False  # MUTATION: ignore summary_of.line -- True.


def test_swarm_follow_summary_needs_summary_of(swarm, world):
    _summary_world(world)
    with pytest.raises(SwarmError, match="summary_of"):  # MUTATION: count only what was followed -- a wrong line.
        server._call(swarm, "swarm_follow", {"task_ids": _FOLLOWED, "format": "summary"})
    assert _tool("swarm_follow")["inputSchema"]["properties"]["format"]["enum"] == [
        "json", "lines", "progress", "summary"]  # MUTATION: no summary format in the schema.


def _js_states(name: str) -> set[str]:
    match = re.search(r"const " + name + r" = \[([^\]]*)\]", _RUN_JS.read_text())
    assert match, f"run.js has no {name}"
    return set(re.findall(r"'([A-Z_]+)'", match.group(1)))


def test_the_bridges_buckets_equal_run_js():
    from swarm_mcp import compact

    # MUTATION: move CANCELLED to done in the bridge only -- the sets differ.
    assert set(compact.SUMMARY_RUNNING) == _js_states("SUMMARY_RUNNING")
    assert set(compact.SUMMARY_DONE) == _js_states("SUMMARY_DONE")
    assert set(compact.SUMMARY_FAILED) == _js_states("SUMMARY_FAILED")
    assert compact.summary_bucket("PARKED") == "queued" and compact.summary_bucket(None) == "queued"  # MUTATION: unread is running.


# --------------------------------------------------------------------------
# run.js and the row: the bridge's line, copied
# --------------------------------------------------------------------------


_STEPS = [
    {"step_id": "a", "task_id": "task_a", "depends_on": [], "state": "SUCCEEDED", "console": None},
    {"step_id": "b", "task_id": "task_b", "depends_on": [], "state": "RUNNING", "console": None},
    {"step_id": "c", "task_id": "task_c", "depends_on": [], "state": "QUEUED", "console": None},
]


def test_run_js_hands_the_summary_row_its_summary_of(tmp_path):
    attached = {"workflow_id": "wf_s", "state": "RUNNING", "state_note": None, "title": "scan", "label": None,
                "console": _LINK, "bridge_version": "0.1.0", "follow_error": None, "error": None, "steps": _STEPS}
    answers = {"ATTACH": attached,
               "SUMMARY": {"state": "FINISHED", "last_error": None, "tasks": []},
               "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": _LINK, "steps": []}}
    got = _run(tmp_path, {"attach": "wf_s", "title": "scan", "rows": "summary"}, answers)
    (row,) = [c for c in got["calls"] if c["agentType"] == "sc:summary"]
    (given,) = [line for line in row["prompt"].split("\n") if line.startswith("summary_of: ")]
    summary_of = json.loads(given[len("summary_of: "):])
    assert summary_of == {  # MUTATION: no summary_of in the prompt -- ValueError on unpacking.
        "noun": "step", "total": 3, "counted": {"running": 0, "queued": 0, "done": 1, "failed": 0},
        "prs": [], "console": _LINK,
        "line": "3 steps: 1 running · 1 queued · 1 done · 0 failed · console: " + _LINK,
    }


def test_the_summary_row_follows_with_format_summary_and_writes_the_line_verbatim():
    text = " ".join(_SUMMARY_AGENT.read_text().split())
    assert '`format: "summary"`' in text  # MUTATION: keep the row on progress -- absent.
    assert '`format: "progress"`' not in text  # MUTATION: leave both formats in -- present.
    assert "`summary_of`" in text and "verbatim" in text  # MUTATION: let the row count -- absent.
    assert "line_changed" in text  # MUTATION: write every line -- absent.


# --------------------------------------------------------------------------
# 4. The README's agent count, the skill, and the version
# --------------------------------------------------------------------------


def test_the_readme_counts_every_agent_the_plugin_ships():
    agents = sorted(p.stem for p in (_PLUGIN / "agents").glob("*.md"))
    words = {5: "five", 6: "six", 7: "seven"}
    text = " ".join(_README.read_text().split())
    assert f"All {words[len(agents)]} agents run on" in text  # MUTATION: leave "All five" -- absent.
    assert "sc:summary" in text.split("agents run on", 1)[1][:600]  # MUTATION: name five of six -- absent.


def test_the_skill_names_the_rows_argument_for_both_attaches():
    flat = " ".join(_SKILL.read_text().split())
    assert '{attach: "<wf_id>", rows: "per-step"}' in flat  # MUTATION: keep the suffix as the way -- absent.
    assert '{attach: "all", rows: "per-step"}' in flat  # MUTATION: no per-step attach-all -- absent.


def test_the_plugin_version_moved_one_patch_past_0_5_52_with_its_pin():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    # Past it, not equal to it: a later lane moves it again. 0.5.52 still read
    # the attach suffix; SC-ATTACH-SUFFIX-DROP's release refuses it.
    assert version > (0, 5, 52)  # MUTATION: no bump -- 0.5.52.
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in pinned)  # MUTATION: bump the version, not the pin.
