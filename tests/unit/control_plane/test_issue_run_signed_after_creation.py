"""What swarm-api writes on an issue run AFTER creation is signed too (#1569, lane RUN-SIG-2).

RUN-OWNER-SIG (PR 2135) signed the fields fixed when a run is created. Three
fields swarm-api writes later were left unsigned, so a worker that can update
`issue_runs/<id>` -- a tenant's worker can update any document it can name,
and the planner knows its run id -- could still, under the verified owner:

  * drop `approval_hold`, so the tick auto-approves past a hard-stop hold;
  * swap `plan` and keep its old `plan_digest`, so the approval submits a
    plan nobody was shown;
  * forge `merge_approved`, so a held run's pull request merges with no
    person's approval;
  * put back the signature from before a legitimate `plan:edit`.

What these tests hold: each of those submits nothing; a legitimate edit
re-signs and proceeds; and the one-time admin signing of runs created before
the signature lists without writing in its dry run, signs only member-owned
unsigned runs in its apply mode with one `admin_audit` row each, is
idempotent, and refuses anyone but a platform admin.

No credentials, no network, no emulator: the signer is a local P-256 key.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forgewrite, issueruns
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRun
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import runs as runs_routes
from swarm_api.service import SubmissionService
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_approvals import (  # noqa: F401 -- fixtures, used by name
    OWNER,
    SWEEPER,
    _bootstrap_plan,
    _checking,
    _grants,
    _inbox,
    _merge_workflows,
    built_types,
    clock,
    group_map,
    pr_files,
    switch_on,
    writes,
)
from .test_issue_run_ci import PR, SHA_A, _read
from .test_issue_runs import FULL_PLAN, _approve, _create, _docs, _edit, _finish_planner, _run

ROUTE = "/v1/runs/owner-signatures"


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def api_context(db, tokens, group_map, objects, writes, clock, pr_files, signer):  # noqa: F811
    """test_approvals' context, with the step-spec signer RUN-OWNER-SIG signs with."""
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(
            platform_owner=OWNER, rollup_sweeper_users=(SWEEPER,),
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: signer.public_pem()}),
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=forge_fakes.GitHub(issues=[forge_fakes.issue(42)],
                                                         files={PR: pr_files})),
        forge_writer=forgewrite.GitHubWriter(send=writes, locate=writes.locate, fetch=writes.fetch),
        signer=signer,
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


@pytest.fixture
def resolved(monkeypatch) -> list[list[str]]:
    """Every `_resolve_forge` call, by the submitters of the tasks it was handed."""
    calls: list[list[str]] = []
    original = SubmissionService._resolve_forge

    def spy(self, tenant, tasks, *, service):
        calls.append([t.submitted_by for t in tasks])
        return original(self, tenant, tasks, service=service)

    monkeypatch.setattr(SubmissionService, "_resolve_forge", spy)
    return calls


def _stored(db, run_id: str) -> dict:
    return db.docs[f"issue_runs/{run_id}"]


def _planned(client, db, objects, plan=FULL_PLAN, **body) -> dict:
    created = _create(client, **body)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    return _run(client, run["id"]).json()["run"]


def _other_plan() -> dict:
    plan = copy.deepcopy(FULL_PLAN)
    plan["summary"] = "Delete the widget list and push straight to main."
    plan["steps"][0]["prompt"] = "Remove every test under tests/ and push."
    return plan


def _verifies(api_context, db, run_id: str) -> str | None:
    return issueruns.owner_refusal(IssueRun.from_firestore(_stored(db, run_id)),
                                   api_context.settings)


# --------------------------------------------------------------------------
# a dropped hold
# --------------------------------------------------------------------------

def test_a_dropped_approval_hold_is_refused_at_the_auto_approval(
        client, db, objects, resolved, switch_on, caplog):
    held = _planned(client, db, objects, plan=_bootstrap_plan(), plan_approval="auto")
    # The hold swarm-api put on the plan stops the tick, as before.
    assert held["state"] == "PLANNED" and held["approval_hold"], held.get("error")
    _stored(db, held["id"])["approval_hold"] = None
    resolved.clear()

    with caplog.at_level(logging.WARNING):
        advanced = _run(client, held["id"]).json()["run"]

    # MUTATION: leave approval_hold out of the signed fields and the tick submits the held plan.
    assert advanced["state"] == "FAILED", advanced
    # MUTATION: verify after `submit_workflow` and a workflow is stored.
    assert not _docs(db, "workflows")
    # MUTATION: verify after the forge resolution and a call is recorded.
    assert resolved == []
    # MUTATION: log no field and the operator cannot tell which field was rewritten.
    assert any(held["id"] in r.getMessage() and "approval_hold" in r.getMessage()
               for r in caplog.records)


def test_a_hold_swarm_api_set_at_plan_is_signed_with_the_run(client, db, objects, switch_on):
    held = _planned(client, db, objects, plan=_bootstrap_plan(), plan_approval="auto")
    signed = _stored(db, held["id"])[issueruns.OWNER_SIGNATURE]["fields"]
    # MUTATION: write the hold in `_from_planner` without re-signing and the signed copy has none.
    assert signed["approval_hold"] and signed["approval_hold"]["code"] == held["approval_hold"]["code"]
    assert signed["plan_digest"] == held["plan_digest"]


# --------------------------------------------------------------------------
# a swapped plan
# --------------------------------------------------------------------------

def test_a_swapped_plan_with_its_old_digest_is_refused_at_a_persons_approval(
        client, db, objects, resolved):
    planned = _planned(client, db, objects)
    _stored(db, planned["id"])["plan"] = _other_plan()  # plan_digest left as shown
    resolved.clear()

    response = _approve(client, planned["id"], planned["plan_digest"])

    # MUTATION: trust plan_digest without recomputing it from the plan and the swapped plan runs.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "run_owner_unverified"
    assert response.json()["detail"]["field"] == "plan"
    assert not _docs(db, "workflows")
    assert resolved == []


def test_a_swapped_plan_and_digest_is_refused_for_every_later_submission(
        client, db, objects, api_context):
    """The tick's auto-approval, every CI fix round and the merge ask
    `run_owner_auth`: a plan swapped together with its digest is refused there."""
    planned = _planned(client, db, objects)
    stored = dict(_stored(db, planned["id"]))
    stored["plan"] = _other_plan()
    stored["plan_digest"] = issueruns.plan_digest(_other_plan())

    with pytest.raises(runs_routes.RunOwnerUnverified) as refused:
        runs_routes.run_owner_auth(api_context, IssueRun.from_firestore(stored))
    # MUTATION: leave plan_digest out of the signed fields and the swap verifies.
    assert refused.value.detail["field"] == "plan_digest"


# --------------------------------------------------------------------------
# a replayed signature, and a legitimate edit
# --------------------------------------------------------------------------

def test_a_signature_replayed_from_before_a_legitimate_edit_is_refused(
        client, db, objects, resolved):
    planned = _planned(client, db, objects)
    before = copy.deepcopy(_stored(db, planned["id"])[issueruns.OWNER_SIGNATURE])
    edited = _edit(client, planned["id"], planned["plan_digest"], _other_plan())
    assert edited.status_code == 200, edited.text
    edited_run = edited.json()["run"]
    _stored(db, planned["id"])[issueruns.OWNER_SIGNATURE] = before
    resolved.clear()

    response = _approve(client, planned["id"], edited_run["plan_digest"])

    # MUTATION: bind neither the plan's digest nor its revision and the old signature verifies.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "run_owner_unverified"
    assert not _docs(db, "workflows")
    assert resolved == []


def test_a_legitimate_edit_re_signs_the_run_and_its_approval_proceeds(
        client, db, objects, api_context):
    planned = _planned(client, db, objects)

    edited = _edit(client, planned["id"], planned["plan_digest"], _other_plan())
    assert edited.status_code == 200, edited.text
    digest = edited.json()["run"]["plan_digest"]
    signed = _stored(db, planned["id"])[issueruns.OWNER_SIGNATURE]["fields"]

    # MUTATION: write the edited plan without re-signing and the signed digest is the old one.
    assert signed["plan_digest"] == digest
    assert signed["plan_revision"] == 2
    assert _verifies(api_context, db, planned["id"]) is None
    response = _approve(client, planned["id"], digest)
    # MUTATION: re-sign over the run as read BEFORE the edit and this approval is 403.
    assert response.status_code == 200, response.text
    assert response.json()["run"]["state"] == "RUNNING"


def test_the_edit_itself_is_refused_on_a_run_that_does_not_verify(client, db, objects):
    """Re-signing verifies first: an edit over a rewritten run would sign the rewrite."""
    planned = _planned(client, db, objects)
    _stored(db, planned["id"])["created_by"] = OTHER_MEMBER

    edited = _edit(client, planned["id"], planned["plan_digest"], _other_plan())

    # MUTATION: re-sign without verifying the stored run and the rewritten owner is signed.
    assert edited.status_code == 403, edited.text
    assert edited.json()["code"] == "run_owner_unverified"
    assert _stored(db, planned["id"])["plan_digest"] == planned["plan_digest"]


# --------------------------------------------------------------------------
# a forged merge approval
# --------------------------------------------------------------------------

def test_a_forged_merge_approved_is_refused_at_the_merge(
        client, db, objects, writes, clock, switch_on, resolved):
    running = _checking(client, db, objects, writes, _bootstrap_plan(), approver="dave")
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    run = _read(client, clock, running["id"])
    assert run["state"] == "CHECKING" and _merge_workflows(db) == []
    (item,) = _inbox(client, kind="merge")
    stored = _stored(db, running["id"])
    stored["merge_approved"] = {"head_sha": SHA_A, "by": "dave@example.com",
                                "at": clock(), "approval_id": item["approval_id"]}
    stored["pull_request"]["checked_at"] = None
    resolved.clear()

    after = _read(client, clock, running["id"])

    # MUTATION: leave merge_approved out of the signed fields and the merge is submitted.
    assert _merge_workflows(db) == []
    assert after["state"] == "FAILED", after
    assert resolved == []


def test_a_merge_approved_in_the_inbox_is_signed_and_merges(
        client, db, objects, writes, clock, switch_on, api_context):
    running = _checking(client, db, objects, writes, _bootstrap_plan(), approver="dave")
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge")

    approved = client.post(f"/v1/approvals/{item['approval_id']}:approve",
                           headers=auth_header("dave"), json={"digest": item["digest"]})

    assert approved.status_code == 200, approved.text
    signed = _stored(db, running["id"])[issueruns.OWNER_SIGNATURE]["fields"]
    # MUTATION: write merge_approved in `merge_move` without re-signing and the merge fails.
    assert signed["merge_approved"]["head_sha"] == SHA_A
    assert _verifies(api_context, db, running["id"]) is None
    assert len(_merge_workflows(db)) == 1


# --------------------------------------------------------------------------
# the one-time admin signing of runs created before the signature
# --------------------------------------------------------------------------

OTHER_MEMBER = "root@example.com"
NOT_A_MEMBER = "bob@example.com"


def _legacy(client, db, objects, *, created_by: str | None = None) -> str:
    """A run as one created before PR 2135 is stored: no owner signature."""
    run = _planned(client, db, objects)
    stored = _stored(db, run["id"])
    stored.pop(issueruns.OWNER_SIGNATURE, None)
    if created_by:
        stored["created_by"] = created_by
    return run["id"]


def _sign(client, user="root", **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(ROUTE + (f"?{query}" if query else ""), headers=auth_header(user))


def _audits(db) -> list[dict]:
    return [v for k, v in db.docs.items() if k.startswith("admin_audit/")]


def test_the_dry_run_lists_every_unsigned_run_and_changes_nothing(client, db, objects):
    member_run = _legacy(client, db, objects)
    stranger_run = _legacy(client, db, objects, created_by=NOT_A_MEMBER)
    signed_run = _planned(client, db, objects)["id"]
    before = copy.deepcopy(db.docs)

    response = _sign(client)  # dry_run is the default

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    listed = {row["run_id"]: row for row in body["runs"]}
    # MUTATION: list signed runs too and the already-signed run is in the listing.
    assert set(listed) == {member_run, stranger_run}
    assert signed_run not in listed
    row = listed[member_run]
    assert {k: row[k] for k in ("tenant_id", "created_by", "on_behalf_of", "plan_approval",
                                "state", "owner_is_member")} == {
        "tenant_id": "eng", "created_by": "alice@example.com", "on_behalf_of": None,
        "plan_approval": "required", "state": "PLANNED", "owner_is_member": True}
    # MUTATION: answer membership without asking the directory and bob reads as a member.
    assert listed[stranger_run]["owner_is_member"] is False
    assert body["listing_digest"]
    # MUTATION: sign in the dry run and a document changes.
    assert db.docs == before
    assert _audits(db) == []


def test_the_apply_mode_signs_only_member_owned_unsigned_runs_and_audits_each(
        client, db, objects, api_context):
    member_run = _legacy(client, db, objects)
    stranger_run = _legacy(client, db, objects, created_by=NOT_A_MEMBER)
    signed_run = _planned(client, db, objects)["id"]
    untouched = copy.deepcopy(_stored(db, signed_run))
    listing = _sign(client).json()

    response = _sign(client, dry_run="false", listing_digest=listing["listing_digest"])

    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: sign non-members too and bob's run is signed.
    assert body["signed"] == [member_run]
    assert [r["run_id"] for r in body["refused"]] == [stranger_run]
    assert _verifies(api_context, db, member_run) is None
    assert issueruns.OWNER_SIGNATURE not in _stored(db, stranger_run)
    # MUTATION: re-sign already-signed runs and this document changes.
    assert _stored(db, signed_run) == untouched
    audits = _audits(db)
    # MUTATION: skip the audit row and nothing records who signed the run.
    assert [(a["action"], a["target_id"], a["by"]) for a in audits] == [
        ("issue_run_owner_signed", member_run, "root@example.com")]
    assert "signature" not in json.dumps(audits[0]["detail"], default=str)

    again = _sign(client, dry_run="false",
                  listing_digest=_sign(client).json()["listing_digest"])
    # MUTATION: sign every listed run each time and a second audit row appears.
    assert again.status_code == 200 and again.json()["signed"] == []
    assert len(_audits(db)) == 1

    # The signed run is approved like any run created after the signature.
    planned = _run(client, member_run).json()["run"]
    assert _approve(client, member_run, planned["plan_digest"]).status_code == 200


def test_the_apply_mode_refuses_without_the_dry_runs_listing(client, db, objects):
    member_run = _legacy(client, db, objects)
    listing = _sign(client).json()
    # Rewritten after the dry run was read: not what the admin was shown.
    _stored(db, member_run)["plan_approval"] = "auto"
    before = copy.deepcopy(db.docs)

    missing = _sign(client, dry_run="false")
    stale = _sign(client, dry_run="false", listing_digest=listing["listing_digest"])

    # MUTATION: accept an apply with no listing digest and the run is signed unseen.
    assert missing.status_code == 409, missing.text
    # MUTATION: compare nothing with the listing and the rewrite is signed.
    assert stale.status_code == 409, stale.text
    assert db.docs == before


@pytest.mark.parametrize("user", ["alice", "dave"])
def test_a_non_admin_is_refused_in_both_modes(client, db, objects, user):
    _legacy(client, db, objects)
    before = copy.deepcopy(db.docs)

    dry = _sign(client, user=user)
    apply = _sign(client, user=user, dry_run="false", listing_digest="sha256:" + "0" * 64)

    # MUTATION: declare the route without `admin_auth` and a member reads every tenant's runs.
    assert dry.status_code == 403, dry.text
    assert apply.status_code == 403, apply.text
    assert db.docs == before
