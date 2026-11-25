# source-text: contract - the frozen states.py transition comment for PARKED -> SUCCEEDED must carry the exact text of contract request 157, which has no behaviour to observe
"""A re-review whose verdict gate is recorded shut is ended by the scheduler (contract request 157).

Accepted by the owner 2026-11-24. In implement -> review -> fix -> re-review ->
merge, a first review's MERGE (or anything outside the gate's `verdict_in`)
shuts the re-review's gate: its worker runs no agent, re-publishes the staged
verdict and ends SUCCEEDED. For that copy it was promoted, admitted (every
pool), leased, dispatched and started. The scheduler already reads the shut
gate before any lease (`runtime.republishes`); with request 157 it ends the
step PARKED -> SUCCEEDED in one guarded transaction instead, with no lease,
no capacity and no pod, and releases its dependants (the merge step), which
stage the same verdict bytes the worker would have re-published.

What is held here:

    the frozen PARKED row's comment is request 157's (that the request is ACCEPTED
        is docs/contract-change-requests.md's record; a unit test reads no docs, issue 71)
    a shut gate ends the re-review SUCCEEDED: no lease, no dispatch, the
        worker's verdict record, one `succeeded` event, and the merge step
        released and admitted in the same finish event
    the merge step's staging finds the first review's verdict through the
        re-review's manifest entry (`agent_worker.inputs.artifact_reference`)
    a second drain writes nothing twice
    an open gate is dispatched as today
    a gate recorded for another review, or by a step the re-review does not
        build on, does not count
    a task that moved, or a gate record that changed, between the read and
        the write is left alone
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker.inputs import InputUnavailable
from scheduler import store as store_mod

from .conftest import seed_pool, seed_task, seed_tenant

REPO = Path(__file__).resolve().parents[3]
BROWSER = "claude-code-browser"
CODE = "claude-code"
POLICY = {"policy": "browser_if_ui", "browser": BROWSER, "code": CODE}
GATE = {"task_id": "task_review", "verdict_in": ["NOT_YET"]}
VERDICT_FILE = "review.json"
REVIEW_URI = (f"gs://swarm-artifacts-example-project/tenants/eng/tasks/task_review/attempts/att_r1/"
              f"artifacts/{VERDICT_FILE}")


def _alternate() -> dict[str, Any]:
    return {
        "runner_profile": CODE, "resource_class": "standard", "timeout_seconds": 3600,
        "provider": "anthropic", "model": None, "model_class": None,
        "spec_signature": "c2lnbmVkLWNvZGU=", "spec_key_version": "kv/1", "spec_format": 1,
    }


def _record(verdict: str, *, agent_ran: bool, task_id: str = "task_review",
            **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "file": VERDICT_FILE, "verdict": verdict,
            "verdict_in": ["NOT_YET"], "agent_ran": agent_ran,
            "findings": [{"severity": "minor", "text": "a nit"}], "findings_dropped": 0,
            **extra}


def _lane(db, *, integrator_record: dict[str, Any] | None,
          builds_on: str = "task_integrator") -> dict[str, Any]:
    """review and integrator SUCCEEDED, the re-review PARKED on both, the merge step PARKED on it."""
    seed_tenant(db, "eng", max_active=10, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=10)
    review = seed_task(db, task_id="task_review", tenant_id="eng", state="SUCCEEDED",
                       runner_profile=BROWSER, workflow_id="wf_1")
    review["result_summary"] = {
        "git": {"files": [], "files_truncated": False},
        "verdict": "MERGE",
        "artifacts": [{"name": VERDICT_FILE, "uri": REVIEW_URI, "bytes": 211}],
    }
    integrator = seed_task(db, task_id="task_integrator", tenant_id="eng", state="SUCCEEDED",
                           runner_profile=CODE, workflow_id="wf_1")
    integrator["result_summary"] = {
        "git": {"files": [{"path": "apps/swarm-ui/src/App.tsx", "status": "M"}],
                "files_truncated": False},
    }
    if integrator_record is not None:
        integrator["result_summary"]["verdict_gate"] = integrator_record
        if not integrator_record.get("agent_ran", True):
            integrator["result_summary"]["skipped_agent"] = (
                f"review verdict {integrator_record['verdict']}")
    rereview = seed_task(db, task_id="task_rereview", tenant_id="eng", state="PARKED",
                         park_reason="DEPENDENCY_INCOMPLETE", runner_profile=BROWSER,
                         resource_class="browser", provider="anthropic",
                         depends_on=("task_integrator", "task_review"), workflow_id="wf_1")
    rereview["spec_signature"] = "c2lnbmVkLWJyb3dzZXI="
    rereview["spec_key_version"] = "kv/1"
    rereview["spec_format"] = 1
    rereview["metadata"] = {
        "runtime": {**POLICY, "alternate": _alternate(),
                    "inherit": {"step": "review", "task_id": "task_review"}},
        "dispatch": {"builds_on": builds_on, "verdict_gate": dict(GATE)},
    }
    seed_task(db, task_id="task_merge", tenant_id="eng", state="PARKED",
              park_reason="DEPENDENCY_INCOMPLETE", runner_profile="mock",
              depends_on=("task_rereview",), workflow_id="wf_1")
    return rereview


def _events(db, task_id: str, kind: str) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for key, doc in db.docs.items()
            if key.startswith(prefix) and doc["type"] == kind]


def _leases_for(db, task_id: str) -> list[dict[str, Any]]:
    return [d for k, d in db.docs.items() if k.startswith("leases/") and d.get("task_id") == task_id]


# -- the frozen change and its record ------------------------------------------


def test_the_parked_row_comment_is_request_157s():
    text = (REPO / "apps/common/swarm_common/states.py").read_text()
    block = (
        "    # PARKED -> SUCCEEDED: a step the control plane finishes without a worker\n"
        "    # (#748, contract request 52). It held no lease, so there is nothing to\n"
        "    # release. Written by swarm-api, guarded on its own claim, and by the\n"
        "    # scheduler for a re-review whose verdict gate the platform's own record\n"
        "    # shows shut, in the same guarded write that would otherwise promote it\n"
        "    # (contract request 157).\n"
    )
    # MUTATION: keep "only swarm-api writes it" and the scheduler's write
    # contradicts the frozen text.
    assert block in text
    assert "only swarm-api writes it" not in text


# -- the scheduler half ------------------------------------------------------


def test_a_shut_gate_re_review_is_ended_with_no_lease_and_no_pod(db, make_scheduler, dispatcher):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    # MUTATION: promote it READY as before and it is leased and dispatched.
    assert doc["state"] == "SUCCEEDED"
    assert doc["park_reason"] is None and doc["completed_at"] is not None
    assert doc["current_lease_id"] is None and doc["attempt_count"] == 0
    # Invariants 1-3: never admitted, so no lease and no pod for it.
    assert _leases_for(db, "task_rereview") == []
    assert "task_rereview" not in [r["task_id"] for r in dispatcher.dispatched]
    summary = doc["result_summary"]
    assert summary["published_by"] == "control_plane"
    assert summary["skipped_agent"] == "review verdict MERGE"
    assert summary["verdict_gate"] == {
        "task_id": "task_review", "file": VERDICT_FILE, "verdict": "MERGE",
        "verdict_in": ["NOT_YET"], "agent_ran": False,
        "findings": [{"severity": "minor", "text": "a nit"}], "findings_dropped": 0,
    }
    (entry,) = summary["artifacts"]
    assert entry["name"] == VERDICT_FILE and entry["uri"] == REVIEW_URI
    assert entry["bytes"] == 211
    assert entry["written_by"] == "control_plane"
    assert entry["republished_from"] == "task_review"
    (event,) = _events(db, "task_rereview", "succeeded")
    assert event["detail"]["published_by"] == "control_plane"
    assert _events(db, "task_rereview", "ready") == []


def test_its_dependants_become_eligible_in_the_same_finish_event(db, make_scheduler, dispatcher):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))

    make_scheduler().release_dependants("task_integrator")

    merge = db.docs["tasks/task_merge"]
    # MUTATION: no `_note_ended` and the merge step waits for the next drain.
    assert merge["state"] != "PARKED", merge.get("park_reason")
    assert [r["task_id"] for r in dispatcher.dispatched] == ["task_merge"]


def test_the_merge_step_stages_the_reviews_verdict_through_the_re_review(db, make_scheduler):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))
    make_scheduler().release_dependants("task_integrator")
    rereview = dict(db.docs["tasks/task_rereview"])

    located = inputs_mod.artifact_reference(
        rereview, tenant_id="eng", upstream_task_id="task_rereview", filename=VERDICT_FILE)

    # The first review's own object: the bytes the worker would have copied.
    assert located.key == f"tenants/eng/tasks/task_review/attempts/att_r1/artifacts/{VERDICT_FILE}"
    assert located.size_bytes == 211 and located.inline is None


def test_a_size_skipped_reviews_inline_verdict_is_passed_on_inline(db, make_scheduler):
    _lane(db, integrator_record=_record("SKIPPED", agent_ran=False, gate_reading="MERGE",
                                        skipped_review="diff under the size gate"))
    review = db.docs["tasks/task_review"]
    review["result_summary"] = {
        "review_skipped": "diff under the size gate",
        "artifacts": [{"name": "verdict.json", "inline": '{"verdict": "SKIPPED"}',
                       "written_by": "control_plane"}],
    }
    db.docs["tasks/task_integrator"]["result_summary"]["verdict_gate"]["file"] = "verdict.json"

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    assert doc["state"] == "SUCCEEDED"
    assert doc["result_summary"]["verdict_gate"]["gate_reading"] == "MERGE"
    assert doc["result_summary"]["skipped_agent"] == "review verdict SKIPPED"
    located = inputs_mod.artifact_reference(
        dict(doc), tenant_id="eng", upstream_task_id="task_rereview", filename="verdict.json")
    assert located.inline == b'{"verdict": "SKIPPED"}'


def test_a_second_drain_writes_nothing_twice(db, make_scheduler):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))
    scheduler = make_scheduler()
    scheduler.release_dependants("task_integrator")
    scheduler.release_dependants("task_integrator")
    make_scheduler().drain()

    assert len(_events(db, "task_rereview", "succeeded")) == 1
    assert _leases_for(db, "task_rereview") == []


def test_an_open_gate_is_dispatched_as_today(db, make_scheduler, dispatcher):
    _lane(db, integrator_record=_record("NOT_YET", agent_ran=True))

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    assert doc["state"] != "SUCCEEDED"
    assert len(_leases_for(db, "task_rereview")) == 1
    assert [r["task_id"] for r in dispatcher.dispatched] == ["task_rereview"]
    assert db.docs["tasks/task_merge"]["state"] == "PARKED"


@pytest.mark.parametrize("record, builds_on", [
    (_record("MERGE", agent_ran=False, task_id="task_other_review"), "task_integrator"),
    (_record("MERGE", agent_ran=False), "task_review"),
], ids=["another-review", "not-the-step-it-builds-on"])
def test_a_gate_recorded_for_another_step_does_not_count(db, make_scheduler, dispatcher,
                                                         record, builds_on):
    _lane(db, integrator_record=record, builds_on=builds_on)

    make_scheduler().release_dependants("task_integrator")

    assert db.docs["tasks/task_rereview"]["state"] != "SUCCEEDED"
    assert _events(db, "task_rereview", "succeeded") == []
    assert [r["task_id"] for r in dispatcher.dispatched] == ["task_rereview"]


def _between_read_and_write(monkeypatch, change):
    """Run `change` once, after the drain decided the gate is shut and before
    the guarded transaction re-reads."""
    real = store_mod.SchedulerStore.end_shut_gate_rereview
    calls = {"n": 0}

    def wrapped(self, *args, **kwargs):
        if calls["n"] == 0:
            change()
        calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(store_mod.SchedulerStore, "end_shut_gate_rereview", wrapped)
    return calls


@pytest.mark.parametrize("field, value", [
    ("state", "CANCELLED"),
    ("park_reason", "MANUAL_PAUSE"),
    ("current_generation", 4),
], ids=["cancelled", "parked-again-for-another-reason", "new-generation"])
def test_a_task_that_moved_between_read_and_write_is_left_alone(
        db, make_scheduler, dispatcher, monkeypatch, field, value):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))

    def change() -> None:
        db.docs["tasks/task_rereview"][field] = value

    calls = _between_read_and_write(monkeypatch, change)
    make_scheduler().release_dependants("task_integrator")

    assert calls["n"] == 1
    doc = db.docs["tasks/task_rereview"]
    # MUTATION: drop the guard and a stale read ends a moved task SUCCEEDED.
    assert doc["state"] != "SUCCEEDED" and doc["result_summary"] is None
    assert _events(db, "task_rereview", "succeeded") == []
    assert _leases_for(db, "task_rereview") == []
    assert db.docs["tasks/task_merge"]["state"] == "PARKED"


def test_a_gate_record_that_changed_between_read_and_write_is_left_alone(
        db, make_scheduler, monkeypatch):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))

    def change() -> None:
        db.docs["tasks/task_integrator"]["result_summary"]["verdict_gate"] = _record(
            "NOT_YET", agent_ran=True)

    calls = _between_read_and_write(monkeypatch, change)
    make_scheduler().release_dependants("task_integrator")

    assert calls["n"] == 1
    # MUTATION: trust the pre-transaction read and this re-review is ended
    # on a verdict the record no longer holds.
    assert db.docs["tasks/task_rereview"]["state"] != "SUCCEEDED"
    assert _events(db, "task_rereview", "succeeded") == []


def test_a_review_with_no_verdict_file_to_point_at_is_dispatched_as_today(
        db, make_scheduler, dispatcher):
    _lane(db, integrator_record=_record("MERGE", agent_ran=False))
    db.docs["tasks/task_review"]["result_summary"]["artifacts"] = []

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    assert doc["state"] != "SUCCEEDED"
    assert doc["runner_profile"] == CODE
    assert [r["task_id"] for r in dispatcher.dispatched] == ["task_rereview"]


# -- the worker staging half --------------------------------------------------


def _ended_rereview(**overrides: Any) -> dict[str, Any]:
    entry = {"name": VERDICT_FILE, "uri": REVIEW_URI, "bytes": 211,
             "written_by": "control_plane", "republished_from": "task_review"}
    entry.update(overrides.pop("entry", {}))
    doc: dict[str, Any] = {
        "id": "task_rereview", "tenant_id": "eng", "state": "SUCCEEDED",
        "depends_on": ["task_integrator", "task_review"],
        "metadata": {"dispatch": {"builds_on": "task_integrator", "verdict_gate": dict(GATE)}},
        "result_summary": {
            "published_by": "control_plane",
            "verdict_gate": _record("MERGE", agent_ran=False),
            "skipped_agent": "review verdict MERGE",
            "artifacts": [entry],
        },
    }
    for key, value in overrides.items():
        doc[key] = value
    return doc


def test_the_pointer_resolves_only_to_the_gates_review():
    located = inputs_mod.artifact_reference(
        _ended_rereview(), tenant_id="eng", upstream_task_id="task_rereview",
        filename=VERDICT_FILE)
    assert located.key.startswith("tenants/eng/tasks/task_review/")


@pytest.mark.parametrize("doc", [
    _ended_rereview(entry={"republished_from": "task_integrator"}),
    _ended_rereview(entry={"written_by": "worker"}),
    _ended_rereview(depends_on=["task_integrator"]),
    _ended_rereview(metadata={"dispatch": {"verdict_gate": {"task_id": "task_x",
                                                            "verdict_in": ["NOT_YET"]}}}),
    _ended_rereview(result_summary={
        "published_by": "control_plane", "verdict_gate": _record("MERGE", agent_ran=True),
        "artifacts": [{"name": VERDICT_FILE, "uri": REVIEW_URI, "bytes": 211,
                       "written_by": "control_plane", "republished_from": "task_review"}]}),
], ids=["not-the-gates-review", "not-the-control-plane", "not-a-parent",
        "another-gate", "agent-ran"])
def test_any_other_pointer_is_refused(doc):
    # MUTATION: accept any `republished_from` and a tenant-writable document
    # points the merge step at any of its tenant's artifacts.
    with pytest.raises(InputUnavailable):
        inputs_mod.artifact_reference(doc, tenant_id="eng", upstream_task_id="task_rereview",
                                      filename=VERDICT_FILE)


def test_a_pointer_into_another_tenant_is_refused():
    doc = _ended_rereview(entry={"uri": REVIEW_URI.replace("tenants/eng/", "tenants/ops/")})
    with pytest.raises(InputUnavailable):
        inputs_mod.artifact_reference(doc, tenant_id="eng", upstream_task_id="task_rereview",
                                      filename=VERDICT_FILE)


def test_the_restated_names_are_the_workers():
    from scheduler import runtime as runtime_mod

    assert runtime_mod.ARTIFACT_WRITER == inputs_mod.INLINE_WRITER
    assert runtime_mod.REPUBLISHED_FROM_KEY == inputs_mod.REPUBLISHED_FROM_KEY
    assert runtime_mod.INLINE_ARTIFACT_KEY == inputs_mod.INLINE_ARTIFACT_KEY
    assert runtime_mod.PUBLISHED_BY_CONTROL_PLANE == inputs_mod.PUBLISHED_BY_CONTROL_PLANE
