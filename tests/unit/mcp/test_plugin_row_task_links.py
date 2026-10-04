"""Every SwarmCloud row in Claude Code says where its task is watched, whatever its state.

Owner request, 2026-10-04 (functionality wave 4, lane P2): for each task of a
workflow shown in Claude Code through the sc plugin, a link that opens the task
in the console REGARDLESS of its status -- queued, parked, dispatched,
starting, running, succeeded, failed, cancelled, dead-lettered -- visible
without waiting for the step to start or finish. Until then a row wrote its
link on its first progress line and its last, so a row parked for an hour, or
running for two, showed no link where the person looked; and the row's LABEL,
the text /workflows shows, carried none at all.

'A console link is copied, never built' (owner decision 2026-10-01) STAYS. The
links below sit on a host and path no code here could derive, so a consumer
that built its own link instead of copying the served one prints a different
string and fails.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from swarm_mcp import compact, sc

import test_plugin_agents_and_workflows as plugin
from test_auto_attach import _run_all
from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_STEP_MD = _PLUGIN / "agents" / "step.md"
_SKILL_MD = _PLUGIN / "skills" / "sc" / "SKILL.md"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"


def _served(task_id: str) -> str:
    return f"https://console.example.test/served/agent/{task_id}?from=api"


def _served_workflow(workflow_id: str) -> str:
    return f"https://console.example.test/served/flow/{workflow_id}?from=api"


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def _rows(got: dict) -> list[dict]:
    return [c for c in got["calls"] if c["agentType"] == "sc:step"]


# --------------------------------------------------------------------------
# A row's LABEL carries its task's link from the moment the row exists
# --------------------------------------------------------------------------


_ATTACHED = {
    "workflow_id": "wf_9",
    "console": _served_workflow("wf_9"),
    "state": "RUNNING",
    "state_note": None,
    "bridge_version": "0.1.0",
    "follow_error": None,
    "error": None,
    "steps": [
        {"step_id": "implement", "task_id": "task_1", "depends_on": [], "state": "SUCCEEDED",
         "console": _served("task_1")},
        {"step_id": "review", "task_id": "task_2", "depends_on": ["implement"], "state": "RUNNING",
         "console": _served("task_2")},
        # Parked and never started: the row exists, and nothing has happened yet.
        {"step_id": "fix", "task_id": "task_3", "depends_on": ["review"], "state": "PARKED",
         "console": _served("task_3")},
    ],
}

_ATTACH_ANSWERS = {
    "ATTACH": _ATTACHED,
    "step:review": plugin._step("SUCCEEDED", cost_usd=0.4, duration_s=600, console=_served("task_2")),
    "step:fix": plugin._step("SUCCEEDED", cost_usd=0.2, duration_s=300, console=_served("task_3")),
    "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": _served_workflow("wf_9"), "steps": []},
}


def test_a_parked_never_started_steps_row_label_carries_its_link(tmp_path):
    got = plugin._run(tmp_path, {"attach": "wf_9"}, _ATTACH_ANSWERS)
    assert "error" not in got, got.get("error")
    labels = {c["prompt"].split("\n")[1]: c["label"] for c in _rows(got)}
    assert labels["step_id: fix"].endswith(" · " + _served("task_3")), labels
    assert labels["step_id: review"].endswith(" · " + _served("task_2")), labels


def test_every_submitted_steps_row_label_carries_its_link_before_it_starts(tmp_path):
    submitted = {
        **plugin._SUBMITTED,
        "console": _served_workflow("wf_1"),
        "steps": [{**s, "console": _served(s["task_id"])} for s in plugin._SUBMITTED["steps"]],
    }
    got = plugin._run(tmp_path, plugin._SPEC, {**plugin._ANSWERS, "SUBMIT": submitted})
    assert "error" not in got, got.get("error")
    for call in _rows(got):
        task_id = call["prompt"].split("\n")[0].removeprefix("task_id: ")
        assert call["label"].startswith("[SwarmCloud] scan · stage "), call["label"]
        assert call["label"].endswith(" · " + _served(task_id)), call["label"]


def test_a_long_workflow_name_never_cuts_the_link_out_of_the_label(tmp_path):
    """The name is cut to fit, never the link: a clipped URL opens nothing."""
    spec = {**plugin._SPEC, "label": "a-very-long-workflow-label-" * 4}
    submitted = {
        **plugin._SUBMITTED,
        "spec_digest": plugin._bridge_digest(spec),
        "steps": [{**s, "console": _served(s["task_id"])} for s in plugin._SUBMITTED["steps"]],
    }
    got = plugin._run(tmp_path, spec, {**plugin._ANSWERS, "SUBMIT": submitted})
    assert "error" not in got, got.get("error")
    for call in _rows(got):
        task_id = call["prompt"].split("\n")[0].removeprefix("task_id: ")
        assert "…" in call["label"], "the long name was not cut, so this proves nothing"
        assert call["label"].endswith(" · " + _served(task_id)), call["label"]


def test_a_row_label_is_a_bare_https_url_with_no_escape_sequence(tmp_path):
    """A bare URL: the terminal makes it clickable, and nothing in the label can
    render as stray escape bytes where OSC 8 is not passed through."""
    got = plugin._run(tmp_path, {"attach": "wf_9"}, _ATTACH_ANSWERS)
    for call in _rows(got):
        assert "\x1b" not in call["label"] and "\x07" not in call["label"]
        assert re.search(r" · https://\S+$", call["label"]), call["label"]


def test_a_step_with_no_served_link_keeps_its_plain_label(tmp_path):
    got = plugin._run(tmp_path, plugin._SPEC, plugin._ANSWERS)
    assert "error" not in got, got.get("error")
    assert all("http" not in c["label"] for c in got["calls"]), [c["label"] for c in got["calls"]]


def test_each_row_is_handed_its_submission_link_to_fall_back_on(tmp_path):
    got = plugin._run(tmp_path, {"attach": "wf_9"}, _ATTACH_ANSWERS)
    prompts = {c["prompt"].split("\n")[1]: c["prompt"] for c in _rows(got)}
    assert "console: " + _served("task_3") in prompts["step_id: fix"].split("\n")
    assert "console: " + _served("task_2") in prompts["step_id: review"].split("\n")


def test_a_row_with_no_submission_link_is_told_none(tmp_path):
    got = plugin._run(tmp_path, plugin._SPEC, plugin._ANSWERS)
    for call in _rows(got):
        assert "console: none" in call["prompt"].split("\n"), call["prompt"]


# --------------------------------------------------------------------------
# A reply without a link falls back to the submission's, never a built one
# --------------------------------------------------------------------------


def test_a_row_answer_without_console_falls_back_to_the_submission_link(tmp_path):
    answers = {**_ATTACH_ANSWERS, "step:fix": plugin._step("SUCCEEDED", console=None)}
    got = plugin._run(tmp_path, {"attach": "wf_9"}, answers)
    assert "error" not in got, got.get("error")
    fix = next(row for row in got["result"]["steps"] if row["step_id"] == "fix")
    assert fix["console"] == _served("task_3"), fix
    line = next(line for line in got["logs"] if line.startswith("fix "))
    assert line.endswith(" · console: " + _served("task_3")), line
    # Exactly the served string, and no other URL anywhere: nothing was built.
    urls = set(re.findall(r"https?://\S+", json.dumps(got)))
    served = {_served(f"task_{n}") for n in (1, 2, 3)} | {_served_workflow("wf_9")}
    assert urls <= served, urls - served


# --------------------------------------------------------------------------
# The workflow's own link: on its Result row and in the result
# --------------------------------------------------------------------------


def test_the_result_row_label_and_result_carry_the_workflow_link(tmp_path):
    got = plugin._run(tmp_path, {"attach": "wf_9"}, _ATTACH_ANSWERS)
    status = next(c for c in got["calls"] if c["prompt"].startswith("STATUS"))
    assert status["label"].endswith(" · " + _served_workflow("wf_9")), status["label"]
    assert got["result"]["console"] == _served_workflow("wf_9")
    assert got["logs"][-1].endswith(" · console: " + _served_workflow("wf_9")), got["logs"]


def test_the_result_falls_back_to_the_submission_link_when_status_has_none(tmp_path):
    answers = {**_ATTACH_ANSWERS, "STATUS": {**_ATTACH_ANSWERS["STATUS"], "console": None}}
    got = plugin._run(tmp_path, {"attach": "wf_9"}, answers)
    assert got["result"]["console"] == _served_workflow("wf_9")


def test_a_refused_follow_still_carries_the_workflow_link(tmp_path):
    attached = {**_ATTACHED, "follow_error": "unknown format 'progress'"}
    got = plugin._run(tmp_path, {"attach": "wf_9"}, {**_ATTACH_ANSWERS, "ATTACH": attached})
    assert got["result"]["state"] == "FOLLOW_REFUSED"
    assert got["result"]["console"] == _served_workflow("wf_9")


# --------------------------------------------------------------------------
# The bridge: every progress line ends with the link, parked → running → done
# --------------------------------------------------------------------------


def _link_tasks(swarm, monkeypatch) -> None:  # noqa: F811 - fixture name
    original = swarm.task

    def task(task_id):
        doc = dict(original(task_id))
        doc["links"] = {"console": _served(task_id)}
        return doc

    monkeypatch.setattr(swarm, "task", task)


def test_every_progress_line_parked_running_succeeded_ends_with_the_link(swarm, world, monkeypatch):  # noqa: F811
    world.task("task_a", state="PARKED")
    doc = world.db.docs["tasks/task_a"]
    doc["step_id"] = "review"
    _link_tasks(swarm, monkeypatch)

    written: list[str] = []
    reply = compact.watch_progress(swarm, ["task_a"], step_id="review")
    written += reply["progress"]

    doc.update({"state": "RUNNING", "started_at": NOW, "attempt_count": 1, "max_attempts": 3})
    reply = compact.watch_progress(swarm, ["task_a"], since=reply["since"], step_id="review")
    assert reply["changed"] is True
    written += reply["progress"]

    doc.update({"state": "SUCCEEDED", "completed_at": NOW})
    reply = compact.watch_progress(swarm, ["task_a"], since=reply["since"], step_id="review")
    assert reply["stop"] is True
    written += reply["progress"]

    assert len(written) == 3, written
    assert [line.split("] ", 1)[1].split(" ")[0] for line in written] == ["PARKED", "RUNNING", "SUCCEEDED"], written
    for line in written:
        assert line.endswith(" · console: " + _served("task_a")), line


def test_an_unchanged_reply_writes_nothing_even_with_a_link(swarm, world, monkeypatch):  # noqa: F811
    """The link is not news: a reply whose state did not move stays silent."""
    world.task("task_a", state="PARKED")
    world.db.docs["tasks/task_a"]["step_id"] = "review"
    _link_tasks(swarm, monkeypatch)
    first = compact.watch_progress(swarm, ["task_a"], step_id="review", wait_seconds=0)
    again = compact.watch_progress(swarm, ["task_a"], since=first["since"], step_id="review", wait_seconds=0)
    assert again["progress"] == [] and again["changed"] is False


# --------------------------------------------------------------------------
# step.md: every line the row writes ends with the link
# --------------------------------------------------------------------------


def test_step_md_ends_every_written_line_with_the_link():
    flat = _flat(_STEP_MD)
    first, _, rest = flat.partition("## 2. Follow it until it stops")
    follow = rest.partition("## 2a.")[0]
    assert "Every line this row writes ends with `· console: <link>`" in flat
    assert "console" in first and "console" in follow
    assert "only on its first" not in flat.lower()


def test_step_md_falls_back_to_the_prompts_link_and_never_builds_one():
    flat = _flat(_STEP_MD)
    assert "`console` line in your prompt" in flat
    assert "tasks[0].console" in flat
    assert "never build" in flat.lower()
    assert "/agents/" not in flat and "https://" not in flat


# --------------------------------------------------------------------------
# `sc workflows` and the attach --all listing
# --------------------------------------------------------------------------


def test_sc_workflows_prints_each_current_steps_link(monkeypatch):
    listing = {
        "tenant_id": "eng", "count": 1, "complete": True,
        "workflows": [{
            "workflow_id": "wf_b", "label": "nightly", "state": "RUNNING", "age_seconds": 90,
            "console": _served_workflow("wf_b"),
            "current_steps": [
                {"step_id": "draft", "task_id": "task_d", "state": "PARKED", "console": _served("task_d")},
                {"step_id": "lint", "task_id": "task_l", "state": "QUEUED"},
            ],
        }],
    }
    monkeypatch.setattr(sc, "running_workflows", lambda client: listing)
    out = io.StringIO()
    args = sc.build_parser().parse_args(["workflows"])
    assert sc.cmd_workflows(object(), args, out) == sc.EXIT_OK
    lines = out.getvalue().splitlines()
    assert any(line.strip() == "console: " + _served_workflow("wf_b") for line in lines), lines
    draft = next(line for line in lines if "draft" in line and "task_d" in line)
    assert draft.rstrip().endswith("console: " + _served("task_d")), lines
    lint = next(line for line in lines if "lint" in line and "task_l" in line)
    assert "console" not in lint, "a step the API served no link for gets none"


def test_attach_all_lists_every_followed_workflows_link(tmp_path):
    ids = ["wf_a", "wf_b"]
    answers = {
        "LIST": {"count": 2, "error": None, "workflows": [
            {"workflow_id": i, "label": None, "state": "RUNNING", "console": _served_workflow(i)} for i in ids
        ]},
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    }
    for i in ids:
        answers["ATTACH\nworkflow_id: " + i] = {
            **_ATTACHED, "workflow_id": i, "console": _served_workflow(i),
            "steps": [{"step_id": "work", "task_id": "t_" + i, "depends_on": [], "state": "RUNNING",
                       "console": _served("t_" + i)}],
        }
    answers["step:work"] = plugin._step("SUCCEEDED", console=None)
    got = _run_all(tmp_path, answers)
    assert "error" not in got, got.get("error")
    for i in ids:
        listed = [line for line in got["logs"] if line.startswith(i + " ") and "following" in line]
        assert listed and listed[0].endswith(" · console: " + _served_workflow(i)), got["logs"]


def test_the_sc_skill_says_every_step_and_row_carries_its_link():
    flat = _flat(_SKILL_MD)
    assert "each current step's console link" in flat
    assert "every row's label carries its task's console link" in flat


# --------------------------------------------------------------------------
# The version and its pin move together
# --------------------------------------------------------------------------


_PREVIOUS_RELEASED_VERSION = (0, 5, 11)


def test_the_plugin_version_and_its_bridge_pin_moved_together():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#subdirectory=apps/swarm-mcp" in arg for arg in pinned), pinned
