"""The in-flight route (docs/design/context-quality.md §4.2 layer I, §4.4, §7 row CTXQ-I).

Drives the real route, `POST /v1/attempts/{attempt_id}/context/inflight`,
over the in-memory Firestore, with a worker identity the static verifier maps
to the tenant's worker service account and an attempt key registered exactly
as a worker registers one (`test_child_tasks_api`). The task's spec is signed
by a local P-256 key the settings trust (`test_git_tools_api`), so the
repository is read from a SIGNED spec as in production. The open pull
requests come from a fake reader in place of the forge, so nothing reaches
the network.

What each case pins, from §7's row CTXQ-I:

* an unproven caller is 403, the same answer for every cause, and nothing is
  read for it -- `child_submit_unproven`, the child routes' established code
  for the same attempt proof;
* a stale generation is 409 `child_submit_fenced`, and nothing is read for
  it either;
* another tenant's lane is never listed -- its live run, its running task --
  while the same tenant's, on the same files, is (the control);
* a forge that could not be read is said, never answered as "no pull
  requests".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import IssueRef
from swarm_api.waker import NullWaker

from .conftest import seed_task, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    ready_parent,
    worker_tokens,
)

SIGNER = LocalSpecSigner()
REPOSITORY_URL = "https://github.com/acme/widgets.git"
OWN_WORKFLOW = "wf_own"
ROUTES = "apps/api/routes.py"
SCHEMAS = "apps/api/schemas.py"
ELSEWHERE = "docs/unrelated.md"


class Pulls:
    """The open pull requests, as `territory.open_pull_requests` answers them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer: dict[str, Any] = {
            "ok": True, "code": None, "read_at": "2026-10-14T12:00:00+00:00",
            "pull_requests": [], "pull_requests_truncated": False,
        }

    def add(self, number: int, title: str, files: list[str] | None) -> None:
        self.answer["pull_requests"].append(
            {"number": number, "title": title, "files": files, "files_truncated": False})

    def __call__(self, tenant: Any, record: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((tenant.tenant_id, dict(record)))
        return self.answer


@pytest.fixture
def pulls() -> Pulls:
    return Pulls()


@pytest.fixture
def client(db, worker_tokens, group_map, objects, pulls) -> TestClient:  # noqa: F811
    from swarm_api.attemptcontext import AttemptContext
    from swarm_api.routes import attemptcontext as routes
    from swarm_api.routes.children import _worker_verifier

    from .conftest import api_settings

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=api_settings(
            child_key=KEY,
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
        ),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    application = create_app(ctx)

    def service() -> AttemptContext:
        return AttemptContext.from_context(ctx, verifier=_worker_verifier(ctx), pulls=pulls)

    application.dependency_overrides[routes.attempt_context] = service
    return TestClient(application, raise_server_exceptions=False)


def running_task(client, db, attempt: Attempt | None = None, *,
                 sign: bool = True) -> tuple[Attempt, dict[str, Any]]:
    """A RUNNING step of the caller's own issue run, its worker keyed, its spec signed."""
    attempt = attempt or Attempt()
    doc = ready_parent(client, db, attempt, runner_profile="claude-code",
                       repository_url=REPOSITORY_URL, workflow_id=OWN_WORKFLOW,
                       step_id="impl-s1")
    if sign:
        SIGNER.sign_document(doc, doc["id"])
    return attempt, doc


def call(client, attempt: Attempt, files: list[str] | None = None, *,
         token: str = WORKER_TOKEN, sign: bool = True, signer: Attempt | None = None,
         path_attempt: str | None = None, at: float | None = None, tamper: bool = False,
         extra: dict[str, Any] | None = None) -> Any:
    """The worker's call, shaped exactly as `agent_worker.graphmcp.remote.InFlightRelay.call` sends it."""
    t = attempt.tuple
    body = json.dumps({
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, "request_id": "req-1",
        "files": files if files is not None else [ROUTES], **(extra or {}),
    }, sort_keys=True).encode("utf-8")
    path = f"/v1/attempts/{path_attempt or t.attempt_id}/context/inflight"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update((signer or attempt).sign("POST", path, body, at=at))
    if tamper:
        body = body.replace(b"req-1", b"req-2")
    return client.post(path, content=body, headers=headers)


def seed_run(db, tenant: str, run_id: str, files: list[str], *, workflow_id: str | None = None,
             pull: int | None = None, state: RunState = RunState.RUNNING,
             repository: str = "acme/widgets") -> None:
    owner, name = repository.split("/")
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    IssueRuns(db).create(IssueRun(
        id=run_id, tenant_id=tenant, created_by="alice@example.com", created_at=at, updated_at=at,
        state=state, issue=IssueRef(owner=owner, repo=name, number=40 + len(run_id)),
        plan_approval="auto", auto_merge=False, fix_rounds=2, planner_task_id=f"plan_{run_id}",
        plan={"summary": "s", "steps": [{"step_id": "s1", "files": files}]},
        workflow_id=workflow_id,
        pull_request=None if pull is None else {"number": pull, "url": "u", "head_sha": "a" * 40},
    ))


def seed_running(db, tenant: str, task_id: str, *, repository_url: str = REPOSITORY_URL,
                 workflow_id: str | None = None) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state="RUNNING",
                    runner_profile="claude-code", workflow_id=workflow_id)
    doc["repository_url"] = repository_url


# --------------------------------------------------------------------------
# authentication: one 403 for every cause, and nothing read
# --------------------------------------------------------------------------


def test_an_unproven_caller_is_the_same_403_for_every_cause(client, db, pulls):
    attempt, _ = running_task(client, db)
    stranger = Attempt()  # a key nobody registered
    answers = {
        "no ID token": call(client, attempt, token=""),
        "a person's ID token": call(client, attempt, token="token-alice"),
        "another tenant's worker": call(client, attempt, token=OTHER_WORKER_TOKEN),
        "no proof": call(client, attempt, sign=False),
        "a key this attempt never registered": call(client, attempt, signer=stranger),
        "a stale timestamp": call(client, attempt, at=1_000_000_000),
        "a body the proof did not sign": call(client, attempt, tamper=True),
        "another attempt in the path": call(client, attempt, path_attempt="att_someone_else"),
    }
    unregistered = Attempt(task="task_never_registered", attempt="att_x", lease="lease_x")
    answers["an attempt with no registration"] = call(client, unregistered)
    statuses = {cause: r.status_code for cause, r in answers.items()}
    assert set(statuses.values()) == {403}, statuses
    bodies = {cause: r.json() for cause, r in answers.items()}
    first = next(iter(bodies.values()))
    assert all(body == first for body in bodies.values()), bodies
    # The child routes' established code: the same attempt proof.
    assert first["code"] == "child_submit_unproven"
    # Nothing was read for an unproven caller: not the forge.
    assert pulls.calls == []
    # The control: the same call, proven, is answered.
    assert call(client, attempt).status_code == 200


# --------------------------------------------------------------------------
# the fence (invariant 5)
# --------------------------------------------------------------------------


def test_a_stale_generation_is_409_and_reads_nothing(client, db, pulls):
    attempt, doc = running_task(client, db)
    seed_run(db, "eng", "run_other", [ROUTES])
    # The reconciler reclaimed the attempt; a new generation holds the task.
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_parent_2"
    response = call(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "child_submit_fenced"
    assert "run_other" not in response.text
    assert pulls.calls == []


def test_a_released_lease_and_a_cancelled_task_are_fenced_too(client, db):
    attempt, doc = running_task(client, db)
    assert call(client, attempt).status_code == 200  # the control
    doc["cancel_requested"] = True
    assert call(client, attempt).json()["code"] == "child_submit_fenced"
    doc["cancel_requested"] = False
    db.docs[f"leases/{attempt.tuple.lease_id}"]["released_at"] = datetime.now(timezone.utc)
    response = call(client, attempt)
    assert response.status_code == 409 and response.json()["code"] == "child_submit_fenced"


# --------------------------------------------------------------------------
# the signed spec, and what the caller may name (invariant 10)
# --------------------------------------------------------------------------


def test_an_unsigned_task_is_refused_before_anything_is_read(client, db, pulls):
    attempt, _ = running_task(client, db, sign=False)
    response = call(client, attempt)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert "spec_unverified" in response.json()["message"]
    assert pulls.calls == []


def test_the_caller_names_files_never_a_repository(client, db, pulls):
    attempt, _ = running_task(client, db)
    response = call(client, attempt, extra={"repository": "someone/else"})
    assert response.status_code == 422, response.text
    assert pulls.calls == []
    for bad in (["/etc/passwd"], ["../outside.py"], ["apps/*.py"], []):
        assert call(client, attempt, bad).status_code == 422, bad


def test_the_repository_is_the_signed_specs(client, db, pulls):
    attempt, _ = running_task(client, db)
    answer = call(client, attempt).json()
    assert answer["repository"] == "acme/widgets"
    assert [(tenant, record["owner"], record["repo"]) for tenant, record in pulls.calls] == [
        ("eng", "acme", "widgets")]


# --------------------------------------------------------------------------
# tenant isolation (invariant 9)
# --------------------------------------------------------------------------


def test_another_tenants_lane_is_never_listed_and_the_same_tenants_is(client, db):
    attempt, _ = running_task(client, db)
    # The same files, the same repository, in two tenants.
    seed_run(db, "research", "run_theirs", [ROUTES])
    seed_running(db, "research", "task_theirs")
    seed_run(db, "eng", "run_ours", [ROUTES])
    seed_running(db, "eng", "task_ours")
    response = call(client, attempt, [ROUTES])
    assert response.status_code == 200, response.text
    answer = response.json()
    # The control: the same tenant's lane and running task are listed.
    assert [lane["run_id"] for lane in answer["lanes"]] == ["run_ours"]
    assert answer["lanes"][0]["shared"][0]["path"] == ROUTES
    assert [t["task_id"] for t in answer["tasks_undeclared"]] == ["task_ours"]
    # The other tenant's are nowhere in the answer.
    assert "run_theirs" not in response.text
    assert "task_theirs" not in response.text
    assert "research" not in response.text


# --------------------------------------------------------------------------
# what is listed
# --------------------------------------------------------------------------


def test_only_overlapping_lanes_and_pull_requests_are_listed(client, db, pulls):
    attempt, _ = running_task(client, db)
    seed_run(db, "eng", "run_overlap", [ROUTES])
    seed_run(db, "eng", "run_dir", ["apps/api/"])
    seed_run(db, "eng", "run_apart", [ELSEWHERE])
    seed_run(db, "eng", "run_other_repo", [ROUTES], repository="acme/gadgets")
    seed_run(db, "eng", "run_done", [ROUTES], state=RunState.DONE)
    pulls.add(11, "edits the routes", [ROUTES, ELSEWHERE])
    pulls.add(12, "edits the docs", [ELSEWHERE])
    pulls.add(13, "files not read", None)
    answer = call(client, attempt, [ROUTES, SCHEMAS]).json()
    assert sorted(lane["run_id"] for lane in answer["lanes"]) == ["run_dir", "run_overlap"]
    assert [p["number"] for p in answer["pull_requests"]] == [11]
    assert answer["pull_requests"][0]["shared"][0]["path"] == ROUTES
    # Not read is never "no overlap": said, and the answer is not complete.
    assert answer["pull_requests_unread"] == [13]
    assert answer["complete"] is False


def test_the_callers_own_run_and_pull_request_are_left_out(client, db, pulls):
    attempt, _ = running_task(client, db)
    seed_run(db, "eng", "run_own", [ROUTES], workflow_id=OWN_WORKFLOW, pull=77)
    seed_running(db, "eng", "task_sibling", workflow_id=OWN_WORKFLOW)
    pulls.add(77, "the caller's own pull request", [ROUTES])
    pulls.add(78, "someone else's", [ROUTES])
    answer = call(client, attempt).json()
    assert answer["lanes"] == []
    assert [p["number"] for p in answer["pull_requests"]] == [78]
    assert answer["tasks_undeclared"] == []
    assert answer["excluded"] == {"run_id": "run_own", "pull_request": 77}


def test_a_running_task_of_a_live_run_is_its_lane_not_an_undeclared_task(client, db):
    attempt, _ = running_task(client, db)
    seed_run(db, "eng", "run_ours", [ROUTES], workflow_id="wf_ours")
    seed_running(db, "eng", "task_of_run", workflow_id="wf_ours")
    seed_running(db, "eng", "task_standalone")
    seed_running(db, "eng", "task_other_repo",
                 repository_url="https://github.com/acme/gadgets.git")
    answer = call(client, attempt).json()
    assert [lane["run_id"] for lane in answer["lanes"]] == ["run_ours"]
    assert [t["task_id"] for t in answer["tasks_undeclared"]] == ["task_standalone"]
    # The caller itself is never listed.
    assert "task_parent" not in json.dumps(answer["tasks_undeclared"])


def test_a_forge_that_could_not_be_read_is_said_never_an_empty_list(client, db, pulls):
    attempt, _ = running_task(client, db)
    pulls.answer = {"ok": False, "code": "issue_read_failed", "read_at": None,
                    "pull_requests": [], "pull_requests_truncated": False}
    answer = call(client, attempt).json()
    assert answer["pull_requests_read"] == {"ok": False, "code": "issue_read_failed",
                                            "read_at": None}
    assert answer["complete"] is False


def test_a_full_read_with_nothing_in_flight_is_complete(client, db):
    attempt, _ = running_task(client, db)
    answer = call(client, attempt).json()
    assert answer["pull_requests_read"]["ok"] is True
    assert (answer["lanes"], answer["pull_requests"], answer["tasks_undeclared"]) == ([], [], [])
    assert answer["complete"] is True
    assert answer["files"] == [ROUTES]
