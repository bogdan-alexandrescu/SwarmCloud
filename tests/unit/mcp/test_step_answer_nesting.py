"""A row that nests its answer one level too deep still reaches the run's result whole (#1763).

MEASURED, 2026-10-20 ~22:40Z, plugin sc 0.5.45, in the journal of the
QAFIX2-OVERVIEW attach run (wf_4b9dc2654dce4a43a342): its review, fix and
merge rows called StructuredOutput with the documented object wrapped in one
more `result`,

    {"result": {"state": "...", "result": {"state": ..., "answer_excerpt": ..., ...}}}

and `stepAnswer()` read the outer `result` as the step's fields: it found only
`state` and a nested `result` there, so the run's summary kept the state and
nulled the excerpt, cost, duration, PR and last_error, with no artifacts. The
re-review row of the same run answered `{state, result: {...}}` and kept every
field -- the control, held here beside the nested rows. Two `attach_tasks`
rows (task_2a8fa001a602413086d9, task_a8380238991242adadfb) lost theirs the
same way, so the task rows are held too: both callers share stepAnswer().

What these tests hold:

  * the doubly nested answer, with or without a root `state`, reads every
    field from the inner object, for a step row and for a task row;
  * the documented answer, the flat pre-0.5.17 one, `{state, result: null}`
    and `{state: null, result: null}` read exactly as before;
  * the unwrap is one level: a `result` carrying step fields of its own is
    never replaced by a `result` nested inside it, and a third level is not
    read.
"""

from __future__ import annotations

import json
import subprocess

from test_auto_attach import _BY_PROMPT
from test_plugin_agents_and_workflows import _ANSWERS, _PLUGIN, _RUN_JS, _SPEC, _load, _node, _run, _step

_FIELDS = ("state", "answer_excerpt", "cost_usd", "duration_s", "pr_url", "artifacts", "last_error", "console")


def _fields(state: str, n: int) -> dict:
    """The bridge's null-free `result` for one step, distinct per `n`."""
    return {
        "state": state,
        "answer_excerpt": f"VERDICT: MERGE {n} -- every finding fixed",
        "cost_usd": 1.13 + n,
        "duration_s": 300.0 + n,
        "pr_url": f"https://github.com/acme/widgets/pull/{1760 + n}",
        "artifacts": [f"report-{n}.md", f"diff-{n}.patch"],
        "last_error": f"PARKED on CI_PENDING · PR #{1760 + n}",
        "console": f"https://c.test/agents/task_{n}",
        "questions": [],
    }


def _nested(fields: dict, *, root_state: bool = False) -> dict:
    """The shape the review, fix and merge rows sent: `{state, result}` itself
    inside a `result`, the root `state` left out (or kept, when asked)."""
    answer = {"result": {"state": fields["state"], "result": fields}}
    if root_state:
        answer["state"] = fields["state"]
    return answer


def _documented(fields: dict) -> dict:
    """The re-review row's shape, the control: `{state, result: {...}}`."""
    return {"state": fields["state"], "result": fields}


def _expected(fields: dict) -> dict:
    return {k: fields[k] for k in _FIELDS}


def _status_all(state: str = "SUCCEEDED") -> dict:
    return {"state": state, "state_note": None, "console": None,
            "steps": [{"step_id": s["step_id"], "state": state} for s in _SPEC["steps"]]}


def _step_rows(tmp_path, answers: dict) -> dict:
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "STATUS": _status_all(), **answers})
    assert "error" not in got, got.get("error")
    return {row["step_id"]: row for row in got["result"]["steps"]}


def _task_rows(tmp_path, answers: dict) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    tasks = [{"task_id": task_id, "title": task_id} for task_id in answers]
    fixture.write_text(json.dumps({"args": {"attach_tasks": tasks},
                                   "answers": {f"task_id: {k}": v for k, v in answers.items()}}))
    done = subprocess.run([_node(), str(harness), str(_RUN_JS), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return {row["task_id"]: row for row in got["result"]["tasks"]}


# --------------------------------------------------------------------------
# 1. followRow: the measured run's shapes, side by side
# --------------------------------------------------------------------------


def test_a_step_row_nested_one_level_too_deep_keeps_every_field(tmp_path):
    review = _fields("SUCCEEDED", 1)
    merge = _fields("PARKED", 2)
    re_review = _fields("SUCCEEDED", 3)
    rows = _step_rows(tmp_path, {
        "step:scan-01": _nested(review),
        "step:join": _nested(merge, root_state=True),
        "step:scan-02": _documented(re_review),
    })

    # MUTATION: stepAnswer() reading answer.result as the fields again (no
    # unwrap) nulls the excerpt, cost, duration, PR, last_error and console and
    # empties artifacts here, while the state still reads -- the #1763 summary.
    assert {k: rows["scan-01"].get(k) for k in _FIELDS} == _expected(review), rows["scan-01"]
    # MUTATION: unwrapping only when the root `state` is absent loses the merge
    # row, which carried one; its last_error "PARKED on CI_PENDING" is the
    # field the measured run lost.
    assert {k: rows["join"].get(k) for k in _FIELDS} == _expected(merge), rows["join"]
    # MUTATION: an unwrap that also fires on the documented shape (reading
    # answer.result.result, absent there) nulls the control's fields.
    assert {k: rows["scan-02"].get(k) for k in _FIELDS} == _expected(re_review), rows["scan-02"]
    # MUTATION: a nested row read with no state goes to rowFromStatus and is
    # marked `state_from`; the inner state is read, so none of them is.
    assert all("state_from" not in rows[s] for s in ("scan-01", "join", "scan-02")), rows


def test_the_inner_state_wins_over_the_outer_ones(tmp_path):
    inner = _fields("FAILED", 4)
    answer = {"state": "RUNNING", "result": {"state": "SUCCEEDED", "result": inner}}
    rows = _step_rows(tmp_path, {"step:scan-01": answer})
    # MUTATION: taking answer.result.state (or answer.state) before the inner
    # result.state reports SUCCEEDED (or RUNNING) over the bridge's FAILED.
    assert rows["scan-01"]["state"] == "FAILED", rows["scan-01"]


def test_a_nested_row_with_no_inner_state_takes_the_middle_then_the_root_state(tmp_path):
    middle = {k: v for k, v in _fields("x", 5).items() if k != "state"}
    root = {k: v for k, v in _fields("x", 6).items() if k != "state"}
    rows = _step_rows(tmp_path, {
        "step:scan-01": {"state": "RUNNING", "result": {"state": "FAILED", "result": middle}},
        "step:scan-02": {"state": "FAILED", "result": {"result": root}},
    })
    # MUTATION: dropping the answer.result.state fallback reads RUNNING here.
    assert rows["scan-01"]["state"] == "FAILED" and rows["scan-01"]["pr_url"] == middle["pr_url"], rows["scan-01"]
    # MUTATION: dropping the answer.state fallback sends this row to the
    # status read (SUCCEEDED, state_from) instead of its own FAILED.
    assert rows["scan-02"]["state"] == "FAILED" and "state_from" not in rows["scan-02"], rows["scan-02"]
    assert rows["scan-02"]["answer_excerpt"] == root["answer_excerpt"], rows["scan-02"]


# --------------------------------------------------------------------------
# 2. Every shape that read correctly before reads the same
# --------------------------------------------------------------------------


def test_the_shapes_that_read_before_read_the_same(tmp_path):
    flat = _fields("SUCCEEDED", 7)
    rows = _step_rows(tmp_path, {
        "step:scan-01": _step("SUCCEEDED", cost_usd=0.21, duration_s=252,
                              pr_url="https://github.com/acme/widgets/pull/231", artifacts=["a.md"]),
        "step:scan-02": {"state": "FAILED", "result": None},
        "step:join": {"state": None, "result": None},
        "step:report": _documented(flat),
    })
    # MUTATION: unwrapping a flat pre-0.5.17 answer (it has no `result`) or
    # normalising it would change these; it is taken as it is.
    assert {k: rows["scan-01"][k] for k in ("state", "cost_usd", "duration_s", "pr_url", "artifacts")} == {
        "state": "SUCCEEDED", "cost_usd": 0.21, "duration_s": 252,
        "pr_url": "https://github.com/acme/widgets/pull/231", "artifacts": ["a.md"]}
    # MUTATION: treating a null `result` as something to unwrap throws or
    # loses the root state.
    assert rows["scan-02"]["state"] == "FAILED" and rows["scan-02"]["artifacts"] == [], rows["scan-02"]
    assert "state_from" not in rows["scan-02"], rows["scan-02"]
    # MUTATION: a `{state: null, result: null}` row must still read its state
    # from the workflow status (followRow's existing check), not end stateless.
    assert rows["join"]["state"] == "SUCCEEDED" and rows["join"]["state_from"] == "workflow status", rows["join"]
    assert {k: rows["report"].get(k) for k in _FIELDS} == _expected(flat), rows["report"]


def test_the_unwrap_is_one_level_and_never_replaces_a_result_with_fields(tmp_path):
    own = _fields("SUCCEEDED", 8)
    deeper = _fields("FAILED", 9)
    rows = _step_rows(tmp_path, {
        # A result with step fields of its own, and a `result` inside it too.
        "step:scan-01": {"state": "SUCCEEDED", "result": {**own, "result": deeper}},
        # Three levels: the unwrap reads one, finds no fields, and stops.
        "step:scan-02": {"state": "SUCCEEDED", "result": {"result": {"result": deeper}}},
    })
    # MUTATION: unwrapping whenever an inner object `result` exists (ignoring
    # the outer fields) reads FAILED and #9's fields here.
    assert {k: rows["scan-01"].get(k) for k in _FIELDS} == _expected(own), rows["scan-01"]
    # MUTATION: a loop that unwraps until it finds fields reads #9's here.
    assert rows["scan-02"]["state"] == "SUCCEEDED", rows["scan-02"]
    assert rows["scan-02"]["answer_excerpt"] is None and rows["scan-02"]["pr_url"] is None, rows["scan-02"]
    assert rows["scan-02"]["artifacts"] == [], rows["scan-02"]


# --------------------------------------------------------------------------
# 3. The task rows (attach_tasks) share stepAnswer()
# --------------------------------------------------------------------------


def test_a_task_row_nested_one_level_too_deep_keeps_every_field(tmp_path):
    first = _fields("SUCCEEDED", 10)
    second = _fields("SUCCEEDED", 11)
    control = _fields("FAILED", 12)
    rows = _task_rows(tmp_path, {
        "task_2a8fa001a602413086d9": _nested(first),
        "task_a8380238991242adadfb": _nested(second, root_state=True),
        "task_control": _documented(control),
    })
    # MUTATION: followTaskRow reading the answer without stepAnswer's unwrap
    # nulls every field of the two measured Product Oracle rows.
    assert {k: rows["task_2a8fa001a602413086d9"].get(k) for k in _FIELDS} == _expected(first)
    assert {k: rows["task_a8380238991242adadfb"].get(k) for k in _FIELDS} == _expected(second)
    # MUTATION: an unwrap that fires on the documented shape nulls the control.
    assert {k: rows["task_control"].get(k) for k in _FIELDS} == _expected(control)


# --------------------------------------------------------------------------
# 4. The agents are told the object is never wrapped
# --------------------------------------------------------------------------


def test_the_step_and_task_agents_say_the_object_is_never_wrapped():
    for name in ("step.md", "task.md"):
        _, body = _load(_PLUGIN / "agents" / name)
        flat = " ".join(body.split())
        # MUTATION: deleting the sentence from either agent fails here; it is
        # the instruction the review, fix and merge rows of #1763 broke.
        assert ("The object passed to `StructuredOutput` IS `{state, result}`, "
                "never wrapped in another `result`") in flat, name
