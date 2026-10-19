"""A step may declare `inputs.territory`; a malformed one is a 422 at submit.

Lane TERRITORY-WARN, part of #1468. `territory` is a list of repo-relative
paths and globs naming the only files the step's lane may edit; at publish the
worker warns on a diff that leaves it (tests/unit/worker/test_territory_warn.py).
Every profile takes it, as every profile takes `prompt`: it is the platform's,
no runner reads it, and the frozen catalogue does not declare it. Its shape is
checked here, at every door, so a malformed entry is refused with
`invalid_input` naming the key, before any task exists -- not discovered at
publish, after the lane has spent its compute.
"""

from __future__ import annotations

import pytest

from .conftest import auth_header


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
            "steps": [{"step_id": "lane", "runner_profile": "claude-code", "input": input}],
            **extra,
        },
    )


DOORS = {"POST /v1/tasks": _task, "POST /v1/tasks/batch": _batch, "a workflow step": _workflow}

TERRITORY = ["apps/agent-worker/agent_worker/territory.py", "tests/unit/worker/", "docs/**/*.md"]


@pytest.mark.parametrize("door", DOORS)
def test_a_territory_is_accepted_and_stored_as_sent(client, db, door):
    response = DOORS[door](client, {"prompt": "fix it", "territory": TERRITORY})

    assert response.status_code == 201, response.text
    tasks = _tasks(db)
    assert len(tasks) == 1, tasks
    assert db.docs[tasks[0]]["input"] == {"prompt": "fix it", "territory": TERRITORY}


MALFORMED = {
    "an unclosed class": ["apps/[x.py"],
    "an absolute path": ["/etc/passwd"],
    "a parent segment": ["../other-repo/a.py"],
    "a partial double star": ["apps/**x/a.py"],
    "a brace": ["apps/{a,b}.py"],
    "a backslash": ["apps\\a.py"],
    "a blank entry": [""],
    "not a string": [3],
    "not a list": "apps/",
    "an empty list": [],
}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", MALFORMED)
def test_a_malformed_territory_is_a_422_and_creates_nothing(client, db, door, case):
    response = DOORS[door](client, {"prompt": "fix it", "territory": MALFORMED[case]})

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_input", body
    assert body["detail"]["key"] == "territory", body
    assert not _tasks(db), "a refused submission created a task"


def test_a_malformed_territory_on_a_workflow_names_the_step(client, db):
    response = _workflow(client, {"prompt": "fix it", "territory": ["apps/[x"]})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["step_id"] == "lane"
