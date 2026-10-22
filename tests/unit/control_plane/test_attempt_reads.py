"""An agent's reads of its own tenant's state (docs/design/agent-autonomy.md §3.4,
§3.5's route; lane AU-4 STATE-READS, part of #1933).

Drives the real route, `GET /v1/attempts/{attempt_id}/reads/{tool}`, over the
in-memory Firestore and the in-memory bucket, signed as the worker signs: the
attempt key registered at `state/open`, `swarm-state-req/v1` over the path
WITH its query (the restatement in `test_attemptstate`). Each test names the
rule of the row it holds:

  * another tenant's agent id answers exactly as a missing one;
  * a stale generation is `attempt_fenced`;
  * the tenant comes from the lease, not the request;
  * a log tail is redacted;
  * `logs_query` with no view configured is `logs_unavailable`;
  * a filter a caller sends cannot name another namespace;
  * every tool is read-only.

Offline: no credentials, no emulator. Every fake credential is built at
runtime (`secrets.token_hex`), never one literal.
"""

from __future__ import annotations

import copy
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import attemptreads
from swarm_api.attemptreads import ARGUMENTS, TOOLS, Execution, logs_filter
from swarm_api.ratelimit import TokenBucketLimiter

from .conftest import seed_task, seed_tenant
from .test_attemptstate import (  # noqa: F401 - fixtures
    ATTEMPT,
    LEASE,
    TASK,
    Worker,
    client,
    opened,
    worker_tokens,
)

ROUTE = "/v1/attempts/{attempt_id}/reads/{tool}"
VIEW = "projects/swarm-test-project/locations/global/buckets/_Default/views/swarm-workers"
NOW = datetime.now(timezone.utc)
#: The agents an agent reads: one of its own tenant's, one of another's.
MINE = "task_mine_failed"
THEIRS = "task_theirs_failed"
MISSING = "task_nobody_has"
WORKFLOW_MINE = "wf_mine"
WORKFLOW_THEIRS = "wf_theirs"
RUN_MINE = "run_mine"
RUN_THEIRS = "run_theirs"


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    """Each test its own budget: the module's is shared by every request."""
    monkeypatch.setattr(attemptreads, "_BUDGET", TokenBucketLimiter(120 / 3600.0, 120))
    monkeypatch.delenv(attemptreads.WORKER_LOG_VIEW_ENV, raising=False)


class FakeLogs:
    """Cloud Logging, as `CloudLogsReader.entries` sees it: what it was asked."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.entries_out = entries or []

    def entries(self, view: str, filter_text: str, *, limit: int) -> list[dict[str, Any]]:
        self.calls.append((view, filter_text))
        return list(self.entries_out)


@pytest.fixture
def logs(monkeypatch) -> FakeLogs:
    fake = FakeLogs()
    monkeypatch.setattr(attemptreads.CloudLogsReader, "entries",
                        lambda self, view, filter_text, *, limit: fake.entries(
                            view, filter_text, limit=limit))
    return fake


def read(client, worker: Worker, tool: str, args: str = "", *, sign: bool = True,
         token: str | None = None, headers: dict[str, str] | None = None) -> Any:
    path = ROUTE.format(attempt_id=worker.tuple.attempt_id, tool=tool)
    query = worker.query() + (f"&{args}" if args else "")
    sent = {"Authorization": f"Bearer {token or worker.token}", **(headers or {})}
    if sign:
        sent.update(worker.sign("GET", f"{path}?{query}", b""))
    return client.get(f"{path}?{query}", headers=sent)


def _attempt(db, attempt_id: str, tenant: str, task_id: str, *, backend: str,
             execution: str, error: str | None = None) -> None:
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant, "generation": 1,
        "lease_id": f"lease_{attempt_id}", "backend": backend, "execution_name": execution,
        "created_at": NOW - timedelta(minutes=30), "started_at": NOW - timedelta(minutes=29),
        "completed_at": NOW - timedelta(minutes=5), "exit_code": 1, "error": error,
        "peak_rss_bytes": 2048, "oom_near_miss": True, "checkpoints": [],
    }


def _event(db, task_id: str, tenant: str, event_id: str, kind: str, minutes_ago: int) -> None:
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id, "task_id": task_id, "tenant_id": tenant, "type": kind,
        "at": NOW - timedelta(minutes=minutes_ago), "generation": 1, "detail": {"n": event_id},
    }


def _failed(db, task_id: str, tenant: str, *, error: str = "boom", workflow: str | None = None,
            backend: str = "CLOUD_RUN_JOB", execution: str | None = None) -> dict[str, Any]:
    seed_tenant(db, tenant)
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state="FAILED",
                    created_at=NOW - timedelta(hours=1), workflow_id=workflow)
    doc.update({"last_error": error, "completed_at": NOW - timedelta(minutes=5),
                "end_cause": "crash", "attempt_count": 1,
                "result_summary": {"artifacts": [], "agent_streams": {}}})
    attempt_id = f"att_{task_id}"
    _attempt(db, attempt_id, tenant, task_id, backend=backend,
             execution=execution or f"swarm-job-{tenant}-mock-{task_id[-4:]}")
    _event(db, task_id, tenant, f"evt_{task_id}_1", "heartbeat", 20)
    _event(db, task_id, tenant, f"evt_{task_id}_2", "failed", 5)
    return doc


def _world(db) -> None:
    """One failed agent, workflow and issue run in `eng` (the reader's tenant),
    and the same in `research`, whose ids an `eng` agent must not learn of."""
    for tenant, task_id, wf, run in (("eng", MINE, WORKFLOW_MINE, RUN_MINE),
                                     ("research", THEIRS, WORKFLOW_THEIRS, RUN_THEIRS)):
        _failed(db, task_id, tenant, workflow=wf)
        db.docs[f"workflows/{wf}"] = {
            "workflow_id": wf, "tenant_id": tenant, "state": "FAILED",
            "submitted_by": "alice@example.com", "created_at": NOW - timedelta(hours=1),
            "updated_at": NOW, "steps": [{"step_id": "only", "runner_profile": "mock",
                                          "input": {}, "task_id": task_id}],
        }
        db.docs[f"issue_runs/{run}"] = {
            "id": run, "tenant_id": tenant, "state": "FAILED", "created_by": "alice@example.com",
            "created_at": NOW - timedelta(hours=1),
            "issue": {"owner": "acme", "repo": "widgets", "number": 7}, "workflow_id": wf,
            "history": [{"at": NOW, "to": "FAILED"}],
        }


#: One well-formed argument string per tool, naming the reader's own records.
GOOD_ARGS = {
    "agents_list": "state=FAILED&window_hours=7",
    "agent_view": f"agent_id={MINE}",
    "agent_events": f"agent_id={MINE}",
    "agent_log_tail": f"agent_id={MINE}&stream=agent_stdout",
    "agent_artifacts": f"agent_id={MINE}",
    "workflow_view": f"workflow_id={WORKFLOW_MINE}",
    "run_view_sc": f"run_id={RUN_MINE}",
    "heartbeats": f"agent_id={MINE}",
    "logs_query": f"agent_id={MINE}&severity=ERROR",
}
#: The same, naming an id of another tenant -- and naming nothing at all.
FOREIGN = {
    "agent_view": ("agent_id", THEIRS),
    "agent_events": ("agent_id", THEIRS),
    "agent_log_tail": ("agent_id", THEIRS),
    "agent_artifacts": ("agent_id", THEIRS),
    "heartbeats": ("agent_id", THEIRS),
    "logs_query": ("agent_id", THEIRS),
    "workflow_view": ("workflow_id", WORKFLOW_THEIRS),
    "run_view_sc": ("run_id", RUN_THEIRS),
}


def test_the_vocabulary_is_the_rows():
    # MUTATION: dropping or renaming any tool in attemptreads.ARGUMENTS turns this red.
    assert set(TOOLS) == {"agents_list", "agent_view", "agent_events", "agent_log_tail",
                          "agent_artifacts", "workflow_view", "run_view_sc", "heartbeats",
                          "logs_query"}
    for tool in TOOLS:
        # MUTATION: adding a `tenant_id`, `namespace` or `filter` argument to any tool turns this red.
        assert not ARGUMENTS[tool] & {"tenant_id", "tenant", "namespace", "filter", "resource"}


def test_another_tenants_agent_answers_exactly_as_a_missing_one(client, db, monkeypatch, logs):
    monkeypatch.setenv(attemptreads.WORKER_LOG_VIEW_ENV, VIEW)
    # With the store's own cross-tenant refusal switched ON, which names that
    # the workflow exists elsewhere: the read must still answer as missing.
    monkeypatch.setenv("REFUSAL_WORKFLOW_NOT_IN_TENANT", "on")
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    for tool, (name, foreign) in FOREIGN.items():
        theirs = read(client, worker, tool, f"{name}={foreign}")
        missing = read(client, worker, tool, f"{name}={MISSING}")
        # MUTATION: answering another tenant's id with 403 (or any status but 404) turns this red.
        assert theirs.status_code == missing.status_code == 404, (tool, theirs.text)
        # MUTATION: a message that says the id exists elsewhere (Store.get_workflow's
        # `workflow_not_in_tenant`) turns this red.
        assert theirs.json() == {**missing.json(), "message":
                                 missing.json()["message"].replace(MISSING, foreign)}, tool
        assert "research" not in theirs.text, tool
    # MUTATION: a listing that is not scoped to the lease's tenant serves THEIRS and turns this red.
    listed = read(client, worker, "agents_list", "state=FAILED")
    assert listed.status_code == 200, listed.text
    assert [a["id"] for a in listed.json()["result"]["agents"]] == [MINE]
    # MUTATION: dropping the tenant check on the run behind `run_id` turns this red.
    assert read(client, worker, "agents_list", f"run_id={RUN_THEIRS}").status_code == 404
    assert logs.calls == [], "no other tenant's agent may reach Cloud Logging"


def test_a_stale_generation_is_attempt_fenced(client, db):
    worker = Worker()
    task = opened(client, db, worker)
    _world(db)
    task["current_generation"] = 2
    db.docs[f"leases/{LEASE}"]["generation"] = 2
    for tool in TOOLS:
        response = read(client, worker, tool, GOOD_ARGS[tool])
        # MUTATION: removing the fence read (`_tenant_of_lease`) before a handler turns this red.
        assert response.status_code == 409, (tool, response.text)
        assert response.json()["code"] == "attempt_fenced", tool
    # A released lease is the same answer.
    task["current_generation"] = 1
    db.docs[f"leases/{LEASE}"].update({"generation": 1, "released_at": NOW})
    # MUTATION: a fence that ignores `released_at` turns this red.
    assert read(client, worker, "agent_view", GOOD_ARGS["agent_view"]).json()["code"] \
        == "attempt_fenced"


def test_the_tenant_comes_from_the_lease_not_the_request(client, db):
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    # A header naming another workspace is not a scope here.
    response = read(client, worker, "agents_list", "state=FAILED",
                    headers={"X-Swarm-Tenant": "research"})
    assert response.status_code == 200, response.text
    # MUTATION: scoping the reads by anything but the lease's tenant serves THEIRS and turns this red.
    assert response.json()["tenant_id"] == db.docs[f"leases/{LEASE}"]["tenant_id"] == "eng"
    assert [a["id"] for a in response.json()["result"]["agents"]] == [MINE]
    # A second `tenant_id` in the query is refused, never read as a tool's.
    # MUTATION: letting a repeated tuple name through (`seen_tuple`) turns this red.
    assert read(client, worker, "agents_list", "tenant_id=research").status_code == 422
    # A tool argument that names a tenant is not a tool argument.
    # MUTATION: accepting an argument the tool does not declare turns this red.
    assert read(client, worker, "agent_view", f"agent_id={THEIRS}&tenant=research").status_code \
        == 422
    # A lease that is another tenant's is the fence, whatever the tuple says.
    db.docs[f"leases/{LEASE}"]["tenant_id"] = "research"
    fenced = read(client, worker, "agents_list", "state=FAILED")
    # MUTATION: taking the tenant from the tuple without checking the lease's turns this red.
    assert fenced.status_code == 409 and fenced.json()["code"] == "attempt_fenced"
    assert THEIRS not in fenced.text


def test_a_log_tail_is_redacted(client, db, objects):
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    leaked = "ghp_" + secrets.token_hex(18)
    base = f"tenants/eng/tasks/{MINE}/attempts/att_{MINE}/logs"
    objects.put(f"{base}/agent_stdout.log", f"cloning...\nusing token {leaked} to push\ndone\n")
    db.docs[f"tasks/{MINE}"]["last_error"] = f"push failed with {leaked}"
    tail = read(client, worker, "agent_log_tail", GOOD_ARGS["agent_log_tail"])
    assert tail.status_code == 200, tail.text
    body = tail.json()["result"]
    assert body["status"] == "ok" and "cloning" in body["content"]
    # MUTATION: serving the log window without read-time redaction turns this red.
    assert leaked not in tail.text
    view = read(client, worker, "agent_view", GOOD_ARGS["agent_view"])
    assert view.status_code == 200, view.text
    # MUTATION: dropping the JsonMasker pass over every answer serves `last_error` raw and turns this red.
    assert leaked not in view.text
    assert view.json()["result"]["agent"]["attempts"][0]["oom_near_miss"] is True


def test_a_long_log_is_served_as_its_last_64_kib(client, db, objects):
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    base = f"tenants/eng/tasks/{MINE}/attempts/att_{MINE}/logs"
    head = "early line\n" * 2000
    tail_text = "the last words\n" * 6000
    objects.put(f"{base}/agent_stdout.log", head + tail_text)
    body = read(client, worker, "agent_log_tail", GOOD_ARGS["agent_log_tail"]).json()["result"]
    # MUTATION: serving the head of the log instead of its tail turns this red.
    assert body["content"].rstrip().endswith("the last words")
    assert "early line" not in body["content"] and body["from_start"] is False
    assert len(body["content"].encode()) <= attemptreads.LOG_TAIL_BYTES


def test_logs_query_with_no_view_is_logs_unavailable(client, db, logs):
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    response = read(client, worker, "logs_query", GOOD_ARGS["logs_query"])
    # MUTATION: querying Cloud Logging project-wide when no view is configured turns this red.
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "logs_unavailable"
    assert attemptreads.WORKER_LOG_VIEW_ENV in response.json()["message"]
    assert logs.calls == []


def test_logs_query_reads_through_the_view_with_the_servers_filter(client, db, monkeypatch, logs):
    monkeypatch.setenv(attemptreads.WORKER_LOG_VIEW_ENV, VIEW)
    leaked = "ghp_" + secrets.token_hex(18)
    logs.entries_out = [{"timestamp": NOW.isoformat(), "severity": "ERROR",
                         "resource": {"type": "cloud_run_job"},
                         "textPayload": f"git push with {leaked} failed"}]
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    response = read(client, worker, "logs_query", GOOD_ARGS["logs_query"])
    assert response.status_code == 200, response.text
    ((view, filter_text),) = logs.calls
    assert view == VIEW
    # MUTATION: a filter not built from the agent's own executions turns this red.
    assert f'"swarm-job-eng-mock-{MINE[-4:]}"' in filter_text and "severity>=ERROR" in filter_text
    # MUTATION: serving a Cloud Logging line without the masker turns this red.
    assert leaked not in response.text
    assert response.json()["result"]["entries"][0]["severity"] == "ERROR"


def test_a_filter_a_caller_sends_cannot_name_another_namespace(client, db, monkeypatch, logs):
    monkeypatch.setenv(attemptreads.WORKER_LOG_VIEW_ENV, VIEW)
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    db.docs[f"attempts/att_{MINE}"].update({"backend": "GKE_AUTOPILOT",
                                            "execution_name": "swarm-eng-mock-abc12"})
    for extra in ("filter=resource.labels.namespace_name%3D%22swarm-tenant-research%22",
                  "namespace=swarm-tenant-research", "resource=k8s_container",
                  "query=anything"):
        refused = read(client, worker, "logs_query", f"{GOOD_ARGS['logs_query']}&{extra}")
        # MUTATION: ignoring (rather than refusing) an undeclared argument turns this red.
        assert refused.status_code == 422, (extra, refused.text)
    assert logs.calls == []
    assert read(client, worker, "logs_query", GOOD_ARGS["logs_query"]).status_code == 200
    ((_, filter_text),) = logs.calls
    # MUTATION: deriving the namespace from anything but the lease's tenant turns this red.
    assert 'resource.labels.namespace_name="swarm-tenant-eng"' in filter_text
    assert "research" not in filter_text
    # A window outside the agent's life, or longer than a day, is refused before any read.
    born = (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    # MUTATION: dropping the life bound on `since` turns this red.
    assert read(client, worker, "logs_query",
                f"{GOOD_ARGS['logs_query']}&since={born}").status_code == 422
    assert len(logs.calls) == 1


def test_an_execution_name_never_reaches_the_filter_as_text():
    since, until = NOW - timedelta(hours=1), NOW
    hostile = Execution("GKE_AUTOPILOT", 'x" OR resource.labels.namespace_name="swarm-tenant-b')
    # MUTATION: dropping the `_NAME` check on an execution name turns this red.
    assert logs_filter("eng", [hostile], severity="DEFAULT", since=since, until=until) == ""
    text = logs_filter("eng", [Execution("GKE_AUTOPILOT", "swarm-eng-mock-1")],
                       severity="DEFAULT", since=since, until=until)
    assert text.count("namespace_name=") == 1 and "swarm-tenant-eng" in text


def test_every_tool_is_read_only(client, db, objects, monkeypatch, logs):
    monkeypatch.setenv(attemptreads.WORKER_LOG_VIEW_ENV, VIEW)
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    objects.put(f"tenants/eng/tasks/{MINE}/attempts/att_{MINE}/logs/agent_stdout.log", "x\n")
    # MUTATION: mounting any method but GET on the read route turns this red.
    from swarm_api.routes.attemptreads import router

    methods = {m for r in router.routes if getattr(r, "path", "") == ROUTE
               for m in getattr(r, "methods", ())}
    assert methods == {"GET"}
    path = ROUTE.format(attempt_id=ATTEMPT, tool="agent_view")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, f"{path}?{worker.query()}").status_code == 405, method
    before_docs = copy.deepcopy(db.docs)
    before_objects = dict(objects.objects)
    for tool in TOOLS:
        response = read(client, worker, tool, GOOD_ARGS[tool])
        assert response.status_code == 200, (tool, response.text)
        assert response.json()["result"] is not None, tool
    # MUTATION: a read that advances an issue run (`routes.runs.advance_run`) or writes
    # anything else turns this red.
    assert db.docs == before_docs
    assert dict(objects.objects) == before_objects


def test_an_unproven_or_foreign_caller_reads_nothing(client, db):
    from .test_attemptstate import OTHER_WORKER

    worker = Worker()
    opened(client, db, worker)
    _world(db)
    # MUTATION: skipping `AttemptState.call_get`'s proof check turns this red.
    assert read(client, worker, "agent_view", GOOD_ARGS["agent_view"], sign=False).status_code \
        == 403
    # MUTATION: skipping the worker-token check turns this red.
    assert read(client, worker, "agent_view", GOOD_ARGS["agent_view"],
                token=OTHER_WORKER).status_code == 401


def test_the_budget_is_per_attempt_and_hourly(client, db, monkeypatch):
    monkeypatch.setattr(attemptreads, "_BUDGET", TokenBucketLimiter(2 / 3600.0, 2))
    worker = Worker()
    opened(client, db, worker)
    _world(db)
    for _ in range(2):
        assert read(client, worker, "agent_view", GOOD_ARGS["agent_view"]).status_code == 200
    # MUTATION: removing the budget check turns this red.
    assert read(client, worker, "agent_view", GOOD_ARGS["agent_view"]).status_code == 429
