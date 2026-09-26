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
    """Task documents, not the events and other subcollections under them."""
    return [key for key in db.docs if key.startswith("tasks/") and key.count("/") == 1]


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
#: limit's text and reset time -- and `attempt_count`, which the lifecycle
#: writes from the task document and the mock bounds its park by.
MOCK_WITHHELD = (
    "spend",
    "provider",
    "credential_revoked_times",
    "credential_detail",
    "quota_detail",
    "reset_at",
    "attempt_count",
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
        ("sleep_seconds", -1, "0..3600"),
        ("sleep_seconds", 3600.5, "0..3600"),
        ("cpu_burn_seconds", -0.1, "0..3600"),
        ("cpu_burn_seconds", 3601, "0..3600"),
        ("steps", 0, "1..1000"),
        ("steps", 1001, "1..1000"),
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


# -- a number Firestore cannot store ---------------------------------------------------

#: Python reads a JSON integer of any length, and Firestore stores a signed
#: 64-bit one. Before every declared number had a ceiling, the API accepted
#: `steps: 10**30` and the task write raised on encoding it: a 500 at the
#: store instead of a 422 at the door.
HUGE = 10**30
PAST_INT64 = 2**63
NUMERIC_MOCK_INPUTS = ("steps", "sleep_seconds", "cpu_burn_seconds", "retry_after_seconds", "exit_code")


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("key", NUMERIC_MOCK_INPUTS)
@pytest.mark.parametrize("value", [HUGE, PAST_INT64, -HUGE], ids=["1e30", "2**63", "-1e30"])
def test_a_huge_integer_is_refused_at_the_door_not_at_the_write(client, db, door, key, value):
    body = _refused(DOORS[door](client, "mock", {"prompt": "x", key: value}), key)
    assert body["detail"]["expected"], body
    assert not _tasks(db), "a refused submission created a task"


def test_a_refusal_does_not_echo_a_huge_number_back(client, db):
    """`InputRefused` names the key and the bound, and repeats a value no
    longer than it has to: three hundred digits are not a clue."""
    body = _refused(_task(client, "mock", {"prompt": "x", "steps": 10**300}), "steps")
    assert len(body["message"]) < 300, body["message"]
    assert "1..1000" in body["message"], body["message"]


def test_every_declared_number_has_a_floor_and_a_ceiling_firestore_can_store():
    """THE RULE IS STRUCTURAL, not a habit of whoever writes the next input:
    `RunnerInput` refuses a number declared without both bounds, and an integer
    whose bounds leave the signed 64-bit range. This walks the catalogue so a
    declaration that slipped past it would still be caught here."""
    lowest, highest = -(2**63), 2**63 - 1
    checked = 0
    for name, profile in RUNNER_PROFILES.items():
        for key, spec in (profile.inputs or {}).items():
            if spec.kind not in ("number", "integer"):
                continue
            checked += 1
            assert spec.minimum is not None and spec.maximum is not None, (
                f"{name}.{key} is a {spec.kind} with no "
                f"{'floor' if spec.minimum is None else 'ceiling'}"
            )
            if spec.kind == "integer":
                assert lowest <= spec.minimum and spec.maximum <= highest, f"{name}.{key}"
    assert checked >= len(NUMERIC_MOCK_INPUTS), f"checked {checked} numeric inputs"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "integer", "minimum": 1},
        {"kind": "number", "maximum": 5},
        {"kind": "integer"},
        {"kind": "number"},
        {"kind": "integer", "minimum": 0, "maximum": 2**63},
        {"kind": "integer", "minimum": -(2**63) - 1, "maximum": 0},
    ],
    ids=["no-ceiling", "no-floor", "integer-unbounded", "number-unbounded",
         "ceiling-past-int64", "floor-past-int64"],
)
def test_the_catalogue_refuses_a_number_it_could_not_store(kwargs):
    from swarm_common.profiles import RunnerInput

    with pytest.raises(ValueError):
        RunnerInput(**kwargs)


#: A profile whose inputs are not declared yet has no bound to break, so the
#: store's own limit is the only one: the API refuses what Firestore cannot
#: encode, wherever in the input it sits.
UNSTORABLE_UNDECLARED = {
    "a browser viewport": ("browser", {"url": "https://example.com", "viewport_width": HUGE}),
    "a nested generic value": (
        "generic", {"command": "pytest", "paths": ["tests"], "extra": {"deep": [1, -PAST_INT64 - 1]}},
    ),
}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", UNSTORABLE_UNDECLARED)
def test_a_profile_not_declared_yet_refuses_an_integer_firestore_cannot_store(client, db, door, case):
    name, input = UNSTORABLE_UNDECLARED[case]
    response = DOORS[door](client, name, input)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["path"].startswith("input."), body
    assert "64-bit" in body["message"], body
    assert not _tasks(db), "a refused submission created a task"


def test_metadata_with_an_integer_firestore_cannot_store_is_refused(client, db):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x"}, "metadata": {"run": {"n": HUGE}}},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["path"] == "metadata.run.n", response.text
    assert not _tasks(db)


@pytest.mark.parametrize("value", [2**63 - 1, -(2**63)], ids=["int64-max", "int64-min"])
def test_the_edges_of_the_signed_64_bit_range_are_storable(client, value):
    """The refusal is Firestore's range, not a guess at it: both ends are kept."""
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "browser", "input": {"url": "https://example.com", "n": value}},
    )
    assert response.status_code == 201, response.text


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


# -- one rule, one answer ------------------------------------------------------------

#: Inputs whose answer differs by profile: a key only the mock declares, one
#: only the browser runner reads, one only the generic runner reads, and the
#: key the CLI runners would pass as `--model`.
ONE_ANSWER_INPUTS = {
    "the prompt alone": {"prompt": "x"},
    "a mock knob": {"prompt": "x", "sleep_seconds": 1},
    "a browser url": {"prompt": "x", "url": "https://example.com"},
    "a generic command": {"prompt": "x", "command": "pytest"},
    "a model": {"prompt": "x", "model": "x"},
}


@pytest.mark.parametrize("sent", ONE_ANSWER_INPUTS)
@pytest.mark.parametrize("name", sorted(n for n, p in RUNNER_PROFILES.items() if p.available))
def test_the_api_and_the_shared_rule_give_one_answer_for_every_profile(client, db, name, sent):
    """THE RULE HAS ONE HOME, `swarm_common.profiles.check_inputs`, which the
    API and the bridge both call. The review of #213 found the API deciding the
    not-declared-yet case for itself, ahead of the call: the shared function
    refused every key for `browser` and `generic` while the API accepted every
    key, so a caller that trusted the one rule got the opposite of the API's
    answer. Whatever a profile declares, the two answer alike."""
    from swarm_common.profiles import InputRefused, check_inputs

    input = ONE_ANSWER_INPUTS[sent]
    try:
        check_inputs(RUNNER_PROFILES[name], {k: v for k, v in input.items() if k != "prompt"})
        rule = 201
    except InputRefused:
        rule = 422
    response = _task(client, name, input)
    assert response.status_code == rule, (
        f"the shared rule answers {rule} for {name} with {sent}, the API "
        f"{response.status_code}: {response.text}"
    )
    assert bool(_tasks(db)) == (rule == 201)


def test_the_shared_rule_is_where_not_declared_yet_is_decided():
    """`inputs is None` means NOT DECLARED YET (open with the owner on #218),
    and the input is then bounded by its size alone -- which the caller that
    measures the size decides, not this function. So the shared rule hands it
    back unchecked, and no caller has to catch the None before asking."""
    from swarm_common.profiles import check_inputs

    undeclared = dataclasses.replace(RUNNER_PROFILES["mock"], inputs=None)
    raw = {"url": "https://example.com", "actions": [{"type": "screenshot"}], "command": "pytest"}
    assert check_inputs(undeclared, raw) == raw


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
