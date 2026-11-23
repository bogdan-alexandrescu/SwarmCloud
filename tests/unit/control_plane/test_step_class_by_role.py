"""A step's model class by its ROLE when its caller named none (the owner, 2026-11-23).

docs/design/model-per-step.md §4.12. `swarm_api.stepclass.ROLE_CLASSES` is
the one table; every site calls `stepclass.choose`:

  * each role gets its default: the planner `fable`; a first review that
    touches no risk path, the re-review, the CI-fix and conflict-fix rounds
    and a fix loop's re-review rounds `sonnet`; build, fix, security review
    and a first review on a risk path or an unread diff `opus`;
  * a class the caller named always wins;
  * a class the deployment does not pin falls back to the profile's default,
    as `issueruns._check_class` does;
  * a first review is decided at the review gate, where its diff is read: a
    diff read whole on no risk path is re-signed onto `sonnet`, a risk path
    or a cut file list keeps the `opus` it was signed with;
  * the schedules pass the planner's class;
  * nothing changes for a profile that runs no Claude model.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.profiles import RUNNER_PROFILES
from swarm_api import fixloop, issueruns, mergewake, scheduletypes, stepclass
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueci import ci_fix_continuation
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.schedtypes import issue_sweep
from swarm_api.validation import StepSpec, parse_issue_ref, rereview_step_for

from .conftest import api_settings, seed_task, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_verdict_publish import (  # noqa: F401 -- fixtures are used by name
    IMPL,
    SWEEPER,
    _push,
    github,
    store,
    waker,
)

CODE = "claude-code"
#: Every class pinned, as a deployment whose terraform pins them all.
PINS = {name: f"claude-{name}-pin" for name in RUNNER_PROFILES[CODE].model_classes}
REF = parse_issue_ref("example-org/example#7")


# ---------------------------------------------------------------------------
# The table and the rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role, expected", [
    (stepclass.ROLE_PLANNER, "fable"),
    (stepclass.ROLE_BUILD, None),
    (stepclass.ROLE_REVIEW, "sonnet"),
    (stepclass.ROLE_REVIEW_RISK, None),
    (stepclass.ROLE_REVIEW_UNREAD, None),
    (stepclass.ROLE_SECURITY_REVIEW, None),
    (stepclass.ROLE_FIX, None),
    (stepclass.ROLE_REREVIEW, "sonnet"),
    (stepclass.ROLE_CI_FIX, "sonnet"),
    (stepclass.ROLE_CONFLICT_FIX, "sonnet"),
])
def test_each_role_gets_its_default(role, expected):
    choice = stepclass.choose(role, CODE, PINS)
    # `opus` is the profile's default: sent as nothing, so it signs as before.
    assert choice.model_class == expected
    assert choice.chosen_by == f"role default: {role}"


def test_the_table_is_the_owners():
    assert dict(stepclass.ROLE_CLASSES) == {
        "planner": "fable", "build": "opus",
        "review, no risk path": "sonnet", "review, risk path": "opus",
        "review, diff not read": "opus", "security review": "opus", "fix": "opus",
        "re-review": "sonnet", "CI fix": "sonnet", "conflict fix": "sonnet",
    }


@pytest.mark.parametrize("role", list(stepclass.ROLE_CLASSES))
def test_an_explicit_class_wins(role):
    choice = stepclass.choose(role, CODE, PINS, explicit="haiku")
    assert (choice.model_class, choice.chosen_by) == ("haiku", stepclass.CALLER)


@pytest.mark.parametrize("pins", [None, {}, {"opus": "claude-opus-pin"}])
def test_an_unpinned_class_falls_back_to_the_profile_default(pins):
    for role in (stepclass.ROLE_PLANNER, stepclass.ROLE_REVIEW, stepclass.ROLE_CI_FIX):
        choice = stepclass.choose(role, CODE, pins)
        assert choice.model_class is None
        assert "not pinned, profile default" in choice.chosen_by


@pytest.mark.parametrize("profile", ["codex", "mock"])
def test_a_profile_that_runs_no_claude_model_gets_no_class(profile):
    assert not RUNNER_PROFILES[profile].model_classes
    for role in stepclass.ROLE_CLASSES:
        assert stepclass.choose(role, profile, PINS).model_class is None


def test_only_a_callers_stored_class_is_explicit():
    assert stepclass.explicit_class({"model_class": "haiku"}) == "haiku"
    assert stepclass.explicit_class(
        {"model_class": "haiku", "metadata": {"class_chosen_by": "caller"}}) == "haiku"
    assert stepclass.explicit_class(
        {"model_class": "sonnet",
         "metadata": {"class_chosen_by": "role default: review, no risk path"}}) is None
    assert stepclass.explicit_class({"model_class": None}) is None


# ---------------------------------------------------------------------------
# The planner, and the schedules that start runs
# ---------------------------------------------------------------------------


def test_the_planner_runs_fable_when_the_caller_named_none():
    task = issueruns.planner_task(REF, "run_1", class_models=PINS)
    assert task.model_class == "fable"
    assert task.metadata["class_chosen_by"] == "role default: planner"


def test_the_planners_explicit_class_wins():
    task = issueruns.planner_task(REF, "run_1", model_class="opus", class_models=PINS)
    assert task.model_class == "opus"
    assert task.metadata["class_chosen_by"] == "caller"


def test_an_unpinned_planner_class_runs_the_profile_default():
    task = issueruns.planner_task(REF, "run_1", class_models={"opus": "claude-opus-pin"})
    assert task.model_class is None


@pytest.mark.parametrize("model", [scheduletypes.IssueSweepParams,
                                   scheduletypes.IssuePlanOnlyParams,
                                   scheduletypes.FailureReviewParams])
def test_the_schedules_carry_a_planner_class_defaulting_to_fable(model):
    assert model.model_validate({}).planner_model_class == "fable"
    assert model.model_validate({"planner_model_class": "sonnet"}).planner_model_class == "sonnet"
    with pytest.raises(ValueError):
        model.model_validate({"planner_model_class": "claude-opus-5-5"})


def test_the_schedules_pass_the_planner_class():
    schedule = {"type": "issue-sweep", "gate": {"plan": "approve", "merge": "off"}}
    # The default is the role default: `planner_task` applies it, with its pin fallback.
    assert issue_sweep.run_options(schedule, 3, planner_model_class="fable") \
        .planner_model_class is None
    assert issue_sweep.run_options(schedule, 3, planner_model_class="sonnet") \
        .planner_model_class == "sonnet"


def test_the_schedule_executors_hand_start_run_the_class(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_start_run(ctx, owner, ref, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after the call")

    monkeypatch.setattr(issue_sweep, "start_run", fake_start_run)

    class _Firing:
        ctx = None
        owner = None
        schedule = {"schedule_id": "sch_1"}
        firing = {"firing_id": "f_1"}
        mark = {"schedule_id": "sch_1"}

        def recorded(self, entry):  # pragma: no cover - nothing adopted
            return entry

    class _Candidate:
        ref = REF
        snapshot = None

    class _Selection:
        adopted: list = []
        candidates = [_Candidate()]
        room = 1
        start_failures: list = []

    monkeypatch.setattr(issue_sweep, "open_work_without", lambda snapshot, number: None)
    options = issue_sweep.RunOptions(plan_approval="auto", auto_merge=False,
                                     merge_approval=None, fix_rounds=3,
                                     planner_model_class="sonnet")
    with pytest.raises(RuntimeError):
        issue_sweep.fire(_Firing(), _Selection(), options)
    assert seen["model_class"] == "sonnet"


# ---------------------------------------------------------------------------
# The re-review, the CI-fix and conflict-fix rounds, the fix loop's rounds
# ---------------------------------------------------------------------------


def _review_shape(review_class: str | None = None):
    steps = [
        StepSpec(step_id="implement", depends_on=(), input_from={}, when_step=None,
                 when_verdicts=(), builds_on=None, runner_profile=CODE),
        StepSpec(step_id="review", depends_on=("implement",),
                 input_from={"implement": "swarm-work.patch"}, when_step=None,
                 when_verdicts=(), builds_on="implement", runner_profile=CODE),
        StepSpec(step_id="fix", depends_on=("review",), input_from={"review": "verdict.json"},
                 when_step="review", when_verdicts=("NOT_YET",), builds_on="implement",
                 runner_profile=CODE),
    ]
    review = {"step_id": "review", "runner_profile": CODE,
              "input": {"prompt": "review it"}, "input_from": {"implement": "swarm-work.patch"},
              "metadata": {}, "model_class": review_class}
    return steps, review


def test_the_rereview_runs_sonnet():
    steps, review = _review_shape()
    step = rereview_step_for(steps, review, "integrate", PINS)
    assert step is not None
    assert step["model_class"] == "sonnet"
    assert step["metadata"]["class_chosen_by"] == "role default: re-review"


def test_the_rereview_keeps_a_class_the_caller_named_on_the_review():
    steps, review = _review_shape("haiku")
    step = rereview_step_for(steps, review, "integrate", PINS)
    assert step["model_class"] == "haiku"


def test_an_unpinned_rereview_runs_the_profile_default():
    steps, review = _review_shape()
    assert "model_class" not in rereview_step_for(steps, review, "integrate", None)


def _ci_fix(pins):
    return ci_fix_continuation(
        continues_task="task_x", repository_url="https://github.com/example-org/example",
        lead="CI is red.", round_no=1, rounds=3, excerpt="boom", step_input={},
        metadata={}, class_models=pins)


def _conflict_fix(pins):
    return mergewake.conflict_fix_continuation(
        continues_task="task_x", repository_url="https://github.com/example-org/example",
        number=3, url="https://github.com/example-org/example/pull/3", base="main",
        round_no=1, rounds=2, metadata={}, class_models=pins)


@pytest.mark.parametrize("build, role", [(_ci_fix, "CI fix"), (_conflict_fix, "conflict fix")])
def test_the_ci_fix_and_conflict_fix_rounds_run_sonnet(build, role):
    step = build(PINS).steps[0]
    assert step.model_class == "sonnet"
    assert step.metadata["class_chosen_by"] == f"role default: {role}"
    assert build(None).steps[0].model_class is None


def test_the_fix_loops_rereview_rounds_run_sonnet_and_its_fix_rounds_opus():
    review_doc = {"runner_profile": CODE, "model_class": "sonnet",
                  "metadata": {"class_chosen_by": "role default: review, no risk path"}}
    loop = type("L", (), {"block": {}})()
    profile, model_class, _ = fixloop._override(loop, "re_review", review_doc)
    # A class a role default chose for the review is not the caller's.
    assert model_class is None
    rereview = stepclass.choose(stepclass.ROLE_REREVIEW, profile, PINS, explicit=model_class)
    assert rereview.model_class == "sonnet"
    fix = stepclass.choose(stepclass.ROLE_FIX, CODE, PINS)
    assert fix.model_class is None  # opus, the profile's default
    spec = fixloop.review_round_spec(
        repository_url="https://github.com/example-org/example", round_no=1, rounds=2,
        head="a" * 40, number=3, findings=(), profile=CODE, model_class=rereview.model_class,
        chosen_by=rereview.chosen_by, prompt=None, record={})
    assert spec.steps[0].model_class == "sonnet"
    assert spec.steps[0].metadata["class_chosen_by"] == "role default: re-review"


def test_a_loops_override_class_still_wins():
    loop = type("L", (), {"block": {"re_review": {"model_class": "opus"}}})()
    _, model_class, _ = fixloop._override(loop, "re_review", {"runner_profile": CODE})
    assert model_class == "opus"


# ---------------------------------------------------------------------------
# The first review: decided at the review gate
# ---------------------------------------------------------------------------

SIGNER = LocalSpecSigner()
REVIEW = "task_review0001"
SMALL_FILE = "apps/scheduler/scheduler/loop.py"
RISK_FILE = "apps/agent-worker/agent_worker/secrets.py"
SWEEPER_TOKEN = "token-sweeper"


@pytest.fixture
def pinned_client(db, tokens, group_map, objects, github, waker, store) -> TestClient:
    seed_tenant(db, "eng")
    tokens = dict(tokens)
    tokens[SWEEPER_TOKEN] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    from swarm_api import forgewrite

    context = build_context(
        settings=api_settings(
            rollup_sweeper_users=(SWEEPER,),
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
            worker_class_models=dict(PINS),
        ),
        db=db, signer=SIGNER, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=waker,
        metrics=ApiMetrics(), objects=objects, forge_tokens=store,
        forge_writer=forgewrite.GitHubWriter(send=github, locate=github.locate,
                                             fetch=github.fetch),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _parent(db, *paths: str, lines: int, truncated: bool = False) -> None:
    doc = seed_task(db, task_id=IMPL, tenant_id="eng", state="SUCCEEDED", runner_profile=CODE)
    doc["result_summary"] = {"git": {
        "files": [{"path": p, "status": "M"} for p in paths], "files_truncated": truncated,
        "insertions": lines, "deletions": 0}}
    doc["completed_at"] = datetime.now(timezone.utc)


def _review(db, *, marked: bool = True, model_class: str | None = None,
            runner_profile: str = CODE) -> dict[str, Any]:
    doc = seed_task(db, task_id=REVIEW, tenant_id="eng", state="PARKED",
                    park_reason="DEPENDENCY_INCOMPLETE", runner_profile=runner_profile,
                    provider="anthropic", depends_on=(IMPL,))
    doc["model_class"] = model_class
    doc["metadata"] = {"review_gate": {"min_lines": 300},
                       **({"class_chosen_by": stepclass.REVIEW_AT_GATE} if marked else {})}
    return SIGNER.sign_document(doc, REVIEW)


def _decide(client, db) -> dict[str, Any]:
    response = _push(client, IMPL)
    assert response.status_code == 200, response.text
    doc = db.docs[f"tasks/{REVIEW}"]
    assert doc["metadata"]["control_publish"]["state"] == "declined", doc["metadata"]
    return doc


def test_a_review_touching_no_risk_path_is_resigned_onto_sonnet(pinned_client, db):
    _parent(db, SMALL_FILE, lines=400)
    _review(db)

    doc = _decide(pinned_client, db)

    assert doc["model_class"] == "sonnet"
    assert doc["model"] == PINS["sonnet"]
    assert doc["metadata"]["class_chosen_by"] == "role default: review, no risk path"
    # Signed again by swarm-api: the worker's check passes on the new class.
    assert SIGNER.verifies(doc, REVIEW)


def test_a_review_touching_a_risk_path_stays_on_opus(pinned_client, db):
    _parent(db, RISK_FILE, lines=3)
    before = dict(_review(db))

    doc = _decide(pinned_client, db)

    assert doc["model_class"] is None  # the profile's default, opus
    assert doc["spec_signature"] == before["spec_signature"]
    assert doc["metadata"]["class_chosen_by"] == "role default: review, risk path"


def test_a_review_whose_file_list_was_cut_stays_on_opus(pinned_client, db):
    _parent(db, SMALL_FILE, lines=400, truncated=True)
    _review(db)

    doc = _decide(pinned_client, db)

    assert doc["model_class"] is None
    assert doc["metadata"]["class_chosen_by"] == "role default: review, diff not read"


def test_a_review_whose_caller_named_a_class_is_not_touched(pinned_client, db):
    _parent(db, SMALL_FILE, lines=400)
    before = dict(_review(db, marked=False, model_class="haiku"))

    doc = _decide(pinned_client, db)

    assert doc["model_class"] == "haiku"
    assert doc["spec_signature"] == before["spec_signature"]
    assert "class_chosen_by" not in doc["metadata"]


def test_a_review_on_a_profile_with_no_claude_model_is_not_touched(pinned_client, db):
    _parent(db, SMALL_FILE, lines=400)
    before = dict(_review(db, runner_profile="mock"))

    doc = _decide(pinned_client, db)

    assert doc.get("model_class") is None
    assert doc["spec_signature"] == before["spec_signature"]


def test_a_gated_claude_review_is_marked_for_the_gate_at_submission(client, db):
    from .conftest import auth_header

    spec = {"strategy": "integrate", "repository_url": "https://github.com/example-org/example.git",
            "steps": [
                {"step_id": "implement", "runner_profile": "mock",
                 "input": {"prompt": "implement the change"}},
                {"step_id": "review", "runner_profile": CODE, "depends_on": ["implement"],
                 "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
                 "input": {"prompt": "review it; write verdict.json"}},
                {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
                 "builds_on": "implement", "input_from": {"review": "verdict.json"},
                 "when": {"step": "review", "verdict_in": ["NOT_YET"]},
                 "input": {"prompt": "fix every finding"}},
            ]}
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    tasks = {s["step_id"]: db.docs[f"tasks/{s['task_id']}"]
             for s in response.json()["workflow"]["steps"]}
    assert tasks["review"]["metadata"]["class_chosen_by"] == stepclass.REVIEW_AT_GATE
    assert tasks["review"].get("model_class") is None  # signed on opus until the gate
    assert "class_chosen_by" not in tasks["implement"]["metadata"]
