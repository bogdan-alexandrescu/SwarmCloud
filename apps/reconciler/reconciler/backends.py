"""Reading and stopping real execution on the two backends.

Everything here is I/O against Google APIs, kept behind two small interfaces so
`detect.py` and `repair.py` can be exercised without a project. Two rules hold
across both implementations:

* **Nothing is touched unless this platform created it.** The project is shared
  with a live GKE cluster, a VPC and a dozen service accounts belonging to other
  teams. Every list is label-filtered on `managed-by=swarm` and every delete
  re-checks the label on the object it is about to remove, because a list filter
  is a query and a re-check is a guarantee.
* **Termination is confirmed, not requested.** `terminate()` returns True only
  when the backend acknowledged it. The caller uses that return value to decide
  whether it may release the slot, and a slot released after an unconfirmed
  termination is exactly the duplicate-execution bug this service exists to
  prevent.
"""

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .model import ExecutionPhase, ExecutionView, JobResourceView, as_datetime

MANAGED_LABEL = "managed-by"
MANAGED_VALUE = "swarm"
TASK_LABEL = "swarm-task-id"
ATTEMPT_LABEL = "swarm-attempt-id"
TENANT_LABEL = "swarm-tenant-id"
GENERATION_LABEL = "swarm-generation"


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
            for execution in self._executions_client().list_executions(parent=job_name):
                views.append(self._execution_view(execution, job_name))
        return views

    def _list_jobs(self) -> list[Any]:
        return [
            job
            for job in self._jobs_client().list_jobs(parent=self.parent)
            if str(getattr(job, "name", "")).rsplit("/", 1)[-1].startswith(self._prefix)
        ]

    def _is_managed(self, resource: Any) -> bool:
        labels = dict(getattr(resource, "labels", {}) or {})
        return labels.get(MANAGED_LABEL) == MANAGED_VALUE

    def _execution_view(self, execution: Any, job_name: str) -> ExecutionView:
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

        return ExecutionView(
            name=str(getattr(execution, "name", "")),
            backend=self.name,
            phase=phase,
            created_at=as_datetime(getattr(execution, "create_time", None)),
            task_id=labels.get(TASK_LABEL) or env.get("TASK_ID"),
            attempt_id=labels.get(ATTEMPT_LABEL) or env.get("ATTEMPT_ID"),
            tenant_id=labels.get(TENANT_LABEL) or env.get("TENANT_ID"),
            generation=_int_or_none(labels.get(GENERATION_LABEL) or env.get("GENERATION")),
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
        from google.cloud import run_v2

        request = run_v2.CancelExecutionRequest(name=execution.name)
        operation = self._executions_client().cancel_execution(request=request)
        result = operation.result(timeout=120)
        cancelled = getattr(result, "name", None) == execution.name
        if self._log:
            self._log.info(
                "cloud run execution cancelled", execution=execution.name, confirmed=cancelled
            )
        return True if cancelled else bool(result)

    def list_job_resources(self) -> list[JobResourceView]:
        resources: list[JobResourceView] = []
        for job in self._list_jobs():
            if not self._is_managed(job):
                continue
            labels = dict(getattr(job, "labels", {}) or {})
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
                    tenant_id=labels.get(TENANT_LABEL),
                    runner_profile=labels.get("swarm-runner-profile"),
                    created_at=as_datetime(getattr(job, "create_time", None)),
                    last_execution_at=last,
                    managed=True,
                    active_executions=active,
                )
            )
        return resources

    def delete_job_resource(self, resource: JobResourceView) -> bool:
        if not resource.managed:
            raise PermissionError(
                f"refusing to delete unmanaged Cloud Run job {resource.name}: "
                f"missing {MANAGED_LABEL}={MANAGED_VALUE}"
            )
        # Re-read and re-check the label: the list that produced this view may
        # be seconds old, and this project is shared with other teams.
        job = self._jobs_client().get_job(name=resource.name)
        if not self._is_managed(job):
            raise PermissionError(f"job {resource.name} is not managed by swarm; refusing delete")
        operation = self._jobs_client().delete_job(name=resource.name)
        operation.result(timeout=120)
        if self._log:
            self._log.info("cloud run job resource deleted", job=resource.name)
        return True


# ---------------------------------------------------------------------------
# GKE Autopilot
# ---------------------------------------------------------------------------


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
            configuration.host = (
                endpoint if endpoint.startswith("https://") else f"https://{endpoint}"
            )
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
        return f"{MANAGED_LABEL}={MANAGED_VALUE}"

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
                    task_id=labels.get(TASK_LABEL),
                    attempt_id=labels.get(ATTEMPT_LABEL),
                    tenant_id=labels.get(TENANT_LABEL),
                    generation=_int_or_none(labels.get(GENERATION_LABEL)),
                    namespace=str(metadata.namespace),
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
                    tenant_id=labels.get(TENANT_LABEL) or name[len(self._prefix):],
                    runner_profile=None,
                    created_at=_ensure_utc(getattr(metadata, "creation_timestamp", None)),
                    last_execution_at=last,
                    managed=labels.get(MANAGED_LABEL) == MANAGED_VALUE,
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
                f"refusing to delete namespace {resource.name}: not labelled "
                f"{MANAGED_LABEL}={MANAGED_VALUE}"
            )
        namespace = self._core.read_namespace(name=resource.name)
        labels = dict(namespace.metadata.labels or {})
        if labels.get(MANAGED_LABEL) != MANAGED_VALUE:
            raise PermissionError(f"namespace {resource.name} is not managed by swarm")
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
