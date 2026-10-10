"""A shut verdict gate re-publishes the verdict it staged (merge chain follow-up, part of #295).

Owner decision, 2026-10-10. `metadata.merge` "on_merge_verdict" appends a
re-review gated on the first review's NOT_YET (swarm-api,
`validation.rereview_step_for`), and a merge step that reads the re-review's
`verdict.json`. On MERGE the re-review runs no agent, so the worker passes the
verdict it staged on as its own artifact, byte for byte
(`lifecycle.Worker._republish_staged_verdict`); the merge then reads the first
review's MERGE through it. On NOT_YET the fix and the re-review run their
agents, and the merge reads the re-review's own verdict.

Each world here runs real worker attempts on the mock runner, in order, and
then the worker's real merge action on the artifact the re-review uploaded:

  * MERGE: neither the fix's nor the re-review's runner starts, and the pull
    request is merged;
  * NOT_YET: the fix's agent runs, then the re-review's, and the merge merges
    or stops on what the re-review wrote;
  * the re-published file is the staged one, byte for byte;
  * a gated step that staged no verdict still fails, publishing nothing.

No credentials, no network: Firestore, the object store and GitHub are fakes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from merge_world import PINNED, MergeWorld
from test_verdict_gate import upstream_verdict
from worker_seeds import seed_attempt

#: The first review's verdict, deliberately NOT in the shape `json.dumps`
#: writes -- key order, spacing, a non-ASCII finding, no trailing newline --
#: so a re-serialised file could not pass for the staged one.
FIRST_MERGE = '{ "findings" : ["naïve fix is fine"],\n  "verdict":"MERGE" }'
FIRST_NOT_YET = json.dumps({"verdict": "NOT_YET", "findings": ["the guard checks the wrong field"]})


def _seed_gated_step(
    db: Any, n: int, *, artifact: tuple[str, str] | None = None, stage: bool = True,
) -> str:
    """`task_<n>` stages `verdict.json` from task_up and is gated on its NOT_YET.

    `artifact` is what its mock agent writes, when the agent runs."""
    task_id = f"task_{n}"
    task_input: dict[str, Any] = {"prompt": "after the review", "steps": 1, "sleep_seconds": 0.01}
    if artifact is not None:
        task_input["artifact_name"], task_input["artifact_text"] = artifact
    seed_attempt(db, task_id=task_id, attempt_id=f"att_{n}", lease_id=f"lease_{n}",
                 task_input=task_input)
    db.doc(f"tasks/{task_id}")["metadata"] = {
        "input_from": {"task_up": "verdict.json"} if stage else {},
        "dispatch": {
            "strategy": "collect",
            "carrier": "checkpoints",
            "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"]},
        },
    }
    return task_id


def _run(worker_factory: Any, monkeypatch: Any, n: int, *, agent_runs: bool) -> tuple[int, bool]:
    """Run `task_<n>`'s attempt; `(exit code, whether its runner started)`."""
    worker, _config, _exporter = worker_factory(
        task_id=f"task_{n}", attempt_id=f"att_{n}", lease_id=f"lease_{n}"
    )
    started: list[bool] = []
    real = worker._run_child_supervised

    def runner(*args: Any, **kwargs: Any):
        started.append(True)
        if not agent_runs:
            raise AssertionError("the runner was started behind a shut verdict gate")
        return real(*args, **kwargs)

    monkeypatch.setattr(worker, "_run_child_supervised", runner)
    return worker.run(), bool(started)


def _artifact(db: Any, task_id: str, name: str = "verdict.json") -> bytes | None:
    """The bytes `task_id` uploaded as `name`, None when it uploaded none."""
    summary = db.doc(f"tasks/{task_id}").get("result_summary") or {}
    for entry in summary.get("artifacts") or ():
        if entry.get("name") == name:
            uri = entry["uri"]
            assert uri.startswith("file://"), uri
            return Path(uri[len("file://"):]).read_bytes()
    return None


def _merge_on(tmp_path: Path, verdict_bytes: bytes):
    """The worker's merge action, reading `verdict_bytes` as the re-review's verdict."""
    (tmp_path / "forge").mkdir()
    world = MergeWorld(tmp_path / "forge")
    context = world.context()
    # `context()` stages its own `verdict`; the re-review's upload replaces
    # it, as the merge step's staging of that artifact would.
    world.verdict_path.write_bytes(verdict_bytes)
    return world, merge.run_merge(context)


def test_on_merge_no_fix_and_no_rereview_agent_runs_and_the_pull_request_merges(
    db, worker_factory, monkeypatch, tmp_path
):
    upstream_verdict(db, worker_factory, FIRST_MERGE)
    fix = _seed_gated_step(db, 3)
    assert _run(worker_factory, monkeypatch, 3, agent_runs=False) == (ExitCode.OK, False)
    rereview = _seed_gated_step(db, 4, artifact=("verdict.json", '{"verdict": "NOT_YET"}'))
    assert _run(worker_factory, monkeypatch, 4, agent_runs=False) == (ExitCode.OK, False)

    task = db.doc(f"tasks/{rereview}")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["result_summary"]["verdict_gate"]["agent_ran"] is False
    assert db.doc(f"tasks/{fix}")["result_summary"]["verdict_gate"]["agent_ran"] is False
    passed_on = _artifact(db, rereview)
    assert passed_on is not None, "the shut gate published no verdict for the merge to read"

    world, outcome = _merge_on(tmp_path, passed_on)
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED


def test_the_passed_on_verdict_is_the_staged_file_byte_for_byte(db, worker_factory, monkeypatch):
    upstream_verdict(db, worker_factory, FIRST_MERGE)
    rereview = _seed_gated_step(db, 4)
    assert _run(worker_factory, monkeypatch, 4, agent_runs=False) == (ExitCode.OK, False)

    staged_from = _artifact(db, "task_up")
    passed_on = _artifact(db, rereview)
    assert staged_from == FIRST_MERGE.encode("utf-8")
    assert passed_on == staged_from
    # And it is what this step READ: the digest its own worker measured
    # when it staged the file.
    (staged,) = db.doc(f"tasks/{rereview}")["result_summary"]["staged_inputs"]
    assert staged["sha256"] == hashlib.sha256(passed_on).hexdigest()


def test_on_not_yet_the_fix_then_the_rereview_run_and_the_merge_follows_the_rereview(
    db, worker_factory, monkeypatch, tmp_path
):
    upstream_verdict(db, worker_factory, FIRST_NOT_YET)
    fix = _seed_gated_step(db, 3)
    assert _run(worker_factory, monkeypatch, 3, agent_runs=True) == (ExitCode.OK, True)
    assert db.doc(f"tasks/{fix}")["result_summary"]["verdict_gate"]["agent_ran"] is True

    own = json.dumps({"verdict": "MERGE", "findings": []})
    rereview = _seed_gated_step(db, 4, artifact=("verdict.json", own))
    assert _run(worker_factory, monkeypatch, 4, agent_runs=True) == (ExitCode.OK, True)
    # The re-review's OWN verdict, not the first review's passed on.
    assert _artifact(db, rereview) == own.encode("utf-8")

    world, outcome = _merge_on(tmp_path, own.encode("utf-8"))
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_on_not_yet_a_rereview_that_still_says_not_yet_stops_the_merge(
    db, worker_factory, monkeypatch, tmp_path
):
    upstream_verdict(db, worker_factory, FIRST_NOT_YET)
    _seed_gated_step(db, 3)
    assert _run(worker_factory, monkeypatch, 3, agent_runs=True) == (ExitCode.OK, True)
    still = json.dumps({"verdict": "NOT_YET", "findings": ["still broken"]})
    rereview = _seed_gated_step(db, 4, artifact=("verdict.json", still))
    assert _run(worker_factory, monkeypatch, 4, agent_runs=True) == (ExitCode.OK, True)

    world, outcome = _merge_on(tmp_path, _artifact(db, rereview) or b"")
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert world.merge_calls() == []


def test_a_gated_step_that_staged_no_verdict_still_fails_and_passes_nothing_on(
    db, worker_factory, monkeypatch
):
    upstream_verdict(db, worker_factory, FIRST_MERGE)
    rereview = _seed_gated_step(db, 4, stage=False)
    assert _run(worker_factory, monkeypatch, 4, agent_runs=False) == (ExitCode.FAILED, False)

    task = db.doc(f"tasks/{rereview}")
    assert task["state"] == TaskState.FAILED.value
    assert "task_up" in task["last_error"]
    assert _artifact(db, rereview) is None
