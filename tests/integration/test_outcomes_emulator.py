"""GET /v1/outcomes and its drift route, against the Firestore EMULATOR, through the real client.

WHY THIS FILE EXISTS. Every other outcomes test runs on FakeFirestore
(tests/unit/control_plane/test_outcomes_route.py). The fake executes the
queries the module builds, but it is not the client that runs in production, so
none of these had ever been run before merge (PR #196 review):

  * the ended-tasks and arrivals range queries, with their explicit ordering;
  * the attempts query, `tenant_id ==` plus `task_id in`, chunked;
  * `get_all` of the rollup documents, of their shards, and of workflow parents
    by id, where a missing document comes back with `exists` False;
  * `count()` over `completed_at == null`, which the client turns into the
    unary IS_NULL filter before it is sent;
  * the rollup write: one batch of whole-document sets and shard deletes, read
    back as real Timestamps, whose `built_at` must compare equal across a base
    and its shards;
  * the live day's delta, queried from a `built_through` that came back from
    Firestore rather than from Python.

The fixture is the unit test's week, byte for byte in its facts, so every
expected number below is the unit test's number. A difference between the two
files is therefore a difference between the fake and the real client, which is
exactly what this file is here to find.

WHAT THE EMULATOR CANNOT PROVE. It does not enforce composite indexes, so the
`tasks-tenant-completed` index (terraform/modules/firestore/indexes.tf) is proven
only against real Firestore. docs/outcomes.md records what was measured there,
read-only, on 2026-09-25.

ISOLATION. Each test gets its own emulator project id, so the suite's `-n auto`
workers never see each other's documents.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()
if not EMULATOR:
    # CI's integration job starts the emulator and exports this. If it ever
    # stops doing so, this file must not turn into a green step that looked at
    # nothing -- the failure mode .github/workflows/application.yml's comment on
    # that step describes -- so on a runner it is a hard error.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError(
            "FIRESTORE_EMULATOR_HOST is not set in CI; the outcomes emulator tests "
            "would otherwise pass having run nothing"
        )
    pytest.skip("needs the Firestore emulator (FIRESTORE_EMULATOR_HOST)", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402
from google.cloud import firestore  # noqa: E402

from swarm_common.config import Settings  # noqa: E402
from swarm_common.models import Tenant  # noqa: E402

from swarm_api.auth import StaticTokenVerifier  # noqa: E402
from swarm_api.credentials import InMemoryCredentials  # noqa: E402
from swarm_api.deps import build_context  # noqa: E402
from swarm_api.groups import StaticGroups  # noqa: E402
from swarm_api.main import create_app  # noqa: E402
from swarm_api.metrics import ApiMetrics  # noqa: E402
from swarm_api.outcomes import Outcomes, wilson  # noqa: E402
from swarm_api.settings import ApiSettings  # noqa: E402
from swarm_api.waker import NullWaker  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 11, 10, 2, tzinfo=UTC)
WEEK = {"tz": "UTC", "span": "7d"}

PROJECT = "saga-agents-staging"
ENG_GROUP = "eng@saga.xyz"
RESEARCH_GROUP = "research@saga.xyz"
ADMIN_GROUP = "swarm-admins@saga.xyz"
GROUP_MAP = {
    "alice@saga.xyz": (ENG_GROUP,),
    "bob@saga.xyz": (RESEARCH_GROUP,),
    "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
}
TOKENS = {
    f"token-{email.split('@')[0]}": {
        "email": email, "email_verified": True, "sub": f"sub-{email}", "hd": "saga.xyz",
    }
    for email in GROUP_MAP
}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def auth_header(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-{user}"}


def api_settings() -> ApiSettings:
    """The unit suite's settings (tests/unit/control_plane/conftest.py), restated
    because that conftest is a package-relative module this directory cannot
    import."""
    core = Settings(
        project_id=PROJECT,
        region="us-central1",
        environment="test",
        firestore_database="swarm",
        artifact_bucket=f"{PROJECT}-swarm-artifacts",
        allowed_domains=("saga.xyz",),
        max_batch_size=100,
        max_input_bytes=256 * 1024,
        max_workflow_steps=50,
        requests_per_second=20,
        default_tenant_max_active=20,
        default_tenant_capacity_units=40,
        prewarm_max_agents=10,
        prewarm_lead_seconds=120,
    )
    return ApiSettings(
        core=core,
        tenant_groups=(ENG_GROUP, RESEARCH_GROUP),
        admin_groups=(ADMIN_GROUP,),
        group_cache_ttl_seconds=60,
        dispatch_topic="",
        max_page_size=200,
        default_page_size=50,
        rate_limit_burst=200,
    )


# --------------------------------------------------------------------------
# Seeding, through the real client
# --------------------------------------------------------------------------

class Seed:
    """Documents collected by path, then written in batches of at most 500."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def tenant(self, tenant_id: str) -> None:
        tenant = Tenant(
            tenant_id=tenant_id,
            kind="group",
            principal=f"{tenant_id}@saga.xyz",
            created_at=at(1, 0),
            display_name=tenant_id,
            max_active=20,
            capacity_units=40,
            enabled=True,
            credentials=[],
            service_account=f"swarm-agent-worker-{tenant_id}@{PROJECT}.iam.gserviceaccount.com",
            gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{tenant_id}",
            namespace=f"swarm-tenant-{tenant_id}",
        )
        self.docs[f"tenants/{tenant_id}"] = asdict(tenant)
        self.docs[f"pools/tenant:{tenant_id}"] = {
            "name": f"tenant:{tenant_id}", "hard_limit": 20, "adaptive_target": None,
            "quota_derived_limit": None, "active": 0, "enabled": True, "updated_at": at(1, 0),
        }

    def task(
        self,
        task_id: str,
        *,
        state: str,
        created: datetime,
        completed: datetime | None = None,
        tenant: str = "eng",
        profile: str = "mock",
        submitted_by: str = "alice@saga.xyz",
        workflow_id: str | None = None,
        step_id: str | None = None,
        depends_on: tuple[str, ...] = (),
        attempt_count: int = 1,
        last_error: str | None = None,
        cancel_requested: bool = False,
    ) -> None:
        self.docs[f"tasks/{task_id}"] = {
            "id": task_id, "tenant_id": tenant, "created_at": created, "updated_at": created,
            "state": state, "runner_profile": profile, "resource_class": "standard",
            "input": {}, "submitted_by": submitted_by, "provider": None, "model": None,
            "priority": 0, "metadata": {}, "repository_url": None, "repository_ref": None,
            "timeout_seconds": 600, "max_attempts": 3, "attempt_count": attempt_count,
            "next_eligible_at": None, "park_reason": None, "blocked_by": [],
            "current_lease_id": None, "current_generation": 0, "workflow_id": workflow_id,
            "step_id": step_id, "depends_on": list(depends_on),
            "cancel_requested": cancel_requested, "started_at": None,
            "completed_at": completed, "last_error": last_error, "result_summary": None,
            "latest_checkpoint": None,
        }

    def attempt(
        self,
        attempt_id: str,
        *,
        task_id: str,
        generation: int = 1,
        created: datetime,
        started: datetime | None = None,
        exit_code: int | None = None,
        cost: float | None = None,
        tenant: str = "eng",
    ) -> None:
        self.docs[f"attempts/{attempt_id}"] = {
            "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant,
            "generation": generation, "lease_id": f"lease_{attempt_id}",
            "backend": "CLOUD_RUN_JOB", "execution_name": None, "created_at": created,
            "started_at": started, "completed_at": None, "exit_code": exit_code,
            "error": None, "cost_usd": cost,
        }

    def write(self, db: Any) -> None:
        items = list(self.docs.items())
        for start in range(0, len(items), 400):
            batch = db.batch()
            for path, data in items[start:start + 400]:
                batch.set(db.document(path), data)
            batch.commit()


def seed_week(seed: Seed) -> None:
    """tests/unit/control_plane/test_outcomes_route.py `seed_week`, fact for fact."""
    seed.tenant("eng")
    seed.tenant("research")

    # 22 Sep (bucket 3)
    seed.task("s1", state="SUCCEEDED", created=at(21, 10), completed=at(22, 9),
              profile="claude-code", workflow_id="wf1", step_id="a")
    seed.attempt("a_s1", task_id="s1", created=at(22, 8), started=at(22, 8, 1), exit_code=0, cost=1.5)
    seed.task("s2", state="SUCCEEDED", created=at(22, 8), completed=at(22, 12), attempt_count=2)
    seed.attempt("a_s2a", task_id="s2", generation=1, created=at(22, 8, 5), started=at(22, 8, 10),
                 exit_code=75, cost=None)
    seed.attempt("a_s2b", task_id="s2", generation=2, created=at(22, 10, 55), started=at(22, 11),
                 exit_code=0, cost=0.0)
    seed.task("f1", state="FAILED", created=at(22, 9), completed=at(22, 13), profile="claude-code",
              workflow_id="wf1", step_id="b", depends_on=("s1",),
              last_error="runner exceeded its 600s timeout and was killed")
    seed.attempt("a_f1", task_id="f1", created=at(22, 12, 45), started=at(22, 12, 50),
                 exit_code=137, cost=2.25)
    for name in ("c1", "c2", "c3"):
        seed.task(name, state="CANCELLED", created=at(22, 7), completed=at(22, 7, 30),
                  attempt_count=0, cancel_requested=True)
    seed.task("c4", state="CANCELLED", created=at(22, 9), completed=at(22, 13, 5),
              workflow_id="wf1", step_id="c", depends_on=("f1",), attempt_count=0,
              last_error="an upstream workflow step did not succeed")

    # 23 Sep (bucket 4)
    seed.task("d1", state="DEAD_LETTERED", created=at(23, 1), completed=at(23, 2),
              profile="generic", attempt_count=3, submitted_by="bob@saga.xyz")
    seed.attempt("a_d1a", task_id="d1", generation=1, created=at(23, 1, 5), started=at(23, 1, 10),
                 exit_code=1)
    seed.attempt("a_d1b", task_id="d1", generation=2, created=at(23, 1, 35), started=at(23, 1, 40),
                 exit_code=1)
    seed.task("s3", state="SUCCEEDED", created=at(23, 3), completed=at(23, 4))
    seed.attempt("a_s3", task_id="s3", created=at(23, 3, 25), started=at(23, 3, 30), cost=0.0)
    # A step whose parent is research's task: read as unreadable, never used.
    seed.task("x1", state="SUCCEEDED", created=at(23, 5), completed=at(23, 6),
              workflow_id="wf2", step_id="x", depends_on=("r1",))
    seed.attempt("a_x1", task_id="x1", created=at(23, 5, 25), started=at(23, 5, 30), exit_code=0)

    # 24 Sep (bucket 5): only cancels, so no rate
    for name in ("c5", "c6"):
        seed.task(name, state="CANCELLED", created=at(24, 10), completed=at(24, 10, 30),
                  attempt_count=0, last_error="cancelled on request; runner stopped on SIGTERM")
    seed.task("q1", state="RUNNING", created=at(24, 12))

    # 25 Sep (bucket 6, today, in progress)
    seed.task("s4", state="SUCCEEDED", created=at(25, 8), completed=at(25, 9))
    seed.attempt("a_s4", task_id="s4", created=at(25, 8, 25), started=at(25, 8, 30), exit_code=0,
                 cost=0.0)

    # Outside the span
    seed.task("old", state="SUCCEEDED", created=at(10, 10), completed=at(12, 10))
    seed.task("t_nocomp", state="FAILED", created=at(10, 11), completed=None)

    seed.docs["workflows/wf1"] = {
        "workflow_id": "wf1", "tenant_id": "eng", "created_at": at(21, 10),
        "updated_at": at(21, 10), "state": "RUNNING", "submitted_by": "alice@saga.xyz",
        "steps": [
            {"step_id": "a", "runner_profile": "claude-code", "input": {}, "depends_on": [],
             "task_id": "s1"},
            {"step_id": "b", "runner_profile": "claude-code", "input": {}, "depends_on": ["a"],
             "task_id": "f1"},
            {"step_id": "c", "runner_profile": "mock", "input": {}, "depends_on": ["b"],
             "task_id": "c4"},
        ],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False,
    }

    # research, on the same days
    seed.task("r1", tenant="research", state="SUCCEEDED", created=at(22, 8), completed=at(22, 10),
              submitted_by="bob@saga.xyz")
    seed.task("r2", tenant="research", state="FAILED", created=at(22, 8, 30), completed=at(22, 11),
              submitted_by="bob@saga.xyz", last_error="boom")
    seed.attempt("a_r2", task_id="r2", tenant="research", created=at(22, 10, 30),
                 started=at(22, 10, 31), exit_code=1)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def db() -> Any:
    # A project of its own per test: the emulator keys every document by
    # project, so parallel workers cannot see each other's tenants or days.
    return firestore.Client(project=f"outcomes-{uuid.uuid4().hex[:12]}", database="(default)")


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _context(db: Any, clock: Clock) -> Any:
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(TOKENS),
        groups=StaticGroups(GROUP_MAP),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        now=clock,
    )


@pytest.fixture
def ctx(db, clock):
    seed = Seed()
    seed_week(seed)
    seed.write(db)
    return _context(db, clock)


@pytest.fixture
def api(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def ok(api: TestClient, user: str, **params: Any) -> dict[str, Any]:
    response = api.get("/v1/outcomes", params=params, headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def rollup_ids(db: Any) -> list[str]:
    return sorted(snap.id for snap in db.collection("outcome_days").stream())


def rollup_doc(db: Any, doc_id: str) -> dict[str, Any]:
    snap = db.collection("outcome_days").document(doc_id).get()
    assert snap.exists, doc_id
    return snap.to_dict()


# --------------------------------------------------------------------------
# The week, through the real client: the unit test's numbers, exactly
# --------------------------------------------------------------------------

def test_the_week_reads_the_same_through_the_real_client(api):
    body = ok(api, "alice", **WEEK)
    assert body["scope"] == {"kind": "tenant", "tenant_id": "eng"}
    assert body["since"] == "2026-09-19T00:00:00+00:00"
    assert body["until"] == "2026-09-25T11:10:02+00:00"

    buckets = body["buckets"]
    assert [b["start"][:10] for b in buckets] == [f"2026-09-{d}" for d in range(19, 26)]
    assert [b["state"] for b in buckets] == ["sealed"] * 6 + ["open"]
    for early in buckets[:3]:
        assert early["ended"] == 0 and early["rate"] is None, "a measured zero, not a gap"

    sep22 = buckets[3]
    assert (sep22["succeeded"], sep22["failed"], sep22["dead_lettered"]) == (2, 1, 0)
    assert sep22["cancelled"] == {
        "total": 4, "requested": 3, "after_failure": 1, "workflow_sweep": 0, "other": 0,
    }
    assert sep22["rate"] == wilson(2, 3)
    assert sep22["failure_classes"]["timeout"] == 1
    assert sep22["cost"] == {"sum_usd": 3.75, "attempts": 4, "reporting": 3}

    sep23 = buckets[4]
    assert (sep23["succeeded"], sep23["failed"], sep23["dead_lettered"]) == (2, 0, 1)
    assert sep23["failure_classes"]["no_reason"] == 1
    assert sep23["cost"] == {"sum_usd": 0.0, "attempts": 4, "reporting": 1}

    assert buckets[5]["ended"] == 2 and buckets[5]["rate"] is None
    assert buckets[6]["succeeded"] == 1 and buckets[6]["in_progress"] is True

    # The throughput lane, by created_at: s1 arrived on 21 Sep, ended on 22 Sep.
    assert [b["submitted"] for b in buckets] == [0, 0, 1, 6, 3, 3, 1]

    totals = body["totals"]
    assert totals["complete"] is True
    assert (totals["succeeded"], totals["failed"], totals["dead_lettered"]) == (5, 1, 1)
    assert totals["cancelled"]["total"] == 6 and totals["ended"] == 13
    assert totals["rate"] == wilson(5, 7)
    cost = totals["cost"]
    assert (cost["sum_usd"], cost["attempts"], cost["reporting"]) == (3.75, 9, 5)
    assert cost["retries"] == {"sum_usd": 0.0, "attempts": 2, "reporting": 1}

    retries = body["retries"]
    assert retries["needed_retry"] == {"k": 2, "of": 7}
    assert retries["not_final"]["by_exit"] == [
        {"exit_code": 1, "label": "failed", "n": 1},
        {"exit_code": 75, "label": "parked", "n": 1},
    ]
    assert retries["admissions_without_attempt_doc"] == 1

    # f1 became eligible when its parent s1 finished: a parent read by get_all.
    claude = next(r for r in body["latency"]["by_profile"] if r["runner_profile"] == "claude-code")
    assert claude["failed"]["wait"]["values_s"] == [13800.0]

    coverage = body["coverage"]
    assert coverage["days"] == {"total": 7, "sealed": 6, "live": 1, "unread": 0}
    assert coverage["derived_now"] == 7
    assert coverage["built_through"] == "2026-09-25T11:08:02Z"
    # x1's parent is research's task: the tenant-checked get_all reads it as absent.
    assert coverage["wait_excluded"] == 1
    # The real aggregation over `completed_at == null` (IS_NULL on the wire).
    assert coverage["terminal_without_completed_at"] == 1


def test_workflows_that_failed_go_through_the_real_rollup_path(api, db):
    block = ok(api, "alice", **WEEK)["workflows_failed"]
    assert (block["with_ended_steps"], block["with_failed_steps"]) == (2, 1)
    row = block["rows"][0]
    assert row["workflow_id"] == "wf1"
    assert row["first_failed"]["step_id"] == "b" and row["first_failed"]["failure_class"] == "timeout"
    assert row["state"] == "FAILED"
    assert row["steps"] == {
        "total": 3, "succeeded": 1, "failed": 1, "dead_lettered": 0, "cancelled": 1,
        "open": 0, "unreadable": 0,
    }
    assert db.collection("workflows").document("wf1").get().to_dict()["state"] == "FAILED"


# --------------------------------------------------------------------------
# The rollup documents, written and read back by the real client
# --------------------------------------------------------------------------

def test_the_rollup_is_written_once_and_reused_through_the_real_client(api, db, clock):
    first = ok(api, "alice", **WEEK)
    ids = rollup_ids(db)
    assert ids == [f"eng_2026-09-{d}" for d in range(19, 26)], "tenant scope writes only eng's days"
    assert rollup_doc(db, "eng_2026-09-24")["sealed"] is True
    live = rollup_doc(db, "eng_2026-09-25")
    assert live["sealed"] is False
    # A Timestamp on the way back, and the same instant.
    assert live["built_through"] == NOW - timedelta(seconds=120)

    cached = ok(api, "alice", **WEEK)
    assert cached["cached"] is True and cached["generated_at"] == first["generated_at"]

    clock.now = NOW + timedelta(minutes=2)
    again = ok(api, "alice", **WEEK)
    assert again["cached"] is False
    assert again["coverage"]["derived_now"] == 0, "every day came back from its document"
    assert again["reads"] < first["reads"]
    assert again["totals"] == first["totals"]
    assert again["buckets"][:6] == first["buckets"][:6]


def test_the_live_day_folds_in_a_delta_queried_from_a_stored_cut(api, db, clock):
    ok(api, "alice", **WEEK)
    seed = Seed()
    seed.task("s5", state="SUCCEEDED", created=at(25, 11, 9), completed=at(25, 11, 11))
    seed.write(db)

    clock.now = NOW + timedelta(minutes=2)
    body = ok(api, "alice", **WEEK)
    assert body["coverage"]["derived_now"] == 0
    assert body["buckets"][6]["succeeded"] == 2 and body["buckets"][6]["submitted"] == 2
    rewritten = rollup_doc(db, "eng_2026-09-25")
    assert rewritten["n_ended"] == 2
    assert rewritten["built_at"] == NOW + timedelta(minutes=2)


def test_a_large_day_is_sharded_in_one_batch_and_read_back_whole(ctx, db, clock):
    ctx.outcomes = Outcomes(
        store=ctx.store, rollups=ctx.rollups, metrics=ctx.metrics, now=clock,
        shard_bytes=900, max_shards=11,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    first = ok(client, "alice", **WEEK)
    base = rollup_doc(db, "eng_2026-09-22")
    assert base["shards"] >= 1
    shards = [rollup_doc(db, f"eng_2026-09-22_s{n}") for n in range(1, base["shards"] + 1)]
    assert all(shard["built_at"] == base["built_at"] for shard in shards)

    clock.now = NOW + timedelta(minutes=2)
    second = ok(client, "alice", **WEEK)
    assert second["coverage"]["derived_now"] == 0
    assert second["buckets"][3] == first["buckets"][3]


def test_the_drift_route_reports_then_repairs_on_the_real_client(api, db, clock):
    ok(api, "alice", **WEEK)
    db.collection("tasks").document("s3").update({"state": "FAILED"})  # under a sealed day

    clock.now = NOW + timedelta(minutes=2)
    assert ok(api, "alice", **WEEK)["buckets"][4]["succeeded"] == 2, "sealed doc served as written"

    report = api.post(
        "/v1/admin/outcomes/rollup",
        params={"tenant_id": "eng", "since": "2026-09-19", "until": "2026-09-25"},
        headers=auth_header("root"),
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["report"]["examined"] == 6
    # Five stored sealed days re-derive to exactly what was written: the JSON
    # round trip through a real document loses nothing the drift check compares.
    assert body["report"]["agreed"] == 5 and body["report"]["disagreed"] == 1
    assert body["report"]["unknown"] == 0 and body["report"]["repaired"] == 0
    assert [(d["day"], d["changed_ids"], d["repaired"]) for d in body["drifted"]] == [
        ("2026-09-23", ["s3"], False)
    ]

    repaired = api.post(
        "/v1/admin/outcomes/rollup",
        params={"tenant_id": "eng", "since": "2026-09-23", "until": "2026-09-24", "repair": "true"},
        headers=auth_header("root"),
    ).json()
    assert repaired["report"]["repaired"] == 1

    clock.now = NOW + timedelta(minutes=4)
    after = ok(api, "alice", **WEEK)["buckets"][4]
    assert (after["succeeded"], after["failed"], after["dead_lettered"]) == (1, 1, 1)


# --------------------------------------------------------------------------
# Platform scope, and a day with more ended tasks than one `in` may name
# --------------------------------------------------------------------------

def test_platform_scope_lists_tenants_through_the_real_client(api, db):
    body = ok(api, "root", scope="platform", **WEEK)
    assert body["scope"] == {
        "kind": "platform", "tenants": ["eng", "research"], "excluded": [],
        "tenants_complete": True,
    }
    sep22 = body["buckets"][3]
    assert (sep22["succeeded"], sep22["failed"]) == (3, 2)
    assert (body["totals"]["succeeded"], body["totals"]["failed"]) == (6, 2)
    assert body["totals"]["rate"] == wilson(6, 9)
    # An admin's whole-platform view drops the tenant filter from the count.
    assert body["coverage"]["terminal_without_completed_at"] == 1
    assert any(i.startswith("research_") for i in rollup_ids(db))


def test_a_day_of_more_than_thirty_ended_tasks_reads_every_attempt(db, clock):
    """Real Firestore refuses an `in` over 30 values -- measured read-only on dev,
    2026-09-25: "'IN' supports up to 30 comparison values." -- so the attempts
    of a busy day must be read in chunks of at most 30. 35 tasks each with one
    reporting attempt: every attempt found, none admitted without its document."""
    seed = Seed()
    seed.tenant("eng")
    for i in range(35):
        seed.task(f"b{i:02d}", state="SUCCEEDED", created=at(24, 9), completed=at(24, 10, i))
        seed.attempt(f"a_b{i:02d}", task_id=f"b{i:02d}", created=at(24, 9, 30),
                     started=at(24, 9, 31), exit_code=0, cost=0.25)
    seed.write(db)
    client = TestClient(create_app(_context(db, clock)), raise_server_exceptions=False)

    body = ok(client, "alice", **WEEK)
    day = body["buckets"][5]
    assert day["state"] == "sealed" and day["unread_reason"] is None
    assert day["succeeded"] == 35
    assert day["cost"] == {"sum_usd": 8.75, "attempts": 35, "reporting": 35}
    assert body["retries"]["admissions_without_attempt_doc"] == 0
