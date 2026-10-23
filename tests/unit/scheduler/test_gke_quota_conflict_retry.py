"""A GKE create that loses the ResourceQuota update race is retried, not failed
(GKE-QUOTA-409; part of #2063).

On 2026-10-23 at 07:55:54Z a Job create in `swarm-tenant-smoke` answered

    (409) Reason: Conflict
    "Operation cannot be fulfilled on resourcequotas ...: the object has been
    modified; please apply your changes to the latest version and try again"

300 ms after its lease. That is Kubernetes' quota admission losing an
optimistic-concurrency race with another create in the same namespace: the
quota's usage could not be updated, so nothing was created and the request is
safe to send again. `GkeJobDispatcher.dispatch` turned it into
`gke_create_job_failed` and the acceptance task ended FAILED.

What holds now, each pinned below:

  * a quota 409 is retried inside the same dispatch, a bounded number of times,
    and a later success creates exactly one Job and returns the attempt's
    recorded execution name;
  * a quota 409 that outlasts the bound is a DispatchError, which the drain
    already answers by releasing the lease and returning the task to READY
    (invariant 1) -- the attempt is not FAILED while retries remain;
  * a 409 `AlreadyExists` for THIS attempt's Job name is the Job existing: it is
    adopted, never created twice and never failed. Any other name is an error;
  * a 403 keeps its code and its message.

The Kubernetes client is faked with its own `ApiException`, carrying the body
the API server sends, so the classifier reads what production reads. No cluster
and no sleeping: `time.sleep` is replaced and its calls recorded.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from kubernetes.client import ApiException

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

import scheduler.dispatch as dispatch_mod  # noqa: E402
from scheduler.dispatch import (  # noqa: E402
    BackendRouter,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
)
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

NOW = datetime(2026, 10, 23, 7, 55, 54, tzinfo=timezone.utc)
TENANT = "smoke"
NAMESPACE = f"swarm-tenant-{TENANT}"
PROFILE = "browser"


def api_error(status: int, http_reason: str, body: dict[str, Any]) -> ApiException:
    """What `kubernetes.client` raises: the HTTP reason phrase on `.reason`,
    the API server's Status object, as JSON text, on `.body`."""
    error = ApiException(status=status, reason=http_reason)
    error.body = json.dumps(body)
    return error


def quota_conflict() -> ApiException:
    """The 409 measured on 2026-10-23, in the shape the API server sends it."""
    return api_error(409, "Conflict", {
        "kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
        "message": ('Operation cannot be fulfilled on resourcequotas "gke-resource-quotas": '
                    "the object has been modified; please apply your changes to the "
                    "latest version and try again"),
        "reason": "Conflict",
        "details": {"name": "gke-resource-quotas", "kind": "resourcequotas"},
        "code": 409,
    })


def already_exists(name: str) -> ApiException:
    return api_error(409, "Conflict", {
        "kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
        "message": f'jobs.batch "{name}" already exists',
        "reason": "AlreadyExists",
        "details": {"name": name, "group": "batch", "kind": "jobs"},
        "code": 409,
    })


def forbidden() -> ApiException:
    return api_error(403, "Forbidden", {
        "kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
        "message": (f'jobs.batch is forbidden: User "117405034245659033603" cannot create '
                    f'resource "jobs" in API group "batch" in the namespace "{NAMESPACE}"'),
        "reason": "Forbidden",
        "details": {"group": "batch", "kind": "jobs"},
        "code": 403,
    })


class ScriptedBatch:
    """BatchV1Api whose creates answer from a script: an exception is raised,
    anything else means the Job was created. Every call is recorded, and a
    Job counts as created only when its call did not raise."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    def create_namespaced_job(self, *, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"namespace": namespace, "name": body["metadata"]["name"]})
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, BaseException):
            raise answer
        self.created.append(body)
        return {"metadata": {"name": body["metadata"]["name"]}}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every backoff the dispatcher takes, and no real waiting."""
    taken: list[float] = []
    monkeypatch.setattr(dispatch_mod.time, "sleep", taken.append)
    return taken


def make_tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT, kind="group", principal=f"{TENANT}@example.com", created_at=NOW,
        service_account=f"swarm-agent-worker-{TENANT}@p.iam.gserviceaccount.com",
        gcs_prefix=f"gs://p-swarm-artifacts/tenants/{TENANT}", namespace=NAMESPACE,
    )


def make_task() -> Task:
    profile = RUNNER_PROFILES[PROFILE]
    return Task(
        id="task_b8af6d3193ef45699a83", tenant_id=TENANT, created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=PROFILE, resource_class=profile.resource_class,
        input={}, submitted_by="accept@example.com", provider=profile.provider,
        timeout_seconds=1800,
    )


def make_lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1", task_id=task.id, attempt_id="att_2c1a232102544ce8b634",
        tenant_id=task.tenant_id, generation=1, pools=["global"], units=1,
        state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )


def dispatch(batch: ScriptedBatch) -> str:
    task = make_task()
    dispatcher = GkeJobDispatcher(scheduler_settings(), target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=batch, identity="swarm-scheduler@p.example")
    return dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[PROFILE],
                               tenant=make_tenant())


def own_job_name() -> str:
    """The Job name this attempt renders, read from a successful create rather
    than re-derived, so the test cannot agree with a wrong derivation."""
    batch = ScriptedBatch()
    dispatch(batch)
    (job,) = batch.created
    return str(job["metadata"]["name"])


# --- inside one dispatch ------------------------------------------------------------

def test_a_quota_conflict_then_success_creates_one_job_and_returns_the_execution_name(sleeps):
    batch = ScriptedBatch(quota_conflict(), None)

    execution = dispatch(batch)

    name = own_job_name()
    # MUTATION: drop the quota-conflict retry, so the first 409 raises
    # gke_create_job_failed -- dispatch raises and this line is never reached.
    assert execution == f"{NAMESPACE}/{name}"
    # MUTATION: retry by creating twice per try, or keep creating after the
    # success -- more than one Job reaches the cluster.
    assert len(batch.created) == 1
    # MUTATION: retry without the first try being counted (a loop that calls
    # once more after success) -- the call log is not exactly [409, ok].
    assert len(batch.calls) == 2
    # MUTATION: retry with no backoff at all -- a tight loop re-reads the same
    # stale quota version and loses the race again.
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_a_quota_conflict_that_outlasts_the_bound_is_a_dispatch_error_after_four_tries(sleeps):
    batch = ScriptedBatch(*(quota_conflict() for _ in range(6)))

    with pytest.raises(DispatchError) as raised:
        dispatch(batch)

    # MUTATION: raise the bound (or make it unbounded) -- a fifth create is
    # sent; lower it -- fewer than four are.
    assert len(batch.calls) == 4
    # MUTATION: count a refused create as created.
    assert batch.created == []
    # MUTATION: give up with a new code -- the loop, the runbook and the UI
    # know gke_create_job_failed; a new code is a contract request first.
    assert raised.value.code == "gke_create_job_failed"
    # MUTATION: a longer backoff (or a fixed one over 2 s in total) -- the
    # drain that runs this dispatch inline stalls every other tenant behind it.
    assert len(sleeps) == 3 and 0 < sum(sleeps) < 2.0


def test_each_quota_retry_logs_one_line_with_namespace_try_and_reason(sleeps, caplog):
    batch = ScriptedBatch(quota_conflict(), quota_conflict(), None)

    with caplog.at_level("WARNING", logger="scheduler.dispatch"):
        dispatch(batch)

    lines = [r.getMessage() for r in caplog.records if "resourcequota" in r.getMessage()]
    # MUTATION: drop the retry log, or log once per dispatch instead of per try.
    assert len(lines) == 2, lines
    for n, line in enumerate(lines, start=1):
        # MUTATION: leave out the namespace, the try number or the reason.
        assert NAMESPACE in line and f"try={n}" in line and "Conflict" in line, line
        # MUTATION: log the manifest (or the exception's body) -- the line
        # carries the pod spec, its env and its identities.
        assert "apiVersion" not in line and "SWARM_EXECUTION_NAME" not in line, line


def test_already_exists_for_this_attempts_own_job_is_adopted_without_a_second_create(sleeps):
    name = own_job_name()
    batch = ScriptedBatch(already_exists(name))

    execution = dispatch(batch)

    # MUTATION: treat AlreadyExists like any other error -- dispatch raises
    # gke_create_job_failed and the lease is released under a running Job.
    assert execution == f"{NAMESPACE}/{name}"
    # MUTATION: answer AlreadyExists by creating again (or by deleting and
    # recreating) -- a second create is sent.
    assert len(batch.calls) == 1
    # MUTATION: retry AlreadyExists as if it were the quota race.
    assert sleeps == []


def test_already_exists_for_another_jobs_name_is_an_error(sleeps):
    name = own_job_name()
    batch = ScriptedBatch(already_exists(name + "-other"))

    with pytest.raises(DispatchError) as raised:
        dispatch(batch)

    # MUTATION: adopt any AlreadyExists without comparing the name -- this
    # attempt would record a Job some other attempt (another generation) owns.
    assert raised.value.code == "gke_create_job_failed"
    # MUTATION: retry an AlreadyExists for another name.
    assert len(batch.calls) == 1


def test_a_403_is_unchanged(sleeps):
    batch = ScriptedBatch(forbidden(), None)

    with pytest.raises(DispatchError) as raised:
        dispatch(batch)

    # MUTATION: retry every 4xx (or every ApiException) -- the 403 is sent
    # twice and the second answer, a success, is returned.
    assert len(batch.calls) == 1
    # MUTATION: route the 403 through the conflict branch, or drop its code.
    assert raised.value.code == "gke_create_job_forbidden"
    # MUTATION: lose the 403's namespace-may-not-exist explanation.
    assert "does not prove this is a permissions problem" in str(raised.value)
    assert sleeps == []


def test_a_plain_409_conflict_on_another_object_is_not_retried(sleeps):
    other = api_error(409, "Conflict", {
        "kind": "Status", "status": "Failure", "reason": "Conflict", "code": 409,
        "message": 'Operation cannot be fulfilled on jobs.batch "x": the object has been modified',
        "details": {"name": "x", "group": "batch", "kind": "jobs"},
    })
    batch = ScriptedBatch(other, None)

    with pytest.raises(DispatchError) as raised:
        dispatch(batch)

    # MUTATION: retry on status 409 alone, without reading the subject.
    assert len(batch.calls) == 1
    assert raised.value.code == "gke_create_job_failed"


# --- through the drain loop -----------------------------------------------------------

@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    seed_tenant(store, TENANT, max_active=20, credentials=("anthropic", "openai"))
    seed_pool(store, "global", hard_limit=40)
    return store


def drain_once(db: FakeFirestore, batch: ScriptedBatch) -> Any:
    settings = scheduler_settings()
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=batch)
    router = BackendRouter(cloud_run=gke, gke=gke, settings=settings)
    scheduler = Scheduler(settings=settings, store=SchedulerStore(db), router=router,
                          metrics=SchedulerMetrics())
    seed_task(db, task_id="task_q", tenant_id=TENANT, runner_profile=PROFILE,
              resource_class=RUNNER_PROFILES[PROFILE].resource_class,
              provider=RUNNER_PROFILES[PROFILE].provider)
    return scheduler.drain()


def test_four_quota_conflicts_release_the_lease_and_the_task_waits_again(db, sleeps):
    batch = ScriptedBatch(*(quota_conflict() for _ in range(6)))

    report = drain_once(db, batch)

    task = db.docs["tasks/task_q"]
    (lease,) = [d for k, d in db.docs.items() if k.startswith("leases/")]
    assert report.leased == 1, report.to_dict()
    # MUTATION: let the persistent conflict escape as something other than a
    # DispatchError -- the drain raises instead of reporting a dispatch failure.
    assert report.dispatch_failures == 1, report.to_dict()
    # MUTATION: end the attempt FAILED (or the task) on a quota conflict while
    # retries remain -- the acceptance failure of 2026-10-23.
    assert task["state"] == TaskState.READY.value
    # MUTATION: keep the lease -- a slot held by no container (invariant 1).
    assert lease["released_at"] is not None
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs[f"pools/tenant:{TENANT}"]["active"] == 0
    # MUTATION: return the task with no backoff -- the next drain re-leases it
    # at once. The existing 30 s return path is what waits.
    assert task["next_eligible_at"] is not None
    # MUTATION: retry unboundedly inside the drain.
    assert len(batch.calls) == 4 and batch.created == []
