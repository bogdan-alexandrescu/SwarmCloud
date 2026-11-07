"""One copy of the QA flow step's stray-tar rule, read by swarm-api and the worker (part of #2688).

PR #3521 wrote the rule twice: `swarm_api.qaspec.check_flow_output` (no
production caller) and the worker's `agent_worker.flow_output.normalise`, which
restated the layout because the worker image does not ship swarm-api. The owner's
pick on 6 November 2026 is the worker-side normaliser, with ONE copy of the
rule: `swarm_rollup.flowlayout.pick_flow_tar`, in the package both images build.

One case table runs through the rule, the qaspec check and the worker's
normaliser: a lone stray tar is taken as `<step>.tar` with one line naming
both; two are refused, each named; none is refused naming the owed tar; a
correctly named tar is untouched. The worker records the line on the step: its
log, `result_summary` and a RUNNING event with cause `flow_output`.
No credentials, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import flow_output
from swarm_api import qaspec
from swarm_common.states import EventType, TaskState
from swarm_rollup import flowlayout

from worker_seeds import seed_attempt

#: (case, files the step wrote, flow ids, tar read, renamed, line, ok)
CASES = [
    ("one stray", ["flow-1.tar", "flow-a.json", "flow-a.tar"], ["a"], "flow-1.tar",
     ("flow-1.tar", "flows-1.tar"),
     "flows-1 packaged its flow output as flow-1.tar, not flows-1.tar: read as flows-1.tar", True),
    ("two strays", ["flow-1.tar", "batch.tar", "flow-a.json"], ["a"], None, None,
     "flows-1 wrote batch.tar, flow-1.tar in place of flows-1.tar: refused, as the layout names one tar, "
     "flows-1.tar", False),
    ("no tar", ["notes.md", "flow-a.json", "flow-a.tar"], ["a"], None, None, "flows-1 wrote no flows-1.tar", False),
    ("named right", ["flows-1.tar", "flow-1.tar", "flow-a.json"], ["a"], "flows-1.tar", None, None, True),
]
IDS = [case[0] for case in CASES]


@pytest.mark.parametrize(("case", "files", "flow_ids", "tar", "renamed", "line", "ok"), CASES, ids=IDS)
def test_the_shared_rule(case, files, flow_ids, tar, renamed, line, ok):
    own = {flowlayout.FLOW_TAR.format(id=fid) for fid in flow_ids}
    pick = flowlayout.pick_flow_tar("flows-1", files, own)
    assert pick.expected == "flows-1.tar"
    assert (pick.tar, pick.renamed, pick.line, pick.ok) == (tar, renamed, line, ok)


@pytest.mark.parametrize(("case", "files", "flow_ids", "tar", "renamed", "line", "ok"), CASES, ids=IDS)
def test_qaspec_reads_the_table_by_the_shared_rule(case, files, flow_ids, tar, renamed, line, ok):
    check = qaspec.check_flow_output("flows-1", flow_ids, files=files, documents={"a": {"flow": "a", "runs": []}})
    assert (check.tar, check.renamed, check.ok) == (tar, renamed, ok)
    assert check.lines == ([line] if line else [])


@pytest.mark.parametrize(("case", "files", "flow_ids", "tar", "renamed", "line", "ok"), CASES, ids=IDS)
def test_the_worker_normalises_the_table_by_the_shared_rule(tmp_path, case, files, flow_ids, tar, renamed,
                                                            line, ok):
    for name in files:
        (tmp_path / name).write_bytes(name.encode())
    record = flow_output.normalise(tmp_path, "flows-1", ["flows-1.tar"])
    if line is None:
        # A correctly named tar is untouched, and so is every other file.
        assert record is None
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(files)
        return
    assert record is not None
    assert (record["ok"], record["renamed"], record["lines"]) == (ok, list(renamed) if renamed else None, [line])
    if renamed:
        assert (tmp_path / "flows-1.tar").read_bytes() == renamed[0].encode()
        assert not (tmp_path / renamed[0]).exists()
    else:
        # Refused: nothing moves, and no `flows-1.tar` appears.
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(files)


def test_both_callers_go_through_the_one_rule(tmp_path, monkeypatch):
    # MUTATION: restate the rule in qaspec or the worker and this sees no call.
    seen: list[str] = []
    real = flowlayout.pick_flow_tar

    def spy(step_id: str, files: Any, own: Any) -> flowlayout.FlowTarPick:
        seen.append(step_id)
        return real(step_id, files, own)

    monkeypatch.setattr(flowlayout, "pick_flow_tar", spy)
    qaspec.check_flow_output("flows-1", [], files=["flow-1.tar"], documents={})
    (tmp_path / "flow-1.tar").write_bytes(b"x")
    flow_output.normalise(tmp_path, "flows-2", ["flows-2.tar"])
    assert seen == ["flows-1", "flows-2"]


def test_the_layout_names_are_stated_once():
    assert qaspec.FLOW_LAYOUT["batch_tar"] is flowlayout.BATCH_TAR
    assert qaspec.FLOW_LAYOUT["flow_tar"] is flowlayout.FLOW_TAR
    assert qaspec.FLOW_LAYOUT["json"] is flowlayout.FLOW_JSON


def _flow_events(db: Any) -> list[dict[str, Any]]:
    return [event for event in db.events("task_1")
            if (event.get("detail") or {}).get("cause") == flow_output.EVENT_CAUSE]


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


def test_the_worker_records_the_rename_line_as_an_event(db, store, worker_factory):
    doc = _flow_step(db, worker_factory, "flow-1.tar")
    assert doc["state"] == TaskState.SUCCEEDED.value
    line = "flows-1 packaged its flow output as flow-1.tar, not flows-1.tar: read as flows-1.tar"
    assert doc["result_summary"][flow_output.SUMMARY_KEY]["lines"] == [line]
    (event,) = _flow_events(db)
    assert event["type"] == EventType.RUNNING.value
    assert event["detail"]["lines"] == [line]
    assert event["detail"]["renamed"] == ["flow-1.tar", "flows-1.tar"] and event["detail"]["ok"] is True


def test_a_refused_flow_step_names_the_owed_tar_in_its_event(db, store, worker_factory):
    doc = _flow_step(db, worker_factory, "notes.md")
    assert doc["state"] != TaskState.SUCCEEDED.value
    (event,) = _flow_events(db)
    assert event["detail"]["lines"] == ["flows-1 wrote no flows-1.tar"] and event["detail"]["ok"] is False


def test_a_correctly_named_flow_step_records_no_event(db, store, worker_factory):
    doc = _flow_step(db, worker_factory, "flows-1.tar")
    assert doc["state"] == TaskState.SUCCEEDED.value
    assert _flow_events(db) == []
    assert flow_output.SUMMARY_KEY not in doc["result_summary"]
