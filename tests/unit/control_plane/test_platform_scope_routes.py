"""A platform-scope schedule's plan and merge are a platform admin's on every route (PLATFORM-SCHED-ROUTES, part of #2157).

PLATFORM-SCHED-APPROVALS (PR 2292) refused `POST /v1/approvals/{id}:approve`
and `:reject` of a platform-scope schedule's item to anyone but a platform
admin. Two more routes decided the same items, and the failure-review
schedule reaches both, since it creates issue runs with plan and merge gates.
BGD decided on 2026-10-25 to close them with the same check:

* `POST /v1/runs/{id}/plan:approve` and `:reject` (`sc plan approve`);
* the `/swarmcloud approve` comment, on a plan and on a merge
  (`ghapprove._approve_plan`, `_approve_merge`).

The schedule is read from the run's or the record's STORED reference. A
refused decision decides nothing. PLATFORM_OWNER is a platform admin, so the
owner waiver (#2259) still applies to the owner on the comment path. A
workspace-scope schedule's run is decided exactly as before.

Who is who (`test_platform_scope_approvals`): `alice` a member, `dave` eng's
workspace admin, `erin` a platform admin, `root` PLATFORM_OWNER.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import logging

import pytest

from swarm_api import approvals, ghapprove

from .conftest import auth_header
from .test_github_comment_approvals import _command, _comment, _connect, _replies
from .test_platform_scope_approvals import (  # noqa: F401  (fixtures)
    ADMINS,
    DAVE,
    REFUSED,
    SCHEDULE,
    _audit,
    _checking,
    _docs,
    _inbox,
    _merge_workflows,
    _plan_item,
    _refused,
    _schedule,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    workspace_admin,
    writes,
)
from .test_approval_owner_waiver import _owner_hold_merge_waiting, _waivers
from .test_issue_run_ci import PR, SHA_A, _read
from .test_issue_runs import FULL_PLAN

#: Each person's GitHub account: (login, GitHub user id).
LOGINS = {"alice": ("alice-gh", 1001), "dave": ("Dave-GH", 1004), "erin": ("erin-gh", 1005),
          "root": ("root-gh", 1000)}


def _plan_route(client, run: dict, verb: str, user: str):
    body = {"plan_digest": run["plan_digest"]}
    if verb == "reject":
        body["reason"] = "out of scope"
    return client.post(f"/v1/runs/{run['id']}/plan:{verb}", headers=auth_header(user), json=body)


def _state(db, run: dict) -> str:
    return db.docs[f"issue_runs/{run['id']}"]["state"]


def _connect_all(db) -> None:
    for user, (login, gh_id) in LOGINS.items():
        _connect(db, f"{user}@example.com", login, user_id=gh_id)


def _say(writes, number: int, digest: str, user: str) -> int:
    login, gh_id = LOGINS[user]
    return _comment(writes, number, _command(digest), login=login, user_id=gh_id)


def _merge_waiting(client, db, objects, writes, clock, mode: str) -> tuple[dict, dict]:
    """A failure-review-style run of the schedule, green, its merge request posted on its pull request."""
    _schedule(db, mode)
    running = _checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored["merge_approval"] = "required"
    stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE, "firing_id": f"{SCHEDULE}:9"}
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (item,) = _inbox(client, user="root", kind="merge")
    assert item["subject"]["schedule_id"] == SCHEDULE
    return running, item


# ---------------------------------------------------------------------------
# POST /v1/runs/{id}/plan:approve and :reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED)
def test_a_member_and_a_workspace_admin_cannot_decide_a_platform_scope_plan_on_the_run_routes(
        client, db, objects, user):
    run = _plan_item(client, db, objects, "platform")
    # MUTATION: drop `platform_only` from `approve_plan` / `reject_plan` and these are 200.
    _refused(_plan_route(client, run, "approve", user))
    _refused(_plan_route(client, run, "reject", user))
    assert _state(db, run) == "PLANNED"
    stored = db.docs[f"issue_runs/{run['id']}"]
    assert not stored.get("approved_by") and not stored.get("rejected_by")
    assert not _docs(db, "workflows")
    assert _audit(db, SCHEDULE) == []


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admin_decides_a_platform_scope_plan_on_the_run_routes(client, db, objects, user):
    run = _plan_item(client, db, objects, "platform")
    approved = _plan_route(client, run, "approve", user)
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"
    assert db.docs[f"issue_runs/{run['id']}"]["approved_by"] == f"{user}@example.com"

    other = _plan_item(client, db, objects, "platform")
    rejected = _plan_route(client, other, "reject", user)
    assert rejected.status_code == 200, rejected.text
    assert _state(db, other) == "REJECTED"


def test_the_run_routes_refusal_is_logged_with_who_asked_and_what(client, db, objects, caplog):
    run = _plan_item(client, db, objects, "platform")
    with caplog.at_level(logging.WARNING, logger="swarm_api.routes.schedules"):
        _refused(_plan_route(client, run, "approve", "dave"))
        _refused(_plan_route(client, run, "reject", "dave"))
    lines = [r.getMessage() for r in caplog.records if "platform_admin_required" in r.getMessage()]
    assert len(lines) == 2, [r.getMessage() for r in caplog.records]
    for line, action in zip(lines, ("approve", "reject")):
        assert f"schedule={SCHEDULE}" in line and f"email={DAVE}" in line
        assert "tenant=eng" in line and f"action={action}" in line


def test_a_refused_reject_of_a_run_another_tenant_cannot_see_is_still_404(client, db, objects):
    run = _plan_item(client, db, objects, "platform")
    response = client.post(f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("bob"),
                           json={"plan_digest": run["plan_digest"]})
    assert response.status_code == 404, response.text
    assert _state(db, run) == "PLANNED"


@pytest.mark.parametrize("user", REFUSED)
@pytest.mark.parametrize("verb,after", [("approve", "RUNNING"), ("reject", "REJECTED")])
def test_a_workspace_scope_plan_is_decided_on_the_run_routes_as_before(client, db, objects, user, verb, after):
    run = _plan_item(client, db, objects, "workspace")
    response = _plan_route(client, run, verb, user)
    assert response.status_code == 200, response.text
    assert _state(db, run) == after


@pytest.mark.parametrize("verb,after", [("approve", "RUNNING"), ("reject", "REJECTED")])
def test_a_run_of_no_schedule_is_decided_on_the_run_routes_as_before(client, db, objects, verb, after):
    from .test_approvals import _planned

    run = _planned(client, db, objects)
    response = _plan_route(client, run, verb, "dave")
    assert response.status_code == 200, response.text
    assert _state(db, run) == after


# ---------------------------------------------------------------------------
# /swarmcloud approve <digest>, on a plan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED)
def test_a_member_and_a_workspace_admins_comment_approves_no_platform_scope_plan(
        client, db, objects, writes, api_context, user):
    _connect_all(db)
    run = _plan_item(client, db, objects, "platform")
    _say(writes, 42, run["plan_digest"], user)

    polled = ghapprove.poll(api_context)

    # MUTATION: drop `_platform_only` from `ghapprove._approve_plan` and this approves 1.
    assert polled.get("approved", 0) == 0, (polled, _replies(writes, 42))
    assert _state(db, run) == "PLANNED"
    assert not _docs(db, "workflows")
    assert _audit(db, SCHEDULE) == []
    (reply,) = _replies(writes, 42)
    assert "platform_admin_required" in reply and "platform admin" in reply


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admins_comment_approves_a_platform_scope_plan(client, db, objects, writes, api_context, user):
    _connect_all(db)
    run = _plan_item(client, db, objects, "platform")
    cid = _say(writes, 42, run["plan_digest"], user)

    polled = ghapprove.poll(api_context)

    # MUTATION: leave the comment's AuthContext at `is_admin=False` and erin's is refused.
    assert polled["approved"] == 1, (polled, _replies(writes, 42))
    assert _state(db, run) == "RUNNING"
    assert db.docs[f"issue_runs/{run['id']}"]["approved_by"] == f"{user}@example.com"
    (entry,) = [e for e in _audit(db, SCHEDULE) if e["action"] == "approval_approved"]
    assert entry["detail"]["via"] == "github_comment" and entry["detail"]["comment_id"] == cid


def test_a_comments_refusal_is_logged_with_who_asked_and_what(client, db, objects, writes, api_context, caplog):
    _connect_all(db)
    run = _plan_item(client, db, objects, "platform")
    _say(writes, 42, run["plan_digest"], "dave")
    with caplog.at_level(logging.WARNING, logger="swarm_api.routes.schedules"):
        ghapprove.poll(api_context)
    (line,) = [r.getMessage() for r in caplog.records if "platform_admin_required" in r.getMessage()]
    assert f"schedule={SCHEDULE}" in line and f"email={DAVE}" in line and "action=approve" in line


@pytest.mark.parametrize("user", REFUSED)
def test_a_workspace_scope_plan_is_approved_by_comment_as_before(client, db, objects, writes, api_context, user):
    _connect_all(db)
    run = _plan_item(client, db, objects, "workspace")
    _say(writes, 42, run["plan_digest"], user)
    assert ghapprove.poll(api_context)["approved"] == 1
    assert _state(db, run) == "RUNNING"


# ---------------------------------------------------------------------------
# /swarmcloud approve <digest>, on a merge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED)
def test_a_member_and_a_workspace_admins_comment_approves_no_platform_scope_merge(
        client, db, objects, writes, clock, api_context, user):
    _connect_all(db)
    running, item = _merge_waiting(client, db, objects, writes, clock, "platform")
    _say(writes, PR, approvals.merge_digest_text(item["digest"]), user)

    polled = ghapprove.poll(api_context)

    # MUTATION: drop `_platform_only` from `ghapprove._approve_merge` and this approves 1.
    assert polled.get("approved", 0) == 0, (polled, _replies(writes, PR))
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "pending"
    assert not db.docs[f"issue_runs/{running['id']}"].get("merge_approved")
    _read(client, clock, running["id"])
    assert _merge_workflows(db) == []
    assert "platform_admin_required" in _replies(writes, PR)[-1]


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admins_comment_approves_a_platform_scope_merge(
        client, db, objects, writes, clock, api_context, user):
    _connect_all(db)
    running, item = _merge_waiting(client, db, objects, writes, clock, "platform")
    _say(writes, PR, approvals.merge_digest_text(item["digest"]), user)

    assert ghapprove.poll(api_context)["approved"] == 1, _replies(writes, PR)
    decided = db.docs[f"approvals/{item['approval_id']}"]
    assert decided["state"] == "approved" and decided["decided_by"] == f"{user}@example.com"
    _read(client, clock, running["id"])
    assert len(_merge_workflows(db)) == 1


def test_a_workspace_scope_merge_is_approved_by_a_members_comment_as_before(
        client, db, objects, writes, clock, api_context):
    _connect_all(db)
    running, item = _merge_waiting(client, db, objects, writes, clock, "workspace")
    _say(writes, PR, approvals.merge_digest_text(item["digest"]), "dave")
    assert ghapprove.poll(api_context)["approved"] == 1, _replies(writes, PR)
    _read(client, clock, running["id"])
    assert len(_merge_workflows(db)) == 1


def test_the_owners_waiver_still_applies_by_comment_on_a_platform_scope_schedule(
        client, db, objects, writes, clock, api_context, monkeypatch):
    """#2259's merge (an owner_only hold, the owner the schedule's last editor) on a platform-scope schedule."""
    from .test_approvals import SWITCH

    monkeypatch.setenv(SWITCH, "on")
    running, item = _owner_hold_merge_waiting(client, db, objects, writes, clock)
    db.docs[f"schedules/{SCHEDULE}"]["scope"] = {"mode": "platform"}
    _connect(db, "erin@example.com", "erin-gh", user_id=1005)

    # A platform admin who is not the owner still meets the hold: nothing moves.
    _say(writes, PR, approvals.merge_digest_text(item["digest"]), "erin")
    assert ghapprove.poll(api_context).get("approved", 0) == 0
    assert _waivers(db) == []

    _say(writes, PR, approvals.merge_digest_text(item["digest"]), "root")
    assert ghapprove.poll(api_context)["approved"] == 1, _replies(writes, PR)
    decided = db.docs[f"approvals/{item['approval_id']}"]
    assert decided["state"] == "approved" and decided["decided_by"] == "root@example.com"
    (waiver,) = _waivers(db)
    assert waiver["by"] == "root@example.com" and waiver["detail"]["waived"] == "second_person_required"
    assert db.docs[f"issue_runs/{running['id']}"]["merge_approved"]["head_sha"] == SHA_A
