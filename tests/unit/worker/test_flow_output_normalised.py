"""The worker normalises a QA flow step's stray tar before the upload (QA-FLOW-CAPTURE, part of #2688).

The owner's pick on PR #3521 (2026-11-06, `worker-normalise`): two legs of the
AI Studio session's QA re-run 8 wrote their batch as flow-1.tar, not
flows-1.tar. The worker, at the end of a flow step whose `<step>.tar` is owed,
renames a lone stray tar to it with a line saying so, so the missing-output
check passes and the flow report stages it; several are refused, named, and the
attempt fails naming the owed tar; any other step's files never move.

The worker cases run the production worker over in-memory Firestore and a local
object store, with the mock runner writing the stray tar. No credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_worker import expected_outputs as expected_mod
from agent_worker import flow_output
from agent_worker.objectstore import LocalObjectStore
from swarm_api import qaspec
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt


def test_the_worker_reads_the_layout_qaspec_states():
    # MUTATION: rename a layout entry in qaspec and the worker reads another name.
    assert flow_output.BATCH_TAR == qaspec.FLOW_LAYOUT["batch_tar"]
    assert flow_output.FLOW_TAR == qaspec.FLOW_LAYOUT["flow_tar"]
    assert flow_output.FLOW_JSON == qaspec.FLOW_LAYOUT["json"]


def _write(root: Path, *names: str) -> None:
    for name in names:
        (root / name).write_bytes(b"x")


def test_a_lone_stray_tar_is_renamed_to_the_owed_one(tmp_path):
    _write(tmp_path, "flow-1.tar", "flow-save.json", "flow-save.tar", "flow-open.json", "flow-open.tar")
    record = flow_output.normalise(tmp_path, "flows-1", ["flows-1.tar"])
    assert record is not None and record["ok"] is True
    assert record["renamed"] == ["flow-1.tar", "flows-1.tar"]
    assert record["lines"] == ["flows-1 packaged its flow output as flow-1.tar, not flows-1.tar: "
                               "renamed to flows-1.tar"]
    assert (tmp_path / "flows-1.tar").exists() and not (tmp_path / "flow-1.tar").exists()
    # A flow's own tar, named by its json, is never read as the batch's.
    assert (tmp_path / "flow-save.tar").exists() and (tmp_path / "flow-open.tar").exists()


def test_several_strays_are_refused_and_left_in_place(tmp_path):
    _write(tmp_path, "flow-1.tar", "batch.tar")
    record = flow_output.normalise(tmp_path, "flows-1", ["flows-1.tar"])
    assert record is not None and record["ok"] is False and record["renamed"] is None
    assert record["lines"] == ["flows-1 wrote batch.tar, flow-1.tar in place of flows-1.tar: refused, "
                               "as the layout names one tar, flows-1.tar"]
    assert not (tmp_path / "flows-1.tar").exists()


def test_no_tar_at_all_is_said(tmp_path):
    _write(tmp_path, "flow-save.json", "flow-save.tar")
    record = flow_output.normalise(tmp_path, "flows-2", ["flows-2.tar"])
    assert record is not None and record["lines"] == ["flows-2 wrote no flows-2.tar"]


def test_a_step_that_wrote_its_tar_or_owes_none_is_untouched(tmp_path):
    _write(tmp_path, "flows-1.tar", "flow-1.tar")
    assert flow_output.normalise(tmp_path, "flows-1", ["flows-1.tar"]) is None
    assert (tmp_path / "flow-1.tar").exists()
    # A step that owes no `<step>.tar`, or is no flow step, keeps its files.
    _write(tmp_path, "other.tar")
    assert flow_output.normalise(tmp_path, "flows-3", ["flows-1.tar"]) is None
    assert flow_output.normalise(tmp_path, "capture-home", ["capture-home.tar"]) is None
    assert flow_output.normalise(tmp_path, None, ["flows-1.tar"]) is None


def test_a_mutating_flows_step_tar_is_its_own(tmp_path):
    _write(tmp_path, "flow-save.json", "flows.tar")
    record = flow_output.normalise(tmp_path, "flow-save", ["flow-save.tar"])
    assert record is not None and record["renamed"] == ["flows.tar", "flow-save.tar"]


def _flow_step(db: Any, worker_factory: Any, artifact_name: str) -> dict[str, Any]:
    seed_attempt(db, task_input={
        "prompt": "run the flows", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": artifact_name, "artifact_text": "a tar",
    })
    db.doc("tasks/task_1")["step_id"] = "flows-1"
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": ["flows-1.tar"]}
    worker, _config, _exporter = worker_factory()
    worker.run()
    return db.doc("tasks/task_1")


def test_the_worker_renames_a_flow_steps_stray_tar_before_the_upload(db, store, worker_factory):
    doc = _flow_step(db, worker_factory, "flow-1.tar")
    # MUTATION: skip `_normalise_flow_output` and the attempt fails for flows-1.tar.
    assert doc["state"] == TaskState.SUCCEEDED.value
    key = f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/flows-1.tar"
    assert isinstance(store, LocalObjectStore) and store.download_bytes(key) == b"a tar"
    record = doc["result_summary"][flow_output.SUMMARY_KEY]
    assert record["renamed"] == ["flow-1.tar", "flows-1.tar"] and record["ok"] is True
    assert not doc["result_summary"].get(expected_mod.MISSING_SUMMARY_KEY)


def test_a_flow_step_with_no_tar_still_fails_naming_the_owed_one(db, store, worker_factory):
    doc = _flow_step(db, worker_factory, "notes.md")
    assert doc["state"] != TaskState.SUCCEEDED.value
    assert "flows-1.tar" in doc["result_summary"][expected_mod.MISSING_SUMMARY_KEY]
    assert doc["result_summary"][flow_output.SUMMARY_KEY]["lines"] == ["flows-1 wrote no flows-1.tar"]
