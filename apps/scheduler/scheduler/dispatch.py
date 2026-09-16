"""Turning a lease into a running container.

Dispatch is strictly DOWNSTREAM of the lease (invariant 1 and 3): nothing here
runs until capacity has already been reserved in Firestore, and every failure
path hands that capacity straight back rather than leaving a slot held by a
container that will never exist.

Cloud Run Jobs is the primary backend. GKE Autopilot takes browser work, because
Chromium needs a large /dev/shm and GKE gives direct control over it.

Per-tenant-per-profile Job resources exist because of a Cloud Run constraint,
not a preference: Cloud Run sets the service account on the JOB resource and it
cannot be overridden per execution. One shared Job would therefore run every
tenant's work as the same identity, and invariant 9 -- a tenant's provider key
must never be reachable from another tenant's pod -- would be unenforceable. So
each (tenant, profile) gets its own Job bound to that tenant's service account,
and the reconciler garbage-collects the ones that stop being used.

Two rules from the contract are applied here and are not negotiable:
  * requests == limits. Cloud Run expresses this as limits only; on GKE both
    are set to the same values. Bursting past a request is what gets a container
    OOM-killed under node pressure.
  * No Spot, anywhere. Spot Pods cannot use Autopilot extended run time, so
    Spot and "no preemption" are mutually exclusive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, RunnerProfile

log = logging.getLogger(__name__)

_NAME_SAFE = re.compile(r"[^a-z0-9-]+")


class DispatchError(Exception):
    """Dispatch failed. The caller releases the lease and returns to READY."""


class Dispatcher(Protocol):
    def dispatch(
        self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant
    ) -> str: ...


def sanitize_name(*parts: str, max_length: int = 63) -> str:
    """A Cloud Run / k8s safe name: lowercase alnum and dashes, <= 63 chars."""
    joined = "-".join(p for p in parts if p)
    slug = _NAME_SAFE.sub("-", joined.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise DispatchError(f"cannot build a resource name from {parts!r}")
    if len(slug) > max_length:
        # Truncating alone would collide for two long tenant names sharing a
        # prefix, so the tail carries a hash of the full name.
        import hashlib

        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
        slug = slug[: max_length - 9].rstrip("-") + "-" + digest
    if not slug[0].isalpha():
        slug = "s" + slug[: max_length - 1]
    return slug


def job_id_for(tenant_id: str, profile_name: str) -> str:
    return sanitize_name("swarm", tenant_id, profile_name)


def worker_env(*, task: Task, lease: Lease, tenant: Tenant, settings: Any) -> dict[str, str]:
    """The ONLY thing the environment carries is identifiers.

    No image, no command, no resource spec. A worker launched with a doctored
    environment still reads what to run from the frozen catalogue keyed by
    RUNNER_PROFILE, which is invariant 10 enforced at the last possible moment.
    """
    return {
        "TASK_ID": task.id,
        "ATTEMPT_ID": lease.attempt_id,
        "LEASE_ID": lease.lease_id,
        "GENERATION": str(lease.generation),
        "TENANT_ID": task.tenant_id,
        "RUNNER_PROFILE": task.runner_profile,
        "PROJECT_ID": settings.project_id,
        "REGION": settings.region,
        "FIRESTORE_DATABASE": settings.core.firestore_database,
        "ARTIFACT_BUCKET": settings.core.artifact_bucket,
        "GCS_PREFIX": tenant.gcs_prefix or "",
        "CHECKPOINT_INTERVAL_SECONDS": str(
            RUNNER_PROFILES[task.runner_profile].checkpoint_interval_seconds
        ),
        "MAX_IN_WORKER_RETRY_DELAY_SECONDS": str(
            settings.core.max_in_worker_retry_delay_seconds
        ),
        "TASK_TIMEOUT_SECONDS": str(task.timeout_seconds),
    }


def image_uri(settings: Any, profile: RunnerProfile) -> str:
    return f"{settings.artifact_registry_host}/{profile.image}:{settings.worker_image_tag}"


# --------------------------------------------------------------------------
# Cloud Run Jobs
# --------------------------------------------------------------------------

class CloudRunJobDispatcher:
    def __init__(self, settings: Any, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self._ensured: set[str] = set()

    def _jobs(self) -> Any:
        if self._client is None:
            from google.cloud import run_v2

            self._client = run_v2.JobsClient()
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self._settings.project_id}/locations/{self._settings.region}"

    def job_name(self, job_id: str) -> str:
        return f"{self.parent}/jobs/{job_id}"

    def _build_job(self, profile: RunnerProfile, tenant: Tenant) -> Any:
        from google.api import launch_stage_pb2
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        rc = RESOURCE_CLASSES[profile.resource_class]
        env = [
            run_v2.EnvVar(name="RUNNER_PROFILE", value=profile.name),
            run_v2.EnvVar(name="TENANT_ID", value=tenant.tenant_id),
        ]
        for secret_env in profile.secrets:
            if not profile.provider:
                continue
            # The tenant's OWN secret, by the one spelling the frozen contract
            # defines. A shared secret here would break invariant 9.
            env.append(
                run_v2.EnvVar(
                    name=secret_env,
                    value_source=run_v2.EnvVarSource(
                        secret_key_ref=run_v2.SecretKeySelector(
                            secret=tenant.secret_name(profile.provider),
                            version="latest",
                        )
                    ),
                )
            )

        container = run_v2.Container(
            name="worker",
            image=image_uri(self._settings, profile),
            command=list(profile.command),
            env=env,
            # Cloud Run expresses sizing as limits; the platform never sets a
            # request below the limit, so requests == limits holds.
            resources=run_v2.ResourceRequirements(
                limits={"cpu": str(int(rc.cpu)), "memory": f"{rc.memory_gib}Gi"}
            ),
            volume_mounts=[run_v2.VolumeMount(name="workspace", mount_path="/workspace")],
        )

        volume = run_v2.Volume(
            name="workspace",
            # MEDIUM_UNSPECIFIED is the disk-backed ephemeral volume (Preview).
            # MEMORY would charge the workspace against the container's RAM and
            # OOM-kill long agent runs, which is exactly what the sizing work
            # was meant to prevent.
            empty_dir=run_v2.EmptyDirVolumeSource(
                medium=run_v2.EmptyDirVolumeSource.Medium.MEDIUM_UNSPECIFIED,
                size_limit=f"{rc.disk_gib}Gi",
            ),
        )

        return run_v2.Job(
            # Ephemeral disk is a Preview feature, so the resource must declare
            # a pre-GA launch stage or the API rejects it.
            launch_stage=launch_stage_pb2.LaunchStage.BETA,
            labels={
                "managed-by": "swarm-scheduler",
                "swarm-tenant": sanitize_name(tenant.tenant_id),
                "swarm-profile": sanitize_name(profile.name),
            },
            template=run_v2.ExecutionTemplate(
                parallelism=1,
                task_count=1,
                template=run_v2.TaskTemplate(
                    containers=[container],
                    volumes=[volume],
                    # The platform owns retries: a retry is a new attempt with a
                    # new fencing generation. Cloud Run retrying underneath us
                    # would run the agent twice on one generation.
                    max_retries=0,
                    timeout=duration_pb2.Duration(seconds=profile.timeout_seconds),
                    service_account=tenant.service_account,
                    execution_environment=run_v2.ExecutionEnvironment.EXECUTION_ENVIRONMENT_GEN2,
                ),
            ),
        )

    def ensure_job(self, profile: RunnerProfile, tenant: Tenant) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2

        job_id = job_id_for(tenant.tenant_id, profile.name)
        name = self.job_name(job_id)
        if job_id in self._ensured:
            return name

        client = self._jobs()
        try:
            client.get_job(request=run_v2.GetJobRequest(name=name))
            self._ensured.add(job_id)
            return name
        except gexc.NotFound:
            pass
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(f"could not read Cloud Run job {job_id}: {exc}") from exc

        try:
            operation = client.create_job(
                request=run_v2.CreateJobRequest(
                    parent=self.parent,
                    job_id=job_id,
                    job=self._build_job(profile, tenant),
                )
            )
            operation.result(timeout=120)
        except gexc.AlreadyExists:
            # Another scheduler instance created it between our get and create.
            pass
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(f"could not create Cloud Run job {job_id}: {exc}") from exc
        self._ensured.add(job_id)
        return name

    def dispatch(self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        name = self.ensure_job(profile, tenant)
        env = worker_env(task=task, lease=lease, tenant=tenant, settings=self._settings)
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    name="worker",
                    env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()],
                )
            ],
            task_count=1,
            timeout=duration_pb2.Duration(seconds=task.timeout_seconds),
        )
        try:
            operation = self._jobs().run_job(
                request=run_v2.RunJobRequest(name=name, overrides=overrides)
            )
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(f"run_job failed for {name}: {exc}") from exc

        # The execution name is available from the operation's metadata without
        # waiting for the execution to FINISH -- waiting here would hold the
        # drain loop open for the whole length of an agent run.
        metadata = getattr(operation, "metadata", None)
        execution_name = getattr(metadata, "name", None)
        if not execution_name:
            execution_name = f"{name}/executions/pending-{lease.attempt_id}"
        return execution_name


# --------------------------------------------------------------------------
# GKE Autopilot
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GkeTarget:
    endpoint: str
    ca_cert_path: str
    namespace_template: str = "swarm-{tenant}"


class GkeJobDispatcher:
    """Creates a batch/v1 Job in the tenant's own namespace.

    The cluster endpoint and CA bundle come from the environment (written by the
    terraform track) rather than from a live container.googleapis.com lookup, so
    dispatch needs no extra API dependency and no extra IAM role on the hot path.
    """

    def __init__(self, settings: Any, *, target: GkeTarget | None = None,
                 batch_api: Any | None = None) -> None:
        self._settings = settings
        self._target = target
        self._batch_api = batch_api

    def _api(self) -> Any:
        if self._batch_api is not None:
            return self._batch_api
        if self._target is None:
            raise DispatchError(
                "GKE dispatch is not configured: set GKE_ENDPOINT and GKE_CA_CERT_PATH"
            )
        import google.auth
        import google.auth.transport.requests
        from kubernetes import client as k8s

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        configuration = k8s.Configuration()
        configuration.host = self._target.endpoint
        configuration.ssl_ca_cert = self._target.ca_cert_path
        configuration.api_key = {"authorization": f"Bearer {credentials.token}"}
        self._batch_api = k8s.BatchV1Api(k8s.ApiClient(configuration))
        return self._batch_api

    def namespace_for(self, tenant: Tenant) -> str:
        if tenant.namespace:
            return tenant.namespace
        template = self._target.namespace_template if self._target else "swarm-{tenant}"
        return sanitize_name(template.format(tenant=tenant.tenant_id))

    def _manifest(self, *, task: Task, lease: Lease, profile: RunnerProfile,
                  tenant: Tenant) -> dict[str, Any]:
        rc = RESOURCE_CLASSES[profile.resource_class]
        env = [{"name": k, "value": v} for k, v in
               worker_env(task=task, lease=lease, tenant=tenant, settings=self._settings).items()]
        for secret_env in profile.secrets:
            if not profile.provider:
                continue
            env.append(
                {
                    "name": secret_env,
                    "valueFrom": {
                        "secretKeyRef": {
                            # Synced from the tenant's own Secret Manager secret
                            # into the tenant's own namespace. Another tenant's
                            # pod cannot mount it: different namespace.
                            "name": tenant.secret_name(profile.provider),
                            "key": secret_env,
                        }
                    },
                }
            )
        resources = {
            "cpu": str(int(rc.cpu)),
            "memory": f"{rc.memory_gib}Gi",
            "ephemeral-storage": f"{rc.disk_gib}Gi",
        }
        job_name = sanitize_name("swarm", task.id.replace("task_", ""), str(lease.generation))
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace_for(tenant),
                "labels": {
                    "managed-by": "swarm-scheduler",
                    "swarm-tenant": sanitize_name(tenant.tenant_id),
                    "swarm-profile": sanitize_name(profile.name),
                    "swarm-task": sanitize_name(task.id),
                },
                "annotations": {
                    # Autopilot extended run time: the pod is not evicted for
                    # scale-down. This is only available on on-demand capacity,
                    # which is why Spot is disabled platform-wide.
                    "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
                },
            },
            "spec": {
                # The platform owns retries; a k8s-level retry would re-run the
                # agent under a stale fencing generation.
                "backoffLimit": 0,
                "completions": 1,
                "parallelism": 1,
                "activeDeadlineSeconds": task.timeout_seconds,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": {
                            "swarm-task": sanitize_name(task.id),
                            "swarm-tenant": sanitize_name(tenant.tenant_id),
                        }
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": sanitize_name("swarm", tenant.tenant_id),
                        "automountServiceAccountToken": True,
                        "containers": [
                            {
                                "name": "worker",
                                "image": image_uri(self._settings, profile),
                                "command": list(profile.command),
                                "env": env,
                                # requests == limits, both directions, no bursting.
                                "resources": {"requests": resources, "limits": resources},
                                "volumeMounts": [
                                    {"name": "workspace", "mountPath": "/workspace"},
                                    {"name": "dshm", "mountPath": "/dev/shm"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "workspace", "emptyDir": {
                                "sizeLimit": f"{rc.disk_gib}Gi"}},
                            # Chromium's shared memory. The 64 MiB default is
                            # what makes headless Chrome crash under load, and
                            # is the reason browser work is on GKE at all.
                            {"name": "dshm", "emptyDir": {
                                "medium": "Memory", "sizeLimit": "2Gi"}},
                        ],
                    },
                },
            },
        }

    def dispatch(self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant) -> str:
        manifest = self._manifest(task=task, lease=lease, profile=profile, tenant=tenant)
        namespace = manifest["metadata"]["namespace"]
        try:
            created = self._api().create_namespaced_job(namespace=namespace, body=manifest)
        except Exception as exc:  # kubernetes.client.ApiException and transport errors
            raise DispatchError(f"could not create GKE job in {namespace}: {exc}") from exc
        name = getattr(getattr(created, "metadata", None), "name", None)
        if not name and isinstance(created, dict):
            name = created.get("metadata", {}).get("name")
        return f"{namespace}/{name or manifest['metadata']['name']}"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

class BackendRouter:
    """Picks the dispatcher for a profile's resolved backend."""

    def __init__(self, *, cloud_run: Dispatcher | None, gke: Dispatcher | None,
                 settings: Any) -> None:
        self._cloud_run = cloud_run
        self._gke = gke
        self._settings = settings

    def for_backend(self, backend: Backend) -> Dispatcher:
        if backend is Backend.CLOUD_RUN_JOB:
            if self._cloud_run is None or not self._settings.core.enable_cloud_run_jobs:
                raise DispatchError("Cloud Run Jobs dispatch is disabled")
            return self._cloud_run
        if backend is Backend.GKE_AUTOPILOT:
            if self._gke is None or not self._settings.core.enable_gke_autopilot:
                raise DispatchError("GKE Autopilot dispatch is disabled")
            return self._gke
        raise DispatchError(f"backend {backend} was never resolved to a concrete target")

    def dispatch(
        self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant, backend: Backend
    ) -> str:
        return self.for_backend(backend).dispatch(
            task=task, lease=lease, profile=profile, tenant=tenant
        )


def build_router(settings: Any) -> BackendRouter:
    import os

    endpoint = os.environ.get("GKE_ENDPOINT", "").strip()
    ca_path = os.environ.get("GKE_CA_CERT_PATH", "").strip()
    target = GkeTarget(endpoint=endpoint, ca_cert_path=ca_path) if endpoint and ca_path else None
    return BackendRouter(
        cloud_run=CloudRunJobDispatcher(settings),
        gke=GkeJobDispatcher(settings, target=target),
        settings=settings,
    )
