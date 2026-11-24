"""The bridge sends one idempotency key per logical submission, and the same
key when it re-sends that submission after a timeout.

Two halves. Over a recorded wire: every submit route carries a key, a new
submission a new key, a timed-out send is re-sent once with the same body and
key, a refusal is never re-sent, and the caller's payload never holds the key.
Against the REAL swarm-api (its app over the in-memory Firestore, as
test_against_the_real_api.py builds it): a send the API accepted but whose
answer was lost is re-sent, and the API answers the first submission's ids
and creates nothing.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from swarm_mcp import client as mcp_client
from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmClient, SwarmError

from control_plane.conftest import api_settings, seed_tenant
from control_plane.fakes import FakeFirestore

BASE = "http://api.invalid"
AUTH = {"Authorization": "Bearer token-alice"}
REPO = "https://github.com/example-org/example-repo"


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- over a recorded wire -----------------------------------------------------


@pytest.fixture()
def wire(monkeypatch):
    """Every request the client sends, and a script of what each send meets:
    an exception to raise, or None for a 200 with `answer`."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Wire", (), {})()
    state.sent: list[tuple[str, str, dict[str, Any] | None]] = []
    state.script: list[BaseException | None] = []
    state.answer = {"task": {"id": "task_1"}}

    def _opener(req, timeout=None):  # noqa: ARG001
        body = json.loads(req.data) if req.data else None
        state.sent.append((req.get_method(), req.full_url[len(BASE):], body))
        failure = state.script.pop(0) if state.script else None
        if failure is not None:
            raise failure
        return _Response(json.dumps(state.answer).encode())

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return state


def _keys(wire) -> list[str]:
    return [body["idempotency_key"] for (_m, _p, body) in wire.sent]


def test_each_submit_route_sends_a_key(wire):
    client = SwarmClient(base_url=BASE)
    client.dispatch(repository_url=REPO, prompt="x", runner_profile="claude-code")
    wire.answer = {"tasks": [{"id": "task_1"}]}
    client.dispatch_batch([{"runner_profile": "mock", "input": {"prompt": "x"}}])
    wire.answer = {"workflow": {"workflow_id": "wf_1", "steps": []}}
    workflows.submit(client, steps=[{"id": "a", "prompt": "x"}])
    assert [(m, p) for (m, p, _b) in wire.sent] == [
        ("POST", "/v1/tasks"), ("POST", "/v1/tasks/batch"), ("POST", "/v1/workflows")]
    keys = _keys(wire)
    assert all(isinstance(k, str) and 16 <= len(k) <= 128 for k in keys), keys
    assert len(set(keys)) == 3, "three submissions, three keys"


def test_a_new_submission_of_the_same_work_gets_a_new_key(wire):
    client = SwarmClient(base_url=BASE)
    client.dispatch(repository_url=REPO, prompt="x", runner_profile="claude-code")
    client.dispatch(repository_url=REPO, prompt="x", runner_profile="claude-code")
    first, second = _keys(wire)
    assert first != second


@pytest.mark.parametrize("route", ["task", "batch", "workflow", "task_with_credentials"])
def test_a_timed_out_send_is_resent_once_with_the_same_key_and_body(wire, route):
    wire.script = [TimeoutError("timed out")]
    client = SwarmClient(base_url=BASE)
    if route == "task":
        client.dispatch(repository_url=REPO, prompt="x", runner_profile="claude-code")
    elif route == "batch":
        wire.answer = {"tasks": [{"id": "task_1"}]}
        client.dispatch_batch([{"runner_profile": "mock", "input": {"prompt": "x"}}])
    elif route == "workflow":
        wire.answer = {"workflow": {"workflow_id": "wf_1", "steps": []}}
        workflows.submit(client, steps=[{"id": "a", "prompt": "x"}])
    else:
        server._send_task(
            client, {"srv": {"value": "v", "expires_in": 60}},
            repository_url=REPO, prompt="x", runner_profile="claude-code")
    assert len(wire.sent) == 2, wire.sent
    (m1, p1, b1), (m2, p2, b2) = wire.sent
    assert (m1, p1) == (m2, p2)
    assert b1 == b2, "the re-send is the same submission: same body, same key"


def test_two_timeouts_in_a_row_stop_after_two_sends_and_say_so(wire):
    wire.script = [TimeoutError("timed out"), TimeoutError("timed out")]
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url=BASE).dispatch_batch([{"runner_profile": "mock", "input": {}}])
    assert len(wire.sent) == 2
    assert len(set(_keys(wire))) == 1
    text = str(caught.value)
    assert "may have been accepted" in text and "read the running workflows" in text
    assert _keys(wire)[0] not in text, "the key is never in a reply"


def test_a_refusal_is_not_resent(wire):
    headers = email.message.Message()
    wire.script = [urllib.error.HTTPError(f"{BASE}/v1/tasks/batch", 400, "Bad", headers,
                                          io.BytesIO(b'{"code": "bad", "message": "no"}'))]
    with pytest.raises(SwarmError):
        SwarmClient(base_url=BASE).dispatch_batch([{"runner_profile": "mock", "input": {}}])
    assert len(wire.sent) == 1


def test_the_callers_payload_never_holds_the_key(wire):
    tasks = [{"runner_profile": "mock", "input": {"prompt": "x"}}]
    wire.answer = {"tasks": [{"id": "task_1"}]}
    answer = SwarmClient(base_url=BASE).dispatch_batch(tasks)
    assert tasks == [{"runner_profile": "mock", "input": {"prompt": "x"}}]
    assert "idempotency_key" not in json.dumps(answer)


# -- against the real API -----------------------------------------------------


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def api(db) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier({"token-alice": {
            "email": "alice@example.com", "email_verified": True,
            "sub": "sub-alice", "hd": "example.com"}}),
        groups=StaticGroups({"alice@example.com": ("eng@example.com",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    seed_tenant(db, "eng")
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture()
def lossy(api, monkeypatch):
    """The real application behind the client's transport, where the FIRST
    POST is delivered and accepted but its answer is lost to a timeout."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Lossy", (), {})()
    state.answers: list[dict[str, Any]] = []
    state.lost = 0

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len(BASE):]
        response = api.request(req.get_method(), path, content=req.data,
                               headers={**dict(req.headers), **AUTH})
        if response.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, response.status_code, "error",
                                         response.headers, io.BytesIO(response.content))
        if req.get_method() == "POST" and state.lost == 0:
            state.lost += 1
            raise TimeoutError("timed out")
        state.answers.append(response.json())
        return _Response(response.content, response.status_code)

    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    return state


def _task_ids(db) -> list[str]:
    return [p.split("/")[1] for p in db.paths("tasks/") if p.count("/") == 1]


def test_a_batch_accepted_before_its_timeout_is_not_duplicated_by_the_resend(lossy, db):
    created = SwarmClient(base_url=BASE).dispatch_batch(
        [{"runner_profile": "mock", "input": {"prompt": "one"}},
         {"runner_profile": "mock", "input": {"prompt": "two"}}])
    assert lossy.lost == 1
    assert lossy.answers[0]["idempotent_replay"] is True
    assert sorted(t["id"] for t in created) == sorted(_task_ids(db))
    assert len(_task_ids(db)) == 2


def test_a_task_accepted_before_its_timeout_is_not_duplicated_by_the_resend(lossy, db):
    task = SwarmClient(base_url=BASE).dispatch(prompt="x", runner_profile="mock")
    assert lossy.answers[0]["idempotent_replay"] is True
    assert _task_ids(db) == [task["id"]]


def test_a_workflow_accepted_before_its_timeout_is_not_duplicated_by_the_resend(lossy, db):
    workflows.submit(SwarmClient(base_url=BASE),
                     steps=[{"step_id": "a", "runner_profile": "mock", "input": {"prompt": "x"}}])
    assert lossy.answers[0]["idempotent_replay"] is True
    assert len([p for p in db.paths("workflows/") if p.count("/") == 1]) == 1
    assert len(_task_ids(db)) == 1
