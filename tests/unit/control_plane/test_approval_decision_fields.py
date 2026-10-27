"""What the Automate decision page reads, and who may reject (AUTO-API-APPROVALS, part of #2417).

BGD picked the Automate design on 2026-10-26: A4 (the approvals list) from
proposal A, A5 (one approval) from proposal B. Their "new fields" lists name
what the API did not serve; this lane serves them, read-only:

  * `may_decide {approve, reject, why, deciders}` for the reader, on the list
    and the detail, computed by the decision routes' own checks -- and it
    agrees with what the route then does;
  * `superseded_by {approval_id, head_sha, at}` and `head_sha_now` on a
    superseded merge (A5e);
  * `merge.checks {passed, failed, pending}` at the head the approval names
    (B's A5 card 1), absent once the head has moved;
  * `reason_files` on a hold: which matched file each reason came from (B's
    A5 card 2), absent for a reason no file pairs with by path.

And BGD's rule change (A5f): ONLY SOMEONE WHO MAY APPROVE AN ITEM MAY REJECT
IT. A member the approve would refuse is 403 `reject_approver_required` on
`:reject`, behind its refusal switch, which ships on; off, any member rejects,
as before.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest

from swarm_api import approvals, forge, forgewrite, refusals
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_grant, seed_tenant
from .test_approvals import (  # noqa: F401  (fixtures)
    ADMIN,
    FULL_PLAN,
    OWNER,
    SWEEPER,
    _bootstrap_plan,
    _checking,
    _decide,
    _firing,
    _grants,
    _inbox,
    _planned,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    switch_on,
    writes,
)
from .test_issue_run_ci import PR, SHA_A, SHA_B, _read
from .test_schedule_tick import seed_schedule

REJECT_SWITCH = refusals.env_name("reject_approver_required")
SCHEDULE = "sch_000000000001"


@pytest.fixture
def api_context(db, tokens, group_map, objects, writes, clock, pr_files):
    """test_approvals' context with a settings key, so a test can name a
    workspace admin: a role grant is signed, and an unsigned one is refused."""
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(platform_owner=OWNER, rollup_sweeper_users=(SWEEPER,),
                              settings_key=secrets.token_hex(32)),
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
    )


def _detail(client, item_id, user="alice") -> dict:
    response = client.get(f"/v1/approvals/{item_id}", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def _named_firing(db, approvers=("dave@example.com",)) -> None:
    seed_tenant(db, "eng")  # membership is asked of the directory, through the tenant's group
    seed_schedule(db, gate={"run": "approve", "approvers": list(approvers)})
    _firing(db, f"{SCHEDULE}:1")


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------

def test_the_reject_rule_is_a_switch_that_ships_on():
    switch = refusals.SWITCHES["reject_approver_required"]
    assert switch.default is True
    assert switch.env == REJECT_SWITCH == "REFUSAL_REJECT_APPROVER_REQUIRED"
    assert refusals.enforced("reject_approver_required", environ={})
    assert not refusals.enforced("reject_approver_required", environ={REJECT_SWITCH: "off"})
    assert approvals.RejectApproverRequired.code == "reject_approver_required"
    assert approvals.RejectApproverRequired.status_code == 403


# --------------------------------------------------------------------------
# may_decide, and the reject rule it reads out
# --------------------------------------------------------------------------

def test_any_member_may_decide_an_item_of_the_members_rule(client, db):
    seed_schedule(db, gate={"run": "approve"})
    _firing(db, f"{SCHEDULE}:1")
    (item,) = _inbox(client)
    assert item["may_decide"] == {"approve": True, "reject": True, "why": None, "deciders": ["members"]}
    assert _detail(client, item["approval_id"])["approval"]["may_decide"] == item["may_decide"]


def test_a_member_off_a_named_list_may_not_approve_and_now_may_not_reject(client, db):
    _named_firing(db)
    (item,) = _inbox(client)
    seen = item["may_decide"]
    assert seen["approve"] is False and seen["reject"] is False
    assert "named approvers" in seen["why"]
    assert seen["deciders"] == ["dave@example.com"]

    # The route agrees with what was read out: the approve and the reject are refused.
    assert _decide(client, item["approval_id"], "approve", digest="digest-1").status_code == 403
    refused = _decide(client, item["approval_id"], "reject", reason="not this week")
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "reject_approver_required"
    assert refused.json()["detail"]["approve_refusal"] == "approver_not_allowed"
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "pending"
    assert db.docs[f"schedule_firings/{SCHEDULE}:1"]["state"] == "awaiting_approval"


def test_an_approver_on_the_list_may_reject(client, db):
    _named_firing(db)
    (item,) = _inbox(client, user="dave")
    assert item["may_decide"]["approve"] is True and item["may_decide"]["reject"] is True
    response = _decide(client, item["approval_id"], "reject", user="dave", reason="not this week")
    assert response.status_code == 200, response.text
    assert db.docs[f"schedule_firings/{SCHEDULE}:1"]["state"] == "rejected"


def test_with_the_switch_off_any_member_rejects_as_before(client, db, monkeypatch):
    monkeypatch.setenv(REJECT_SWITCH, "off")
    _named_firing(db)
    (item,) = _inbox(client)
    assert item["may_decide"]["approve"] is False and item["may_decide"]["reject"] is True
    response = _decide(client, item["approval_id"], "reject", reason="not this week")
    assert response.status_code == 200, response.text
    assert db.docs[f"schedule_firings/{SCHEDULE}:1"]["state"] == "rejected"


def test_a_held_plans_creator_may_not_reject_it_and_a_second_member_may(client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    item_id = f"run:{run['id']}"
    (item,) = _inbox(client)
    assert item["may_decide"]["approve"] is False and item["may_decide"]["reject"] is False
    assert item["may_decide"]["deciders"] == ["second_member", "workspace_admins"]

    refused = _decide(client, item_id, "reject", reason="out of scope")
    assert refused.status_code == 403 and refused.json()["code"] == "reject_approver_required"
    assert refused.json()["detail"]["approve_refusal"] == "hold_approver_required"
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"

    (other,) = _inbox(client, user="dave")
    assert other["may_decide"]["approve"] is True and other["may_decide"]["reject"] is True
    rejected = _decide(client, item_id, "reject", user="dave", reason="out of scope")
    assert rejected.status_code == 200, rejected.text
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "REJECTED"

    # BGD 2026-10-26, "let an admin approve it, no need for two approvers":
    # once alice is eng's admin, her own held run is hers to reject.
    again = _planned(client, db, objects, _bootstrap_plan())
    _name_admin(client, "alice@example.com")
    (mine,) = _inbox(client)
    assert mine["may_decide"]["approve"] is True and mine["may_decide"]["reject"] is True
    assert mine["may_decide"]["why"] is None
    assert _decide(client, f"run:{again['id']}", "reject", reason="out of scope").status_code == 200
    assert db.docs[f"issue_runs/{again['id']}"]["state"] == "REJECTED"


def _name_admin(client, email: str, tenant: str = "eng") -> None:
    """`root` (a platform admin) names `email` an admin of the workspace."""
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=auth_header("root"),
                          json={"email": email, "role": "admin"})
    assert response.status_code == 200, response.text


def test_a_workspace_admin_approves_their_own_held_run_alone(client, db, objects, switch_on):
    """Under a `second_member` hold a workspace admin needs no second member,
    even as the run's creator (BGD 2026-10-26)."""
    _name_admin(client, "alice@example.com")
    run = _planned(client, db, objects, _bootstrap_plan())
    assert run["approval_hold"]["approvers"] == "second_member"
    (item,) = _inbox(client)
    seen = item["may_decide"]
    assert seen["approve"] is True and seen["reject"] is True and seen["why"] is None
    assert seen["deciders"] == ["second_member", "workspace_admins"]
    approved = _decide(client, item["approval_id"], "approve", digest=run["plan_digest"])
    assert approved.status_code == 200, approved.text
    assert db.docs[f"issue_runs/{run['id']}"]["approved_by"] == "alice@example.com"


def test_a_non_admin_creator_may_neither_approve_nor_reject_and_is_told_an_admin_may(
        client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    (item,) = _inbox(client)
    seen = item["may_decide"]
    assert seen["approve"] is False and seen["reject"] is False
    assert "workspace admin" in seen["why"]
    refused = _decide(client, item["approval_id"], "approve", digest=run["plan_digest"])
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"
    assert "workspace admin" in refused.json()["message"]
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"


def test_a_platform_admin_acting_in_the_workspace_approves_their_own_held_run(client, db, objects, switch_on):
    """erin is a platform admin, not the owner, and holds no workspace grant."""
    seed_grant(db, "eng", ADMIN, "example-org/widgets")
    run = _planned(client, db, objects, _bootstrap_plan(), user="erin")
    (item,) = _inbox(client, user="erin")
    assert item["may_decide"]["approve"] is True and item["may_decide"]["reject"] is True
    approved = _decide(client, item["approval_id"], "approve", user="erin", digest=run["plan_digest"])
    assert approved.status_code == 200, approved.text


def test_a_workspace_admin_of_another_workspace_is_still_excluded(client, db, objects, switch_on):
    """Admin of `research` is not admin of `eng`: alice's eng run keeps the rule."""
    _name_admin(client, "alice@example.com", tenant="research")
    run = _planned(client, db, objects, _bootstrap_plan())
    (item,) = _inbox(client)
    assert item["may_decide"]["approve"] is False
    refused = _decide(client, item["approval_id"], "approve", digest=run["plan_digest"])
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"


def test_an_unreadable_role_admits_nobody_the_hold_excludes(db, monkeypatch):
    """The admin exception fails closed: a role that cannot be read is not admin."""
    from swarm_api.roles import RoleUnavailable

    class Store:
        def person_role(self, *a, **k):
            raise RoleUnavailable("unreadable")

    class Authenticator:
        def holds_platform_admin(self, *a, **k):
            return False

    class Ctx:
        roles = Store()
        authenticator = Authenticator()

    assert approvals.workspace_admin(Ctx(), "eng", "alice@example.com") is False
    assert approvals.workspace_admin(Ctx(), "eng", approvals.AUTO_APPROVER) is False


def test_a_report_only_hold_refuses_neither_the_approve_nor_the_reject(client, db, objects):
    """The hold's switch is off: the approve would go through, so the reject does."""
    run = _planned(client, db, objects, _bootstrap_plan())
    (item,) = _inbox(client)
    assert item["may_decide"]["approve"] is True and item["may_decide"]["reject"] is True
    assert _decide(client, f"run:{run['id']}", "reject", reason="out of scope").status_code == 200


def test_a_platform_scope_item_says_only_a_platform_admin_decides(client, db):
    seed_schedule(db, gate={"run": "approve"})
    db.docs[f"schedules/{SCHEDULE}"]["scope"] = {"mode": "platform"}
    _firing(db, f"{SCHEDULE}:1")
    (item,) = _inbox(client)
    seen = item["may_decide"]
    assert seen["approve"] is False and seen["reject"] is False
    assert "platform admin" in seen["why"]
    assert seen["deciders"] == ["platform_admins"]
    (root,) = _inbox(client, user="root")
    assert root["may_decide"]["approve"] is True and root["may_decide"]["reject"] is True


def test_a_decided_item_leaves_nothing_to_decide(client, db):
    seed_schedule(db, gate={"run": "approve"})
    _firing(db, f"{SCHEDULE}:1")
    (item,) = _inbox(client)
    assert _decide(client, item["approval_id"], "approve", digest="digest-1").status_code == 200
    seen = _detail(client, item["approval_id"], user="dave")["approval"]["may_decide"]
    assert seen["approve"] is False and seen["reject"] is False
    assert "already approved" in seen["why"]


def test_a_rejection_of_a_decided_item_is_still_409_not_a_who_refusal(client, db):
    _named_firing(db)
    (item,) = _inbox(client, user="dave")
    assert _decide(client, item["approval_id"], "reject", user="dave", reason="no").status_code == 200
    again = _decide(client, item["approval_id"], "reject", reason="no")
    assert again.status_code == 409 and again.json()["code"] == "already_decided"


# --------------------------------------------------------------------------
# merge.checks, superseded_by, head_sha_now
# --------------------------------------------------------------------------

def _merge_item(client, db, objects, writes, clock) -> tuple[dict, dict]:
    running = _checking(client, db, objects, writes, FULL_PLAN)
    db.docs[f"issue_runs/{running['id']}"]["merge_approval"] = "required"
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge")
    return running, item


def test_a_merge_serves_its_check_counts_at_the_head_it_names(client, db, objects, writes, clock):
    running, item = _merge_item(client, db, objects, writes, clock)
    pull = db.docs[f"issue_runs/{running['id']}"]["pull_request"]
    # The reading the CI loop wrote beside its head (`issueci.check_summary`).
    assert pull["head_sha"] == SHA_A and pull["check_counts"]["passed"] == 1
    detail = _detail(client, item["approval_id"])
    assert detail["merge"]["checks"] == {"passed": 1, "failed": 0, "pending": 0}
    # The canonical example, #2417 A5: 10 of 10 passed.
    pull["check_counts"] = {"passed": 10, "failed": 0, "pending": 0, "skipped": 0}
    assert _detail(client, item["approval_id"])["merge"]["checks"] == {"passed": 10, "failed": 0, "pending": 0}
    assert detail["approval"]["head_sha_now"] == SHA_A
    assert "superseded_by" not in detail["approval"]

    # After a push the reading is the new head's: the counts are not this approval's.
    db.docs[f"issue_runs/{running['id']}"]["pull_request"]["head_sha"] = SHA_B
    moved = _detail(client, item["approval_id"])
    assert "checks" not in moved["merge"]
    assert moved["approval"]["head_sha_now"] == SHA_B


def test_a_superseded_merge_names_the_request_to_decide_instead(
    client, db, objects, writes, clock, switch_on,
):
    running = _checking(client, db, objects, writes, _bootstrap_plan(), approver="dave")
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (old,) = _inbox(client, kind="merge")

    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (new,) = _inbox(client, kind="merge")
    assert new["approval_id"] != old["approval_id"]

    detail = _detail(client, old["approval_id"])["approval"]
    assert detail["state"] == "superseded"
    assert detail["head_sha_now"] == SHA_B
    assert detail["superseded_by"] == {"approval_id": new["approval_id"], "head_sha": SHA_B,
                                       "at": new["requested_at"]}
    assert detail["may_decide"]["approve"] is False and "already superseded" in detail["may_decide"]["why"]


def test_superseded_by_is_absent_when_no_request_was_made_at_the_new_head(client, db, objects, writes, clock):
    running, item = _merge_item(client, db, objects, writes, clock)
    db.docs[f"issue_runs/{running['id']}"]["pull_request"]["head_sha"] = SHA_B
    db.docs[f"approvals/{item['approval_id']}"]["state"] = "superseded"
    detail = _detail(client, item["approval_id"])["approval"]
    assert detail["head_sha_now"] == SHA_B
    assert "superseded_by" not in detail


def test_another_tenants_request_is_never_named(db):
    doc = {"approval_id": "apr_x", "tenant_id": "eng", "kind": "merge", "state": "superseded",
           "subject": {"run_id": "run_1"}}
    newer = approvals.approval_id("merge", "run_1:" + SHA_B)
    db.docs[f"approvals/{newer}"] = {"approval_id": newer, "tenant_id": "research", "kind": "merge",
                                     "digest": {"head_sha": SHA_B}, "requested_at": datetime.now(timezone.utc)}
    assert approvals.superseded_by(db, doc, SHA_B) is None
    db.docs[f"approvals/{newer}"]["tenant_id"] = "eng"
    assert approvals.superseded_by(db, doc, SHA_B)["approval_id"] == newer


@pytest.mark.parametrize("pull", [
    None,
    {"head_sha": SHA_B, "check_counts": {"passed": 1, "failed": 0, "pending": 0}},
    {"head_sha": SHA_A},
    {"head_sha": SHA_A, "check_counts": {"passed": 1, "failed": 0}},
    {"head_sha": SHA_A, "check_counts": {"passed": "1", "failed": 0, "pending": 0}},
    {"head_sha": SHA_A, "check_counts": {"passed": True, "failed": 0, "pending": 0}},
])
def test_merge_checks_are_absent_rather_than_invented(pull):
    doc = {"kind": "merge", "digest": {"head_sha": SHA_A, "verdict": "MERGE"}}
    assert approvals.merge_checks(doc, pull) is None


def test_merge_checks_leave_out_the_skipped_bucket():
    doc = {"kind": "merge", "digest": {"head_sha": SHA_A, "verdict": "MERGE"}}
    pull = {"head_sha": SHA_A, "check_counts": {"passed": 10, "failed": 0, "pending": 0, "skipped": 2}}
    assert approvals.merge_checks(doc, pull) == {"passed": 10, "failed": 0, "pending": 0}


# --------------------------------------------------------------------------
# reason_files
# --------------------------------------------------------------------------

def test_reason_files_pairs_the_canonical_example():
    """#2417's A5 example: held NEEDS_OWNER for `workflows` and `shared`."""
    hold = {"code": "NEEDS_OWNER", "reasons": ["workflows", "shared"],
            "matched": [".github/workflows/unit-weights-refresh.yml", "scripts/lib/common.sh"]}
    assert approvals.reason_files(hold) == {
        "workflows": [".github/workflows/unit-weights-refresh.yml"], "shared": ["scripts/lib/common.sh"],
    }


def test_reason_files_reads_protected_paths_from_the_registration_and_the_issue_for_security():
    hold = {"reasons": ["iam", "protected_path", "security"],
            "matched": ["terraform/bootstrap/main.tf", "billing/rates.yaml", "label:security"]}
    assert approvals.reason_files(hold, ["billing/**", "terraform/bootstrap/**"]) == {
        "iam": ["terraform/bootstrap/main.tf"], "protected_path": ["billing/rates.yaml"],
        "security": ["label:security"],
    }
    # Without the registration, protected_path is left out, never guessed.
    assert "protected_path" not in approvals.reason_files(hold)


def test_a_reason_no_file_pairs_with_by_path_is_left_out():
    """An IAM stop found in a `.tf` file's patch matches no IAM path rule."""
    hold = {"reasons": ["iam"], "matched": ["terraform/modules/tenancy/main.tf"]}
    assert approvals.reason_files(hold) is None
    assert "reason_files" not in approvals.with_reason_files(hold)


def test_a_held_plans_detail_pairs_its_reasons(client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    detail = _detail(client, f"run:{run['id']}")["approval"]
    assert detail["hold"]["reason_files"] == {"iam": ["terraform/bootstrap/main.tf"]}


def test_a_held_merges_detail_pairs_its_reasons(client, db, objects, writes, clock, switch_on, pr_files):
    pr_files.append("terraform/bootstrap/deployer.tf")
    running = _checking(client, db, objects, writes, FULL_PLAN)
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge")
    detail = _detail(client, item["approval_id"])
    assert detail["merge"]["approval_hold"]["reason_files"] == {"iam": ["terraform/bootstrap/deployer.tf"]}
    assert detail["approval"]["hold"]["reason_files"] == {"iam": ["terraform/bootstrap/deployer.tf"]}


def test_deciders_name_the_narrowest_rule_that_applies():
    hold = {"code": "NEEDS_SECOND_MEMBER", "approvers": "second_member", "reasons": ["iam"], "matched": []}
    common = {"schedule": None, "merge_tier": False}
    assert approvals.deciders(None, "eng", kind="plan", approvers=None, hold=hold, platform_scope=False,
                              **common) == ["second_member", "workspace_admins"]
    assert approvals.deciders(None, "eng", kind="spec", approvers="members", hold=None, platform_scope=False,
                              **common) == ["platform_admins"]
    assert approvals.deciders(None, "eng", kind="run", approvers="owner_only", hold=None, platform_scope=False,
                              **common) == ["owner_only"]
    owner_hold = {"code": "NEEDS_OWNER", "approvers": "owner_only"}
    assert approvals.deciders(None, "eng", kind="merge", approvers=["dave@example.com"], hold=owner_hold,
                              platform_scope=False, **common) == ["owner_only"]
    assert approvals.deciders(None, "eng", kind="run", approvers=None, hold=None, platform_scope=True,
                              **common) == ["platform_admins"]
    assert approvals.deciders(None, "eng", kind="run", approvers="members", hold=None, platform_scope=False,
                              **common) == ["members"]


def test_a_merge_tier_named_list_leaves_out_the_schedules_last_editor(client, db):
    """SD5: alice created the schedule (its last gate editor), so of a named
    list of alice and dave only dave may decide its merge-tier items."""
    _named_firing(db, approvers=("alice@example.com", "Dave@example.com"))
    (item,) = _inbox(client, user="dave")
    assert item["may_decide"]["deciders"] == ["alice@example.com", "dave@example.com"]
    db.docs[f"approvals/{item['approval_id']}"]["merge_tier"] = True
    (tier,) = _inbox(client, user="dave")
    assert tier["may_decide"]["deciders"] == ["dave@example.com"]
    (alice,) = _inbox(client)
    assert alice["may_decide"]["approve"] is False and alice["may_decide"]["reject"] is False
    assert "dave@example.com" in alice["may_decide"]["why"].lower()
    refused = _decide(client, item["approval_id"], "reject", reason="no")
    assert refused.status_code == 403 and refused.json()["detail"]["approve_refusal"] == "second_person_required"
