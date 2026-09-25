"""A GKE worker reaches the metadata server by ADDRESS, and a Cloud Run worker is left alone.

Silent GKE worker, task_1f699ef4cdbb4cc79c16 / att_78e34e7537de487ba71d
(2026-09-24). The tenant egress NetworkPolicy allowed DNS only to the
`k8s-app=kube-dns` pods, while swarm-autopilot answers pod DNS from the
host-network NodeLocal DNSCache, which no rule allowed. Every lookup by NAME was
dropped. google-auth's first request went to the metadata server by IP and got
a 200 (it is in the gke-metadata-server log); the project-id lookup that follows
it goes to `metadata.google.internal` by name, and there is no later request
from that pod in the log, and no token was ever minted for the tenant's GSA. The
worker then retried Firestore in silence until the reconciler killed it.

google-auth reads two variables when `google.auth.compute_engine._metadata` is
imported: GCE_METADATA_HOST, the host of every `get()` (project id, service
account email, access and identity tokens), and GCE_METADATA_IP, the host of the
`ping()` that decides whether the process is on Google Cloud at all. The GKE
dispatcher sets both to the link-local address, so google-auth's metadata and
token requests no longer need a lookup. That is ALL it does: Firestore, Secret
Manager, Cloud Storage, the quota broker, the provider APIs and every browser
target are still reached by name, and still need the NetworkPolicy DNS rule.

WHAT IS ASSERTED HERE
  * every Job the GKE dispatcher sends names the address, for every profile --
    a profile on Cloud Run today is one catalogue edit from GKE;
  * the PROPERTY, not the spelling: under the environment the dispatcher sends,
    google-auth's own ping, project-id and token requests go to
    169.254.169.254 over plain HTTP on port 80 (the port the tenant egress
    policy opens there), and none of them names a host that must be resolved.
    A misspelled or renamed variable fails this even when the first test passes;
  * Cloud Run is NOT given the override. Its metadata path works by name
    (execution swarm-job-u-sw-c90291-mock-6smzz), it has no NetworkPolicy, and
    a Cloud Run execution override MERGES into the Job's environment -- so the
    variables in the shared `worker_env` would have changed every Cloud Run
    execution to fix a GKE-only fault;
  * nor does terraform give them to the Cloud Run Jobs it creates: no file
    those Jobs take their environment from -- terraform/infra,
    modules/cloud_run_jobs, and every environment's tfvars, whose
    `settings_env` is merged into every one of them -- names either variable.
    That check reads text, not a plan; what it cannot see is in its docstring.

The templates under kubernetes/worker-templates/ are held to the same two
entries by tests/unit/worker/test_worker_templates_name_the_metadata_server.py.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import CloudRunJobDispatcher, GkeJobDispatcher, GkeTarget, worker_env

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, FakeJobsClient, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]

#: The GKE metadata server's link-local address. gke-metadata-server answers
#: here on port 80 for every pod on a Workload Identity node pool.
METADATA_SERVER_IP = "169.254.169.254"

#: The two names google-auth reads. `GCE_METADATA_ROOT`, the old spelling of
#: the first, is deliberately not one of them: google-auth reads it only when
#: GCE_METADATA_HOST is unset.
METADATA_ENV = ("GCE_METADATA_HOST", "GCE_METADATA_IP")


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


def _gke_worker_env(settings, tenant, profile_name: str) -> dict[str, str]:
    """The `worker` container's environment in the Job the GKE dispatcher SENDS.

    Through `dispatch()`, not `_manifest()`, so what is asserted is what reached
    the Kubernetes client.
    """
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    )
    task = make_task(profile_name)
    dispatcher.dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES[profile_name], tenant=tenant
    )
    assert len(api.created) == 1, "the dispatcher did not create exactly one Job"
    containers = api.created[0][1]["spec"]["template"]["spec"]["containers"]
    worker = next(c for c in containers if c["name"] == "worker")
    assert all("valueFrom" not in e for e in worker["env"]), "worker env must be literal values"
    return {e["name"]: e["value"] for e in worker["env"]}


@pytest.mark.parametrize("profile_name", sorted(RUNNER_PROFILES))
def test_every_gke_worker_is_told_the_metadata_servers_address(settings, tenant, profile_name):
    """MUTATION: drop either entry from the GKE worker's environment in
    scheduler/dispatch.py, or change its value. This fails naming both."""
    env = _gke_worker_env(settings, tenant, profile_name)
    # The anchor: this is the real worker environment, not an empty dict that
    # would make the assertion below about nothing.
    assert env.get("TASK_ID") == "task_abc123"

    got = {name: env.get(name) for name in METADATA_ENV}
    assert got == {name: METADATA_SERVER_IP for name in METADATA_ENV}, (
        f"the GKE worker for {profile_name!r} is dispatched with {got}. Without "
        f"GCE_METADATA_HOST={METADATA_SERVER_IP} google-auth fetches the project "
        f"id and every token from metadata.google.internal BY NAME, and on "
        f"swarm-autopilot a pod whose DNS is dropped by its NetworkPolicy then "
        f"hangs in silence (task_1f699ef4cdbb4cc79c16)."
    )


class _MetadataResponse:
    """What gke-metadata-server answers: 200, with the Metadata-Flavor header."""

    status = 200

    def __init__(self) -> None:
        self.data = PROJECT.encode("utf-8")
        self.headers = {"content-type": "text/plain", "metadata-flavor": "Google"}


class _RecordingRequest:
    """A `google.auth.transport.Request` that records every URL it is sent to."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        self.urls.append(url)
        return _MetadataResponse()


def test_google_auth_reaches_the_gke_metadata_server_without_a_dns_lookup(
    settings, tenant, monkeypatch
):
    """THE PROPERTY: under the GKE worker's environment, google-auth needs no DNS.

    google-auth reads its metadata host ONCE, when `_metadata` is imported --
    in the container that is before any worker code runs, with the pod's
    environment already in place. So the module is reloaded here with exactly
    that environment, and google-auth's own `ping`, `get_project_id` and
    token `get` are driven through a request that records where they went. The
    module is reloaded again afterwards with the environment restored, so no
    other test sees the override.

    Red before the fix on the project-id and token URLs, both of which went to
    `metadata.google.internal` -- the exact pair the incident's pod never
    reached. The ping was already by IP, which is why the metadata-server log
    shows it and nothing after it.
    """
    import importlib  # noqa: PLC0415

    from google.auth import environment_vars  # noqa: PLC0415
    from google.auth.compute_engine import _metadata  # noqa: PLC0415

    env = _gke_worker_env(settings, tenant, "browser")

    request = _RecordingRequest()
    try:
        # Whatever the machine running this has set is not the pod's.
        for name in (
            environment_vars.GCE_METADATA_HOST,
            environment_vars.GCE_METADATA_ROOT,
            environment_vars.GCE_METADATA_IP,
        ):
            monkeypatch.delenv(name, raising=False)
        # The WHOLE container environment, not just the two names this file
        # expects: if google-auth reads a name the dispatcher does not set,
        # that is exactly what this test exists to notice.
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        metadata = importlib.reload(_metadata)

        on_gce = metadata.ping(request)
        project = metadata.get_project_id(request)
        metadata.get(request, "instance/service-accounts/default/token")
    finally:
        monkeypatch.undo()
        importlib.reload(_metadata)

    assert on_gce is True and project == PROJECT, (
        "the recording request did not answer the way a metadata server does, so "
        "the URLs below would be about nothing"
    )
    assert len(request.urls) == 3, f"expected ping, project id and token; saw {request.urls}"

    wrong = {}
    for url in request.urls:
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            ipaddress.ip_address(host)
            by_address = host == METADATA_SERVER_IP
        except ValueError:
            by_address = False
        if not by_address or (parts.scheme, parts.port or 80) != ("http", 80):
            wrong[url] = host
    assert not wrong, (
        f"under the environment the GKE dispatcher sends, google-auth sent "
        f"{sorted(wrong)} somewhere other than http://{METADATA_SERVER_IP}:80. A "
        f"host NAME there is a DNS lookup on the path every Google credential "
        f"takes, and the tenant egress policy opens this address on port 80 and "
        f"988 only."
    )


def _cloud_run_profiles() -> list[str]:
    return sorted(
        name
        for name, profile in RUNNER_PROFILES.items()
        if resolve_backend(profile) is Backend.CLOUD_RUN_JOB
    )


def test_cloud_run_workers_are_not_given_the_gke_metadata_override(settings, tenant):
    """Cloud Run does not need it, and must not get it by accident.

    The override belongs to the GKE container only. Put it in `worker_env` --
    the builder BOTH dispatchers share -- and every Cloud Run execution gets it
    through its container override, which merges into the Job's environment.
    Cloud Run's metadata path by name is measured working, it has no
    NetworkPolicy to drop DNS, and nothing here measured that its metadata
    server answers the GKE address the same way.

    MUTATION: move the two entries from the GKE worker's environment into
    `worker_env`. The GKE tests above stay green and this goes red, naming the
    execution override.
    """
    profiles = _cloud_run_profiles()
    assert profiles, "no runner profile resolves to CLOUD_RUN_JOB; this would check nothing"

    leaked: dict[str, list[str]] = {}
    for profile_name in profiles:
        client = FakeJobsClient()
        dispatcher = CloudRunJobDispatcher(settings, client=client)
        task = make_task(profile_name)
        lease = make_lease(task)
        dispatcher.dispatch(
            task=task, lease=lease, profile=RUNNER_PROFILES[profile_name], tenant=tenant
        )
        overrides = client.runs[0]["overrides"].container_overrides
        override_names = {e.name for o in overrides for e in o.env}
        # The anchor: the override really is the worker's environment.
        assert "TASK_ID" in override_names, f"{profile_name}: no worker env in the override"

        job = client.created[0]["job"]
        job_names = {e.name for c in job.template.template.containers for e in c.env}
        shared = set(worker_env(task=task, lease=lease, tenant=tenant, settings=settings))

        for where, names in (
            (f"{profile_name}: Cloud Run execution override", override_names),
            (f"{profile_name}: Cloud Run Job the scheduler creates", job_names),
            (f"{profile_name}: worker_env (shared by both dispatchers)", shared),
        ):
            hit = sorted(names & set(METADATA_ENV))
            if hit:
                leaked[where] = hit

    assert not leaked, (
        f"the GKE-only metadata override reached Cloud Run: {leaked}. It belongs "
        f"on the GKE worker container alone."
    )


def _cloud_run_job_env_sources() -> dict[str, list[Path]]:
    """Every file in this repository a terraform Cloud Run Job's environment is built from.

    Terraform creates a `swarm-job-<tenant>-<profile>` Job for each Cloud Run
    profile a tenant holds, and the scheduler runs executions of it. That Job's
    container environment is assembled in three places:

      * terraform/infra -- `local.jobs[*].env` is `merge(local.common_env, ...)`,
        and `local.common_env` is `merge({...}, var.settings_env)` (locals.tf),
        with the variable's default in variables.tf. A `*.tfvars` in the root
        itself would be auto-loaded, so one is read too if it ever appears;
      * terraform/environments/<env>/<env>.tfvars -- the value of `settings_env`
        every plan in this repository uses (scripts/lib/common.sh `tf_var_file`,
        release.yml, terraform.yml). A key there lands in EVERY worker Job;
      * terraform/modules/cloud_run_jobs -- turns that map, and `secret_env`,
        into the container's env blocks, and could add a literal block of its own.
    """
    tf = REPO / "terraform"
    return {
        "terraform/infra": sorted([*(tf / "infra").glob("*.tf"), *(tf / "infra").glob("*.tfvars*")]),
        "terraform/modules/cloud_run_jobs": sorted((tf / "modules" / "cloud_run_jobs").glob("*.tf")),
        "terraform/environments": sorted((tf / "environments").glob("*/*.tfvars*")),
    }


def _hcl_code(line: str) -> str:
    """`line` without its `#` or `//` comment -- a comment OUTSIDE a string only.

    So `URL = "https://x", GCE_METADATA_HOST = "y"` keeps the name after the
    `//` in its URL, and a comment that merely names a variable is not taken
    for an assignment. `/* */` blocks are not stripped: a name inside one is
    reported, which errs the loud way.
    """
    out = []
    in_string = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if ch == "\\":
                out.append(line[i : i + 2])
                i += 2
                continue
            in_string = ch != '"'
        elif ch == '"':
            in_string = True
        elif ch == "#" or line.startswith("//", i):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def test_terraform_does_not_give_the_cloud_run_jobs_the_override():
    """No file a terraform Cloud Run Job's environment is built from names either variable.

    Those Jobs are the other half of what a Cloud Run worker sees: an execution
    override merges INTO the Job's environment, so a GCE_METADATA_* that
    terraform put there reaches every execution the scheduler runs, whatever
    `worker_env` says. The sources are listed in `_cloud_run_job_env_sources`.
    Comments may name the variables; code may not.

    MUTATION (pushed to this test's pull request, then reverted):
    GCE_METADATA_HOST in dev.tfvars' `settings_env`, and a literal
    GCE_METADATA_IP env block in modules/cloud_run_jobs/main.tf. The first
    version of this test read terraform/infra/*.tf alone and stayed green on
    both. This one fails naming both files.

    WHAT THIS DOES NOT SEE. It reads text, not a plan. A `-var settings_env=...`
    on a command line, or a tfvars file kept outside this repository, reaches
    the Jobs without passing through any file read here. And application.yml,
    which runs this file, does not trigger on a pull request that changes only
    terraform files, so a tfvars-only change meets this check on its push to
    main rather than on its pull request.
    """
    # The comment stripper, both ways, so the scan below is about something: a
    # name in a comment is not code, and a `//` inside a string is not a comment.
    assert "GCE_METADATA_HOST" not in _hcl_code('A = "x" # GCE_METADATA_HOST = "y"')
    assert "GCE_METADATA_HOST" in _hcl_code('m = { U = "https://x", GCE_METADATA_HOST = "y" }')

    sources = _cloud_run_job_env_sources()
    empty = sorted(where for where, paths in sources.items() if not paths)
    assert not empty, f"read no files under {empty}; the check would be about nothing there"

    # Every environment, derived from the directory rather than listed: a new
    # environment's tfvars is read without anyone remembering to add it.
    environments = {p for p in (REPO / "terraform" / "environments").iterdir() if p.is_dir()}
    unread = environments - {p.parent for paths in sources.values() for p in paths}
    assert not unread, f"no tfvars read for {sorted(str(p.relative_to(REPO)) for p in unread)}"

    assigned = []
    for paths in sources.values():
        for path in paths:
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if re.search(r"GCE_METADATA_[A-Z]+", _hcl_code(line)):
                    assigned.append(f"{path.relative_to(REPO)}:{number}")
    assert not assigned, (
        f"terraform names the GKE-only metadata override at {assigned}, in the "
        f"files the Cloud Run Jobs terraform creates take their environment from "
        f"(an environment's settings_env is merged into every one of those Jobs). "
        f"Cloud Run workers reach their metadata server by name and do not need it."
    )
