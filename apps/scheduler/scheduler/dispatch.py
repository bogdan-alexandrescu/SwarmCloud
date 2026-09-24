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
and the reconciler garbage-collects the ones that stop being used. The same
constraint applies to sizing, which Cloud Run also pins on the Job resource, so a
workflow step that named a smaller resource class gets a Job of its own -- see
the sizing note below.

Two rules from the contract are applied here and are not negotiable:
  * requests == limits. Cloud Run expresses this as limits only; on GKE both
    are set to the same values. Bursting past a request is what gets a container
    OOM-killed under node pressure.
  * No Spot, anywhere. Spot Pods cannot use Autopilot extended run time, so
    Spot and "no preemption" are mutually exclusive.

SIZING COMES FROM `task.resource_class`, NOT from `profile.resource_class`.
Those are usually the same, and differ only when a workflow step named a
smaller named class (validation.py permits nothing larger). Admission has always
accounted against the TASK's class -- `pool_names_for` and the weighted `units`
both read `task.resource_class` inside the frozen admission transaction -- so a
container sized from the profile's class instead would mean the reserved
capacity and the running container disagree: the pool that governs the workload
would never be in the lease's `required` list (so a drain or an admin cap on it
would not stop the task), and the weighted global budget would be charged for
something other than what is running. The Cloud Run Job id carries the class for
the same reason: Cloud Run fixes sizing on the Job resource, so one Job per
(tenant, profile) could only ever express one size.

SECRETS ARE NOT INJECTED ON THE GKE PATH. The worker reads its tenant's key from
Secret Manager at runtime using Workload Identity (agent_worker/secrets.py), and
on Cloud Run the Job additionally projects it natively from Secret Manager. On
GKE there is no such native source: a `secretKeyRef` needs a Kubernetes Secret,
nothing in this repository ever creates one, and a Job referencing a missing
Secret never starts (CreateContainerConfigError) while the scheduler records it
as DISPATCHED and holds the lease to the deadline. So the GKE manifest passes no
key material at all and the pod's KSA-to-GSA binding is what grants access.
"""

from __future__ import annotations

import base64
import logging
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE
from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, RunnerProfile

log = logging.getLogger(__name__)

#: IMPORTED, not restated. This was a fourth private copy of `[^a-z0-9-]+`
#: (here, reconciler/detect.py, kubernetes/render.py and identity.py itself),
#: and the reconciler's `sanitised()` reverse-maps a Job label back to the
#: Firestore id this function produced -- so the two escaping rules agreeing is
#: what lets a running execution be matched to its task at all. If they drift,
#: the reconciler either kills a live execution it read as orphaned or never
#: finds a genuinely orphaned one. kubernetes/render.py already imports this
#: symbol for the same reason. docs/audits/2026-09-18/08, finding 1.

#: The image creates uid/gid 10001 with HOME=/home/swarm and runs as it
#: (images/agent-runtime-base/Dockerfile). The pod spec repeats the number so
#: the cluster enforces it rather than trusting the image, and so
#: `runAsNonRoot` has something to check against.
WORKER_UID = 10001
WORKER_HOME = "/home/swarm"

#: THE WORKSPACE MOUNT, AND THE ARTIFACTS DIRECTORY INSIDE IT.
#:
#: `/workspace` was written out in three places here -- the Cloud Run volume
#: mount, the GKE volume mount, and nothing that tied them together -- so it is
#: a constant now for the same reason WORKER_HOME is.
#:
#: WHY SWARM_ARTIFACTS_DIR HAS TO BE SET AT ALL. `runners/base.py` defaults
#: `artifacts_dir` to `work.parent / "artifacts"`, and the work dir is the
#: container's cwd, `/workspace` -- so the default resolves to `/artifacts`, at
#: the root. On GKE_AUTOPILOT the container sets `readOnlyRootFilesystem: true`
#: (CONTAINER_SECURITY_CONTEXT), so the runner died on its first line:
#:
#:     OSError: [Errno 30] Read-only file system: '/artifacts'
#:
#: measured 2026-09-24 on the first browser attempt that ever got as far as
#: starting a container. Every earlier attempt failed before that, at
#: `jobs.batch is forbidden`, so this was the next defect in the queue and had
#: never been reachable.
#:
#: WHY CLOUD_RUN_JOB NEVER SHOWED IT. Identical image, identical
#: `python -m agent_worker.runners.<x>` command out of the frozen catalogue --
#: and a container that does NOT harden the root filesystem, so `mkdir
#: /artifacts` simply succeeded. Twenty-two tasks passed through that path while
#: the same code could not start on GKE. The hardening is right and its absence
#: on Cloud Run is the anomaly.
#:
#: SET FOR BOTH BACKENDS, from this one shared builder, deliberately. Cloud Run
#: does not need it today, and giving it the same value anyway means the two
#: backends put artifacts in the same place, that Cloud Run stops depending on a
#: writable root, and that there is one answer to "where do artifacts go" instead
#: of one per backend. The upload reads `ctx.artifacts_dir`, so it follows.
#:
#: INSIDE THE WORKSPACE VOLUME rather than in a volume of its own: a dedicated
#: emptyDir would need a `sizeLimit`, which is a second disk number to invent and
#: keep in step when `workspace` already takes its limit from the resource
#: class's disk. Artifacts are produced from the workspace and uploaded to
#: ARTIFACT_BUCKET when the attempt ends, so charging them to that budget is the
#: honest accounting.
WORKSPACE_MOUNT = "/workspace"
WORKER_ARTIFACTS_DIR = f"{WORKSPACE_MOUNT}/artifacts"

#: Pod hardening. kubernetes/namespaces/tenant-namespace.yaml holds Pod Security
#: Admission at `enforce: baseline` rather than `restricted` precisely because
#: the manifest this module builds carried no securityContext at all, so
#: enforcing the stronger policy would have rejected every job the dispatcher
#: created. These two blocks are what `restricted` asks for, and they mirror
#: kubernetes/worker-templates/worker-job-browser.yaml field for field.
POD_SECURITY_CONTEXT: dict[str, Any] = {
    "runAsNonRoot": True,
    "runAsUser": WORKER_UID,
    "runAsGroup": WORKER_UID,
    "fsGroup": WORKER_UID,
    "seccompProfile": {"type": "RuntimeDefault"},
}

#: The container runs untrusted agent code from a caller-supplied repository on
#: a cluster shared with other tenants' pods, so it drops every capability and
#: cannot gain privilege. Chromium's own sandbox needs user namespaces and
#: CAP_SYS_ADMIN and stays off; the isolation is the pod and the namespace, and
#: handing a capability back to regain the inner sandbox would be a bad trade.
CONTAINER_SECURITY_CONTEXT: dict[str, Any] = {
    "allowPrivilegeEscalation": False,
    "privileged": False,
    "readOnlyRootFilesystem": True,
    "runAsNonRoot": True,
    "runAsUser": WORKER_UID,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}


class DispatchError(Exception):
    """Dispatch failed. The caller releases the lease and returns to READY.

    `code` is the only part of this that is safe to hand back to a tenant.
    `str(exc)` carries the upstream API's message, and a Cloud Run or Kubernetes
    error routinely echoes the resource it was given: the tenant service account
    email, the job name, the secret names in the manifest. None of that is key
    material, but it is internal infrastructure detail, and `task.last_error` is
    returned to the caller by `codec.task_to_api`. So the full text goes to the
    log and the code goes to the tenant.
    """

    def __init__(self, message: str, *, code: str = "dispatch_failed") -> None:
        super().__init__(message)
        self.code = code


class Dispatcher(Protocol):
    def dispatch(
        self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant
    ) -> str: ...


def resource_class_for(task: Task, profile: RunnerProfile) -> str:
    """The class the container is sized from -- the same one admission charged.

    A task whose stored class is not in the catalogue any more falls back to the
    profile's, because a missing class would otherwise raise a KeyError deep in
    manifest construction. Admission would have failed first in that case
    (`RESOURCE_CLASSES[task.resource_class]` in the drain loop), so this is a
    guard, not a path.
    """
    if task.resource_class in RESOURCE_CLASSES:
        return task.resource_class
    log.warning(
        "task %s names resource class %r which is no longer in the catalogue; "
        "sizing from the profile's own class %r",
        task.id,
        task.resource_class,
        profile.resource_class,
    )
    return profile.resource_class


def sanitize_name(*parts: str, max_length: int = 63) -> str:
    """A Cloud Run / k8s safe name: lowercase alnum and dashes, <= 63 chars."""
    joined = "-".join(p for p in parts if p)
    slug = _NAME_SAFE.sub("-", joined.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise DispatchError(
            f"cannot build a resource name from {parts!r}", code="invalid_resource_name"
        )
    if len(slug) > max_length:
        # Truncating alone would collide for two long tenant names sharing a
        # prefix, so the tail carries a hash of the full name.
        import hashlib

        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
        slug = slug[: max_length - 9].rstrip("-") + "-" + digest
    if not slug[0].isalpha():
        slug = "s" + slug[: max_length - 1]
    return slug


def job_id_for(tenant_id: str, profile_name: str, resource_class: str | None = None) -> str:
    """The Cloud Run Job resource id.

    Still one Job per (tenant, profile) for every task that takes the profile's
    own resource class, which is the overwhelming majority and what CONTRACT.md
    describes. A workflow step that named a SMALLER class gets its own Job,
    because Cloud Run pins CPU, memory and the ephemeral-disk size limit on the
    Job resource and cannot override them per execution -- so without this the
    smaller class would be charged for at admission and then run in the profile's
    full-size container anyway.

    The "job" segment is NOT decoration: terraform/infra/locals.tf names these
    `${name_prefix}-job-${tenant}-${profile}`, and this must match exactly. It
    did not -- the dispatcher asked for `swarm-u-bogdan-mock` while terraform had
    created `swarm-job-u-bogdan-mock` -- so every dispatch 404'd and the
    dispatcher then created its OWN job under the wrong name. Those jobs carry no
    `managed-by=swarm-terraform` label, which means scripts/destroy.sh would
    refuse to run at all rather than delete an unlabelled resource.
    """
    profile = RUNNER_PROFILES.get(profile_name)
    default_class = profile.resource_class if profile else None
    if resource_class and resource_class != default_class:
        return sanitize_name("swarm", "job", tenant_id, profile_name, resource_class)
    return sanitize_name("swarm", "job", tenant_id, profile_name)


def worker_env(*, task: Task, lease: Lease, tenant: Tenant, settings: Any) -> dict[str, str]:
    """The ONLY thing the environment carries is identifiers and endpoints.

    No image, no command, no resource spec. A worker launched with a doctored
    environment still reads what to run from the frozen catalogue keyed by
    RUNNER_PROFILE, which is invariant 10 enforced at the last possible moment.

    QUOTA_BROKER_URL IS HERE BECAUSE THERE IS NOWHERE ELSE IT COULD GO. This
    function is the single source of a worker's execution environment for both
    dispatchers below, so a name it does not carry is a name no worker ever
    sees. The account pool was complete on both sides and entirely inert
    because of that: nothing set it, `WorkerConfig.quota_broker_url` was always
    None, and every worker took the "no broker configured" branch. It is not an
    execution parameter -- it names a platform service, the same way
    ARTIFACT_BUCKET does -- and it still cannot be influenced by a caller,
    because it comes from the scheduler's own settings.

    OMITTED RATHER THAN SET EMPTY when the deployment has no pool. A Cloud Run
    execution override MERGES with the Job's own environment, so an empty value
    here would override a URL that terraform had baked into the Job and turn a
    wired deployment back into an unwired one.
    """
    env = {
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
        # See WORKER_ARTIFACTS_DIR: without this the runner cannot create its
        # own artifacts directory on a read-only root filesystem.
        "SWARM_ARTIFACTS_DIR": WORKER_ARTIFACTS_DIR,
    }
    broker_url = str(getattr(settings, "quota_broker_url", "") or "").strip()
    if broker_url:
        env["QUOTA_BROKER_URL"] = broker_url
        # The audience defaults to the URL inside the worker's client, which is
        # what Cloud Run checks when a service declares no custom audience.
        # This deployment's broker declares one, so the token minted for the
        # URL alone would be rejected by the broker's own `aud` check.
        audience = str(getattr(settings, "quota_broker_audience", "") or "").strip()
        if audience:
            env["QUOTA_BROKER_AUDIENCE"] = audience
    return env


def image_uri(settings: Any, profile: RunnerProfile) -> str:
    """The image a worker for `profile` runs: a DIGEST whenever the deployment pins one.

    `worker_image_refs` (WORKER_IMAGE_REFS, written by terraform from the
    promotion manifest) is authoritative when it is non-empty. A profile whose
    image is missing from it is refused, not given a tag: a tag is resolved
    when the image is pulled, and the GKE template pulls IfNotPresent, so a
    fallback here would run whatever a node last cached under that tag -- the
    exact path the digest map exists to close, taken silently.

    An empty map is the local emulator loop, which has no promotion manifest;
    it keeps the tag it always had. A deployed scheduler is never in that
    state: terraform always sets the map, and check-env-parity requires it.
    """
    refs = getattr(settings, "worker_image_refs", None) or {}
    if refs:
        ref = refs.get(profile.image)
        if not ref:
            raise DispatchError(
                f"no digest-pinned image for {profile.image!r} (runner profile "
                f"{profile.name!r}) in WORKER_IMAGE_REFS, which pins "
                f"{sorted(refs)}; refusing to run it at a tag. The promotion "
                "manifest the deployment was applied from did not include this "
                "image -- rebuild and redeploy every image",
                code="worker_image_not_pinned",
            )
        return ref
    return f"{settings.artifact_registry_host}/{profile.image}:{settings.worker_image_tag}"


def assert_tenant_identity(tenant: Tenant) -> str:
    """Refuse to start anything for a tenant with no service account.

    Cloud Run falls back to the project's DEFAULT compute service account when a
    Job names none, and that identity is shared and usually far more privileged
    than a worker -- so a tenant whose infrastructure was never provisioned would
    run its code as it. Invariant 9 requires the tenant's own identity, and a
    task that cannot have one belongs back in READY with a clear error, not in a
    container.
    """
    if not tenant.service_account:
        raise DispatchError(
            f"tenant {tenant.tenant_id!r} has no service account; its identity has "
            "not been provisioned (terraform tenants map, or "
            "scripts/register-tenant.sh) and the platform will not run its work "
            "under a shared one",
            code="tenant_identity_missing",
        )
    return tenant.service_account


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

    def _pool_can_serve(self, tenant: Tenant, profile: RunnerProfile) -> bool:
        """Whether this tenant's credential for `profile` comes from the pool.

        Both halves matter. A deployment with no broker has no pool, so a
        missing per-tenant key is simply a missing key. A tenant that HAS
        registered a key keeps its secret mounted whether or not a pool exists,
        because the pool is an addition to that tenant's options and not a
        replacement for them.
        """
        if not str(getattr(self._settings, "quota_broker_url", "") or "").strip():
            return False
        if not profile.provider:
            return False
        return profile.provider not in (tenant.credentials or [])

    def _build_job(
        self, profile: RunnerProfile, tenant: Tenant, resource_class: str | None = None
    ) -> Any:
        from google.api import launch_stage_pb2
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        assert_tenant_identity(tenant)
        rc = RESOURCE_CLASSES[resource_class or profile.resource_class]
        env = [
            run_v2.EnvVar(name="RUNNER_PROFILE", value=profile.name),
            run_v2.EnvVar(name="TENANT_ID", value=tenant.tenant_id),
        ]
        for secret_env in profile.secrets:
            if not profile.provider:
                continue
            if self._pool_can_serve(tenant, profile):
                # A POOL ACCOUNT IS THIS TENANT'S CREDENTIAL, so there is no
                # per-tenant secret to project and naming one would be naming a
                # secret that does not exist. Cloud Run resolves a
                # `secretKeyRef` when the JOB is created, so that fails the
                # create outright and the tenant cannot be dispatched at all --
                # not "runs without a key", but "never starts". The pool could
                # not replace the per-tenant secret for anyone, which is most
                # of the point of having it.
                #
                # Narrow on purpose: only when this deployment HAS a pool and
                # this tenant has registered no key of its own. Without a pool,
                # a tenant with no key cannot run this profile however the job
                # is shaped, and the existing behaviour -- mount it, fail
                # loudly -- is left exactly as it was.
                #
                # The worker resolves its own credential either way, from
                # Secret Manager or from the pool, so this mount has always
                # been a convenience rather than the thing the agent runs on. A
                # tenant with neither a key nor a usable account still parks as
                # CREDENTIAL_MISSING, in the worker, where the reason can be
                # written onto the task.
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
            volume_mounts=[run_v2.VolumeMount(name="workspace", mount_path=WORKSPACE_MOUNT)],
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
                "swarm-resource-class": sanitize_name(rc.name),
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

    def _refresh_image(
        self,
        client: Any,
        name: str,
        existing: Any,
        profile: RunnerProfile,
        tenant: Tenant,
        resource_class: str | None,
    ) -> None:
        """Bring a job THIS dispatcher created up to the image it would create today.

        A Cloud Run Job pins its image on the job resource, and this method's
        caller used to stop at "it exists". So a job the dispatcher created for a
        tenant terraform does not know -- every self-service `u-<email>` tenant --
        kept the image of the day it was created, at a tag from before the
        digest map existed, for every execution afterwards. Pinning NEW jobs by
        digest does nothing for those; this does.

        ONLY jobs labelled managed-by=swarm-scheduler. Terraform's jobs are
        terraform's: every apply pins them by digest, and the dispatcher
        rewriting one is precisely the repoint modules/cloud_run_jobs keeps the
        image un-ignored in order to catch as drift.

        Checked once per job per process (the caller caches the result in
        `_ensured`), and a new scheduler revision starts with an empty cache --
        which is exactly when the digest map changes.
        """
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2

        labels = dict(getattr(existing, "labels", None) or {})
        if labels.get("managed-by") != "swarm-scheduler":
            return
        wanted = image_uri(self._settings, profile)
        try:
            current = existing.template.template.containers[0].image
        except (AttributeError, IndexError):
            current = ""
        if current == wanted:
            return
        job = self._build_job(profile, tenant, resource_class)
        job.name = name
        try:
            operation = client.update_job(request=run_v2.UpdateJobRequest(job=job))
            operation.result(timeout=120)
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"could not move Cloud Run job {name} from {current or 'an unreadable image'} "
                f"to {wanted}: {exc}",
                code="cloud_run_update_job_failed",
            ) from exc
        log.info("cloud run job %s moved from %s to %s", name, current or "?", wanted)

    def ensure_job(
        self, profile: RunnerProfile, tenant: Tenant, resource_class: str | None = None
    ) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2

        job_id = job_id_for(tenant.tenant_id, profile.name, resource_class)
        name = self.job_name(job_id)
        if job_id in self._ensured:
            return name

        client = self._jobs()
        try:
            existing = client.get_job(request=run_v2.GetJobRequest(name=name))
        except gexc.NotFound:
            existing = None
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"could not read Cloud Run job {job_id}: {exc}",
                code="cloud_run_get_job_failed",
            ) from exc
        if existing is not None:
            self._refresh_image(client, name, existing, profile, tenant, resource_class)
            self._ensured.add(job_id)
            return name

        try:
            operation = client.create_job(
                request=run_v2.CreateJobRequest(
                    parent=self.parent,
                    job_id=job_id,
                    job=self._build_job(profile, tenant, resource_class),
                )
            )
            operation.result(timeout=120)
        except gexc.AlreadyExists:
            # Another scheduler instance created it between our get and create.
            pass
        except gexc.GoogleAPICallError as exc:
            raise DispatchError(
                f"could not create Cloud Run job {job_id}: {exc}",
                code="cloud_run_create_job_failed",
            ) from exc
        self._ensured.add(job_id)
        return name

    def dispatch(self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        # Before any API call: a tenant with no identity is refused here rather
        # than after a round trip that would only be wasted.
        assert_tenant_identity(tenant)
        name = self.ensure_job(profile, tenant, resource_class_for(task, profile))
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
            raise DispatchError(
                f"run_job failed for {name}: {exc}", code="cloud_run_run_job_failed"
            ) from exc

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

def gke_api_host(endpoint: str) -> str:
    """The `host` a kubernetes client Configuration needs for this cluster.

    GKE_ENDPOINT is `google_container_cluster.endpoint` passed straight through
    by terraform/infra/locals.tf, and that attribute is a BARE address --
    `34.118.229.12`, no scheme. The kubernetes client concatenates
    `configuration.host` with the resource path and hands the result to urllib3,
    which needs a scheme to choose a connection pool at all: without it every
    call fails on a URL it cannot parse, and it fails AFTER admission has taken
    the lease, so the task is already committed to a backend that will never
    start it.

    This function is duplicated, deliberately: the identical body lives in
    `reconciler.backends`. The two services are separate images that share only
    the FROZEN `swarm_common` package -- images/swarm-scheduler/Dockerfile
    copies `apps/common/` and `apps/scheduler/` and nothing else -- so neither
    can import the other and there is nowhere in-image to put a single copy.
    `tests/unit/control_plane/test_gke_client_host.py` pins the two together by
    asserting they agree on the same inputs, which is exactly the check that was
    missing while they did not: the reconciler added the scheme and this side
    did not.
    """
    raw = (endpoint or "").strip()
    if not raw:
        # The caller treats "unset" as "GKE is not configured" and refuses to
        # dispatch; it must not be turned into the URL "https://".
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
    cached. This dispatcher is built once per process -- `main.create_app` puts
    the Scheduler in app state and the one-minute Cloud Scheduler tick keeps the
    instance warm -- so a token minted at first use is the token every later
    dispatch carries.

    The reconciler proved what that costs. Instance 00a41e8c of swarm-reconciler
    started at 2026-09-22T00:18:03Z, minted on its first pass at 00:20:24Z, and
    from 00:55:08Z answered 401 Unauthorized for 3h50m and 40 consecutive
    passes; the identical construction lived here. On this side the damage is
    worse in one way and better in another: every GKE dispatch on a warm
    instance fails, so a browser-profile task never starts -- but the 401 is
    wrapped into a DispatchError by `GkeJobDispatcher.dispatch`, so the lease
    does come back and no capacity leaks.

    `Credentials.valid` is google.auth's own answer to "would this token still be
    accepted", already carrying google.auth's refresh threshold, so a token this
    returns had time left when the request was built. Credentials that cannot say
    when they expire are refreshed on every call: one extra metadata round trip
    is cheaper than one dispatch lost to a dead token.
    """
    import google.auth.transport.requests

    expiry = getattr(credentials, "expiry", None)
    if expiry is None or not getattr(credentials, "valid", False):
        credentials.refresh(google.auth.transport.requests.Request())
    token = getattr(credentials, "token", None)
    if not token:
        # Never the token itself. `DispatchError.code` and the message below
        # reach `task.last_error`, which the API returns to the tenant verbatim.
        raise RuntimeError(
            "google.auth returned no access token for the GKE API; "
            "no job can be created without one"
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

    This function is duplicated in `reconciler.backends`, deliberately and for
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


#: What the dispatch-failure message says instead of an address when this
#: process cannot name the identity it is authenticating with. It is a sentence
#: rather than an empty string or a plausible-looking default: a fabricated
#: address in this message would send the reader to check a RoleBinding against
#: an account that was never presented, which is the same class of mistake as
#: the numeric-default this repository refused in `render.py` -- it reads as a
#: fact and authorises nobody.
UNNAMED_IDENTITY = "an identity this process could not name"


def authenticated_identity(credentials: Any) -> str:
    """The service account this process presents to the Kubernetes API server.

    WHY THIS IS IN THE ERROR MESSAGE AT ALL. GKE resolves a Google service
    account that authenticated with an OAuth ACCESS token -- which is how
    `install_google_bearer_token` reaches the API -- to the account's numeric
    `uniqueId`, not to its email. So the 403 the API server sends back names a
    subject like

        User "117405034245659033603" cannot create resource "jobs" ...

    while every RoleBinding in this repository was written naming the EMAIL. On
    2026-09-24 that made a binding which applied cleanly authorise nobody, and
    the two spellings were far enough apart that nothing connected the digits in
    the error to the address in the manifest. Naming the account we authenticated
    AS, beside the namespace we tried, is what closes that gap: the reader then
    has the email to look up in the RoleBinding and the digits to compare it to.

    ASKED ONCE, AND NOT REFRESHED HERE. `swarm_api.delegation.service_account_email`
    does the same lookup in three escalating steps, the second of which is a
    `credentials.refresh()` -- on Cloud Run the attribute is the literal string
    "default" until something refreshes it. That step is unnecessary on this path
    because `install_google_bearer_token` has already minted a token from these
    credentials before this is called, so the refresh has happened; and the third
    step, a urllib call to the metadata server, is deliberately NOT copied here
    because this value is only ever used to decorate a failure message. A network
    call inside an error path can turn one failed dispatch into a hung one.

    Returns "" when the credentials cannot name themselves (a user credential
    from `gcloud auth application-default login`, or an unrefreshed compute
    credential). The caller substitutes UNNAMED_IDENTITY rather than guessing.
    """
    email = getattr(credentials, "service_account_email", None)
    # "default" is what compute and Cloud Run credentials report before a
    # refresh; it names no account and must not be printed as if it did.
    if isinstance(email, str) and email and email != "default" and "@" in email:
        return email
    return ""


@dataclass(frozen=True)
class GkeTarget:
    endpoint: str
    ca_cert_path: str
    namespace_template: str = "swarm-tenant-{tenant}"
    #: Kubernetes service account the pod runs as. It must be the KSA the
    #: Workload Identity binding was issued for, or the pod gets no Google
    #: identity at all and cannot read its tenant's secret, its GCS prefix or
    #: Firestore. Terraform's `tenancy` module binds
    #: `<pool>[<namespace>/<ksa_name>]` with `ksa_name` defaulting to
    #: `swarm-agent-worker`, so that is the default here; `WORKER_KSA_NAME`
    #: overrides it for a cluster provisioned with a different spelling.
    ksa_name: str = "swarm-agent-worker"
    #: The cluster CA as terraform actually supplies it. `locals.tf` sets
    #: GKE_CA_CERT_B64 for this service (and for the reconciler) from
    #: `module.gke_autopilot[0].ca_certificate`, which is base64 exactly as the
    #: container API returns it. Nothing anywhere writes a CA file into this
    #: image, so `ca_cert_path` alone could never be satisfied in a deployed
    #: environment; it stays for a path supplied out of band.
    ca_cert_b64: str = ""

    @property
    def api_host(self) -> str:
        """`endpoint` as a URL the kubernetes client can use. See gke_api_host."""
        return gke_api_host(self.endpoint)


class GkeJobDispatcher:
    """Creates a batch/v1 Job in the tenant's own namespace.

    The cluster endpoint and CA bundle come from the environment (written by the
    terraform track) rather than from a live container.googleapis.com lookup, so
    dispatch needs no extra API dependency and no extra IAM role on the hot path.
    """

    def __init__(self, settings: Any, *, target: GkeTarget | None = None,
                 batch_api: Any | None = None, identity: str = "") -> None:
        self._settings = settings
        self._target = target
        self._batch_api = batch_api
        self._ca_file: str | None = None
        #: WHO THIS DISPATCHER AUTHENTICATES AS, for the failure message. Filled
        #: in by `_api()` from the credentials it resolves, so in a deployment it
        #: needs no wiring and cannot disagree with the token actually sent.
        #:
        #: `identity` travels with `batch_api` and exists for the same reason:
        #: a caller that supplies a ready-made Kubernetes client has taken over
        #: authentication, so this class can no longer learn the identity from
        #: google.auth and cannot invent it. Supplying the client without the
        #: identity is honest and costs only the vaguer message.
        self._identity: str = str(identity or "")

    def _ca_cert_file(self, target: GkeTarget) -> str:
        """A path on local disk holding the cluster CA.

        Terraform hands this service the CA base64-encoded in GKE_CA_CERT_B64,
        never as a file, so a base64 value is materialised once per process --
        the same thing the reconciler does with the same variable.
        """
        if target.ca_cert_path:
            return target.ca_cert_path
        if self._ca_file is None:
            handle = tempfile.NamedTemporaryFile(
                prefix="gke-ca-", suffix=".crt", delete=False
            )
            handle.write(base64.b64decode(target.ca_cert_b64))
            handle.close()
            self._ca_file = handle.name
        return self._ca_file

    def _api(self) -> Any:
        if self._batch_api is not None:
            return self._batch_api
        if self._target is None:
            raise DispatchError(
                "GKE dispatch is not configured: set GKE_ENDPOINT and GKE_CA_CERT_B64",
                code="gke_not_configured",
            )
        try:
            host = self._target.api_host
            ca_cert = self._ca_cert_file(self._target)
        except ValueError as exc:
            # A malformed GKE_ENDPOINT or a CA that is not base64 is a
            # configuration fault, and the dispatch loop catches DispatchError
            # and NOTHING else: any other exception escapes it with the lease
            # still held, so the slot stays occupied until the dispatch deadline
            # for a container that was never created. (binascii.Error, which
            # b64decode raises, is a ValueError.)
            raise DispatchError(
                f"GKE dispatch is misconfigured: {exc}", code="gke_misconfigured"
            ) from exc
        import google.auth
        from kubernetes import client as k8s

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        configuration = k8s.Configuration()
        configuration.host = host
        configuration.ssl_ca_cert = ca_cert
        # Per request, not once. `self._batch_api` is cached for the life of the
        # process and this method returns it unchanged on every later dispatch,
        # so a token minted here would be the token every subsequent
        # create_namespaced_job carried until the instance died.
        install_google_bearer_token(configuration, credentials)
        # AFTER the token is installed, not before: minting the token is what
        # refreshes these credentials, and an unrefreshed compute credential
        # reports its email as the literal string "default".
        self._identity = authenticated_identity(credentials)
        self._batch_api = k8s.BatchV1Api(k8s.ApiClient(configuration))
        return self._batch_api

    def namespace_for(self, tenant: Tenant) -> str:
        if tenant.namespace:
            return tenant.namespace
        template = (
            self._target.namespace_template if self._target else "swarm-tenant-{tenant}"
        )
        return sanitize_name(template.format(tenant=tenant.tenant_id))

    def ksa_for(self, tenant: Tenant) -> str:
        """The Kubernetes service account the pod runs as.

        NOT derived from the tenant id. The KSA only means anything because a
        Workload Identity binding maps `<pool>[<namespace>/<ksa>]` to the
        tenant's Google service account, and that binding is issued by
        provisioning for a fixed KSA name inside the tenant's own namespace --
        the namespace is what makes it per-tenant, which is exactly why terraform
        notes that a pod in another tenant's namespace cannot impersonate this
        GSA even if it guesses the KSA name. Inventing a third spelling here
        produced a pod with no Google identity at all.
        """
        configured = str(getattr(self._settings, "worker_ksa_name", "") or "").strip()
        if not configured and self._target is not None:
            configured = self._target.ksa_name
        return sanitize_name(configured or "swarm-agent-worker")

    def _manifest(self, *, task: Task, lease: Lease, profile: RunnerProfile,
                  tenant: Tenant) -> dict[str, Any]:
        assert_tenant_identity(tenant)
        rc = RESOURCE_CLASSES[resource_class_for(task, profile)]
        # Identifiers only. No provider key is injected: there is no Kubernetes
        # Secret to project from, and the worker fetches its tenant's key from
        # Secret Manager itself under the identity this pod's KSA assumes.
        env = [{"name": k, "value": v} for k, v in
               worker_env(task=task, lease=lease, tenant=tenant, settings=self._settings).items()]
        resources = {
            "cpu": str(int(rc.cpu)),
            "memory": f"{rc.memory_gib}Gi",
            "ephemeral-storage": f"{rc.disk_gib}Gi",
        }
        job_name = sanitize_name("swarm", task.id.replace("task_", ""), str(lease.generation))
        labels = {
            "managed-by": "swarm-scheduler",
            "swarm-tenant": sanitize_name(tenant.tenant_id),
            "swarm-profile": sanitize_name(profile.name),
            "swarm-resource-class": sanitize_name(rc.name),
            "swarm-task": sanitize_name(task.id),
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace_for(tenant),
                "labels": labels,
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
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": self.ksa_for(tenant),
                        # The workload is arbitrary code from a caller-supplied
                        # repository on a cluster shared with other tenants. A
                        # mounted Kubernetes API token is the first thing a
                        # prompt injection or a malicious repository reaches for,
                        # and the worker never calls the Kubernetes API.
                        "automountServiceAccountToken": False,
                        # The worker checkpoints on SIGTERM; killing it at the
                        # 30s default would lose the workspace it was uploading.
                        "terminationGracePeriodSeconds": 120,
                        "securityContext": POD_SECURITY_CONTEXT,
                        "containers": [
                            {
                                "name": "worker",
                                "image": image_uri(self._settings, profile),
                                "command": list(profile.command),
                                "env": env,
                                # requests == limits, both directions, no bursting.
                                "resources": {"requests": resources, "limits": resources},
                                "securityContext": CONTAINER_SECURITY_CONTEXT,
                                "volumeMounts": [
                                    {"name": "workspace", "mountPath": WORKSPACE_MOUNT},
                                    {"name": "dshm", "mountPath": "/dev/shm"},
                                    # readOnlyRootFilesystem means every path the
                                    # runtime writes to needs a volume: /tmp, and
                                    # a HOME for tool caches and crash dumps.
                                    {"name": "tmp", "mountPath": "/tmp"},
                                    {"name": "home", "mountPath": WORKER_HOME},
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
                            {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}},
                            {"name": "home", "emptyDir": {"sizeLimit": "4Gi"}},
                        ],
                    },
                },
            },
        }

    @staticmethod
    def _is_forbidden(exc: BaseException) -> bool:
        """True for a 403 from the API server, however the client reports it.

        `kubernetes.client.ApiException` carries `.status`, but this path also
        sees transport wrappers and, in tests, plain exceptions -- so the
        status is preferred and the message is the fallback rather than the
        other way round. Being wrong in the FALSE direction costs the extra
        sentence below; being wrong in the TRUE direction would add it to an
        unrelated failure, so neither is expensive and neither is guessed at
        beyond these two signals.
        """
        status = getattr(exc, "status", None)
        if status is not None:
            try:
                return int(status) == 403
            except (TypeError, ValueError):
                pass
        text = str(exc)
        return "is forbidden" in text or '"code": 403' in text or "'code': 403" in text

    def presented_identity(self) -> str:
        """The identity for the failure message, never blank and never invented."""
        return self._identity or UNNAMED_IDENTITY

    def dispatch(self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant) -> str:
        manifest = self._manifest(task=task, lease=lease, profile=profile, tenant=tenant)
        namespace = manifest["metadata"]["namespace"]
        try:
            created = self._api().create_namespaced_job(namespace=namespace, body=manifest)
        except DispatchError:
            raise
        except Exception as exc:  # kubernetes.client.ApiException and transport errors
            # KUBERNETES AUTHORISES BEFORE IT RESOLVES, so a 403 here does NOT
            # establish that this is a permissions problem.
            #
            # A Job created into a namespace that does not exist comes back as
            #
            #     jobs.batch is forbidden: User "1174050342..." cannot create
            #     resource "jobs" in API group "batch" in the namespace
            #     "swarm-tenant-eng"
            #
            # -- the authorizer runs against the namespaced request before
            # anything looks for the namespace, so the API server reports a
            # missing permission and never a missing namespace. There is no 404
            # to wait for. On 2026-09-23 that message sent three separate
            # investigations at IAM while the actual cause was that the
            # provisioner spelled the namespace `swarm-eng` and this dispatcher
            # spelled it `swarm-tenant-eng`; all seven `browser` tasks this
            # platform had ever accepted had failed that way, over two days.
            #
            # So the namespace is NAMED and the ambiguity is stated, in the
            # message rather than in a doc nobody reads at 03:00. `loop.py`
            # logs `str(exc)` on its `dispatch failed` line, which is where an
            # operator meets this. docs/gke-dispatch-403.md has the mechanism and
            # docs/incidents/2026-09-24-gke-dispatch.md the whole sequence.
            #
            # AND THE IDENTITY IS NAMED TOO, which it was not until 2026-09-24.
            # Four of the seven causes behind that outage -- a wrong namespace, a
            # missing Role, a missing RoleBinding, and a RoleBinding whose subject
            # named the right account by the wrong one of its two names -- all
            # produce this one message, and the two facts that separate them are
            # WHERE we tried and WHO we were. The upstream text supplies the
            # numeric uniqueId GKE resolved us to; only this process knows which
            # email that is, and the gap between those two spellings is what hid
            # the real cause for two days. Both are now in one line.
            if self._is_forbidden(exc):
                identity = self.presented_identity()
                raise DispatchError(
                    f"could not create GKE job in {namespace} as {identity}: {exc} "
                    f"-- NOTE: a 403 here does not prove this is a permissions "
                    f"problem. Kubernetes authorises before it resolves, so a Job "
                    f"created into a namespace that DOES NOT EXIST is also reported "
                    f"as `jobs.batch is forbidden`, never as 404. Before changing any "
                    f"IAM, confirm the namespace {namespace} exists and that its "
                    f"swarm-dispatcher RoleBinding names the account this dispatcher "
                    f"authenticated as -- {identity} -- BOTH by email and by numeric "
                    f"uniqueId: kubectl get ns {namespace} && kubectl get rolebinding "
                    f"swarm-dispatcher -n {namespace} -o yaml. This dispatcher "
                    f"presents an OAuth access token, and on that path GKE names the "
                    f"caller by uniqueId -- the `User \"...\"` above -- so a subject "
                    f"list carrying the email alone applies cleanly and authorises "
                    f"nobody. See docs/gke-dispatch-403.md and "
                    f"docs/incidents/2026-09-24-gke-dispatch.md.",
                    code="gke_create_job_forbidden",
                ) from exc
            raise DispatchError(
                f"could not create GKE job in {namespace} as "
                f"{self.presented_identity()}: {exc}",
                code="gke_create_job_failed",
            ) from exc
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
                raise DispatchError(
                    "Cloud Run Jobs dispatch is disabled", code="backend_disabled"
                )
            return self._cloud_run
        if backend is Backend.GKE_AUTOPILOT:
            if self._gke is None or not self._settings.core.enable_gke_autopilot:
                raise DispatchError(
                    "GKE Autopilot dispatch is disabled", code="backend_disabled"
                )
            return self._gke
        raise DispatchError(
            f"backend {backend} was never resolved to a concrete target",
            code="backend_unresolved",
        )

    def dispatch(
        self, *, task: Task, lease: Lease, profile: RunnerProfile, tenant: Tenant, backend: Backend
    ) -> str:
        return self.for_backend(backend).dispatch(
            task=task, lease=lease, profile=profile, tenant=tenant
        )


def _gke_ca_file() -> str:
    """Materialise the cluster CA bundle, returning a path, or "" if unset.

    The environment carries the bundle base64-encoded (`GKE_CA_CERT_B64`, written
    by terraform from the cluster's own output) because a Cloud Run environment
    variable is a string and a CA bundle is a file. This end of it used to read
    `GKE_CA_CERT_PATH` instead -- a name nothing has ever set, and a different
    shape besides -- so `target` was always None and every GKE dispatch died on
    "GKE dispatch is not configured".

    It never looked like a configuration bug: the scheduler treats a dispatch
    failure as transient, returns the task to READY and retries on the next
    drain, so the symptom was a browser task that stayed queued forever rather
    than anything that crashed or alerted. The reconciler read the right name
    all along (reconciler/backends.py), which is what made this survivable and
    also what made it invisible -- the two halves disagreed and only one was
    ever exercised.
    """
    import base64
    import os
    import tempfile

    ca_b64 = os.environ.get("GKE_CA_CERT_B64", "").strip()
    if not ca_b64:
        return ""
    handle = tempfile.NamedTemporaryFile(prefix="gke-ca-", suffix=".crt", delete=False)
    handle.write(base64.b64decode(ca_b64))
    handle.close()
    return handle.name


def build_router(settings: Any) -> BackendRouter:
    import os

    endpoint = os.environ.get("GKE_ENDPOINT", "").strip()
    # _gke_ca_file already decodes GKE_CA_CERT_B64 to a file, so there is one
    # representation of the bundle here rather than two.
    ca_path = _gke_ca_file()
    target = None
    if endpoint and ca_path:
        target = GkeTarget(
            endpoint=endpoint,
            ca_cert_path=ca_path,
            ksa_name=getattr(settings, "worker_ksa_name", "") or "swarm-agent-worker",
        )
    return BackendRouter(
        cloud_run=CloudRunJobDispatcher(settings),
        gke=GkeJobDispatcher(settings, target=target),
        settings=settings,
    )
