"""`POST /v1/workflows` accepts a `fix-loop` step, and refuses everything §4.1 refuses (FL-SUBMIT, part of #2509).

docs/design/fix-loop.md §4.1, §4.6 and §9 (the owner, 2026-10-27). A workflow step
on the catalogue profile `fix-loop` carries a `fix_loop` block naming the
review whose verdict starts the loop, the fix of round 1 (under `integrate`),
`max_rounds` (1-5, default 3, Q5) and optional `fix_round` / `re_review`
overrides that take a runner profile BY NAME, a model class and a prompt and
nothing else (invariant 10). What is held here:

  1. every §4.1 rule is a 422 that names the step and the field, and writes
     no task;
  2. `max_rounds` 0 and 6 are refused, and absent it resolves to 3;
  3. the block is stored in the loop task's signed `metadata.dispatch.fix_loop`
     (the spec digest covers it) and `GET /v1/tasks/{id}` serves it in
     `dispatch`;
  4. `metadata.merge` "on_merge_verdict" with a loop appends ONLY the merge,
     whose signed `merge_target` names the loop (`review_loop`) in place of a
     review, and no re-review (Q8); without a loop it is unchanged;
  5. the loop is never the integrator: under `integrate` the fix still is,
     and integrates the implementer only.

The profile SHIPS UNAVAILABLE (contract request 124): the tests that need it
patch the catalogue entry with `monkeypatch`, never profiles.py, and the first
test pins that, unavailable, the step is refused as disabled.

Measured for §10 before relying on `direct-pr` (FL-SUBMIT's first item): a
`direct-pr` step with `allow_empty_diff: true` that changes nothing ends
SUCCEEDED `no_change` and opens no pull request -- the harvest returns before
deferring a publish (`Worker._harvest_git`, `work.is_empty`), and
`_published_nothing` exempts `no_change`
(tests/unit/worker/test_allow_empty_diff.py::
test_a_pull_request_step_that_may_change_nothing_is_not_published_nothing).
So `direct-pr` loops are accepted.

No credentials, no network, no emulator: every agent step is `mock`.
"""

from __future__ import annotations

import copy
import secrets
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.validation import FIX_LOOP_DEFAULT_ROUNDS, FIX_LOOP_MAX_ROUNDS, FIX_LOOP_PROFILE
from swarm_api.waker import NullWaker
from swarm_common import specsign
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import PROJECT, api_settings, auth_header
from .spec_signer import LocalSpecSigner

REPO = "https://github.com/octo-org/widget-shop.git"
_DROP = object()


@pytest.fixture
def loop_enabled(monkeypatch):
    """`fix-loop`, available for this test only (FL-ACCEPT makes it so for real)."""
    monkeypatch.setitem(RUNNER_PROFILES, FIX_LOOP_PROFILE,
                        replace(RUNNER_PROFILES[FIX_LOOP_PROFILE], available=True))


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


def _apply(step: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if value is _DROP:
            step.pop(key, None)
        else:
            step[key] = value
    return step


def _loop(block: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": "loop", "runner_profile": FIX_LOOP_PROFILE, "depends_on": ["fix"],
        "fix_loop": block if block is not None else {"review": "review", "fix": "fix"},
    }
    return _apply(step, overrides)


def _integrate(loop: dict[str, Any] | None = None, *, fix: dict[str, Any] | None = None,
               review: dict[str, Any] | None = None, **top: Any) -> dict[str, Any]:
    """implement -> review -> fix (gated, the integrator) -> loop, §5.1."""
    spec: dict[str, Any] = {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            _apply({"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
                    "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
                    "input": {"prompt": "review; write verdict.json"}}, review or {}),
            _apply({"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
                    "builds_on": "implement", "input_from": {"review": "verdict.json"},
                    "when": {"step": "review", "verdict_in": ["NOT_YET"]},
                    "input": {"prompt": "fix every finding"}}, fix or {}),
            loop if loop is not None else _loop(),
        ],
    }
    spec.update(top)
    return spec


def _direct(loop: dict[str, Any] | None = None, *, review: dict[str, Any] | None = None,
            **top: Any) -> dict[str, Any]:
    """implement -> review (builds on it, may change nothing) -> loop, §4.1 direct-pr."""
    spec: dict[str, Any] = {
        "strategy": "direct-pr",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            _apply({"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
                    "builds_on": "implement", "allow_empty_diff": True,
                    "input_from": {"implement": "swarm-work.patch"},
                    "input": {"prompt": "review; write verdict.json"}}, review or {}),
            loop if loop is not None else _loop({"review": "review"}, depends_on=["review"]),
        ],
    }
    spec.update(top)
    return spec


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: s for s in body["workflow"]["steps"]}


def _dispatch(db, step: dict[str, Any]) -> dict[str, Any]:
    return db.docs[f"tasks/{step['task_id']}"]["metadata"]["dispatch"]


def _refused(client, db, spec, field: str, *, step_id: str = "loop") -> dict[str, Any]:
    before = {k for k in db.docs if k.startswith(("tasks/", "workflows/"))}
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["field"] == field, body
    assert body["detail"]["step_id"] == step_id, body
    assert f"step {step_id!r}" in body["message"], body
    assert {k for k in db.docs if k.startswith(("tasks/", "workflows/"))} == before, (
        "a refusal wrote something")
    return body


# --------------------------------------------------------------------------
# The profile ships unavailable
# --------------------------------------------------------------------------

def test_an_unavailable_fix_loop_is_refused_as_disabled(client, db):
    assert not RUNNER_PROFILES[FIX_LOOP_PROFILE].available
    response = _post(client, _integrate())
    assert response.status_code == 422, response.text
    assert "not available" in response.text
    assert not [k for k in db.docs if k.startswith("tasks/")]


# --------------------------------------------------------------------------
# Accepted: the block, signed in the loop's dispatch
# --------------------------------------------------------------------------

def test_an_integrate_loop_is_stored_in_the_loops_signed_dispatch(client, db, loop_enabled):
    response = _post(client, _integrate())
    assert response.status_code == 201, response.text
    body = response.json()
    steps = _by_id(body)
    assert list(steps) == ["implement", "review", "fix", "loop"]
    assert _dispatch(db, steps["loop"])["fix_loop"] == {
        "review": "review", "review_task_id": steps["review"]["task_id"],
        "fix": "fix", "fix_task_id": steps["fix"]["task_id"],
        "max_rounds": FIX_LOOP_DEFAULT_ROUNDS,
    }
    for other in ("implement", "review", "fix"):
        assert "fix_loop" not in _dispatch(db, steps[other])
    # The loop is never the integrator: the fix still is, and integrates the
    # implementer only.
    assert body["dispatch"]["integrator_step_id"] == "fix"
    assert _dispatch(db, steps["fix"])["integrates"] == [steps["implement"]["task_id"]]


def test_absent_max_rounds_resolves_to_three(client, db, loop_enabled):
    assert FIX_LOOP_DEFAULT_ROUNDS == 3
    response = _post(client, _integrate())
    assert response.status_code == 201, response.text
    loop = _by_id(response.json())["loop"]
    assert _dispatch(db, loop)["fix_loop"]["max_rounds"] == 3


@pytest.mark.parametrize("rounds", [1, 2, FIX_LOOP_MAX_ROUNDS])
def test_max_rounds_in_range_is_stored_as_asked(client, db, loop_enabled, rounds):
    response = _post(client, _integrate(_loop({"review": "review", "fix": "fix",
                                               "max_rounds": rounds})))
    assert response.status_code == 201, response.text
    assert _dispatch(db, _by_id(response.json())["loop"])["fix_loop"]["max_rounds"] == rounds


def test_the_overrides_are_stored_by_name(client, db, loop_enabled):
    block = {"review": "review", "fix": "fix", "max_rounds": 2,
             "fix_round": {"runner_profile": "mock", "prompt": "you are a fix round"},
             "re_review": {"prompt": "you are a re-review"}}
    response = _post(client, _integrate(_loop(block)))
    assert response.status_code == 201, response.text
    stored = _dispatch(db, _by_id(response.json())["loop"])["fix_loop"]
    assert stored["fix_round"] == {"runner_profile": "mock", "prompt": "you are a fix round"}
    assert stored["re_review"] == {"prompt": "you are a re-review"}


def test_a_direct_pr_loop_is_accepted(client, db, loop_enabled):
    response = _post(client, _direct())
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert _dispatch(db, steps["loop"])["fix_loop"] == {
        "review": "review", "review_task_id": steps["review"]["task_id"],
        "max_rounds": FIX_LOOP_DEFAULT_ROUNDS,
    }


def test_get_task_serves_the_block_in_dispatch(client, db, loop_enabled):
    response = _post(client, _integrate())
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    read = client.get(f"/v1/tasks/{steps['loop']['task_id']}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["task"]["dispatch"]["fix_loop"]["max_rounds"] == 3
    assert read.json()["task"]["dispatch"]["fix_loop"]["review"] == "review"
    plain = client.get(f"/v1/tasks/{steps['fix']['task_id']}", headers=auth_header("alice"))
    assert "fix_loop" not in plain.json()["task"]["dispatch"]


def test_an_override_prompt_is_served_masked_in_metadata_and_the_workflow(
        client, db, loop_enabled):
    # `metadata_value` serves `dispatch` as stored; the loop's block in it
    # carries the caller's override prompt, so it is masked there too.
    literal = "DB_" + "PASSWORD=" + secrets.token_hex(16)
    block = {"review": "review", "fix": "fix",
             "fix_round": {"prompt": f"use {literal} to reach the database"}}
    response = _post(client, _integrate(_loop(block)))
    assert response.status_code == 201, response.text
    loop_id = _by_id(response.json())["loop"]["task_id"]
    task = client.get(f"/v1/tasks/{loop_id}", headers=auth_header("alice")).json()["task"]
    served = task["metadata"]["dispatch"]["fix_loop"]["fix_round"]["prompt"]
    assert literal not in served and served != block["fix_round"]["prompt"], served
    assert literal not in str(task["dispatch"]), task["dispatch"]
    assert task["metadata_redaction_count"] >= 1, task
    workflow_id = response.json()["workflow"]["workflow_id"]
    view = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    assert view.status_code == 200, view.text
    assert literal not in view.text
    assert _dispatch(db, {"task_id": loop_id})["fix_loop"]["fix_round"]["prompt"] == (
        block["fix_round"]["prompt"]), "the stored block is the caller's, unmasked"


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def signed_client(db, tokens, group_map, signer) -> TestClient:
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"), signer=signer,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _digest(doc: dict[str, Any]) -> bytes:
    return specsign.spec_digest(specsign.canonical_step_spec(
        doc, task_id=doc["id"], spec_format=doc["spec_format"]))


@pytest.mark.parametrize("key, value", [
    ("max_rounds", 5),
    ("review_task_id", "task_forged"),
    ("fix_task_id", "task_forged"),
    ("fix_round", {"runner_profile": "mock"}),
])
def test_the_block_changes_the_spec_digest(signed_client, db, signer, loop_enabled, key, value):
    response = _post(signed_client, _integrate())
    assert response.status_code == 201, response.text
    loop = _by_id(response.json())["loop"]
    doc = db.docs[f"tasks/{loop['task_id']}"]
    assert signer.verifies(doc, doc["id"])
    tampered = copy.deepcopy(doc)
    tampered["metadata"]["dispatch"]["fix_loop"][key] = value
    assert _digest(tampered) != _digest(doc)
    assert not signer.verifies(tampered, doc["id"])


# --------------------------------------------------------------------------
# metadata.merge with a loop: only the merge, reading the loop (Q8, §4.6)
# --------------------------------------------------------------------------

def test_on_merge_verdict_with_a_loop_appends_only_a_merge_reading_the_loop(
    client, db, loop_enabled,
):
    response = _post(client, _integrate(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    body = response.json()
    steps = _by_id(body)
    assert list(steps) == ["implement", "review", "fix", "loop", "merge"]
    merge = steps["merge"]
    assert merge["depends_on"] == ["fix", "loop"]
    assert merge["input_from"] == {}
    assert _dispatch(db, merge)["merge_target"] == {
        "pull_request": steps["fix"]["task_id"],
        "review_loop": steps["loop"]["task_id"],
    }
    assert body["dispatch"]["integrator_step_id"] == "fix"


def test_merge_on_with_a_loop_reads_the_loop_too(client, db, loop_enabled):
    response = _post(client, _integrate(metadata={"merge": "on"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "loop", "merge"]
    assert _dispatch(db, steps["merge"])["merge_target"] == {
        "pull_request": steps["fix"]["task_id"],
        "review_loop": steps["loop"]["task_id"],
    }


def test_a_direct_pr_loop_merges_the_implementers_pull_request(client, db, loop_enabled):
    response = _post(client, _direct(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "loop", "merge"]
    assert steps["merge"]["depends_on"] == ["implement", "loop"]
    assert _dispatch(db, steps["merge"])["merge_target"] == {
        "pull_request": steps["implement"]["task_id"],
        "review_loop": steps["loop"]["task_id"],
    }


def test_a_stated_merge_behind_a_loop_must_read_the_loop(client, db, loop_enabled):
    spec = _integrate(metadata={"merge": "on"})
    spec["steps"].append({"step_id": "merge", "runner_profile": "merge",
                          "depends_on": ["fix", "loop"]})
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert _dispatch(db, steps["merge"])["merge_target"]["review_loop"] == steps["loop"]["task_id"]

    spec["steps"][-1] = {"step_id": "merge", "runner_profile": "merge",
                         "depends_on": ["fix", "review"], "input_from": {"review": "verdict.json"}}
    response = _post(client, spec)
    assert response.status_code == 422, response.text


def test_on_merge_verdict_without_a_loop_is_unchanged(client, db):
    spec = _integrate(metadata={"merge": "on_merge_verdict"})
    spec["steps"].pop()
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "re-review", "merge"]
    assert _dispatch(db, steps["merge"])["merge_target"] == {
        "pull_request": steps["fix"]["task_id"],
        "review": steps["re-review"]["task_id"],
        "verdict_file": "verdict.json",
    }


# --------------------------------------------------------------------------
# Refusals: each a 422 naming the step, nothing written (§4.1)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rounds", [0, 6, -1, True, "3", 2.5, None])
def test_max_rounds_out_of_range_is_refused(client, db, loop_enabled, rounds):
    block = {"review": "review", "fix": "fix", "max_rounds": rounds}
    _refused(client, db, _integrate(_loop(block)), "fix_loop.max_rounds")


def test_collect_is_refused(client, db, loop_enabled):
    spec = _integrate(strategy="collect")
    for step in spec["steps"]:
        step.pop("builds_on", None)
    _refused(client, db, spec, "strategy")


def test_a_block_on_another_profile_is_refused(client, db, loop_enabled):
    spec = _integrate()
    spec["steps"][2]["fix_loop"] = {"review": "review", "fix": "fix"}
    _refused(client, db, spec, "fix_loop", step_id="fix")


def test_a_fix_loop_step_without_a_block_is_refused(client, db, loop_enabled):
    _refused(client, db, _integrate(_loop(fix_loop=_DROP)), "fix_loop")


@pytest.mark.parametrize("block", [["review"], "review", 3])
def test_a_block_that_is_not_an_object_is_refused(client, db, loop_enabled, block):
    _refused(client, db, _integrate(_loop(fix_loop=block)), "fix_loop")


def test_an_unknown_key_in_the_block_is_refused_naming_it(client, db, loop_enabled):
    block = {"review": "review", "fix": "fix", "budget": {"usd": 5}}
    _refused(client, db, _integrate(_loop(block)), "fix_loop.budget")


@pytest.mark.parametrize("field, value", [
    ("resource_class", "standard"),
    ("timeout_seconds", 60),
    ("builds_on", "implement"),
    ("when", {"step": "review", "verdict_in": ["NOT_YET"]}),
    ("allow_empty_diff", True),
])
def test_a_container_field_on_the_loop_is_refused_naming_it(client, db, loop_enabled,
                                                            field, value):
    _refused(client, db, _integrate(_loop(**{field: value})), field)


def test_two_loops_are_refused(client, db, loop_enabled):
    spec = _integrate()
    spec["steps"].append(_loop(step_id="loop-2", depends_on=["loop"]))
    _refused(client, db, spec, "runner_profile", step_id="loop-2")


def test_staging_from_the_loop_is_refused(client, db, loop_enabled):
    spec = _integrate()
    spec["steps"].append({"step_id": "after", "runner_profile": "mock",
                          "depends_on": ["loop"], "input_from": {"loop": "verdict.json"},
                          "input": {"prompt": "read it"}})
    body = _refused(client, db, spec, "input_from")
    assert body["detail"]["staged_by"] == ["after"]


@pytest.mark.parametrize("review", [None, "", "nope", "loop"])
def test_a_review_that_is_not_a_step_upstream_is_refused(client, db, loop_enabled, review):
    _refused(client, db, _integrate(_loop({"review": review, "fix": "fix"})), "fix_loop.review")


def test_a_review_that_is_not_an_ancestor_is_refused(client, db, loop_enabled):
    spec = _direct(_loop({"review": "sibling"}, depends_on=["review"]))
    spec["steps"].insert(1, {"step_id": "sibling", "runner_profile": "mock",
                             "input": {"prompt": "unrelated"}})
    _refused(client, db, spec, "fix_loop.review")


# -- under integrate --------------------------------------------------------

def test_integrate_without_a_fix_is_refused(client, db, loop_enabled):
    _refused(client, db, _integrate(_loop({"review": "review"})), "fix_loop.fix")


def test_integrate_with_a_fix_that_is_not_the_integrator_is_refused(client, db, loop_enabled):
    spec = _integrate(_loop({"review": "review", "fix": "implement"}))
    _refused(client, db, spec, "fix_loop.fix")


def test_integrate_with_an_ungated_fix_is_refused(client, db, loop_enabled):
    _refused(client, db, _integrate(fix={"when": _DROP}), "fix_loop.fix")


@pytest.mark.parametrize("verdicts", [["MERGE"], ["NOT_YET", "MERGE"]])
def test_integrate_with_a_fix_gated_on_other_verdicts_is_refused(client, db, loop_enabled,
                                                                 verdicts):
    spec = _integrate(fix={"when": {"step": "review", "verdict_in": verdicts}})
    _refused(client, db, spec, "fix_loop.fix")


def test_integrate_with_a_fix_gated_on_another_review_is_refused(client, db, loop_enabled):
    spec = _integrate()
    spec["steps"].insert(2, {"step_id": "review-b", "runner_profile": "mock",
                             "depends_on": ["review"], "input": {"prompt": "review again"}})
    fix = spec["steps"][3]
    fix["depends_on"] = ["review-b"]
    fix["input_from"] = {"review-b": "verdict.json"}
    fix["when"] = {"step": "review-b", "verdict_in": ["NOT_YET"]}
    _refused(client, db, spec, "fix_loop.fix")


def test_integrate_with_the_loop_not_depending_on_the_fix_is_refused(client, db, loop_enabled):
    _refused(client, db, _integrate(_loop(depends_on=["review"])), "depends_on")


# -- under direct-pr --------------------------------------------------------

def test_direct_pr_with_a_fix_is_refused(client, db, loop_enabled):
    spec = _direct(_loop({"review": "review", "fix": "implement"}, depends_on=["review"]))
    _refused(client, db, spec, "fix_loop.fix")


def test_direct_pr_with_the_loop_not_depending_on_the_review_is_refused(
    client, db, loop_enabled,
):
    # The review is upstream of the loop, through `notes`, but not its parent.
    spec = _direct(_loop({"review": "review"}, depends_on=["notes"]))
    spec["steps"].insert(2, {"step_id": "notes", "runner_profile": "mock",
                             "depends_on": ["review"], "input": {"prompt": "take notes"}})
    _refused(client, db, spec, "depends_on")


def test_direct_pr_with_a_review_that_builds_on_nothing_is_refused(client, db, loop_enabled):
    _refused(client, db, _direct(review={"builds_on": _DROP}), "fix_loop.review")


def test_direct_pr_with_a_review_that_may_not_change_nothing_is_refused(
    client, db, loop_enabled,
):
    _refused(client, db, _direct(review={"allow_empty_diff": False}), "fix_loop.review")


# -- the overrides: a profile by name, a model class, a prompt (invariant 10)

@pytest.mark.parametrize("which", ["fix_round", "re_review"])
@pytest.mark.parametrize("key, value", [
    ("image", "gcr.io/attacker/image:latest"),
    ("command", ["sh", "-c", "id"]),
    ("resources", {"cpu": "64"}),
    ("resource_class", "large"),
    ("runtime", "browser_if_ui"),
    ("backend", "gke"),
])
def test_an_override_takes_nothing_but_a_profile_a_model_class_and_a_prompt(
    client, db, loop_enabled, which, key, value,
):
    block = {"review": "review", "fix": "fix", which: {key: value}}
    _refused(client, db, _integrate(_loop(block)), f"fix_loop.{which}.{key}")


@pytest.mark.parametrize("which", ["fix_round", "re_review"])
@pytest.mark.parametrize("profile", ["no-such-profile", "merge", FIX_LOOP_PROFILE, "await-input"])
def test_an_override_profile_must_be_an_agent_profile_the_platform_runs(
    client, db, loop_enabled, which, profile,
):
    block = {"review": "review", "fix": "fix", which: {"runner_profile": profile}}
    _refused(client, db, _integrate(_loop(block)), f"fix_loop.{which}.runner_profile")


@pytest.mark.parametrize("which", ["fix_round", "re_review"])
def test_an_override_model_class_the_profile_does_not_take_is_refused(
    client, db, loop_enabled, which,
):
    block = {"review": "review", "fix": "fix", which: {"model_class": "claude-opus-5-5"}}
    _refused(client, db, _integrate(_loop(block)), f"fix_loop.{which}.model_class")


@pytest.mark.parametrize("which", ["fix_round", "re_review"])
@pytest.mark.parametrize("value", [["a"], 3, "x" * (16 * 1024 + 1), ""])
def test_an_override_prompt_that_is_not_a_bounded_string_is_refused(
    client, db, loop_enabled, which, value,
):
    block = {"review": "review", "fix": "fix", which: {"prompt": value}}
    _refused(client, db, _integrate(_loop(block)), f"fix_loop.{which}.prompt")


@pytest.mark.parametrize("which", ["fix_round", "re_review"])
def test_an_override_that_is_not_an_object_is_refused(client, db, loop_enabled, which):
    block = {"review": "review", "fix": "fix", which: "claude-code"}
    _refused(client, db, _integrate(_loop(block)), f"fix_loop.{which}")
