"""A workflow step that pushes to an EXISTING swarm branch instead of its own (#263).

A SwarmCloud pull request that goes red in CI gets a fix attempt without an
operator: `.github/workflows/ci-fix.yml` submits a one-step `claude-code`
workflow that names the task whose branch went red in `continues_task`. The
worker then clones that task's branch and pushes the fix onto it, so the fix
lands on the pull request that is red rather than opening a second one.

What this file holds is the API's half, which is where the rules live:

  * A caller names a TASK, never a branch. The branch is derived from the task
    id by the worker, with the same prefix it pushed under -- exactly how the
    integrator's upstream branches are derived -- so nothing a caller sends can
    point a push at an arbitrary ref.
  * Only a task in the caller's own tenant can be continued, and a task in
    another tenant is refused with the SAME words as one that does not exist.
    Anything else would be a cross-tenant push and an enumeration oracle.
  * Only a `direct-pr` task has a branch to continue, only a `direct-pr`
    workflow may continue it, and only with one step: two steps pushing to the
    one branch race each other to a non-fast-forward.
  * A continuation of a continuation continues the ORIGINAL branch. The fix's
    own task id names a branch nothing ever pushed.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from swarm_api.validation import DispatchOptions

from .conftest import auth_header

REPO = "https://github.com/saga-xyz/example"


def _task_doc(db, task_id: str) -> dict:
    return db.docs[f"tasks/{task_id}"]


def _submit_direct_pr_task(client, user: str = "alice", **extra) -> str:
    body = {
        "runner_profile": "mock",
        "strategy": "direct-pr",
        "repository_url": REPO,
        **extra,
    }
    response = client.post("/v1/tasks", headers=auth_header(user), json=body)
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def _fix_workflow(continues: str, **overrides) -> dict:
    body = {
        "steps": [
            {
                "step_id": "ci-fix",
                "runner_profile": "mock",
                "input": {"prompt": "fix the failing unit test"},
            }
        ],
        "strategy": "direct-pr",
        "continues_task": continues,
    }
    body.update(overrides)
    return body


def _step_task(db, response) -> dict:
    workflow = response.json()["workflow"]
    (step,) = workflow["steps"]
    return _task_doc(db, step["task_id"])


# --------------------------------------------------------------------------
# The accepted shape
# --------------------------------------------------------------------------

def test_a_fix_step_records_the_task_whose_branch_it_continues(client, db):
    original = _submit_direct_pr_task(client)

    response = client.post(
        "/v1/workflows", headers=auth_header("alice"), json=_fix_workflow(original)
    )
    assert response.status_code == 201, response.text

    task = _step_task(db, response)
    assert task["metadata"]["dispatch"] == {
        "strategy": "direct-pr",
        "carrier": "checkpoints",
        "continues": original,
    }
    # The repository is the continued task's: the fix has to push where the
    # red pull request lives, and a caller need not (and cannot usefully) say
    # it again.
    assert task["repository_url"] == REPO
    # No ref from the caller. The worker checks out the continued branch
    # itself, from the task id, so the two can never disagree.
    assert task["repository_ref"] is None
    assert response.json()["dispatch"]["continues_task"] == original


def test_the_same_repository_named_again_is_accepted_whatever_its_spelling(client, db):
    """`.git` and a trailing slash name the same repository on every forge."""
    original = _submit_direct_pr_task(client)
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json=_fix_workflow(original, repository_url=REPO + ".git"),
    )
    assert response.status_code == 201, response.text


def test_a_continuation_of_a_continuation_continues_the_original_branch(client, db):
    """The fix's own id names a branch nobody pushed. The root's is the PR's."""
    original = _submit_direct_pr_task(client)
    first = client.post(
        "/v1/workflows", headers=auth_header("alice"), json=_fix_workflow(original)
    )
    assert first.status_code == 201, first.text
    first_fix = first.json()["workflow"]["steps"][0]["task_id"]

    second = client.post(
        "/v1/workflows", headers=auth_header("alice"), json=_fix_workflow(first_fix)
    )
    assert second.status_code == 201, second.text
    assert _step_task(db, second)["metadata"]["dispatch"]["continues"] == original
    assert second.json()["dispatch"]["continues_task"] == original


def test_a_workflow_that_continues_nothing_is_unchanged(client, db):
    """The default carries no `continues` key at all, so no worker reads one."""
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [{"step_id": "a", "runner_profile": "mock"}],
            "strategy": "direct-pr",
            "repository_url": REPO,
        },
    )
    assert response.status_code == 201, response.text
    assert "continues" not in _step_task(db, response)["metadata"]["dispatch"]
    assert "continues_task" not in response.json()["dispatch"]


# --------------------------------------------------------------------------
# Tenant isolation: the property worth most here
# --------------------------------------------------------------------------

def test_another_tenants_task_cannot_be_continued_and_reads_as_missing(client, db):
    """bob is in research, alice in eng. A push to alice's branch with bob's
    token would be a cross-tenant write; the refusal must also not confirm
    that the id exists."""
    alices = _submit_direct_pr_task(client, user="alice")

    theirs = client.post(
        "/v1/workflows", headers=auth_header("bob"), json=_fix_workflow(alices)
    )
    missing = client.post(
        "/v1/workflows",
        headers=auth_header("bob"),
        json=_fix_workflow("task_00000000000000000000"),
    )
    assert theirs.status_code == 422, theirs.text
    assert missing.status_code == 422, missing.text
    assert theirs.json()["code"] == missing.json()["code"] == "invalid_dispatch"
    # Byte-for-byte the same sentence, bar the id the caller sent.
    assert theirs.json()["message"].replace(alices, "X") == missing.json()[
        "message"
    ].replace("task_00000000000000000000", "X")
    # And nothing was created for bob to run.
    assert [
        key for key, doc in db.docs.items()
        if key.startswith("tasks/") and doc.get("tenant_id") != _task_doc(db, alices)["tenant_id"]
    ] == []


def test_continues_task_must_be_a_task_id(client):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json=_fix_workflow("../../refs/heads/main"),
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# The shapes that cannot mean anything
# --------------------------------------------------------------------------

def test_only_direct_pr_may_continue_a_branch(client):
    original = _submit_direct_pr_task(client)
    for strategy in ("collect", "integrate"):
        response = client.post(
            "/v1/workflows",
            headers=auth_header("alice"),
            json=_fix_workflow(original, strategy=strategy),
        )
        assert response.status_code == 422, (strategy, response.text)
        assert response.json()["code"] == "invalid_dispatch"


def test_a_task_that_pushed_no_branch_cannot_be_continued(client):
    """A `collect` task never pushed, so there is no branch to check out."""
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    )
    collected = response.json()["task"]["id"]
    refused = client.post(
        "/v1/workflows", headers=auth_header("alice"), json=_fix_workflow(collected)
    )
    assert refused.status_code == 422, refused.text
    assert "direct-pr" in refused.json()["message"]


def test_one_step_only(client):
    """Two steps on one branch race to a non-fast-forward; one of them loses."""
    original = _submit_direct_pr_task(client)
    body = _fix_workflow(original)
    body["steps"].append({"step_id": "second", "runner_profile": "mock"})
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"


def test_a_ref_beside_continues_task_is_refused(client):
    """The continued branch IS the ref. A second one could only disagree."""
    original = _submit_direct_pr_task(client)
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json=_fix_workflow(original, repository_ref="main"),
    )
    assert response.status_code == 422, response.text


def test_a_different_repository_is_refused(client):
    original = _submit_direct_pr_task(client)
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json=_fix_workflow(original, repository_url="https://github.com/saga-xyz/other"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"


def test_nothing_is_written_when_a_continuation_is_refused(client, db):
    before = {key for key in db.docs if key.startswith(("tasks/", "workflows/"))}
    client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json=_fix_workflow("task_00000000000000000000"),
    )
    after = {key for key in db.docs if key.startswith(("tasks/", "workflows/"))}
    assert after == before


# --------------------------------------------------------------------------
# The block the worker reads
# --------------------------------------------------------------------------

def test_the_role_helpers_keep_the_continuation():
    """`with_role` rebuilds the options; a field it forgot would vanish."""
    options = DispatchOptions(strategy="direct-pr", continues="task_0123456789abcdef0123")
    assert options.to_metadata()["continues"] == "task_0123456789abcdef0123"
    assert options.with_role("contributor").continues == "task_0123456789abcdef0123"
