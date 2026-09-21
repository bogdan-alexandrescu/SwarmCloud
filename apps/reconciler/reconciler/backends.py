"""Reading and stopping real execution on the two backends.

Everything here is I/O against Google APIs, kept behind two small interfaces so
`detect.py` and `repair.py` can be exercised without a project. Two rules hold
across both implementations:

* **Nothing is touched unless this platform created it.** The project is shared
  with a live GKE cluster, a VPC and a dozen service accounts belonging to other
  teams. Every list is label-filtered on the `managed-by=swarm*` family and
  every delete re-checks the label on the object it is about to remove, because
  a list filter is a query and a re-check is a guarantee.
* **Termination is confirmed, not requested.** `terminate()` returns True only
  when the backend acknowledged it. The caller uses that return value to decide
  whether it may release the slot, and a slot released after an unconfirmed
  termination is exactly the duplicate-execution bug this service exists to
  prevent.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

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
    """True for a per-tenant namespace this service may collect when empty.

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
    name: str

    def list_executions(self) -> list[ExecutionView]: ...
    def terminate(self, execution: ExecutionView) -> bool: ...
    def list_job_resources(self) -> list[JobResourceView]: ...
    def delete_job_resource(self, resource: JobResourceView) -> bool: ...


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        """Cancel the execution and return whether Cloud Run CONFIRMED it.

        Only an operation whose result names this exact execution counts.
        `bool(result)` would not: a protobuf is truthy whenever it carries any
        field at all, so falling back to it reports success for essentially any
        completed operation -- including one that returned a different
        execution, which is precisely the case the name comparison exists to
        catch. `repair.py` gates the lease release on this boolean, so a false
        True releases a slot while the first agent may still be running: two
        agents, one task, one credential, one repository.

        A `NotFound` is a confirmed stop: the execution this service was asked
        to end does not exist any more.
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
        cancelled = getattr(result, "name", None) == execution.name
        if self._log:
            log = self._log.info if cancelled else self._log.error
            log(
                "cloud run execution cancelled"
                if cancelled
                else "cloud run did not confirm the cancellation; NOT releasing the slot",
                execution=execution.name,
                returned=str(getattr(result, "name", "")) or None,
                confirmed=cancelled,
            )
        return cancelled

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
    """

    name = "GKE_AUTOPILOT"

    def __init__(
        self,
        *,
        namespace_prefix: str = "swarm-",
        batch_api: Any | None = None,
        core_api: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self._prefix = namespace_prefix
        self._batch = batch_api
        self._core = core_api
        self._log = logger
        self._ca_file: str | None = None

    # -- client ---------------------------------------------------------
    def _configure(self) -> None:
        if self._batch is not None and self._core is not None:
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
            import google.auth.transport.requests

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(google.auth.transport.requests.Request())
            configuration = k8s_client.Configuration()
            configuration.host = gke_api_host(endpoint)
            configuration.ssl_ca_cert = self._ca_file
            configuration.api_key = {"authorization": f"Bearer {credentials.token}"}
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

    # -- reads ----------------------------------------------------------
    def list_executions(self) -> list[ExecutionView]:
        self._configure()
        jobs = self._batch.list_job_for_all_namespaces(label_selector=self.label_selector)
        views: list[ExecutionView] = []
        for job in getattr(jobs, "items", []) or []:
            metadata = job.metadata
            if not str(metadata.namespace or "").startswith(self._prefix):
                continue
            labels = dict(metadata.labels or {})
            if not is_swarm_managed(labels):
                continue
            env = self._pod_env(job)
            # The namespace is per-tenant and a workload cannot move itself
            # between namespaces, so it -- not the container's own environment
            # -- decides whose task this execution is allowed to be about.
            namespace = str(metadata.namespace)
            status = job.status
            active = int(getattr(status, "active", 0) or 0)
            succeeded = int(getattr(status, "succeeded", 0) or 0)
            failed = int(getattr(status, "failed", 0) or 0)
            if active > 0:
                phase = ExecutionPhase.RUNNING
            elif succeeded > 0:
                phase = ExecutionPhase.SUCCEEDED
            elif failed > 0:
                phase = ExecutionPhase.FAILED
            else:
                phase = ExecutionPhase.RUNNING  # created, no pods scheduled yet
            views.append(
                ExecutionView(
                    name=str(metadata.name),
                    backend=self.name,
                    phase=phase,
                    created_at=_ensure_utc(getattr(metadata, "creation_timestamp", None)),
                    task_id=env.get(TASK_ENV) or _first(labels, TASK_LABELS),
                    attempt_id=env.get(ATTEMPT_ENV) or _first(labels, ATTEMPT_LABELS),
                    tenant_id=owning_tenant(
                        env.get(TENANT_ENV) or _first(labels, TENANT_LABELS),
                        namespace[len(self._prefix):],
                        resource=f"{namespace}/{metadata.name}",
                        logger=self._log,
                    ),
                    generation=_int_or_none(
                        env.get(GENERATION_ENV) or _first(labels, GENERATION_LABELS)
                    ),
                    namespace=namespace,
                )
            )
        return views

    def list_job_resources(self) -> list[JobResourceView]:
        """Namespaces here: the per-tenant namespace is the GKE analogue of a
        Cloud Run Job resource, and an empty one is what gets collected."""
        self._configure()
        namespaces = self._core.list_namespace(label_selector=self.label_selector)
        resources: list[JobResourceView] = []
        for namespace in getattr(namespaces, "items", []) or []:
            metadata = namespace.metadata
            name = str(metadata.name)
            if not name.startswith(self._prefix):
                continue
            labels = dict(metadata.labels or {})
            jobs = self._batch.list_namespaced_job(namespace=name)
            items = getattr(jobs, "items", []) or []
            active = sum(1 for job in items if int(getattr(job.status, "active", 0) or 0) > 0)
            last = max(
                (_ensure_utc(job.metadata.creation_timestamp) for job in items),
                default=None,
            )
            resources.append(
                JobResourceView(
                    name=name,
                    tenant_id=_first(labels, TENANT_LABELS) or name[len(self._prefix):],
                    runner_profile=None,
                    created_at=_ensure_utc(getattr(metadata, "creation_timestamp", None)),
                    last_execution_at=last,
                    managed=is_namespace_gc_eligible(labels),
                    active_executions=active,
                )
            )
        return resources

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
        self._configure()
        from kubernetes.client.rest import ApiException

        if not resource.managed:
            raise PermissionError(
                f"refusing to delete namespace {resource.name}: it does not carry "
                f"both a recognised {MANAGED_LABEL} marker and a tenant label"
            )
        namespace = self._core.read_namespace(name=resource.name)
        labels = dict(namespace.metadata.labels or {})
        if not is_namespace_gc_eligible(labels) or not resource.name.startswith(self._prefix):
            raise PermissionError(
                f"namespace {resource.name} is not a swarm tenant namespace; refusing delete"
            )
        jobs = self._batch.list_namespaced_job(namespace=resource.name)
        if getattr(jobs, "items", []):
            return False             # not empty after all; leave it alone
        try:
            self._core.delete_namespace(name=resource.name)
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise
        if self._log:
            self._log.info("tenant namespace deleted", namespace=resource.name)
        return True


def _ensure_utc(value: Any) -> datetime | None:
    parsed = as_datetime(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
