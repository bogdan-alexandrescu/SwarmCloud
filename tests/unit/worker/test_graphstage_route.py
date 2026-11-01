"""An implement step gets its graph snapshot through swarm-api, never a Firestore listing (lane GRAPH-STAGING, part of #1694).

Measured 2026-10-19/20: 73 of 74 implement steps logged "no graph snapshot is
staged" -- 33 of them `staging failed (PermissionDenied)`. The refused call is
`graphstage._candidates`'s `index_versions` query (`.limit().stream()`): a
Firestore list, and the tenant worker's role has `entities.get` but no
`entities.list` (docs/design/tenant-settings-integrity.md §1.3). The design
(#1464, design B) removes direct access rather than widening it, so the fix
is NOT a new permission: swarm-api resolves the step's graph
(`POST /v1/attempts/{attempt_id}/context/graph`, `swarm_api.attemptcontext`)
and the worker stages from its answer (`agent_worker.graphroute`).

What this file holds:

* an implement step for a repository with a promoted index gets the snapshot
  staged, from the route's answer, with no Firestore call at all;
* a repository without an index says so in words, in the agent's prompt line;
* the worker makes no direct listing call: on the route, and on the direct
  path a worker without an attempt key still takes, where a listing is
  refused as production refuses it;
* a superseded attempt (409) is the fence, raised for the lifecycle;
* the lifecycle hands staging the route.

Offline: a LocalObjectStore, the worker's FakeFirestore, a local git repo and
a fake transport in place of swarm-api.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import PermissionDenied

from agent_worker import graph_ticket, graphroute, graphstage, lifecycle
from agent_worker.children import PROOF_HEADER, TIMESTAMP_HEADER, ChildApiError
from agent_worker.errors import ExitCode

from fakes import FakeFirestore, FakeQuery
from test_graphstage import REPO_ID, URL, _promote, clone  # noqa: F401  (clone is a fixture)
from worker_seeds import TENANT, seed_attempt

CODEX = ["python", "-m", "agent_worker.runners.codex"]
CFG = SimpleNamespace(tenant_id=TENANT, task_id="task_impl", attempt_id="att_impl",
                      lease_id="lease_impl", generation=3)
ROUTE = f"/v1/attempts/{CFG.attempt_id}/context/graph"


class RaisingFirestore:
    """A Firestore client that fails the test on any call at all."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"a Firestore call was made (db.{name})")


@pytest.fixture
def listings(monkeypatch) -> list[str]:
    """The worker's role in production: a get answers, a listing is PermissionDenied.

    Every Firestore query the fake serves -- a collection's `stream()` and a
    `limit(n).stream()` -- is refused and recorded here."""
    refused: list[str] = []

    def stream(self: FakeQuery, **_options: Any) -> Any:
        refused.append(self._prefix)
        raise PermissionDenied("Missing or insufficient permissions.")

    monkeypatch.setattr(FakeQuery, "stream", stream)
    return refused


class Key:
    """The child path's attempt key: signs, and records what it signed."""

    def __init__(self) -> None:
        self.signed: list[tuple[str, str, bytes]] = []

    def headers(self, method: str, path: str, body: bytes, *, now: float) -> dict[str, str]:
        self.signed.append((method, path, body))
        return {PROOF_HEADER: "proof-" + str(len(self.signed)), TIMESTAMP_HEADER: str(int(now))}


class Api:
    """swarm-api's graph context route, answered from `answer` or refused with `error`."""

    def __init__(self, answer: dict[str, Any] | None = None,
                 error: ChildApiError | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, str, bytes, dict[str, str]]] = []

    def call(self, method: str, path: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, body or b"", dict(headers or {})))
        if self.error is not None:
            raise self.error
        return 200, dict(self.answer or {})


def route_of(api: Api, *, keyed: bool = True) -> graphroute.GraphContextRoute:
    children = SimpleNamespace(_key=Key(), _api=api, keyed=keyed)
    return graphroute.GraphContextRoute(CFG, children=children, wall=lambda: 1_760_000_000.0)


def served(seed: FakeFirestore, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """What swarm-api's route answers for the documents `seed` holds (its shape:
    `swarm_api.attemptcontext.AttemptContext.graph_context`)."""
    prefix = f"repositories/{REPO_ID}/index_versions/"
    registration = seed.documents.get(f"repositories/{REPO_ID}")
    return {
        "ok": True,
        "repo_id": REPO_ID,
        "registration": registration,
        "versions": [{"id": path[len(prefix):], **doc} for path, doc in
                     sorted(seed.documents.items()) if path.startswith(prefix)],
        "tenant_settings": None,
        "workspace_settings": settings if settings is not None else
        {"code_intelligence": {"context": "on", "context_ab": "off"}},
    }


def prepare(work: Path, checkout: Path, base: str, *, db: Any, store: Any,
            route: Any) -> dict[str, str]:
    return graphstage.prepare(work, profile_name="codex", runner_argv=CODEX, db=db,
                              store=store, tenant_id=TENANT, repository_url=URL,
                              task_id=CFG.task_id, checkout=checkout, base_sha=base,
                              graph_context=route)


def record(work: Path) -> dict[str, Any]:
    path = work / graphstage.CONTEXT_DIR_NAME / graphstage.STAGE_RECORD_NAME
    return json.loads(path.read_text())


# --------------------------------------------------------------------------
# a promoted index: staged from swarm-api's answer
# --------------------------------------------------------------------------


def test_an_implement_step_with_a_promoted_index_gets_the_snapshot_staged(store, clone):
    checkout, first, second = clone
    seed = FakeFirestore()
    _promote(seed, store, first)
    api = Api(served(seed))

    env = prepare(checkout.parent, checkout, second, db=RaisingFirestore(), store=store,
                  route=route_of(api))

    # MUTATION: `prepare` ignores `graph_context` (or `stage` never asks it) ->
    # the RaisingFirestore is read, staging fails, and nothing is staged.
    assert graphstage.SNAPSHOT_ENV in env, record(checkout.parent)
    snapshot = Path(env[graphstage.SNAPSHOT_ENV])
    assert (snapshot / "manifest.json").is_file()
    stage = record(checkout.parent)
    assert stage["staged"] is True and stage["index_sha"] == first
    # One signed POST to this attempt's route, naming the attempt and nothing else.
    # MUTATION: a body carrying the repository URL or a repo_id -> not the tuple.
    assert [(m, p) for m, p, _b, _h in api.calls] == [("POST", ROUTE)]
    body = json.loads(api.calls[0][2])
    assert body == {"tenant_id": TENANT, "task_id": CFG.task_id, "attempt_id": CFG.attempt_id,
                    "lease_id": CFG.lease_id, "generation": CFG.generation}
    # MUTATION: send the call unsigned -> swarm-api answers 403 and nothing is staged.
    assert PROOF_HEADER in api.calls[0][3]


def test_the_body_is_the_one_swarm_apis_route_accepts():
    from swarm_api.attemptcontext import GraphContextBody

    api = Api({"ok": True, "repo_id": REPO_ID, "registration": None, "versions": []})
    route_of(api).get("repository")
    # MUTATION: a field the strict server model does not declare -> every call is a 422.
    assert set(json.loads(api.calls[0][2])) == set(GraphContextBody.model_fields)


def test_one_call_answers_both_the_repository_and_the_settings(store, clone):
    api = Api({"ok": True, "repo_id": REPO_ID, "registration": None, "versions": [],
               "tenant_settings": {"x": 1}, "workspace_settings": {"y": 2}})
    route = route_of(api)
    assert route.get("context") == {"tenant_settings": {"x": 1}, "workspace_settings": {"y": 2}}
    assert route.get("repository")["repo_id"] == REPO_ID
    # MUTATION: no answer cache in GraphContextRoute -> two calls per staging.
    assert len(api.calls) == 1


# --------------------------------------------------------------------------
# no index: said in words
# --------------------------------------------------------------------------


def test_a_repository_without_an_index_says_so_in_words(store, clone):
    checkout, _first, second = clone
    seed = FakeFirestore()
    seed.seed(f"repositories/{REPO_ID}", {"tenant_id": TENANT, "repo_id": REPO_ID,
                                           "index": {"current_sha": None}})
    env = prepare(checkout.parent, checkout, second, db=RaisingFirestore(), store=store,
                  route=route_of(Api(served(seed))))

    # MUTATION: answer an empty version list as a staging failure -> "staging failed (...)".
    assert env.get(graphstage.UNSTAGED_ENV) == \
        "no promoted index with a graph is kept for this repository"
    note = graphstage.unstaged_note(env)
    assert note is not None and "no promoted index" in note
    assert record(checkout.parent)["reason"] == env[graphstage.UNSTAGED_ENV]


def test_an_unregistered_repository_says_so_in_words(store, clone):
    checkout, _first, second = clone
    result = graphstage.stage(
        db=RaisingFirestore(), store=store, tenant_id=TENANT, repository_url=URL,
        task_id=CFG.task_id, checkout=checkout, base_sha=second,
        context_dir=checkout.parent / graphstage.CONTEXT_DIR_NAME,
        graph_context=route_of(Api(served(FakeFirestore()))))
    assert not result.staged
    # MUTATION: read a None registration as a mapping -> "staging failed (AttributeError)".
    assert result.reason == "the repository is not registered in this workspace"


# --------------------------------------------------------------------------
# no direct listing call, on either path
# --------------------------------------------------------------------------


def test_the_listing_control_refuses_as_production_does(listings):
    """The control: `listings == []` below could not hold by accident."""
    with pytest.raises(PermissionDenied):
        list(FakeFirestore().collection("repositories").limit(21).stream())
    assert listings == ["repositories"]


def test_the_direct_path_makes_no_listing_call_and_still_stages_the_promoted_index(
        store, clone, listings):
    """A worker with no attempt key (no SWARM_API_URL) reads Firestore directly:
    by document id only, as its role allows."""
    checkout, first, second = clone
    db = FakeFirestore()
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "off"}})

    env = prepare(checkout.parent, checkout, second, db=db, store=store,
                  route=route_of(Api(), keyed=False))

    # MUTATION: restore `versions.limit(MAX_VERSIONS_READ).stream()` in
    # `graphstage._candidates` -> PermissionDenied, "staging failed (PermissionDenied)".
    assert listings == []
    assert graphstage.SNAPSHOT_ENV in env, record(checkout.parent)


def test_a_route_that_is_not_answered_falls_back_to_reads_by_id(store, clone, listings):
    checkout, first, second = clone
    db = FakeFirestore()
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "off"}})
    api = Api(error=ChildApiError(404, "not_found", "no such route", retryable=False))

    env = prepare(checkout.parent, checkout, second, db=db, store=store, route=route_of(api))

    assert len(api.calls) == 1
    # MUTATION: a route failure ends staging -> nothing staged while swarm-api rolls out.
    assert graphstage.SNAPSHOT_ENV in env, record(checkout.parent)
    assert listings == []


def test_a_superseded_attempt_is_the_fence(store, clone):
    checkout, first, second = clone
    seed = FakeFirestore()
    _promote(seed, store, first)
    api = Api(error=ChildApiError(409, "child_submit_fenced", "superseded", retryable=False))
    # MUTATION: read a 409 as "unavailable" -> the stale attempt goes on to run its agent.
    with pytest.raises(graph_ticket.GraphTicketFenced):
        prepare(checkout.parent, checkout, second, db=RaisingFirestore(), store=store,
                route=route_of(api))


# --------------------------------------------------------------------------
# the lifecycle hands staging the route
# --------------------------------------------------------------------------


def test_the_lifecycle_hands_staging_the_route(db, worker_factory, monkeypatch):
    seen: list[Any] = []

    def capture(*_args: Any, **kwargs: Any) -> dict[str, str]:
        seen.append(kwargs.get("graph_context"))
        return {}

    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", capture)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    # MUTATION: `_graph_context_env` without `graph_context=` -> [None], and
    # every unswitched tenant's step lists Firestore again.
    assert len(seen) == 1 and isinstance(seen[0], graphroute.GraphContextRoute)
