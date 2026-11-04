"""A fix loop's re-review is UI-gated by its signed `rendered_review_paths` (FL-WORKER).

docs/design/fix-loop.md §4.3 "The UI rule must still bind it": a
`re-review:<k>` is a lone one-step `collect` workflow pinned to the head fix k
pushed. No dependant stages its `verdict.json` and it stages no patch, so the
rule of #2184 -- read the staged patches' headers and the `builds_on` step's
file list -- finds nothing to gate. swarm-api signs the lane's own changed
paths into the re-review's dispatch block as `rendered_review_paths`, and the
worker reads that list as the patch headers it would otherwise read:

  * a re-review whose list names a UI path, run without a browser, reads
    NOT_YET with the rendered-review reason, and its prompt says so;
  * a list with no UI path (tests under a UI prefix are not UI) keeps MERGE;
  * a list that is present but cannot be read gates the review naming no
    path, as a cut file list does: not told is not seen.

No cloud credentials: the worker runs against the fakes, the mock runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt

MERGE = json.dumps({"verdict": "MERGE", "findings": []})
UI_FILE = "apps/swarm-ui/src/WorkflowViews.tsx"


def _rereview(db: Any, worker_factory: Any, paths: Any) -> dict[str, Any]:
    """Run a lone re-review whose signed dispatch names `paths`, if not None."""
    seed_attempt(db, task_id="task_rr", attempt_id="att_rr", lease_id="lease_rr", task_input={
        "prompt": "re-review the head", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "verdict.json", "artifact_text": MERGE,
    })
    dispatch: dict[str, Any] = {"strategy": "collect"}
    if paths is not None:
        dispatch[verdict_mod.RENDERED_REVIEW_PATHS_KEY] = paths
    # No `expected_outputs`, no `input_from`, no `builds_on`: nothing but the
    # signed list says this review judges a UI change.
    db.doc("tasks/task_rr")["metadata"] = {"dispatch": dispatch}
    worker, _config, _exporter = worker_factory(
        task_id="task_rr", attempt_id="att_rr", lease_id="lease_rr")
    assert worker.run() == ExitCode.OK
    doc = db.doc("tasks/task_rr")
    assert doc["state"] == TaskState.SUCCEEDED.value
    return doc


def _uploaded(store: LocalObjectStore) -> dict[str, Any]:
    key = f"tenants/{TENANT}/tasks/task_rr/attempts/att_rr/artifacts/verdict.json"
    return json.loads(store.download_bytes(key))


def test_a_rereview_with_ui_paths_signed_and_no_browser_reads_not_yet(
    db, store, worker_factory, runner_inputs
):
    doc = _rereview(db, worker_factory, ["apps/scheduler/scheduler/loop.py", UI_FILE])

    uploaded = _uploaded(store)
    # MUTATION: ignore the signed list and the MERGE goes up unrendered.
    assert uploaded["verdict"] == "NOT_YET"
    assert uploaded["findings"][0].startswith(f"{verdict_mod.NOT_REVIEWED_RENDERED}: ")
    record = doc["result_summary"][verdict_mod.RENDERED_REVIEW_KEY]
    assert record["verdict_given"] == "MERGE" and record["verdict"] == "NOT_YET"
    assert record["ui_paths"] == [UI_FILE]
    prompt = runner_inputs[-1]["prompt"]
    assert verdict_mod.RENDERED_REVIEW_PROFILE in prompt and UI_FILE in prompt


def test_a_rereview_whose_signed_paths_touch_no_ui_keeps_its_merge(
    db, store, worker_factory, runner_inputs
):
    doc = _rereview(db, worker_factory, [
        "apps/scheduler/scheduler/loop.py", "apps/swarm-ui/src/__tests__/fixLoop.test.tsx"])

    assert _uploaded(store) == json.loads(MERGE)
    assert verdict_mod.RENDERED_REVIEW_KEY not in doc["result_summary"]
    assert verdict_mod.RENDERED_REVIEW_PROFILE not in runner_inputs[-1]["prompt"]


def test_a_collect_step_with_no_signed_list_is_not_a_review(db, store, worker_factory):
    doc = _rereview(db, worker_factory, None)

    assert _uploaded(store) == json.loads(MERGE)
    assert verdict_mod.RENDERED_REVIEW_KEY not in doc["result_summary"]


def test_an_unreadable_signed_list_gates_the_review_naming_no_path(db, store, worker_factory):
    doc = _rereview(db, worker_factory, "apps/swarm-ui/")

    assert _uploaded(store)["verdict"] == "NOT_YET"
    assert doc["result_summary"][verdict_mod.RENDERED_REVIEW_KEY]["ui_paths"] == []


# ---------------------------------------------------------------------------
# the pure reading
# ---------------------------------------------------------------------------


def test_the_signed_list_is_read_as_patch_headers_are():
    read = verdict_mod.ui_paths_in_dispatch
    key = verdict_mod.RENDERED_REVIEW_PATHS_KEY
    assert read({}) is None
    assert read(None) is None
    assert read({key: ["apps/scheduler/x.py"]}) == ([], False)
    assert read({key: [UI_FILE, "/docs/web-ui/a.html", UI_FILE]}) == (
        [UI_FILE, "docs/web-ui/a.html"], False)
    # An entry that is not a path is a list that cannot say what it covers.
    assert read({key: [UI_FILE, 7]}) == ([UI_FILE], True)
    assert read({key: "apps/swarm-ui/x.tsx"}) == ([], True)
    assert read({key: [None]}) == ([], True)


def test_a_rereview_on_claude_code_with_a_signed_ui_path_reads_not_yet(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    path = artifacts / "verdict.json"
    path.write_text(MERGE)
    paths, unknown = verdict_mod.ui_paths_in_dispatch(
        {verdict_mod.RENDERED_REVIEW_PATHS_KEY: [UI_FILE]}) or ([], False)
    assert paths == [UI_FILE] and unknown is False

    record = verdict_mod.gate_rendered_review(
        path, ui_paths=paths, runner_profile="claude-code", artifacts=artifacts)

    assert record is not None and record["verdict"] == "NOT_YET"
    assert json.loads(path.read_text())["verdict"] == "NOT_YET"
    read = verdict_mod.read_verdict(path, task_id="task_rr", filename="verdict.json")
    assert read.verdict == "NOT_YET"
