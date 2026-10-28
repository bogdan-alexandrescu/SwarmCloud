"""`GET /v1/approvals?state=`: the last 7 days' decided and expired approvals (#2417 A4).

BGD 2026-10-27 (AUTO-A4A5): the approvals API returns recent decisions, so
the console's Approvals page can draw its "decided" and "expired" sections
from the server rather than from what it already holds. What these hold,
against the real app and the in-memory Firestore:

  * decided (approved, rejected) and expired items of the last 7 days are
    served NEWEST FIRST by `decided_at`, each with `state`, `decided_by`,
    `decided_by_name`, `decided_at` and `reason`; older ones are not;
  * `state=pending|decided|expired|all` filters, and anything else is 422;
  * the default is the inbox exactly as before: a caller that never asks
    for history gets the same rows and the same response keys;
  * projected kinds follow the same rule from their own source of truth: a
    plan approved, rejected or expired on its run, a step answered or
    expired on its task;
  * another tenant's decisions never appear (invariant 9);
  * a read is bounded, and says so when it was full.

Each assertion names the MUTATION it catches. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from swarm_api import approvals

from .conftest import auth_header, seed_task
from .test_approvals import (  # noqa: F401 -- fixtures, used by name
    _decide,
    _grants,
    _planned,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .test_issue_runs import PLAN, _create, _finish_planner, _run
from .test_schedule_tick import seed_schedule

ALICE = "alice@example.com"


def _get(client, user="alice", **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get("/v1/approvals" + (f"?{query}" if query else ""), headers=auth_header(user))


def _read(client, user="alice", **params) -> dict:
    response = _get(client, user, **params)
    assert response.status_code == 200, response.text
    return response.json()


def _record(db, clock, name: str, *, state: str, decided_ago: timedelta | None,
            tenant_id: str = "eng", kind: str = "proposal", reason: str | None = None,
            by: str = ALICE) -> dict:
    """An approval record as `request` and `decide` (or `_close`) leave it."""
    decided_at = clock.at - decided_ago if decided_ago is not None else None
    requested = (decided_at or clock.at) - timedelta(hours=1)
    item_id = approvals.approval_id(kind, f"{tenant_id}:{name}")
    doc = {
        "approval_id": item_id, "tenant_id": tenant_id, "kind": kind,
        "subject": {"title": name}, "digest": f"digest-{name}", "summary": name,
        "approvers": "members", "state": state, "requested_at": requested,
        "expires_at": requested + timedelta(hours=72),
        "decided_by": by if state != approvals.PENDING else None,
        "decided_at": decided_at, "reason": reason,
    }
    if state == approvals.EXPIRED:
        doc["decided_by"] = approvals.EXPIRY_ACTOR
    db.docs[f"approvals/{item_id}"] = doc
    return doc


def _seed_week(db, clock) -> dict[str, dict]:
    return {
        "pending": _record(db, clock, "pending", state=approvals.PENDING, decided_ago=None),
        "approved": _record(db, clock, "approved", state=approvals.APPROVED, decided_ago=timedelta(days=2)),
        "rejected": _record(db, clock, "rejected", state=approvals.REJECTED, decided_ago=timedelta(hours=3),
                            reason="not this one"),
        "expired": _record(db, clock, "expired", state=approvals.EXPIRED, decided_ago=timedelta(days=5)),
        "old": _record(db, clock, "old", state=approvals.APPROVED, decided_ago=timedelta(days=8)),
        "superseded": _record(db, clock, "superseded", state=approvals.SUPERSEDED,
                              decided_ago=timedelta(hours=1)),
    }


def _ids(rows: list[dict]) -> list[str]:
    return [r["approval_id"] for r in rows]


# --------------------------------------------------------------------------
# records: the window, the order, the fields
# --------------------------------------------------------------------------

def test_decided_and_expired_records_of_the_last_7_days_are_served_newest_first(client, db, clock):
    seeded = _seed_week(db, clock)
    body = _read(client, state="all")
    history = [r for r in body["approvals"] if r["state"] != "pending"]

    # MUTATION: sorting the history oldest first, or by `requested_at`
    # instead of `decided_at`, reorders these three.
    assert _ids(history) == _ids([seeded["rejected"], seeded["approved"], seeded["expired"]])
    # MUTATION: dropping the 7-day cut (or widening HISTORY_DAYS) serves `old`;
    # adding `superseded` to the history states serves it.
    assert seeded["old"]["approval_id"] not in _ids(body["approvals"])
    assert seeded["superseded"]["approval_id"] not in _ids(body["approvals"])
    # MUTATION: the pending inbox stops coming first under `all`.
    assert body["approvals"][0]["approval_id"] == seeded["pending"]["approval_id"]

    rejected = history[0]
    # MUTATION: serving a history row without its decision fields, or without
    # the name lookup the inbox gives `decided_by`.
    assert rejected["decided_by"] == ALICE and "decided_by_name" in rejected
    assert rejected["decided_at"] == (clock.at - timedelta(hours=3)).isoformat()
    assert rejected["reason"] == "not this one"
    # MUTATION: a history row loses the fields a pending one carries.
    assert {"kind", "subject", "summary", "approvers", "requested_at", "expires_at",
            "may_decide"} <= set(rejected)
    # MUTATION: `may_decide` offering a decision on a closed item.
    assert rejected["may_decide"]["approve"] is False and rejected["may_decide"]["reject"] is False
    assert history[2]["decided_by"] == approvals.EXPIRY_ACTOR
    # MUTATION: dropping the flag, or setting it on a read that was not full.
    assert body["history_truncated"] is False


def test_a_record_decided_just_inside_the_window_is_served_and_just_outside_is_not(client, db, clock):
    inside = _record(db, clock, "inside", state=approvals.REJECTED,
                     decided_ago=timedelta(days=7) - timedelta(minutes=1), reason="late")
    outside = _record(db, clock, "outside", state=approvals.REJECTED,
                      decided_ago=timedelta(days=7) + timedelta(minutes=1), reason="later")
    served = _ids(_read(client, state="decided")["approvals"])
    # MUTATION: a cut of 6 days drops `inside`; a cut of 8 serves `outside`.
    assert inside["approval_id"] in served and outside["approval_id"] not in served


# --------------------------------------------------------------------------
# the state filter
# --------------------------------------------------------------------------

def test_the_state_filter_serves_each_section_alone(client, db, clock):
    seeded = _seed_week(db, clock)
    # MUTATION: `decided` serving expired rows, or the pending inbox.
    assert _ids(_read(client, state="decided")["approvals"]) == _ids([seeded["rejected"], seeded["approved"]])
    # MUTATION: `expired` serving decided rows.
    assert _ids(_read(client, state="expired")["approvals"]) == _ids([seeded["expired"]])
    # MUTATION: `pending` serving history.
    assert _ids(_read(client, state="pending")["approvals"]) == _ids([seeded["pending"]])


def test_an_unknown_state_is_422(client, db, clock):
    _seed_week(db, clock)
    response = _get(client, state="approved")
    # MUTATION: accepting any word (and serving the inbox) instead of refusing it.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unknown_state"


def test_the_kind_filter_applies_to_the_history_too(client, db, clock):
    proposal = _record(db, clock, "p", state=approvals.APPROVED, decided_ago=timedelta(hours=1))
    _record(db, clock, "s", state=approvals.APPROVED, decided_ago=timedelta(hours=2), kind="spec")
    # MUTATION: the history ignoring `kind`.
    assert _ids(_read(client, state="decided", kind="proposal")["approvals"]) == [proposal["approval_id"]]


# --------------------------------------------------------------------------
# a caller that never asks for history sees no change
# --------------------------------------------------------------------------

def test_a_pending_only_callers_response_is_unchanged(client, db, clock):
    seeded = _seed_week(db, clock)
    default = _read(client)
    # MUTATION: a default other than `pending` serves history rows here.
    assert _ids(default["approvals"]) == _ids([seeded["pending"]])
    # MUTATION: adding `history_truncated` (or anything) to the default shape.
    assert set(default) == {"approvals", "tenant_id"}
    # MUTATION: `state=pending` differing from the default in any way.
    assert _read(client, state="pending") == default


# --------------------------------------------------------------------------
# tenant isolation
# --------------------------------------------------------------------------

def test_another_tenants_decisions_never_appear(client, db, clock):
    theirs = _record(db, clock, "theirs", state=approvals.APPROVED, decided_ago=timedelta(hours=1),
                     tenant_id="research", by="bob@example.com")
    lapsed = _record(db, clock, "lapsed", state=approvals.EXPIRED, decided_ago=timedelta(hours=2),
                     tenant_id="research")
    ours = _record(db, clock, "ours", state=approvals.APPROVED, decided_ago=timedelta(hours=3))
    # MUTATION: dropping the tenant filter from the query (and the re-check)
    # serves research's rows to an eng member.
    assert _ids(_read(client, state="all")["approvals"]) == [ours["approval_id"]]
    assert _ids(_read(client, user="bob", state="all")["approvals"]) == \
        _ids([theirs, lapsed])


def test_another_tenants_decided_plan_never_appears(client, db, objects):
    run = _planned(client, db, objects)
    assert _decide(client, f"run:{run['id']}", "reject", reason="out of scope").status_code == 200
    # MUTATION: projecting runs from any tenant's list.
    assert _read(client, user="bob", state="all")["approvals"] == []


# --------------------------------------------------------------------------
# bounded
# --------------------------------------------------------------------------

def test_a_full_read_is_bounded_and_says_so(client, db, clock, monkeypatch):
    monkeypatch.setattr(approvals, "HISTORY_LIMIT", 2)
    for n in range(3):
        _record(db, clock, f"r{n}", state=approvals.APPROVED, decided_ago=timedelta(hours=n + 1))
    body = _read(client, state="decided")
    # MUTATION: an unbounded read serves all three; a dropped flag hides it.
    assert len(body["approvals"]) == 2
    assert body["history_truncated"] is True


# --------------------------------------------------------------------------
# projected kinds, from their own source of truth
# --------------------------------------------------------------------------

def test_a_plan_approved_or_rejected_from_the_inbox_is_served_from_its_run(client, db, objects, clock):
    first = _planned(client, db, objects)
    item = next(i for i in _read(client)["approvals"] if i["approval_id"] == f"run:{first['id']}")
    assert _decide(client, item["approval_id"], "approve", digest=item["digest"]).status_code == 200
    clock.at += timedelta(hours=1)
    second = _planned(client, db, objects)
    assert _decide(client, f"run:{second['id']}", "reject", reason="out of scope").status_code == 200

    rows = _read(client, state="decided")["approvals"]
    # MUTATION: dropping the run source serves neither; ordering oldest
    # first puts the approval before the rejection.
    assert _ids(rows) == [f"run:{second['id']}", f"run:{first['id']}"]
    rejected, approved = rows
    # MUTATION: reading the decision from anywhere but the run.
    assert rejected["state"] == "rejected" and rejected["reason"] == "out of scope"
    assert rejected["decided_by"] == ALICE and rejected["kind"] == "plan"
    assert approved["state"] == "approved" and approved["decided_by"] == ALICE
    assert approved["decided_at"] is not None and approved["projected"] is True
    # MUTATION: a decided plan still served as pending.
    assert _read(client)["approvals"] == []


def test_a_scheduled_plan_that_expired_is_served_as_expired(client, db, objects, clock):
    seed_schedule(db)
    created = _create(client).json()["run"]
    db.docs[f"issue_runs/{created['id']}"]["metadata"] = {"schedule": {
        "schedule_id": "sch_000000000001", "firing_id": "sch_000000000001:5", "type": "issue-sweep"}}
    _finish_planner(db, objects, created, PLAN)
    assert _run(client, created["id"]).json()["run"]["state"] == "PLANNED"
    for hours in (71, 2):  # as test_approvals' expiry test: a tick before, then one after
        clock.at += timedelta(hours=hours)
        client.post("/v1/admin/runs/advance?tenant_id=eng", headers={"Authorization": "Bearer token-sweeper"})
    assert db.docs[f"issue_runs/{created['id']}"]["state"] == "REJECTED"

    (row,) = _read(client, state="expired")["approvals"]
    # MUTATION: serving expiry's rejection as a person's `rejected`.
    assert row["approval_id"] == f"run:{created['id']}" and row["state"] == "expired"
    assert row["decided_by"] == approvals.EXPIRY_ACTOR and row["reason"] is None
    assert _read(client, state="decided")["approvals"] == []
    clock.at += timedelta(days=8)
    # MUTATION: the run source ignoring the 7-day cut.
    assert _read(client, state="expired")["approvals"] == []


def _closed_step(db, clock, task_id: str, *, answered_ago: timedelta | None = None,
                 expired_ago: timedelta | None = None, tenant_id: str = "eng") -> dict:
    opened = clock.at - timedelta(days=1)
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="SUCCEEDED",
                    runner_profile="await-input", workflow_id="wf_review",
                    created_at=opened - timedelta(minutes=30))
    doc["submitted_by"] = ALICE
    doc["step_id"] = "review"
    doc["input"] = {"prompt": "Read draft.md."}
    window: dict = {"opened_at": opened, "expires_at": opened + timedelta(days=2)}
    if answered_ago is not None:
        window["answer"] = {"answered_by": ALICE, "answered_at": clock.at - answered_ago}
    if expired_ago is not None:
        window["expired"] = {"at": clock.at - expired_ago, "on_expire": "fail"}
    doc["metadata"] = {"dispatch": {"await_input": {"form": "verdict", "artifact": "review.json"}},
                       "await_input": window}
    return doc


def test_an_await_input_step_answered_or_expired_is_served_from_its_task(client, db, clock):
    _closed_step(db, clock, "t_answered", answered_ago=timedelta(hours=1))
    _closed_step(db, clock, "t_lapsed", expired_ago=timedelta(hours=2))
    _closed_step(db, clock, "t_theirs", answered_ago=timedelta(hours=1), tenant_id="research")
    _closed_step(db, clock, "t_old", answered_ago=timedelta(days=8))

    decided = _read(client, state="decided")["approvals"]
    # MUTATION: dropping the task source; serving another tenant's step or
    # one answered more than 7 days ago.
    assert _ids(decided) == ["input:t_answered"]
    assert decided[0]["state"] == approvals.ANSWERED and decided[0]["decided_by"] == ALICE
    assert decided[0]["kind"] == "input" and "may_decide" not in decided[0]
    expired = _read(client, state="expired")["approvals"]
    # MUTATION: an expired step served as answered, or not at all.
    assert _ids(expired) == ["input:t_lapsed"] and expired[0]["state"] == "expired"


@pytest.mark.parametrize("state", ["pending", "decided", "expired", "all"])
def test_a_viewer_of_no_tenant_gets_nothing(client, db, clock, state):
    _seed_week(db, clock)
    # MUTATION: a history read that skips `tenant_scope` serves eng's rows
    # to carol, who is in no group.
    response = _get(client, user="carol", state=state)
    assert response.status_code in (200, 403, 404), response.text
    if response.status_code == 200:
        assert all(r["tenant_id"] != "eng" for r in response.json()["approvals"])
