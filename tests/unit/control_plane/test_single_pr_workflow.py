"""The `single-pr` strategy: one pull request, merged by the chain itself (#295).

The chain is implement -> review -> post-verdict -> fix -> proof -> merge
(docs/merge-step.md §3). Every agent step declares a `pr_role`; the two
worker-action steps, `post-verdict` and `merge`, run no agent and hold none.
The API writes the role, and the upstream TASK ids each role needs, into the
step's signed dispatch block:

  * `pr_role` on every step (`none` on a worker action);
  * `pr_author`, the author's task id, on a reader and an amender, whose
    worker derives the branch `swarm/<author task id>` from it;
  * `verdict_source: {review: <task id>}` on post-verdict (§4.3);
  * `merges: {author, review, post-verdict, fix, proof}` on merge (§4.1).

This file is the contract for what the API accepts, refuses and stores. The
merge and post-verdict profiles are DISABLED in the frozen catalogue until
#342 is enforced and the owner creates the Apps; every acceptance test below
enables them for its own duration only, and the first test proves the chain
is refused without that.

No credentials, no network, no emulator: the agent steps run `mock`.
"""

from __future__ import annotations

import copy
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
from swarm_api.validation import (
    DISPATCH_STRATEGIES,
    PR_ROLES,
    SINGLE_PR,
    dispatchable_strategies,
)
from swarm_api.waker import NullWaker
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import PROJECT, api_settings, auth_header
from .spec_signer import LocalSpecSigner

REPO = "https://github.com/saga-xyz/example.git"

#: The profiles a `single-pr` chain needs and the catalogue disables (#342).
_CHAIN_PROFILES = ("merge", "post-verdict")


@pytest.fixture
def chain_enabled(monkeypatch):
    """The merge and post-verdict profiles, available for this test only."""
    for name in _CHAIN_PROFILES:
        monkeypatch.setitem(RUNNER_PROFILES, name, replace(RUNNER_PROFILES[name], available=True))


def _chain() -> dict[str, Any]:
    """docs/workflows.md's chain, with `mock` agents.

    `fix` depends on `review` as well as `post-verdict`: it stages review.json,
    and `validate_dag` requires every `input_from` source to be a direct
    dependency. What keeps its agent behind the posted verdict is the edge to
    `post-verdict`, which is the one the rules below require.
    """
    return {
        "on_step_failure": "fail_workflow",
        "repository_url": REPO,
        "repository_ref": "main",
        "strategy": "single-pr",
        "steps": [
            {"step_id": "implement", "runner_profile": "mock", "pr_role": "author",
             "input": {"prompt": "implement; write pr-title.txt"}},
            {"step_id": "review", "runner_profile": "mock", "pr_role": "reader",
             "depends_on": ["implement"],
             "input": {"prompt": "review; write review.json"}},
            {"step_id": "post-verdict", "runner_profile": "post-verdict",
             "depends_on": ["review"], "input": {}},
            {"step_id": "fix", "runner_profile": "mock", "pr_role": "amender",
             "depends_on": ["review", "post-verdict"],
             "input_from": {"review": "review.json"},
             "input": {"prompt": "fix every finding in review.json"}},
            {"step_id": "proof", "runner_profile": "mock", "pr_role": "reader",
             "depends_on": ["fix"],
             "input": {"prompt": "prove it; write proof.json"}},
            {"step_id": "merge", "runner_profile": "merge",
             "depends_on": ["post-verdict", "proof"],
             "input_from": {"proof": "proof.json"}, "input": {}},
        ],
    }


def _step(spec: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(s for s in spec["steps"] if s["step_id"] == step_id)


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _task_docs_by_step(db, body) -> dict[str, dict[str, Any]]:
    return {
        s["step_id"]: db.docs[f"tasks/{s['task_id']}"] for s in body["workflow"]["steps"]
    }


def _assert_refused(client, db, spec, code: str) -> dict:
    before = {k for k in db.docs if k.startswith("tasks/")}
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == code, body
    assert {k for k in db.docs if k.startswith("tasks/")} == before, "a refusal created tasks"
    return body


# --------------------------------------------------------------------------
# The strategy exists, and stays unreachable while its profiles are disabled
# --------------------------------------------------------------------------

def test_single_pr_is_an_accepted_strategy():
    assert SINGLE_PR == "single-pr"
    assert SINGLE_PR in DISPATCH_STRATEGIES
    assert PR_ROLES == ("author", "reader", "amender", "none")


def test_the_chain_is_refused_while_the_merge_profiles_are_disabled(client, db):
    """The owner's decision (2026-10-01): disabled for every tenant until #342."""
    assert not RUNNER_PROFILES["merge"].available
    body = _assert_refused(client, db, _chain(), "validation_failed")
    assert body["detail"]["disabled"] is True


def test_single_pr_is_not_dispatchable_until_its_profiles_are(monkeypatch):
    assert SINGLE_PR not in dispatchable_strategies()
    assert set(dispatchable_strategies()) == set(DISPATCH_STRATEGIES) - {SINGLE_PR}
    for name in _CHAIN_PROFILES:
        monkeypatch.setitem(RUNNER_PROFILES, name, replace(RUNNER_PROFILES[name], available=True))
    assert dispatchable_strategies() == DISPATCH_STRATEGIES


# --------------------------------------------------------------------------
# The chain is accepted, and every step's dispatch block says what it needs
# --------------------------------------------------------------------------

def test_the_chain_is_accepted_and_each_block_names_its_upstream_tasks(
    client, db, chain_enabled
):
    response = _post(client, _chain())
    assert response.status_code == 201, response.text
    docs = _task_docs_by_step(db, response.json())
    ids = {step: doc["id"] for step, doc in docs.items()}
    blocks = {step: doc["metadata"]["dispatch"] for step, doc in docs.items()}

    for block in blocks.values():
        assert block["strategy"] == "single-pr"
        # Not `integrate`: no step integrates anything.
        assert "role" not in block and "integrates" not in block

    assert blocks["implement"]["pr_role"] == "author"
    assert "pr_author" not in blocks["implement"]
    for step, role in (("review", "reader"), ("fix", "amender"), ("proof", "reader")):
        assert blocks[step]["pr_role"] == role
        # A task id, never a branch: the worker derives `swarm/<id>`.
        assert blocks[step]["pr_author"] == ids["implement"]

    assert blocks["post-verdict"]["pr_role"] == "none"
    assert blocks["post-verdict"]["verdict_source"] == {"review": ids["review"]}
    assert "pr_author" not in blocks["post-verdict"]
    # post-verdict reads review.json from the path its own signed block
    # derives, never through `input_from`'s staging (§4.3).
    assert "input_from" not in docs["post-verdict"]["metadata"]

    assert blocks["merge"]["pr_role"] == "none"
    assert blocks["merge"]["merges"] == {
        "author": ids["implement"],
        "review": ids["review"],
        "post-verdict": ids["post-verdict"],
        "fix": ids["fix"],
        "proof": ids["proof"],
    }
    assert docs["merge"]["metadata"]["input_from"] == {ids["proof"]: "proof.json"}
    # Only the merge and the post-verdict steps carry these.
    for step in ("implement", "review", "fix", "proof"):
        assert "merges" not in blocks[step] and "verdict_source" not in blocks[step]
    assert "verdict_source" not in blocks["merge"]
    assert "merges" not in blocks["post-verdict"]


def test_a_chain_without_a_fix_names_no_fix(client, db, chain_enabled):
    spec = _chain()
    spec["steps"] = [s for s in spec["steps"] if s["step_id"] != "fix"]
    _step(spec, "proof")["depends_on"] = ["post-verdict"]
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    docs = _task_docs_by_step(db, response.json())
    assert set(docs["merge"]["metadata"]["dispatch"]["merges"]) == {
        "author", "review", "post-verdict", "proof",
    }


def test_a_caller_may_state_merges_and_it_must_agree_with_the_graph(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "merge")["merges"] = {
        "author": "implement", "review": "review", "post-verdict": "post-verdict",
        "fix": "fix", "proof": "proof",
    }
    response = _post(client, spec)
    assert response.status_code == 201, response.text

    spec = _chain()
    _step(spec, "merge")["merges"] = {
        "author": "implement", "review": "proof", "post-verdict": "post-verdict",
        "fix": "fix", "proof": "review",
    }
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "merge"


def test_merges_is_refused_on_any_step_but_the_merge(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "proof")["merges"] = {"author": "implement"}
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "proof"


# --------------------------------------------------------------------------
# The signed spec covers the new keys
# --------------------------------------------------------------------------

@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def signed_client(db, tokens, group_map, signer) -> TestClient:
    context = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def test_pr_role_merges_and_verdict_source_are_inside_the_signature(
    signed_client, db, signer, chain_enabled
):
    response = _post(signed_client, _chain())
    assert response.status_code == 201, response.text
    docs = _task_docs_by_step(db, response.json())
    for step, doc in docs.items():
        assert signer.verifies(doc, doc["id"]), step

    def tampered(step: str, key: str, value: Any) -> bool:
        doc = copy.deepcopy(docs[step])
        doc["metadata"]["dispatch"][key] = value
        return signer.verifies(doc, doc["id"])

    # T10 (§7): a rewritten block no longer verifies.
    other = docs["fix"]["id"]
    assert not tampered("merge", "merges", {**docs["merge"]["metadata"]["dispatch"]["merges"],
                                            "review": other})
    assert not tampered("post-verdict", "verdict_source", {"review": other})
    assert not tampered("review", "pr_role", "author")
    assert not tampered("fix", "pr_author", other)


# --------------------------------------------------------------------------
# What the API refuses (docs/merge-step.md §3)
# --------------------------------------------------------------------------

def test_single_pr_is_refused_for_a_single_task(client, db, chain_enabled):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x"},
              "strategy": "single-pr", "repository_url": REPO},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"


def test_single_pr_needs_a_repository(client, db, chain_enabled):
    spec = _chain()
    del spec["repository_url"]
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["missing"] == "repository_url"


def test_exactly_one_author(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "implement")["pr_role"] = "reader"
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["authors"] == []

    spec = _chain()
    _step(spec, "review")["pr_role"] = "author"
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["authors"] == ["implement", "review"]


def test_the_author_is_an_ancestor_of_every_other_step(client, db, chain_enabled):
    spec = _chain()
    spec["steps"].insert(1, {"step_id": "side", "runner_profile": "mock", "pr_role": "reader",
                             "input": {"prompt": "x"}})
    _step(spec, "proof")["depends_on"] = ["fix", "side"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["not_downstream_of_author"] == ["side"]


@pytest.mark.parametrize("profile", ["merge", "post-verdict"])
def test_exactly_one_of_each_worker_action(client, db, chain_enabled, profile):
    spec = _chain()
    spec["steps"] = [s for s in spec["steps"] if s["runner_profile"] != profile]
    for s in spec["steps"]:
        s["depends_on"] = [d for d in s.get("depends_on", []) if d != profile]
        s["input_from"] = {k: v for k, v in s.get("input_from", {}).items() if k != profile}
    if profile == "post-verdict":
        _step(spec, "fix")["depends_on"] = ["review"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["runner_profile"] == profile
    assert body["detail"]["steps"] == []

    spec = _chain()
    twin = copy.deepcopy(next(s for s in spec["steps"] if s["runner_profile"] == profile))
    twin["step_id"] = f"{profile}-2"
    spec["steps"].append(twin)
    if profile == "post-verdict":
        _step(spec, "merge")["depends_on"].append(twin["step_id"])
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["runner_profile"] == profile
    assert sorted(body["detail"]["steps"]) == sorted([profile, twin["step_id"]])


def test_merge_is_the_only_sink(client, db, chain_enabled):
    spec = _chain()
    spec["steps"].append({"step_id": "after", "runner_profile": "mock", "pr_role": "reader",
                          "depends_on": ["proof", "post-verdict"], "input": {"prompt": "x"}})
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert sorted(body["detail"]["terminal_steps"]) == ["after", "merge"]


def test_post_verdict_stages_nothing(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "post-verdict")["input_from"] = {"review": "review.json"}
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "post-verdict"


def test_post_verdict_depends_on_one_reader_and_nothing_else(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "post-verdict")["depends_on"] = ["review", "implement"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "post-verdict"

    # Its one dependency must be the review, a reader: the author's own work
    # is not a verdict.
    spec = _chain()
    _step(spec, "post-verdict")["depends_on"] = ["implement"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "post-verdict"


def test_the_fix_depends_on_post_verdict_directly(client, db, chain_enabled):
    """T3a (§7): without the edge the fix's agent could act before the verdict is posted."""
    spec = _chain()
    _step(spec, "fix")["depends_on"] = ["review"]
    _step(spec, "proof")["depends_on"] = ["fix", "post-verdict"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "fix"


def test_every_step_after_the_review_waits_for_the_posted_verdict(client, db, chain_enabled):
    spec = _chain()
    spec["steps"].insert(2, {"step_id": "second-look", "runner_profile": "mock",
                             "pr_role": "reader", "depends_on": ["review"],
                             "input": {"prompt": "x"}})
    _step(spec, "proof")["depends_on"] = ["fix", "second-look"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "second-look"


def test_the_merge_depends_on_post_verdict_directly(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "merge")["depends_on"] = ["proof"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "merge"


@pytest.mark.parametrize(
    "input_from",
    [{}, {"proof": "outcome.json"}, {"proof": "proof.json", "fix": "notes.md"},
     {"fix": "proof.json"}],
)
def test_the_merge_stages_exactly_the_proofs_proof_json(client, db, chain_enabled, input_from):
    spec = _chain()
    merge = _step(spec, "merge")
    merge["input_from"] = input_from
    merge["depends_on"] = sorted({"post-verdict", "proof", *input_from})
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "merge"


def test_no_step_stages_from_post_verdict(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "fix")["input_from"] = {"review": "review.json", "post-verdict": "x.json"}
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "fix"


def test_at_most_one_amender(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "proof")["pr_role"] = "amender"
    _step(spec, "proof")["depends_on"] = ["fix", "post-verdict"]
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["amenders"] == ["fix", "proof"]


# --------------------------------------------------------------------------
# `pr_role` and the worker-action profiles belong to `single-pr` alone
# --------------------------------------------------------------------------

def test_an_agent_step_must_declare_its_role(client, db, chain_enabled):
    spec = _chain()
    del _step(spec, "proof")["pr_role"]
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "proof"


@pytest.mark.parametrize("role", ["none", "integrator", "Author"])
def test_an_agent_step_role_must_be_a_real_one(client, db, chain_enabled, role):
    spec = _chain()
    _step(spec, "proof")["pr_role"] = role
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "proof"


def test_a_worker_action_holds_no_role_but_none(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "post-verdict")["pr_role"] = "none"
    _step(spec, "merge")["pr_role"] = "none"
    response = _post(client, spec)
    assert response.status_code == 201, response.text

    spec = _chain()
    _step(spec, "merge")["pr_role"] = "reader"
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "merge"


@pytest.mark.parametrize("strategy", ["collect", "direct-pr", "integrate"])
def test_pr_role_is_refused_outside_single_pr(client, db, strategy):
    spec = {
        "strategy": strategy, "repository_url": REPO,
        "steps": [
            {"step_id": "a", "runner_profile": "mock", "pr_role": "author",
             "input": {"prompt": "x"}},
            {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"],
             "input": {"prompt": "y"}},
        ],
    }
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "a"


@pytest.mark.parametrize("strategy", ["collect", "direct-pr", "integrate"])
@pytest.mark.parametrize("profile", _CHAIN_PROFILES)
def test_a_worker_action_profile_is_refused_outside_single_pr(
    client, db, chain_enabled, strategy, profile
):
    spec = {
        "strategy": strategy, "repository_url": REPO,
        "steps": [
            {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "x"}},
            {"step_id": "b", "runner_profile": profile, "depends_on": ["a"], "input": {}},
        ],
    }
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["runner_profile"] == profile


@pytest.mark.parametrize("profile", _CHAIN_PROFILES)
def test_a_worker_action_profile_is_refused_as_a_task(client, db, chain_enabled, profile):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": profile, "input": {}, "strategy": "direct-pr",
              "repository_url": REPO},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    assert response.json()["detail"]["runner_profile"] == profile
    assert not [k for k in db.docs if k.startswith("tasks/")]


def test_a_worker_action_step_cannot_be_gated(client, db, chain_enabled):
    spec = _chain()
    merge = _step(spec, "merge")
    merge["input_from"] = {"proof": "proof.json"}
    merge["when"] = {"step": "proof", "verdict_in": ["MERGE"]}
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "merge"


def test_builds_on_is_refused_under_single_pr(client, db, chain_enabled):
    spec = _chain()
    _step(spec, "review")["builds_on"] = "implement"
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "review"
    assert body["detail"]["strategy"] == "single-pr"
