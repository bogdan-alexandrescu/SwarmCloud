"""A workflow step may name its own `repository_ref` (#2895).

THE GAP. A workflow named one ref for every step, so a step that had to read
a pull request's branch -- a review of work already pushed, beside steps on
`main` -- could not be expressed in one workflow. A step now carries its own
`repository_ref`, stored on its task's existing (signed) `Task.repository_ref`;
its dispatch block says `step_ref: true` so the worker clones that ref as the
step's own. The other steps keep the workflow's ref and exactly the dispatch
block they stored before.

The rules, each checked here: an abbreviated sha is refused as at the
workflow level (`check_repository_ref`); a step ref is refused beside
`builds_on`, beside `pr_role`, under a workflow `continues_task`, `merge_pr`
or `publish_to_pr`, and under `integrate` on a step the integrator merges;
the forge is asked once per distinct step ref, before any task exists.

Offline: the real routes over FakeFirestore; the forge is the fake of
test_repository_ref_missing_branch.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import auth_header
from .test_repository_ref_missing_branch import FULL, FakeForge, FakeTokens, _client

REPO = "https://github.com/example-org/example.git"
BRANCH = "swarm/task_" + "0123456789abcdef0123"
#: `refusals.env_name("repository_ref_not_found")`.
SWITCH = "REFUSAL_REPOSITORY_REF_NOT_FOUND"


def _spec(step_ref: str | None = None, *, on: str = "b", **workflow: Any) -> dict[str, Any]:
    steps = [
        {"step_id": "a", "runner_profile": "mock"},
        {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
        {"step_id": "c", "runner_profile": "mock", "depends_on": ["b"]},
    ]
    if step_ref is not None:
        next(s for s in steps if s["step_id"] == on)["repository_ref"] = step_ref
    return {"repository_url": REPO, "repository_ref": "main", "steps": steps, **workflow}


def _post(client, spec: dict[str, Any]):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _stored(db) -> list[dict[str, Any]]:
    return [d for k, d in db.docs.items() if k.startswith("tasks/") and k.count("/") == 1]


def _by_step(db) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["workflow_step"]: d for d in _stored(db)}


def _nothing_written(db) -> bool:
    return not any(k.startswith("tasks/") for k in db.docs)


def _refused(response, db, *words: str) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["detail"]["field"] == "steps[1].repository_ref"
    assert body["detail"]["step_id"] == "b"
    for word in words:
        assert word in body["message"], body["message"]
    assert _nothing_written(db), "nothing may be written on a refusal"
    return body


@pytest.fixture
def make_client(db, tokens, group_map, objects):
    def _make(*, forge=None):
        return _client(db, tokens, group_map, objects,
                       forge_tokens=FakeTokens(), forge=forge)
    return _make


def test_only_the_step_with_a_ref_carries_it(client, db):
    response = _post(client, _spec(BRANCH))

    assert response.status_code == 201, response.text
    tasks = _by_step(db)
    assert tasks["b"]["repository_ref"] == BRANCH
    assert tasks["a"]["repository_ref"] == "main"
    assert tasks["c"]["repository_ref"] == "main"
    assert tasks["b"]["metadata"]["dispatch"]["step_ref"] is True
    assert "step_ref" not in tasks["a"]["metadata"]["dispatch"]
    assert "step_ref" not in tasks["c"]["metadata"]["dispatch"]
    # The read-back serves it on that step only.
    served = {
        s: client.get(f"/v1/tasks/{t['id']}", headers=auth_header("alice")).json()["task"]["dispatch"]
        for s, t in tasks.items()
    }
    assert served["b"]["step_ref"] is True
    assert "step_ref" not in served["a"] and "step_ref" not in served["c"]


def test_a_step_without_a_ref_stores_exactly_the_block_it_did_before(client, db):
    assert _post(client, _spec()).status_code == 201
    before = {s: t["metadata"]["dispatch"] for s, t in _by_step(db).items()}
    assert all("step_ref" not in block for block in before.values())
    for key in [k for k in db.docs if k.startswith("tasks/")]:
        del db.docs[key]

    assert _post(client, _spec(BRANCH)).status_code == 201
    after = {s: t["metadata"]["dispatch"] for s, t in _by_step(db).items()}

    assert after["a"] == before["a"]
    assert after["c"] == before["c"]
    assert after["b"] == {**before["b"], "step_ref": True}


def test_a_step_ref_equal_to_the_workflows_is_still_the_steps_own(client, db):
    assert _post(client, _spec("main")).status_code == 201

    tasks = _by_step(db)
    assert tasks["b"]["repository_ref"] == "main"
    assert tasks["b"]["metadata"]["dispatch"]["step_ref"] is True


def test_a_step_ref_with_no_workflow_ref_is_the_steps_alone(client, db):
    spec = _spec(BRANCH)
    del spec["repository_ref"]

    assert _post(client, spec).status_code == 201, "a step ref needs no workflow ref"
    tasks = _by_step(db)
    assert tasks["b"]["repository_ref"] == BRANCH
    assert tasks["a"]["repository_ref"] is None


@pytest.mark.parametrize("ref", [FULL, "release/2026-10", "v1.2.3", "feature-b7bda42e"])
def test_a_full_sha_or_a_branch_or_tag_is_accepted(client, db, ref):
    response = _post(client, _spec(ref))

    assert response.status_code == 201, response.text
    assert _by_step(db)["b"]["repository_ref"] == ref


@pytest.mark.parametrize("ref", ["b7bda42e", "b7bda42", FULL[:39], FULL[:12]])
def test_an_abbreviated_sha_is_refused_as_at_the_workflow_level(client, db, ref):
    step = _post(client, _spec(ref))
    workflow = _post(client, {**_spec(), "repository_ref": ref})

    body = _refused(step, db, ref, "40", "branch", "tag")
    assert body["detail"]["repository_ref"] == ref
    level = workflow.json()
    assert workflow.status_code == 422
    assert level["code"] == body["code"]
    # The same sentence, said of the step.
    assert body["message"].endswith(level["message"])


def test_a_step_ref_beside_builds_on_is_refused(client, db):
    spec = _spec(BRANCH)
    spec["steps"][1]["builds_on"] = "a"

    _refused(_post(client, spec), db, "builds_on")


def test_a_step_ref_beside_a_pr_role_is_refused(client, db):
    spec = _spec(BRANCH, strategy="single-pr")
    for step, role in zip(spec["steps"], ("author", "reader", "amender")):
        step["pr_role"] = role

    _refused(_post(client, spec), db, "pr_role")


@pytest.mark.parametrize("field,value", [
    ("continues_task", "task_" + "77d63ca81cbb423ba839"),
    ("merge_pr", {"number": 7, "head_sha": FULL}),
    ("publish_to_pr", 7),
])
def test_a_step_ref_under_a_ref_the_workflow_supplies_is_refused(client, db, field, value):
    spec = _spec(BRANCH, strategy="direct-pr", **{field: value})
    del spec["repository_ref"]

    _refused(_post(client, spec), db, field)


def _integrate(step_ref_on: str) -> dict[str, Any]:
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "repository_ref": "main",
        "steps": [
            {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "one half"}},
            {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "other half"}},
            {"step_id": "join", "runner_profile": "mock", "depends_on": ["a", "b"],
             "input": {"prompt": "integrate"}},
        ],
    }
    next(s for s in spec["steps"] if s["step_id"] == step_ref_on)["repository_ref"] = BRANCH
    return spec


def test_a_step_ref_on_a_step_the_integrator_merges_is_refused(client, db):
    _refused(_post(client, _integrate("b")), db, "integrate")


def test_a_step_ref_on_the_integrator_is_refused(client, db):
    response = _post(client, _integrate("join"))

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["step_id"] == "join"
    assert "integrate" in body["message"]
    assert _nothing_written(db)


def test_a_workflow_without_a_repository_refuses_a_step_ref(client, db):
    spec = _spec(BRANCH)
    del spec["repository_url"]
    del spec["repository_ref"]

    _refused(_post(client, spec), db, "repository_url")


def test_the_forge_is_asked_once_per_distinct_step_ref(make_client, db, monkeypatch):
    monkeypatch.setenv(SWITCH, "on")
    forge = FakeForge(branches={"main", BRANCH})
    client = make_client(forge=forge)
    spec = _spec(BRANCH)
    spec["steps"][2]["repository_ref"] = BRANCH
    spec["steps"].append({"step_id": "d", "runner_profile": "mock",
                          "depends_on": ["c"], "repository_ref": "main"})

    response = _post(client, spec)

    assert response.status_code == 201, response.text
    # The workflow's ref once; the step ref once, for two steps; a step ref
    # equal to the workflow's is not asked again.
    assert sorted(forge.calls) == sorted([
        ("branch", "example-org/example", "main"),
        ("branch", "example-org/example", BRANCH),
    ])
    tasks = _by_step(db)
    assert [tasks[s]["repository_ref"] for s in "abcd"] == ["main", BRANCH, BRANCH, "main"]


def test_a_missing_step_branch_is_refused_before_any_task(make_client, db, monkeypatch):
    monkeypatch.setenv(SWITCH, "on")
    forge = FakeForge(branches={"main"})
    client = make_client(forge=forge)

    response = _post(client, _spec(BRANCH))

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "repository_ref_not_found"
    assert body["detail"]["repository_ref"] == BRANCH
    assert ("branch", "example-org/example", BRANCH) in forge.calls
    assert _nothing_written(db)


def test_a_refused_step_ref_costs_no_forge_call(make_client, db, monkeypatch):
    monkeypatch.setenv(SWITCH, "on")
    forge = FakeForge(branches={"main", BRANCH})
    client = make_client(forge=forge)
    spec = _spec(BRANCH)
    spec["steps"][1]["builds_on"] = "a"

    _refused(_post(client, spec), db, "builds_on")
    assert ("branch", "example-org/example", BRANCH) not in forge.calls


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
