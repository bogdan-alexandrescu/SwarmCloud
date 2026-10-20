"""A person answers an await-input step (docs/design/human-steps.md §4.2, §4.6; lane HW-API-ANSWER).

Who may answer (any member of the step's tenant, asked freshly; never a
service account), the step's state (409 `not_open`, `already_answered`, the
replay that is the original 200), the form's redaction, the signature, the one
winner among an answer, the expiry and a cancel, and the expiry sweep's two
endings. The files and the size gate are test_await_input_files.py.

The workflow is submitted through `POST /v1/workflows` with the profile made
available for the test (request D is the owner's), so the step under test is
exactly what submission stores and signs. The scheduler's park is applied with
the scheduler's own window (`scheduler.awaitinput.wait_window`).

Offline: an in-memory Firestore, an in-memory bucket, a local P-256 signer, a
fake Cloud Identity session. Every fake token is built at run time.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_worker.inputs import artifact_key
from scheduler.awaitinput import wait_window
from scheduler.loop import HUMAN_PARK_MAX_SECONDS
from swarm_api import awaitinput
from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import CloudIdentityGroups, StaticGroups
from swarm_api.inputwriter import InMemoryInputWriter
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import ENG_GROUP, NoForgeTokens, api_settings, auth_header, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner

PROFILE = "await-input"
SIGNER = LocalSpecSigner()
#: A service account admitted by ALLOWED_USERS and confirmed in eng: an
#: ordinary member to every route but the answer.
BOT = "swarm-bot@saga-agents-staging.iam.gserviceaccount.com"
TICK = "swarm-schedule-tick@saga-agents-staging.iam.gserviceaccount.com"


def fake_token() -> str:
    """A GitHub-shaped token, built at run time: never a literal in this file."""
    return "gh" + "p_" + secrets.token_hex(20)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def await_enabled(monkeypatch):
    """`await-input`, available for this test only."""
    monkeypatch.setitem(RUNNER_PROFILES, PROFILE, replace(RUNNER_PROFILES[PROFILE], available=True))


@pytest.fixture
def await_dark(monkeypatch):
    """`await-input` unavailable, as it shipped before request D (entry 105):
    the dark path stays tested now that the catalogue makes it available."""
    monkeypatch.setitem(RUNNER_PROFILES, PROFILE, replace(
        RUNNER_PROFILES[PROFILE], available=False,
        disabled_reason="await-input is switched off for this test"))


@pytest.fixture
def group_map(group_map):
    return {**group_map, BOT: (ENG_GROUP,)}


@pytest.fixture
def tokens(tokens):
    return {
        **tokens,
        "token-tick": {"email": TICK, "email_verified": True, "sub": "sub-tick"},
    }


def make_context(db, tokens, objects, groups, **settings: Any):
    base = dict(
        spec_signing_key_version=KEY_VERSION,
        spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
        allowed_users=(BOT,),
        schedule_tick_users=(TICK,),
    )
    base.update(settings)
    return build_context(
        settings=api_settings(**base),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
        signer=SIGNER,
    )


@pytest.fixture
def api_context(db, tokens, group_map, objects, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, objects, StaticGroups(group_map))


@pytest.fixture
def writer(objects) -> InMemoryInputWriter:
    return InMemoryInputWriter(objects)


@pytest.fixture
def client(api_context, writer) -> TestClient:
    return TestClient(create_app(api_context, input_writer=writer), raise_server_exceptions=False)


# --------------------------------------------------------------------------
# a workflow: draft -> review (await-input) -> revise
# --------------------------------------------------------------------------

def block(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "form": "verdict",
        "artifact": "review.json",
        "max_files": 10,
        "expires_after_seconds": 3_600,
        "on_expire": "fail",
    }
    value.update(overrides)
    return value


def spec(await_block: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "steps": [
            {"step_id": "draft", "runner_profile": "mock", "input": {"prompt": "write draft.md"}},
            {"step_id": "review", "runner_profile": PROFILE, "depends_on": ["draft"],
             "input_from": {"draft": "draft.md"},
             "input": {"prompt": "Read draft.md. Approve it, or list what must change."},
             "await_input": await_block or block()},
            {"step_id": "revise", "runner_profile": "mock", "depends_on": ["review", "draft"],
             "input_from": {"review": (await_block or block())["artifact"]},
             "input": {"prompt": "revise draft.md"}},
        ],
    }


def launch(client, db, await_block: dict[str, Any] | None = None, *, user: str = "alice",
           headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.post("/v1/workflows", headers={**auth_header(user), **(headers or {})},
                           json=spec(await_block))
    assert response.status_code == 201, response.text
    body = response.json()
    steps = {s["step_id"]: s["task_id"] for s in body["workflow"]["steps"]}
    return {"workflow_id": body["workflow"]["workflow_id"], **steps}


def park(db, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The scheduler's park: READY -> PARKED(AWAITING_INPUT) with its window."""
    doc = db.docs[f"tasks/{task_id}"]
    moment = now or datetime.now(timezone.utc)
    metadata = dict(doc.get("metadata") or {})
    metadata["await_input"] = wait_window(metadata, moment, max_seconds=HUMAN_PARK_MAX_SECONDS)
    doc.update(state="PARKED", park_reason="AWAITING_INPUT", next_eligible_at=moment,
               metadata=metadata, updated_at=moment)
    return doc


def past_due(db, task_id: str) -> None:
    db.docs[f"tasks/{task_id}"]["metadata"]["await_input"]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )


def url(ids: dict[str, Any], step: str = "review") -> str:
    return f"/v1/workflows/{ids['workflow_id']}/steps/{step}/input"


def verdict(answer_id: str = "r1-alice", value: str = "NOT_YET", findings=None) -> dict[str, Any]:
    return {"answer_id": answer_id,
            "payload": {"verdict": value, "findings": findings or ["Section 3 omits the cost."]}}


def snapshot(db, objects, task_id: str) -> tuple[Any, ...]:
    """Everything an answer could have written: the task, its events, the bucket."""
    events = sorted(k for k in db.docs if k.startswith(f"tasks/{task_id}/events/"))
    return json.dumps(db.docs[f"tasks/{task_id}"], default=str, sort_keys=True), events, \
        sorted(objects.objects)


def terminal_events(db, task_id: str) -> list[str]:
    return sorted(
        d["type"] for k, d in db.docs.items()
        if k.startswith(f"tasks/{task_id}/events/")
        and d["type"] in ("succeeded", "dead_lettered", "cancelled")
    )


@pytest.fixture
def waiting(client, db, await_enabled) -> dict[str, Any]:
    ids = launch(client, db)
    park(db, ids["review"])
    return ids


# --------------------------------------------------------------------------
# the answer, end to end
# --------------------------------------------------------------------------

def test_a_member_answers_and_the_step_succeeds_with_the_form_as_its_artifact(
    client, db, objects, waiting
):
    response = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "SUCCEEDED" and body["replayed"] is False
    assert body["answered_by"] == "alice@saga.xyz" and body["via"] == "console"
    doc = db.docs[f"tasks/{waiting['review']}"]
    assert doc["state"] == "SUCCEEDED" and doc["park_reason"] is None
    summary = doc["result_summary"]
    assert summary["published_by"] == "control_plane"
    assert summary["input"]["source"] == "person"
    assert summary["input"]["answered_by"] == "alice@saga.xyz"
    entry = summary["artifacts"][0]
    assert entry["name"] == "review.json"
    stored = json.loads(objects.objects[artifact_key(
        entry["uri"], tenant_id="eng", upstream_task_id=waiting["review"], filename="review.json")])
    assert stored == {"verdict": "NOT_YET", "findings": ["Section 3 omits the cost."]}
    record = doc["metadata"]["await_input"]["answer"]
    assert record["answer_id"] == "r1-alice" and record["sha256"] == entry["sha256"]
    assert terminal_events(db, waiting["review"]) == ["succeeded"]
    # The window the scheduler wrote is kept beside the record.
    assert "opened_at" in doc["metadata"]["await_input"]


def test_the_answer_rings_task_finished_for_the_step(client, db, api_context, waiting):
    client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert ("task_finished", {"task_id": waiting["review"], "tenant_id": "eng",
                              "state": "SUCCEEDED"}) in api_context.waker.calls


# --------------------------------------------------------------------------
# who may answer (§4.2)
# --------------------------------------------------------------------------

def test_a_service_account_caller_is_403_and_nothing_is_written(client, db, objects, waiting):
    # The bot is an ordinary member everywhere else: it reads the workflow.
    assert client.get(f"/v1/workflows/{waiting['workflow_id']}",
                      headers=auth_header("swarm-bot")).status_code == 200
    before = snapshot(db, objects, waiting["review"])
    response = client.post(url(waiting), headers=auth_header("swarm-bot"), json=verdict())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "person_required"
    assert snapshot(db, objects, waiting["review"]) == before


def test_the_schedule_tick_is_not_a_person_either(api_context):
    tick = AuthContext(
        principal=Principal(email=TICK, subject="s", domain="x", groups=()),
        tenant_id="eng", is_admin=False, is_schedule_tick=True,
    )
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(api_context, tick, "eng")
    assert refused.value.detail["reason"] == "person_required"


def test_another_tenants_step_is_404_and_nothing_is_written(client, db, objects, waiting):
    before = snapshot(db, objects, waiting["review"])
    # bob is in research: the workflow is eng's.
    response = client.post(url(waiting), headers=auth_header("bob"), json=verdict())
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    assert client.get(url(waiting), headers=auth_header("bob")).status_code == 404
    # The same answer as a step that does not exist.
    missing = client.post(f"/v1/workflows/{waiting['workflow_id']}/steps/nope/input",
                          headers=auth_header("alice"), json=verdict())
    assert missing.status_code == 404
    assert response.json()["message"].startswith(f"workflow {waiting['workflow_id']!r} has no step")
    assert snapshot(db, objects, waiting["review"]) == before


def test_a_step_that_is_not_an_await_step_is_404(client, db, waiting):
    response = client.post(url(waiting, "draft"), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 404


class FakeDirectory:
    """Cloud Identity over a dict: `groups:lookup` and `checkTransitiveMembership`."""

    def __init__(self, members: dict[str, set[str]]) -> None:
        self.members = members
        self.failing: set[str] = set()
        self.checks: list[tuple[str, str]] = []

    def get(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: float):
        assert headers["x-goog-user-project"]
        if url.endswith("groups:lookup"):
            return _Response(200, {"name": f"groups/{params['groupKey.id']}"})
        group = url.split("/groups/", 1)[1].split("/memberships", 1)[0]
        email = params["query"].split("'")[1]
        self.checks.append((group, email))
        if group in self.failing:
            return _Response(503, {})
        return _Response(200, {"hasMembership": email in self.members.get(group, set())})


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def directory() -> FakeDirectory:
    return FakeDirectory({ENG_GROUP: {"alice@saga.xyz"}, "research@saga.xyz": {"bob@saga.xyz"}})


@pytest.fixture
def live_client(db, tokens, objects, writer, directory, await_enabled, monkeypatch):
    """Membership from a directory behind the real resolver and its real cache."""
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    groups = CloudIdentityGroups("saga-agents-staging", session=directory, ttl_seconds=3_600)
    context = make_context(db, tokens, objects, groups)
    return TestClient(create_app(context, input_writer=writer), raise_server_exceptions=False)


def test_a_member_removed_after_the_cache_filled_is_403(live_client, db, objects, directory):
    ids = launch(live_client, db)
    park(db, ids["review"])
    # The cache holds "alice is in eng": the next requests pass on it.
    assert live_client.get(url(ids), headers=auth_header("alice")).json()["may_answer"] is True
    directory.members[ENG_GROUP].discard("alice@saga.xyz")
    checks_before = len(directory.checks)
    # Control: the ordinary request path still answers from the cache.
    assert live_client.get(url(ids), headers=auth_header("alice")).status_code == 200
    assert len(directory.checks) == checks_before
    before = snapshot(db, objects, ids["review"])
    response = live_client.post(url(ids), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "not_a_member"
    # The decision went to the directory, past the cache.
    assert (ENG_GROUP, "alice@saga.xyz") in directory.checks[checks_before:]
    assert snapshot(db, objects, ids["review"]) == before


def test_a_member_answers_through_the_fresh_check(live_client, db, directory):
    ids = launch(live_client, db)
    park(db, ids["review"])
    response = live_client.post(url(ids), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 200, response.text


def test_a_lookup_failure_is_503_and_nothing_is_written(live_client, db, objects, directory):
    ids = launch(live_client, db)
    park(db, ids["review"])
    assert live_client.get(url(ids), headers=auth_header("alice")).status_code == 200
    directory.failing.add(ENG_GROUP)
    before = snapshot(db, objects, ids["review"])
    response = live_client.post(url(ids), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "membership_unavailable"
    assert snapshot(db, objects, ids["review"]) == before


def _personal_tenant(db) -> Tenant:
    tenant = Tenant(
        tenant_id="u-carol", kind="user", principal="carol@saga.xyz",
        created_at=datetime.now(timezone.utc), display_name="carol", max_active=4,
        capacity_units=8, enabled=True, credentials=[],
        service_account="swarm-agent-worker-u-carol@x.iam.gserviceaccount.com",
        gcs_prefix="gs://b/tenants/u-carol", namespace="swarm-tenant-u-carol",
    )
    from dataclasses import asdict

    db.docs["tenants/u-carol"] = asdict(tenant)
    return tenant


def _person(email: str, tenant_id: str) -> AuthContext:
    return AuthContext(principal=Principal(email=email, subject="s", domain="saga.xyz", groups=()),
                       tenant_id=tenant_id, is_admin=False)


def test_a_personal_tenants_step_refuses_anyone_but_its_owner(client, db, api_context):
    _personal_tenant(db)
    awaitinput.member_refusal(api_context, _person("carol@saga.xyz", "u-carol"), "u-carol")
    for other in ("alice@saga.xyz", "root@saga.xyz"):   # root is an admin: no bypass
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(api_context, _person(other, "u-carol"), "u-carol")
        assert refused.value.detail["reason"] == "not_a_member"
    # Through the route, another person cannot even select carol's tenant.
    response = client.post("/v1/workflows/wf_x/steps/review/input",
                           headers={**auth_header("bob"), "X-Swarm-Tenant": "u-carol"},
                           json=verdict())
    assert response.status_code == 403


def test_an_admin_answers_only_as_a_member(client, db, waiting):
    # root is an admin AND in eng: it answers, as a member.
    response = client.post(url(waiting), headers=auth_header("root"), json=verdict())
    assert response.status_code == 200, response.text
    assert response.json()["answered_by"] == "root@saga.xyz"


# --------------------------------------------------------------------------
# the step's state (§4.6 step 4)
# --------------------------------------------------------------------------

def test_an_answer_before_the_park_is_409_not_open(client, db, objects, await_enabled):
    ids = launch(client, db)
    assert db.docs[f"tasks/{ids['review']}"]["state"] != "PARKED" or \
        db.docs[f"tasks/{ids['review']}"]["park_reason"] != "AWAITING_INPUT"
    before = snapshot(db, objects, ids["review"])
    response = client.post(url(ids), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "not_open"
    assert client.get(url(ids), headers=auth_header("alice")).json()["status"] == "not_open"
    assert snapshot(db, objects, ids["review"]) == before


def test_a_second_answer_is_409_already_answered_and_a_replay_is_the_original_200(
    client, db, objects, waiting
):
    first = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert first.status_code == 200, first.text
    after_first = snapshot(db, objects, waiting["review"])

    replay = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first.json(), "replayed": True}

    second = client.post(url(waiting), headers=auth_header("root"),
                         json=verdict(answer_id="r1-root", value="MERGE"))
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["reason"] == "already_answered" and detail["answered_by"] == "alice@saga.xyz"

    # The same answer_id with another payload is not a replay.
    changed = client.post(url(waiting), headers=auth_header("alice"),
                          json=verdict(value="MERGE"))
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason"] == "already_answered"
    assert snapshot(db, objects, waiting["review"]) == after_first


def test_a_cancelled_step_is_409_cancelled(client, db, waiting):
    assert client.post(f"/v1/workflows/{waiting['workflow_id']}/cancel",
                       headers=auth_header("alice")).status_code == 200
    response = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "cancelled"


# --------------------------------------------------------------------------
# the form (§4.6 step 6)
# --------------------------------------------------------------------------

def test_a_form_with_a_fake_token_is_stored_redacted(client, db, objects, waiting):
    token = fake_token()
    response = client.post(url(waiting), headers=auth_header("alice"),
                           json=verdict(findings=[f"the config has {token} in it"]))
    assert response.status_code == 200, response.text
    assert response.json()["redaction_count"] >= 1
    entry = db.docs[f"tasks/{waiting['review']}"]["result_summary"]["artifacts"][0]
    key = artifact_key(entry["uri"], tenant_id="eng", upstream_task_id=waiting["review"],
                       filename="review.json")
    stored = objects.objects[key].decode("utf-8")
    assert token not in stored
    assert "ghp_********" in stored
    assert token not in json.dumps(db.dump(), default=str)


@pytest.mark.parametrize("payload, position", [
    ({"verdict": "SHIP_IT", "findings": []}, "payload.verdict"),
    ({"verdict": "MERGE", "findings": [1]}, "payload.findings"),
    ({"verdict": "MERGE", "findings": ["x"] * 51}, "payload.findings"),
    ({"verdict": "MERGE", "findings": ["x" * 1001]}, "payload.findings[0]"),
    ({"verdict": "MERGE", "extra": 1}, "payload"),
    ("MERGE", "payload"),
])
def test_a_verdict_that_does_not_fit_the_form_is_422_naming_its_position(
    client, db, objects, waiting, payload, position
):
    before = snapshot(db, objects, waiting["review"])
    response = client.post(url(waiting), headers=auth_header("alice"),
                           json={"answer_id": "a1", "payload": payload})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "invalid_payload" and detail["position"] == position
    assert snapshot(db, objects, waiting["review"]) == before


def test_a_text_form_is_taken_and_bounded(client, db, await_enabled):
    ids = launch(client, db, block(form="text", artifact="input.json"))
    park(db, ids["review"])
    too_long = client.post(url(ids), headers=auth_header("alice"),
                           json={"answer_id": "t1", "payload": {"text": "x" * (64 * 1024 + 1)}})
    assert too_long.status_code == 422
    ok = client.post(url(ids), headers=auth_header("alice"),
                     json={"answer_id": "t1", "payload": {"text": "Looks right."}})
    assert ok.status_code == 200, ok.text


def _asked(db, objects, draft_id: str, questions: list[dict[str, Any]]) -> None:
    """The upstream agent wrote questions.json and the worker counted it."""
    key = f"tenants/eng/tasks/{draft_id}/attempts/att_1/artifacts/questions.json"
    data = json.dumps(questions).encode()
    objects.put(key, data)
    db.docs[f"tasks/{draft_id}"].update(state="SUCCEEDED", result_summary={
        "artifacts": [{"name": "questions.json", "bytes": len(data), "uri": objects.uri(key)}],
        "questions": len(questions),
    })


def test_an_answers_form_must_answer_every_question_with_its_options(
    client, db, objects, await_enabled
):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json={"steps": [
        {"step_id": "ask", "runner_profile": "mock", "input": {"prompt": "ask"}},
        {"step_id": "review", "runner_profile": PROFILE, "depends_on": ["ask"],
         "input_from": {"ask": "questions.json"}, "input": {"prompt": "Answer them."},
         "await_input": block(form="answers", artifact="answers.json")},
    ]})
    assert response.status_code == 201, response.text
    steps = {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}
    ids = {"workflow_id": response.json()["workflow"]["workflow_id"], **steps}
    _asked(db, objects, ids["ask"], [
        {"question": "Which region?", "options": [{"label": "us"}, {"label": "eu"}]},
        {"question": "Anything else?", "options": []},
    ])
    park(db, ids["review"])
    view = client.get(url(ids), headers=auth_header("alice")).json()
    assert [q["question"] for q in view["questions"]] == ["Which region?", "Anything else?"]

    def send(answers):
        return client.post(url(ids), headers=auth_header("alice"),
                           json={"answer_id": "q1", "payload": {"answers": answers}})

    unanswered = send([{"question_index": 0, "choice": "us", "text": None}])
    assert unanswered.status_code == 422 and unanswered.json()["detail"]["unanswered"] == [1]
    not_an_option = send([{"question_index": 0, "choice": "asia", "text": None},
                          {"question_index": 1, "choice": None, "text": "no"}])
    assert not_an_option.status_code == 422
    assert not_an_option.json()["detail"]["position"] == "payload.answers[0].choice"
    ok = send([{"question_index": 1, "choice": None, "text": "no"},
               {"question_index": 0, "choice": "eu", "text": None}])
    assert ok.status_code == 200, ok.text
    entry = db.docs[f"tasks/{ids['review']}"]["result_summary"]["artifacts"][0]
    stored = json.loads(objects.objects[artifact_key(
        entry["uri"], tenant_id="eng", upstream_task_id=ids["review"], filename="answers.json")])
    assert stored == {"answers": [{"question_index": 0, "choice": "eu", "text": None},
                                  {"question_index": 1, "choice": None, "text": "no"}]}


@pytest.mark.parametrize("answer_id", ["", "Upper", "x" * 65, "has space"])
def test_an_answer_id_outside_its_shape_is_422(client, waiting, answer_id):
    response = client.post(url(waiting), headers=auth_header("alice"),
                           json={"answer_id": answer_id, "payload": {"verdict": "MERGE"}})
    assert response.status_code == 422
    assert response.json()["detail"]["position"] == "answer_id"


# --------------------------------------------------------------------------
# the signature (§4.6 step 5)
# --------------------------------------------------------------------------

def test_an_unsigned_spec_is_refused_and_nothing_is_written(client, db, objects, waiting):
    doc = db.docs[f"tasks/{waiting['review']}"]
    doc.pop("spec_signature", None)
    before = snapshot(db, objects, waiting["review"])
    response = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "spec_refused"
    assert snapshot(db, objects, waiting["review"]) == before


def test_a_rewritten_declaration_is_refused(client, db, objects, waiting):
    doc = db.docs[f"tasks/{waiting['review']}"]
    doc["metadata"]["dispatch"]["await_input"]["max_files"] = 10
    doc["metadata"]["dispatch"]["await_input"]["artifact"] = "other.json"
    response = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "spec_refused"
    assert not objects.objects


# --------------------------------------------------------------------------
# one winner (§4.6, "The race, written out")
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["expiry", "cancel", "answer"])
def test_answer_expiry_and_cancel_racing_commit_exactly_once(
    client, db, api_context, writer, waiting, first
):
    task_id = waiting["review"]
    past_due(db, task_id)
    cancel = f"/v1/workflows/{waiting['workflow_id']}/cancel"
    fired: list[str] = []

    def interleave(key: str) -> None:
        # Inside the answer, after its checks passed and before its
        # transaction: the others land here.
        if fired:
            return
        fired.append(key)
        if first == "expiry":
            awaitinput.expire_due(api_context, writer)
            client.post(cancel, headers=auth_header("alice"))
        elif first == "cancel":
            client.post(cancel, headers=auth_header("alice"))
            awaitinput.expire_due(api_context, writer)

    writer.before_create = interleave
    answered = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    writer.before_create = None
    if first == "answer":
        awaitinput.expire_due(api_context, writer)
        client.post(cancel, headers=auth_header("alice"))

    doc = db.docs[f"tasks/{task_id}"]
    assert len(terminal_events(db, task_id)) == 1, terminal_events(db, task_id)
    if first == "answer":
        assert answered.status_code == 200, answered.text
        assert doc["state"] == "SUCCEEDED" and doc["metadata"]["await_input"]["answer"]
    else:
        assert answered.status_code == 409, answered.text
        assert answered.json()["detail"]["reason"] == (
            "expired" if first == "expiry" else "cancelled")
        assert doc["state"] == ("DEAD_LETTERED" if first == "expiry" else "CANCELLED")
        assert "answer" not in doc["metadata"]["await_input"]
        # The loser's objects exist and no manifest names them.
        assert writer.created and not (doc.get("result_summary") or {}).get("artifacts")


# --------------------------------------------------------------------------
# expiry (§4.6, the schedule tick)
# --------------------------------------------------------------------------

def test_expire_due_honours_fail(client, db, api_context, writer, waiting):
    past_due(db, waiting["review"])
    report = awaitinput.expire_due(api_context, writer)
    assert report is not None and report.failed == 1
    doc = db.docs[f"tasks/{waiting['review']}"]
    assert doc["state"] == "DEAD_LETTERED"
    assert doc["last_error"].startswith("await_input_expired: nobody answered within 3600")
    assert terminal_events(db, waiting["review"]) == ["dead_lettered"]
    assert not writer.created
    assert client.get(url(waiting), headers=auth_header("alice")).json()["status"] == "expired"
    late = client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert late.status_code == 409 and late.json()["detail"]["reason"] == "expired"


def test_expire_due_honours_default(client, db, objects, api_context, writer, await_enabled):
    ids = launch(client, db, block(on_expire="default",
                                   default={"verdict": "MERGE", "findings": []}))
    park(db, ids["review"])
    past_due(db, ids["review"])
    report = awaitinput.expire_due(api_context, writer)
    assert report is not None and report.defaulted == 1
    doc = db.docs[f"tasks/{ids['review']}"]
    assert doc["state"] == "SUCCEEDED"
    summary = doc["result_summary"]
    assert summary["input"]["source"] == "default" and summary["input"]["answered_by"] is None
    entry = summary["artifacts"][0]
    key = artifact_key(entry["uri"], tenant_id="eng", upstream_task_id=ids["review"],
                       filename="review.json")
    assert json.loads(objects.objects[key]) == {"verdict": "MERGE", "findings": []}
    assert ("task_finished", {"task_id": ids["review"], "tenant_id": "eng",
                              "state": "SUCCEEDED"}) in api_context.waker.calls


def test_expire_due_leaves_a_step_that_is_not_due(db, api_context, writer, waiting):
    report = awaitinput.expire_due(api_context, writer)
    assert report is not None and report.due == 0
    assert db.docs[f"tasks/{waiting['review']}"]["state"] == "PARKED"


def test_expire_due_dead_letters_an_unsigned_step_whatever_it_declares(
    client, db, api_context, writer, await_enabled
):
    ids = launch(client, db, block(on_expire="default",
                                   default={"verdict": "MERGE", "findings": []}))
    park(db, ids["review"])
    past_due(db, ids["review"])
    db.docs[f"tasks/{ids['review']}"].pop("spec_signature")
    report = awaitinput.expire_due(api_context, writer)
    assert report is not None and report.refused == 1
    doc = db.docs[f"tasks/{ids['review']}"]
    assert doc["state"] == "DEAD_LETTERED"
    assert doc["last_error"].startswith("await_input_spec_refused")
    assert not writer.created


def test_a_default_whose_object_fails_is_retried_by_the_next_tick(
    client, db, api_context, writer, await_enabled
):
    ids = launch(client, db, block(on_expire="default",
                                   default={"verdict": "MERGE", "findings": []}))
    park(db, ids["review"])
    past_due(db, ids["review"])
    writer.fail = True
    assert awaitinput.expire_due(api_context, writer).errors == 1
    assert db.docs[f"tasks/{ids['review']}"]["state"] == "PARKED"
    writer.fail = False
    assert awaitinput.expire_due(api_context, writer).defaulted == 1


def test_the_schedule_tick_runs_the_expiry(client, db, waiting):
    past_due(db, waiting["review"])
    response = client.post("/v1/admin/schedules/tick", headers={"Authorization": "Bearer token-tick"})
    assert response.status_code == 200, response.text
    assert response.json()["await_input"]["failed"] == 1
    assert db.docs[f"tasks/{waiting['review']}"]["state"] == "DEAD_LETTERED"


def test_while_the_profile_is_dark_the_tick_reads_nothing_and_says_nothing(
    await_dark, client, db, api_context, writer
):
    assert not RUNNER_PROFILES[PROFILE].available
    assert awaitinput.expire_due(api_context, writer) is None
    response = client.post("/v1/admin/schedules/tick", headers={"Authorization": "Bearer token-tick"})
    assert response.status_code == 200, response.text
    assert "await_input" not in response.json()


# --------------------------------------------------------------------------
# what the card and the bridge read
# --------------------------------------------------------------------------

def test_the_step_view_shows_the_wait_and_then_the_answer(client, db, waiting):
    view = client.get(url(waiting), headers=auth_header("alice")).json()
    assert view["status"] == "waiting" and view["may_answer"] is True
    assert view["form"] == "verdict" and view["artifact"] == "review.json"
    assert view["max_answer_bytes"] == 16 * 1024 * 1024
    assert view["shows"] == [{"step_id": "draft", "task_id": waiting["draft"], "name": "draft.md"}]
    assert view["expires_at"] and view["answer"] is None
    client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    view = client.get(url(waiting), headers=auth_header("alice")).json()
    assert view["status"] == "answered" and view["may_answer"] is False
    assert view["answer"]["answered_by"] == "alice@saga.xyz"


def test_the_bot_is_shown_no_form(client, waiting):
    view = client.get(url(waiting), headers=auth_header("swarm-bot")).json()
    assert view["may_answer"] is False


def test_the_inputs_list_holds_the_tenants_waiting_steps_only(client, db, waiting):
    listed = client.get("/v1/inputs", headers=auth_header("alice")).json()
    assert [i["task_id"] for i in listed["items"]] == [waiting["review"]]
    assert client.get("/v1/inputs", headers=auth_header("bob")).json()["items"] == []
    client.post(url(waiting), headers=auth_header("alice"), json=verdict())
    assert client.get("/v1/inputs", headers=auth_header("alice")).json()["items"] == []
    assert client.get("/v1/inputs?status=answered", headers=auth_header("alice")).status_code == 422
