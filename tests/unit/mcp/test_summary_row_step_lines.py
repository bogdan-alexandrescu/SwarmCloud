"""A WORKFLOW'S SUMMARY ROW NAMES EVERY STEP ON ITS OWN LINE (owner
2026-11-04, 23:05): Claude Code shows a workflow with the same structure as
the console. The row's output lists every step the SERVED workflow holds --
the steps SwarmCloud appended (a merge) included -- grouped by the console's
stages (`Stage N`, the dependency level plus one), each with its state and
console link, and writes the lines again only when a step's STATE changes:
a pull request appearing, or the remote log moving, writes nothing.

THE MUTATIONS THIS CATCHES: no step lines (the first assertion goes red);
step lines written on every answer (the count of boards goes red); grouping
by the spec's order instead of the stage (the board's text goes red).
"""

from __future__ import annotations

import json
import subprocess

from test_plugin_agents_and_workflows import _HARNESS, _RUN_JS, _node

#: _HARNESS, with a list of answers under one key handed out in turn.
_SEQUENCE = _HARNESS.replace(
    "const answer = fixture.answers[key]",
    "let answer = fixture.answers[key]\n"
    "  if (Array.isArray(answer)) answer = answer.length > 1 ? answer.shift() : answer[0]",
)
assert _SEQUENCE != _HARNESS


def _link(task_id: str) -> str:
    return f"https://console.example.test/agents/{task_id}"


_WF_LINK = "https://console.example.test/workflows/wf_s"


def _step(step_id: str, state: str, *parents: str) -> dict:
    return {"step_id": step_id, "task_id": f"task_{step_id}", "depends_on": list(parents), "state": state,
            "console": _link(f"task_{step_id}")}


def _run(tmp_path, answers: dict) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_SEQUENCE)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": {"attach": "wf_s", "title": "scan", "rows": "summary"},
                                   "answers": answers}))
    done = subprocess.run([_node(), str(harness), str(_RUN_JS), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return got


def _board(implement: str, review: str, merge: str, fix: str) -> str:
    """The lines in the console's order: stage by stage, and inside a stage
    the order the workflow was served in (merge before fix here)."""
    return "\n".join([
        "Stage 1",
        f"  implement · {implement} · console: {_link('task_implement')}",
        "Stage 2",
        f"  review · {review} · console: {_link('task_review')}",
        "Stage 3",
        f"  merge · {merge} · console: {_link('task_merge')}",
        f"  fix · {fix} · console: {_link('task_fix')}",
    ])


def test_the_summary_row_names_every_served_step_by_stage_and_rewrites_only_on_a_state_change(tmp_path):
    # The spec had three steps; SwarmCloud appended `merge`, which the served
    # workflow holds after review, in stage 3 beside `fix`.
    steps = [_step("implement", "RUNNING"), _step("review", "QUEUED", "implement"),
             _step("merge", "QUEUED", "review"), _step("fix", "QUEUED", "review")]
    attached = {"workflow_id": "wf_s", "state": "RUNNING", "state_note": None, "title": "scan", "label": None,
                "console": _WF_LINK, "bridge_version": "0.1.0", "follow_error": None, "error": None,
                "steps": steps}
    told = lambda **states: [{"task_id": f"task_{k}", "state": v} for k, v in states.items()]  # noqa: E731
    answers = {
        "ATTACH": attached,
        "SUMMARY": [
            # A state changed: the lines are written again.
            {"state": "running", "last_error": None,
             "tasks": told(implement="SUCCEEDED", review="RUNNING", merge="QUEUED", fix="QUEUED")},
            # Only a pull request appeared: no state moved, nothing written.
            {"state": "running", "last_error": None,
             "tasks": [{**t, "pr_url": "https://github.test/o/r/pull/1"} for t in
                       told(implement="SUCCEEDED", review="RUNNING", merge="QUEUED", fix="QUEUED")]},
        ],
        "STATUS": {"state": "RUNNING", "state_note": None, "console": _WF_LINK, "steps": []},
    }
    got = _run(tmp_path, answers)
    boards = [line for line in got["logs"] if isinstance(line, str) and line.startswith("Stage 1\n")]
    first = _board("RUNNING", "QUEUED", "QUEUED", "QUEUED")
    second = _board("SUCCEEDED", "RUNNING", "QUEUED", "QUEUED")
    assert boards == [first, second], got["logs"]
    # The row is handed the same lines, one `step:` entry per served step.
    (row, *_) = [c for c in got["calls"] if c["agentType"] == "sc:summary"]
    entries = [line for line in row["prompt"].split("\n") if line.startswith("step: ")]
    assert entries == [
        f"step: task_implement | Stage 1 | implement | RUNNING | {_link('task_implement')}",
        f"step: task_review | Stage 2 | review | QUEUED | {_link('task_review')}",
        f"step: task_merge | Stage 3 | merge | QUEUED | {_link('task_merge')}",
        f"step: task_fix | Stage 3 | fix | QUEUED | {_link('task_fix')}",
    ]


_SUMMARY_AGENT = _RUN_JS.parent.parent / "agents" / "summary.md"


def _section(text: str, heading: str) -> str:
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def test_the_summary_agent_writes_a_browser_tasks_browser_unused_line_once_on_its_own_line():
    """Follow-up answered yes on 5 November: a finished claude-code-browser
    task that never used its browser says so in its row's output."""
    named = " ".join(_section(_SUMMARY_AGENT.read_text(encoding="utf-8"), "## 2. Named on a line of their own").split())
    # MUTATION: the bullet left out of section 2 -- each of these goes red.
    assert "`claude-code-browser`" in named and "`outcome.browser_unused`" in named
    assert "once, on the step's own line" in named
