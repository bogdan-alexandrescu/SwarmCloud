"""What the dispatchers actually ask the platform to create.

No network here: the real `run_v2` protobuf types and the real manifest builder
run, and the assertions are about the resource that would be submitted. These
are the contract rules that are invisible until production breaks:

  * requests == limits, so nothing bursts and gets OOM-killed under node pressure
  * no Spot, anywhere -- Spot Pods cannot use Autopilot extended run time
  * the container's service account is the TENANT's, and the secret it mounts is
    the tenant's own, because Cloud Run fixes the service account on the JOB
  * retries are the platform's, never the backend's, or a retry would re-run the
    agent under a stale fencing generation
  * the environment carries IDENTIFIERS ONLY: no image, no command, no sizing
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend
from swarm_common.states import TaskState

from scheduler.dispatch import (
    BackendRouter,
    CloudRunJobDispatcher,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    job_id_for,
    sanitize_name,
    worker_env,
)

from .conftest import PROJECT, scheduler_settings

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings():
    return scheduler_settings()


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def make_task(profile_name: str = "claude-code") -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_abc123",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        timeout_seconds=1800,
    )


def make_lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1",
        task_id=task.id,
        attempt_id="att_1",
        tenant_id=task.tenant_id,
        generation=3,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )


# -- naming ---------------------------------------------------------------

def test_names_are_cloud_run_and_k8s_safe():
    assert job_id_for("eng", "claude-code") == "swarm-eng-claude-code"
    assert sanitize_name("Swarm", "TEAM_Alpha", "codex") == "swarm-team-alpha-codex"
    long = sanitize_name("swarm", "x" * 200, "browser")
    assert len(long) <= 63 and long[0].isalpha()


def test_similar_long_names_do_not_collide():
    a = sanitize_name("swarm", "t" * 70 + "-alpha")
    b = sanitize_name("swarm", "t" * 70 + "-beta")
    assert a != b, "truncation alone would collide; the hash tail must differ"


def test_a_name_that_sanitizes_to_nothing_is_an_error():
    with pytest.raises(DispatchError):
        sanitize_name("---")


# -- worker environment ---------------------------------------------------

def test_worker_environment_carries_identifiers_only(settings, tenant):
    task = make_task()
    env = worker_env(task=task, lease=make_lease(task), tenant=tenant, settings=settings)

    assert env["TASK_ID"] == task.id
    assert env["GENERATION"] == "3"
    assert env["RUNNER_PROFILE"] == "claude-code"
    assert env["TENANT_ID"] == "eng"
    assert env["FIRESTORE_DATABASE"] == "swarm", "never the (default) database"

    forbidden = {"IMAGE", "COMMAND", "ARGS", "CPU", "MEMORY", "BACKEND", "RESOURCE_CLASS"}
    assert not (forbidden & set(env)), (
        "a worker must read what to run from the frozen catalogue, not the environment"
    )
    assert not any("KEY" in name or "SECRET" in name for name in env), (
        "key material is mounted from Secret Manager, never passed as a value"
    )


# -- Cloud Run ------------------------------------------------------------

class FakeOperation:
    def __init__(self, name: str = "projects/p/locations/l/jobs/j/executions/e-1") -> None:
        self.metadata = type("Meta", (), {"name": name})()

    def result(self, timeout=None):
        return self.metadata


class FakeJobsClient:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = set(existing or ())
        self.created: list[dict] = []
        self.runs: list[dict] = []

    def get_job(self, request):
        from google.api_core import exceptions as gexc

        if request.name not in self.existing:
            raise gexc.NotFound(request.name)
        return object()

    def create_job(self, request):
        self.created.append({"job_id": request.job_id, "job": request.job,
                             "parent": request.parent})
        self.existing.add(f"{request.parent}/jobs/{request.job_id}")
        return FakeOperation()

    def run_job(self, request):
        self.runs.append({"name": request.name, "overrides": request.overrides})
        return FakeOperation()


def test_cloud_run_job_is_sized_from_the_catalogue_with_requests_equal_to_limits(
    settings, tenant
):
    dispatcher = CloudRunJobDispatcher(settings, client=FakeJobsClient())
    profile = RUNNER_PROFILES["claude-code"]
    job = dispatcher._build_job(profile, tenant)

    container = job.template.template.containers[0]
    rc = RESOURCE_CLASSES[profile.resource_class]
    assert container.resources.limits["cpu"] == str(int(rc.cpu))
    assert container.resources.limits["memory"] == f"{rc.memory_gib}Gi"
    assert list(container.command) == list(profile.command)
    assert container.image.endswith(f"/{profile.image}:{settings.worker_image_tag}")


def test_cloud_run_job_runs_as_the_tenant_and_mounts_the_tenants_own_secret(
    settings, tenant
):
    dispatcher = CloudRunJobDispatcher(settings, client=FakeJobsClient())
    profile = RUNNER_PROFILES["claude-code"]
    job = dispatcher._build_job(profile, tenant)

    template = job.template.template
    assert template.service_account == tenant.service_account

    secret_envs = [e for e in template.containers[0].env if e.value_source.secret_key_ref.secret]
    assert [e.name for e in secret_envs] == ["ANTHROPIC_API_KEY"]
    assert secret_envs[0].value_source.secret_key_ref.secret == "swarm-tenant-eng-anthropic"
    assert secret_envs[0].value_source.secret_key_ref.secret != "swarm-tenant-research-anthropic"


def test_cloud_run_job_never_retries_on_its_own(settings, tenant):
    dispatcher = CloudRunJobDispatcher(settings, client=FakeJobsClient())
    job = dispatcher._build_job(RUNNER_PROFILES["mock"], tenant)
    assert job.template.template.max_retries == 0, (
        "a backend-level retry would re-run the agent under a stale generation"
    )


def test_cloud_run_workspace_is_disk_backed_not_memory(settings, tenant):
    from google.cloud import run_v2

    dispatcher = CloudRunJobDispatcher(settings, client=FakeJobsClient())
    profile = RUNNER_PROFILES["claude-code"]
    job = dispatcher._build_job(profile, tenant)

    volume = job.template.template.volumes[0]
    assert volume.name == "workspace"
    assert volume.empty_dir.medium != run_v2.EmptyDirVolumeSource.Medium.MEMORY, (
        "a memory-backed workspace is charged against the container's RAM"
    )
    assert volume.empty_dir.size_limit == (
        f"{RESOURCE_CLASSES[profile.resource_class].disk_gib}Gi"
    )


def test_the_job_is_created_once_per_tenant_and_profile(settings, tenant):
    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    task = make_task()
    lease = make_lease(task)
    profile = RUNNER_PROFILES["claude-code"]

    dispatcher.dispatch(task=task, lease=lease, profile=profile, tenant=tenant)
    dispatcher.dispatch(task=task, lease=lease, profile=profile, tenant=tenant)

    assert len(client.created) == 1, "the Job resource is reused across executions"
    assert client.created[0]["job_id"] == "swarm-eng-claude-code"
    assert len(client.runs) == 2


def test_execution_overrides_carry_the_attempt_identity(settings, tenant):
    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    task = make_task()
    lease = make_lease(task)

    name = dispatcher.dispatch(
        task=task, lease=lease, profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )
    assert name.endswith("executions/e-1")

    override = client.runs[0]["overrides"].container_overrides[0]
    env = {e.name: e.value for e in override.env}
    assert override.name == "worker"
    assert env["LEASE_ID"] == "lease_1"
    assert env["ATTEMPT_ID"] == "att_1"
    assert env["GENERATION"] == "3"
    assert client.runs[0]["overrides"].timeout.seconds == task.timeout_seconds


def test_a_cloud_run_api_error_becomes_a_dispatch_error(settings, tenant):
    from google.api_core import exceptions as gexc

    class Broken(FakeJobsClient):
        def run_job(self, request):
            raise gexc.ServiceUnavailable("backend is down")

    dispatcher = CloudRunJobDispatcher(settings, client=Broken({f"{PROJECT}"}))
    task = make_task("mock")
    with pytest.raises(DispatchError):
        dispatcher.dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES["mock"], tenant=tenant
        )


# -- GKE -------------------------------------------------------------------

class FakeBatchApi:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create_namespaced_job(self, namespace, body):
        self.created.append((namespace, body))
        return {"metadata": {"name": body["metadata"]["name"]}}


def test_browser_work_goes_to_gke_with_a_real_dev_shm(settings, tenant):
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=api)
    profile = RUNNER_PROFILES["browser"]
    assert resolve_backend(profile) is Backend.GKE_AUTOPILOT

    task = make_task("browser")
    dispatcher.dispatch(task=task, lease=make_lease(task), profile=profile, tenant=tenant)

    namespace, body = api.created[0]
    assert namespace == "swarm-tenant-eng", "a tenant's pods live in that tenant's namespace"
    volumes = {v["name"]: v for v in body["spec"]["template"]["spec"]["volumes"]}
    assert volumes["dshm"]["emptyDir"]["medium"] == "Memory"
    assert volumes["dshm"]["emptyDir"]["sizeLimit"] == "2Gi", (
        "the 64MiB default /dev/shm is what crashes headless Chromium"
    )


def test_gke_requests_equal_limits_exactly(settings, tenant):
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=api)
    task = make_task("browser")
    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
    )

    container = api.created[0][1]["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == container["resources"]["limits"]
    rc = RESOURCE_CLASSES["browser"]
    assert container["resources"]["limits"]["cpu"] == str(int(rc.cpu))
    assert container["resources"]["limits"]["memory"] == f"{rc.memory_gib}Gi"


def test_gke_job_has_no_spot_selector_and_no_backend_retries(settings, tenant):
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=api)
    task = make_task("browser")
    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
    )

    body = api.created[0][1]
    rendered = repr(body)
    assert "spot" not in rendered.lower(), (
        "Spot Pods cannot use Autopilot extended run time; Spot is disabled platform-wide"
    )
    assert body["spec"]["backoffLimit"] == 0
    assert body["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert body["spec"]["activeDeadlineSeconds"] == task.timeout_seconds


def test_gke_without_configuration_refuses_rather_than_guessing(settings, tenant):
    dispatcher = GkeJobDispatcher(settings, target=None)
    task = make_task("browser")
    with pytest.raises(DispatchError) as exc:
        dispatcher.dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
        )
    assert "GKE_ENDPOINT" in str(exc.value)


# -- routing ---------------------------------------------------------------

def test_router_sends_each_profile_to_its_resolved_backend(settings, tenant):
    jobs = FakeJobsClient()
    api = FakeBatchApi()
    router = BackendRouter(
        cloud_run=CloudRunJobDispatcher(settings, client=jobs),
        gke=GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api),
        settings=settings,
    )

    for profile_name in ("claude-code", "browser"):
        profile = RUNNER_PROFILES[profile_name]
        task = make_task(profile_name)
        router.dispatch(
            task=task,
            lease=make_lease(task),
            profile=profile,
            tenant=tenant,
            backend=resolve_backend(profile),
        )

    assert len(jobs.runs) == 1
    assert len(api.created) == 1


def test_router_refuses_a_disabled_backend(settings, tenant):
    from .conftest import core_settings

    disabled = scheduler_settings(core=core_settings(enable_gke_autopilot=False))
    router = BackendRouter(cloud_run=None, gke=object(), settings=disabled)
    with pytest.raises(DispatchError):
        router.for_backend(Backend.GKE_AUTOPILOT)


def test_router_refuses_an_unresolved_backend(settings):
    router = BackendRouter(cloud_run=object(), gke=object(), settings=settings)
    with pytest.raises(DispatchError):
        router.for_backend(Backend.AUTO)


# -- the container is sized from the TASK's class, not the profile's --------

def make_overridden_task(profile_name: str, resource_class: str) -> Task:
    """A workflow step that named a SMALLER named class than its profile.

    `validate_resource_class_override` permits nothing larger, and admission has
    always charged the task's class: `pool_names_for(resource_class=
    task.resource_class)` and `RESOURCE_CLASSES[task.resource_class].units` both
    read it inside the frozen transaction.
    """
    task = make_task(profile_name)
    task.resource_class = resource_class
    return task


def test_cloud_run_sizes_the_container_from_the_task_not_the_profile(settings, tenant):
    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    profile = RUNNER_PROFILES["browser"]                     # browser: 8 cpu / 16 GiB
    task = make_overridden_task("browser", "standard")       # standard: 4 cpu / 8 GiB

    dispatcher.dispatch(task=task, lease=make_lease(task), profile=profile, tenant=tenant)

    job = client.created[0]["job"]
    container = job.template.template.containers[0]
    rc = RESOURCE_CLASSES["standard"]
    assert container.resources.limits["cpu"] == str(int(rc.cpu))
    assert container.resources.limits["memory"] == f"{rc.memory_gib}Gi"
    assert job.template.template.volumes[0].empty_dir.size_limit == f"{rc.disk_gib}Gi"


def test_an_overridden_class_gets_its_own_cloud_run_job(settings, tenant):
    """Cloud Run pins sizing on the JOB, so one Job cannot express two sizes.

    Without this the smaller class is charged at admission and the container runs
    at the profile's full size anyway -- capacity reserved against a pool that
    does not govern the workload, at the wrong weight.
    """
    assert job_id_for("eng", "browser") == "swarm-eng-browser"
    assert job_id_for("eng", "browser", "browser") == "swarm-eng-browser", (
        "the profile's own class is the common case and keeps one Job per "
        "(tenant, profile)"
    )
    assert job_id_for("eng", "browser", "standard") == "swarm-eng-browser-standard"

    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    profile = RUNNER_PROFILES["browser"]
    for task in (make_task("browser"), make_overridden_task("browser", "standard")):
        dispatcher.dispatch(task=task, lease=make_lease(task), profile=profile, tenant=tenant)

    assert [job["job_id"] for job in client.created] == [
        "swarm-eng-browser",
        "swarm-eng-browser-standard",
    ]


def test_gke_sizes_the_container_from_the_task_not_the_profile(settings, tenant):
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=api)
    task = make_overridden_task("browser", "standard")
    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"], tenant=tenant
    )

    container = api.created[0][1]["spec"]["template"]["spec"]["containers"][0]
    rc = RESOURCE_CLASSES["standard"]
    assert container["resources"]["limits"]["cpu"] == str(int(rc.cpu))
    assert container["resources"]["limits"]["memory"] == f"{rc.memory_gib}Gi"
    assert container["resources"]["limits"]["ephemeral-storage"] == f"{rc.disk_gib}Gi"
    assert container["resources"]["requests"] == container["resources"]["limits"]


# -- pod hardening ---------------------------------------------------------

def gke_pod_spec(settings, tenant, profile_name: str = "browser") -> dict:
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=api)
    task = make_task(profile_name)
    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES[profile_name], tenant=tenant
    )
    return api.created[0][1]["spec"]["template"]["spec"]


def test_gke_pod_runs_non_root_with_no_privilege_escalation(settings, tenant):
    """Untrusted agent code, on a cluster shared with other tenants' pods.

    kubernetes/namespaces/tenant-namespace.yaml holds Pod Security Admission at
    `enforce: baseline` rather than `restricted` because this manifest used to
    carry no securityContext at all.
    """
    spec = gke_pod_spec(settings, tenant)

    pod = spec["securityContext"]
    assert pod["runAsNonRoot"] is True
    assert pod["runAsUser"] == 10001
    assert pod["seccompProfile"] == {"type": "RuntimeDefault"}

    container = spec["containers"][0]["securityContext"]
    assert container["allowPrivilegeEscalation"] is False
    assert container["privileged"] is False
    assert container["readOnlyRootFilesystem"] is True
    assert container["capabilities"] == {"drop": ["ALL"]}
    assert container["seccompProfile"] == {"type": "RuntimeDefault"}


def test_gke_pod_gets_no_kubernetes_api_token(settings, tenant):
    """The worker never calls the Kubernetes API, and a mounted token is the
    first thing a prompt injection or a malicious repository reaches for."""
    assert gke_pod_spec(settings, tenant)["automountServiceAccountToken"] is False


def test_gke_pod_runs_as_the_ksa_the_workload_identity_binding_was_issued_for(
    settings, tenant
):
    """The KSA is not derived from the tenant id.

    Terraform binds `<pool>[<namespace>/<ksa_name>]` with `ksa_name` defaulting
    to `swarm-agent-worker`; the NAMESPACE is what makes the binding per-tenant.
    A pod asking for any other KSA gets no Google identity at all, so it cannot
    read its tenant's secret, its GCS prefix or Firestore.
    """
    spec = gke_pod_spec(settings, tenant)
    assert spec["serviceAccountName"] == "swarm-agent-worker"


def test_the_ksa_name_is_configurable_for_a_differently_provisioned_cluster(
    settings, tenant
):
    from .conftest import scheduler_settings

    other = scheduler_settings(worker_ksa_name="swarm-worker")
    assert gke_pod_spec(other, tenant)["serviceAccountName"] == "swarm-worker"


def test_the_worker_has_writable_paths_under_a_read_only_root(settings, tenant):
    """readOnlyRootFilesystem is only safe if every written path is a volume."""
    spec = gke_pod_spec(settings, tenant)
    mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert {"/workspace", "/dev/shm", "/tmp", "/home/swarm"} <= mounts
    volumes = {v["name"] for v in spec["volumes"]}
    assert {"workspace", "dshm", "tmp", "home"} <= volumes


def test_the_worker_checkpoint_gets_a_grace_period(settings, tenant):
    """The worker checkpoints on SIGTERM; the 30s default would lose the upload."""
    assert gke_pod_spec(settings, tenant)["terminationGracePeriodSeconds"] == 120


# -- the GKE path injects no key material ----------------------------------

def test_gke_job_does_not_reference_a_kubernetes_secret_that_nothing_creates(
    settings, tenant
):
    """A secretKeyRef needs a Kubernetes Secret, and nothing in this repository
    ever creates one -- not terraform, not a SecretProviderClass, not
    external-secrets. Every such pod fails CreateContainerConfigError while the
    scheduler records it as DISPATCHED and holds the lease to the deadline.

    The worker reads the key from Secret Manager itself under the identity this
    pod's KSA assumes (agent_worker/secrets.py), so no injection is needed.
    """
    spec = gke_pod_spec(settings, tenant)
    env = spec["containers"][0]["env"]

    assert all("valueFrom" not in entry for entry in env), (
        "the GKE path must not project from a Kubernetes Secret"
    )
    assert "secretKeyRef" not in repr(spec)
    assert not any("KEY" in entry["name"] or "SECRET" in entry["name"] for entry in env)
    # The identifiers the worker needs are still there.
    names = {entry["name"] for entry in env}
    assert {"TASK_ID", "TENANT_ID", "RUNNER_PROFILE", "GENERATION"} <= names


# -- a tenant with no identity never starts a container ---------------------

def test_neither_backend_will_run_work_for_a_tenant_with_no_service_account(settings):
    """Cloud Run falls back to the project's DEFAULT compute service account
    when a Job names none, which is shared and far more privileged than a
    worker. A tenant whose infrastructure was never provisioned belongs back in
    READY with a clear error."""
    unprovisioned = Tenant(
        tenant_id="newteam",
        kind="group",
        principal="newteam@saga.xyz",
        created_at=NOW,
        service_account=None,
        namespace="swarm-tenant-newteam",
    )
    task = make_task("mock")

    with pytest.raises(DispatchError) as cloud_run:
        CloudRunJobDispatcher(settings, client=FakeJobsClient()).dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES["mock"],
            tenant=unprovisioned,
        )
    assert cloud_run.value.code == "tenant_identity_missing"

    browser = make_task("browser")
    with pytest.raises(DispatchError) as gke:
        GkeJobDispatcher(
            settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=FakeBatchApi()
        ).dispatch(
            task=browser, lease=make_lease(browser), profile=RUNNER_PROFILES["browser"],
            tenant=unprovisioned,
        )
    assert gke.value.code == "tenant_identity_missing"


def test_a_dispatch_error_carries_a_code_separate_from_its_message(settings, tenant):
    """The message is for the operator's log; only the code reaches the tenant."""
    from google.api_core import exceptions as gexc

    class Broken(FakeJobsClient):
        def run_job(self, request):
            raise gexc.ServiceUnavailable(
                f"denied for {tenant.service_account} on swarm-tenant-eng-anthropic"
            )

    dispatcher = CloudRunJobDispatcher(settings, client=Broken())
    task = make_task("mock")
    with pytest.raises(DispatchError) as exc:
        dispatcher.dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES["mock"], tenant=tenant
        )
    assert exc.value.code == "cloud_run_run_job_failed"
    assert tenant.service_account in str(exc.value), "the log keeps the detail"
    assert tenant.service_account not in exc.value.code


# -- admission and dispatch agree, end to end ------------------------------

def real_backend_scheduler(db, settings, batch_api):
    """The real drain loop over the real dispatchers, with fake backend clients."""
    from scheduler.loop import Scheduler
    from scheduler.metrics import SchedulerMetrics
    from scheduler.store import SchedulerStore

    return Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(
            cloud_run=CloudRunJobDispatcher(settings, client=FakeJobsClient()),
            gke=GkeJobDispatcher(
                settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=batch_api
            ),
            settings=settings,
        ),
        metrics=SchedulerMetrics(),
    )


def test_the_pool_that_is_charged_is_the_pool_that_governs_the_container(db, settings):
    """CONTRACT.md invariants 2 and 10, through the real drain loop.

    Before: a `browser` step overridden to `standard` reserved `resource:standard`
    at 1 unit and then ran an 8 cpu / 16 GiB container, so `resource:browser` --
    the pool that actually governed the workload -- was never in the lease's
    `required` list. An admin drain or cap on it did not stop the task, and the
    weighted global budget was charged half of what was running.
    """
    from .conftest import seed_pool, seed_task, seed_tenant

    api = FakeBatchApi()
    seed_tenant(db, "eng", credentials=("anthropic",), max_active=20)
    seed_pool(db, "global", hard_limit=100)
    seed_pool(db, "resource:standard", hard_limit=10)
    seed_pool(db, "resource:browser", hard_limit=10)
    seed_task(
        db,
        task_id="task_override",
        tenant_id="eng",
        runner_profile="browser",
        resource_class="standard",
        provider="anthropic",
    )

    report = real_backend_scheduler(db, settings, api).drain()
    assert report.dispatched == 1, report.to_dict()

    lease = next(d for path, d in db.docs.items() if path.startswith("leases/"))
    container = api.created[0][1]["spec"]["template"]["spec"]["containers"][0]
    charged = RESOURCE_CLASSES["standard"]

    assert "resource:standard" in lease["pools"]
    assert lease["units"] == charged.units
    assert container["resources"]["limits"]["cpu"] == str(int(charged.cpu))
    assert container["resources"]["limits"]["memory"] == f"{charged.memory_gib}Gi"
    assert db.docs["pools/resource:standard"]["active"] == charged.units
    assert db.docs["pools/resource:browser"]["active"] == 0


def test_a_cap_on_the_class_that_runs_really_stops_the_task(db, make_scheduler):
    """The other half: capping `resource:standard` at 0 refuses the overridden
    step, because that is the class its container is sized at."""
    from .conftest import seed_pool, seed_task, seed_tenant

    seed_tenant(db, "eng", credentials=("anthropic",), max_active=20)
    seed_pool(db, "global", hard_limit=100)
    seed_pool(db, "resource:standard", hard_limit=0)
    seed_task(
        db,
        task_id="task_override",
        tenant_id="eng",
        runner_profile="browser",
        resource_class="standard",
        provider="anthropic",
    )

    report = make_scheduler().drain()

    assert report.leased == 0
    assert report.denied == 1
    blocked = db.docs["tasks/task_override"]["blocked_by"]
    assert [entry["pool"] for entry in blocked] == ["resource:standard"]
