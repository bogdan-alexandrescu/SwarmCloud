"""The `custom-prompt` schedule type (docs/schedules.md §3.12, lane S10h).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT. Its file exists and declares its budget caps, so the
  catalogue serves it available and the tick's loader imports it.
* THE DIGEST is over the validated spec: key order and a default written
  out do not change it; an edit does.
* NOTHING RUNS UNTIL A PLATFORM ADMIN APPROVES THE DIGEST. The first firing
  asks for one `spec` approval carrying the spec, creates no workflow and
  records a failed `api_action` naming the approval; a second firing asks
  nothing new. Approved, the next firing submits. An edited spec asks again;
  a rejected digest stays refused; an expired request is asked again.
* MERGE IS NEVER AUTOMATIC: the workflow is submitted with `metadata.merge`
  off whatever the spec says, as the schedule's owner, marked.
* THE REPOSITORY is bounded by the scope, through a refusal that ships off.
* A DRY RUN asks for no approval and submits nothing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from swarm_api import approvals, refusals, schedulefire, scheduletypes
from swarm_api.schedtypes import custom_prompt
from swarm_api.schemas import WorkflowCreate

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    NOW,
    SCHEDULE_ID,
    ctx,
    firing_for,
    github,
    make_schedule,
    register,
    tasks,
    writes,
)

ADMIN = "admin@example.com"


def spec(prompt: str = "tidy the changelog", **extra: Any) -> dict[str, Any]:
    return {"steps": [{"step_id": "only", "runner_profile": "mock", "input": {"prompt": prompt}}], **extra}


def standing(db, *, prompt: str = "tidy the changelog", repo_ids: tuple[str, ...] = (),
             spec_extra: dict | None = None, **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_="custom-prompt", repo_ids=repo_ids, cron="0 */6 * * *",
                         params={"spec": spec(prompt, **(spec_extra or {}))}, max_concurrent=1, **kwargs)


def spec_records(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("approvals/") and d.get("kind") == approvals.SPEC]


def approve_spec(db, doc: dict[str, Any], *, approve: bool = True) -> None:
    approvals.decide(db, NOW, doc["tenant_id"], doc["approval_id"], approve=approve, by=ADMIN,
                     digest=doc["digest"], reason=None if approve else "not this instruction")


def digest_of(schedule: dict[str, Any]) -> str:
    return custom_prompt.spec_digest(WorkflowCreate.model_validate(schedule["params"]["spec"]))


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("custom-prompt")
    assert scheduletypes.availability(entry) == (True, "")
    assert scheduletypes.budget_caps(entry) == custom_prompt.BUDGET_CAPS
    assert schedulefire.load_executor(entry) is custom_prompt


def test_the_gate_floor_is_approve_run_and_never_merge_auto() -> None:
    entry = scheduletypes.get("custom-prompt")
    assert entry.floor_gate.run == "approve" and entry.floor_gate.merge == "approve"


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def test_the_digest_is_of_the_validated_spec() -> None:
    plain = WorkflowCreate.model_validate(spec())
    spelled = WorkflowCreate.model_validate({"priority": 0, **spec(), "on_step_failure": "fail_workflow"})
    edited = WorkflowCreate.model_validate(spec("tidy the changelog and the readme"))
    assert custom_prompt.spec_digest(plain) == custom_prompt.spec_digest(spelled)
    assert custom_prompt.spec_digest(plain) != custom_prompt.spec_digest(edited)
    assert custom_prompt.spec_digest(plain).startswith("sha256:")


# ---------------------------------------------------------------------------
# The admin's approval of a digest
# ---------------------------------------------------------------------------


def test_an_unapproved_spec_asks_a_platform_admin_and_creates_nothing(db, ctx) -> None:
    schedule = standing(db)
    recorded: list[dict[str, Any]] = []

    work = custom_prompt.create(firing_for(ctx, schedule, recorded=recorded))

    (record,) = spec_records(db)
    assert record["state"] == approvals.PENDING and record["approvers"] == custom_prompt.SPEC_APPROVERS
    assert record["digest"] == digest_of(schedule) and record["subject"]["schedule_id"] == SCHEDULE_ID
    assert record["spec"]["steps"][0]["input"]["prompt"] == "tidy the changelog"
    assert record["merge_tier"] is True and record["tenant_id"] == "eng"
    assert tasks(db) == []
    (item,) = work
    assert item["kind"] == "api_action" and item["outcome"] == "failed"
    assert item["detail"]["code"] == custom_prompt.SPEC_PENDING
    assert item["detail"]["approval_id"] == record["approval_id"]
    assert recorded == work

    custom_prompt.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))
    assert len(spec_records(db)) == 1 and tasks(db) == []


def test_an_approved_digest_runs_one_workflow_with_merge_off_as_the_owner(db, ctx) -> None:
    schedule = standing(db, spec_extra={"metadata": {"merge": "on"}})
    custom_prompt.create(firing_for(ctx, schedule))
    (record,) = spec_records(db)
    approve_spec(db, record)

    firing = firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next")
    (item,) = custom_prompt.create(firing)

    assert item["kind"] == "workflow" and item["repo_id"] is None
    (task,) = tasks(db)
    assert task["workflow_id"] == item["id"] and task["submitted_by"] == ALICE
    assert task["metadata"]["schedule"] == firing.mark
    # `merge: auto` is never allowed: the spec's "on" is submitted off.
    assert task["metadata"]["merge"] == "off"
    assert len(spec_records(db)) == 1


def test_an_edited_spec_needs_the_approval_again(db, ctx) -> None:
    schedule = standing(db)
    custom_prompt.create(firing_for(ctx, schedule))
    approve_spec(db, spec_records(db)[0])

    edited = standing(db, prompt="delete the changelog")
    (item,) = custom_prompt.create(firing_for(ctx, edited, fid=f"{SCHEDULE_ID}:next"))

    assert item["kind"] == "api_action" and item["detail"]["code"] == custom_prompt.SPEC_PENDING
    assert tasks(db) == []
    pending = [r for r in spec_records(db) if r["state"] == approvals.PENDING]
    assert [r["digest"] for r in pending] == [digest_of(edited)]


def test_a_rejected_digest_stays_refused_and_is_not_asked_again(db, ctx) -> None:
    schedule = standing(db)
    custom_prompt.create(firing_for(ctx, schedule))
    approve_spec(db, spec_records(db)[0], approve=False)

    (item,) = custom_prompt.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert item["detail"]["code"] == custom_prompt.SPEC_REJECTED and tasks(db) == []
    assert len(spec_records(db)) == 1


def test_an_expired_request_is_asked_again_as_the_next_round(db, ctx) -> None:
    schedule = standing(db)
    custom_prompt.create(firing_for(ctx, schedule))
    (first,) = spec_records(db)
    db.docs[f"approvals/{first['approval_id']}"]["state"] = approvals.EXPIRED

    custom_prompt.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    (second,) = [r for r in spec_records(db) if r["state"] == approvals.PENDING]
    assert second["approval_id"] != first["approval_id"] and second["round"] == 1
    assert second["digest"] == first["digest"] and tasks(db) == []


def test_another_tenants_record_under_the_id_is_never_read_as_an_approval(db, ctx) -> None:
    schedule = standing(db)
    digest = digest_of(schedule)
    item_id = approvals.approval_id(approvals.SPEC, custom_prompt.spec_key(SCHEDULE_ID, digest, 0))
    db.docs[f"approvals/{item_id}"] = {"approval_id": item_id, "tenant_id": "research",
                                       "kind": approvals.SPEC, "state": approvals.APPROVED, "digest": digest}
    with pytest.raises(LookupError):
        custom_prompt.create(firing_for(ctx, schedule))
    assert tasks(db) == []


def test_no_room_creates_nothing_and_asks_nothing(db, ctx) -> None:
    assert custom_prompt.create(firing_for(ctx, standing(db), room=0)) == []
    assert spec_records(db) == [] and tasks(db) == []


# ---------------------------------------------------------------------------
# The repository bound
# ---------------------------------------------------------------------------


def _approved(db, ctx, schedule: dict[str, Any]) -> None:
    custom_prompt.create(firing_for(ctx, schedule))
    approve_spec(db, spec_records(db)[0])


def test_a_repository_outside_the_scope_is_refused_when_switched_on(db, ctx, monkeypatch) -> None:
    in_scope = register(db)
    register(db, repo="gadgets")
    schedule = standing(db, repo_ids=(in_scope,),
                        spec_extra={"repository_url": "https://github.com/example-org/gadgets"})
    _approved(db, ctx, schedule)
    monkeypatch.setenv(refusals.env_name(custom_prompt.CustomPromptRepositoryOutOfScope.code), "on")

    with pytest.raises(custom_prompt.CustomPromptRepositoryOutOfScope) as caught:
        custom_prompt.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert caught.value.status_code == 403 and tasks(db) == []


def test_the_repository_refusal_ships_off() -> None:
    switch = refusals.SWITCHES[custom_prompt.CustomPromptRepositoryOutOfScope.code]
    assert switch.default is False
    assert not refusals.enforced(switch.code, {})


def test_a_repository_in_scope_is_named_on_the_work_item(db, ctx) -> None:
    repo_id = register(db)
    schedule = standing(db, repo_ids=(repo_id,),
                        spec_extra={"repository_url": "https://github.com/example-org/widgets"})
    firing = firing_for(ctx, schedule)
    assert custom_prompt.repository_in_scope(firing, WorkflowCreate.model_validate(schedule["params"]["spec"])) \
        == (repo_id, True)


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_asks_nothing_and_submits_nothing(db, ctx) -> None:
    schedule = standing(db)

    output = custom_prompt.dry_run(firing_for(ctx, schedule))

    assert output["would"] == "request_spec_approval" and output["spec_digest"] == digest_of(schedule)
    assert output["merge"] == "off" and output["steps"] == [{"step_id": "only", "runner_profile": "mock"}]
    assert "Workflows: No access" in output["forge"]["not_held"]
    assert spec_records(db) == [] and tasks(db) == []


def test_a_dry_run_of_an_approved_spec_says_it_would_submit(db, ctx) -> None:
    schedule = standing(db)
    _approved(db, ctx, schedule)
    before = len(db.docs)

    output = custom_prompt.dry_run(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert output["would"] == "submit_workflow"
    assert output["spec_approval"]["state"] == approvals.APPROVED
    assert len(db.docs) == before and tasks(db) == []


def test_the_approval_expires_on_the_schedules_ttl(db, ctx) -> None:
    schedule = standing(db, gate={"approval_ttl_hours": 24})
    custom_prompt.create(firing_for(ctx, schedule))
    (record,) = spec_records(db)
    assert record["expires_at"] == record["requested_at"] + timedelta(hours=24)
