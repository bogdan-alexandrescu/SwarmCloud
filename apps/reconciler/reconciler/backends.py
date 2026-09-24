"""Reading and stopping real execution on the two backends.

Everything here is I/O against Google APIs, kept behind two small interfaces so
`detect.py` and `repair.py` can be exercised without a project. Two rules hold
across both implementations:

* **Nothing is touched unless this platform created it.** The project is shared
  with a live GKE cluster, a VPC and a dozen service accounts belonging to other
  teams. Every list is label-filtered on the `managed-by=swarm*` family and
  every delete re-checks the label on the object it is about to remove, because
  a list filter is a query and a re-check is a guarantee.
* **Termination is proven, not requested.** `terminate()` returns True only
  when the backend acknowledged the stop, or when the backend's own record of
  the execution says it has already finished -- never on the strength of having
  asked. The caller uses that return value to decide whether it may release the
  slot, and a slot released after an unproven termination is exactly the
  duplicate-execution bug this service exists to prevent. The second form of
  proof exists because Cloud Run reports a cancellation it carried out as a
  FAILED operation whenever the execution did not succeed; see `terminate`.

Two asymmetries here are deliberate, and both exist because being BLIND is far
more dangerous than seeing too much.

**Reading accepts the whole `managed-by=swarm*` family; deleting does not.**
Components stamp their own marker -- the dispatcher writes `swarm-scheduler`,
terraform writes `swarm-terraform`, the API writes `swarm-api`. A reconciler
that recognised only one of those would list zero executions, conclude that
every running task had no execution behind it, release its slot and let the
scheduler start a second agent on the same task: the exact duplicate-execution
failure this service exists to prevent, caused by a label typo. So reads accept
the family. Deletes are narrower -- only the runtime markers (`swarm`,
`swarm-scheduler`) -- because a `swarm-terraform` resource has an owner that
will simply recreate it, and fighting terraform is not this service's job.

**Identifiers are read from the environment first, labels second.** Label values
are sanitised to fit Kubernetes' and Cloud Run's character rules, so the task
`task_9f3a` appears in a label as `task-9f3a`. Matching that against a Firestore
document id would fail, and a failed match reads as "this execution belongs to
no task I know about" -- an orphan, which gets terminated. The container
environment carries the identifiers verbatim, so that is what is trusted, with
the label kept only as a hint that `detect` can resolve by sanitised comparison.

**The TENANT is the one identifier that is never taken from the container.**
Everything else in a pod spec is a hint; the tenant decides whose work this
service is allowed to fence, release and re-queue, and this service runs with an
identity that can do that to every tenant at once. A container environment is
written by whoever created the object, so a Job whose env claimed another
tenant's `TENANT_ID` and `TASK_ID` would be a cross-tenant kill switch operated
by a trusted identity. So the tenant comes from the enclosing object instead --
the Kubernetes namespace, or the Cloud Run Job resource's own label, both of
which the platform creates per tenant and neither of which a workload can edit
from inside itself. A container that disagrees is logged and overruled, and
`detect.scope_executions_to_their_tenant` then drops any claim on a task that
belongs to someone else.
"""

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Protocol

from .detect import sanitised
from .model import ExecutionPhase, ExecutionView, JobResourceView, as_datetime

MANAGED_LABEL = "managed-by"
#: The marker this service itself would stamp. Other components stamp their own.
MANAGED_VALUE = "swarm"

#: Every marker this platform's components stamp. Listed explicitly rather than
#: matched with a `swarm-*` glob so that a resource another team happens to
#: label `swarm-something` is not silently adopted by the reconciler.
MANAGED_VALUES: tuple[str, ...] = (
    MANAGED_VALUE,
    "swarm-scheduler",
    "swarm-api",
    "swarm-worker",
    "swarm-terraform",
    "swarm-bootstrap",
)

#: Markers whose resources the reconciler may DELETE. `swarm-terraform` is
#: absent on purpose: terraform owns those and will recreate them.
GC_MANAGED_VALUES: frozenset[str] = frozenset({"swarm", "swarm-scheduler", "swarm-worker"})

#: Both label spellings in use across the platform. The dispatcher writes the
#: short form; earlier drafts of this service assumed the long one. Reading both
#: costs one dict lookup and removes a whole class of false orphans.
TASK_LABELS = ("swarm-task-id", "swarm-task")
ATTEMPT_LABELS = ("swarm-attempt-id", "swarm-attempt")
TENANT_LABELS = ("swarm-tenant-id", "swarm-tenant")
GENERATION_LABELS = ("swarm-generation",)

TASK_LABEL = TASK_LABELS[0]
ATTEMPT_LABEL = ATTEMPT_LABELS[0]
TENANT_LABEL = TENANT_LABELS[0]
GENERATION_LABEL = GENERATION_LABELS[0]

#: Environment variables the dispatcher sets on every worker container. These
#: carry the identifiers verbatim, which labels cannot.
TASK_ENV = "TASK_ID"
ATTEMPT_ENV = "ATTEMPT_ID"
TENANT_ENV = "TENANT_ID"
GENERATION_ENV = "GENERATION"


def managed_marker(labels: dict[str, Any] | None) -> str | None:
    """The `managed-by` value, when it is one this platform recognises."""
    value = (labels or {}).get(MANAGED_LABEL)
    return value if value in MANAGED_VALUES else None


def is_swarm_managed(labels: dict[str, Any] | None) -> bool:
    """True for any resource this platform created. Used for READS."""
    return managed_marker(labels) is not None


def is_gc_eligible(labels: dict[str, Any] | None) -> bool:
    """True only for resources this service is allowed to DELETE."""
    return (managed_marker(labels) or "") in GC_MANAGED_VALUES


def is_namespace_gc_eligible(labels: dict[str, Any] | None) -> bool:
    """True for a per-tenant namespace that is the platform's to collect.

    The reconciler itself no longer collects namespaces -- a Namespace is
    cluster-scoped and it holds no ClusterRole (see
    `GkeBackend.list_job_resources`) -- so this is the rule any out-of-band
    deprovisioning must honour, kept beside the other ownership predicates and
    pinned against the rendered manifest by test_kubernetes_manifests.py.

    Namespaces get their own rule because they are the one platform resource
    whose `managed-by` marker says `swarm-terraform` while no terraform state
    contains it: `scripts/register-tenant.sh` creates it with kubectl and
    relabels it on every run, and there is no kubernetes provider anywhere in
    `terraform/`. Applying the Cloud Run rule here would make namespace
    collection dead code.

    What replaces it is a stronger pair of conditions: a recognised `managed-by`
    marker AND a `swarm-tenant` label, which only this platform ever sets. A
    namespace belonging to another team in this shared project can match neither.
    """
    labels = labels or {}
    return is_swarm_managed(labels) and bool(_first(labels, TENANT_LABELS))


def managed_label_selector() -> str:
    """A set-based Kubernetes selector covering the whole managed family."""
    return f"{MANAGED_LABEL} in ({','.join(MANAGED_VALUES)})"


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def owning_tenant(
    claimed: Any,
    authority: str | None,
    *,
    resource: str,
    logger: Any | None = None,
) -> str | None:
    """Reconcile a container-supplied tenant id against the enclosing object.

    `authority` is derived from something the workload cannot write from inside
    itself -- its namespace, or the label on the per-tenant Job resource it runs
    under. `claimed` is whatever the container's environment or its own labels
    say. The claim is accepted only when it names the same tenant the authority
    does, because the identifiers differ in shape: a namespace is
    `swarm-<tenant>` or `swarm-tenant-<tenant>` depending on which of the two
    dispatch paths created it, and a label has been through `sanitize_name`.

    When there is no authority (nothing per-tenant encloses the object) the
    claim is returned unchanged -- there is nothing to check it against, and
    `detect.scope_executions_to_their_tenant` still refuses to let it act on
    another tenant's task.
    """
    claim = str(claimed).strip() if claimed not in (None, "") else None
    if authority is None or authority == "":
        return claim
    if claim is None:
        return authority
    slug = sanitised(claim)
    if slug and (authority == slug or authority.endswith(f"-{slug}")):
        return claim
    if logger is not None:
        logger.warning(
            "execution claims a tenant its own namespace or job resource does not; "
            "using the enclosing object's tenant instead",
            resource=resource,
            claimed=claim,
            authority=authority,
        )
    return authority


class Backend(Protocol):
    """A backend that can be listed in one call: Cloud Run Jobs."""

    name: str

    def list_executions(self) -> list[ExecutionView]: ...
    def terminate(self, execution: ExecutionView) -> bool: ...
    def list_job_resources(self) -> list[JobResourceView]: ...
    def delete_job_resource(self, resource: JobResourceView) -> bool: ...


@dataclass(frozen=True)
class NamespacedListing:
    """What one pass could and could not see on a namespaced backend.

    Readability is PER NAMESPACE because that is the granularity at which
    Kubernetes grants it. The reconciler's only grant is the namespaced
    `swarm-reaper` Role (kubernetes/rbac/dispatcher-rbac.yaml), applied tenant by
    tenant, and Kubernetes AUTHORISES BEFORE IT RESOLVES -- so a registered
    tenant whose namespace was never provisioned answers 403, exactly like a
    namespace with no RoleBinding. With one boolean for the whole backend, that
    one missing namespace would blind the reconciler to every tenant on GKE.
    """

    executions: list[ExecutionView] = field(default_factory=list)
    #: Namespaces whose Job list came back. Only these support a conclusion
    #: about absence.
    readable: frozenset[str] = frozenset()
    #: namespace -> why it could not be read. Never a secret: see `_describe`.
    unreadable: dict[str, str] = field(default_factory=dict)

    @property
    def blind(self) -> bool:
        """True only when EVERY namespace this pass tried was unreadable."""
        return bool(self.unreadable) and not self.readable


class ProbeOutcome(str, Enum):
    """What a by-name read of one execution established."""

    #: The API answered 404 for a namespace this service may read. The Job
    #: does not exist, so nothing of it can be running.
    ABSENT = "absent"
    #: The Job carries a terminal condition (Complete or Failed).
    FINISHED = "finished"
    #: The Job exists and carries no terminal condition: treat it as running.
    ACTIVE = "active"
    #: Anything else -- 403, 401, a transport error, a Job that is not ours.
    #: Proves nothing either way.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Probe:
    outcome: ProbeOutcome
    execution: ExecutionView | None = None
    detail: str = ""


class NamespacedBackend(Protocol):
    """A backend read namespace by namespace: GKE Autopilot.

    There is no `list_executions()` here, on purpose. Listing the whole backend
    in one call is a cluster-scope request, and this platform grants no
    cluster-scope role to anybody (kubernetes/rbac/worker-rbac.yaml, closing
    note). The caller says which namespaces to read, from control-plane data.
    """

    name: str

    def namespace_for(self, tenant_id: str, recorded: str | None = None) -> str: ...
    def namespace_of(self, execution_name: str | None) -> str | None: ...
    def list_executions_in(self, namespaces: Iterable[str]) -> NamespacedListing: ...
    def probe(self, execution_name: str) -> Probe: ...
    def terminate(self, execution: ExecutionView) -> bool: ...
    def list_job_resources(self) -> list[JobResourceView]: ...
    def delete_job_resource(self, resource: JobResourceView) -> bool: ...


def _api_status(exc: BaseException) -> int | None:
    """The HTTP status a kubernetes client exception carries, if any."""
    status = getattr(exc, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _describe(exc: BaseException) -> str:
    """One line naming a failed read, for logs and the persisted report.

    The status and reason only, never `str(exc)` whole: a kubernetes
    ApiException renders its response headers and body, and this string is
    served to operators through the pass history.
    """
    status = _api_status(exc)
    reason = str(getattr(exc, "reason", "") or "").strip()
    if status is not None:
        return f"{status} {reason}".strip()
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}".strip()


def job_conditions(job: Any) -> dict[str, bool]:
    """Condition type -> whether it is True, from a Job object or a dict."""
    status = getattr(job, "status", None)
    if status is None and isinstance(job, dict):
        status = job.get("status")
    raw = getattr(status, "conditions", None)
    if raw is None and isinstance(status, dict):
        raw = status.get("conditions")
    found: dict[str, bool] = {}
    for condition in raw or []:
        kind = getattr(condition, "type", None)
        state = getattr(condition, "status", None)
        if kind is None and isinstance(condition, dict):
            kind, state = condition.get("type"), condition.get("status")
        if kind:
            found[str(kind)] = str(state) == "True"
    return found


def job_phase(job: Any) -> ExecutionPhase:
    """The phase of a batch/v1 Job, decided by its CONDITIONS.

    Not by `status.failed` or `status.succeeded`. Those are pod counters, and a
    counter can move before the Job is over: with `backoffLimit > 0` a failed
    pod is followed by a replacement, and on Kubernetes >= 1.31 a Job reports
    `SuccessCriteriaMet`/`FailureTarget` while its pods are still terminating.
    `Complete` and `Failed` are the Job controller's own statement that nothing
    of it is running any more, which is the only question that may release a
    slot. A Job with neither is treated as running -- the direction that holds
    the slot, or terminates before it releases.
    """
    conditions = job_conditions(job)
    if conditions.get("Complete"):
        return ExecutionPhase.SUCCEEDED
    if conditions.get("Failed"):
        return ExecutionPhase.FAILED
    return ExecutionPhase.RUNNING


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def execution_is_finished(execution: Any) -> bool:
    """True only when Cloud Run's own record proves this execution is over.

    A separate question from `_execution_view`'s phase, and deliberately so.
    The phase answers "what is this doing", and its `UNKNOWN` bucket is where
    shapes nobody has characterised land; this answers "is the compute gone",
    which is the only question that may release a slot. Reusing the phase would
    quietly let that bucket release slots too.

    All three conditions are required, and each rules out a shape seen on a real
    execution rather than an imagined one:

    * `completion_time` set -- the field Cloud Run writes when an execution
      reaches a terminal state. `swarm-job-eng-claude-code-sq2l6` read back
      `completion_time=2026-09-22T04:00:14Z`, `cancelled_count=1`,
      `running_count=0`, `reconciling=False`; the healthy
      `swarm-job-eng-claude-code-vn5mp` read the same shape with
      `succeeded_count=1`. Only the counts differ, which is why the counts are
      not what is tested: this must confirm a stop, not a success.
    * `running_count == 0` -- an execution with `task_count > 1` can post a
      terminal count for one task while another is still going, and the agent in
      that task is still holding the credential.
    * not `reconciling` -- Cloud Run is still acting on the resource, so its
      counters are mid-flight. sq2l6 grew an `ImmediateRetry` Retry condition at
      04:00:19, five seconds AFTER its completionTime; `maxRetries: 0` meant
      nothing came of it, but a profile with retries would have restarted the
      container under a completion time that was already set.

    An object missing these fields reads as not finished, which is the direction
    that holds the slot.
    """
    if as_datetime(getattr(execution, "completion_time", None)) is None:
        return False
    if int(getattr(execution, "running_count", 0) or 0) > 0:
        return False
    return not bool(getattr(execution, "reconciling", False))


# ---------------------------------------------------------------------------
# Cloud Run Jobs
# ---------------------------------------------------------------------------


class CloudRunBackend:
    """Cloud Run Jobs: the primary backend.

    The dispatcher creates one Job resource per (tenant, runner profile) because
    Cloud Run pins the service account on the Job, not the execution -- so the
    per-tenant identity boundary requires per-tenant Job resources, and this
    class is what stops them accumulating forever.
    """

    name = "CLOUD_RUN_JOB"

    def __init__(
        self,
        project_id: str,
        region: str,
        *,
        job_name_prefix: str = "swarm-",
        executions_client: Any | None = None,
        jobs_client: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self._project_id = project_id
        self._region = region
        self._prefix = job_name_prefix
        self._executions = executions_client
        self._jobs = jobs_client
        self._log = logger

    @property
    def parent(self) -> str:
        return f"projects/{self._project_id}/locations/{self._region}"

    def _executions_client(self) -> Any:
        if self._executions is None:
            from google.cloud import run_v2  # lazy

            self._executions = run_v2.ExecutionsClient()
        return self._executions

    def _jobs_client(self) -> Any:
        if self._jobs is None:
            from google.cloud import run_v2  # lazy

            self._jobs = run_v2.JobsClient()
        return self._jobs

    # -- reads ----------------------------------------------------------
    def list_executions(self) -> list[ExecutionView]:
        views: list[ExecutionView] = []
        for job in self._list_jobs():
            job_name = getattr(job, "name", "")
            if not self._is_managed(job):
                continue
            # The Job resource is created per (tenant, profile) by the
            # dispatcher, and Cloud Run pins the service account on it -- so its
            # tenant label is the identity the executions under it actually run
            # as, and is what an execution's own claim is checked against.
            owner = _first(self._labels(job), TENANT_LABELS)
            for execution in self._executions_client().list_executions(parent=job_name):
                views.append(
                    self._execution_view(
                        execution, job_name, job_tenant=str(owner) if owner else None
                    )
                )
        return views

    def _list_jobs(self) -> list[Any]:
        return [
            job
            for job in self._jobs_client().list_jobs(parent=self.parent)
            if str(getattr(job, "name", "")).rsplit("/", 1)[-1].startswith(self._prefix)
        ]

    @staticmethod
    def _labels(resource: Any) -> dict[str, Any]:
        return dict(getattr(resource, "labels", {}) or {})

    def _is_managed(self, resource: Any) -> bool:
        """Visible to this service: any marker in the platform family."""
        return is_swarm_managed(self._labels(resource))

    def _is_gc_eligible(self, resource: Any) -> bool:
        """Deletable by this service: runtime markers only."""
        return is_gc_eligible(self._labels(resource))

    def _execution_view(
        self, execution: Any, job_name: str, *, job_tenant: str | None = None
    ) -> ExecutionView:
        labels = dict(getattr(execution, "labels", {}) or {})
        env = self._env_from_template(execution)
        running = int(getattr(execution, "running_count", 0) or 0)
        cancelled = int(getattr(execution, "cancelled_count", 0) or 0)
        succeeded = int(getattr(execution, "succeeded_count", 0) or 0)
        failed = int(getattr(execution, "failed_count", 0) or 0)
        completion = as_datetime(getattr(execution, "completion_time", None))

        if running > 0 and completion is None:
            phase = ExecutionPhase.RUNNING
        elif cancelled > 0:
            phase = ExecutionPhase.CANCELLED
        elif succeeded > 0:
            phase = ExecutionPhase.SUCCEEDED
        elif failed > 0:
            phase = ExecutionPhase.FAILED
        elif completion is None:
            phase = ExecutionPhase.RUNNING      # created, not yet reporting
        else:
            phase = ExecutionPhase.UNKNOWN

        # Environment first: it carries the identifiers verbatim, while a label
        # value has been through `sanitize_name` and no longer matches the
        # Firestore document id it came from.
        return ExecutionView(
            name=str(getattr(execution, "name", "")),
            backend=self.name,
            phase=phase,
            created_at=as_datetime(getattr(execution, "create_time", None)),
            task_id=env.get(TASK_ENV) or _first(labels, TASK_LABELS),
            attempt_id=env.get(ATTEMPT_ENV) or _first(labels, ATTEMPT_LABELS),
            tenant_id=owning_tenant(
                env.get(TENANT_ENV) or _first(labels, TENANT_LABELS),
                job_tenant,
                resource=str(getattr(execution, "name", "")),
                logger=self._log,
            ),
            generation=_int_or_none(env.get(GENERATION_ENV) or _first(labels, GENERATION_LABELS)),
            parent=job_name,
        )

    @staticmethod
    def _env_from_template(execution: Any) -> dict[str, str]:
        """Identifiers the dispatcher passed as env overrides, not labels.

        Label values cannot hold every id shape, so the identifiers are read
        from both places and whichever is present wins.
        """
        env: dict[str, str] = {}
        template = getattr(execution, "template", None)
        for container in getattr(template, "containers", []) or []:
            for entry in getattr(container, "env", []) or []:
                name = getattr(entry, "name", None)
                value = getattr(entry, "value", None)
                if name and isinstance(value, str):
                    env[name] = value
        return env

    # -- writes ---------------------------------------------------------
    def terminate(self, execution: ExecutionView) -> bool:
        """Stop the execution and return whether the stop is PROVEN.

        Two kinds of proof, and only two.

        **The cancel was acknowledged for this execution.** Only an operation
        whose result names this exact execution counts. `bool(result)` would
        not: a protobuf is truthy whenever it carries any field at all, so
        falling back to it reports success for essentially any completed
        operation -- including one that returned a different execution, which is
        precisely the case the name comparison exists to catch. `repair.py`
        gates the lease release on this boolean, so a false True releases a slot
        while the first agent may still be running: two agents, one task, one
        credential, one repository.

        **Or Cloud Run's own record says the execution is over.** A `NotFound`
        is the extreme of that: the execution this service was asked to end does
        not exist any more. `_confirmed_finished` is the rest of it, and it
        exists because Cloud Run does NOT distinguish "the cancellation failed"
        from "the execution did not succeed". Every failed cancel seen on
        saga-agents-staging was the second thing:

        * `400 Execution 'swarm-job-u-bogdan-mock-w8g2g' cannot be cancelled
          because it is not running.` -- 16 of these in 1.997s on 2026-09-20,
          because the executions finished between the list and the cancel. That
          pass (pass_0bd239d41f79479385cf) found 17 dead workers and released
          exactly one: sixteen leases held on a read-then-act race.
        * `409 Task swarm-verify-dc9fl-task0 failed with exit code: 1` -- the
          LRO carrying the task's own failure as operation error code 10.
        * `None Unspecified error. 2: Unspecified error.` --
          swarm-job-eng-claude-code-sq2l6 on 2026-09-22. Its CancelExecution
          audit entry reads `status { code: 2, message: "Execution
          swarm-job-eng-claude-code-sq2l6 has failed to complete, 0/1 tasks were
          a success." }` and carries, in the same payload, the execution with
          cancelledCount 1 and completionTime set. The cancellation worked.

        So an unacknowledged cancel is not evidence of anything and the
        execution is re-read instead. A GET that reports it finished is STRONGER
        evidence than an ack: an ack says Cloud Run accepted a request, a
        terminal execution says the compute is gone. Nothing else changes -- if
        the re-read cannot prove termination the original error is re-raised,
        `repair.py` logs it and the slot stays held, which is the outcome this
        module exists to produce.
        """
        from google.api_core import exceptions as gapi_exceptions
        from google.cloud import run_v2

        request = run_v2.CancelExecutionRequest(name=execution.name)
        try:
            operation = self._executions_client().cancel_execution(request=request)
            result = operation.result(timeout=120)
        except gapi_exceptions.NotFound:
            if self._log:
                self._log.info(
                    "cloud run execution already gone", execution=execution.name, confirmed=True
                )
            return True
        except Exception as exc:
            # Re-raised rather than turned into False when the proof fails, so
            # `repair.py`'s "termination failed" carries the real cause. A bare
            # False would report the same refusal with nothing to act on.
            if self._confirmed_finished(execution, cause=exc):
                return True
            raise
        if getattr(result, "name", None) == execution.name:
            if self._log:
                self._log.info(
                    "cloud run execution cancelled", execution=execution.name, confirmed=True
                )
            return True
        returned = str(getattr(result, "name", "")) or None
        if self._confirmed_finished(
            execution, cause=f"cancel acknowledged {returned!r}, not this execution"
        ):
            return True
        if self._log:
            self._log.error(
                "cloud run did not confirm the cancellation; NOT releasing the slot",
                execution=execution.name,
                returned=returned,
                confirmed=False,
            )
        return False

    def _confirmed_finished(self, execution: ExecutionView, *, cause: Any) -> bool:
        """Re-read the execution and say whether Cloud Run reports it finished.

        The read is what makes this safe to act on, so a read that does not
        happen, or does not prove termination, returns False every time. There
        is no inference here: no timeout heuristic, no "it was probably the
        cancel", nothing derived from `cause`, which is carried only so the log
        line says which unacknowledged cancel this is answering.
        """
        from google.api_core import exceptions as gapi_exceptions

        try:
            current = self._executions_client().get_execution(name=execution.name)
        except gapi_exceptions.NotFound:
            if self._log:
                self._log.info(
                    "cloud run execution is gone; treating the cancellation as confirmed",
                    execution=execution.name,
                    cause=str(cause),
                    confirmed=True,
                )
            return True
        except Exception as exc:
            # Cannot see it, so cannot claim it stopped. Same rule as an
            # unreadable backend in `repair._is_actionable`.
            if self._log:
                self._log.error(
                    "could not re-read the execution after an unconfirmed cancellation",
                    execution=execution.name,
                    cause=str(cause),
                    error=str(exc),
                    confirmed=False,
                )
            return False
        if not execution_is_finished(current):
            if self._log:
                self._log.error(
                    "cloud run still reports this execution as running; NOT releasing the slot",
                    execution=execution.name,
                    cause=str(cause),
                    running_count=int(getattr(current, "running_count", 0) or 0),
                    reconciling=bool(getattr(current, "reconciling", False)),
                    confirmed=False,
                )
            return False
        if self._log:
            self._log.info(
                "cloud run reports this execution finished; the stop is confirmed",
                execution=execution.name,
                cause=str(cause),
                cancelled_count=int(getattr(current, "cancelled_count", 0) or 0),
                succeeded_count=int(getattr(current, "succeeded_count", 0) or 0),
                failed_count=int(getattr(current, "failed_count", 0) or 0),
                confirmed=True,
            )
        return True

    def list_job_resources(self) -> list[JobResourceView]:
        resources: list[JobResourceView] = []
        for job in self._list_jobs():
            if not self._is_managed(job):
                continue
            labels = self._labels(job)
            executions = list(self._executions_client().list_executions(parent=job.name))
            active = sum(
                1 for e in executions if self._execution_view(e, job.name).is_active
            )
            last = max(
                (as_datetime(getattr(e, "create_time", None)) for e in executions),
                default=None,
            )
            resources.append(
                JobResourceView(
                    name=str(job.name),
                    tenant_id=_first(labels, TENANT_LABELS),
                    runner_profile=_first(labels, ("swarm-runner-profile", "swarm-profile")),
                    created_at=as_datetime(getattr(job, "create_time", None)),
                    last_execution_at=last,
                    # `managed` here means "this service may delete it", which is
                    # the only question the GC asks of the flag.
                    managed=self._is_gc_eligible(job),
                    active_executions=active,
                )
            )
        return resources

    def delete_job_resource(self, resource: JobResourceView) -> bool:
        if not resource.managed:
            raise PermissionError(
                f"refusing to delete unmanaged Cloud Run job {resource.name}: "
                f"{MANAGED_LABEL} is not one of {sorted(GC_MANAGED_VALUES)}"
            )
        # Re-read and re-check the label: the list that produced this view may
        # be seconds old, and this project is shared with other teams.
        job = self._jobs_client().get_job(name=resource.name)
        if not self._is_gc_eligible(job):
            raise PermissionError(
                f"job {resource.name} does not carry a deletable {MANAGED_LABEL} "
                f"marker; refusing delete"
            )
        operation = self._jobs_client().delete_job(name=resource.name)
        operation.result(timeout=120)
        if self._log:
            self._log.info("cloud run job resource deleted", job=resource.name)
        return True


# ---------------------------------------------------------------------------
# GKE Autopilot
# ---------------------------------------------------------------------------


def gke_api_host(endpoint: str) -> str:
    """The `host` a kubernetes client Configuration needs for this cluster.

    GKE_ENDPOINT is `google_container_cluster.endpoint` passed straight through
    by terraform/infra/locals.tf, and that attribute is a BARE address --
    `34.118.229.12`, no scheme. The kubernetes client concatenates
    `configuration.host` with the resource path and hands the result to urllib3,
    which needs a scheme to choose a connection pool at all: without it the GKE
    backend is unreadable on every pass, and a reconciler that cannot see a
    backend refuses to act on anything it might hold.

    This function is duplicated, deliberately: the identical body lives in
    `scheduler.dispatch`. The two services are separate images that share only
    the FROZEN `swarm_common` package -- images/swarm-reconciler/Dockerfile
    copies `apps/common/` and `apps/reconciler/` and nothing else -- so neither
    can import the other and there is nowhere in-image to put a single copy.
    `tests/unit/control_plane/test_gke_client_host.py` pins the two together by
    asserting they agree on the same inputs, which is exactly the check that was
    missing while they did not: this side added the scheme and the scheduler,
    which dispatches to the very same cluster, did not.
    """
    raw = (endpoint or "").strip()
    if not raw:
        # The caller treats "unset" as "not configured for out-of-cluster use"
        # and falls back to in-cluster config; it must not become "https://".
        return ""
    scheme, separator, rest = raw.partition("://")
    if not separator:
        rest = raw
    elif scheme.lower() != "https":
        # The client puts a cloud-platform OAuth token in an Authorization
        # header on every call; any other scheme puts it on the wire in clear.
        raise ValueError(
            "GKE_ENDPOINT must be a bare host or an https:// URL; "
            f"{scheme}:// would send the bearer token in clear text"
        )
    rest = rest.rstrip("/")
    if not rest:
        raise ValueError(f"GKE_ENDPOINT is not a usable host: {endpoint!r}")
    return f"https://{rest}"


def google_bearer_token(credentials: Any) -> str:
    """The access token for the NEXT Kubernetes API call, refreshed if stale.

    A Google OAuth access token lives about an hour, and on Cloud Run often much
    less: the metadata server hands out whatever remains of the token it has
    cached, so a first mint can come back with half an hour of life. That is
    fine for a one-shot job and fatal for this service, because a single
    kubernetes client is built once and then lives as long as the process --
    `service.create_app` keeps the Reconciler (and therefore this backend) in
    app state, and `run.googleapis.com/cpu-throttling: 'false'` plus a
    five-minute Cloud Scheduler tick keeps the instance warm for hours.

    That is not a theoretical decay. swarm-reconciler instance 00a41e8c started
    at 2026-09-22T00:18:03Z and minted its token on the first pass at 00:20:24Z;
    every GKE list from 00:55:08Z onwards returned 401 Unauthorized, for 3h50m
    and 40 consecutive passes, on that same process. Nothing in the failure said
    "token": a 401 from the Kubernetes API reads the same whether the bearer was
    absent, malformed or merely dead, and the earlier passes on that instance had
    returned 403, so the log looked like an IAM problem that had somehow got
    worse. Meanwhile `repair.run_once` was correctly refusing to act on a backend
    it could not read, so every GKE workload went unreconciled -- a stuck
    browser-profile task would have held its lease and its capacity forever.

    So: no token is ever cached past its own expiry. `Credentials.valid` is
    google.auth's own answer to "would this token still be accepted", already
    carrying google.auth's refresh threshold, so a token this returns had time
    left when the request was built. Credentials that cannot say when they
    expire are refreshed on every call: minting one more token costs a metadata
    round trip, and serving one dead token costs a reconciliation pass.
    """
    import google.auth.transport.requests

    expiry = getattr(credentials, "expiry", None)
    if expiry is None or not getattr(credentials, "valid", False):
        credentials.refresh(google.auth.transport.requests.Request())
    token = getattr(credentials, "token", None)
    if not token:
        # Never the token itself, here or anywhere: this message reaches the
        # reconciliation report, which the API serves to operators.
        raise RuntimeError(
            "google.auth returned no access token for the GKE API; "
            "the cluster cannot be read without one"
        )
    return str(token)


def install_google_bearer_token(configuration: Any, credentials: Any) -> None:
    """Give a kubernetes Configuration a token it refreshes for every request.

    `refresh_api_key_hook` is the kubernetes client's own extension point for
    exactly this, and it is called on every single request:
    `ApiClient.update_params_for_auth` -> `Configuration.auth_settings` ->
    `get_api_key_with_prefix("authorization")` -> the hook. Hooking it is what
    lets the client, its connection pool and the CA file on disk stay cached for
    the life of the process while the credential does not.

    This function is duplicated in `scheduler.dispatch`, deliberately and for
    the same reason `gke_api_host` is: the two services ship as separate images
    that share only the FROZEN `swarm_common` package, so neither can import the
    other and there is nowhere in-image to put a single copy.
    `tests/unit/control_plane/test_gke_client_auth.py` pins the two copies
    together, by running the same expiring credential through both and asserting
    the same sequence of headers comes out.
    """

    def refresh(config: Any) -> None:
        config.api_key["authorization"] = f"Bearer {google_bearer_token(credentials)}"

    # Refused, not assumed. A Configuration that does not KNOW about the hook
    # would accept the attribute and never call it -- silently restoring the
    # one-shot token this replaces, with no test and no log line to show for it.
    # Both images resolve `kubernetes>=30` at build time rather than from a
    # lock, and this repository has already lost a working check to a client
    # version that quietly ignored what it was handed.
    if not hasattr(configuration, "refresh_api_key_hook"):
        raise RuntimeError(
            "this kubernetes client Configuration has no refresh_api_key_hook, "
            "so a GKE access token cannot be refreshed per request"
        )
    # Seeded as well as hooked. `auth_settings()` only asks for a bearer token
    # when `api_key` ALREADY holds an "authorization" entry, so a Configuration
    # carrying the hook alone would send no Authorization header at all -- and
    # the API server answers a missing bearer with the very same 401 this is
    # here to stop.
    configuration.api_key = {}
    refresh(configuration)
    configuration.refresh_api_key_hook = refresh


@dataclass
class GkeConnection:
    endpoint: str
    ca_cert_path: str


class GkeBackend:
    """GKE Autopilot: browser and oversized profiles.

    The reconciler runs on Cloud Run, not in the cluster, so it authenticates
    with its own service-account token against the cluster endpoint. The CA
    certificate arrives base64-encoded in the environment (Terraform emits it),
    which avoids a Container API round trip on every pass.

    EVERY CALL HERE IS NAMESPACED, and that is the fix for an outage, not a
    style. This class used to list with `list_job_for_all_namespaces` and
    `list_namespace`, both CLUSTER-scope. The reconciler's only Kubernetes grant
    is the namespaced `swarm-reaper` Role, and no ClusterRole exists anywhere in
    this repository by policy (kubernetes/rbac/worker-rbac.yaml, closing note),
    so the Job list answered 403 -- first on 2026-09-19, then on every pass
    from 2026-09-22T04:45Z, 766 passes by the 24th:
    `jobs.batch is forbidden: User "108023754768362642341" cannot list resource
    "jobs" ... at the cluster scope`. `repair.run_once` then treated GKE as
    unreadable and correctly refused to conclude that anything on it was dead,
    so the one component able to release a dead GKE task's lease never did. On
    2026-09-24 that stranded five leases, held 10 units on every pool, and left
    workflow wf_ebb3ab2d65664707a559 RUNNING with its cancel ignored for hours.

    So the namespace set comes from the control plane -- the registered tenants,
    named by the same rule the dispatcher uses, plus the namespace recorded in
    every live GKE attempt's `execution_name` -- and each is read with
    `list_namespaced_job`, which the Role grants.
    """

    name = "GKE_AUTOPILOT"

    def __init__(
        self,
        *,
        # `swarm-tenant-`, not `swarm-`, and the difference is not cosmetic:
        # this prefix is SLICED OFF a namespace name to recover a tenant id
        # (`namespace[len(self._prefix):]`, twice below) whenever the
        # `swarm-tenant` label is missing. The short spelling turned
        # `swarm-tenant-eng` into the tenant `tenant-eng`, so a finding about a
        # real orphan was filed against a tenant that does not exist. See
        # `ReconcilerConfig.namespace_prefix` for the outage this spelling comes
        # from, and `scripts/lib/check-contract-parity.sh` section 6 for what
        # now holds every copy of it together.
        namespace_prefix: str = "swarm-tenant-",
        batch_api: Any | None = None,
        core_api: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self._prefix = namespace_prefix
        self._batch = batch_api
        self._core = core_api
        self._log = logger
        self._ca_file: str | None = None
        self._gc_notice_logged = False

    # -- client ---------------------------------------------------------
    def _configure(self) -> None:
        # Only the batch client is used: every read and write here is a
        # namespaced Job call. `_core` is still built for callers that inspect it.
        if self._batch is not None:
            return
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        endpoint = os.environ.get("GKE_ENDPOINT", "").strip()
        ca_b64 = os.environ.get("GKE_CA_CERT_B64", "").strip()
        if endpoint and ca_b64:
            if self._ca_file is None:
                handle = tempfile.NamedTemporaryFile(
                    prefix="gke-ca-", suffix=".crt", delete=False
                )
                handle.write(base64.b64decode(ca_b64))
                handle.close()
                self._ca_file = handle.name
            import google.auth

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            configuration = k8s_client.Configuration()
            configuration.host = gke_api_host(endpoint)
            configuration.ssl_ca_cert = self._ca_file
            # Per request, not once. `self._batch`/`self._core` below are cached
            # for the life of the process and `_configure` returns early on
            # every later pass, so a token minted here would be the token every
            # subsequent call carried until the instance died.
            install_google_bearer_token(configuration, credentials)
            api_client = k8s_client.ApiClient(configuration)
        else:
            k8s_config.load_incluster_config()
            api_client = k8s_client.ApiClient()
        self._batch = k8s_client.BatchV1Api(api_client)
        self._core = k8s_client.CoreV1Api(api_client)

    @property
    def label_selector(self) -> str:
        """Set-based, so every marker in the managed family is listed.

        An equality selector on one value is how this service goes blind: the
        dispatcher stamps `swarm-scheduler`, and a selector pinned to `swarm`
        would return an empty list of Jobs for a cluster full of running agents.
        """
        return managed_label_selector()

    @staticmethod
    def _pod_env(job: Any) -> dict[str, str]:
        """Identifiers from the worker container's environment.

        Read verbatim, unlike labels, which `sanitize_name` has already rewritten
        by the time they reach the API server.
        """
        env: dict[str, str] = {}
        spec = getattr(job, "spec", None)
        template = getattr(spec, "template", None)
        pod_spec = getattr(template, "spec", None)
        containers = getattr(pod_spec, "containers", None) or []
        for container in containers:
            for entry in getattr(container, "env", None) or []:
                name = getattr(entry, "name", None)
                value = getattr(entry, "value", None)
                if name is None and isinstance(entry, dict):
                    name, value = entry.get("name"), entry.get("value")
                if name and isinstance(value, str):
                    env.setdefault(name, value)
        return env

    # -- naming -------------------------------------------------------
    def namespace_for(self, tenant_id: str, recorded: str | None = None) -> str:
        """The namespace the dispatcher creates this tenant's Jobs in.

        The SAME rule as `scheduler.dispatch.GkeJobDispatcher.namespace_for`,
        in the same order: the `namespace` the tenant document records wins, and
        only a tenant with none falls back to prefix + tenant id, sanitised. The
        precedence is the part that matters -- the dispatcher prefers the
        recorded value over its own template, so a reconciler that derived the
        name from the prefix alone would read one namespace while the dispatcher
        wrote into another, and every task there would look abandoned.

        The prefix is not restated here: it is the one this backend was built
        with, `ReconcilerConfig.namespace_prefix`, which
        `scripts/lib/check-contract-parity.sh` section 6 holds to the
        dispatcher's template. The two services ship as separate images that
        share only the frozen `swarm_common`, so neither can import the other's
        copy of this rule; `tests/unit/control_plane/
        test_reconciler_gke_namespaced.py` pins the two together on the same
        tenants instead, the way `test_gke_client_host.py` pins `gke_api_host`.
        """
        if recorded:
            return str(recorded)
        return sanitised(f"{self._prefix}{tenant_id}")

    def namespace_of(self, execution_name: str | None) -> str | None:
        """The namespace part of a GKE attempt's `execution_name`.

        The dispatcher records `<namespace>/<job>` (`GkeJobDispatcher.dispatch`),
        so this is where THIS attempt's Job was created -- a better answer than
        any derivation from the tenant, because it is what actually happened.
        """
        if not execution_name or "/" not in str(execution_name):
            return None
        namespace = str(execution_name).split("/", 1)[0].strip()
        return namespace or None

    # -- reads ----------------------------------------------------------
    def list_executions_in(self, namespaces: Iterable[str]) -> NamespacedListing:
        """Every platform Job in the named namespaces, and which could be read.

        One `list_namespaced_job` per namespace, each in its own try: a 403 on
        one tenant's namespace -- a missing namespace, a missing RoleBinding --
        costs the findings in that namespace and nothing else.

        A namespace outside `self._prefix` is never read. The prefix is what
        lets a namespace name be turned back into a tenant id (the authority
        `owning_tenant` checks a container's claim against), so a namespace
        without it cannot be attributed safely. It is reported as unreadable,
        which is the direction that holds a lease rather than releasing one.
        """
        wanted = sorted({str(ns).strip() for ns in namespaces if ns and str(ns).strip()})
        if not wanted:
            # Nothing on this backend is the control plane's; nothing to read,
            # and no client to build for it.
            return NamespacedListing()
        self._configure()
        views: list[ExecutionView] = []
        readable: set[str] = set()
        unreadable: dict[str, str] = {}
        for namespace in wanted:
            if not namespace.startswith(self._prefix):
                unreadable[namespace] = (
                    f"outside the tenant namespace prefix {self._prefix!r}; not read"
                )
                continue
            try:
                jobs = self._batch.list_namespaced_job(
                    namespace=namespace, label_selector=self.label_selector
                )
            except Exception as exc:
                unreadable[namespace] = _describe(exc)
                continue
            readable.add(namespace)
            for job in getattr(jobs, "items", []) or []:
                view = self._job_view(job, namespace)
                if view is not None:
                    views.append(view)
        return NamespacedListing(
            executions=views, readable=frozenset(readable), unreadable=unreadable
        )

    def probe(self, execution_name: str) -> Probe:
        """Read ONE Job by name, for when its namespace could not be listed.

        A namespaced `get`, which the `swarm-reaper` Role grants alongside
        `list`. It exists because "I could not list this namespace" and "I
        cannot see this Job" are different claims: RBAC can allow one verb and
        not the other, and a list can fail transiently where a single get does
        not. What each answer proves:

        * 404 -- the API server authorised the request and then found nothing.
          Kubernetes AUTHORISES BEFORE IT RESOLVES, so a 404 is only ever
          reached for a namespace this identity may read: absence confirmed.
        * a Job with a Complete or Failed condition -- finished; see `job_phase`.
        * a Job with neither -- running, as far as anyone can prove.
        * 403 (a missing namespace or RoleBinding), 401, anything else --
          nothing is proven, and the caller must go on holding the slot.
        """
        namespace = self.namespace_of(execution_name)
        name = str(execution_name).split("/", 1)[1].strip() if namespace else ""
        if not namespace or not name:
            return Probe(ProbeOutcome.UNREADABLE, detail="not a <namespace>/<job> name")
        if not namespace.startswith(self._prefix):
            return Probe(
                ProbeOutcome.UNREADABLE,
                detail=f"{namespace} is outside the tenant namespace prefix {self._prefix!r}",
            )
        try:
            self._configure()
            job = self._batch.read_namespaced_job(name=name, namespace=namespace)
        except Exception as exc:
            if _api_status(exc) == 404:
                return Probe(ProbeOutcome.ABSENT, detail=f"{namespace}/{name}: 404")
            return Probe(ProbeOutcome.UNREADABLE, detail=f"{namespace}/{name}: {_describe(exc)}")
        view = self._job_view(job, namespace)
        if view is None:
            # It exists but is not ours by label, or sits in another namespace
            # than the one asked for. Not something this service may act on.
            return Probe(
                ProbeOutcome.UNREADABLE,
                detail=f"{namespace}/{name}: not a platform-managed Job",
            )
        if view.phase is ExecutionPhase.RUNNING:
            return Probe(ProbeOutcome.ACTIVE, execution=view, detail=f"{namespace}/{name}: active")
        return Probe(
            ProbeOutcome.FINISHED,
            execution=view,
            detail=f"{namespace}/{name}: {view.phase.value.lower()}",
        )

    def _job_view(self, job: Any, namespace: str) -> ExecutionView | None:
        """One Job as an ExecutionView, or None when it is not ours to judge."""
        metadata = getattr(job, "metadata", None)
        if metadata is None:
            return None
        # The namespace the API returned it from, not merely the one asked for:
        # a list scoped to one namespace cannot return another, and a Job that
        # claims otherwise is not something to attribute.
        if str(getattr(metadata, "namespace", "") or namespace) != namespace:
            return None
        labels = dict(getattr(metadata, "labels", None) or {})
        if not is_swarm_managed(labels):
            return None
        env = self._pod_env(job)
        return ExecutionView(
            name=str(metadata.name),
            backend=self.name,
            phase=job_phase(job),
            created_at=_ensure_utc(getattr(metadata, "creation_timestamp", None)),
            task_id=env.get(TASK_ENV) or _first(labels, TASK_LABELS),
            attempt_id=env.get(ATTEMPT_ENV) or _first(labels, ATTEMPT_LABELS),
            # The namespace is per-tenant and a workload cannot move itself
            # between namespaces, so it -- not the container's own environment
            # -- decides whose task this execution is allowed to be about.
            tenant_id=owning_tenant(
                env.get(TENANT_ENV) or _first(labels, TENANT_LABELS),
                namespace[len(self._prefix):],
                resource=f"{namespace}/{metadata.name}",
                logger=self._log,
            ),
            generation=_int_or_none(env.get(GENERATION_ENV) or _first(labels, GENERATION_LABELS)),
            namespace=namespace,
        )

    def list_job_resources(self) -> list[JobResourceView]:
        """Nothing: namespace collection needs a grant this platform refuses.

        A Namespace is a cluster-scoped object. Listing, reading and deleting
        one all need a ClusterRole, and none exists here by policy
        (kubernetes/rbac/worker-rbac.yaml, closing note). This method used to
        call `list_namespace` anyway, which answered 403 on every pass; before
        that 403 surfaced it was hidden behind the Job listing's own, so empty
        tenant namespaces have never actually been collected by this service.

        What is lost is small and stated rather than papered over: an empty
        namespace of a DEREGISTERED tenant stays until it is removed by hand. A
        registered tenant's namespace was never collectable anyway -- it holds
        the tenant's service account and Workload Identity binding, which nothing
        in the dispatch path recreates. Per-tenant namespaces are created out of
        band by `scripts/register-tenant.sh`, and the same out-of-band path is
        what should remove them.
        """
        if self._log and not self._gc_notice_logged:
            self._gc_notice_logged = True
            self._log.info(
                "gke namespace collection not performed: namespaces are cluster-scoped "
                "and the reconciler holds no ClusterRole by policy"
            )
        return []

    # -- writes ---------------------------------------------------------
    def terminate(self, execution: ExecutionView) -> bool:
        self._configure()
        from kubernetes.client import V1DeleteOptions
        from kubernetes.client.rest import ApiException

        try:
            self._batch.delete_namespaced_job(
                name=execution.name,
                namespace=execution.namespace,
                body=V1DeleteOptions(
                    # Background propagation removes the pods too; without it the
                    # agent container keeps running under an orphaned pod.
                    propagation_policy="Background",
                    grace_period_seconds=30,
                ),
            )
        except ApiException as exc:
            if exc.status == 404:
                return True          # already gone is the outcome we wanted
            raise
        if self._log:
            self._log.info(
                "k8s job deleted", job=execution.name, namespace=execution.namespace
            )
        return True

    def delete_job_resource(self, resource: JobResourceView) -> bool:
        """Refused, without a call: deleting a Namespace is cluster-scope.

        `list_job_resources` offers nothing to delete, so the collector never
        reaches this. It refuses rather than trying because the attempt would be
        a cluster-scope request this identity is not granted -- a guaranteed 403
        that would read, in the pass history, like a permissions fault to fix.
        `repair._collect_garbage` records a PermissionError as a refusal.
        """
        raise PermissionError(
            f"refusing to delete namespace {resource.name}: a Namespace is cluster-scoped "
            f"and the reconciler holds no ClusterRole (kubernetes/rbac/worker-rbac.yaml); "
            f"deprovision tenant namespaces out of band"
        )


def _ensure_utc(value: Any) -> datetime | None:
    parsed = as_datetime(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
