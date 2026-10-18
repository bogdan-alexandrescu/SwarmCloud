"""The attempt graph ticket, the worker's half (docs/design/graph-service.md
§4.1 step 2-3, §10; lane GS-WORKER, part of #1384).

Minted from swarm-api's route with the attempt key under the route's own
purpose, written to `private/graph-ticket` mode 0600 by write-then-rename,
renewed when a third of its life is left with a 5 s to 60 s backoff, and a 409
is a fenced exit that leaves the lease alone (invariant 5). A fake of the route
VERIFIES every proof with swarm-api's own code, so a worker that signed the
wrong bytes fails here as it would against the real route.

Offline: no credentials, no emulator, no network.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import graph_ticket, lifecycle
from agent_worker.errors import ExitCode
from agent_worker.graphmcp import shim
from swarm_api import childkey as api_childkey
from swarm_api import graph_ticket as api_graph_ticket

from worker_seeds import TENANT, seed_attempt

INDEX_SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def _fake_ticket(exp: int = 2_000_000_000) -> str:
    """A ticket-shaped string built at runtime: never a literal credential."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{payload}.{secrets.token_urlsafe(32)}"


class FakeGraphRoutes:
    """`POST /v1/attempts/{id}/graph-ticket`, proofs checked with swarm-api's code."""

    def __init__(self) -> None:
        self.public_key: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.fail: children_mod.ChildApiError | None = None
        self.expires_in = 600
        self.issued: list[str] = []

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        message = api_graph_ticket.graph_ticket_message(method, path, body or b"", stamp)
        if not (self.public_key and proof and api_childkey.verify_proof(self.public_key, proof,
                                                                         message)):
            raise children_mod.ChildApiError(403, "forbidden", "unproven", retryable=False)
        if self.fail is not None:
            raise self.fail
        ticket = _fake_ticket()
        self.issued.append(ticket)
        payload = json.loads(body)
        return 200, {"ticket": ticket, "expires_in": self.expires_in,
                     "index_sha": payload["index_sha"], "manifest_digest": DIGEST}


class Wall:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []
        self.secrets: list[str] = []

    def info(self, msg: str, **kw: Any) -> None:
        self.lines.append(("info", msg, kw))

    def warning(self, msg: str, **kw: Any) -> None:
        self.lines.append(("warning", msg, kw))

    def register_secret(self, value: str) -> None:
        self.secrets.append(value)

    def text(self) -> str:
        return json.dumps(self.lines, default=str)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
                           lease_id="lease_1", generation=3)


@pytest.fixture
def routes() -> FakeGraphRoutes:
    return FakeGraphRoutes()


def _upkeep(routes: FakeGraphRoutes, wall: Wall | None = None, log: Log | None = None,
            keyed: bool = True) -> graph_ticket.GraphTicket:
    key = children_mod.AttemptKey() if keyed else None
    routes.public_key = key.public if key is not None else None
    children = SimpleNamespace(_key=key, _api=routes, keyed=keyed)
    return graph_ticket.GraphTicket(_cfg(), children=children, logger=log or Log(),
                                    wall=wall or Wall())


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    work, private = tmp_path / "work", tmp_path / "private"
    work.mkdir()
    private.mkdir(mode=0o700)
    return work, private


# --------------------------------------------------------------------------
# restatements held to swarm-api's
# --------------------------------------------------------------------------


def test_the_purpose_and_the_request_are_swarm_apis():
    assert graph_ticket.GRAPH_TICKET_PURPOSE == api_graph_ticket.TICKET_PURPOSE
    assert graph_ticket.REQUEST_FIELDS == set(api_graph_ticket.GraphTicketRequest.model_fields)
    assert graph_ticket.MAX_TICKET_SECONDS == api_graph_ticket.MAX_TICKET_SECONDS
    # The file the shim and the context pack read is the one written here.
    assert graph_ticket.MAX_TICKET_BYTES == shim.MAX_TICKET_BYTES


def test_mint_signs_the_body_swarm_api_reads_under_the_routes_purpose(routes):
    upkeep = _upkeep(routes)
    ticket, seconds = graph_ticket.mint(upkeep.cfg, upkeep.children._key, routes,
                                        index_sha=INDEX_SHA, now=1_000.0)
    assert (ticket, seconds) == (routes.issued[0], 600)
    (call,) = routes.calls
    assert call["path"] == "/v1/attempts/att_1/graph-ticket"
    body = json.loads(call["body"])
    # Exactly the request model's fields: the repository, the digest and the
    # tenant's registration come from the task, never from this body.
    assert set(body) == set(api_graph_ticket.GraphTicketRequest.model_fields)
    assert body["index_sha"] == INDEX_SHA and body["generation"] == 3
    api_graph_ticket.GraphTicketRequest.model_validate(body)


# --------------------------------------------------------------------------
# the file: private/, 0600, write-then-rename
# --------------------------------------------------------------------------


def test_the_first_ticket_is_written_0600_in_private_outside_work(routes, tmp_path):
    work, private = _workspace(tmp_path)
    upkeep = _upkeep(routes)
    assert upkeep.start(private, INDEX_SHA) is None
    path = upkeep.path
    assert path == private / graph_ticket.TICKET_FILE_NAME
    assert work not in path.parents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == routes.issued[0]
    # No temporary is left beside it.
    assert sorted(p.name for p in private.iterdir()) == [graph_ticket.TICKET_FILE_NAME]


def test_a_link_planted_at_the_path_is_replaced_not_followed(routes, tmp_path):
    _, private = _workspace(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (private / graph_ticket.TICKET_FILE_NAME).symlink_to(target)
    upkeep = _upkeep(routes)
    assert upkeep.start(private, INDEX_SHA) is None
    assert target.read_text() == "untouched"
    assert not upkeep.path.is_symlink()


def test_nothing_logged_carries_the_ticket(routes, tmp_path):
    _, private = _workspace(tmp_path)
    log = Log()
    wall = Wall()
    upkeep = _upkeep(routes, wall, log)
    upkeep.start(private, INDEX_SHA)
    wall.now += 500
    upkeep.keep()
    assert len(routes.issued) == 2
    for issued in routes.issued:
        assert issued not in log.text()
        assert issued.split(".")[1] not in log.text()
    # And each is handed to the redaction filter, as jevkey hands its key.
    assert log.secrets == routes.issued


# --------------------------------------------------------------------------
# renewal
# --------------------------------------------------------------------------


def test_the_ticket_is_renewed_when_a_third_of_its_life_is_left(routes, tmp_path):
    _, private = _workspace(tmp_path)
    wall = Wall()
    upkeep = _upkeep(routes, wall)
    upkeep.start(private, INDEX_SHA)
    first_inode = upkeep.path.stat().st_ino

    wall.now += 399                       # 201 s of 600 left: not yet
    assert not upkeep.due()
    upkeep.keep()
    assert len(routes.issued) == 1

    wall.now += 1                         # 200 s left: a third
    assert upkeep.due()
    upkeep.keep()
    assert len(routes.issued) == 2 and upkeep.renewals == 1
    # Renewed by rename: the shim's stat sees a new file, never a half-written one.
    assert upkeep.path.read_text() == routes.issued[1]
    assert upkeep.path.stat().st_ino != first_inode
    assert stat.S_IMODE(upkeep.path.stat().st_mode) == 0o600
    # The renewal asks for the same pinned commit.
    assert json.loads(routes.calls[-1]["body"])["index_sha"] == INDEX_SHA


def test_a_transient_failure_backs_off_from_5_s_to_a_60_s_cap(routes, tmp_path):
    _, private = _workspace(tmp_path)
    wall = Wall()
    upkeep = _upkeep(routes, wall)
    upkeep.start(private, INDEX_SHA)
    held = upkeep.path.read_text()
    routes.fail = children_mod.ChildApiError(503, "graph_ticket_unavailable", "",
                                             retryable=True)
    wall.now += 400
    waits = []
    for _ in range(6):
        before = len(routes.calls)
        upkeep.keep()
        assert len(routes.calls) == before + 1
        waits.append(upkeep.retry_at - wall.now)
        wall.now = upkeep.retry_at - 0.5
        upkeep.keep()                     # inside the backoff: nothing is asked
        assert len(routes.calls) == before + 1
        wall.now = upkeep.retry_at
    assert waits == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    # The ticket in hand is never removed for a transient failure.
    assert upkeep.path.read_text() == held

    routes.fail = None
    upkeep.keep()
    assert upkeep.path.read_text() == routes.issued[-1] != held
    # A success resets the backoff.
    routes.fail = children_mod.ChildApiError(None, "api_unavailable", "", retryable=True)
    wall.now += 400
    upkeep.keep()
    assert upkeep.retry_at - wall.now == 5.0


def test_a_permanent_refusal_mid_run_stops_renewing_and_raises_nothing(routes, tmp_path):
    _, private = _workspace(tmp_path)
    wall = Wall()
    upkeep = _upkeep(routes, wall)
    upkeep.start(private, INDEX_SHA)
    routes.fail = children_mod.ChildApiError(403, "forbidden", "", retryable=False)
    wall.now += 400
    upkeep.keep()
    calls = len(routes.calls)
    wall.now += 600
    assert not upkeep.due()
    upkeep.keep()
    assert len(routes.calls) == calls


def test_an_upkeep_that_never_started_asks_nothing(routes):
    upkeep = _upkeep(routes)
    assert not upkeep.due()
    upkeep.keep()
    assert routes.calls == []


def test_a_discarded_ticket_is_removed_and_never_renewed(routes, tmp_path):
    _, private = _workspace(tmp_path)
    wall = Wall()
    upkeep = _upkeep(routes, wall)
    upkeep.start(private, INDEX_SHA)
    upkeep.discard()
    assert not (private / graph_ticket.TICKET_FILE_NAME).exists()
    wall.now += 600
    assert not upkeep.due()
    upkeep.keep()
    assert len(routes.calls) == 1


# --------------------------------------------------------------------------
# the fence and the refusals
# --------------------------------------------------------------------------


def _fenced() -> children_mod.ChildApiError:
    return children_mod.ChildApiError(409, "conflict", "fenced", retryable=False)


def test_a_409_at_the_first_mint_is_fenced_and_writes_nothing(routes, tmp_path):
    _, private = _workspace(tmp_path)
    routes.fail = _fenced()
    upkeep = _upkeep(routes)
    with pytest.raises(graph_ticket.GraphTicketFenced):
        upkeep.start(private, INDEX_SHA)
    assert list(private.iterdir()) == []


def test_a_409_at_a_renewal_is_fenced(routes, tmp_path):
    _, private = _workspace(tmp_path)
    wall = Wall()
    upkeep = _upkeep(routes, wall)
    upkeep.start(private, INDEX_SHA)
    routes.fail = _fenced()
    wall.now += 400
    with pytest.raises(graph_ticket.GraphTicketFenced):
        upkeep.keep()


def test_a_403_at_the_first_mint_is_a_refusal_not_a_fallback(routes, tmp_path):
    _, private = _workspace(tmp_path)
    routes.fail = children_mod.ChildApiError(403, "forbidden", "graph context is off",
                                             retryable=False)
    upkeep = _upkeep(routes)
    with pytest.raises(graph_ticket.GraphTicketForbidden) as refused:
        upkeep.start(private, INDEX_SHA)
    assert "forbidden" in str(refused.value)
    assert list(private.iterdir()) == []


@pytest.mark.parametrize("error,why", [
    (children_mod.ChildApiError(503, "graph_ticket_unavailable", "", retryable=True),
     "graph_ticket_unavailable"),
    (children_mod.ChildApiError(404, "no_graph", "", retryable=False), "no_graph"),
    (children_mod.ChildApiError(None, "api_unavailable", "", retryable=True), "api_unavailable"),
])
def test_any_other_failure_at_the_first_mint_is_a_reason_to_stage(routes, tmp_path, error, why):
    _, private = _workspace(tmp_path)
    routes.fail = error
    upkeep = _upkeep(routes)
    reason = upkeep.start(private, INDEX_SHA)
    assert reason is not None and why in reason
    assert list(private.iterdir()) == []
    assert not upkeep.due()


def test_without_an_attempt_key_there_is_no_ticket_to_ask_for(routes, tmp_path):
    _, private = _workspace(tmp_path)
    upkeep = _upkeep(routes, keyed=False)
    assert upkeep.start(private, INDEX_SHA) == graph_ticket.NO_ATTEMPT_KEY
    assert routes.calls == []


@pytest.mark.parametrize("payload", [
    {"expires_in": 600},
    {"ticket": "has a space", "expires_in": 600},
    {"ticket": "ok.ok", "expires_in": 0},
    {"ticket": "ok.ok", "expires_in": True},
    {"ticket": "ok.ok", "expires_in": 600, "index_sha": "c" * 40},
])
def test_an_answer_that_is_not_a_ticket_for_this_commit_is_not_written(tmp_path, payload):
    _, private = _workspace(tmp_path)
    routes = FakeGraphRoutes()
    upkeep = _upkeep(routes)

    def call(method, path, *, body=None, headers=None):
        return 200, {"index_sha": INDEX_SHA, **payload}

    routes.call = call  # type: ignore[method-assign]
    assert upkeep.start(private, INDEX_SHA) is not None
    assert list(private.iterdir()) == []


# --------------------------------------------------------------------------
# the probe (§4.1 step 3)
# --------------------------------------------------------------------------


def _ticket_file(tmp_path: Path) -> tuple[Path, str]:
    value = _fake_ticket()
    path = tmp_path / "graph-ticket"
    path.write_text(value)
    return path, value


def test_the_probe_is_one_initialize_with_the_ticket_within_2_s(tmp_path):
    path, value = _ticket_file(tmp_path)
    sent: list[tuple[str, dict, dict, float]] = []

    def transport(url, body, headers, timeout):
        sent.append((url, json.loads(body), headers, timeout))
        return 200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                "result": {"serverInfo": {"name": "swarm-graph"}}}).encode()

    assert graph_ticket.probe("http://graph.test/mcp", path, transport=transport) is None
    ((url, body, headers, timeout),) = sent
    assert url == "http://graph.test/mcp" and timeout == graph_ticket.PROBE_TIMEOUT_SECONDS == 2.0
    assert body["method"] == "initialize"
    assert headers["Authorization"] == f"Bearer {value}"


@pytest.mark.parametrize("answer,why", [
    ((503, b'{"error":"disabled"}'), "HTTP 503"),
    ((401, b'{"error":"ticket_refused"}'), "HTTP 401"),
    ((404, b'{"error":"index_missing"}'), "HTTP 404"),
    ((200, b"not json"), "could not be read"),
    ((200, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"x"}}'),
     "could not be read"),
    (shim.ServiceUnreachable("timed out"), "timed out"),
])
def test_a_failed_probe_says_why_and_never_raises(tmp_path, answer, why):
    path, _ = _ticket_file(tmp_path)

    def transport(url, body, headers, timeout):
        if isinstance(answer, Exception):
            raise answer
        return answer

    reason = graph_ticket.probe("http://graph.test/mcp", path, transport=transport)
    assert reason is not None and why in reason


def test_a_probe_without_a_readable_ticket_fails_without_asking(tmp_path):
    asked = []
    reason = graph_ticket.probe("http://graph.test/mcp", tmp_path / "absent",
                                transport=lambda *a: asked.append(a) or (200, b"{}"))
    assert reason is not None and asked == []


# --------------------------------------------------------------------------
# the lifecycle: a 409 is a fenced exit that leaves the lease alone
# --------------------------------------------------------------------------


AGENT = r'''
import json, os, pathlib, sys, time
time.sleep(float(sys.argv[1]))
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None, "metrics": {},
     "artifacts": []}))
'''


@pytest.fixture
def sleepy_agent(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    monkeypatch.setattr(lifecycle, "_runner_argv",
                        lambda cfg: [sys.executable, str(script), "4"])


def test_the_lifecycle_owns_one_upkeep_for_the_attempt(db, worker_factory):
    worker, _, _ = worker_factory()
    assert isinstance(worker.graph_ticket, graph_ticket.GraphTicket)
    assert worker.graph_ticket.children is worker.children


def test_a_fenced_first_mint_stands_down_without_touching_the_lease(
        db, worker_factory, monkeypatch, sleepy_agent):
    def fenced(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise graph_ticket.GraphTicketFenced("graph ticket: 409 conflict")

    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", fenced)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None


def test_a_fenced_renewal_mid_run_stops_the_agent_and_leaves_the_lease(
        db, worker_factory, monkeypatch, sleepy_agent):
    def fenced(self: graph_ticket.GraphTicket) -> None:
        raise graph_ticket.GraphTicketFenced("graph ticket: 409 conflict")

    monkeypatch.setattr(graph_ticket.GraphTicket, "due", lambda self: True)
    monkeypatch.setattr(graph_ticket.GraphTicket, "keep", fenced)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None


def test_a_renewal_that_is_not_due_costs_the_poll_nothing(
        db, worker_factory, monkeypatch, sleepy_agent):
    kept: list[int] = []
    monkeypatch.setattr(graph_ticket.GraphTicket, "keep", lambda self: kept.append(1))
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    assert kept == []


def test_the_ticket_file_is_never_in_the_checkpoint(routes, tmp_path):
    """`private/` is the workspace's never-checkpointed directory; the ticket
    lives there and nowhere under `work/`."""
    from agent_worker import workspace as workspace_mod

    assert "private" in workspace_mod.__doc__ and "never" in workspace_mod.__doc__
    _, private = _workspace(tmp_path)
    upkeep = _upkeep(routes)
    upkeep.start(private, INDEX_SHA)
    assert upkeep.path.parent.name == "private"
    assert os.path.commonpath([upkeep.path, tmp_path / "work"]) != str(tmp_path / "work")
