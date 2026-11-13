"""Plans pipeline, lane P1: `run.source` and `GET /v1/pipeline` (docs/design/plans-pipeline.md §3.1-§3.2).

What these hold, against the real app and the in-memory Firestore:

  * every run names what produced it: a run made by hand serves `hand`, a
    sweep's `sweep`, a plan-only schedule's `plan_only`, a failure-review
    fix run `failure_review` with the cause's fingerprint, each written at
    start; a run stored before the field derives it from `schedule.type`;
  * the board places each card in exactly one stage of §2.1's table, a
    pending merge folded into its run's card;
  * another tenant's runs and approvals never appear (invariant 9);
  * a failed approvals read answers `partial` with the runs still served;
  * a filter's hidden cards are counted in `by_stage.*.hidden`.

Each assertion names the MUTATION it catches. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from swarm_api import approvals, issueruns, pipeline
from swarm_api.issueruns import IssueRun, RunState
from swarm_api.schedtypes import failure_review, issue_sweep
from swarm_api.validation import IssueRef

from .conftest import auth_header
from .test_approvals import (  # noqa: F401 -- fixtures, used by name
    _grants,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .test_issue_runs import PLAN, _create
from .test_schedule_tick import seed_schedule

ISSUE = IssueRef(owner="example-org", repo="widgets", number=42)


def _mark(type_: str, schedule_id: str = "sch_000000000001") -> dict:
    return {"schedule_id": schedule_id, "firing_id": f"{schedule_id}:1", "slot": "2026-10-03T09:00:00Z",
            "type": type_}


def _seed_run(db, clock, run_id: str, state: RunState, *, tenant_id: str = "eng", schedule=None,
              source=None, ago: timedelta = timedelta(hours=2), **extra) -> dict:
    at = clock.at - ago
    run = IssueRun(
        id=run_id, tenant_id=tenant_id, created_by="alice@example.com", created_at=at, updated_at=at,
        state=state, issue=ISSUE, plan_approval="required", auto_merge=False, fix_rounds=2,
        planner_task_id=f"task_{run_id}", plan=PLAN if state != RunState.PLANNING else None,
        plan_digest=issueruns.plan_digest(PLAN) if state != RunState.PLANNING else None,
        history=[{"from": RunState.PLANNING.value, "to": state.value, "at": at, "by": "x"}],
        schedule=schedule, **extra,
    )
    doc = run.to_firestore()
    if source is not None:
        doc["source"] = source
    db.docs[f"issue_runs/{run_id}"] = doc
    return doc


def _record(db, clock, name: str, *, kind: str, state: str = approvals.PENDING, tenant_id: str = "eng",
            subject: dict | None = None, digest=None, decided_ago: timedelta | None = None) -> dict:
    decided_at = clock.at - decided_ago if decided_ago is not None else None
    requested = (decided_at or clock.at) - timedelta(hours=1)
    item_id = approvals.approval_id(kind, f"{tenant_id}:{name}")
    doc = {
        "approval_id": item_id, "tenant_id": tenant_id, "kind": kind,
        "subject": subject or {"title": name}, "digest": digest, "summary": name,
        "approvers": "members", "state": state, "requested_at": requested,
        "expires_at": requested + timedelta(hours=72),
        "decided_by": "alice@example.com" if state != approvals.PENDING else None,
        "decided_at": decided_at, "reason": None,
    }
    db.docs[f"approvals/{item_id}"] = doc
    return doc


def _pipeline(client, user="alice", **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get("/v1/pipeline" + (f"?{query}" if query else ""), headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(body: dict) -> dict[str, dict]:
    return {card["card_id"]: card for card in body["cards"]}


# --------------------------------------------------------------------------
# run.source (§3.1)
# --------------------------------------------------------------------------

def test_a_run_made_by_hand_serves_source_hand_and_stores_it(client, db):
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    # MUTATION: `POST /v1/runs` writing no source, or another kind.
    assert run["source"] == {"kind": "hand", "schedule_type": None, "schedule_id": None,
                             "firing_id": None, "fingerprint": None, "proposal_id": None}
    # MUTATION: serving a derived source without storing it at creation.
    assert db.docs[f"issue_runs/{run['id']}"]["source"]["kind"] == "hand"


def test_a_stored_run_without_source_derives_it_from_its_schedule_type(db, clock):
    _seed_run(db, clock, "run_old1", RunState.PLANNED, schedule=_mark("issue-sweep"))
    _seed_run(db, clock, "run_old2", RunState.PLANNED, schedule=_mark("issue-plan-only"))
    _seed_run(db, clock, "run_old3", RunState.PLANNED)
    for run_id in ("run_old1", "run_old2", "run_old3"):
        assert "source" not in db.docs[f"issue_runs/{run_id}"]
    sweep = IssueRun.from_firestore(db.docs["issue_runs/run_old1"]).to_api()["source"]
    # MUTATION: an old sweep run read as `hand`, or without its schedule's ids.
    assert sweep == {"kind": "sweep", "schedule_type": "issue-sweep", "schedule_id": "sch_000000000001",
                     "firing_id": "sch_000000000001:1", "fingerprint": None, "proposal_id": None}
    assert IssueRun.from_firestore(db.docs["issue_runs/run_old2"]).to_api()["source"]["kind"] == "plan_only"
    # MUTATION: a run with no schedule derived as anything but `hand`.
    assert IssueRun.from_firestore(db.docs["issue_runs/run_old3"]).to_api()["source"]["kind"] == "hand"


def test_a_stored_source_wins_over_the_derivation(db, clock):
    stored = issueruns.run_source(_mark("failure-review"), fingerprint="fr-0123456789ab")
    _seed_run(db, clock, "run_fr", RunState.PLANNED, schedule=_mark("failure-review"), source=stored)
    served = IssueRun.from_firestore(db.docs["issue_runs/run_fr"]).to_api()["source"]
    # MUTATION: re-deriving on read drops the fingerprint written at start.
    assert served["kind"] == "failure_review" and served["fingerprint"] == "fr-0123456789ab"


class _Captured(Exception):
    pass


def _capture_start_run(monkeypatch, module) -> list[dict]:
    calls: list[dict] = []

    def fake(ctx, auth, ref, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="run_new", tenant_id="eng", issue=ref)

    monkeypatch.setattr(module, "start_run", fake)
    return calls


@pytest.mark.parametrize("type_, kind", [("issue-sweep", "sweep"), ("issue-plan-only", "plan_only")])
def test_the_sweep_executor_writes_its_source_at_start(monkeypatch, type_, kind):
    calls = _capture_start_run(monkeypatch, issue_sweep)
    mark = _mark(type_)
    firing = SimpleNamespace(
        ctx=SimpleNamespace(), schedule={"schedule_id": mark["schedule_id"]}, mark=mark,
        owner=SimpleNamespace(email="alice@example.com"), firing={"firing_id": mark["firing_id"]},
        recorded=lambda entry: entry,
    )
    candidate = SimpleNamespace(ref=ISSUE, snapshot=None)
    selection = SimpleNamespace(adopted=[], candidates=[candidate], room=1, start_failures=[], started=[],
                                report=SimpleNamespace(skipped_by_reason={}))
    monkeypatch.setattr(issue_sweep, "open_work_without", lambda snapshot, number: {})
    options = issue_sweep.RunOptions(plan_approval="required", auto_merge=False, fix_rounds=2,
                                     merge_approval=None)
    issue_sweep.start(firing, selection, options)
    # MUTATION: the executor passing no source (the run would derive it, but
    # §3.1 writes it at start), or a plan-only firing written as `sweep`.
    (call,) = calls
    assert call["source"]["kind"] == kind and call["source"]["schedule_type"] == type_
    assert call["source"]["firing_id"] == mark["firing_id"]


def test_a_failure_review_fix_run_writes_its_fingerprint_at_start(monkeypatch):
    calls = _capture_start_run(monkeypatch, failure_review)
    mark = _mark("failure-review")
    filing = object.__new__(failure_review._Filing)
    recorded: list[tuple] = []
    filing.review = {"tenant_id": "eng", "firing_id": mark["firing_id"]}
    schedule = {"schedule_id": mark["schedule_id"], "type": "failure-review", "gate": {"merge": "off"}}
    filing.firing = SimpleNamespace(schedule=schedule, mark=mark,
                                    owner=SimpleNamespace(email="alice@example.com"))
    filing.registration = {"owner": "example-org", "repo": "widgets"}
    filing.runs = SimpleNamespace(for_issue=lambda tenant_id, ref: [])
    filing.ctx = SimpleNamespace(store=SimpleNamespace(get_tenant=lambda tid: None), forge_tokens=None,
                                 forge=None, now=lambda: None)
    filing.causes = SimpleNamespace(record_run=lambda fp, run_id, firing_id: recorded.append((fp, run_id)))
    filing.start_failures = []
    monkeypatch.setattr(failure_review, "read_open_work", lambda *a, **k: {})
    monkeypatch.setattr(issue_sweep, "params_of", lambda firing: SimpleNamespace(fix_rounds=2))
    cause = SimpleNamespace(fingerprint="fr-0123456789ab")
    assert filing.start(cause, 42) == "run_new"
    # MUTATION: a failure-review run started as a sweep, or without the cause's fingerprint.
    (call,) = calls
    assert call["source"]["kind"] == "failure_review"
    assert call["source"]["fingerprint"] == "fr-0123456789ab"
    assert call["source"]["schedule_type"] == "failure-review"
    assert recorded == [("fr-0123456789ab", "run_new")]


# --------------------------------------------------------------------------
# GET /v1/pipeline (§3.2): one stage per card
# --------------------------------------------------------------------------

EXPECTED_STAGE = {
    RunState.PLANNING: "found",
    RunState.PLANNED: "planned",
    RunState.APPROVED: "running",
    RunState.RUNNING: "running",
    RunState.CHECKING: "pr_ci",
    RunState.FIXING: "pr_ci",
    RunState.DONE: "ended",
    RunState.FAILED: "ended",
    RunState.CANCELLED: "ended",
    RunState.REJECTED: "ended",
    RunState.NOT_READY: "ended",
}


def test_every_run_state_lands_in_its_one_stage(client, db, clock):
    for state in RunState:
        _seed_run(db, clock, f"run_{state.value.lower()}", state)
    body = _pipeline(client)
    cards = _by_id(body)
    for state in RunState:
        card = cards[f"run:run_{state.value.lower()}"]
        # MUTATION: any state moved to another column, or left off the board.
        assert card["stage"] == EXPECTED_STAGE[state], state
        assert card["state"] == state.value.lower()
    # MUTATION: a card placed twice.
    ids = [c["card_id"] for c in body["cards"]]
    assert len(ids) == len(set(ids)) == len(RunState)
    # MUTATION: only a pending plan waits on a person among runs.
    waiting = {c["card_id"] for c in body["cards"] if c["waits_on_person"]}
    assert waiting == {"run:run_planned"}
    assert cards["run:run_planned"]["approval_kind"] == "plan"
    assert body["waits_on_person"] == 1
    assert body["window_days"] == pipeline.PIPELINE_WINDOW_DAYS == 14
    assert body["partial"] == []
    # MUTATION: a count that is not the stage's cards.
    for stage in pipeline.STAGES:
        assert body["by_stage"][stage]["count"] == sum(1 for c in body["cards"] if c["stage"] == stage)
        assert body["by_stage"][stage]["hidden"] == 0


def test_a_held_plan_is_a_hold_card_in_planned(client, db, clock):
    hold = {"code": "NEEDS_OWNER", "approvers": "owner_only", "reasons": ["iam"], "matched": ["iam.tf"],
            "set_at": clock.at}
    _seed_run(db, clock, "run_held", RunState.PLANNED, approval_hold=hold)
    card = _by_id(_pipeline(client))["run:run_held"]
    # MUTATION: a held run projected as `plan`, or not waiting on a person.
    assert card["stage"] == "planned" and card["approval_kind"] == "hold" and card["waits_on_person"]
    assert card["subject"]["approval_id"] == "run:run_held"


def test_proposals_land_in_found_while_pending_and_in_ended_once_decided(client, db, clock):
    seed_schedule(db, schedule_id="sch_000000000009", type_="observer")
    subject = {"schedule_id": "sch_000000000009", "firing_id": "sch_000000000009:1", "title": "x"}
    pending = _record(db, clock, "p1", kind=approvals.PROPOSAL, subject=subject)
    approved = _record(db, clock, "p2", kind=approvals.PROPOSAL, state=approvals.APPROVED, subject=subject,
                       decided_ago=timedelta(days=2))
    rejected = _record(db, clock, "p3", kind=approvals.PROPOSAL, state=approvals.REJECTED, subject=subject,
                       decided_ago=timedelta(days=3))
    expired = _record(db, clock, "p4", kind=approvals.PROPOSAL, state=approvals.EXPIRED, subject=subject,
                      decided_ago=timedelta(days=1))
    superseded = _record(db, clock, "p5", kind=approvals.PROPOSAL, state=approvals.SUPERSEDED,
                         subject=subject, decided_ago=timedelta(days=1))
    old = _record(db, clock, "p6", kind=approvals.PROPOSAL, state=approvals.APPROVED, subject=subject,
                  decided_ago=timedelta(days=20))
    cards = _by_id(_pipeline(client))
    card = cards[pending["approval_id"]]
    # MUTATION: a pending proposal anywhere but `found`, or not waiting on a person.
    assert card["stage"] == "found" and card["waits_on_person"] and card["approval_kind"] == "proposal"
    # MUTATION: a proposal's source not read from its schedule's type.
    assert card["source"]["schedule_type"] == "observer" and card["source"]["kind"] == "observer"
    assert card["source"]["proposal_id"] == pending["approval_id"]
    for doc in (approved, rejected, expired, superseded):
        # MUTATION: a decided proposal left in `found`, or waiting on a person.
        assert cards[doc["approval_id"]]["stage"] == "ended"
        assert cards[doc["approval_id"]]["waits_on_person"] is False
    # MUTATION: no window on ended cards (PIPELINE_WINDOW_DAYS).
    assert old["approval_id"] not in cards


def test_a_pending_merge_folds_into_its_runs_card(client, db, clock):
    _seed_run(db, clock, "run_pr", RunState.CHECKING,
              pull_request={"number": 57, "url": "https://example.invalid/pr/57", "head_sha": "a" * 40,
                            "checks": "green"})
    merge = _record(db, clock, "m1", kind=approvals.MERGE, subject={"run_id": "run_pr", "issue": "x"},
                    digest={"head_sha": "a" * 40, "verdict": None})
    body = _pipeline(client)
    cards = _by_id(body)
    # MUTATION: the merge served as a second card beside its run.
    assert merge["approval_id"] not in cards
    card = cards["run:run_pr"]
    assert card["stage"] == "pr_ci" and card["approval_kind"] == "merge" and card["waits_on_person"]
    assert card["subject"]["approval_id"] == merge["approval_id"]
    assert card["pull_request"] == {"number": 57, "url": "https://example.invalid/pr/57", "checks": "green"}
    assert len(body["cards"]) == 1


def test_an_ended_run_older_than_the_window_is_not_on_the_board(client, db, clock):
    _seed_run(db, clock, "run_recent", RunState.DONE, ago=timedelta(days=13))
    _seed_run(db, clock, "run_ancient", RunState.DONE, ago=timedelta(days=15))
    cards = _by_id(_pipeline(client))
    # MUTATION: the window dropped or set below 14 days.
    assert "run:run_recent" in cards and "run:run_ancient" not in cards


# --------------------------------------------------------------------------
# isolation, partial reads, filters
# --------------------------------------------------------------------------

def test_another_tenants_runs_and_approvals_never_appear(client, db, clock):
    _seed_run(db, clock, "run_mine", RunState.PLANNED)
    _seed_run(db, clock, "run_theirs", RunState.PLANNED, tenant_id="research")
    _seed_run(db, clock, "run_theirs_done", RunState.DONE, tenant_id="research")
    theirs = _record(db, clock, "theirs", kind=approvals.PROPOSAL, tenant_id="research")
    ids = set(_by_id(_pipeline(client)))
    # MUTATION: dropping the tenant filter from any read (invariant 9).
    assert ids == {"run:run_mine"}
    bob = set(_by_id(_pipeline(client, user="bob")))
    assert bob == {"run:run_theirs", "run:run_theirs_done", theirs["approval_id"]}


def test_a_failed_approvals_read_answers_partial_and_the_runs_still_draw(client, db, clock, monkeypatch):
    _seed_run(db, clock, "run_planned", RunState.PLANNED)
    _record(db, clock, "p1", kind=approvals.PROPOSAL)

    def broken(*args, **kwargs):
        raise RuntimeError("approvals read failed")

    monkeypatch.setattr(pipeline, "_approval_records", broken)
    body = _pipeline(client)
    # MUTATION: one failed read failing the whole board, or failing silently.
    assert body["partial"] == [{"read": "approvals", "status": 503}]
    assert set(_by_id(body)) == {"run:run_planned"}


def test_a_source_filter_counts_what_it_hides_per_stage(client, db, clock):
    _seed_run(db, clock, "run_hand", RunState.PLANNED)
    _seed_run(db, clock, "run_sweep", RunState.PLANNED, schedule=_mark("issue-sweep"))
    _seed_run(db, clock, "run_fr", RunState.RUNNING, schedule=_mark("failure-review"),
              source=issueruns.run_source(_mark("failure-review"), fingerprint="fr-0123456789ab"))
    _seed_run(db, clock, "run_hand_done", RunState.DONE)
    body = _pipeline(client, source="failure_review")
    # MUTATION: the filter not applied, or applied to the wrong field.
    assert set(_by_id(body)) == {"run:run_fr"}
    # MUTATION: `hidden` not counted, or counted in the wrong stage.
    assert body["by_stage"]["planned"] == {"count": 0, "hidden": 2, "oldest_at": None}
    assert body["by_stage"]["running"]["count"] == 1 and body["by_stage"]["running"]["hidden"] == 0
    assert body["by_stage"]["ended"]["hidden"] == 1
    waits = _pipeline(client, waits="person")
    assert set(_by_id(waits)) == {"run:run_hand", "run:run_sweep"}
    assert waits["by_stage"]["running"]["hidden"] == 1


def test_a_state_filter_leaves_its_columns(client, db, clock):
    for state in RunState:
        _seed_run(db, clock, f"run_{state.value.lower()}", state)
    stages = {c["stage"] for c in _pipeline(client, state="running")["cards"]}
    # MUTATION: `state=running` leaving the found or ended columns.
    assert stages == {"running", "pr_ci"}
    assert {c["stage"] for c in _pipeline(client, state="ended")["cards"]} == {"ended"}
    assert {c["stage"] for c in _pipeline(client, state="waiting")["cards"]} == {"found", "planned"}


def test_an_unknown_filter_value_is_422(client):
    # MUTATION: an unknown filter silently serving everything.
    for params in ("source=nobody", "state=sleeping", "waits=robot"):
        assert client.get(f"/v1/pipeline?{params}", headers=auth_header("alice")).status_code == 422


def test_the_pipeline_writes_nothing(client, db, clock):
    _seed_run(db, clock, "run_pr", RunState.DONE)
    # A merge whose run has moved on: the inbox would close it; the board must not.
    merge = _record(db, clock, "m1", kind=approvals.MERGE, subject={"run_id": "run_pr"},
                    digest={"head_sha": "b" * 40})
    before = {k: dict(v) for k, v in db.docs.items()}
    _pipeline(client)
    # MUTATION: reusing `approvals.inbox`, which syncs and closes records.
    assert db.docs == before
    assert db.docs[f"approvals/{merge['approval_id']}"]["state"] == approvals.PENDING
