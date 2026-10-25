"""#2259: an owner-only merge hold the owner last edited, and a hold the plan's prompt alone set.

Measured 2026-10-24 on `apr_76e9cdc4800ea95c5609fd9b` (run
`run_52b61f4ac2d74a01a6c9`, PR 2234): `owner_only` admitted only the
platform owner, and the second-person rule then refused the owner as the
schedule's last editor, so nobody could decide it. The hold itself had
matched `apps/common/swarm_common` and `.github/workflows` from the plan's
PROMPT, while the pull request changed neither.

Owner decision 2026-10-24, "Owner may decide, audited":

1. Where only the owner may decide, the owner decides as the last editor
   too, and the audit carries a `second_person_waived_owner` row beside the
   decision's. Everyone else, and a named list, keep the rule exactly.
2. At the merge point the hold's stops run again over the pull request's
   own files and patches; a hold the prompt alone set clears when none of
   it matches. A real match keeps it; a security-class hold never clears.
"""

from __future__ import annotations

import copy
import json

import pytest

from swarm_api import approvals, ghapprove, issueruns
from swarm_api.repositories import repo_id_for

from .conftest import auth_header, seed_tenant
from .test_github_comment_approvals import _command, _comment, _connect, _replies
from .test_approvals import (  # noqa: F401 -- the fixtures the client is built from
    OWNER,
    SWITCH,
    _audit,
    _decide,
    _firing,
    _inbox,
    _merge_workflows,
    _grants,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .test_issue_run_ci import PR, SHA_A, _read, _workflow_ends
from .test_issue_run_keyword import _review_writes, _verdict
from .test_issue_runs import FULL_PLAN, _approve, _create, _finish_planner, _run
from .test_schedule_tick import seed_schedule

SCHEDULE = "sch_000000000001"
DAVE = "dave@example.com"
#: The hold #2259 measured: NEEDS_OWNER for `contract` and `workflows`.
OWNER_HOLD = {"code": "NEEDS_OWNER", "approvers": "owner_only", "reasons": ["contract", "workflows"],
              "matched": ["apps/common/swarm_common", ".github/workflows"]}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


def _merge_tier_item(client, db, *, last_editor: str, approvers) -> dict:
    """A pending merge-tier firing approval on a schedule `last_editor` created."""
    seed_tenant(db, "eng")
    seed_schedule(db, owner=last_editor, gate={"run": "approve", "approvers": approvers})
    db.docs[f"schedules/{SCHEDULE}"]["created_by"] = last_editor
    _firing(db, f"{SCHEDULE}:7")
    (item,) = _inbox(client, user="root")
    db.docs[f"approvals/{item['approval_id']}"]["merge_tier"] = True
    return item


def _waivers(db) -> list[dict]:
    return [e for e in _audit(db, SCHEDULE) if e["action"] == "second_person_waived_owner"]


# --------------------------------------------------------------------------
# 1. the owner waiver
# --------------------------------------------------------------------------

def test_the_owner_decides_an_owner_only_merge_tier_item_the_owner_last_edited_and_the_audit_says_so(client, db):
    item = _merge_tier_item(client, db, last_editor=OWNER, approvers="owner_only")

    decided = _decide(client, item["approval_id"], "approve", user="root", digest="digest-1")

    # MUTATION: drop `owner_waiver=True` from the route and this is 403 second_person_required.
    assert decided.status_code == 200, decided.text
    actions = [e["action"] for e in _audit(db, SCHEDULE)]
    assert "approval_approved" in actions and "second_person_waived_owner" in actions
    (waiver,) = _waivers(db)
    assert waiver["by"] == OWNER
    assert waiver["detail"]["approval_id"] == item["approval_id"]
    assert waiver["detail"]["schedule_id"] == SCHEDULE
    assert waiver["detail"]["last_editor"] == OWNER
    assert waiver["detail"]["waived"] == "second_person_required"


def test_another_member_as_last_editor_is_still_refused_and_told_another_member_must(client, db):
    item = _merge_tier_item(client, db, last_editor=DAVE, approvers="members")

    refused = _decide(client, item["approval_id"], "approve", user="dave", digest="digest-1")

    assert refused.status_code == 403 and refused.json()["code"] == "second_person_required"
    assert "another member must" in refused.json()["message"]
    assert _waivers(db) == []
    # The rule holds the other way too: the owner, not the last editor, decides it unwaived.
    assert _decide(client, item["approval_id"], "approve", user="root", digest="digest-1").status_code == 200
    assert _waivers(db) == []


def test_a_named_list_of_the_owner_alone_is_owner_only_and_the_waiver_records_the_list(client, db):
    """BGD 2026-10-25, "Yes: treat [owner] like owner_only": a named list whose
    only entry is the platform owner gets the waiver, and its audit row says
    what the list was, not `owner_only`. Rendered 2026-10-25 (fixture S3):
    nobody could decide it."""
    item = _merge_tier_item(client, db, last_editor=OWNER, approvers=[OWNER])

    decided = _decide(client, item["approval_id"], "approve", user="root", digest="digest-1")

    # MUTATION: `owner_alone` without the named-list case and this is 403 second_person_required.
    assert decided.status_code == 200, decided.text
    (waiver,) = _waivers(db)
    assert waiver["by"] == OWNER and waiver["detail"]["last_editor"] == OWNER
    assert waiver["detail"]["waived"] == "second_person_required"
    assert waiver["detail"]["approvers"] == [OWNER]


def test_a_named_list_of_the_owner_written_in_another_case_and_twice_is_the_owner_alone(client, db):
    """Normalised (strip, lower), the list names one address, the owner's; the
    audit keeps it as it stands."""
    written = [" " + OWNER.upper() + " ", OWNER]
    item = _merge_tier_item(client, db, last_editor=OWNER, approvers=written)

    decided = _decide(client, item["approval_id"], "approve", user="root", digest="digest-1")

    assert decided.status_code == 200, decided.text
    (waiver,) = _waivers(db)
    assert waiver["detail"]["approvers"] == written


def test_a_named_list_of_the_last_editor_alone_who_is_not_the_owner_keeps_the_rule(client, db):
    """A non-owner named alone is never waived, and the refusal says nobody may decide."""
    item = _merge_tier_item(client, db, last_editor=DAVE, approvers=[DAVE])

    refused = _decide(client, item["approval_id"], "approve", user="dave", digest="digest-1")

    assert refused.status_code == 403 and refused.json()["code"] == "second_person_required"
    message = refused.json()["message"]
    assert "named approvers must" not in message and DAVE not in message
    assert "names only you" in message and "nobody may decide it" in message
    assert "another member must" not in message and "platform owner" not in message
    assert _waivers(db) == []


def test_owner_alone_reads_a_named_list_as_the_owner_alone_only_when_it_names_nobody_else():
    """The crafted lists: case, whitespace and duplicates of the owner are the
    owner; an alias, a blank entry, anyone else, or no known owner are not."""
    other = "dave@example.com"
    assert approvals.owner_alone([OWNER], owner=OWNER)
    assert approvals.owner_alone([" Root@Example.COM "], owner=OWNER)
    assert approvals.owner_alone([OWNER, OWNER.upper()], owner=" " + OWNER.upper())
    assert not approvals.owner_alone([OWNER])  # nobody said who the owner is
    assert not approvals.owner_alone([OWNER], owner="")
    assert not approvals.owner_alone([OWNER, other], owner=OWNER)
    assert not approvals.owner_alone([other], owner=OWNER)
    assert not approvals.owner_alone(["root+alias@example.com"], owner=OWNER)
    assert not approvals.owner_alone([OWNER, ""], owner=OWNER)
    assert not approvals.owner_alone([], owner=OWNER)
    assert not approvals.owner_alone("members", owner=OWNER)
    assert not approvals.owner_alone(OWNER, owner=OWNER)  # a bare string is not a list
    assert approvals.owner_alone("owner_only")


def test_a_named_lists_refusal_names_only_the_other_approvers(client, db):
    item = _merge_tier_item(client, db, last_editor=OWNER, approvers=[OWNER, DAVE])

    refused = _decide(client, item["approval_id"], "approve", user="root", digest="digest-1")

    assert refused.status_code == 403 and refused.json()["code"] == "second_person_required"
    message = refused.json()["message"]
    assert message.endswith("another of the schedule's named approvers must: " + DAVE)
    assert OWNER not in message


def test_the_waiver_is_never_returned_to_anyone_but_the_owner():
    """`check_gate_approver` itself: not the owner, no waiver, whatever the hold says."""

    class _Tenant:
        kind = "group"

    class _Store:
        def get_tenant(self, tenant_id):
            return _Tenant()

    class _Ctx:
        store = _Store()
        db = None

    schedule = {"schedule_id": SCHEDULE}
    original = approvals.schedaudit.last_editor
    approvals.schedaudit.last_editor = lambda db, tenant_id, sched: DAVE
    try:
        with pytest.raises(approvals.ScheduleForbidden) as refused:
            approvals.check_gate_approver(
                _Ctx(), "eng", "members", email=DAVE, is_owner=False, schedule=schedule,
                merge_tier=True, hold=OWNER_HOLD, owner_waiver=True,
            )
        assert refused.value.code == "second_person_required"
        assert "only the platform owner may decide it" in refused.value.message
        # The comment path carries the waiver too (owner, 2026-10-25), so the
        # refusal no longer sends the owner to the console alone.
        assert "console" not in refused.value.message
        # Without the caller asking, the owner is refused too: no silent waiver.
        with pytest.raises(approvals.ScheduleForbidden):
            approvals.check_gate_approver(
                _Ctx(), "eng", "members", email=DAVE, is_owner=True, schedule=schedule,
                merge_tier=True, hold=OWNER_HOLD,
            )
        waived = approvals.check_gate_approver(
            _Ctx(), "eng", "members", email=DAVE, is_owner=True, schedule=schedule,
            merge_tier=True, hold=OWNER_HOLD, owner_waiver=True,
        )
        assert waived is not None and waived["last_editor"] == DAVE
    finally:
        approvals.schedaudit.last_editor = original


def test_a_named_list_of_one_reaches_the_waiver_only_as_the_owner_it_names():
    """`check_gate_approver` itself: [dave] is never owner-only, for dave or the owner."""

    class _Tenant:
        kind = "group"

    class _Store:
        def get_tenant(self, tenant_id):
            return _Tenant()

    class _Authenticator:
        def is_tenant_member(self, email, tenant):
            return True

    class _Ctx:
        store = _Store()
        authenticator = _Authenticator()
        db = None

    schedule = {"schedule_id": SCHEDULE}
    original = approvals.schedaudit.last_editor
    try:
        approvals.schedaudit.last_editor = lambda db, tenant_id, sched: DAVE
        with pytest.raises(approvals.ScheduleForbidden) as refused:
            approvals.check_gate_approver(_Ctx(), "eng", [DAVE], email=DAVE, is_owner=False, schedule=schedule,
                                          merge_tier=True, owner_waiver=True)
        assert refused.value.code == "second_person_required"
        # The owner is not on [dave]'s list, so the owner is not its approver at all.
        with pytest.raises(approvals.ScheduleForbidden) as refused:
            approvals.check_gate_approver(_Ctx(), "eng", [DAVE], email=OWNER, is_owner=True, schedule=schedule,
                                          merge_tier=True, owner_waiver=True)
        assert refused.value.code == "approver_not_allowed"
        approvals.schedaudit.last_editor = lambda db, tenant_id, sched: OWNER
        # [owner] for the owner: waived, the list recorded as it stands.
        waived = approvals.check_gate_approver(_Ctx(), "eng", [OWNER.upper()], email=OWNER, is_owner=True,
                                               schedule=schedule, merge_tier=True, owner_waiver=True)
        assert waived is not None and waived["approvers"] == [OWNER.upper()]
        # Not asked for, never given.
        with pytest.raises(approvals.ScheduleForbidden):
            approvals.check_gate_approver(_Ctx(), "eng", [OWNER], email=OWNER, is_owner=True, schedule=schedule,
                                          merge_tier=True)
    finally:
        approvals.schedaudit.last_editor = original


def _scheduled_checking(client, db, objects, writes, plan, *, approver="root") -> dict:
    created = _create(client, auto_merge=True)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    planned = _run(client, run["id"]).json()["run"]
    response = _approve(client, run["id"], planned["plan_digest"], user=approver)
    assert response.status_code == 200, response.text
    running = response.json()["run"]
    writes.open_pull(PR, SHA_A)
    integrator = _workflow_ends(db, running["workflow_id"])
    integrator["result_summary"]["git"]["pushed_head"] = SHA_A
    _review_writes(db, objects, running["workflow_id"], json.dumps(_verdict(True, True)))
    return running


def test_the_owner_approves_an_owner_only_merge_hold_on_a_schedule_the_owner_last_edited(
    client, db, objects, writes, clock, switch_on,
):
    """#2259 as measured: the run's hold is owner_only, the gate is members, the owner last edited it."""
    seed_tenant(db, "eng")
    seed_schedule(db, owner=OWNER)
    db.docs[f"schedules/{SCHEDULE}"]["created_by"] = OWNER
    running = _scheduled_checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}
    # A real stop (not the prompt's): the plan's own file is the frozen contract.
    stored["approval_hold"] = {**OWNER_HOLD, "reasons": ["protected_path"], "matched": ["src/widgets/list.py"]}
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge", user="root")
    assert item["approvers"] == "owner_only"

    member = _decide(client, item["approval_id"], "approve", user="dave", digest=item["digest"])
    assert member.status_code == 403 and member.json()["code"] == "hold_approver_required"
    owner = _decide(client, item["approval_id"], "approve", user="root", digest=item["digest"])

    assert owner.status_code == 200, owner.text
    assert len(_merge_workflows(db)) == 1
    (waiver,) = _waivers(db)
    assert waiver["detail"]["hold"] == "NEEDS_OWNER" and waiver["detail"]["kind"] == "merge"


def _owner_hold_merge_waiting(client, db, objects, writes, clock) -> tuple[dict, dict]:
    """#2259's merge, waiting on its pull request: an owner_only hold, the owner the last editor."""
    seed_tenant(db, "eng")
    seed_schedule(db, owner=OWNER)
    db.docs[f"schedules/{SCHEDULE}"]["created_by"] = OWNER
    _connect(db, OWNER, "root-gh")
    _connect(db, DAVE, "Dave-GH")
    running = _scheduled_checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}
    stored["approval_hold"] = {**OWNER_HOLD, "reasons": ["protected_path"], "matched": ["src/widgets/list.py"]}
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge", user="root")
    return running, item


def test_the_owners_approve_comment_on_an_owner_only_hold_the_owner_last_edited_merges_with_the_waiver(
    client, db, objects, writes, clock, api_context, switch_on,
):
    """Owner answer 2026-10-25: a `/swarmcloud approve` comment carries the owner waiver too."""
    running, item = _owner_hold_merge_waiting(client, db, objects, writes, clock)
    digest = approvals.merge_digest_text(item["digest"])

    cid = _comment(writes, PR, _command(digest), login="root-gh", user_id=1000)
    polled = ghapprove.poll(api_context)

    # MUTATION: drop `owner_waiver=True` from `ghapprove._approve_merge` and
    # this is refused second_person_required, approved 0.
    assert polled["approved"] == 1, (polled, _replies(writes, PR))
    decided = db.docs[f"approvals/{item['approval_id']}"]
    assert decided["state"] == "approved" and decided["decided_by"] == OWNER
    assert db.docs[f"issue_runs/{running['id']}"]["merge_approved"]["head_sha"] == SHA_A
    (approved,) = [e for e in _audit(db, SCHEDULE) if e["action"] == "approval_approved"]
    assert approved["detail"]["via"] == "github_comment" and approved["detail"]["comment_id"] == cid
    # The console's row, exactly: the same waiver, beside the decision's.
    (waiver,) = _waivers(db)
    assert waiver["by"] == OWNER and waiver["detail"]["approval_id"] == item["approval_id"]
    assert waiver["detail"]["waived"] == "second_person_required"
    assert waiver["detail"]["last_editor"] == OWNER and waiver["detail"]["hold"] == "NEEDS_OWNER"
    assert waiver["detail"]["kind"] == "merge"


def test_another_members_approve_comment_on_an_owner_only_hold_is_refused_and_told_who_may_decide(
    client, db, objects, writes, clock, api_context, switch_on,
):
    _, item = _owner_hold_merge_waiting(client, db, objects, writes, clock)
    digest = approvals.merge_digest_text(item["digest"])

    _comment(writes, PR, _command(digest), login="Dave-GH", user_id=1004)
    polled = ghapprove.poll(api_context)

    assert polled.get("approved", 0) == 0
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "pending"
    assert _waivers(db) == []
    (reply,) = _replies(writes, PR)[1:]
    assert "only the platform owner may approve" in reply
    assert "another member" not in reply


def test_the_last_editors_approve_comment_on_an_ordinary_merge_keeps_the_second_person_rule(
    client, db, objects, writes, clock, api_context, switch_on,
):
    """Everyone else keeps the rule on the comment path: dave last edited a `members` gate."""
    seed_tenant(db, "eng")
    seed_schedule(db, owner=DAVE)
    db.docs[f"schedules/{SCHEDULE}"]["created_by"] = DAVE
    _connect(db, DAVE, "Dave-GH")
    running = _scheduled_checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}
    stored["approval_hold"] = None
    stored["merge_approval"] = "required"
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge", user="root")

    _comment(writes, PR, _command(approvals.merge_digest_text(item["digest"])), login="Dave-GH", user_id=1004)
    ghapprove.poll(api_context)

    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "pending"
    assert _waivers(db) == []
    (reply,) = _replies(writes, PR)[1:]
    assert "last changed this schedule" in reply and "another member must" in reply


def test_the_owners_approve_comment_on_a_named_list_of_the_owner_alone_merges_with_the_waiver(
    client, db, objects, writes, clock, api_context, switch_on,
):
    """BGD 2026-10-25, "Yes: treat [owner] like owner_only", on the comment path:
    the gate names only the owner, the owner last edited it, no hold."""
    seed_tenant(db, "eng")
    seed_schedule(db, owner=OWNER)
    db.docs[f"schedules/{SCHEDULE}"]["created_by"] = OWNER
    db.docs[f"schedules/{SCHEDULE}"]["gate"]["approvers"] = [OWNER]
    _connect(db, OWNER, "root-gh")
    running = _scheduled_checking(client, db, objects, writes, FULL_PLAN)
    stored = db.docs[f"issue_runs/{running['id']}"]
    stored.setdefault("metadata", {})["schedule"] = {"schedule_id": SCHEDULE}
    stored["approval_hold"] = None
    stored["merge_approval"] = "required"
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge", user="root")

    cid = _comment(writes, PR, _command(approvals.merge_digest_text(item["digest"])), login="root-gh", user_id=1000)
    polled = ghapprove.poll(api_context)

    # MUTATION: `owner_alone` without the named-list case and this is refused second_person_required.
    assert polled["approved"] == 1, (polled, _replies(writes, PR))
    assert db.docs[f"approvals/{item['approval_id']}"]["decided_by"] == OWNER
    (approved,) = [e for e in _audit(db, SCHEDULE) if e["action"] == "approval_approved"]
    assert approved["detail"]["comment_id"] == cid
    (waiver,) = _waivers(db)
    assert waiver["by"] == OWNER and waiver["detail"]["approval_id"] == item["approval_id"]
    assert waiver["detail"]["approvers"] == [OWNER] and waiver["detail"]["last_editor"] == OWNER


# --------------------------------------------------------------------------
# 2. a prompt-only hold, re-checked on the pull request's own change
# --------------------------------------------------------------------------

#: PR 2234's plan, in shape: its files are the worker's, its prompt names
#: what must not be touched.
_PROMPT = ("Fix the rebase in apps/agent-worker/agent_worker/gitops.py. Never edit "
           "apps/common/swarm_common/ (FROZEN) or .github/workflows/.")
_PLAIN = "@@ -1,3 +1,4 @@\n+    return head\n"


def _prompt_plan() -> dict:
    plan = copy.deepcopy(FULL_PLAN)
    plan["steps"][0]["files"] = ["apps/agent-worker/agent_worker/gitops.py"]
    plan["steps"][0]["prompt"] = _PROMPT
    return plan


def _at_merge(hold, paths, *, patches, issue_read=None, plan=None):
    return issueruns.merge_hold(
        hold, plan or _prompt_plan(), paths, platform=True, hard_stop_paths=[],
        issue_read=issue_read, patches=patches,
    )


def _prompt_hold(issue_read=None) -> dict:
    hold = issueruns.plan_hold(_prompt_plan(), platform=True, hard_stop_paths=[], issue_read=issue_read)
    assert hold is not None and {"contract", "workflows"} <= set(hold["reasons"]), hold
    return hold


def test_a_prompt_only_hold_on_a_pull_request_whose_files_match_nothing_clears():
    files = ["apps/agent-worker/agent_worker/gitops.py", "apps/agent-worker/agent_worker/lifecycle.py"]

    hold, why = _at_merge(_prompt_hold(), files, patches={f: _PLAIN for f in files})

    # MUTATION: return `widen_hold(current, found)` unconditionally and both are the old hold.
    assert hold is None
    assert why == "hold set from the plan's prompt; the pull request changes none of it"


def test_a_hold_whose_category_matches_the_real_files_stays():
    files = ["apps/agent-worker/agent_worker/gitops.py", "apps/common/swarm_common/states.py"]
    current = _prompt_hold()

    hold, why = _at_merge(current, files, patches={f: _PLAIN for f in files})

    assert why is None
    assert hold is not None and hold["code"] == "NEEDS_OWNER"
    assert set(current["reasons"]) <= set(hold["reasons"])


def test_a_security_label_hold_never_clears():
    issue_read = {"labels": ["security"], "body": ""}
    files = ["apps/agent-worker/agent_worker/gitops.py"]
    current = _prompt_hold(issue_read)

    hold, why = _at_merge(current, files, patches={f: _PLAIN for f in files}, issue_read=issue_read)

    assert why is None and "security" in hold["reasons"]


def test_a_hold_from_the_plans_own_files_is_not_the_prompts_and_stays():
    plan = _prompt_plan()
    plan["steps"][0]["files"].append("apps/common/swarm_common/states.py")
    current = issueruns.plan_hold(plan, platform=True, hard_stop_paths=[])
    files = ["apps/agent-worker/agent_worker/gitops.py"]

    hold, why = _at_merge(current, files, patches={f: _PLAIN for f in files}, plan=plan)

    assert why is None and "contract" in hold["reasons"]


def test_without_the_patches_a_prompt_only_hold_only_widens():
    """A prompt's `secrets` term cannot be shown absent from lines nobody read."""
    files = ["apps/agent-worker/agent_worker/gitops.py"]

    hold, why = _at_merge(_prompt_hold(), files, patches=None)

    assert why is None and hold is not None


def test_a_file_the_forge_sent_no_patch_for_keeps_a_prompt_only_hold():
    """A large diff's omitted patch could hide a secret-shaped line: not shown clear."""
    files = ["apps/agent-worker/agent_worker/gitops.py", "package-lock.json"]

    hold, why = _at_merge(_prompt_hold(), files, patches={files[0]: _PLAIN, files[1]: None})

    assert why is None and hold is not None


def _platform_repo(db) -> None:
    rid = repo_id_for("eng", "example-org", "widgets")
    doc = db.docs.setdefault(f"repositories/{rid}", {"repo_id": rid, "tenant_id": "eng",
                                                     "owner": "example-org", "repo": "widgets"})
    doc["platform"] = True


def _prompt_only_run(client, db, objects, writes) -> dict:
    _platform_repo(db)
    plan = copy.deepcopy(FULL_PLAN)
    plan["steps"][0]["prompt"] = "Sort WidgetList by name. Never edit apps/common/swarm_common/ (FROZEN)."
    running = _scheduled_checking(client, db, objects, writes, plan)
    hold = db.docs[f"issue_runs/{running['id']}"]["approval_hold"]
    assert hold is not None and "contract" in hold["reasons"], hold
    return running


def test_the_merge_point_clears_a_prompt_only_hold_when_the_pull_request_touches_none_of_it(
    client, db, objects, writes, clock, switch_on, pr_files,
):
    pr_files[:] = [("src/widgets/list.py", _PLAIN)]
    running = _prompt_only_run(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")

    _read(client, clock, running["id"])

    stored = db.docs[f"issue_runs/{running['id']}"]
    assert stored["approval_hold"] is None
    cleared = stored["pull_request"]["hold_cleared"]
    assert cleared["why"] == "hold set from the plan's prompt; the pull request changes none of it"
    assert cleared["head_sha"] == SHA_A and "contract" in cleared["reasons"]
    assert _inbox(client, kind="merge", user="root") == []
    assert len(_merge_workflows(db)) == 1


def test_the_merge_point_keeps_a_prompt_hold_the_pull_request_really_matches(
    client, db, objects, writes, clock, switch_on, pr_files,
):
    pr_files[:] = [("src/widgets/list.py", _PLAIN), ("apps/common/swarm_common/states.py", _PLAIN)]
    running = _prompt_only_run(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")

    _read(client, clock, running["id"])

    stored = db.docs[f"issue_runs/{running['id']}"]
    assert stored["approval_hold"] is not None and "contract" in stored["approval_hold"]["reasons"]
    assert "hold_cleared" not in stored["pull_request"]
    assert _merge_workflows(db) == []
    (item,) = _inbox(client, kind="merge", user="root")
    detail = client.get(f"/v1/approvals/{item['approval_id']}", headers=auth_header("root"))
    assert detail.status_code == 200, detail.text
    assert detail.json()["merge"]["hold_cleared"] is None
