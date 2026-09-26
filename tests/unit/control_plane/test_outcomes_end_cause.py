"""Why a task ended, as the outcome ledger reads it: the typed cause first (#185).

The owner's decisions of 2026-09-25 on #185 (the decisions comment), as this
file pins them:

  * 9 -- contract requests 23 and 24 are ACCEPTED. `Task.end_cause` is typed in
    the frozen contract and written by every terminal writer
    (tests/unit/control_plane/test_end_cause_writers.py and
    tests/unit/worker/test_end_cause_*.py hold the writers); `swarm_api.
    outcomes` classifies by it and falls back to the `last_error` prefix
    classifier ONLY for a task without one. `RunnerProfile.cost_declared` says
    which runners declare their cost, and the ledger stops naming `mock`.
  * 2 -- a cancel cascade is split: "after a cancel" is not "after a failure".
    A task without a cause is split at derive time by what each parent's OWN
    END sent down -- not its state: a CANCELLED parent may itself be a
    failure's cascade, so the split follows the chain up (the review of #217).
  * 4 -- the worker refusing to stage a declared input (`InputUnavailable`) is
    its own class, `inputs_unavailable`, not a runner error.

And the review's findings on #196, fixed here: the classifier's version was
stored on every day and never read, the 15-minute seal grace had no test, and
`workflows_failed` served 0 rather than null when it does not apply.

WRITTEN TO FAIL AGAINST THE CODE BEFORE THE FIX, ON ITS ASSERTIONS. Nothing new
is imported at module level -- `EndCause`, the new keyword arguments and the new
maps are reached through the module at test time -- so on the old code every
case below runs and fails on what it asserts, rather than the file failing to
import. The seal-grace case is a PIN of behaviour that was already right; it
cannot go red here, and the pull request proves it by a mutation instead.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_api import outcomes
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_common import models
from swarm_common.profiles import RUNNER_PROFILES

from .test_outcomes_route import NOW, WEEK, Clock, _context, at, attempt, ok, seed_week, task

UTC = timezone.utc
CASCADE_TEXT = "an upstream workflow step did not succeed"


# --------------------------------------------------------------------------
# The frozen contract carries the two accepted requests
# --------------------------------------------------------------------------

#: Contract request 23's values, reconciled with the owner's decisions: the
#: request's ten, plus INPUTS_UNAVAILABLE (decision 4).
END_CAUSES = [
    "timeout",
    "cannot_start",
    "lost_worker",
    "outputs_missing",
    "inputs_unavailable",
    "dispatch_failed",
    "runner_error",
    "cancel_requested",
    "failed_parent",
    "cancelled_parent",
    "workflow_sweep",
]


def _task(**extra):
    now = datetime(2026, 9, 25, tzinfo=UTC)
    return models.Task(
        id="task_1", tenant_id="eng", created_at=now, updated_at=now,
        state=models.TaskState.FAILED, runner_profile="mock", resource_class="standard",
        input={}, submitted_by="alice@saga.xyz", **extra,
    )


def test_end_cause_is_typed_in_the_frozen_contract():
    end_cause = getattr(models, "EndCause", None)
    assert end_cause is not None, "swarm_common.models has no EndCause (contract request 23)"
    assert [c.value for c in end_cause] == END_CAUSES
    fields = {f.name: f for f in dataclasses.fields(models.Task)}
    assert "end_cause" in fields, "Task has no end_cause field"
    assert fields["end_cause"].default is None, "an old document must decode with no cause"
    # Stored as its string value, beside the state, never as an enum object.
    assert _task(end_cause=end_cause.TIMEOUT).to_firestore()["end_cause"] == "timeout"
    assert _task().to_firestore()["end_cause"] is None


def test_the_catalogue_says_which_runners_declare_their_cost():
    flags = {name: getattr(profile, "cost_declared", None) for name, profile in RUNNER_PROFILES.items()}
    assert flags["mock"] is True, f"mock does not declare its cost: {flags}"
    assert all(flag is False for name, flag in flags.items() if name != "mock"), flags
    assert outcomes.DECLARED_COST_PROFILES == frozenset(n for n, f in flags.items() if f)
    # The ledger reads the flag; it names no profile of its own any more.
    source = Path(outcomes.__file__).read_text()
    assert '"mock"' not in source and "'mock'" not in source, (
        "swarm_api/outcomes.py still names a profile; DECLARED_COST_PROFILES must come "
        "from RunnerProfile.cost_declared"
    )


def test_every_end_cause_has_exactly_one_class():
    """The two maps partition the enum: a value added to the contract and not
    here is caught now, not counted as "other" for months."""
    values = {c.value for c in models.EndCause}
    failure = outcomes._FAILURE_OF_CAUSE
    cancel = outcomes._CANCEL_OF_CAUSE
    assert set(failure) | set(cancel) == values
    assert not set(failure) & set(cancel)
    assert set(failure.values()) <= set(outcomes.FAILURE_KEYS)
    assert set(cancel.values()) <= set(outcomes.CANCEL_KEYS)


def test_the_fixed_orders_carry_the_new_classes_where_an_attempt_meets_them():
    assert [k for k, _ in outcomes.FAILURE_CLASSES] == [
        "runner_error", "timeout", "lost_worker", "could_not_start", "inputs_unavailable",
        "outputs_missing", "dispatch_failed", "other", "no_reason",
    ]
    assert dict(outcomes.FAILURE_CLASSES)["inputs_unavailable"] == "inputs unavailable"
    assert [k for k, _ in outcomes.CANCEL_CAUSES] == [
        "requested", "after_failure", "after_cancel", "workflow_sweep", "other",
    ]
    assert dict(outcomes.CANCEL_CAUSES)["after_cancel"] == "after a cancel"
    # Both versions moved, so every stored day is re-derived under the new rules.
    assert outcomes.DERIVE_VERSION >= 2
    assert outcomes.CLASSIFIER_VERSION >= 2
    assert outcomes.VOCAB["classifier_version"] == outcomes.CLASSIFIER_VERSION


# --------------------------------------------------------------------------
# The classifier: the cause first, the text only without one
# --------------------------------------------------------------------------

def test_a_typed_end_cause_is_read_before_any_text():
    classify = outcomes.classify_failure
    # The text and the exit code say "runner error"; the writer said timeout.
    assert classify("FAILED", "runner exited 1", 1, end_cause="timeout") == "timeout"
    assert classify("FAILED", "reconciled: lease silent", None, end_cause="cannot_start") == "could_not_start"
    assert classify("FAILED", None, None, end_cause="inputs_unavailable") == "inputs_unavailable"
    assert classify("DEAD_LETTERED", "boom", 1, end_cause="lost_worker") == "lost_worker"
    # A cause this image does not know is counted, as "other" -- never re-guessed from the text.
    assert classify("FAILED", "runner exited 1", 1, end_cause="a_cause_from_a_newer_writer") == "other"
    # A success has no failure class, whatever it carries.
    assert classify("SUCCEEDED", None, 0, end_cause="timeout") is None
    # Without a cause, the text rules are unchanged.
    assert classify("FAILED", "runner exited 1", 1) == "runner_error"
    assert classify("FAILED", None, None) == "no_reason"


@pytest.mark.parametrize(
    "text",
    [
        # dev, 2026-09-25: 6 of the 13 "runner errors"
        "upstream task task_4b1e9 did not produce an artifact named 'summary.md'; "
        "it uploaded 'notes.md'",
        # dev, 2026-09-25: 4 of the 13
        "upstream tasks task_a1, task_b2 all stage 'report.md' into this workspace; one would "
        "silently overwrite the other, so the attempt is refused rather than run on whichever "
        "arrived last",
    ],
)
def test_the_workers_refusal_to_stage_an_input_is_its_own_class(text):
    assert outcomes.classify_failure("FAILED", text, 1) == "inputs_unavailable"


SWEEP_TEXT = (
    "workflow step a is FAILED and on_step_failure is fail_workflow, "
    "so steps that had not started were cancelled"
)


def _ended_doc(task_id: str, state: str, *, parents=(), text=None, cause=None, requested=False) -> dict:
    """A task document as the ledger reads it, ended at 12:00 on 25 Sep."""
    moment = datetime(2026, 9, 25, 12, tzinfo=UTC)
    return {
        "id": task_id, "tenant_id": "eng", "state": state, "created_at": moment,
        "completed_at": moment, "depends_on": list(parents), "last_error": text,
        "end_cause": cause, "cancel_requested": requested, "runner_profile": "mock",
    }


def _cascade(task_id: str, *parents: str, cause=None) -> dict:
    """What the scheduler writes on a dependant of a parent that did not succeed."""
    return _ended_doc(task_id, "CANCELLED", parents=parents, text=CASCADE_TEXT, cause=cause)


def _split(task: dict, *docs: dict | None, unreadable: tuple[str, ...] = ()) -> str:
    """The cancel cause the derive gives `task`, over the documents it holds.
    `unreadable` names parents it could not read (missing, or another tenant's)."""
    known: dict = {doc["id"]: doc for doc in docs if doc is not None}
    known.update({task_id: None for task_id in unreadable})
    return outcomes.tuple_from_docs(task, [], known)["cancel_cause"]


#: One parent, as each terminal writer leaves it, and what it makes a cascade
#: below it that carries no cause of its own.
ONE_PARENT = [
    ("failed", _ended_doc("p", "FAILED", text="runner exited 1"), "after_failure"),
    ("dead-lettered", _ended_doc("p", "DEAD_LETTERED"), "after_failure"),
    ("requested, by the flag", _ended_doc("p", "CANCELLED", requested=True), "after_cancel"),
    ("requested, by the prefix", _ended_doc("p", "CANCELLED", text="cancelled on request; x"), "after_cancel"),
    ("requested, typed", _ended_doc("p", "CANCELLED", cause="cancel_requested", requested=True), "after_cancel"),
    ("a cancel's cascade, typed", _ended_doc("p", "CANCELLED", cause="cancelled_parent"), "after_cancel"),
    # THE PARENT'S OWN END, NOT ITS STATE (the review of #217): a parent that
    # is itself a failure's cascade, or the fail_workflow sweep's, is CANCELLED
    # and passed on a failure.
    ("a failure's cascade, typed", _ended_doc("p", "CANCELLED", cause="failed_parent"), "after_failure"),
    ("swept, typed", _ended_doc("p", "CANCELLED", cause="workflow_sweep"), "after_failure"),
    ("swept, by its text", _ended_doc("p", "CANCELLED", text=SWEEP_TEXT), "after_failure"),
    # A parent cancelled for a reason nothing records: CANCELLED is not a
    # cancel somebody made, so the cascade below it is not claimed as one.
    ("cancelled, why unknown", _ended_doc("p", "CANCELLED"), "other"),
    ("stopped on SIGTERM, no flag", _ended_doc("p", "CANCELLED", text="runner stopped on SIGTERM"), "other"),
]


@pytest.mark.parametrize("parent, expected", [(p, e) for _, p, e in ONE_PARENT], ids=[i for i, _, _ in ONE_PARENT])
def test_a_cascade_cancel_with_no_cause_is_split_by_its_parent_s_own_end(parent, expected):
    assert _split(_cascade("k", "p"), parent) == expected


def test_a_cascade_with_several_parents_is_a_failure_s_if_any_parent_sent_one():
    requested = _ended_doc("p1", "CANCELLED", requested=True)
    failed = _ended_doc("p2", "FAILED")
    ok_parent = _ended_doc("p3", "SUCCEEDED")
    assert _split(_cascade("k", "p1", "p2"), requested, failed) == "after_failure"
    assert _split(_cascade("k", "p3", "p1"), ok_parent, requested) == "after_cancel"
    # A parent that sent something the read cannot name might have been the
    # failure: the cascade is not claimed to be a cancel's.
    unknown = _ended_doc("p4", "CANCELLED")
    assert _split(_cascade("k", "p1", "p4"), requested, unknown) == "other"
    assert _split(_cascade("k", "p1", "p5"), requested, unreadable=("p5",)) == "other"
    # A failure wins over both.
    assert _split(_cascade("k", "p4", "p2"), unknown, failed) == "after_failure"
    # Nothing read at all: the text cannot say which.
    assert _split(_cascade("k", "p5"), unreadable=("p5",)) == "other"
    assert _split(_cascade("k")) == "other"


def test_a_failure_s_cascade_is_a_failure_s_however_far_down_it_reaches():
    """THE REVIEW OF #217. a FAILED -> b -> c -> d, none carrying a cause (every
    task on dev before the deploy). `c`'s parent is CANCELLED, but by the
    failure's cascade, so `c` and `d` are "after a failure" too. Read one hop
    up, they were "after a cancel": a cancel nobody made, moved out of the
    failure's cascade count by the very re-derive that was meant to fix it."""
    a = _ended_doc("a", "FAILED", text="runner exited 1")
    b, c, d = _cascade("b", "a"), _cascade("c", "b"), _cascade("d", "c")
    for task in (b, c, d):
        assert _split(task, a, b, c, d) == "after_failure", task["id"]
    # A typed link anywhere in the chain is read as its writer said.
    assert _split(d, _cascade("c", "b", cause="failed_parent")) == "after_failure"
    assert _split(d, _cascade("c", "b", cause="cancelled_parent")) == "after_cancel"


def test_a_cancel_s_cascade_is_a_cancel_s_however_far_down_it_reaches():
    a = _ended_doc("a", "CANCELLED", requested=True)
    b, c = _cascade("b", "a"), _cascade("c", "b")
    assert _split(b, a, b, c) == "after_cancel"
    assert _split(c, a, b, c) == "after_cancel"


def test_a_chain_the_read_cannot_finish_is_not_guessed():
    """`b` is an untyped cascade whose own parent was not read, and a corrupt
    pair names each other: neither is claimed, and neither loops."""
    b, c = _cascade("b", "a"), _cascade("c", "b")
    assert _split(c, b, c) == "other"
    x, y = _cascade("x", "y"), _cascade("y", "x")
    assert _split(x, x, y) == "other"


def test_a_cancel_that_carries_its_cause_is_never_split_again():
    failed = _ended_doc("p", "FAILED")
    requested = _ended_doc("p", "CANCELLED", requested=True)
    assert _split(_cascade("k", "p", cause="cancelled_parent"), failed) == "after_cancel"
    assert _split(_cascade("k", "p", cause="failed_parent"), requested) == "after_failure"
    cause = outcomes.cancel_cause
    assert cause(False, "anything at all", end_cause="workflow_sweep") == "workflow_sweep"
    assert cause(True, "cancelled on request; x", end_cause="cancel_requested") == "requested"
    # A failure's cause on a cancelled task names no cancel cause.
    assert cause(False, None, end_cause="timeout") == "other"


# --------------------------------------------------------------------------
# Through the route
# --------------------------------------------------------------------------

@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def api(db, tokens, group_map, clock) -> TestClient:
    seed_week(db)
    # 22 Sep: a second cascade in wf1, from a parent somebody CANCELLED. wf1 also
    # has c4, cancelled after its parent f1 FAILED.
    task(db, "p2", state="CANCELLED", created=at(22, 9), completed=at(22, 9, 30),
         workflow_id="wf1", step_id="e", attempt_count=0, cancel_requested=True)
    task(db, "k3", state="CANCELLED", created=at(22, 9), completed=at(22, 9, 35),
         workflow_id="wf1", step_id="d", depends_on=("p2",), attempt_count=0,
         last_error=CASCADE_TEXT)
    # 23 Sep: a cascade whose task carries its cause, and failures whose causes
    # the text would get wrong or could not tell.
    task(db, "p3", state="CANCELLED", created=at(23, 6), completed=at(23, 7),
         workflow_id="wf3", step_id="a", attempt_count=0, cancel_requested=True)
    typed = task(db, "k4", state="CANCELLED", created=at(23, 6), completed=at(23, 7, 6),
                 workflow_id="wf3", step_id="b", depends_on=("p3",), attempt_count=0,
                 last_error=CASCADE_TEXT)
    typed["end_cause"] = "cancelled_parent"
    task(db, "i1", state="FAILED", created=at(23, 7), completed=at(23, 8),
         last_error="upstream task task_4b1e9 did not produce an artifact named 'summary.md'; "
                    "it uploaded nothing")
    attempt(db, "a_i1", task_id="i1", created=at(23, 7, 30), started=at(23, 7, 31), exit_code=1)
    timed = task(db, "t1", state="FAILED", created=at(23, 8), completed=at(23, 9),
                 last_error="runner exited 1")
    timed["end_cause"] = "timeout"
    attempt(db, "a_t1", task_id="t1", created=at(23, 8, 30), started=at(23, 8, 31), exit_code=1)
    ctx = _context(db, tokens, StaticGroups(group_map), clock)
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def test_the_route_splits_the_cascade_and_reads_each_task_s_cause(api):
    body = ok(api, "alice", **WEEK)
    sep22, sep23 = body["buckets"][3], body["buckets"][4]

    assert sep22["cancelled"] == {
        "total": 6, "requested": 4, "after_failure": 1, "after_cancel": 1,
        "workflow_sweep": 0, "other": 0,
    }, "c4 followed a FAILED parent and k3 a CANCELLED one: two different cascades"
    assert sep23["cancelled"] == {
        "total": 2, "requested": 1, "after_failure": 0, "after_cancel": 1,
        "workflow_sweep": 0, "other": 0,
    }
    assert sep23["failure_classes"]["inputs_unavailable"] == 1, sep23["failure_classes"]
    assert sep23["failure_classes"]["timeout"] == 1, "t1's cause says timeout; its text never did"
    assert sep23["failure_classes"]["runner_error"] == 0, sep23["failure_classes"]
    assert body["totals"]["cancelled"]["after_cancel"] == 2
    assert body["vocab"]["classifier_version"] == outcomes.CLASSIFIER_VERSION


def test_a_failure_s_cascade_count_leaves_out_what_a_cancel_took(api):
    """wf1 lost c4 to its failed step and k3 to a stop by hand: only c4 is the failure's."""
    rows = ok(api, "alice", **WEEK)["workflows_failed"]["rows"]
    wf1 = next(r for r in rows if r["workflow_id"] == "wf1")
    assert wf1["cascade_cancelled"] == 1


def test_the_derive_follows_a_failure_s_cascade_up_through_days_it_did_not_read(api, db):
    """THE REVIEW OF #217, through the route. wfc is a -> b -> c -> d under
    `continue`, from before end causes: `a` FAILED and `b` was cancelled on 20
    Sep, `c` and `d` on 21 Sep. Deriving 21 Sep reads `c`'s parent `b` for the
    wait figure, but `b` is itself only a cascade, so the derive must read `a`
    as well -- a day it was not deriving -- to know that `c` and `d` followed a
    failure. Read one hop up they were two cancels nobody made, and the
    workflow's failure took one step instead of three."""
    common = {"created": at(20, 9), "workflow_id": "wfc", "attempt_count": 0}
    task(db, "ca", state="FAILED", completed=at(20, 10), step_id="a", last_error="runner exited 1",
         **{**common, "attempt_count": 1})
    attempt(db, "a_ca", task_id="ca", created=at(20, 9, 30), started=at(20, 9, 31), exit_code=1)
    task(db, "cb", state="CANCELLED", completed=at(20, 10, 1), step_id="b", depends_on=("ca",),
         last_error=CASCADE_TEXT, **common)
    task(db, "cc", state="CANCELLED", completed=at(21, 10, 2), step_id="c", depends_on=("cb",),
         last_error=CASCADE_TEXT, **common)
    task(db, "cd", state="CANCELLED", completed=at(21, 10, 3), step_id="d", depends_on=("cc",),
         last_error=CASCADE_TEXT, **common)

    body = ok(api, "alice", **WEEK)
    sep20, sep21 = body["buckets"][1], body["buckets"][2]
    assert sep20["cancelled"]["after_failure"] == 1
    assert sep21["cancelled"] == {
        "total": 2, "requested": 0, "after_failure": 2, "after_cancel": 0,
        "workflow_sweep": 0, "other": 0,
    }, "c and d followed a's failure, through b; nobody cancelled anything"
    wfc = next(r for r in body["workflows_failed"]["rows"] if r["workflow_id"] == "wfc")
    assert wfc["cascade_cancelled"] == 3


def test_a_day_stored_by_another_classifier_is_derived_again(api, db, clock):
    """The review of #196: `classifier_version` was written on every day and read
    by nothing, so a classifier change would have been served over old classes."""
    ok(api, "alice", **WEEK)
    stale = outcomes.CLASSIFIER_VERSION - 1
    db.docs["outcome_days/eng_2026-09-20"]["classifier_version"] = stale
    clock.now = NOW + timedelta(minutes=2)
    body = ok(api, "alice", **WEEK)
    assert body["coverage"]["derived_now"] == 1, "a day on another classifier was served as stored"
    assert db.docs["outcome_days/eng_2026-09-20"]["classifier_version"] == outcomes.CLASSIFIER_VERSION


def test_workflows_that_failed_count_nothing_when_they_do_not_apply(api):
    """kind=standalone leaves no step in view: the counts are not measured, so null, not 0."""
    block = ok(api, "alice", kind="standalone", **WEEK)["workflows_failed"]
    assert block == {
        "applicable": False,
        "with_ended_steps": None,
        "with_failed_steps": None,
        "rows_total": None,
        "rows": [],
        "failing_steps": [],
    }


def test_a_day_is_open_for_fifteen_minutes_after_it_ends_and_sealed_at_the_fifteenth(api, db, clock):
    """THE SEAL GRACE, PINNED (the review of #196 found it untested).

    The worker records spend before its terminal write, and each terminal
    writer stamps `completed_at` with its own clock, so a UTC day is complete
    fifteen minutes after it ends and not before. Until then its bucket is
    `open` (drawn "settling") and its stored day stays live; at the fifteenth
    minute it is sealed by a full derive. MUTATION: any other grace.
    """
    assert outcomes.OUTCOMES_SEAL_GRACE_S == 900
    ok(api, "alice", **WEEK)  # 25 Sep is stored live

    clock.now = datetime(2026, 9, 26, 0, 14, 59, tzinfo=UTC)
    body = ok(api, "alice", **WEEK)
    sep25 = next(b for b in body["buckets"] if b["start"].startswith("2026-09-25"))
    assert (sep25["state"], sep25["in_progress"]) == ("open", False), "settling, not sealed, not so far"
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is False
    assert body["coverage"]["seal_grace_s"] == 900
    assert body["buckets"][-1]["in_progress"] is True

    clock.now = datetime(2026, 9, 26, 0, 15, 0, tzinfo=UTC)
    body = ok(api, "alice", **WEEK)
    sep25 = next(b for b in body["buckets"] if b["start"].startswith("2026-09-25"))
    assert (sep25["state"], sep25["in_progress"]) == ("sealed", False)
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is True
    today = body["buckets"][-1]
    assert (today["state"], today["in_progress"]) == ("open", True)
