"""The reconciler must read GKE without cluster-scope rights, and act on what it reads.

INCIDENT wf_ebb3ab2d65664707a559, 2026-09-24. Five browser tasks sat in
DISPATCHED for hours with `cancel_requested=true`, their five leases holding 10
units on every one of seven pools, and the workflow read RUNNING throughout. Their
pods had died at 03:55Z and their Jobs were deleted by TTL at 04:55Z. The one
component able to release a dead GKE task's lease is the reconciler, and on
every pass it logged the same thing:

    jobs.batch is forbidden: User "108023754768362642341" cannot list resource
    "jobs" in API group "batch" at the cluster scope

`GkeBackend.list_executions` called `list_job_for_all_namespaces`, a
CLUSTER-scope list. The reconciler's only Kubernetes grant is the namespaced
`swarm-reaper` Role, and no ClusterRole exists anywhere by policy
(kubernetes/rbac/worker-rbac.yaml). So GKE read as unreadable on every pass,
`_is_actionable` correctly refused to conclude that anything on it was dead,
and the stale_lease + missing_execution findings for every stranded lease were
dropped -- while the pass reported `findings=0`, which is also what a healthy
pass reports.

WHAT THIS FILE PINS, each against the real `Reconciler`, the real
`ControlStore` and the real `firestore.transactional` over the in-memory
Firestore, so a released lease is the frozen `release_lease_in_transaction`
actually returning units to every pool (invariant 2):

  F-1  namespaces come from the control plane and are read one by one; nothing
       cluster-scoped is ever called; readability is per namespace, judged at
       the attempt's OWN namespace; a tenant's namespace is named by the
       dispatcher's own rule, 63-character truncation and hash included.
  F-3  a requested cancel is finished as CANCELLED in the same pass, never
       READY, and never downgraded to FAILED by spent attempts.
  F-4  when a namespace cannot be listed, the attempt's Job is read by name:
       404 and a terminal condition release; 403 proves nothing; an active
       Job is killed first -- but ONLY where the list path would kill it too,
       i.e. under a lease that is stale by the ordinary rule. An active Job
       under a heartbeating lease disproves the finding and is left alone.
  F-5  a held-back finding is counted as suppressed, and each log line the
       alert policy counts is the one the code writes, compared against the
       metric's parsed FILTER rather than anywhere in the file.

The fake Kubernetes API below models the reconciler's REAL grant rather than a
permissive one. That is the whole point: a fake that answered the cluster-scope
list would have let the old code pass every one of these.
"""

from __future__ import annotations

import io
import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from swarm_common.models import Tenant, pool_names_for, utcnow

from reconciler import repair
from reconciler.backends import GkeBackend
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import JobResourceView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from scheduler.dispatch import GkeJobDispatcher, GkeTarget

from .conftest import scheduler_settings
from .fakes import FakeFirestore

REPO = Path(__file__).resolve().parents[3]

#: DERIVED, never spelled again in this file -- the prefix the reconciler is
#: configured with, which scripts/lib/check-contract-parity.sh section 6 holds
#: to the dispatcher's template. A fixture restating it would agree with any
#: bug in it.
NS = ReconcilerConfig.namespace_prefix
ENG = "eng"
ENG_NS = f"{NS}{ENG}"
#: The reconciler GSA's numeric uniqueId, as GKE named it in the live 403.
RECONCILER_UID = "108023754768362642341"

GKE = "GKE_AUTOPILOT"
CLOUD_RUN = "CLOUD_RUN_JOB"


# ---------------------------------------------------------------------------
# The Kubernetes API as the reconciler's identity actually experiences it
# ---------------------------------------------------------------------------


def forbidden(verb: str, namespace: str | None = None) -> Exception:
    """The ApiException GKE returns, shaped like the live one."""
    from kubernetes.client.rest import ApiException

    where = f'in the namespace "{namespace}"' if namespace else "at the cluster scope"
    exc = ApiException(status=403, reason="Forbidden")
    exc.body = (
        f'jobs.batch is forbidden: User "{RECONCILER_UID}" cannot {verb} resource "jobs" '
        f'in API group "batch" {where}'
    )
    return exc


def not_found(name: str) -> Exception:
    from kubernetes.client.rest import ApiException

    return ApiException(status=404, reason="Not Found")


def api_error(status: int) -> Exception:
    """A LIST that fails for a reason other than RBAC: APF, a blip, a timeout."""
    from kubernetes.client.rest import ApiException

    reasons = {429: "Too Many Requests", 500: "Internal Server Error", 504: "Gateway Timeout"}
    return ApiException(status=status, reason=reasons.get(status, "Error"))


class RbacBatchApi:
    """BatchV1Api under the `swarm-reaper` Role and nothing else.

    `listable`/`gettable`/`deletable` are the namespaces where the Role is bound
    AND the namespace exists. Everywhere else answers 403, because Kubernetes
    authorises before it resolves: a missing namespace and a missing
    RoleBinding look identical. Every cluster-scope call answers 403, always.
    """

    def __init__(
        self,
        *,
        jobs: list[Any] | None = None,
        listable: set[str] | None = None,
        gettable: set[str] | None = None,
        deletable: set[str] | None = None,
        on_delete: Callable[[str, str], None] | None = None,
        list_fails_with: int | None = None,
    ) -> None:
        self.jobs = list(jobs or [])
        self.listable = set(listable or set())
        self.gettable = set(self.listable if gettable is None else gettable)
        self.deletable = set(self.listable if deletable is None else deletable)
        self.on_delete = on_delete
        #: Every LIST fails with this status, RBAC or not -- the transient
        #: failure a single GET a moment later does not share.
        self.list_fails_with = list_fails_with
        self.calls: list[tuple[str, ...]] = []
        self.deleted: list[str] = []

    # -- cluster scope: never granted ------------------------------------
    def list_job_for_all_namespaces(self, **kwargs: Any) -> Any:
        self.calls.append(("list_job_for_all_namespaces",))
        raise forbidden("list")

    # -- namespaced ------------------------------------------------------
    def list_namespaced_job(self, namespace: str, label_selector: str | None = None,
                            **kwargs: Any) -> Any:
        self.calls.append(("list_namespaced_job", namespace))
        if self.list_fails_with is not None:
            raise api_error(self.list_fails_with)
        if namespace not in self.listable:
            raise forbidden("list", namespace)
        return SimpleNamespace(
            items=[j for j in self.jobs if j.metadata.namespace == namespace]
        )

    def read_namespaced_job(self, name: str, namespace: str, **kwargs: Any) -> Any:
        self.calls.append(("read_namespaced_job", namespace, name))
        if namespace not in self.gettable:
            raise forbidden("get", namespace)
        for job in self.jobs:
            if job.metadata.namespace == namespace and job.metadata.name == name:
                return job
        raise not_found(name)

    def delete_namespaced_job(self, name: str, namespace: str, body: Any = None,
                              **kwargs: Any) -> Any:
        self.calls.append(("delete_namespaced_job", namespace, name))
        if namespace not in self.deletable:
            raise forbidden("delete", namespace)
        if self.on_delete is not None:
            self.on_delete(namespace, name)
        before = len(self.jobs)
        self.jobs = [
            j for j in self.jobs
            if not (j.metadata.namespace == namespace and j.metadata.name == name)
        ]
        if len(self.jobs) == before:
            raise not_found(name)
        self.deleted.append(f"{namespace}/{name}")
        return SimpleNamespace(status="Success")

    def cluster_scope_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "list_job_for_all_namespaces"]

    def listed(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "list_namespaced_job"]


class RbacCoreApi:
    """CoreV1Api: Namespaces are cluster-scoped, so every call here is refused."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_namespace(self, **kwargs: Any) -> Any:
        self.calls.append("list_namespace")
        raise forbidden("list")

    def read_namespace(self, name: str, **kwargs: Any) -> Any:
        self.calls.append("read_namespace")
        raise forbidden("get")

    def delete_namespace(self, name: str, **kwargs: Any) -> Any:
        self.calls.append("delete_namespace")
        raise forbidden("delete")


def k8s_job(
    *,
    task_id: str,
    namespace: str = ENG_NS,
    generation: int = 1,
    tenant: str = ENG,
    active: int = 0,
    failed: int = 0,
    conditions: list[tuple[str, str]] | None = None,
) -> Any:
    """A batch/v1 Job shaped as `GkeJobDispatcher._manifest` creates it."""
    name = f"swarm-{task_id.replace('task_', '')}-{generation}"
    env = {
        "TASK_ID": task_id,
        "ATTEMPT_ID": f"att_{task_id[5:]}",
        "TENANT_ID": tenant,
        "GENERATION": str(generation),
    }
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels={"managed-by": "swarm-scheduler", "swarm-tenant": tenant},
            creation_timestamp=utcnow() - timedelta(minutes=20),
        ),
        status=SimpleNamespace(
            active=active,
            succeeded=0,
            failed=failed,
            conditions=[SimpleNamespace(type=t, status=s) for t, s in (conditions or [])],
        ),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[
                        SimpleNamespace(
                            name="worker",
                            env=[SimpleNamespace(name=k, value=v) for k, v in env.items()],
                        )
                    ]
                )
            )
        ),
    )


class FlatBackend:
    """A backend listed in one call, as Cloud Run is. Scripted."""

    def __init__(self, name: str = CLOUD_RUN, *, executions: list[Any] | None = None,
                 resources: list[Any] | None = None, gc_raises: Exception | None = None):
        self.name = name
        self._executions = list(executions or [])
        self._resources = list(resources or [])
        self.gc_raises = gc_raises
        self.deleted: list[str] = []

    def list_executions(self) -> list[Any]:
        return list(self._executions)

    def terminate(self, execution: Any) -> bool:
        return True

    def list_job_resources(self) -> list[Any]:
        if self.gc_raises is not None:
            raise self.gc_raises
        return list(self._resources)

    def delete_job_resource(self, resource: Any) -> bool:
        self.deleted.append(resource.name)
        return True


# ---------------------------------------------------------------------------
# Control-plane seeding
# ---------------------------------------------------------------------------


def seed_tenant(db: FakeFirestore, tenant: str, *, record_namespace: bool = False) -> None:
    doc: dict[str, Any] = {
        "tenant_id": tenant,
        "kind": "user" if tenant.startswith("u-") else "group",
        "principal": f"{tenant}@saga.xyz",
        "enabled": True,
    }
    if record_namespace:
        # What `swarm_api.store.ensure_tenant` and register-tenant.sh write.
        doc["namespace"] = f"{NS}{tenant}"
    db.docs[f"tenants/{tenant}"] = doc


def seed_stranded(
    db: FakeFirestore,
    task_id: str,
    *,
    tenant: str = ENG,
    backend: str = GKE,
    generation: int = 1,
    cancel_requested: bool = False,
    attempt_count: int = 1,
    max_attempts: int = 3,
    minutes_ago: int = 20,
    state: str = "DISPATCHED",
    heartbeat_seconds_ago: int | None = None,
) -> dict[str, str]:
    """A task exactly as the incident left it -- or, with a heartbeat, a healthy one.

    By default: DISPATCHED, lease never heartbeated (`heartbeat_at: None`, which
    admission writes), dispatch deadline long past, attempt naming
    `<namespace>/<job>` the way `GkeJobDispatcher.dispatch` records it. Two
    units on each of the seven pools a browser task acquires.

    `heartbeat_seconds_ago` makes the lease one a worker is keeping alive: the
    worker's heartbeat writes `heartbeat_at = now` and pushes `expires_at` out
    by its extension (`agent_worker/control.py`), so both move together here.
    """
    suffix = task_id.replace("task_", "")
    lease_id, attempt_id = f"lease_{suffix}", f"att_{suffix}"
    now = utcnow()
    created = now - timedelta(minutes=minutes_ago)
    if heartbeat_seconds_ago is None:
        heartbeat_at, expires_at = None, created + timedelta(seconds=120)
    else:
        heartbeat_at = now - timedelta(seconds=heartbeat_seconds_ago)
        expires_at = heartbeat_at + timedelta(seconds=120)
    pools = pool_names_for(
        tenant_id=tenant, provider="anthropic", resource_class="browser",
        runner_profile="browser", backend=backend,
    )
    namespace = f"{NS}{tenant}"
    execution_name = (
        f"{namespace}/swarm-{suffix}-{generation}" if backend == GKE
        else f"projects/p/locations/us-central1/jobs/swarm-job-{tenant}-browser/executions/x-{suffix}"
    )
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": tenant, "state": state,
        "runner_profile": "browser", "resource_class": "browser",
        "current_generation": generation, "current_lease_id": lease_id,
        "attempt_count": attempt_count, "max_attempts": max_attempts,
        "updated_at": created, "cancel_requested": cancel_requested,
    }
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id, "task_id": task_id, "attempt_id": attempt_id,
        "tenant_id": tenant, "generation": generation, "pools": pools, "units": 2,
        "state": state, "created_at": created,
        "dispatch_deadline": created + timedelta(seconds=300),
        "expires_at": expires_at,
        "heartbeat_at": heartbeat_at, "released_at": None,
    }
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant,
        "generation": generation, "lease_id": lease_id, "backend": backend,
        "created_at": created, "execution_name": execution_name,
        "started_at": created if heartbeat_at is not None else None,
    }
    for pool in pools:
        doc = db.docs.setdefault(
            f"pools/{pool}",
            {"name": pool, "hard_limit": 10, "active": 0, "enabled": True, "updated_at": now},
        )
        doc["active"] += 2
    return {"task": task_id, "lease": lease_id, "attempt": attempt_id,
            "execution": execution_name, "namespace": namespace}


def config(**overrides: Any) -> ReconcilerConfig:
    base: dict[str, Any] = dict(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_checkpoint_gc=False,
    )
    base.update(overrides)
    return ReconcilerConfig(**base)


def plain_filter(field: str, op: str, value: Any) -> Any:
    """The where-clause ControlStore builds, in a shape the fake can evaluate.

    google's `FieldFilter("released_at", "==", None)` rewrites the operator to
    the IS_NULL unary enum, which the control-plane fake did not model when
    this file was written: it read every unreleased lease as NOT matching, the
    snapshot held no leases, and every test here passed or failed on an empty
    world. The first run of this file did exactly that. PR #19 has since taught
    the fake IS_NULL (and made it raise on any operator it does not know), so
    this seam is no longer load-bearing; it is kept because it is what these
    tests were proven red and green against. Same field, same operator, same
    value -- only the object carrying them differs, through the seam
    ControlStore already exposes for this.
    """
    return SimpleNamespace(field_path=field, op_string=op, value=value)


def reconciler(db: FakeFirestore, *backends: Any, store_class: type = ControlStore,
               **overrides: Any) -> tuple[Reconciler, io.StringIO]:
    """The production Reconciler over the production ControlStore.

    No `txn_runner` is passed, so every write goes through the real
    `firestore.transactional` driving the fake's transaction protocol -- the
    same control flow production runs.
    """
    stream = io.StringIO()
    logger = build_logger(stream=stream)
    store = store_class(db, logger=logger, filter_factory=plain_filter)
    return (
        Reconciler(store=store, backends=list(backends), config=config(**overrides),
                   logger=logger),
        stream,
    )


def gke(batch: RbacBatchApi, core: RbacCoreApi | None = None) -> GkeBackend:
    return GkeBackend(
        namespace_prefix=NS,
        batch_api=batch,
        core_api=core or RbacCoreApi(),
        logger=build_logger(stream=io.StringIO()),
    )


def log_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def pool_actives(db: FakeFirestore) -> dict[str, int]:
    return {path[len("pools/"):]: doc["active"] for path, doc in db.docs.items()
            if path.startswith("pools/")}


def event_types(db: FakeFirestore, task_id: str) -> list[str]:
    return [e["type"] for e in db.collection_docs(f"tasks/{task_id}/events")]


# ---------------------------------------------------------------------------
# F-1: no cluster scope, per-namespace readability
# ---------------------------------------------------------------------------


def test_the_reconciler_never_asks_the_cluster_for_anything_cluster_scoped():
    """(1) The live 403, as a test.

    Four registered tenants, as in saga-agents-staging on 2026-09-24 -- and only
    `eng`'s namespace carries the `swarm-reaper` binding. The other three answer
    403 like a missing namespace does. That must cost nothing: none of them
    holds an attempt, so none is an error, and GKE is not "unavailable".
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    for other in ("smoke", "u-bogdan", "u-sw-c90291"):
        seed_tenant(db, other)
    batch, core = RbacBatchApi(listable={ENG_NS}), RbacCoreApi()
    rec, stream = reconciler(db, gke(batch, core))

    report = rec.run_once()

    gke_errors = [e for e in report.errors if GKE in e]
    assert gke_errors == [], f"a readable eng namespace must leave GKE readable: {gke_errors}"
    assert batch.cluster_scope_calls() == [], "list_job_for_all_namespaces is cluster-scope"
    assert core.calls == [], f"Namespace objects are cluster-scope; called {core.calls}"
    assert ENG_NS in batch.listed()
    # Every registered tenant's namespace was asked about, by the dispatcher's
    # own naming rule -- and the three refusals are recorded, not raised.
    assert {f"{NS}smoke", f"{NS}u-bogdan", f"{NS}u-sw-c90291"} <= set(batch.listed())
    assert set(report.unreadable_namespaces[GKE]) == {
        f"{NS}smoke", f"{NS}u-bogdan", f"{NS}u-sw-c90291"
    }
    assert not any(
        line["message"] == repair.BACKEND_UNAVAILABLE for line in log_lines(stream)
    )


def test_a_stranded_gke_lease_is_released_once_its_namespace_reads_empty():
    """(2) The incident's own lease, repaired.

    DISPATCHED, `heartbeat_at` None, dispatch deadline 15 minutes gone, attempt
    on GKE_AUTOPILOT in `swarm-tenant-eng`, and the namespaced list returns
    nothing because the Job was TTL-deleted. Today this is dropped as
    "unreadable" at repair.py's `_is_actionable`, on every pass, for ever.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_2a417cb24edb4d2cb59e")
    batch = RbacBatchApi(listable={ENG_NS}, jobs=[])
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    lease = db.docs[f"leases/{ids['lease']}"]
    assert lease["released_at"] is not None, [o.as_dict() for o in report.outcomes]
    # All seven pools, in the one frozen release transaction (invariant 2).
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "READY"
    assert task["current_lease_id"] is None
    # The generation was fenced BEFORE the release (invariant 5): any worker of
    # generation 1 that ever wakes up exits without running the agent.
    assert task["current_generation"] == 2
    assert "lease_released" in event_types(db, ids["task"])
    assert batch.cluster_scope_calls() == []


def test_one_unreadable_namespace_does_not_hold_another_tenants_lease():
    """(3) Readability is per namespace, both ways.

    `u-bogdan` answers 403 (no RoleBinding there); `eng` reads. The eng lease is
    released and repaired; the u-bogdan lease is held, because nothing proves
    its execution is gone.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    seed_tenant(db, "u-bogdan", record_namespace=True)
    readable = seed_stranded(db, "task_64a451a63efb48438265", tenant=ENG)
    blind = seed_stranded(db, "task_719225c0557148c793d9", tenant="u-bogdan")
    batch = RbacBatchApi(listable={ENG_NS}, gettable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert report.leases_examined == 2, "both leases must be in the snapshot"
    assert db.docs[f"leases/{readable['lease']}"]["released_at"] is not None
    assert db.docs[f"tasks/{readable['task']}"]["state"] == "READY"

    assert db.docs[f"leases/{blind['lease']}"]["released_at"] is None
    assert db.docs[f"tasks/{blind['task']}"]["state"] == "DISPATCHED"
    assert db.docs[f"tasks/{blind['task']}"]["current_generation"] == 1
    # Shared pools went down by exactly the one lease that was released.
    assert db.docs["pools/global"]["active"] == 2
    assert db.docs["pools/tenant:u-bogdan"]["active"] == 2
    assert db.docs["pools/tenant:eng"]["active"] == 0
    # The held namespace holds one of our attempts, so it IS an error.
    assert any(f"{NS}u-bogdan" in e for e in report.errors), report.errors


def test_nothing_is_released_when_the_attempts_own_namespace_is_unreadable():
    """(4) The guard. Another namespace reading fine cures nothing here.

    GKE as a whole is readable -- `smoke` answers -- so a check that asked only
    "is the backend readable?" would release eng's lease on no evidence at all.
    The attempt lives in `swarm-tenant-eng`, which answers 403 to list and get.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    seed_tenant(db, "smoke", record_namespace=True)
    ids = seed_stranded(db, "task_30074d78ced8432b9a3b")
    batch = RbacBatchApi(listable={f"{NS}smoke"}, gettable={f"{NS}smoke"})
    before = pool_actives(db)
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    # Without this the guard passes in an empty world: nothing to release.
    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None
    assert pool_actives(db) == before
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "DISPATCHED"
    assert task["current_generation"] == 1, "nothing may be fenced on no evidence"
    assert report.findings == 0
    # The precondition that makes this a guard rather than a restatement of
    # "GKE is blind": the backend WAS readable, through another namespace.
    assert f"{NS}smoke" in batch.listed()
    assert not any(e.startswith(f"{GKE}: listing executions failed") for e in report.errors)


def test_the_reconciler_names_a_tenant_namespace_exactly_as_the_dispatcher_does():
    """Two images, one naming rule, pinned together.

    The dispatcher reads `tenant.namespace` FIRST and its template second
    (`GkeJobDispatcher.namespace_for`). A reconciler that derived the name from
    the prefix alone would read one namespace while Jobs were created in
    another, and every task there would read as abandoned.
    """
    backend = GkeBackend(namespace_prefix=NS, batch_api=RbacBatchApi(), core_api=RbacCoreApi())
    dispatchers = [
        GkeJobDispatcher(scheduler_settings(), target=None),
        GkeJobDispatcher(scheduler_settings(), target=GkeTarget("10.0.0.1", "/dev/null")),
    ]
    tenants = [
        Tenant(tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=utcnow()),
        Tenant(tenant_id="u-bogdan", kind="user", principal="bogdan@saga.xyz",
               created_at=utcnow()),
        Tenant(tenant_id="u-sw-c90291", kind="user", principal="sw@saga.xyz",
               created_at=utcnow()),
        # Recorded, and deliberately NOT what the template would derive: the
        # recorded value must win on both sides.
        Tenant(tenant_id="smoke", kind="group", principal="smoke@saga.xyz",
               created_at=utcnow(), namespace=f"{NS}smoke-legacy"),
        Tenant(tenant_id="eng", kind="group", principal="eng@saga.xyz",
               created_at=utcnow(), namespace=ENG_NS),
    ]
    for dispatcher in dispatchers:
        for tenant in tenants:
            assert backend.namespace_for(tenant.tenant_id, tenant.namespace) == (
                dispatcher.namespace_for(tenant)
            ), tenant


#: Tenant ids that exercise EVERY branch of the dispatcher's `sanitize_name`,
#: not only the short clean ids in use. `identity._slug` caps a tenant id at 11
#: characters and `scripts/register-tenant.sh` refuses anything longer, so the
#: truncation branch is unreachable through either TODAY -- which is exactly
#: why a restated copy can drift there unseen, and why the pin must reach it: a
#: tenant document written by hand, or by a future path, is bound by neither.
NAMING_CORPUS = [
    "eng",
    "u-bogdan",
    "Eng_Platform.Team",             # lossy: case, underscore, dot
    "double__underscore--dash",      # runs collapse to one dash
    "--edges--",                     # stripped at both ends
    "a" * 50,                        # 13 + 50 = 63: the last length kept whole
    "a" * 51,                        # one over: truncated, hashed
    "platform-engineering-" * 4,     # a dash exactly at the cut
    "x" * 200,
]


def test_the_reconciler_names_a_long_tenant_namespace_exactly_as_the_dispatcher_does():
    """Past 63 characters the dispatcher truncates and appends a hash; so must we.

    `sanitised` -- which the reconciler used to name namespaces -- mirrors only
    the character-class half of `sanitize_name`. For a tenant id over 50
    characters the dispatcher creates `swarm-tenant-aaaa...-<sha8>` while the
    reconciler read `swarm-tenant-aaaa...` in full: a namespace nobody writes
    to, listed empty on every pass, and orphans in the real one never found.
    Not reachable through today's registration paths (see NAMING_CORPUS), and
    a live attempt is always read at the namespace its own `execution_name`
    records -- but one rule stated twice must agree everywhere, not only where
    the inputs happen to be short.
    """
    backend = GkeBackend(namespace_prefix=NS, batch_api=RbacBatchApi(), core_api=RbacCoreApi())
    dispatchers = [
        GkeJobDispatcher(scheduler_settings(), target=None),
        GkeJobDispatcher(scheduler_settings(), target=GkeTarget("10.0.0.1", "/dev/null")),
    ]
    for tenant_id in NAMING_CORPUS:
        tenant = Tenant(tenant_id=tenant_id, kind="group", principal=f"{tenant_id}@saga.xyz",
                        created_at=utcnow())
        for dispatcher in dispatchers:
            expected = dispatcher.namespace_for(tenant)
            assert len(expected) <= 63
            assert backend.namespace_for(tenant_id, None) == expected, tenant_id


def test_the_reconciler_and_the_dispatcher_sanitise_names_identically():
    """Two copies of one rule, held together the way `gke_api_host`'s are.

    The reconciler image ships `apps/common` and `apps/reconciler` and nothing
    else, so it cannot import the scheduler's `sanitize_name`; the frozen
    `swarm_common` is the only place both could share one (contract request 16
    in docs/contract-change-requests.md). Until then this equality is the only
    thing that keeps the copies from drifting.
    """
    from reconciler.backends import sanitize_name as reconciler_sanitize
    from scheduler.dispatch import DispatchError
    from scheduler.dispatch import sanitize_name as scheduler_sanitize

    cases: list[tuple[str, ...]] = [(t,) for t in NAMING_CORPUS]
    cases += [
        (f"{NS}{t}",) for t in NAMING_CORPUS
    ] + [
        ("9lives",),                  # not led by a letter: an `s` is prepended
        ("swarm", "", "job", "eng"),  # empty parts are skipped, not joined
        ("swarm", "job", "u-bogdan", "claude-code"),
    ]
    for parts in cases:
        for max_length in (63, 40, 20):
            assert reconciler_sanitize(*parts, max_length=max_length) == (
                scheduler_sanitize(*parts, max_length=max_length)
            ), (parts, max_length)

    # Neither copy invents a name out of nothing.
    with pytest.raises(DispatchError):
        scheduler_sanitize("___")
    with pytest.raises(ValueError):
        reconciler_sanitize("___")


# ---------------------------------------------------------------------------
# F-3: a requested cancel finishes in one hop
# ---------------------------------------------------------------------------


def test_a_cancel_requested_task_with_no_execution_ends_cancelled_in_one_pass():
    """Not READY-then-cancelled-by-the-scheduler: CANCELLED, now, with its lease back."""
    db = FakeFirestore()
    ids = seed_stranded(db, "task_637eb5eaae9445a6b186", backend=CLOUD_RUN,
                        cancel_requested=True)
    rec, _ = reconciler(db, FlatBackend(CLOUD_RUN))

    report = rec.run_once()

    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "CANCELLED", task
    assert task["completed_at"] is not None
    assert task["current_lease_id"] is None
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert set(pool_actives(db).values()) == {0}
    events = db.collection_docs(f"tasks/{ids['task']}/events")
    cancelled = [e for e in events if e["type"] == "cancelled"]
    assert cancelled and cancelled[-1]["detail"]["phase"] == "cancelled"
    assert "ready" not in [e["type"] for e in events]


def test_a_cancel_requested_task_with_spent_attempts_ends_cancelled_not_failed():
    """A task somebody stopped is not a task that failed, whatever its attempt count."""
    db = FakeFirestore()
    ids = seed_stranded(db, "task_b568a623be8645eb87c6", backend=CLOUD_RUN,
                        cancel_requested=True, attempt_count=3, max_attempts=3)
    rec, _ = reconciler(db, FlatBackend(CLOUD_RUN))

    report = rec.run_once()

    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "CANCELLED", task
    assert "failed" not in event_types(db, ids["task"])


def test_a_cancel_pressed_after_the_snapshot_is_still_honoured():
    """The flag is re-read inside the repair transaction, not trusted from the snapshot."""

    class CancelAfterSnapshot(ControlStore):
        def snapshot(self, **kwargs: Any):
            taken = super().snapshot(**kwargs)
            # The API's request_cancel lands between the read and the repair.
            self._db.docs["tasks/task_2a417cb24edb4d2cb59e"]["cancel_requested"] = True
            return taken

    db = FakeFirestore()
    ids = seed_stranded(db, "task_2a417cb24edb4d2cb59e", backend=CLOUD_RUN)
    rec, _ = reconciler(db, FlatBackend(CLOUD_RUN), store_class=CancelAfterSnapshot)

    report = rec.run_once()

    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    assert db.docs[f"tasks/{ids['task']}"]["state"] == "CANCELLED"


def test_the_incident_shape_ends_cancelled_and_frees_every_pool():
    """All five stranded tasks, one pass, the outcome the owner asked for.

    check-2..5 and task_637eb: DISPATCHED, cancel requested, Jobs TTL-deleted,
    `swarm-tenant-eng` readable under the reaper Role. Every task CANCELLED,
    every lease released, every one of the seven pools back to zero.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    for other in ("smoke", "u-bogdan", "u-sw-c90291"):
        seed_tenant(db, other)
    task_ids = [
        "task_719225c0557148c793d9", "task_64a451a63efb48438265",
        "task_30074d78ced8432b9a3b", "task_2a417cb24edb4d2cb59e",
        "task_637eb5eaae9445a6b186",
    ]
    seeded = [seed_stranded(db, t, cancel_requested=True) for t in task_ids]
    assert db.docs["pools/global"]["active"] == 10
    rec, _ = reconciler(db, gke(RbacBatchApi(listable={ENG_NS})))

    report = rec.run_once()

    assert report.leases_examined == 5, "all five stranded leases must be in the snapshot"
    assert [db.docs[f"tasks/{s['task']}"]["state"] for s in seeded] == ["CANCELLED"] * 5
    assert all(db.docs[f"leases/{s['lease']}"]["released_at"] is not None for s in seeded)
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)
    assert report.findings_suppressed == 0


# ---------------------------------------------------------------------------
# F-4: probe by name when the list is unreadable
# ---------------------------------------------------------------------------


def test_a_404_by_name_confirms_absence_and_releases():
    """The list is refused everywhere; a namespaced GET is not, and answers 404."""
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_2a417cb24edb4d2cb59e")
    batch = RbacBatchApi(listable=set(), gettable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    job_name = ids["execution"].split("/", 1)[1]
    assert ("read_namespaced_job", ENG_NS, job_name) in batch.calls
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert db.docs[f"tasks/{ids['task']}"]["state"] == "READY"
    assert any("confirmed by name" in o.reason for o in report.outcomes)
    assert report.findings_suppressed == 0


def test_a_403_by_name_proves_nothing_and_holds_the_lease():
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_2a417cb24edb4d2cb59e")
    batch = RbacBatchApi(listable=set(), gettable=set())
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    job_name = ids["execution"].split("/", 1)[1]
    assert ("read_namespaced_job", ENG_NS, job_name) in batch.calls, "the probe was not tried"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None
    assert db.docs[f"tasks/{ids['task']}"]["current_generation"] == 1
    assert db.docs[f"tasks/{ids['task']}"]["state"] == "DISPATCHED"


def test_an_active_job_found_by_name_is_killed_before_its_lease_is_released():
    """Fence, terminate, release -- in that order -- through a backend whose list failed.

    Also the proof that the backend HANDLE outlives the backend's readability:
    the kill has to be sent to a backend this pass could not list.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_64a451a63efb48438265")
    at_delete: dict[str, Any] = {}

    def observe(namespace: str, name: str) -> None:
        at_delete["generation"] = db.docs[f"tasks/{ids['task']}"]["current_generation"]
        at_delete["released_at"] = db.docs[f"leases/{ids['lease']}"]["released_at"]

    job = k8s_job(task_id=ids["task"], active=1)
    batch = RbacBatchApi(jobs=[job], listable=set(), gettable={ENG_NS},
                         deletable={ENG_NS}, on_delete=observe)
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert batch.deleted == [f"{ENG_NS}/{job.metadata.name}"], batch.calls
    assert at_delete == {"generation": 2, "released_at": None}, (
        "the generation must be fenced before the kill and the lease released only after it"
    )
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    killed = [o for o in report.outcomes if o.terminated]
    assert [o.kind for o in killed] == ["dead_worker"]


@pytest.mark.parametrize("list_status", [429, 500, 403])
def test_a_live_job_under_a_heartbeating_lease_is_never_killed_because_a_list_failed(
    list_status: int,
):
    """An ACTIVE answer by name DISPROVES an absence. It does not convict a lease.

    The list path and the probe path must reach the same verdict on the same
    world. Listed, this Job lands in `by_attempt`: no missing_execution is
    raised, a lease that heartbeated ten seconds ago is not stale, and nothing
    happens. Probed, the missing_execution that the FAILED list produced was
    turned into a dead_worker -- generation fenced 1 -> 2, the Job deleted, the
    lease released, the task re-queued: a healthy agent killed mid-run, its work
    since the last checkpoint lost. On every pass where a list failed and a get
    did not: APF answering 429 to LIST, a 5xx blip, a timeout -- or RBAC that
    grants `get` without `list` (403).
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_5b1e0f4c9d8a4e7b8c21", state="RUNNING",
                        heartbeat_seconds_ago=10)
    job = k8s_job(task_id=ids["task"], active=1)
    if list_status == 403:
        batch = RbacBatchApi(jobs=[job], listable=set(), gettable={ENG_NS},
                             deletable={ENG_NS})
    else:
        batch = RbacBatchApi(jobs=[job], listable={ENG_NS}, list_fails_with=list_status)
    before = pool_actives(db)
    rec, stream = reconciler(db, gke(batch))

    report = rec.run_once()

    assert report.leases_examined == 1, "the lease must be in the snapshot"
    # The path under test RAN: the list failed, and the Job was read by name.
    assert ENG_NS in batch.listed()
    job_name = ids["execution"].split("/", 1)[1]
    assert ("read_namespaced_job", ENG_NS, job_name) in batch.calls, "the probe was not tried"

    assert batch.deleted == [], "a heartbeating agent was killed because a LIST failed"
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["current_generation"] == 1, "a live worker's generation was fenced"
    assert task["state"] == "RUNNING"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None
    assert pool_actives(db) == before
    assert report.findings == 0
    # Disproved, not held back. Counting it as suppressed, or logging
    # NOT_REPAIRING for it, would page after thirty minutes of an agent that is
    # doing exactly what it should.
    assert report.findings_suppressed == 0, [s.as_dict() for s in report.suppressed]
    assert not [l for l in log_lines(stream) if l["message"] == repair.NOT_REPAIRING]


def test_a_live_job_under_a_silent_lease_is_still_killed_before_its_release():
    """The other half, so the fix above cannot be "never kill on a probe".

    The worker heartbeated, then went silent for ten minutes while its Job
    still reads active by name. Listed, that is a dead_worker; probed, it must
    be one too: fence, terminate, and only then release.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_0c6d2a9e71f84b35a1d4", state="RUNNING",
                        heartbeat_seconds_ago=600)
    job = k8s_job(task_id=ids["task"], active=1)
    batch = RbacBatchApi(jobs=[job], listable={ENG_NS}, list_fails_with=500)
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert batch.deleted == [f"{ENG_NS}/{job.metadata.name}"], batch.calls
    assert db.docs[f"tasks/{ids['task']}"]["current_generation"] == 2
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert [o.kind for o in report.outcomes if o.terminated] == ["dead_worker"]


def test_a_job_is_judged_by_its_conditions_not_by_its_failed_counter():
    """`status.failed` moves before a Job is over; `Failed`/`Complete` do not.

    Job A carries a Failed condition: finished, released without a kill.
    Job B has a failed pod and NO terminal condition -- a replacement pod may be
    starting -- so it is treated as running and killed before release.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    done = seed_stranded(db, "task_719225c0557148c793d9")
    retrying = seed_stranded(db, "task_30074d78ced8432b9a3b")
    finished_job = k8s_job(task_id=done["task"], conditions=[("Failed", "True")])
    retrying_job = k8s_job(task_id=retrying["task"], failed=1)
    batch = RbacBatchApi(jobs=[finished_job, retrying_job], listable=set(),
                         gettable={ENG_NS}, deletable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    rec.run_once()

    assert batch.deleted == [f"{ENG_NS}/{retrying_job.metadata.name}"]
    assert db.docs[f"leases/{done['lease']}"]["released_at"] is not None
    assert db.docs[f"leases/{retrying['lease']}"]["released_at"] is not None


# ---------------------------------------------------------------------------
# F-5: blindness is counted, and the alert matches what is written
# ---------------------------------------------------------------------------


def test_a_blind_pass_counts_what_it_held_back_instead_of_reporting_zero():
    """The stranded scenario: every namespace refused, list and get alike.

    It reported `findings=0` -- the same number as a healthy pass.
    """
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, "task_c388c25e50c5421f840d")
    rec, stream = reconciler(db, gke(RbacBatchApi(listable=set(), gettable=set())))

    report = rec.run_once()

    assert report.leases_examined == 1, "the stranded lease must be in the snapshot"
    assert report.findings == 0
    assert report.findings_suppressed == 2
    held = sorted((s.kind, s.lease_id, s.task_id, s.backend) for s in report.suppressed)
    assert held == [
        ("missing_execution", ids["lease"], ids["task"], GKE),
        ("stale_lease", ids["lease"], ids["task"], GKE),
    ]
    as_dict = report.as_dict()
    assert as_dict["findings_suppressed"] == 2
    assert {s["kind"] for s in as_dict["suppressed"]} == {"stale_lease", "missing_execution"}
    # Persisted, so the pass history shows it after the instance scales to zero.
    stored = db.collection_docs("reconciler_passes")
    assert [p["findings_suppressed"] for p in stored] == [2]
    # The two lines the alert policy counts, with the fields it groups by.
    lines = log_lines(stream)
    unavailable = [l for l in lines if l["message"] == repair.BACKEND_UNAVAILABLE]
    assert [l["backend"] for l in unavailable] == [GKE]
    not_repairing = [l for l in lines if l["message"] == repair.NOT_REPAIRING]
    assert {l["lease_id"] for l in not_repairing} == {ids["lease"]}
    # And /reconcile is not turned into a 5xx by any of this: the report is an
    # ordinary return value carrying the error.
    assert any(e.startswith(f"{GKE}: listing executions failed") for e in report.errors)


def _hcl_string_end(text: str, i: int) -> int:
    """Index of the quote closing a string whose body starts at `i`.

    Interpolations are walked, not skipped by character: `"${join(" OR ", x)}"`
    holds quotes of its own, and a lexer that did not know that would lose its
    place in `alerts.tf`'s locals block and read everything after it wrong.
    """
    while i < len(text):
        if text.startswith(("$${", "%%{"), i):
            i += 3
            continue
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i
        if c == "\n":
            raise AssertionError(f"unterminated HCL string near offset {i}")
        if text.startswith(("${", "%{"), i):
            i = _hcl_interpolation_end(text, i + 2)
            continue
        i += 1
    raise AssertionError("unterminated HCL string at end of file")


def _hcl_interpolation_end(text: str, i: int) -> int:
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            i = _hcl_string_end(text, i + 1) + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return i + 1
            depth -= 1
        i += 1
    raise AssertionError("unterminated HCL interpolation")


def hcl_tokens(text: str) -> list[tuple[str, str]]:
    """HCL as (kind, value) tokens, with every comment GONE.

    Just enough lexer to answer "what is this attribute's value", which a
    substring search of the file cannot: in a substring search a comment
    quoting the message satisfies the check exactly as well as the filter does.
    Kinds: STRING (raw body, escapes intact), HEREDOC, IDENT, PUNCT.
    """
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    word = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
    heredoc = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\n")
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "#" or text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        elif (opened := heredoc.match(text, i)) is not None:
            marker = re.compile(rf"^[ \t]*{opened.group(1)}[ \t]*$", re.M)
            closed = marker.search(text, opened.end())
            assert closed is not None, f"unterminated heredoc {opened.group(1)}"
            tokens.append(("HEREDOC", text[opened.end():closed.start()]))
            i = closed.end()
        elif c == '"':
            end = _hcl_string_end(text, i + 1)
            tokens.append(("STRING", text[i + 1:end]))
            i = end + 1
        elif (matched := word.match(text, i)) is not None:
            tokens.append(("IDENT", matched.group(0)))
            i = matched.end()
        else:
            tokens.append(("PUNCT", c))
            i += 1
    return tokens


def hcl_block(tokens: list[tuple[str, str]], *header: str) -> list[tuple[str, str]]:
    """The tokens inside `resource "<type>" "<name>" { ... }`, braces excluded."""
    head = [("IDENT", header[0]), *(("STRING", label) for label in header[1:]), ("PUNCT", "{")]
    for start in range(len(tokens) - len(head) + 1):
        if tokens[start:start + len(head)] == head:
            break
    else:
        raise AssertionError(f"no block {' '.join(header)} in alerts.tf")
    depth, body = 1, []
    for token in tokens[start + len(head):]:
        if token == ("PUNCT", "{"):
            depth += 1
        elif token == ("PUNCT", "}"):
            depth -= 1
            if depth == 0:
                return body
        body.append(token)
    raise AssertionError(f"block {' '.join(header)} is never closed")


_HCL_OPEN = {("PUNCT", "{"), ("PUNCT", "("), ("PUNCT", "[")}
_HCL_CLOSE = {("PUNCT", "}"), ("PUNCT", ")"), ("PUNCT", "]")}


def hcl_attribute(body: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """The value tokens of a TOP-LEVEL `name = <value>` in a block body.

    Top level only, so `key = ...` inside a nested `labels { }` block is never
    mistaken for an attribute of the resource itself.
    """
    depth = 0
    for index, token in enumerate(body):
        if token in _HCL_OPEN:
            depth += 1
        elif token in _HCL_CLOSE:
            depth -= 1
        elif depth == 0 and token == ("IDENT", name) and body[index + 1:index + 2] == [
            ("PUNCT", "=")
        ]:
            return _hcl_value(body[index + 2:])
    raise AssertionError(f"no top-level attribute {name!r}")


def _hcl_value(rest: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One expression: a literal, a name, a call, or a bracketed collection."""
    value: list[tuple[str, str]] = []
    depth = 0
    for position, token in enumerate(rest):
        value.append(token)
        if token in _HCL_OPEN:
            depth += 1
            continue
        if token in _HCL_CLOSE:
            depth -= 1
        if depth:
            continue
        following = rest[position + 1] if position + 1 < len(rest) else None
        if token[0] == "IDENT" and following in (("PUNCT", "("), ("PUNCT", "[")):
            continue                      # a function call or an index follows
        return value
    return value


def hcl_literals(value: list[tuple[str, str]]) -> list[str]:
    """The string literals in an attribute value, escapes decoded."""
    return [
        body.replace('\\"', '"').replace("\\\\", "\\")
        for kind, body in value
        if kind == "STRING"
    ]


def test_the_alert_policy_matches_the_lines_the_reconciler_writes():
    """A log-based metric whose filter matches nothing is not an error anywhere.

    It is a number that stays at zero and an alert that never fires, which is
    indistinguishable from a healthy platform. So each message is compared,
    verbatim, against the FILTER of the metric that counts it -- parsed, with
    comments removed. The first version of this test searched the file's text,
    where the comment above the metrics quotes BACKEND_UNAVAILABLE word for
    word: the filter could have said anything at all and the test would pass.
    """
    alerts = (REPO / "terraform" / "modules" / "monitoring" / "alerts.tf").read_text()
    tokens = hcl_tokens(alerts)
    expected = {
        "reconciler_backend_unavailable": (repair.BACKEND_UNAVAILABLE, {"backend"}),
        "reconciler_not_repairing": (repair.NOT_REPAIRING, {"lease_id", "backend"}),
    }
    for metric, (message, fields) in expected.items():
        body = hcl_block(tokens, "resource", "google_logging_metric", metric)
        filter_literals = hcl_literals(hcl_attribute(body, "filter"))
        assert f'jsonPayload.message="{message}"' in filter_literals, (metric, filter_literals)
        # Every line from the reconciler carries component=reconciler; the
        # shared clause lives in a local the filter must actually use.
        assert ("IDENT", "local.reconciler_log_filter") in hcl_attribute(body, "filter"), metric

        # The fields the metric extracts are the ones the log line carries.
        extractors = hcl_attribute(body, "label_extractors")
        pairs = {
            key[1]: value[1]
            for key, eq, value in zip(extractors, extractors[1:], extractors[2:])
            if key[0] == "IDENT" and eq == ("PUNCT", "=") and value[0] == "STRING"
        }
        assert pairs == {f: f"EXTRACT(jsonPayload.{f})" for f in fields}, (metric, pairs)


# ---------------------------------------------------------------------------
# GC: one backend's failure does not stop another's collection
# ---------------------------------------------------------------------------


def test_one_backends_gc_failure_does_not_abort_another_backends_collection():
    db = FakeFirestore()
    old = utcnow() - timedelta(days=30)
    idle = JobResourceView(
        name="swarm-job-finance-mock", tenant_id="finance", runner_profile="mock",
        created_at=old, last_execution_at=old, managed=True, active_executions=0,
    )
    broken = FlatBackend("BROKEN_FIRST", gc_raises=RuntimeError("gc exploded"))
    healthy = FlatBackend(CLOUD_RUN, resources=[idle])
    rec, _ = reconciler(db, broken, healthy)

    report = rec.run_once()

    assert healthy.deleted == ["swarm-job-finance-mock"]
    assert any("gc exploded" in e for e in report.errors)


def test_gke_offers_no_namespace_for_collection_and_never_touches_one():
    """Namespace GC needs cluster scope; this platform grants none, so none is tried."""
    db = FakeFirestore()
    seed_tenant(db, ENG, record_namespace=True)
    core = RbacCoreApi()
    backend = gke(RbacBatchApi(listable={ENG_NS}), core)
    rec, _ = reconciler(db, backend)

    report = rec.run_once()

    assert backend.list_job_resources() == []
    assert core.calls == []
    assert not [e for e in report.errors if e.startswith("gc")]
    stray = JobResourceView(
        name=f"{NS}gone", tenant_id="gone", runner_profile=None, created_at=None,
        last_execution_at=None, managed=True, active_executions=0,
    )
    with pytest.raises(PermissionError):
        backend.delete_job_resource(stray)
    assert core.calls == []
