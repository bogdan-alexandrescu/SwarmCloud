"""How a caller chooses the way their work gets merged.

Two fields, `strategy` and `carrier`, decided by the owner and written down in
docs/design/dispatch-and-integration.md sections 4.2 and 4.3. This file is the
contract for what the API accepts, what it refuses, and what it stores -- the
WORKER side is a separate lane and nothing here asserts anything about it.

Three properties are worth more than the rest, and each has its own section:

  * THE DEFAULT CHANGES NOTHING. A submission that names neither field must
    produce exactly the task it produced before this feature existed, because
    a deployment whose tenant token is read-only has to keep working.

  * AN UNKNOWN VALUE IS REFUSED, and the refusal names what is accepted. The
    alternative -- ignoring it -- is a caller who asked for one pull request,
    was silently given three, and finds out from their reviewers.

  * `integrate` WITH NOTHING TO INTEGRATE IS AN ERROR, not a quiet downgrade to
    `collect`. That is stated in the task this lane was given and it is the
    single most tempting thing to get wrong, because downgrading always looks
    friendlier at the moment you write it.

No credentials, no network, no emulator: `runner_profile: "mock"` has no
provider, so every task here reaches READY on the in-memory Firestore.
"""

from __future__ import annotations

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.validation import (
    DEFAULT_CARRIER,
    DEFAULT_STRATEGY,
    DISPATCH_CARRIERS,
    DISPATCH_STRATEGIES,
    DispatchOptions,
    StepSpec,
    reject_reserved_metadata,
    resolve_dispatch_options,
    resolve_integrator_step,
)

from .conftest import auth_header

REPO = "https://github.com/saga-xyz/example.git"


def _task_doc(db, task_id: str) -> dict:
    return db.docs[f"tasks/{task_id}"]


def _steps_by_id(workflow: dict) -> dict:
    return {s["step_id"]: s for s in workflow["steps"]}


# --------------------------------------------------------------------------
# The default changes nothing
# --------------------------------------------------------------------------

def test_the_documented_defaults_are_todays_behaviour():
    """`collect` over `checkpoints`: harvest the patch, push nothing."""
    assert DEFAULT_STRATEGY == "collect"
    assert DEFAULT_CARRIER == "checkpoints"
    assert DISPATCH_STRATEGIES == ("collect", "direct-pr", "integrate")
    assert DISPATCH_CARRIERS == ("checkpoints", "branches")


def test_a_submission_naming_neither_field_gets_the_defaults(client, db):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    )
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["dispatch"] == {
        "strategy": "collect",
        "carrier": "checkpoints",
        "role": None,
        "integrates": [],
    }
    stored = _task_doc(db, task["id"])
    assert stored["metadata"]["dispatch"] == {
        "strategy": "collect",
        "carrier": "checkpoints",
    }
    # And nothing else about the task moved: it is admissible immediately, it
    # holds no repository, and it carries no role.
    assert stored["state"] == "READY"
    assert stored["repository_url"] is None


def test_the_default_needs_no_repository(client):
    """The read-only-token deployment. `collect` must never require a repo."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "collect", "carrier": "checkpoints"},
    )
    assert response.status_code == 201, response.text


def test_a_task_predating_the_feature_reads_back_as_collect(client, db, api_store):
    """A stored task with no dispatch block is not reported as null.

    Absent means today's behaviour, and "today's behaviour" has a name.
    """
    created = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    )
    task_id = created.json()["task"]["id"]
    # Take the block back out, which is exactly what a document written before
    # this feature existed looks like.
    _task_doc(db, task_id)["metadata"] = {}

    read = client.get(f"/v1/tasks/{task_id}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["task"]["dispatch"] == {
        "strategy": "collect",
        "carrier": "checkpoints",
        "role": None,
        "integrates": [],
    }


# --------------------------------------------------------------------------
# An unknown value is refused, and the refusal names what is accepted
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["pr", "COLLECT", "direct_pr", "merge", ""])
def test_an_unknown_strategy_is_refused_by_name(client, bad):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": bad},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    # The accepted values are in the MESSAGE, not only in the detail: a curl
    # user reads the message.
    for accepted in DISPATCH_STRATEGIES:
        assert accepted in body["message"]
    assert body["detail"]["accepted_strategies"] == list(DISPATCH_STRATEGIES)


@pytest.mark.parametrize("bad", ["branch", "tarballs", "Checkpoints", "gcs"])
def test_an_unknown_carrier_is_refused_by_name(client, bad):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "carrier": bad},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    for accepted in DISPATCH_CARRIERS:
        assert accepted in body["message"]
    assert body["detail"]["accepted_carriers"] == list(DISPATCH_CARRIERS)


def test_an_unknown_strategy_on_a_workflow_is_refused_the_same_way(client, db):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "squash",
            "steps": [{"step_id": "a", "runner_profile": "mock"}],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    # And nothing was written. A refusal that leaves half a workflow behind is
    # worse than no refusal at all.
    assert not [key for key in db.docs if key.startswith("workflows/")]
    assert not [key for key in db.docs if key.startswith("tasks/")]


def test_an_unknown_value_in_a_batch_refuses_the_whole_batch(client, db):
    response = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={
            "tasks": [
                {"runner_profile": "mock"},
                {"runner_profile": "mock", "strategy": "nonsense"},
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    assert not [key for key in db.docs if key.startswith("tasks/")]


# --------------------------------------------------------------------------
# `integrate` with nothing to integrate is an error, not a downgrade
# --------------------------------------------------------------------------

def test_integrate_on_a_single_task_is_refused(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "integrate", "repository_url": REPO},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert "nothing to integrate" in body["message"]
    # NOT a silent downgrade: no task exists at all.
    assert not [key for key in db.docs if key.startswith("tasks/")]


def test_integrate_in_a_batch_is_refused_because_a_batch_has_no_dependencies(client):
    """A batch is N independent tasks. There is no final step for one to be."""
    response = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={
            "tasks": [
                {"runner_profile": "mock", "strategy": "integrate", "repository_url": REPO},
                {"runner_profile": "mock", "strategy": "integrate", "repository_url": REPO},
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"


def test_integrate_on_a_one_step_workflow_is_refused(client):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "integrate",
            "repository_url": REPO,
            "steps": [{"step_id": "only", "runner_profile": "mock"}],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["steps"] == 1


def test_integrate_with_two_final_steps_is_refused_and_names_them(client):
    """`integrate` promises ONE pull request; two sinks would open two."""
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "integrate",
            "repository_url": REPO,
            "steps": [
                {"step_id": "base", "runner_profile": "mock"},
                {"step_id": "left", "runner_profile": "mock", "depends_on": ["base"]},
                {"step_id": "right", "runner_profile": "mock", "depends_on": ["base"]},
            ],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert sorted(body["detail"]["terminal_steps"]) == ["left", "right"]
    assert "left" in body["message"] and "right" in body["message"]


# --------------------------------------------------------------------------
# What a valid `integrate` workflow stores
# --------------------------------------------------------------------------

def test_integrate_marks_the_sink_and_lists_what_it_must_apply(client, db):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "integrate",
            "carrier": "checkpoints",
            "repository_url": REPO,
            "repository_ref": "main",
            "steps": [
                {"step_id": "api", "runner_profile": "mock"},
                {"step_id": "ui", "runner_profile": "mock"},
                {
                    "step_id": "merge",
                    "runner_profile": "mock",
                    "depends_on": ["api", "ui"],
                },
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dispatch"] == {
        "strategy": "integrate",
        "carrier": "checkpoints",
        "integrator_step_id": "merge",
    }

    steps = _steps_by_id(body["workflow"])
    api = _task_doc(db, steps["api"]["task_id"])
    ui = _task_doc(db, steps["ui"]["task_id"])
    merge = _task_doc(db, steps["merge"]["task_id"])

    assert api["metadata"]["dispatch"]["role"] == "contributor"
    assert ui["metadata"]["dispatch"]["role"] == "contributor"
    assert merge["metadata"]["dispatch"]["role"] == "integrator"
    # Every contributor, and only contributors, in the order their patches are
    # to be applied.
    assert merge["metadata"]["dispatch"]["integrates"] == [api["id"], ui["id"]]
    assert "integrates" not in api["metadata"]["dispatch"]

    # The workflow's repository reached every step, which is the whole reason
    # the field exists: before it, a workflow task could never clone anything.
    for doc in (api, ui, merge):
        assert doc["repository_url"] == REPO
        assert doc["repository_ref"] == "main"
        assert doc["metadata"]["dispatch"]["strategy"] == "integrate"
        assert doc["metadata"]["dispatch"]["carrier"] == "checkpoints"


def test_a_chain_integrates_in_topological_order(client, db):
    """a -> b -> c. `c.depends_on` names only b, so `integrates` must name a too.

    This is the case that makes storing the list worth it: the integrator's own
    `depends_on` holds DIRECT parents only, so a worker reading that field alone
    would never apply `a`'s patch.
    """
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "integrate",
            "repository_url": REPO,
            "steps": [
                {"step_id": "c", "runner_profile": "mock", "depends_on": ["b"]},
                {"step_id": "a", "runner_profile": "mock"},
                {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dispatch"]["integrator_step_id"] == "c"

    steps = _steps_by_id(body["workflow"])
    a = _task_doc(db, steps["a"]["task_id"])
    b = _task_doc(db, steps["b"]["task_id"])
    c = _task_doc(db, steps["c"]["task_id"])
    assert c["metadata"]["dispatch"]["integrates"] == [a["id"], b["id"]]


def test_reading_the_workflow_back_reports_the_dispatch_and_the_integrator(client):
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "integrate",
            "carrier": "branches",
            "repository_url": REPO,
            "steps": [
                {"step_id": "one", "runner_profile": "mock"},
                {"step_id": "two", "runner_profile": "mock", "depends_on": ["one"]},
            ],
        },
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    integrator_task_id = _steps_by_id(created.json()["workflow"])["two"]["task_id"]

    read = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["dispatch"] == {
        "strategy": "integrate",
        "carrier": "branches",
        "integrator_task_id": integrator_task_id,
    }


# --------------------------------------------------------------------------
# A dispatch that has to push needs somewhere to push to
# --------------------------------------------------------------------------

def test_direct_pr_is_accepted_with_a_repository(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "strategy": "direct-pr",
            "repository_url": REPO,
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["dispatch"]["strategy"] == "direct-pr"
    assert task["dispatch"]["role"] is None
    assert _task_doc(db, task["id"])["metadata"]["dispatch"] == {
        "strategy": "direct-pr",
        "carrier": "checkpoints",
    }


def test_direct_pr_without_a_repository_is_refused_rather_than_inert(client):
    """The failure this platform keeps hitting: a feature that is quietly off.

    A `direct-pr` task with no repository would run, succeed, and publish
    nothing, with the caller believing a pull request exists somewhere.
    """
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "direct-pr"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["missing"] == "repository_url"


def test_the_branches_carrier_also_needs_a_repository(client):
    """`carrier: branches` pushes intermediate work; `collect` does not save it."""
    refused = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "strategy": "collect", "carrier": "branches"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["missing"] == "repository_url"

    accepted = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "strategy": "collect",
            "carrier": "branches",
            "repository_url": REPO,
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["task"]["dispatch"]["carrier"] == "branches"


def test_a_workflow_that_has_to_push_needs_a_workflow_repository(client):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "strategy": "integrate",
            "steps": [
                {"step_id": "one", "runner_profile": "mock"},
                {"step_id": "two", "runner_profile": "mock", "depends_on": ["one"]},
            ],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["missing"] == "repository_url"
    assert "workflow" in body["message"]


# --------------------------------------------------------------------------
# `metadata.dispatch` is computed, never accepted
# --------------------------------------------------------------------------

def test_a_caller_cannot_write_the_dispatch_block_directly(client):
    """Otherwise a caller could write a role and an integrates list by hand."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "metadata": {"dispatch": {"strategy": "integrate", "role": "integrator"}},
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["reserved_metadata_keys"] == ["dispatch"]


def test_the_reserved_key_is_refused_on_a_workflow_too(client):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {"dispatch": {"strategy": "collect"}},
            "steps": [{"step_id": "a", "runner_profile": "mock"}],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"


def test_other_caller_metadata_survives_untouched(client, db):
    """The block is added ALONGSIDE the caller's metadata, never instead of it."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "metadata": {"unit": "payments", "origin": "cli"},
        },
    )
    assert response.status_code == 201, response.text
    stored = _task_doc(db, response.json()["task"]["id"])["metadata"]
    assert stored["unit"] == "payments"
    assert stored["origin"] == "cli"
    assert stored["dispatch"]["strategy"] == "collect"


# --------------------------------------------------------------------------
# The pure functions, without HTTP
# --------------------------------------------------------------------------

def test_resolve_returns_the_pair_it_accepted():
    options = resolve_dispatch_options(
        strategy="direct-pr", carrier="branches", scale="task", repository_url=REPO
    )
    assert options == DispatchOptions(strategy="direct-pr", carrier="branches")
    assert options.needs_repository is True


def test_the_default_pair_needs_no_repository():
    options = resolve_dispatch_options(
        strategy=DEFAULT_STRATEGY,
        carrier=DEFAULT_CARRIER,
        scale="task",
        repository_url=None,
    )
    assert options.needs_repository is False
    assert options.to_metadata() == {"strategy": "collect", "carrier": "checkpoints"}


def test_a_role_and_an_integrates_list_only_appear_when_set():
    base = DispatchOptions(strategy="integrate", carrier="checkpoints")
    assert base.to_metadata() == {"strategy": "integrate", "carrier": "checkpoints"}
    integrator = base.with_role("integrator", ["task_a", "task_b"])
    assert integrator.to_metadata() == {
        "strategy": "integrate",
        "carrier": "checkpoints",
        "role": "integrator",
        "integrates": ["task_a", "task_b"],
    }
    assert base.with_role("contributor").to_metadata() == {
        "strategy": "integrate",
        "carrier": "checkpoints",
        "role": "contributor",
    }


def test_resolve_integrator_step_picks_the_only_sink():
    steps = [
        StepSpec(step_id="a", depends_on=()),
        StepSpec(step_id="b", depends_on=("a",)),
        StepSpec(step_id="c", depends_on=("a", "b")),
    ]
    assert resolve_integrator_step(steps) == "c"


def test_resolve_integrator_step_refuses_an_ambiguous_graph():
    steps = [
        StepSpec(step_id="a", depends_on=()),
        StepSpec(step_id="b", depends_on=()),
    ]
    with pytest.raises(ValidationFailed) as exc:
        resolve_integrator_step(steps)
    assert exc.value.code == "invalid_dispatch"
    assert sorted(exc.value.detail["terminal_steps"]) == ["a", "b"]


def test_reject_reserved_metadata_allows_everything_else():
    # `input_from` used to be in this call, pinning it as allowed. It is
    # reserved now, by owner decision on #151; test_input_from_is_reserved.py
    # holds that side.
    reject_reserved_metadata({"unit": "payments", "origin": "cli"})
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({"dispatch": {}})
    assert exc.value.code == "invalid_dispatch"
