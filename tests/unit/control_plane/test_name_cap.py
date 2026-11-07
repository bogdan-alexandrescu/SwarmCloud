"""A name fits one row of the console (owner, 2026-11-07, part of #2688).

Workflow titles of up to 150 characters wrapped onto two or three rows of
the console's lists. Measured at 1440 pixels on fixture data, the Workflows
list's name column holds 34 characters of a name, the Agents list's 71, so a
name holds at most `NAME_MAX_CHARS` (30). swarm-api refuses -- never cuts --
a longer one: a workflow's `metadata.title` and `metadata.unit` (its label)
and a single task's, `name_too_long` through `refusals.refuse`, the field in
the detail. A new refusal ships switched off (`refusals` module docstring):
off, the submission goes through and the log names what it would refuse.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from swarm_api import refusals, wfdescription
from swarm_api.schemas import NAME_MAX_CHARS

from .conftest import auth_header

SWITCH = refusals.env_name("name_too_long")


@pytest.fixture(autouse=True)
def _no_generated_description(monkeypatch):
    monkeypatch.setattr(wfdescription, "after_submission", lambda *a, **k: None)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


def _task(client, **metadata: Any):
    body = {"runner_profile": "mock", "input": {"prompt": "build it"}, "metadata": metadata}
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


def _workflow(client, **metadata: Any):
    spec = {"strategy": "collect", "metadata": metadata,
            "steps": [{"step_id": "build", "runner_profile": "mock", "input": {"prompt": "build it"}}]}
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def test_the_cap_is_the_measured_one_and_its_switch_is_registered():
    assert NAME_MAX_CHARS == 30
    switch = refusals.SWITCHES["name_too_long"]
    assert switch.code == "name_too_long" and switch.default is False


@pytest.mark.parametrize("submit", [_task, _workflow])
@pytest.mark.parametrize("field", ["unit", "title"])
def test_a_name_over_the_cap_is_refused_naming_the_field(client, enforced, submit, field):
    response = submit(client, **{field: "n" * (NAME_MAX_CHARS + 1)})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "name_too_long"
    assert body["detail"]["field"] == f"metadata.{field}"
    assert body["detail"]["max_chars"] == NAME_MAX_CHARS
    assert "metadata.description" in body["message"]


@pytest.mark.parametrize("submit", [_task, _workflow])
def test_a_name_at_the_cap_is_kept_whole(client, enforced, submit):
    name = "n" * NAME_MAX_CHARS
    response = submit(client, unit=name, title=name)
    assert response.status_code == 201, response.text
    if submit is _workflow:
        (step,) = response.json()["workflow"]["steps"]
        task = client.get(f"/v1/tasks/{step['task_id']}", headers=auth_header("alice")).json()["task"]
    else:
        task = response.json()["task"]
    assert task["metadata"]["unit"] == name and task["metadata"]["title"] == name


def test_the_name_is_measured_trimmed(client, enforced):
    assert _task(client, unit=f"  {'n' * NAME_MAX_CHARS}  ").status_code == 201


def test_the_batch_route_refuses_a_long_name(client, enforced):
    body = {"tasks": [{"runner_profile": "mock", "input": {"prompt": "x"},
                       "metadata": {"unit": "n" * (NAME_MAX_CHARS + 1)}}]}
    response = client.post("/v1/tasks/batch", headers=auth_header("alice"), json=body)
    assert response.status_code == 422 and response.json()["code"] == "name_too_long"


def test_switched_off_the_submission_goes_through_and_the_log_says_so(client, caplog, monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    with caplog.at_level(logging.WARNING, logger="swarm_api.refusals"):
        response = _task(client, unit="n" * (NAME_MAX_CHARS + 1))
    assert response.status_code == 201, response.text
    assert "code=name_too_long" in caplog.text
