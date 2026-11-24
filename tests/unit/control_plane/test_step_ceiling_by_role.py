"""A step's session limits by its ROLE when its caller named none (contract request 158).

The owner accepted request 158 on 2026-11-24: the claude-code runner family
declares `context_ceiling`, an optional object input that LOWERS a step's
session limits (`{"type": "limits", "tokens": 1..200000, "tool_calls":
1..150}`), and the control plane stamps a per-role default where it already
resolves a step's role (`swarm_api.stepceiling`, beside `stepclass.choose`):

  * the input is accepted on the seven claude-code-runner profiles and
    refused on every other, naming the field;
  * a value outside the declared bounds is refused at submission, naming it;
  * the planner, a first review, the re-review, the fix-loop's fix and
    re-review rounds, the CI-fix and conflict-fix rounds each get their
    role's default when the caller named none;
  * a caller's own value wins;
  * a build step is unchanged: no stamp, so today's 200,000 / 150;
  * the signed spec carries the stamped value.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs
from swarm_api import fixloop, issueruns, mergewake
from swarm_api.issueci import ci_fix_continuation
from swarm_api.validation import InvalidInput, parse_issue_ref, rereview_step_for, \
    validate_runner_input

from .conftest import auth_header
from .test_step_class_by_role import (  # noqa: F401 -- fixtures are used by name
    PINS,
    SIGNER,
    _review_shape,
    github,
    pinned_client,
    store,
    waker,
)

KEY = "context_ceiling"
CODE = "claude-code"
REF = parse_issue_ref("example-org/example#7")
#: The seven profiles whose runner is the claude-code runner (request 158).
DECLARING = ("claude-code", "claude-code-large", "claude-code-medium", "claude-code-review",
             "indexer", "claude-code-browser", "claude-code-lightpanda")
#: A caller's own value, inside the bounds and unlike every role default.
CALLERS = {"type": "limits", "tokens": 12_345, "tool_calls": 17}


def _table() -> dict[str, dict[str, int]]:
    """The role table, imported here so a missing module fails each test, not collection."""
    return dict(importlib.import_module("swarm_api.stepceiling").ROLE_CEILINGS)


def _default(role: str) -> dict[str, Any]:
    return {"type": "limits", **_table()[role]}


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", DECLARING)
def test_the_claude_code_family_accepts_the_input(profile):
    value = {"type": "limits", "tokens": 120_000, "tool_calls": 100}
    assert check_inputs(RUNNER_PROFILES[profile], {KEY: value}) == {KEY: value}


@pytest.mark.parametrize("profile", sorted(set(RUNNER_PROFILES) - set(DECLARING)))
def test_every_other_profile_refuses_it_naming_the_field(profile):
    with pytest.raises(InputRefused) as refused:
        check_inputs(RUNNER_PROFILES[profile], {KEY: {"type": "limits", "tokens": 1}})
    assert KEY in str(refused.value)


def test_codex_refuses_it_at_submission_naming_the_field():
    with pytest.raises(InvalidInput) as refused:
        validate_runner_input(RUNNER_PROFILES["codex"], {"prompt": "p", KEY: CALLERS},
                              repository_url="https://github.com/example-org/example")
    assert KEY in str(refused.value)


def test_the_input_is_optional():
    assert not RUNNER_PROFILES[CODE].inputs[KEY].required
    assert check_inputs(RUNNER_PROFILES[CODE], {}) == {}


@pytest.mark.parametrize("value, named", [
    ({"tokens": 1000}, "type"),
    ({"type": "limits", "tokens": 1000, "turns": 3}, "turns"),
    ({"type": "limits", "tokens": 0}, "tokens"),
    ({"type": "limits", "tokens": 200_001}, "tokens"),
    ({"type": "limits", "tool_calls": 0}, "tool_calls"),
    ({"type": "limits", "tool_calls": 151}, "tool_calls"),
    ({"type": "limits", "tokens": True}, "tokens"),
    ({"type": "other", "tokens": 1000}, "type"),
])
def test_an_out_of_bounds_value_is_refused_naming_the_field(value, named):
    with pytest.raises(InvalidInput) as refused:
        validate_runner_input(RUNNER_PROFILES[CODE], {"prompt": "p", KEY: value},
                              repository_url=None)
    assert KEY in str(refused.value)
    assert named in str(refused.value)


def test_the_declared_bounds_are_the_workers_hard_caps():
    ceiling = importlib.import_module("agent_worker.runners.ceiling")
    fields = RUNNER_PROFILES[CODE].inputs[KEY].variants["limits"]
    assert fields["tokens"].minimum == 1 and fields["tool_calls"].minimum == 1
    assert fields["tokens"].maximum == ceiling.CONTEXT_CEILING_HARD_CAP == 200_000
    assert fields["tool_calls"].maximum == ceiling.TOOL_CALL_HARD_CAP == 150


def test_the_worker_reads_the_declared_shape_without_change():
    ceiling = importlib.import_module("agent_worker.runners.ceiling")
    limits = ceiling.resolve_limits({KEY: {"type": "limits", "tokens": 90_000,
                                           "tool_calls": 70}}, CODE)
    assert (limits.tokens, limits.tool_calls) == (90_000, 70)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_every_role_default_is_inside_the_bounds_and_lowers_something():
    table = _table()
    assert set(table) >= {"planner", "review, no risk path", "re-review", "fix", "CI fix",
                          "conflict fix"}
    for role, limits in table.items():
        assert check_inputs(RUNNER_PROFILES[CODE], {KEY: {"type": "limits", **limits}})
        assert limits["tokens"] < 200_000 or limits["tool_calls"] < 150, role


def test_a_build_step_has_no_default():
    assert "build" not in _table()


def test_every_review_role_shares_one_default():
    """The gate re-picks a first review's CLASS after submission; its limits
    were stamped at submission, so every review role must share them."""
    table = _table()
    reviews = ["review, no risk path", "review, risk path", "review, diff not read",
               "review, over the size ceiling", "security review"]
    assert len({json.dumps(table[r], sort_keys=True) for r in reviews}) == 1


# ---------------------------------------------------------------------------
# Each role site
# ---------------------------------------------------------------------------


def test_the_planner_gets_its_default():
    task = issueruns.planner_task(REF, "run_1", class_models=PINS)
    assert task.input[KEY] == _default("planner")


def test_a_replan_keeps_the_first_planners_limits():
    first = issueruns.planner_task(REF, "run_1", class_models=PINS)
    first.input[KEY] = dict(CALLERS)
    run = type("R", (), {"issue": REF, "id": "run_1", "claude_session": None})()
    again = issueruns.replan_task(run, first, [], class_models=PINS)
    assert again.input[KEY] == CALLERS
    fresh = issueruns.replan_task(run, issueruns.planner_task(REF, "run_1"), [])
    assert fresh.input[KEY] == _default("planner")


def test_the_rereview_gets_its_default():
    steps, review = _review_shape()
    step = rereview_step_for(steps, review, "integrate", PINS)
    assert step["input"][KEY] == _default("re-review")


def test_the_rereview_keeps_a_value_the_caller_set_on_the_review():
    steps, review = _review_shape()
    review["input"] = {**review["input"], KEY: dict(CALLERS)}
    step = rereview_step_for(steps, review, "integrate", PINS)
    assert step["input"][KEY] == CALLERS


def test_the_ci_fix_round_gets_its_default():
    spec = ci_fix_continuation(
        continues_task="task_x", repository_url="https://github.com/example-org/example",
        lead="CI is red.", round_no=1, rounds=3, excerpt="boom", step_input={"issue": 7},
        metadata={}, class_models=PINS)
    assert spec.steps[0].input[KEY] == _default("CI fix")
    assert spec.steps[0].input["issue"] == 7


def test_the_ci_fix_round_keeps_a_value_in_the_step_input():
    spec = ci_fix_continuation(
        continues_task="task_x", repository_url="https://github.com/example-org/example",
        lead="CI is red.", round_no=1, rounds=3, excerpt="boom",
        step_input={KEY: dict(CALLERS)}, metadata={}, class_models=PINS)
    assert spec.steps[0].input[KEY] == CALLERS


def test_the_conflict_fix_round_gets_its_default():
    spec = mergewake.conflict_fix_continuation(
        continues_task="task_x", repository_url="https://github.com/example-org/example",
        number=3, url="https://github.com/example-org/example/pull/3", base="main",
        round_no=1, rounds=2, metadata={}, class_models=PINS)
    assert spec.steps[0].input[KEY] == _default("conflict fix")


def _fix_round(profile: str = CODE):
    return fixloop.fix_round_spec(
        root="task_root", repository_url="https://github.com/example-org/example",
        round_no=1, rounds=2, number=3, url="https://github.com/example-org/example/pull/3",
        start_head="a" * 40, findings=("one",), earlier=[], profile=profile,
        model_class=None, prompt=None, note=None, resource_class=None, record={})


def _review_round(profile: str = CODE):
    return fixloop.review_round_spec(
        repository_url="https://github.com/example-org/example", round_no=1, rounds=2,
        head="a" * 40, number=3, findings=(), profile=profile, model_class=None,
        prompt=None, record={})


def test_the_fix_loops_rounds_get_their_defaults():
    assert _fix_round().steps[0].input[KEY] == _default("fix")
    assert _review_round().steps[0].input[KEY] == _default("re-review")


def test_a_round_on_a_profile_that_does_not_declare_it_is_not_stamped():
    assert KEY not in _fix_round("codex").steps[0].input
    assert KEY not in _review_round("codex").steps[0].input


# ---------------------------------------------------------------------------
# Workflow compilation: the first review, the build, the caller, the signature
# ---------------------------------------------------------------------------


def _workflow(client: TestClient, review_input: dict[str, Any],
              implement_input: dict[str, Any] | None = None) -> dict[str, str]:
    spec = {"strategy": "integrate",
            "repository_url": "https://github.com/example-org/example.git",
            "steps": [
                {"step_id": "implement", "runner_profile": CODE,
                 "input": {"prompt": "implement the change", **(implement_input or {})}},
                {"step_id": "review", "runner_profile": CODE, "depends_on": ["implement"],
                 "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
                 "input": {"prompt": "review it; write verdict.json", **review_input}},
                {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
                 "builds_on": "implement", "input_from": {"review": "verdict.json"},
                 "when": {"step": "review", "verdict_in": ["NOT_YET"]},
                 "input": {"prompt": "fix every finding"}},
            ]}
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    return {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}


def _tasks(client: TestClient, db, review_input: dict[str, Any],
           implement_input: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    ids = _workflow(client, review_input, implement_input)
    return {step: db.docs[f"tasks/{task_id}"] for step, task_id in ids.items() if task_id}


def test_a_first_review_gets_the_review_default_and_the_build_is_unchanged(
        pinned_client, db):
    tasks = _tasks(pinned_client, db, {})
    assert tasks["review"]["input"][KEY] == _default("review, diff not read")
    assert KEY not in tasks["implement"]["input"]


def test_a_callers_value_on_the_review_wins(pinned_client, db):
    tasks = _tasks(pinned_client, db, {KEY: dict(CALLERS)})
    assert tasks["review"]["input"][KEY] == CALLERS


def test_a_callers_value_on_a_build_step_is_kept(pinned_client, db):
    tasks = _tasks(pinned_client, db, {}, {KEY: dict(CALLERS)})
    assert tasks["implement"]["input"][KEY] == CALLERS


def test_an_out_of_bounds_step_value_is_refused_at_submission(pinned_client):
    spec = {"strategy": "collect",
            "repository_url": "https://github.com/example-org/example.git",
            "steps": [{"step_id": "implement", "runner_profile": CODE,
                       "input": {"prompt": "p", KEY: {"type": "limits", "tool_calls": 151}}}]}
    response = pinned_client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    assert KEY in response.text and "tool_calls" in response.text


def test_the_signed_spec_carries_the_stamped_value(pinned_client, db):
    tasks = _tasks(pinned_client, db, {})
    review = tasks["review"]
    assert review["input"][KEY] == _default("review, diff not read")
    assert SIGNER.verifies(review, review["id"])
    # A worker handed a different value is handed a spec that does not verify.
    tampered = {**review, "input": {**review["input"],
                                    KEY: {"type": "limits", "tokens": 200_000,
                                          "tool_calls": 150}}}
    assert not SIGNER.verifies(tampered, review["id"])
