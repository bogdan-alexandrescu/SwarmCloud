"""An issue run whose workflow submission was never stored is settled (#3612).

`routes.runs._approve` commits PLANNED -> APPROVED, then submits the compiled
workflow, then writes RUNNING with its `workflow_id`. A swarm-api that died
or timed out between the claim and the RUNNING write left the run APPROVED
with no workflow for ever -- and, because the territory guard counts an
APPROVED run as worked, holding every later run that names one of its files.

What these tests hold:

  1. Past `APPROVED_SUBMISSION_BOUND_SECONDS`, one tick moves such a run to
     FAILED, its error saying the submission was never stored, by swarm-api.
  2. That releases its territory: a later auto run held
     `territory_overlap: <its id>` goes on the next tick.
  3. Inside the bound the tick leaves it alone: a submission may be in flight.
  4. A step task of the run's OWN tenant linked to the run
     (`metadata.issue_run.run_id`) is the lost RUNNING write: the tick adopts
     its workflow. Another tenant's task is never read as the run's.
  5. POST /v1/runs/{run_id}:end lets the workspace's admin end one with a
     reason; a plain member is 403, any other state 409, another tenant's run
     404, no reason 422.
  6. An auto-approval never claims a run with a blocking hold, including one
     written between the tick's read and the claim: nothing is submitted.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import secrets
from datetime import timedelta

import pytest

from swarm_api import approvals, forge, forgewrite, issueruns, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRuns, RunState
from swarm_api.routes import runs as runs_routes

from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import ENG_GROUP, api_settings, auth_header
from .test_issue_run_tick import SWEEPER, _stored, _tick
from .test_issue_run_tick import github  # noqa: F401 - a fixture, used by name
from .test_issue_runs import PLAN, _create, _docs, _finish_planner

BOUND = issueruns.APPROVED_SUBMISSION_BOUND_SECONDS
ADMIN = "dave@example.com"


def _plan(*files: str) -> dict:
    plan = copy.deepcopy(PLAN)
    for step in plan["steps"]:
        step["files"] = list(files)
    return plan


@pytest.fixture
def group_map():
    return {
        "alice@example.com": (ENG_GROUP,),
        ADMIN: (ENG_GROUP,),
        "bob@example.com": ("swarm-research@example.com",),
        "carol@example.com": (),
        "root@example.com": ("swarm-admins@example.com", ENG_GROUP),
    }


@pytest.fixture
def api_context(db, tokens, group_map, objects, github):
    """test_issue_run_tick's context, with a settings key built at runtime so a
    workspace role grant is signed and honoured."""
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,), settings_key=secrets.token_hex(32)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


def _grant_admin(ctx, tenant_id: str, email: str) -> None:
    store = ctx.roles
    store.write(tenant_id=tenant_id, subject=email, role=roles.ADMIN, by="root@example.com",
                via="platform_admin", reason="", previous=store.newest_valid(tenant_id, email, fresh=True),
                action=roles.AUDIT_GRANTED, detail={})


def _stuck(client, db, objects, ctx, *, seconds_ago: float, plan=None, issue: str | None = None) -> dict:
    """A run whose APPROVED claim committed and whose workflow was never stored."""
    body = {"issue": issue} if issue else {}
    run = (client.post("/v1/runs", headers=auth_header("alice"), json=body).json()["run"]
           if issue else _create(client).json()["run"])
    _finish_planner(db, objects, run, plan or _plan("src/widgets/list.py"))
    _tick(client)
    assert _stored(db, run["id"])["state"] == "PLANNED"
    stored = _stored(db, run["id"])
    IssueRuns(db, now=ctx.now).transition(
        "eng", run["id"], RunState.APPROVED, by="alice@example.com",
        patch={"approved_by": "alice@example.com",
               "approved_at": ctx.now() - timedelta(seconds=seconds_ago),
               "approved_digest": stored["plan_digest"]},
    )
    assert _stored(db, run["id"])["workflow_id"] is None
    return run


# --------------------------------------------------------------------------
# 1-3. the tick settles a run past the bound, and only past it
# --------------------------------------------------------------------------

def test_the_bound_is_longer_than_one_submission():
    # swarm-api's request timeout is 660 s (terraform): an in-flight
    # submission is never failed.
    assert BOUND >= 1800


def test_a_run_approved_past_the_bound_with_no_workflow_is_failed_by_one_tick(client, db, objects, api_context):
    run = _stuck(client, db, objects, api_context, seconds_ago=BOUND + 60)

    report = _tick(client).json()["report"]

    stored = _stored(db, run["id"])
    assert stored["state"] == "FAILED", stored.get("error")
    assert "never stored" in stored["error"]
    assert stored["history"][-1]["from"] == "APPROVED"
    assert stored["history"][-1]["by"] == runs_routes.SYSTEM == "swarm-api"
    assert report["moved"] == 1
    # FAILED creates no demand (invariant 1): nothing was submitted.
    assert not _docs(db, "workflows")


def test_a_failed_stuck_run_releases_its_territory(client, db, objects, api_context):
    first = _stuck(client, db, objects, api_context, seconds_ago=60)
    second = client.post("/v1/runs", headers=auth_header("alice"),
                         json={"issue": "example-org/widgets#7", "plan_approval": "auto"}).json()["run"]
    _finish_planner(db, objects, second, _plan("src/widgets/list.py", "src/widgets/sort.py"))

    _tick(client)
    held = _stored(db, second["id"])
    assert held["state"] == "PLANNED"
    assert held["hold"] == f"territory_overlap: {first['id']}"

    # The first run's submission is now past the bound.
    db.docs[f"issue_runs/{first['id']}"]["approved_at"] = api_context.now() - timedelta(seconds=BOUND + 1)
    _tick(client)
    assert _stored(db, first["id"])["state"] == "FAILED"
    _tick(client)

    released = _stored(db, second["id"])
    assert released["state"] == "RUNNING", released.get("error")
    assert released["hold"] is None


def test_a_run_approved_inside_the_bound_is_left_alone(client, db, objects, api_context, monkeypatch):
    run = _stuck(client, db, objects, api_context, seconds_ago=BOUND - 120)
    monkeypatch.setattr(runs_routes, "sync_issue", lambda ctx, r: r)
    before = copy.deepcopy(_stored(db, run["id"]))

    report = _tick(client).json()["report"]

    assert _stored(db, run["id"]) == before
    assert report["moved"] == 0 and report["failed"] == 0


# --------------------------------------------------------------------------
# 4. a lost RUNNING write is repaired from the run's own tenant only
# --------------------------------------------------------------------------

def _lost_running_write(client, db, objects, api_context) -> tuple[dict, str]:
    """An auto run that WAS submitted, whose RUNNING write never landed."""
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, _plan("src/widgets/list.py"))
    _tick(client)
    doc = _stored(db, run["id"])
    workflow_id = doc["workflow_id"]
    assert doc["state"] == "RUNNING" and workflow_id
    doc["state"] = "APPROVED"
    doc["workflow_id"] = None
    doc["history"] = doc["history"][:-1]
    doc["approved_at"] = api_context.now() - timedelta(seconds=BOUND + 60)
    return run, workflow_id


def test_a_linked_workflow_in_the_runs_tenant_is_adopted(client, db, objects, api_context):
    run, workflow_id = _lost_running_write(client, db, objects, api_context)
    linked = [t for t in _docs(db, "tasks").values()
              if isinstance((t.get("metadata") or {}).get("issue_run"), dict)
              and t["metadata"]["issue_run"].get("run_id") == run["id"]]
    assert linked and {t.get("workflow_id") for t in linked} == {workflow_id}

    _tick(client)

    stored = _stored(db, run["id"])
    assert stored["state"] == "RUNNING", stored.get("error")
    assert stored["workflow_id"] == workflow_id
    assert stored["history"][-1]["by"] == "swarm-api"
    # Nothing new was submitted: the one workflow is the adopted one.
    assert set(_docs(db, "workflows")) == {f"workflows/{workflow_id}"}


def test_a_linked_task_in_another_tenant_is_never_adopted(client, db, objects, api_context):
    run, workflow_id = _lost_running_write(client, db, objects, api_context)
    for task in _docs(db, "tasks").values():
        if task.get("workflow_id") == workflow_id:
            task["tenant_id"] = "research"

    _tick(client)

    stored = _stored(db, run["id"])
    assert stored["state"] == "FAILED"
    assert stored["workflow_id"] is None
    assert "never stored" in stored["error"]


# --------------------------------------------------------------------------
# 5. an admin ends one
# --------------------------------------------------------------------------

def _end(client, run_id: str, user: str, **body):
    return client.post(f"/v1/runs/{run_id}:end", headers=auth_header(user), json=body)


def test_the_workspace_admin_ends_a_stuck_run_with_a_reason(client, db, objects, api_context):
    _grant_admin(api_context, "eng", ADMIN)
    run = _stuck(client, db, objects, api_context, seconds_ago=30)

    response = _end(client, run["id"], "dave", reason="  the submission died in a deploy  ")

    assert response.status_code == 200, response.text
    assert response.json()["run"]["state"] == "FAILED"
    stored = _stored(db, run["id"])
    assert stored["state"] == "FAILED"
    assert "the submission died in a deploy" in stored["error"]
    assert ADMIN in stored["error"]
    assert stored["history"][-1]["by"] == ADMIN


def test_a_platform_admin_ends_a_stuck_run(client, db, objects, api_context):
    run = _stuck(client, db, objects, api_context, seconds_ago=30)

    response = _end(client, run["id"], "root", reason="cleared after the outage")

    assert response.status_code == 200, response.text
    assert _stored(db, run["id"])["error"].startswith("ended by root@example.com: cleared")


def test_a_plain_member_cannot_end_a_run(client, db, objects, api_context):
    run = _stuck(client, db, objects, api_context, seconds_ago=30)

    response = _end(client, run["id"], "alice", reason="stuck")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert _stored(db, run["id"])["state"] == "APPROVED"


def test_only_an_approved_run_with_no_workflow_can_be_ended(client, db, objects, api_context):
    _grant_admin(api_context, "eng", ADMIN)
    planned = _create(client).json()["run"]
    _finish_planner(db, objects, planned, _plan("src/widgets/list.py"))
    _tick(client)
    assert _stored(db, planned["id"])["state"] == "PLANNED"

    refused = _end(client, planned["id"], "dave", reason="stuck")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "invalid_run_transition"
    assert _stored(db, planned["id"])["state"] == "PLANNED"

    running = client.post("/v1/runs", headers=auth_header("alice"),
                          json={"issue": "example-org/widgets#7", "plan_approval": "auto"}).json()["run"]
    _finish_planner(db, objects, running, _plan("src/other.py"))
    _tick(client)
    assert _stored(db, running["id"])["state"] == "RUNNING"
    refused = _end(client, running["id"], "dave", reason="stuck")
    assert refused.status_code == 409, refused.text
    assert _stored(db, running["id"])["state"] == "RUNNING"


def test_another_tenants_run_is_not_found_and_a_reason_is_required(client, db, objects, api_context):
    _grant_admin(api_context, "eng", ADMIN)
    run = _stuck(client, db, objects, api_context, seconds_ago=30)

    assert _end(client, run["id"], "bob", reason="stuck").status_code == 404
    assert _end(client, run["id"], "dave").status_code == 422
    assert _end(client, run["id"], "dave", reason="   ").status_code == 422
    assert _end(client, run["id"], "dave", reason="x" * 501).status_code == 422
    assert _stored(db, run["id"])["state"] == "APPROVED"


# --------------------------------------------------------------------------
# 6. an auto-approval re-checks the hold inside the claim
# --------------------------------------------------------------------------

SECURITY_HOLD = {"code": issueruns.HOLD_NEEDS_OWNER, "approvers": issueruns.APPROVERS_OWNER_ONLY,
                 "reasons": [issueruns.STOP_SECURITY], "matched": ["label:security"]}


def test_a_blocking_hold_written_before_the_claim_stops_the_auto_approval(
    client, db, objects, api_context, monkeypatch,
):
    assert approvals.hold_blocks(SECURITY_HOLD)
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, _plan("src/widgets/list.py"))
    submitted: list[str] = []
    real_submit = api_context.submissions.submit_workflow
    real_gate = api_context.submissions.workspace_gate

    def submit(auth, spec):
        submitted.append(auth.email)
        return real_submit(auth, spec)

    def gate(auth, tenant):
        # After the tick's read and `_approve`'s pre-claim check, before the
        # claim's transaction: the hold lands on the document, signed.
        IssueRuns(db, now=api_context.now).patch(
            "eng", run["id"], {"approval_hold": SECURITY_HOLD},
            sign=runs_routes.owner_resigner(api_context),
        )
        return real_gate(auth, tenant)

    monkeypatch.setattr(api_context.submissions, "submit_workflow", submit)
    monkeypatch.setattr(api_context.submissions, "workspace_gate", gate)

    _tick(client)

    stored = _stored(db, run["id"])
    assert stored["state"] == "PLANNED", stored.get("error")
    assert stored["approval_hold"]["code"] == issueruns.HOLD_NEEDS_OWNER
    assert submitted == []
    assert not _docs(db, "workflows")


def test_the_claim_refuses_an_auto_approval_whatever_the_hold_was_when_read(
    client, db, objects, api_context, monkeypatch,
):
    """The claim checks `hold_blocks` on the document it read for EVERY
    auto-approval, not only when the hold differs from the one read before
    it: the pre-claim check is stepped round here, as a stale read would."""
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, _plan("src/widgets/list.py"))
    IssueRuns(db, now=api_context.now).patch(
        "eng", run["id"], {"approval_hold": SECURITY_HOLD},
        sign=runs_routes.owner_resigner(api_context),
    )
    _tick(client)  # PLANNING -> PLANNED, held
    held = IssueRuns(db).get("eng", run["id"])
    assert held.state == RunState.PLANNED and held.approval_hold
    monkeypatch.setattr(approvals, "check_hold", lambda *a, **k: None)
    submitted: list[str] = []
    monkeypatch.setattr(api_context.submissions, "submit_workflow",
                        lambda auth, spec: submitted.append(auth.email))

    with pytest.raises(approvals.HoldApproverRequired):
        runs_routes._approve(api_context, runs_routes.run_owner_auth(api_context, held), "eng", held,
                             digest=held.plan_digest, by=issueruns.AUTO_APPROVER)

    assert _stored(db, run["id"])["state"] == "PLANNED"
    assert submitted == []
