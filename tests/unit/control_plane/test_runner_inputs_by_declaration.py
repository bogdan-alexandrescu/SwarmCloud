"""A task's `input` carries what its runner profile declares, from every caller (#142).

WHAT WAS MISSING. The review of PR #201 said it in one line: the API accepted
any input. The plugin's bridge refused a key its profile did not declare, but
the bridge is one client. A script posting to /v1/tasks, the web UI's Submit
form and a workflow step sent whatever they liked, and two of the things they
could send were not data at all:

  * `{"quota_exhausted": true}` to the mock made a task that parked on every
    attempt, and a park does not spend an attempt, so it never ended;
  * `{"model": ...}` to claude-code selects the model the agent runs, which
    tests/unit/mcp/test_model_flag_is_attribution_only.py exists to stop.

THE RULE (contract request 25, accepted by the owner on #142, 2026-09-25).
`RunnerProfile.inputs` in the frozen catalogue declares the keys of `input`
besides `prompt` that a caller may set, each with its kind and bounds. The API
reads that declaration -- it keeps no table of its own -- and refuses anything
else with 422 `invalid_input`, naming the key, and for a declared key the
bound it broke. Nothing is created.

WHAT IS HELD HERE, at each of the three doors a caller has: POST /v1/tasks,
POST /v1/tasks/batch, and a step of POST /v1/workflows.

A PROFILE WHOSE INPUTS ARE NOT DECLARED YET is bounded by size only, as every
profile was before: `browser` cannot start without `url` or `actions`, and
`generic` without `command`, so declaring none for them would refuse every
task they run. Which keys they declare is recorded as an open question in
docs/contract-change-requests.md (25).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header


def _task(client, profile: str, input: Any):
    return client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": profile, "input": input},
    )


def _batch(client, profile: str, input: Any):
    return client.post(
        "/v1/tasks/batch", headers=auth_header("alice"),
        json={"tasks": [{"runner_profile": "mock", "input": {"prompt": "fine"}},
                        {"runner_profile": profile, "input": input}]},
    )


def _step(client, profile: str, input: Any):
    return client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={"steps": [
            {"step_id": "first", "runner_profile": "mock", "input": {"prompt": "fine"}},
            {"step_id": "second", "runner_profile": profile, "input": input,
             "depends_on": ["first"]},
        ]},
    )


DOORS = {"POST /v1/tasks": _task, "POST /v1/tasks/batch": _batch, "a workflow step": _step}


def _raw(client, path: str, body: str):
    """A body sent as TEXT, for the values `json=` would not write the way a
    caller can: `NaN` and `Infinity`, which Python's json.loads -- and so the
    API -- reads as floats."""
    return client.post(
        path,
        headers={**auth_header("alice"), "content-type": "application/json"},
        content=body,
    )


def _tasks(db) -> list[str]:
    return [key for key in db.docs if key.startswith("tasks/")]


def _refused(response, key: str) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_input", body
    assert body["detail"]["key"] == key, body
    assert key in body["message"], body
    return body


# -- an undeclared key -----------------------------------------------------------

#: The keys the owner decided the mock does NOT declare: they write platform
#: records rather than shape a run -- the attempt's spend, whose quota document
#: a park writes, a simulated credential refusal and its text, a simulated rate
#: limit's text and reset time -- and the park count the mock keeps itself.
MOCK_WITHHELD = (
    "spend",
    "provider",
    "credential_revoked_times",
    "credential_detail",
    "quota_detail",
    "reset_at",
    "quota_exhausted_times",
)

#: What the operational scripts sent the mock until this change, which it
#: ignored: every one of them is refused now, and scripts/lib/check-contract-
#: parity.sh holds the scripts to the declaration so a smoke run finds out in
#: CI rather than against a deployment.
IGNORED_BEFORE = ("message", "run_id", "index")


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("key", MOCK_WITHHELD + IGNORED_BEFORE)
def test_the_mock_refuses_a_key_it_does_not_declare_at_every_door(client, db, door, key):
    response = DOORS[door](client, "mock", {"prompt": "x", key: "anything"})
    body = _refused(response, key)
    assert "mock" in body["message"], body
    assert not _tasks(db), "a refused submission created a task"


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("key", ["model", "max_turns", "sleep_seconds", "timeout_seconds"])
def test_claude_code_takes_its_prompt_and_nothing_else(client, db, door, key):
    """`model` is the one that matters: the CLI runners read `input.model` and
    pass it as `--model`. The top-level `model` field is attribution only."""
    response = DOORS[door](client, "claude-code", {"prompt": "x", key: 3})
    _refused(response, key)
    assert not _tasks(db)


def test_a_refused_step_is_named(client, db):
    body = _refused(_step(client, "mock", {"prompt": "x", "spend": {}}), "spend")
    assert body["detail"]["step_id"] == "second", body
    assert "second" in body["message"], body
    assert not _tasks(db), "one refused step must refuse the whole workflow"


# -- a declared key out of its bounds ------------------------------------------------


@pytest.mark.parametrize(
    "key,value,bound",
    [
        ("retry_after_seconds", 0, "1..3600"),
        ("retry_after_seconds", 3601, "1..3600"),
        ("retry_after_seconds", 2.5, "1..3600"),
        ("retry_after_seconds", True, "1..3600"),
        ("retry_after_seconds", "60", "1..3600"),
        ("exit_code", 0, "1..255"),
        ("exit_code", 256, "1..255"),
        ("exit_code", 77, "77"),
        ("exit_code", 78, "78"),
        ("exit_code", 143, "143"),
        ("sleep_seconds", -1, ">= 0"),
        ("steps", 0, ">= 1"),
        ("quota_exhausted", "yes", "boolean"),
        ("fail", 1, "boolean"),
        ("artifact_name", "../escape.txt", "filename"),
    ],
)
def test_a_declared_key_out_of_its_bounds_is_refused_naming_the_bound(client, db, key, value, bound):
    body = _refused(_task(client, "mock", {"prompt": "x", key: value}), key)
    assert bound in body["detail"]["expected"], body
    assert bound in body["message"], body
    assert not _tasks(db)


@pytest.mark.parametrize("door", ["/v1/tasks", "/v1/workflows"])
@pytest.mark.parametrize("key", ["retry_after_seconds", "sleep_seconds", "cpu_burn_seconds"])
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_not_numbers(client, db, door, key, literal):
    """Every comparison with NaN is False, so a bound written `value < minimum`
    passes it; `sleep_seconds: Infinity` is a mock that sleeps until its
    timeout. json.loads accepts both spellings, so the API receives them."""
    one = '{"prompt": "x", "%s": %s}' % (key, literal)
    if door == "/v1/tasks":
        body = '{"runner_profile": "mock", "input": %s}' % one
    else:
        body = '{"steps": [{"step_id": "a", "runner_profile": "mock", "input": %s}]}' % one
    _refused(_raw(client, door, body), key)
    assert not _tasks(db)


# -- what is accepted -----------------------------------------------------------------


EVERY_MOCK_INPUT = {
    "prompt": "all of it",
    "sleep_seconds": 120,
    "cpu_burn_seconds": 0.5,
    "steps": 3,
    "fail": True,
    "fail_message": "on purpose",
    "exit_code": 2,
    "artifact_text": "hello",
    "artifact_name": "out.txt",
    "quota_exhausted": True,
    "retry_after_seconds": 60,
}


@pytest.mark.parametrize("door", DOORS)
def test_the_mock_takes_every_input_it_declares(client, db, door):
    response = DOORS[door](client, "mock", EVERY_MOCK_INPUT)
    assert response.status_code == 201, response.text
    stored = [db.docs[k]["input"] for k in _tasks(db)]
    assert EVERY_MOCK_INPUT in stored, "the input is stored as it was sent"


@pytest.mark.parametrize("name", sorted(n for n, p in RUNNER_PROFILES.items() if p.available))
def test_the_prompt_is_every_available_profiles(client, name):
    response = _task(client, name, {"prompt": "the instructions"})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "name,input",
    [
        ("browser", {"url": "https://example.com", "actions": [{"type": "screenshot", "name": "a.png"}],
                     "extract_text": False, "viewport_width": 800}),
        ("generic", {"command": "pytest", "paths": ["tests"], "working_directory": "repo"}),
    ],
)
def test_a_profile_that_has_not_declared_its_inputs_is_bounded_by_size_only(client, name, input):
    """Undeclared is not "declares none". `inputs is None` in the catalogue
    means nobody has decided yet which keys these runners take, and refusing
    every key would refuse every task they run."""
    assert getattr(RUNNER_PROFILES[name], "inputs", "missing") is None, (
        f"{name} now declares its inputs; move it out of this test"
    )
    response = _task(client, name, input)
    assert response.status_code == 201, response.text


def test_the_api_reads_the_catalogue_and_keeps_no_table_of_its_own(client, monkeypatch):
    """ONE SOURCE. Change the declaration and the API's answer changes with it:
    a key the catalogue starts declaring is accepted, and one it stops
    declaring is refused, with no edit to swarm-api."""
    from swarm_common.profiles import RunnerInput

    redeclared = dataclasses.replace(
        RUNNER_PROFILES["mock"], inputs={"colour": RunnerInput("string", means="a test key")}
    )
    monkeypatch.setitem(RUNNER_PROFILES, "mock", redeclared)

    assert _task(client, "mock", {"prompt": "x", "colour": "red"}).status_code == 201
    _refused(_task(client, "mock", {"prompt": "x", "sleep_seconds": 1}), "sleep_seconds")


def test_the_refusal_says_what_the_profile_does_declare(client):
    body = _refused(_task(client, "mock", {"image": "evil:latest"}), "image")
    declared = body["detail"]["declared"]
    assert "sleep_seconds" in declared and "quota_exhausted" in declared, declared
    json.dumps(body)  # the envelope is plain JSON: no NaN, no object reprs
