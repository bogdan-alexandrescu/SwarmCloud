"""A claude-code step can be pointed at a GitHub issue of its repository (#265).

Contract request 28, accepted by the owner on 2026-09-28: `claude-code` and
`codex` declare one runner input, `issue`, a positive integer naming an issue
in the step's own repository. The API takes it through the declaration, like
every declared key, and refuses it on a submission with no repository: the
worker fetches the issue from the task's `repository_url`, so without one the
attempt would be admitted, spend a lease, and fail before its agent started.

`codex` is disabled on this platform, so the submissions here name
`claude-code`; the catalogue test below holds both declarations.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs

from .conftest import auth_header

REPO = "https://github.com/bogdan-alexandrescu/SwarmCloud.git"


def _tasks(db) -> list[str]:
    return [path for path in db.paths("tasks/") if path.count("/") == 1]


def _task(client, input, **extra):
    return client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "claude-code", "input": input, **extra},
    )


def _batch(client, input, **extra):
    return client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={"tasks": [{"runner_profile": "claude-code", "input": input, **extra}]},
    )


def _workflow(client, input, **extra):
    return client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [{"step_id": "fix", "runner_profile": "claude-code", "input": input}],
            **extra,
        },
    )


DOORS = {"POST /v1/tasks": _task, "POST /v1/tasks/batch": _batch, "a workflow step": _workflow}


# -- the declaration ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["claude-code", "codex"])
def test_the_cli_agent_profiles_declare_issue_and_nothing_else(name):
    declared = RUNNER_PROFILES[name].inputs
    assert declared is not None and set(declared) == {"issue"}, declared
    spec = declared["issue"]
    assert spec.kind == "integer"
    assert spec.minimum == 1
    # Printed as the check applies it: `describe()` rounds past six digits.
    assert spec.describe() == f"integer 1..{int(spec.maximum)}"


@pytest.mark.parametrize("value", [0, -1, 1.5, "265", True, None, 10**12])
def test_an_issue_that_is_not_a_positive_integer_is_refused_by_the_catalogue(value):
    with pytest.raises(InputRefused) as caught:
        check_inputs(RUNNER_PROFILES["claude-code"], {"issue": value})
    assert caught.value.key == "issue"


def test_an_integral_float_issue_is_read_as_the_integer():
    assert check_inputs(RUNNER_PROFILES["claude-code"], {"issue": 265.0}) == {"issue": 265}


# -- at the door -------------------------------------------------------------------


@pytest.mark.parametrize("door", DOORS)
def test_an_issue_with_a_repository_is_accepted_and_stored_as_sent(client, db, door):
    response = DOORS[door](client, {"prompt": "fix it", "issue": 265}, repository_url=REPO)

    assert response.status_code == 201, response.text
    tasks = _tasks(db)
    assert len(tasks) == 1, tasks
    stored = db.docs[tasks[0]]
    assert stored["input"] == {"prompt": "fix it", "issue": 265}
    assert stored["repository_url"] == REPO


@pytest.mark.parametrize("door", DOORS)
def test_an_issue_without_a_repository_is_refused_and_creates_nothing(client, db, door):
    response = DOORS[door](client, {"prompt": "fix it", "issue": 265})

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_input", body
    assert body["detail"]["key"] == "issue", body
    assert body["detail"]["runner_profile"] == "claude-code", body
    assert "repository_url" in body["message"], body
    assert not _tasks(db), "a refused submission created a task"


def test_a_refused_workflow_step_is_named(client, db):
    response = _workflow(client, {"prompt": "fix it", "issue": 265})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["step_id"] == "fix"


@pytest.mark.parametrize("door", DOORS)
def test_an_issue_out_of_its_bounds_is_refused_naming_the_bound(client, db, door):
    response = DOORS[door](client, {"prompt": "fix it", "issue": 0}, repository_url=REPO)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["key"] == "issue", body
    assert body["detail"]["expected"] == RUNNER_PROFILES["claude-code"].inputs["issue"].describe()
    assert not _tasks(db)


def test_the_mock_still_refuses_issue(client, db):
    """The declaration is per profile: `issue` means nothing to the mock."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x", "issue": 3},
              "repository_url": REPO},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["key"] == "issue"
    assert not _tasks(db)
