"""Child tasks, the worker's half (docs/design/child-tasks.md §3.1-§3.3).

A real worker runs a small "agent" script in place of the catalogue's runner
(the same substitution test_cpu_sampler.py makes), and talks to a fake of
swarm-api's worker-only routes that VERIFIES every attempt proof with
swarm-api's own code (`swarm_api.childkey`), so a worker that signed the wrong
bytes fails here as it would against the real route.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import control as control_mod
from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.hardening import FAILED, MemoryProtection
from swarm_api import childkey as api_childkey
from swarm_api import children as api_children
from swarm_api import validation as api_validation
from swarm_common.states import EventType, ParkReason, TaskState

from conftest import TENANT, seed_attempt

NONCE = "a1b2c3d4-test-nonce-not-a-secret"

AGENT = r'''
import json, os, sys, time, pathlib
plan = json.load(open(sys.argv[1]))
spool = os.environ.get("SWARM_CHILDREN")
seen = {"spool": spool, "env": dict(os.environ)}
if spool:
    root = pathlib.Path(spool)
    result = root / "results" / "children.json"
    seen["children_json"] = json.loads(result.read_text()) if result.exists() else None
    staged = root / "results"
    seen["staged"] = sorted(str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file())
    for request in plan.get("requests", []):
        tmp = root / "requests" / (request["request_id"] + ".tmp")
        tmp.write_text(json.dumps(request))
        os.replace(tmp, root / "requests" / (request["request_id"] + ".json"))
    want = [r["request_id"] for r in plan.get("requests", [])]
    deadline = time.time() + float(plan.get("wait", 8))
    while time.time() < deadline and not all((root / "responses" / (w + ".json")).exists() for w in want):
        time.sleep(0.1)
    seen["responses"] = {
        w: json.loads((root / "responses" / (w + ".json")).read_text())
        for w in want if (root / "responses" / (w + ".json")).exists()
    }
    if plan.get("await"):
        (root / "await").write_text("")
with open(plan["record"], "a") as handle:
    handle.write(json.dumps(seen) + "\n")
code = int(plan.get("exit", 0))
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded" if code == 0 else "failed", "summary": "", "output": {},
     "error": None if code == 0 else "agent failed", "metrics": {}, "artifacts": []}))
sys.exit(code)
'''


class FakeChildRoutes:
    """swarm-api's three worker routes over the worker test's Firestore."""

    def __init__(self, db) -> None:
        self.db = db
        self.calls: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.registered_state: str | None = None
        self.fence_submissions = False
        self._made = 0

    def _verify(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER)
        message = api_childkey.request_message(method, path, body, stamp or "")
        if not (self.public_key and proof and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(403, "child_submit_unproven", "", retryable=False)

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            payload = json.loads(body)
            self.registered_state = self.db.doc(f"tasks/{payload['task_id']}")["state"]
            self.registration = payload
            self.public_key = payload.get("public_key")
            return 201, {"registered": "key" if self.public_key else "tombstone"}
        if method == "POST":
            self._verify(method, path, body, headers)
            if self.fence_submissions:
                raise children_mod.ChildSubmitFenced(409, "child_submit_fenced", "", retryable=False)
            payload = json.loads(body)
            for doc_path, doc in self.db.documents.items():
                if doc.get("parent_task_id") == "task_1" and (doc.get("metadata") or {}).get(
                    "child_request_id"
                ) == payload["request_id"]:
                    return 200, {"task": {"id": doc["id"]}, "created": False}
            self._made += 1
            child_id = f"task_child{self._made}"
            self.db.seed(
                f"tasks/{child_id}",
                {
                    "id": child_id, "tenant_id": TENANT, "state": "READY",
                    "parent_task_id": "task_1", "parent_attempt_id": payload["attempt_id"],
                    "metadata": {"child_request_id": payload["request_id"]},
                },
            )
            return 201, {"task": {"id": child_id}, "created": True}
        self._verify(method, path, b"", headers)
        rows = []
        for doc in self.db.documents.values():
            if doc.get("parent_task_id") != "task_1":
                continue
            summary = doc.get("result_summary") or {}
            rows.append(
                {
                    "task_id": doc["id"],
                    "request_id": (doc.get("metadata") or {}).get("child_request_id"),
                    "state": doc["state"],
                    "end_cause": doc.get("end_cause"),
                    "parent_attempt_id": doc.get("parent_attempt_id"),
                    "artifacts": summary.get("artifacts") or [],
                }
            )
        return 200, {"children": rows}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """Replace the runner with the agent script; returns (write_plan, records)."""
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    plan_path = tmp_path / "plan.json"
    record = tmp_path / "record.jsonl"
    monkeypatch.setattr(lifecycle, "_runner_argv", lambda cfg: [sys.executable, str(script), str(plan_path)])

    def write_plan(**plan: Any) -> None:
        plan_path.write_text(json.dumps({"record": str(record), **plan}))

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []

    write_plan()
    return write_plan, records


@pytest.fixture
def routes(db) -> FakeChildRoutes:
    return FakeChildRoutes(db)


def _worker(worker_factory, routes, **kwargs):
    kwargs.setdefault("child_nonce", NONCE)
    kwargs.setdefault("swarm_api_url", "https://swarm-api.example")
    return worker_factory(child_api=routes, **kwargs)


def _request(request_id: str, **child: Any) -> dict[str, Any]:
    return {"request_id": request_id, "runner_profile": "mock", "input": {"prompt": "help"}, **child}


# --------------------------------------------------------------------------
# Restatements held to swarm-api's
# --------------------------------------------------------------------------


def test_the_workers_restatements_match_swarm_api():
    assert children_mod.REQUEST_ID.pattern == api_children.REQUEST_ID.pattern
    assert children_mod.REQUEST_PURPOSE == api_childkey.REQUEST_PURPOSE
    assert children_mod.PROOF_HEADER == api_children.PROOF_HEADER
    assert children_mod.TIMESTAMP_HEADER == api_children.TIMESTAMP_HEADER
    assert children_mod.WORKER_UNPROTECTED == api_childkey.WORKER_UNPROTECTED
    assert children_mod.CHILD_FIELDS == set(api_children.ChildSpec.model_fields)
    assert control_mod.CHILD_AWAIT_RESUMES_METADATA_KEY == api_validation.CHILD_AWAIT_RESUMES_METADATA_KEY
    assert control_mod.CHILD_CASCADE_METADATA_KEY == api_validation.CHILD_CASCADE_METADATA_KEY
    for args in [("POST", "/v1/tasks/t/children", b'{"a":1}', "17"), ("GET", "/p?q=1", b"", "9")]:
        assert children_mod.request_message(*args) == api_childkey.request_message(*args)


# --------------------------------------------------------------------------
# §3.2: registration before the agent exists
# --------------------------------------------------------------------------


def test_the_key_is_registered_while_starting_and_the_agent_gets_only_the_spool(
    db, worker_factory, routes, agent, log_stream
):
    write_plan, records = agent
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes)
    rc = worker.run()
    assert rc == ExitCode.OK, (db.doc("tasks/task_1").get("last_error"), db.doc("tasks/task_1").get("result_summary"))
    assert routes.registered_state == TaskState.STARTING.value
    assert routes.registration["nonce"] == NONCE and routes.public_key
    (seen,) = records()
    assert seen["spool"] and seen["spool"].endswith(".swarm-children")
    # Nothing that authorises a submission reaches the agent.
    env_values = "\n".join(seen["env"].values())
    assert NONCE not in env_values
    assert "SWARM_CHILD_NONCE" not in seen["env"] and "SWARM_API_URL" not in seen["env"]
    assert NONCE not in log_stream.getvalue()
    assert "AttemptKey(<heap only>)" == repr(worker.children._key)


def test_an_unprotected_worker_spends_the_nonce_on_a_tombstone(db, worker_factory, routes, agent):
    """§5 F12: the nonce is spent, and the agent gets no spool."""
    write_plan, records = agent
    seed_attempt(db)
    worker, _, _ = _worker(
        worker_factory, routes, memory=MemoryProtection(FAILED, "prctl refused in this test")
    )
    assert worker.run() == ExitCode.OK
    assert routes.registration.get("refused") == "worker_unprotected"
    assert "public_key" not in routes.registration
    assert records()[0]["spool"] is None


def test_without_a_nonce_there_is_no_child_path(db, worker_factory, routes, agent):
    write_plan, records = agent
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes, child_nonce=None)
    assert worker.run() == ExitCode.OK
    assert routes.calls == []
    assert records()[0]["spool"] is None


# --------------------------------------------------------------------------
# §3.1: submitting through the spool
# --------------------------------------------------------------------------


def test_requests_are_answered_with_a_child_id_or_a_refusal(db, worker_factory, routes, agent):
    write_plan, records = agent
    write_plan(
        requests=[
            _request("one"),
            _request("bad", image="evil:latest"),
            {"request_id": "merge", "runner_profile": "merge"},
        ]
    )
    seed_attempt(db)
    # The children still run: the attempt is awaited implicitly (step 8).
    worker, _, _ = _worker(worker_factory, routes)
    assert worker.run() == ExitCode.PARKED
    (seen,) = records()
    assert seen["responses"]["one"] == {"task_id": "task_child1", "request_id": "one"}
    assert seen["responses"]["bad"]["refused"]["code"] == "request_invalid"
    assert seen["responses"]["merge"]["refused"]["code"] == "worker_action_profile"
    posts = [c for c in routes.calls if c["method"] == "POST" and c["path"].endswith("/children")]
    assert len(posts) == 1, "a local refusal costs no round trip"
    sent = json.loads(posts[0]["body"])
    assert sent["attempt_id"] == "att_1" and sent["generation"] == 1
    assert "image" not in json.dumps(sent)


def test_a_fenced_submission_stops_the_agent_and_leaves_the_lease(db, worker_factory, routes, agent):
    """Invariant 5: a superseded worker ends as a fenced one does, lease untouched."""
    write_plan, records = agent
    write_plan(requests=[_request("one")], wait=6)
    routes.fence_submissions = True
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes)
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None


# --------------------------------------------------------------------------
# §3.3: the await park and the resume
# --------------------------------------------------------------------------


def _await_park(db, worker_factory, routes, agent) -> dict[str, Any]:
    write_plan, _ = agent
    write_plan(requests=[_request("one"), _request("two")], **{"await": True})
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes)
    assert worker.run() == ExitCode.PARKED
    return db.doc("tasks/task_1")


def test_await_checkpoints_parks_refunds_and_releases(db, worker_factory, routes, agent):
    task = _await_park(db, worker_factory, routes, agent)
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.CHILDREN_INCOMPLETE.value
    assert task["current_lease_id"] is None
    # Invariant 1/4: the slot is given back, nothing waits.
    assert db.doc("leases/lease_1")["released_at"] is not None
    # Invariant 8: a checkpoint before the park.
    assert task["latest_checkpoint"]
    # The await does not spend an attempt (bounded).
    assert task["attempt_count"] == 0
    assert task["metadata"]["child_await_resumes"] == 1
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"]["attempt_refunded"] is True


def test_past_the_refund_bound_an_await_counts_as_an_attempt(db, worker_factory, routes, agent):
    """§5 F14."""
    write_plan, _ = agent
    write_plan(requests=[_request("one")], **{"await": True})
    seed_attempt(db)
    db.doc("tasks/task_1").setdefault("metadata", {})["child_await_resumes"] = 4
    worker, _, _ = _worker(worker_factory, routes)
    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["attempt_count"] == 1 and task["metadata"]["child_await_resumes"] == 4


def test_an_await_with_no_live_child_is_ignored(db, worker_factory, routes, agent):
    """§5 F15."""
    write_plan, _ = agent
    write_plan(**{"await": True})
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes)
    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def test_a_failed_agent_fails_its_attempt_and_keeps_its_children(db, worker_factory, routes, agent):
    """§3.3 step 8 / F4: no park on a non-zero exit; the children are not touched."""
    write_plan, _ = agent
    write_plan(requests=[_request("one")], exit=3)
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes)
    worker.run()
    task = db.doc("tasks/task_1")
    assert task["state"] != TaskState.PARKED.value
    assert db.doc("tasks/task_child1")["state"] == "READY"


def test_the_resumed_attempt_stages_its_childrens_results_before_the_agent(
    db, store, worker_factory, routes, agent
):
    task = _await_park(db, worker_factory, routes, agent)
    checkpoint = task["latest_checkpoint"]
    # The children end: one succeeded with an artifact in its tenant prefix,
    # one was cancelled by the await deadline.
    key = f"tenants/{TENANT}/tasks/task_child1/attempts/att_c/artifacts/notes.md"
    store.upload_bytes(key, b"helper notes")
    uri = store.uri(key)
    db.doc("tasks/task_child1").update(
        {"state": "SUCCEEDED", "result_summary": {"artifacts": [{"name": "notes.md", "uri": uri, "bytes": 12}]}}
    )
    db.doc("tasks/task_child2").update({"state": "CANCELLED", "end_cause": "child_cascade"})

    write_plan, records = agent
    write_plan()
    # Admission's next lease: attempt_count back to 1 after the refund, and the
    # metadata the park wrote kept (seed_attempt rewrites the document).
    metadata = dict(task["metadata"])
    seed_attempt(db, attempt_id="att_2", lease_id="lease_2", generation=2, attempt_count=1,
                 latest_checkpoint=checkpoint)
    db.doc("tasks/task_1")["metadata"] = metadata
    second, _, _ = _worker(worker_factory, routes, attempt_id="att_2", lease_id="lease_2", generation=2)
    assert second.run() == ExitCode.OK
    seen = records()[-1]
    children = {c["task_id"]: c for c in seen["children_json"]["children"]}
    assert children["task_child1"]["state"] == "SUCCEEDED"
    assert children["task_child1"]["outputs"] == "staged"
    assert children["task_child1"]["request_id"] == "one"
    assert children["task_child2"]["end_cause"] == "child_cascade"
    assert "task_child1/notes.md" in seen["staged"]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


# --------------------------------------------------------------------------
# §3.4: a cascaded child ends CHILD_CASCADE
# --------------------------------------------------------------------------


def test_a_child_cancelled_because_of_its_parent_ends_child_cascade(db, worker_factory):
    seed_attempt(db, cancel_requested=True)
    db.doc("tasks/task_1").setdefault("metadata", {})["child_cascade"] = {
        "why": "parent_cancelled", "parent_task_id": "task_parent",
    }
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.CANCELLED
    assert db.doc("tasks/task_1")["end_cause"] == "child_cascade"


def test_an_ordinary_cancel_stays_cancel_requested(db, worker_factory):
    seed_attempt(db, cancel_requested=True)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.CANCELLED
    assert db.doc("tasks/task_1")["end_cause"] == "cancel_requested"


def test_a_park_answers_every_request_even_when_the_api_is_down(
    db, worker_factory, routes, agent, monkeypatch
):
    """§5 F2/F3: bounded retries, then a retryable refusal; never an unanswered park."""
    write_plan, records = agent
    write_plan(requests=[_request("one")], wait=0.2, **{"await": True})
    calls = {"n": 0}
    real = routes.call

    def down(method, path, *, body=None, headers=None):
        if method == "POST" and path.endswith("/children"):
            calls["n"] += 1
            raise children_mod.ChildApiError(503, "upstream_unavailable", "", retryable=True)
        return real(method, path, body=body, headers=headers)

    routes.call = down
    written: dict[str, dict] = {}
    real_write = children_mod._write_atomic

    def spy(path, document):
        written[path.name] = document
        return real_write(path, document)

    monkeypatch.setattr(children_mod, "_write_atomic", spy)
    seed_attempt(db)
    worker, _, _ = _worker(worker_factory, routes, child_submit_retry_seconds=1)
    rc = worker.run()
    assert calls["n"] >= 2, "a retryable failure is retried within the bound"
    assert written["one.json"]["refused"]["code"] == "api_unavailable"
    assert written["one.json"]["refused"]["retryable"] is True
    # No child was ever made, so nothing is live: the await is ignored.
    assert rc == ExitCode.OK
