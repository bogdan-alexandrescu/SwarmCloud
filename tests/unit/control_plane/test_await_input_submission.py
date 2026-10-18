"""`POST /v1/workflows` accepts an `await-input` step, and refuses everything §4.1 refuses.

docs/design/human-steps.md §4.1 and §6 (lane HW-API-SUBMIT, part of #1436). A
workflow step that waits for a tenant member's answer names the catalogue
profile `await-input` and carries an `await_input` block. swarm-api checks the
block at submission, normalises it, and stores it in `metadata.dispatch`, which
the spec signature covers, so nothing after submission can change what the
person is asked, how long the step waits or what happens at expiry.

The profile SHIPS UNAVAILABLE (contract request 94): every test that needs it
enables it with `monkeypatch`, and the first test pins that, without that, the
workflow is refused as disabled before anything here is read.
"""

from __future__ import annotations

import copy
import json
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
from swarm_api.waker import NullWaker
from swarm_common import specsign
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import PROJECT, api_settings, auth_header
from .spec_signer import LocalSpecSigner

PROFILE = "await-input"
REPO = "https://github.com/saga-xyz/example.git"


@pytest.fixture
def await_enabled(monkeypatch):
    """`await-input`, available for this test only (request D is the owner's)."""
    monkeypatch.setitem(RUNNER_PROFILES, PROFILE, replace(RUNNER_PROFILES[PROFILE], available=True))


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: the `integrate` cases name a GitHub repository."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


def _block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "form": "verdict",
        "artifact": "review.json",
        "max_files": 10,
        "expires_after_seconds": 86_400,
        "on_expire": "default",
        "default": {"verdict": "MERGE", "findings": []},
        "remind_after_seconds": 14_400,
        "remind": ["Owner@saga.xyz"],
    }
    for key, value in overrides.items():
        if value is _DROP:
            block.pop(key, None)
        else:
            block[key] = value
    return block


_DROP = object()


def _review(**step_overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": "review",
        "runner_profile": PROFILE,
        "depends_on": ["draft"],
        "input_from": {"draft": "draft.md"},
        "input": {"prompt": "Read draft.md. Approve it, or list what must change."},
        "await_input": _block(),
    }
    for key, value in step_overrides.items():
        if value is _DROP:
            step.pop(key, None)
        else:
            step[key] = value
    return step


def _spec(review: dict[str, Any] | None = None, **top: Any) -> dict[str, Any]:
    """§4.1's example: draft -> review (await-input) -> revise."""
    spec: dict[str, Any] = {
        "steps": [
            {"step_id": "draft", "runner_profile": "mock",
             "input": {"prompt": "write draft.md"}},
            review if review is not None else _review(),
            {"step_id": "revise", "runner_profile": "mock", "depends_on": ["review", "draft"],
             "input_from": {"review": "review.json"},
             "input": {"prompt": "revise draft.md using review.json"}},
        ],
    }
    spec.update(top)
    return spec


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _docs(db, body) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: db.docs[f"tasks/{s['task_id']}"] for s in body["workflow"]["steps"]}


def _refused(client, db, spec, field: str, *, code: str = "validation_failed") -> dict:
    before = {k for k in db.docs if k.startswith("tasks/")}
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["detail"]["field"] == field, body
    assert {k for k in db.docs if k.startswith("tasks/")} == before, "a refusal created tasks"
    return body


# --------------------------------------------------------------------------
# Dark until request D
# --------------------------------------------------------------------------

def test_the_profile_ships_unavailable_and_a_workflow_naming_it_is_refused(client, db):
    assert not RUNNER_PROFILES[PROFILE].available
    response = _post(client, _spec())
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["disabled"] is True
    assert not [k for k in db.docs if k.startswith("tasks/")]


# --------------------------------------------------------------------------
# What is accepted, and where it is stored
# --------------------------------------------------------------------------

def test_the_block_lands_in_the_signed_dispatch_block_normalised(client, db, await_enabled):
    response = _post(client, _spec())
    assert response.status_code == 201, response.text
    docs = _docs(db, response.json())
    assert docs["review"]["metadata"]["dispatch"]["await_input"] == {
        "form": "verdict",
        "artifact": "review.json",
        "max_files": 10,
        "expires_after_seconds": 86_400,
        "on_expire": "default",
        "default": {"verdict": "MERGE", "findings": []},
        "remind_after_seconds": 14_400,
        "remind": ["owner@saga.xyz"],
    }
    # Only the await step carries it.
    assert "await_input" not in docs["draft"]["metadata"]["dispatch"]
    assert "await_input" not in docs["revise"]["metadata"]["dispatch"]
    # `input_from` is expanded to task ids, as for any step.
    assert docs["review"]["metadata"]["input_from"] == {docs["draft"]["id"]: "draft.md"}


def test_the_defaults_are_filled_in(client, db, await_enabled):
    review = _review(await_input={"form": "text", "expires_after_seconds": 3_600,
                                  "on_expire": "fail"})
    spec = _spec(review)
    spec["steps"][2]["input_from"] = {"review": "input.json"}
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    assert _docs(db, response.json())["review"]["metadata"]["dispatch"]["await_input"] == {
        "form": "text",
        "artifact": "input.json",
        "max_files": 10,
        "expires_after_seconds": 3_600,
        "on_expire": "fail",
        "default": None,
        "remind_after_seconds": None,
        "remind": ["alice@saga.xyz"],
    }


@pytest.mark.parametrize("form, artifact", [("verdict", "verdict.json"),
                                            ("answers", "answers.json"),
                                            ("text", "input.json")])
def test_the_artifact_defaults_by_form(client, db, await_enabled, form, artifact):
    review = _review(await_input={"form": form, "expires_after_seconds": 3_600,
                                  "on_expire": "fail"})
    if form == "answers":
        review["input_from"] = {"draft": "questions.json"}
    response = _post(client, _spec(review))
    assert response.status_code == 201, response.text
    stored = _docs(db, response.json())["review"]["metadata"]["dispatch"]["await_input"]
    assert stored["artifact"] == artifact


def test_a_full_day_is_accepted_and_one_second_more_is_refused(client, db, await_enabled):
    response = _post(client, _spec(_review(await_input=_block(expires_after_seconds=86_400))))
    assert response.status_code == 201, response.text
    _refused(client, db, _spec(_review(await_input=_block(expires_after_seconds=86_401))),
             "await_input.expires_after_seconds")


def test_the_apis_bound_is_the_schedulers_human_park_bound():
    """§4.1: swarm-api cannot import the scheduler at run time, so it restates
    the number; this holds the two equal."""
    from scheduler.loop import HUMAN_PARK_MAX_SECONDS
    from swarm_api.validation import AWAIT_INPUT_MAX_SECONDS

    assert AWAIT_INPUT_MAX_SECONDS == HUMAN_PARK_MAX_SECONDS == 86_400


def test_answers_with_a_staged_questions_json_is_accepted(client, db, await_enabled):
    review = _review(input_from={"draft": "questions.json"},
                     await_input=_block(form="answers", artifact="answers.json",
                                        on_expire="fail", default=_DROP))
    response = _post(client, _spec(review))
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("default", [
    {"answers": [{"question_index": 0, "choice": "Ship it", "text": None}]},
    {"answers": [{"question_index": 1, "choice": None, "text": "later"}]},
])
def test_an_answers_default_of_the_right_shape_is_accepted(client, db, await_enabled, default):
    review = _review(input_from={"draft": "questions.json"},
                     await_input=_block(form="answers", artifact="answers.json",
                                        default=default))
    response = _post(client, _spec(review))
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# The signature covers it
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


def _digest(doc: dict[str, Any]) -> bytes:
    return specsign.spec_digest(specsign.canonical_step_spec(
        doc, task_id=doc["id"], spec_format=doc["spec_format"]))


@pytest.mark.parametrize("key, value", [
    ("expires_after_seconds", 300),
    ("on_expire", "fail"),
    ("default", {"verdict": "NOT_YET", "findings": []}),
    ("max_files", 0),
    ("form", "text"),
])
def test_await_input_changes_the_spec_digest(signed_client, db, signer, await_enabled,
                                             key, value):
    response = _post(signed_client, _spec())
    assert response.status_code == 201, response.text
    doc = _docs(db, response.json())["review"]
    assert signer.verifies(doc, doc["id"])
    tampered = copy.deepcopy(doc)
    tampered["metadata"]["dispatch"]["await_input"][key] = value
    assert _digest(tampered) != _digest(doc)
    assert not signer.verifies(tampered, doc["id"])


# --------------------------------------------------------------------------
# Refusals: the step's own fields (none means anything without a container)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("resource_class", "standard"),
    ("timeout_seconds", 60),
    ("builds_on", "draft"),
    ("when", {"step": "draft", "verdict_in": ["MERGE"]}),
    ("allow_empty_diff", True),
    ("pr_role", "author"),
    ("merges", {"author": "draft"}),
])
def test_a_container_field_on_an_await_step_is_refused_naming_it(
    client, db, await_enabled, field, value
):
    _refused(client, db, _spec(_review(**{field: value})), field)


def test_an_input_key_other_than_prompt_is_refused_naming_it(client, db, await_enabled):
    review = _review(input={"prompt": "review it", "model": "x"})
    response = _post(client, _spec(review))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["key"] == "model"


def test_await_input_on_any_other_profile_is_refused(client, db, await_enabled):
    spec = _spec()
    spec["steps"][0]["await_input"] = _block()
    _refused(client, db, spec, "await_input")


def test_an_await_step_without_await_input_is_refused(client, db, await_enabled):
    _refused(client, db, _spec(_review(await_input=_DROP)), "await_input")


def test_an_await_step_as_a_standalone_task_is_refused(client, db, await_enabled):
    response = client.post("/v1/tasks", headers=auth_header("alice"),
                           json={"runner_profile": PROFILE, "input": {"prompt": "x"}})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == "runner_profile"
    assert not [k for k in db.docs if k.startswith("tasks/")]


# --------------------------------------------------------------------------
# Refusals: inside the block
# --------------------------------------------------------------------------

def test_assignees_is_refused_and_points_at_remind(client, db, await_enabled):
    body = _refused(client, db, _spec(_review(await_input=_block(assignees=["a@saga.xyz"]))),
                    "await_input.assignees")
    assert "remind" in body["message"]


@pytest.mark.parametrize("block, field", [
    (_block(form=_DROP), "await_input.form"),
    (_block(form="essay"), "await_input.form"),
    (_block(artifact="../review.json"), "await_input.artifact"),
    (_block(artifact=".hidden"), "await_input.artifact"),
    (_block(artifact="a" * 129), "await_input.artifact"),
    (_block(max_files=11), "await_input.max_files"),
    (_block(max_files=-1), "await_input.max_files"),
    (_block(max_files=True), "await_input.max_files"),
    (_block(max_files=2.5), "await_input.max_files"),
    (_block(expires_after_seconds=_DROP), "await_input.expires_after_seconds"),
    (_block(expires_after_seconds=299, remind_after_seconds=None),
     "await_input.expires_after_seconds"),
    (_block(expires_after_seconds=86_401), "await_input.expires_after_seconds"),
    (_block(expires_after_seconds="86400"), "await_input.expires_after_seconds"),
    (_block(on_expire=_DROP), "await_input.on_expire"),
    (_block(on_expire="retry"), "await_input.on_expire"),
    (_block(default=_DROP), "await_input.default"),
    (_block(on_expire="fail"), "await_input.default"),
    (_block(default={"verdict": "MAYBE", "findings": []}), "await_input.default"),
    (_block(default={"verdict": "MERGE", "findings": "none"}), "await_input.default"),
    (_block(default={"verdict": "MERGE", "findings": [], "files": ["a.png"]}),
     "await_input.default"),
    (_block(default={"text": "fine"}), "await_input.default"),
    (_block(default={"verdict": "MERGE", "findings": ["x" * 1_001]}), "await_input.default"),
    (_block(default={"verdict": "MERGE", "findings": ["x"] * 51}), "await_input.default"),
    (_block(default={"verdict": "MERGE", "findings": ["x" * 900] * 20}), "await_input.default"),
    (_block(remind_after_seconds=3_599), "await_input.remind_after_seconds"),
    (_block(expires_after_seconds=7_200, remind_after_seconds=7_201),
     "await_input.remind_after_seconds"),
    (_block(remind=["someone@elsewhere.example"]), "await_input.remind"),
    (_block(remind=["not-an-address"]), "await_input.remind"),
    (_block(remind=[f"p{i}@saga.xyz" for i in range(11)]), "await_input.remind"),
    (_block(remind="owner@saga.xyz"), "await_input.remind"),
    (_block(colour="red"), "await_input.colour"),
])
def test_a_bad_block_is_refused_naming_its_field(client, db, await_enabled, block, field):
    _refused(client, db, _spec(_review(await_input=block)), field)


def test_a_block_that_is_not_an_object_is_refused(client, db, await_enabled):
    _refused(client, db, _spec(_review(await_input=["verdict"])), "await_input")


def test_a_text_default_over_16_kib_is_refused(client, db, await_enabled):
    block = _block(form="text", artifact="input.json", default={"text": "x" * (16 * 1024)})
    spec = _spec(_review(await_input=block))
    spec["steps"][2]["input_from"] = {"review": "input.json"}
    assert len(json.dumps(block["default"])) > 16 * 1024
    _refused(client, db, spec, "await_input.default")


@pytest.mark.parametrize("input_from", [
    {"draft": "draft.md"},
    {},
])
def test_answers_without_a_questions_json_source_is_refused(
    client, db, await_enabled, input_from
):
    review = _review(input_from=input_from,
                     await_input=_block(form="answers", artifact="answers.json",
                                        on_expire="fail", default=_DROP))
    if not input_from:
        review.pop("input_from")
    _refused(client, db, _spec(review), "await_input.form")


@pytest.mark.parametrize("default", [
    {"answers": [{"question_index": -1, "choice": "a", "text": None}]},
    {"answers": [{"question_index": 0, "choice": None, "text": None}]},
    {"answers": [{"question_index": 0, "choice": "a", "text": "x" * 2_001}]},
    {"answers": [{"question_index": 0, "choice": "a", "comment": "x"}]},
    {"answers": "yes"},
])
def test_an_answers_default_of_the_wrong_shape_is_refused(client, db, await_enabled, default):
    review = _review(input_from={"draft": "questions.json"},
                     await_input=_block(form="answers", artifact="answers.json",
                                        default=default))
    _refused(client, db, _spec(review), "await_input.default")


# --------------------------------------------------------------------------
# `metadata.await_input` is the platform's
# --------------------------------------------------------------------------

def _assert_reserved(response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch", body
    assert "await_input" in body["detail"]["reserved_metadata_keys"], body


def test_a_callers_metadata_await_input_is_refused_on_a_task(client, db):
    response = client.post("/v1/tasks", headers=auth_header("alice"), json={
        "runner_profile": "mock", "input": {"prompt": "x"},
        "metadata": {"await_input": {"answer": {"answered_by": "mallory@saga.xyz"}}},
    })
    _assert_reserved(response)
    assert not [k for k in db.docs if k.startswith("tasks/")]


def test_a_callers_metadata_await_input_is_refused_on_a_workflow(client, db, await_enabled):
    _assert_reserved(_post(client, _spec(metadata={"await_input": {}})))
    spec = _spec()
    spec["steps"][1]["metadata"] = {"await_input": {"expires_at": "2099-01-01T00:00:00Z"}}
    _assert_reserved(_post(client, spec))
    assert not [k for k in db.docs if k.startswith("tasks/")]


# --------------------------------------------------------------------------
# `integrate`: an await step pushes no branch
# --------------------------------------------------------------------------

def test_an_await_step_is_never_in_integrates(client, db, await_enabled):
    response = _post(client, _spec(strategy="integrate", repository_url=REPO,
                                   repository_ref="main"))
    assert response.status_code == 201, response.text
    docs = _docs(db, response.json())
    revise = docs["revise"]["metadata"]["dispatch"]
    assert revise["role"] == "integrator"
    assert revise["integrates"] == [docs["draft"]["id"]]
    assert docs["review"]["id"] not in revise["integrates"]


def test_an_await_step_cannot_be_the_integrator(client, db, await_enabled):
    spec = _spec(strategy="integrate", repository_url=REPO, repository_ref="main")
    spec["steps"] = spec["steps"][:2]
    _refused(client, db, spec, "runner_profile")
