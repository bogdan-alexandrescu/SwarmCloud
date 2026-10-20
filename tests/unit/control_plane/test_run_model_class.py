"""An issue run's model classes (#1435, lane MODEL-RUNS of
docs/design/model-per-step.md §6; owner decisions 2026-10-17).

  * `RunCreate.model_class` is the PLANNER's class and reaches the planner
    task; a run naming none submits exactly today's planner (runs `opus`).
  * The planner may write a `model_class` on each plan step (R1 (a)). It is
    part of the plan, so it is inside `plan_digest` and approved with it, and
    it reaches that step of the compiled workflow, chain or stages.
  * The compiled review and fix carry no class and run `opus`, whatever the
    plan's steps named.
  * Until the submission schemas carry `model_class` (lane MODEL-API) the
    planner is not offered the key and a fresh plan naming one is refused, so
    no approved plan compiles to a workflow the schema refuses (R4).

The `classed` fixture stands in for MODEL-API's schema fields with
subclasses, so these hold the issue-run side whether or not that lane has
merged. No credentials, no network, no emulator.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi import Response
from pydantic import Field

from swarm_common.identity import Principal
from swarm_common.profiles import RUNNER_PROFILES, step_model_class

from swarm_api import issueruns
from swarm_api.auth import AuthContext
from swarm_api.issueruns import (
    FIX_STEP,
    REVIEW_STEP,
    InvalidPlan,
    ModelClassNotAccepted,
    UnknownModelClass,
    compile_plan,
    parse_plan,
    plan_digest,
    planner_prompt,
    refuse_planner_model_class,
)
from swarm_api.routes import runs as runs_route
from swarm_api.schemas import RunCreate, TaskCreate, WorkflowCreate, WorkflowStepCreate
from swarm_api.validation import parse_issue_ref

from .conftest import ENG_GROUP
from .test_issue_runs import (  # noqa: F401 -- fixtures, used by name
    PLAN,
    REF,
    _create,
    _docs,
    _finish_planner,
    _run,
    _stored_run,
    api_context,
    forge_tokens,
    github,
)

PROFILE = RUNNER_PROFILES[issueruns.STEP_PROFILE]


class _ClassedTask(TaskCreate):
    model_class: str | None = Field(default=None, max_length=32)


class _ClassedStep(WorkflowStepCreate):
    model_class: str | None = Field(default=None, max_length=32)


class _ClassedWorkflow(WorkflowCreate):
    steps: list[_ClassedStep] = Field(min_length=1)


class _ClassedRun(RunCreate):
    model_class: str | None = Field(default=None, max_length=32)


@pytest.fixture
def classed(monkeypatch):
    """The schemas as lane MODEL-API makes them: a task and a step carry a class."""
    monkeypatch.setattr(issueruns, "TaskCreate", _ClassedTask)
    monkeypatch.setattr(issueruns, "WorkflowStepCreate", _ClassedStep)
    monkeypatch.setattr(issueruns, "WorkflowCreate", _ClassedWorkflow)


@pytest.fixture
def uncarried(monkeypatch):
    """The schemas as they are before lane MODEL-API: no class anywhere."""
    monkeypatch.setattr(issueruns, "model_classes_carried", lambda: False)


@pytest.fixture
def submitted(api_context, monkeypatch):
    """Every TaskCreate the run submits, as it reached `submit_tasks`."""
    seen: list = []
    real = api_context.submissions.submit_tasks

    def recording(auth, specs, **kwargs):
        seen.extend(specs)
        return real(auth, specs, **kwargs)

    monkeypatch.setattr(api_context.submissions, "submit_tasks", recording)
    return seen


def _alice() -> AuthContext:
    return AuthContext(
        principal=Principal(email="alice@example.com", subject="sub-alice@example.com",
                            domain="example.com", groups=()),
        tenant_id="eng", is_admin=False, tenant_principal=ENG_GROUP,
    )


def _classed_plan(**classes: str) -> dict:
    plan = json.loads(json.dumps(PLAN))
    for step in plan["steps"]:
        if step["step_id"] in classes:
            step["model_class"] = classes[step["step_id"]]
    return plan


def _staged(plan: dict) -> dict:
    """The same plan with `depends_on` on every step: it compiles to stages."""
    staged = json.loads(json.dumps(plan))
    staged["steps"][0]["depends_on"] = []
    staged["steps"][1]["depends_on"] = [staged["steps"][0]["step_id"]]
    return staged


# --------------------------------------------------------------------------
# the planner
# --------------------------------------------------------------------------

def test_a_runs_model_class_reaches_its_planner_task(
    classed, client, api_context, submitted, monkeypatch
):
    # A deployment that pins a model for every class: with none pinned, MODEL-API
    # refuses a non-default class `model_class_not_yet_available` at submission.
    service = api_context.submissions
    monkeypatch.setattr(service, "_settings", dataclasses.replace(
        service._settings,
        worker_class_models={name: f"claude-{name}-pin" for name in PROFILE.model_classes},
    ))
    body = _ClassedRun(issue=REF, model_class="fable")
    made = runs_route.create_run(body, Response(), auth=_alice(), ctx=api_context)
    assert made["run"]["state"] == "PLANNING"
    (planner,) = submitted
    assert planner.runner_profile == issueruns.PLANNER_PROFILE
    assert planner.model_class == "fable"


def test_a_run_naming_no_class_submits_todays_planner(client, db, submitted):
    created = _create(client)
    assert created.status_code == 201, created.text
    (planner,) = submitted
    # Not set, so not stored and not signed: the planner runs the default.
    assert "model_class" not in planner.model_fields_set
    assert step_model_class(PROFILE, getattr(planner, "model_class", None)) == "opus"
    assert db.docs[f"tasks/{created.json()['run']['planner_task_id']}"].get("model_class") is None


def test_the_planner_task_carries_a_class_only_when_one_was_named(classed):
    ref = parse_issue_ref(REF)
    assert issueruns.planner_task(ref, "run_x", model_class="haiku").model_class == "haiku"
    assert "model_class" not in issueruns.planner_task(ref, "run_x").model_fields_set


@pytest.mark.parametrize("name", ["opus", "fable", "sonnet", "haiku"])
def test_every_class_of_the_planners_profile_is_accepted(name):
    refuse_planner_model_class(name)


def test_a_class_outside_the_vocabulary_is_refused_naming_the_four():
    with pytest.raises(UnknownModelClass) as refused:
        refuse_planner_model_class("gpt-" + "large")
    assert refused.value.status_code == 422
    assert refused.value.code == "unknown_model_class"
    assert refused.value.detail["model_classes"] == ["opus", "fable", "sonnet", "haiku"]


def test_a_class_the_planners_profile_does_not_run_is_refused(monkeypatch):
    narrow = dataclasses.replace(PROFILE, model_classes=("opus",))
    monkeypatch.setitem(RUNNER_PROFILES, issueruns.PLANNER_PROFILE, narrow)
    with pytest.raises(ModelClassNotAccepted) as refused:
        refuse_planner_model_class("haiku")
    assert refused.value.code == "model_class_not_accepted"
    assert refused.value.detail["model_classes"] == ["opus"]


def test_a_refused_planner_class_creates_nothing(classed, client, db, api_context, submitted):
    body = _ClassedRun(issue=REF, model_class="gpt-" + "large")
    with pytest.raises(UnknownModelClass):
        runs_route.create_run(body, Response(), auth=_alice(), ctx=api_context)
    assert submitted == []
    assert not _docs(db, "tasks") and not _docs(db, issueruns.RUNS_COLLECTION)


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------

def test_a_plan_steps_class_is_inside_the_plan_and_its_digest(classed):
    plan = parse_plan(_classed_plan(**{"ui": "haiku"}))
    assert plan["steps"][1]["model_class"] == "haiku"
    assert "model_class" not in plan["steps"][0]
    assert plan_digest(plan) != plan_digest(parse_plan(PLAN))
    # Changing only the class changes the digest: approval covers it.
    assert plan_digest(plan) != plan_digest(parse_plan(_classed_plan(**{"ui": "sonnet"})))


def test_a_plan_naming_no_class_digests_as_it_did(classed):
    assert parse_plan(PLAN) == parse_plan(json.loads(json.dumps(PLAN)))
    assert plan_digest(parse_plan(PLAN)) == plan_digest(PLAN)


def test_the_planners_classes_are_shown_on_the_run_and_digested(classed, client, db, objects):
    created = _create(client)
    assert created.status_code == 201, created.text
    plan = _classed_plan(**{"sort-key": "sonnet"})
    _finish_planner(db, objects, created.json()["run"], plan)
    run = _run(client, created.json()["run"]["id"]).json()["run"]
    assert run["state"] == "PLANNED", run
    assert run["plan"]["steps"][0]["model_class"] == "sonnet"
    assert run["plan_digest"] == plan_digest(parse_plan(plan))


@pytest.mark.parametrize("name", ["gpt-" + "large", "OPUS", ""])
def test_a_plan_step_naming_a_class_its_profile_does_not_run_is_refused(classed, name):
    with pytest.raises(InvalidPlan) as refused:
        parse_plan(_classed_plan(**{"ui": name}))
    assert "model_class" in refused.value.message


def test_a_plan_naming_a_class_is_refused_while_steps_cannot_carry_one(uncarried):
    with pytest.raises(InvalidPlan) as refused:
        parse_plan(_classed_plan(**{"ui": "haiku"}))
    assert "not yet accepted" in refused.value.message
    # A plan naming none is unaffected.
    assert parse_plan(PLAN) == PLAN


# --------------------------------------------------------------------------
# the compiled workflow
# --------------------------------------------------------------------------

@pytest.mark.parametrize("shape", [lambda p: p, _staged], ids=["chain", "stages"])
def test_a_plan_steps_class_reaches_its_step(classed, shape):
    plan = shape(_classed_plan(**{"ui": "haiku"}))
    spec = compile_plan(_stored_run(plan=plan, plan_digest=plan_digest(plan)))
    by_id = {s.step_id: s for s in spec.steps}
    assert by_id["impl-ui"].model_class == "haiku"
    # The step that named none is not stored a class, so it runs the default.
    assert "model_class" not in by_id["impl-sort-key"].model_fields_set
    assert step_model_class(PROFILE, by_id["impl-sort-key"].model_class) == "opus"


@pytest.mark.parametrize("shape", [lambda p: p, _staged], ids=["chain", "stages"])
def test_review_and_fix_run_opus_whatever_the_plan_named(classed, shape):
    plan = shape(_classed_plan(**{"sort-key": "haiku", "ui": "sonnet"}))
    spec = compile_plan(_stored_run(plan=plan, plan_digest=plan_digest(plan)))
    by_id = {s.step_id: s for s in spec.steps}
    for sid in (REVIEW_STEP, FIX_STEP):
        assert "model_class" not in by_id[sid].model_fields_set, sid
        assert step_model_class(PROFILE, by_id[sid].model_class) == "opus", sid


# --------------------------------------------------------------------------
# what the planner is told
# --------------------------------------------------------------------------

def test_the_planner_is_offered_the_classes_once_steps_carry_them(classed):
    prompt = planner_prompt(parse_issue_ref(REF), run_id="run_x")
    assert '"model_class"' in prompt
    for name in PROFILE.model_classes:
        assert f'"{name}"' in prompt
    assert "The review and the fix always run the default." in prompt


def test_the_planner_is_told_nothing_of_classes_before_steps_carry_them(uncarried):
    prompt = planner_prompt(parse_issue_ref(REF), run_id="run_x")
    assert "model_class" not in prompt
    assert issueruns._PLAN_SHAPE in prompt
