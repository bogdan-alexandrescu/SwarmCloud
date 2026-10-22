"""The `visual-qa` schedule type (docs/schedules.md §3.17; lane VQ-7 TYPE of
docs/design/agent-autonomy.md §6, designed in docs/design/visual-qa-any-app.md
§6; part of #1933).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT. Its file exists and declares its budget caps, so the
  catalogue serves it available and the tick's real loader imports it.
* THE MINIMUM INTERVAL IS 24 HOURS: a cron that fires more often is refused
  when the schedule is built.
* ITS ONE PARAMETER IS `target`, a QA target id; a caller can name nothing
  else about the run (invariant 10).
* A FIRING RUNS `:run` FOR ITS TARGET: one workflow, the schedule owner's,
  marked with the firing, its browser steps stamped `metadata.qa_target` and
  named in `qa_target_runs/{workflow_id}` exactly as the route's are.
* A PENDING HOST MAKES THE FIRING A REFUSAL, NOT A FAILURE: the tick ends it
  `refused` with `qa_target_host_pending`, and nothing is submitted.
* A TARGET OF ANOTHER TENANT IS REFUSED AT CREATION (`check_target`, the
  create and edit hook, which `POST` and `PATCH /v1/schedules` call),
  answering exactly as a missing one, and again at firing time. Creating one
  is a workspace admin's (§6).
* A DRY RUN submits nothing and says what the firing would do.

No cloud and no emulator: the in-memory Firestore, a fake resolver.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import qatargets, schedulefire, schedules, scheduletypes
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, build_context
from swarm_api.errors import ApiError, Forbidden
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.schedtypes import visual_qa
from swarm_api.schemas import WorkflowCreate
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant
from .test_qa_targets import (
    CAROL,
    CAROL_SHOP,
    CAROL_TENANT,
    CLASS_MODELS,
    CONSOLE,
    ENG_WIDGETS,
    PUBLIC,
    FakeResolver,
    seed_repositories,
    target_body,
)
from .test_schedtypes_issue_sweep import NOW, SCHEDULE_ID, firing_for, firings, make_schedule, tasks

ROOT = "root@example.com"
ALICE = "alice@example.com"
TYPE = "visual-qa"


@pytest.fixture
def resolver() -> FakeResolver:
    fake = FakeResolver()
    for host in ("shop.example.com", "widgets.example.com"):
        fake.answers[host] = [PUBLIC]
    return fake


@pytest.fixture
def ctx(db, tokens, group_map, objects, monkeypatch) -> AppContext:
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_repositories(db)
    context = build_context(
        settings=api_settings(worker_class_models=dict(CLASS_MODELS), console_url=CONSOLE),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    context.now = lambda: NOW
    return context


def _targets(db, resolver) -> qatargets.QaTargets:
    return qatargets.QaTargets(db, now=lambda: NOW, resolve=resolver)


def carol_target(db, resolver, *, approved: bool = True, files: bool = True) -> dict[str, Any]:
    """Carol's target in her personal workspace, filing into example-org/shop."""
    target = _targets(db, resolver).create(CAROL_TENANT, target_body(), by=CAROL)
    if approved:
        target = _targets(db, resolver).approve_host(target["id"], host="shop.example.com",
                                                     extra_origin=None, by=ROOT)
    if files:
        db.docs[f"repositories/{CAROL_SHOP}"]["agents_may_file_issues"] = True
    return target


def eng_target(db, resolver) -> dict[str, Any]:
    return _targets(db, resolver).create("eng", target_body(
        name="Widgets", base_url="https://widgets.example.com", repository="example-org/widgets",
    ), by=ROOT)


def qa_schedule(db, target_id: str, *, tenant_id: str = CAROL_TENANT, owner: str = CAROL,
                **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_=TYPE, scope_mode="all", params={"target": target_id},
                         tenant_id=tenant_id, owner=owner, cron="0 6 * * *", max_concurrent=1, **kwargs)


def workflows(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("workflows/") and p.count("/") == 1]


def auth_of(ctx, schedule: dict[str, Any]):
    """The creating caller's context, as the create route has it: from their token."""
    return ctx.authenticator.authenticate(f"Bearer token-{schedule['owner'].split('@')[0]}")


# ---------------------------------------------------------------------------
# The type is built
# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get(TYPE)
    # MUTATION: delete schedtypes/visual_qa.py (or its BUDGET_CAPS) and this is (False, reason).
    assert scheduletypes.availability(entry) == (True, "")
    assert scheduletypes.budget_caps(entry) == visual_qa.BUDGET_CAPS
    # MUTATION: drop `dry_run` from the module and the real loader refuses it.
    assert schedulefire.load_executor(entry) is visual_qa


def test_its_caps_are_the_owners_after_the_first_measured_run() -> None:
    # QA run 5b (wf_025ae735511546d99eff: 17 steps, 362 screenshots) cost $23.62,
    # 94% of the provisional $25; BGD set 40 / 80 / 1 on 2026-10-22 (VQ7-CAPS, #1933).
    # MUTATION: restore the provisional per_run_usd=25, per_day_usd=50 and this fails.
    caps = scheduletypes.budget_caps(scheduletypes.get(TYPE))
    assert (caps.per_run_usd, caps.per_day_usd, caps.max_concurrent) == (40, 80, 1)


def test_it_files_issues_and_never_merges_or_pushes() -> None:
    entry = scheduletypes.get(TYPE)
    # §6: "No merge gate exists to set: it files issues and changes no code."
    # MUTATION: give the floor `merge: approve` and a member could ask for a merge step.
    assert entry.default_gate.merge == "off" and entry.floor_gate.merge == "off"
    params = entry.params_model(target="qat_" + "0" * 16)
    # MUTATION: `pushes=_always` and the repository grant check asks for `write`.
    assert entry.pushes(params) is False
    assert entry.risk(entry.default_gate, params) == "R1"


def test_the_minimum_interval_is_24_hours() -> None:
    entry = scheduletypes.get(TYPE)
    # MUTATION: `min_interval=timedelta(hours=6)` and both of these change.
    assert entry.min_interval == timedelta(hours=24)
    body = schedules.parse_create({
        "name": "nightly QA", "type": TYPE, "scope": {"mode": "all"},
        "cron": "0 */12 * * *", "timezone": "UTC", "params": {"target": "qat_" + "1" * 16},
    })
    with pytest.raises(ApiError) as caught:
        schedules.build_schedule(body, tenant_id="eng", actor=ROOT, now=NOW, registered_repo_ids=[ENG_WIDGETS],
                                 is_admin=True)
    assert caught.value.code == "interval_too_short"
    # CONTROL: daily is accepted.
    daily = schedules.parse_create({**body.model_dump(mode="json"), "cron": "0 6 * * *"})
    doc = schedules.build_schedule(daily, tenant_id="eng", actor=ROOT, now=NOW,
                                   registered_repo_ids=[ENG_WIDGETS], is_admin=True)
    assert doc["params"] == {"target": "qat_" + "1" * 16}


@pytest.mark.parametrize("params", [
    {},
    {"target": "oracle"},
    {"target": "qat_" + "1" * 16, "base_url": "https://elsewhere.example.com"},
    {"target": "qat_" + "1" * 16, "screens": ["home"]},
], ids=["missing", "not-an-id", "a-url", "a-screen-list"])
def test_the_one_parameter_is_a_target_id(params) -> None:
    entry = scheduletypes.get(TYPE)
    gate = schedules.resolve_gate(entry, None)
    # MUTATION: give `target` a default, or allow extra keys, and one of these validates.
    with pytest.raises(ApiError) as caught:
        schedules.resolve_params(entry, params, gate=gate)
    assert caught.value.code == "invalid_params"


# ---------------------------------------------------------------------------
# A firing runs `:run` for its target
# ---------------------------------------------------------------------------


def test_a_firing_runs_the_targets_run_as_the_owner_marked(db, ctx, resolver) -> None:
    target = carol_target(db, resolver)
    schedule = qa_schedule(db, target["id"])

    (item,) = visual_qa.create(firing_for(ctx, schedule, room=1))

    # MUTATION: return [] from `create` and there is no workflow.
    assert item["kind"] == "workflow" and item["repo_id"] == CAROL_SHOP
    (workflow,) = workflows(db)
    assert workflow["workflow_id"] == item["id"] and workflow["tenant_id"] == CAROL_TENANT
    made = [t for t in tasks(db) if t.get("workflow_id") == item["id"]]
    assert made and all(t["submitted_by"] == CAROL for t in made)
    mark = (made[0].get("metadata") or {}).get("schedule") or {}
    # MUTATION: submit through `ctx.submissions` directly and the firing's mark is missing.
    assert mark.get("schedule_id") == SCHEDULE_ID
    browser = {t["step_id"] for t in made if (t.get("metadata") or {}).get("qa_target")}
    # MUTATION: render the workflow without `qatargets.render_run` and no step is stamped.
    assert browser == {"capture-g1"}
    for task in made:
        if task["step_id"] in browser:
            assert task["metadata"]["qa_target"] == {"id": target["id"], "revision": target["revision"]}
        # §1.1: the target travels as metadata, never in a step's input.
        assert set(task["input"]) <= {"prompt", "browser_tool"}
    # The schedule files: the `file` step is there, as `:run`'s default.
    assert "file" in {t["step_id"] for t in made}
    run = db.docs[f"qa_target_runs/{item['id']}"]
    # MUTATION: drop `record_run` and the worker's route serves the browser steps nothing.
    assert run["target_id"] == target["id"] and run["tenant_id"] == CAROL_TENANT
    assert sorted(run["task_ids"]) == sorted(t["id"] for t in made if t["step_id"] in browser)
    assert run["by"] == CAROL and run["file"] == "issues"


def test_a_firings_qa_target_mark_gets_past_the_reserved_key_check(db, ctx, resolver, monkeypatch) -> None:
    """`metadata.qa_target` is reserved (VQ2-RESERVE): `submit_workflow` refuses
    it on every step unless the submission runs inside `qa_target_mark_allowed`
    with that exact mark. The firing goes through the REAL service and its real
    `reject_reserved_metadata`, nothing faked between them."""
    target = carol_target(db, resolver)
    schedule = qa_schedule(db, target["id"])
    rendered: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []

    def render_and_keep(*args: Any, **kwargs: Any):
        answer = qatargets.render_run(*args, **kwargs)
        rendered.append(answer)
        return answer

    monkeypatch.setattr(visual_qa, "render_run", render_and_keep)
    # The service is the real one: no fake that skips the check.
    assert type(ctx.submissions).__module__ == "swarm_api.service"

    # MUTATION: remove `with qa_target_mark_allowed(mark):` around `firing.submit_workflow`
    # in schedtypes/visual_qa.py `create` and this raises 422 invalid_dispatch,
    # `reserved_metadata_keys: ["qa_target"]`, with nothing submitted.
    (item,) = visual_qa.create(firing_for(ctx, schedule, room=1))

    ((workflow, mark, browser),) = rendered
    assert mark == {"id": target["id"], "revision": target["revision"]}
    made = {t["step_id"]: t for t in tasks(db) if t.get("workflow_id") == item["id"]}
    assert browser and set(browser) <= set(made)
    for step_id in browser:
        assert made[step_id]["metadata"]["qa_target"] == mark
    # The schedule's own mark rode along: the two exemptions are separate and both held.
    assert all((t.get("metadata") or {}).get("schedule", {}).get("schedule_id") == SCHEDULE_ID
               for t in made.values())

    # CONTROL: the same rendered workflow, submitted by the same owner outside
    # the block, is refused by the same check -- so the pass above is the wrap.
    owner = schedulefire.schedule_owner_auth(ctx, schedule)
    with pytest.raises(ApiError) as caught:
        ctx.submissions.submit_workflow(owner, WorkflowCreate.model_validate(workflow))
    assert caught.value.status_code == 422 and caught.value.code == "invalid_dispatch"
    assert caught.value.detail == {"reserved_metadata_keys": ["qa_target"]}
    assert len(workflows(db)) == 1


def test_a_firing_with_no_room_creates_nothing(db, ctx, resolver) -> None:
    target = carol_target(db, resolver)
    # MUTATION: skip the room check and `Firing._spend` raises RoomExceeded instead.
    assert visual_qa.create(firing_for(ctx, qa_schedule(db, target["id"]), room=0)) == []
    assert workflows(db) == []


def test_a_tick_fires_the_type_through_the_real_loader(db, ctx, resolver) -> None:
    target = carol_target(db, resolver)
    qa_schedule(db, target["id"])

    report = schedulefire.Ticker(ctx).tick()

    assert report.fired == 1, report.to_api()
    (firing,) = firings(db).values()
    assert firing["state"] != "refused", firing
    (workflow,) = workflows(db)
    assert [w["id"] for w in firing["work"]] == [workflow["workflow_id"]]


def test_a_pending_host_makes_the_firing_a_refusal_not_a_failure(db, ctx, resolver) -> None:
    target = carol_target(db, resolver, approved=False)
    qa_schedule(db, target["id"])

    report = schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    # MUTATION: catch the refusal in `create` and record a failed api_action, and this is `created`.
    assert firing["state"] == schedulefire.REFUSED, firing
    assert firing["skip"]["code"] == qatargets.QaTargetHostPending.code
    assert report.refused == 1 and report.errors == 0 and not report.failures, report.to_api()
    assert workflows(db) == [] and not [p for p in db.docs if p.startswith("qa_target_runs/")]


def test_the_registrations_flag_off_is_refused_too(db, ctx, resolver) -> None:
    target = carol_target(db, resolver, files=False)
    qa_schedule(db, target["id"])

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    # §5: the schedule files, so the flag is `:run`'s refusal for it as for a person.
    assert firing["state"] == schedulefire.REFUSED
    assert firing["skip"]["code"] == qatargets.IssueWritesOff.code
    assert workflows(db) == []


def test_another_tenants_target_is_refused_at_firing_time(db, ctx, resolver) -> None:
    target = carol_target(db, resolver)
    # A schedule document in eng naming carol's target (as a stored document
    # could, whatever the create route checked).
    qa_schedule(db, target["id"], tenant_id="eng", owner=ROOT)

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    # MUTATION: read the target with `any_tenant` in `create` and carol's target runs in eng.
    assert firing["state"] == schedulefire.REFUSED
    assert firing["skip"]["code"] == "not_found"
    assert workflows(db) == []


# ---------------------------------------------------------------------------
# At creation (and edit): the target is the schedule's tenant's
# ---------------------------------------------------------------------------


def test_another_tenants_target_is_refused_at_creation(db, ctx, resolver) -> None:
    target = carol_target(db, resolver)
    schedule = qa_schedule(db, target["id"], tenant_id="eng", owner=ROOT)

    with pytest.raises(ApiError) as caught:
        visual_qa.check_target(ctx, schedule, auth_of(ctx, schedule))
    # MUTATION: look the target up by id alone in `check_target` and this passes.
    assert caught.value.code == visual_qa.TARGET_NOT_FOUND
    # The same answer as an id that exists nowhere: an id is no oracle for another tenant.
    missing = qa_schedule(db, "qat_" + "f" * 16, tenant_id="eng", owner=ROOT)
    with pytest.raises(ApiError) as nowhere:
        visual_qa.check_target(ctx, missing, auth_of(ctx, missing))
    assert (nowhere.value.code, nowhere.value.message) == (caught.value.code, caught.value.message)


def test_the_tenants_own_target_is_accepted_at_creation_even_while_pending(db, ctx, resolver) -> None:
    target = carol_target(db, resolver, approved=False)
    schedule = qa_schedule(db, target["id"])
    # CONTROL for the test above; a pending host is the firing's refusal, not creation's.
    assert visual_qa.check_target(ctx, schedule, auth_of(ctx, schedule)) is None


def test_only_a_workspace_admin_creates_one(db, ctx, resolver) -> None:
    seed_tenant(db, "eng")
    target = eng_target(db, resolver)
    schedule = qa_schedule(db, target["id"], tenant_id="eng", owner=ALICE)

    # §6: "created by a workspace admin"; alice is an eng member, not its admin.
    # MUTATION: drop the `require_workspace_admin` call in `check_target` and this passes.
    with pytest.raises(Forbidden):
        visual_qa.check_target(ctx, schedule, auth_of(ctx, schedule))
    # CONTROL: a platform admin administers every workspace.
    as_root = qa_schedule(db, target["id"], tenant_id="eng", owner=ROOT)
    assert visual_qa.check_target(ctx, as_root, auth_of(ctx, as_root)) is None


# ---------------------------------------------------------------------------
# The same rules through the routes: POST and PATCH /v1/schedules
# ---------------------------------------------------------------------------


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def create_body(target_id: str) -> dict[str, Any]:
    return {"name": "nightly-qa", "type": TYPE, "scope": {"mode": "all"},
            "cron": "0 6 * * *", "timezone": "UTC", "params": {"target": target_id}}


def stored_schedules(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("schedules/") and p.count("/") == 1]


def test_the_route_refuses_another_tenants_target_at_creation(db, client, resolver) -> None:
    seed_tenant(db, "eng")
    theirs = eng_target(db, resolver)

    response = client.post("/v1/schedules", json=create_body(theirs["id"]), headers=auth_header("carol"))

    # MUTATION: drop the `_check_qa_target` call in routes/schedules.py `create_schedule` and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == visual_qa.TARGET_NOT_FOUND
    # Refused before the transaction: nothing is stored.
    assert stored_schedules(db) == []
    # CONTROL: her own target, its host still pending, is created.
    ours = carol_target(db, resolver, approved=False)
    created = client.post("/v1/schedules", json=create_body(ours["id"]), headers=auth_header("carol"))
    assert created.status_code == 201, created.text
    assert len(stored_schedules(db)) == 1


def test_the_route_refuses_a_member_who_is_not_a_workspace_admin(db, client, resolver) -> None:
    seed_tenant(db, "eng")
    target = eng_target(db, resolver)

    response = client.post("/v1/schedules", json=create_body(target["id"]), headers=auth_header("alice"))

    # MUTATION: drop the `_check_qa_target` call in `create_schedule` and alice, a member, creates one.
    assert response.status_code == 403, response.text
    assert stored_schedules(db) == []
    # CONTROL: a platform admin administers every workspace.
    as_root = client.post("/v1/schedules", json=create_body(target["id"]), headers=auth_header("root"))
    assert as_root.status_code == 201, as_root.text


def test_the_route_refuses_an_edit_to_another_tenants_target(db, client, resolver) -> None:
    seed_tenant(db, "eng")
    theirs = eng_target(db, resolver)
    ours = carol_target(db, resolver)
    created = client.post("/v1/schedules", json=create_body(ours["id"]), headers=auth_header("carol"))
    assert created.status_code == 201, created.text
    doc = created.json()["schedule"]

    response = client.patch(f"/v1/schedules/{doc['schedule_id']}",
                            json={"revision": doc["revision"], "params": {"target": theirs["id"]}},
                            headers=auth_header("carol"))

    # MUTATION: drop the `_check_qa_target` call in routes/schedules.py `patch_schedule` and this is 200.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == visual_qa.TARGET_NOT_FOUND
    (stored,) = stored_schedules(db)
    assert stored["params"]["target"] == ours["id"]


# ---------------------------------------------------------------------------
# Dry run (§2.8)
# ---------------------------------------------------------------------------


def test_a_dry_run_submits_nothing_and_names_the_refusal(db, ctx, resolver) -> None:
    target = carol_target(db, resolver, approved=False)
    schedule = qa_schedule(db, target["id"])

    pending = visual_qa.dry_run(firing_for(ctx, schedule, room=1))
    # MUTATION: call `create` from `dry_run` and a workflow (or the refusal) escapes.
    assert pending["would"] == qatargets.QaTargetHostPending.code and pending["target"] == target["id"]

    _targets(db, resolver).approve_host(target["id"], host="shop.example.com", extra_origin=None, by=ROOT)
    ready = visual_qa.dry_run(firing_for(ctx, schedule, room=1))
    assert ready["would"] == "run" and ready["file"] == "issues"
    assert ready["browser_steps"] == ["capture-g1"]
    assert workflows(db) == [] and not [p for p in db.docs if p.startswith("qa_target_runs/")]
