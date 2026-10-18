"""A terminal write never carries a value Firestore refuses (`agent_worker.storable`).

From 2026-10-16 19:51 UTC every claude-code and indexer step that reached
`ControlPlane.finish` with a summary holding an array inside an array crashed
there: `400 Property result_summary contains an invalid nested entity`, 89
times in two hours. The pre-publish gate records its rounds as a list of lists
(`prepublish.Gate._round`), and the summary carries the runner's output whole
when it is small. The branch had been pushed; the terminal state never landed;
the reconciler reclaimed the attempt as a lost worker; the retry's push was
refused because the first attempt's commit was on the branch.

Pinned here:

    finish() stores a summary whose arrays hold arrays, reshaped, never refused
    fail_retryably() does the same, on both of its targets
    the stored summary keeps every value, in order, one level down
    the reshaping is logged by path and count, never by value
    a summary with nothing to reshape is stored exactly as given
    a too-deep value, a non-string key and an empty key are made storable too
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger
from agent_worker.storable import ITEMS_KEY, MAX_DEPTH, storable
from swarm_common.states import TaskState

from fakes import FakeTransactionRunner
from worker_seeds import TENANT, seed_attempt


def _arrays_in_arrays(value: Any, path: str = "$") -> list[str]:
    """Every path where an array holds an array: what Firestore refuses."""
    found: list[str] = []
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, (list, tuple)):
                found.append(f"{path}[{index}]")
            found.extend(_arrays_in_arrays(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_arrays_in_arrays(item, f"{path}.{key}"))
    return found


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, (list, tuple)):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _control(db: Any, log_stream: Any) -> ControlPlane:
    logger = build_logger(
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=1,
        runner_profile="mock",
        stream=log_stream,
    )
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=1,
        logger=logger,
        txn_runner=FakeTransactionRunner(db),
    )


def _gate_summary(marker: str) -> dict[str, Any]:
    """The shape the 2026-10-16 crashes carried: two rounds of checks, each a
    list, and a hunk range as a pair, under the runner's output."""
    return {
        "runner": {
            "status": "succeeded",
            "output": {
                "prepublish": {
                    "rounds": [
                        [{"name": "changed-guards", "status": "passed", "files": 7}],
                        [
                            {"name": "vitest", "status": "skipped", "reason": marker},
                            {"name": "tsc", "status": "skipped"},
                        ],
                    ],
                    "hunks": {"apps/x.py": [(3, 9), (40, 41)]},
                },
            },
        },
    }


def test_finish_stores_a_summary_whose_arrays_hold_arrays(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db, log_stream)

    control.finish(state=TaskState.SUCCEEDED, exit_code=0,
                   result_summary=_gate_summary("not installed"))

    stored = db.doc("tasks/task_1")
    assert stored["state"] == TaskState.SUCCEEDED.value
    assert _arrays_in_arrays(stored["result_summary"]) == []


def test_the_stored_summary_keeps_every_value_in_order(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db, log_stream)

    control.finish(state=TaskState.SUCCEEDED, exit_code=0,
                   result_summary=_gate_summary("not installed"))

    gate = db.doc("tasks/task_1")["result_summary"]["runner"]["output"]["prepublish"]
    assert [[c["name"] for c in r[ITEMS_KEY]] for r in gate["rounds"]] == [
        ["changed-guards"], ["vitest", "tsc"],
    ]
    assert gate["hunks"]["apps/x.py"] == [{ITEMS_KEY: [3, 9]}, {ITEMS_KEY: [40, 41]}]


def test_fail_retryably_stores_it_too_on_both_targets(db, log_stream):
    for attempts, target in ((1, TaskState.READY), (3, TaskState.FAILED)):
        seed_attempt(db, state=TaskState.RUNNING, attempt_count=attempts)
        control = _control(db, log_stream)

        ended = control.fail_retryably(exit_code=1, error="push refused", cause="runner_error",
                                       result_summary=_gate_summary("x"))

        assert ended is target
        assert _arrays_in_arrays(db.doc("tasks/task_1")["result_summary"]) == []


def test_the_reshaping_is_logged_by_path_never_by_value(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db, log_stream)
    marker = "marker-" + secrets.token_hex(8)

    control.finish(state=TaskState.SUCCEEDED, exit_code=0, result_summary=_gate_summary(marker))

    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    reshaped = [line for line in lines if "reshaped" in line.get("message", "")]
    assert len(reshaped) == 1
    assert reshaped[0]["rewritten"] == 4
    assert "$.runner.output.prepublish.rounds[0]" in reshaped[0]["paths"]
    assert marker not in log_stream.getvalue()


def test_a_summary_with_nothing_to_reshape_is_stored_as_given(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db, log_stream)
    summary = {"git": {"files": [{"path": "a.py", "insertions": 3}], "commits": 1}, "ok": True}

    control.finish(state=TaskState.SUCCEEDED, exit_code=0, result_summary=summary)

    assert db.doc("tasks/task_1")["result_summary"] == summary
    assert "reshaped" not in log_stream.getvalue()
    stored, rewritten, paths = storable(summary)
    assert stored is summary and rewritten == 0 and paths == []


def test_too_deep_values_and_unstorable_keys_are_made_storable():
    deep: Any = "leaf"
    for _ in range(MAX_DEPTH + 6):
        deep = {"d": deep}
    summary = {"deep": deep, "keys": {1: "one", "": "empty", "__name__": "reserved"}}

    stored, rewritten, _ = storable(summary)

    assert _depth(stored) <= MAX_DEPTH + 1
    assert set(stored["keys"]) == {"1", "key:", "key:__name__"}
    assert rewritten == 4
