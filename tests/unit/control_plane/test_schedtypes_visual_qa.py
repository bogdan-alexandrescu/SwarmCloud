"""The `visual-qa` schedule type (docs/schedules.md §3.17; lane VQ-7 TYPE of
docs/design/agent-autonomy.md §6, designed in docs/design/visual-qa-any-app.md
§6; part of #1933).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT. Its file exists and declares its budget caps, so the
  catalogue serves it available and the tick's real loader imports it.
* THE MINIMUM INTERVAL IS 24 HOURS: a cron that fires more often is refused
  when the schedule is built.
* ITS PARAMETERS ARE `target`, a QA target id, and `environments`, the
  names of its legs (default: every entry); a caller can name nothing else
  about the run (invariant 10). The names are checked against the target at
  create, edit and every firing (qa-environments.md §7, QAENV-RUN).
* A FIRING IS ONE WORK ITEM PER LEG, within the firing's room, its caps $80
  a leg (BGD, 2026-10-26, for 132-flow legs), `max_concurrent` 2 and $160 a
  day (BGD, 2026-10-23, Q9 (a)).
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


def test_the_schedules_caps_are_80_a_leg_2_at_once_and_160_a_day() -> None:
    # QA run 5b (wf_025ae735511546d99eff: 17 steps, 362 screenshots) cost $23.62;
    # BGD set 40 / 80 / 1 on 2026-10-22 (VQ7-CAPS, #1933) for one leg, and on
    # 2026-10-23 (qa-environments.md §10, Q9 (a)) $40 a leg, max_concurrent 2 and
    # $160 a day: a firing is one work item per leg, so a two-leg app keeps "the
    # daily firing plus one run-now" for both legs. On 2026-10-26 BGD raised the
    # leg to $80: 132 flows at 2 widths at the bench median of $0.14 a flow run
    # are about $37 before capture, review and the report (QA-TRIPWIRE-MAC).
    # MUTATION: keep the 2026-10-23 per_run_usd=40 and this is (40, 160, 2).
    caps = scheduletypes.budget_caps(scheduletypes.get(TYPE))
    assert (caps.per_run_usd, caps.per_day_usd, caps.max_concurrent) == (80, 160, 2)
    # The day still holds at least one leg, so the default budget resolves.
    assert caps.per_day_usd >= caps.per_run_usd


def test_a_creator_may_set_lower_caps_never_higher() -> None:
    entry = scheduletypes.get(TYPE)
    # The caps are the defaults a schedule gets when it names none.
    assert schedules.resolve_budget(entry, None) == {
        "per_run_usd": 80.0, "per_day_usd": 160.0, "max_concurrent": 2,
    }
    # Lower figures are the creator's to choose, the old $40 a leg among them.
    lowered = schedules.resolve_budget(
        entry, schedules.BudgetIn(per_run_usd=40.0, per_day_usd=80.0, max_concurrent=1))
    assert lowered == {"per_run_usd": 40.0, "per_day_usd": 80.0, "max_concurrent": 1}
    # MUTATION: per_run_usd=81 in BUDGET_CAPS and the first of these is accepted.
    for above in ({"per_run_usd": 80.01}, {"per_day_usd": 160.01}, {"max_concurrent": 3}):
        with pytest.raises(schedules.ScheduleInvalid) as raised:
            schedules.resolve_budget(entry, schedules.BudgetIn(**above))
        assert raised.value.code == "budget_above_cap", above


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
    # `environments` unset is every entry of the target (QAENV-RUN), stored as the default.
    assert doc["params"] == {"target": "qat_" + "1" * 16, "environments": None}


@pytest.mark.parametrize("params", [
    {},
    {"target": "oracle"},
    {"target": "qat_" + "1" * 16, "base_url": "https://elsewhere.example.com"},
    {"target": "qat_" + "1" * 16, "screens": ["home"]},
    {"target": "qat_" + "1" * 16, "environments": []},
    {"target": "qat_" + "1" * 16, "environments": ["Staging"]},
    {"target": "qat_" + "1" * 16, "environments": ["staging", "staging"]},
    {"target": "qat_" + "1" * 16, "environments": [{"name": "staging", "base_url": "https://x.example.com"}]},
], ids=["missing", "not-an-id", "a-url", "a-screen-list", "no-environment", "a-bad-name", "a-repeated-name",
        "an-entry-not-a-name"])
def test_the_parameters_are_a_target_id_and_environment_names(params) -> None:
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
    real_render = qatargets.render_run

    def render_and_keep(*args: Any, **kwargs: Any):
        answer = real_render(*args, **kwargs)
        rendered.append(answer)
        return answer

    # The firing renders each leg through `QaTargets.runnable_legs` (QAENV-RUN).
    monkeypatch.setattr(qatargets, "render_run", render_and_keep)
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


# ---------------------------------------------------------------------------
# Environments: one work item per leg (qa-environments.md §2.2, §7; QAENV-RUN)
# ---------------------------------------------------------------------------


def two_leg_target(db, resolver, *, approve: tuple[str, ...] = ("staging", "production")) -> dict[str, Any]:
    """Carol's app with a staging and a production leg, each on its own host."""
    body = target_body()
    body.pop("base_url")
    body["environments"] = [
        {"name": "staging", "base_url": "https://widgets.example.com", "environment": "non_production"},
        {"name": "production", "base_url": "https://shop.example.com"},
    ]
    target = _targets(db, resolver).create(CAROL_TENANT, body, by=CAROL)
    hosts = {"staging": "widgets.example.com", "production": "shop.example.com"}
    for name in approve:
        target = _targets(db, resolver).approve_host(target["id"], host=hosts[name], extra_origin=None, by=ROOT,
                                                     environment=name)
    db.docs[f"repositories/{CAROL_SHOP}"]["agents_may_file_issues"] = True
    return target


def leg_schedule(db, target_id: str, environments: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"target": target_id}
    if environments is not None:
        params["environments"] = environments
    return make_schedule(db, type_=TYPE, scope_mode="all", params=params, tenant_id=CAROL_TENANT, owner=CAROL,
                         cron="0 6 * * *", max_concurrent=2, **kwargs)


def runs(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("qa_target_runs/")]


def test_a_firing_is_one_work_item_per_leg(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver)
    schedule = leg_schedule(db, target["id"])

    items = visual_qa.create(firing_for(ctx, schedule, room=2))

    # MUTATION: run only the first entry in `create` and this is one item.
    assert len(items) == 2 and len(workflows(db)) == 2
    marks = {}
    for item in items:
        made = [t for t in tasks(db) if t.get("workflow_id") == item["id"]]
        (mark,) = {tuple(sorted(t["metadata"]["qa_target"].items())) for t in made
                   if (t.get("metadata") or {}).get("qa_target")}
        marks[dict(mark)["environment"]] = item["id"]
    # Each workflow carries its own leg's mark only.
    assert set(marks) == {"staging", "production"}
    rows = {r["environment"]: r for r in runs(db)}
    assert {name: rows[name]["workflow_id"] for name in rows} == marks
    # The two legs are one group, as a dispatch by hand is.
    (group_id,) = {r["group_id"] for r in rows.values()}
    group = db.docs[f"qa_run_groups/{group_id}"]
    assert group["schedule_id"] == SCHEDULE_ID
    assert {g["environment"]: g["state"] for g in group["legs"]} == {"staging": "submitted",
                                                                     "production": "submitted"}


def test_a_firing_with_room_for_one_leg_starts_one_and_says_why_not_the_other(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver)
    schedule = leg_schedule(db, target["id"])
    firing = firing_for(ctx, schedule, room=1)

    items = visual_qa.create(firing)

    # MUTATION: submit every leg whatever the room and `Firing._spend` raises RoomExceeded.
    assert len(items) == 1 and len(workflows(db)) == 1
    assert firing.explanation["not_started"] == [{"environment": "production", "reason": "no_room"}]
    (group,) = [d for p, d in db.docs.items() if p.startswith("qa_run_groups/")]
    assert {g["environment"]: g["state"] for g in group["legs"]} == {"staging": "submitted",
                                                                     "production": "not_started"}


def test_a_firing_runs_only_the_legs_its_schedule_names(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver, approve=("staging",))
    schedule = leg_schedule(db, target["id"], ["staging"])

    (item,) = visual_qa.create(firing_for(ctx, schedule, room=2))

    # Production's host is pending, and the schedule does not name it: staging runs.
    # MUTATION: run every entry whatever `environments` names and production's refusal ends the firing.
    (row,) = runs(db)
    assert row["environment"] == "staging" and row["workflow_id"] == item["id"]


def test_every_named_leg_is_checked_before_a_firing_submits_any(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver, approve=("staging",))
    leg_schedule(db, target["id"])

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    # MUTATION: submit staging before checking production and its workflow exists.
    assert firing["state"] == schedulefire.REFUSED, firing
    assert firing["skip"]["code"] == qatargets.QaTargetHostPending.code
    assert workflows(db) == [] and runs(db) == []


def test_environments_are_checked_against_the_target_at_create_and_edit(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver)
    schedule = leg_schedule(db, target["id"], ["staging", "qa"])

    with pytest.raises(ApiError) as caught:
        visual_qa.check_target(ctx, schedule, auth_of(ctx, schedule))
    # MUTATION: check only the target in `check_target` and a schedule names a leg that is not there.
    assert caught.value.code == visual_qa.ENVIRONMENT_NOT_FOUND
    assert caught.value.detail == {"environment": "qa"}
    # CONTROL: names the target has are accepted.
    ok = leg_schedule(db, target["id"], ["production"])
    assert visual_qa.check_target(ctx, ok, auth_of(ctx, ok)) is None


def test_environments_are_checked_again_at_every_firing(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver)
    leg_schedule(db, target["id"], ["staging", "production"])
    # The admin removes the production leg after the schedule was made.
    doc = db.docs[f"qa_targets/{target['id']}"]
    doc["environments"] = [e for e in doc["environments"] if e["name"] == "staging"]

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    # MUTATION: skip the names nobody has any more and the firing runs staging alone, unasked.
    assert firing["state"] == schedulefire.REFUSED, firing
    assert firing["skip"]["code"] == "qa_target_environment_unknown"
    assert workflows(db) == []


def test_a_dry_run_names_every_leg_it_would_run(db, ctx, resolver) -> None:
    target = two_leg_target(db, resolver)
    schedule = leg_schedule(db, target["id"])

    answer = visual_qa.dry_run(firing_for(ctx, schedule, room=2))

    assert answer["would"] == "run"
    # MUTATION: dry-run the first entry only and production is missing.
    assert [leg["environment"] for leg in answer["legs"]] == ["staging", "production"]
    assert workflows(db) == []
