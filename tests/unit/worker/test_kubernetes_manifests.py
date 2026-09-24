"""The Kubernetes manifests, held to the same invariants as the Python.

A YAML file that nobody executes in CI is a file that drifts. Everything in
`kubernetes/` is rendered here by the real renderer and then checked against the
frozen contract: sizing comes from `RESOURCE_CLASSES`, the secret name comes
from `Tenant.secret_name`, and the properties the platform depends on --
requests == limits, no Spot tolerations, no mounted API token, a default-deny
NetworkPolicy in both directions -- are asserted rather than reviewed.

The checks that matter most are the ones about ABSENCE. A missing `egress:` key,
a toleration nobody noticed, an `automountServiceAccountToken` that quietly went
back to its default: none of those make a manifest fail to apply, and all of
them are the difference between isolation and the appearance of it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

from quota_broker.accounts import AccountError, validate_label
from scheduler.dispatch import sanitize_name as dispatcher_sanitize_name
from swarm_common.identity import _TENANT_SAFE
from swarm_common.models import Tenant, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"

TENANT = "eng"
PROJECT = "saga-agents-staging"

#: The control plane's numeric uniqueIds in that project. See the assertion at
#: the bottom of this file for why a RoleBinding needs them at all.
SCHEDULER_UID = "117405034245659033603"
RECONCILER_UID = "108023754768362642341"


def _load_renderer() -> Any:
    """Import kubernetes/render.py by path; it is a script, not a package."""
    spec = importlib.util.spec_from_file_location("swarm_k8s_render", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load_renderer()


def documents(text: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == kind]


def one(docs: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    matches = [d for d in by_kind(docs, kind) if name is None or d["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind} {name or ''}, got {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def tenant_docs() -> list[dict[str, Any]]:
    text = render.render_files(
        render.TENANT_FILES,
        {
            "TENANT_ID": TENANT,
            # DERIVED, never spelled again. This line read f"swarm-{TENANT}"
            # and was the THIRD independent spelling of the tenant namespace
            # -- render.py had one, the scheduler had another. The suite
            # therefore asserted against the renderer's own mistake and
            # agreed with it.
            "NAMESPACE": f"{render.NAMESPACE_PREFIX}{TENANT}",
            "KSA_NAME": render.sanitize_name("swarm", TENANT),
            "GSA_EMAIL": f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
            # The control-plane RBAC subjects. Added with
            # rbac/dispatcher-rbac.yaml, which is what lets the scheduler
            # create a Job in a tenant namespace at all -- without it the
            # GKE backend cannot dispatch, which is how it sat broken.
            "SCHEDULER_GSA": f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com",
            "RECONCILER_GSA": f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com",
            # THE NUMERIC uniqueIds, and these are the real ones from
            # saga-agents-staging rather than invented digits. They are not
            # secret -- `gcloud iam service-accounts describe` prints them to
            # anyone who can read the project -- and using the real values means
            # this fixture matches what apply.sh renders, so a future change to
            # the subject shape fails here rather than only in a live cluster.
            "SCHEDULER_UID": SCHEDULER_UID,
            "RECONCILER_UID": RECONCILER_UID,
            "PROJECT_ID": PROJECT,
            "REGION": "us-central1",
            "PSS_ENFORCE": "restricted",
            "POD_CIDR": "10.0.0.0/8",
            "SERVICE_CIDR": "34.118.224.0/20",
            "QUOTA_PODS": "8",
            "QUOTA_JOBS": "32",
            "QUOTA_CPU": "64",
            "QUOTA_MEMORY": "128Gi",
            "QUOTA_EPHEMERAL": "320Gi",
        },
    )
    return documents(text)


def render_job(profile: str = "browser", **overrides: Any) -> dict[str, Any]:
    argv = [
        "job",
        "--tenant",
        TENANT,
        "--profile",
        profile,
        "--task",
        overrides.get("task", "task_9f3a"),
        "--attempt",
        overrides.get("attempt", "att_7b21"),
        "--lease",
        overrides.get("lease", "lease_c4"),
        "--generation",
        str(overrides.get("generation", 3)),
    ]
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(argv)
    docs = documents(buffer.getvalue())
    assert len(docs) == 1
    return docs[0]


# ---------------------------------------------------------------------------
# The namespace
# ---------------------------------------------------------------------------


def test_the_namespace_carries_the_labels_every_other_component_selects_on(tenant_docs):
    ns = one(tenant_docs, "Namespace")
    labels = ns["metadata"]["labels"]
    # scripts/status.sh and scripts/configure-kubectl.sh select on this exact
    # value, and scripts/register-tenant.sh re-applies it with --overwrite on
    # every run. Choosing a different one here would flip-flop the label.
    assert labels["managed-by"] == "swarm-terraform"
    # The reconciler's namespace GC requires the tenant label alongside it.
    assert labels["swarm-tenant"] == TENANT
    # The admission policy bindings select namespaces on this.
    assert labels["app.kubernetes.io/part-of"] == "swarm"


def test_the_namespace_is_gc_eligible_by_the_reconcilers_own_rule(tenant_docs):
    """The manifest and the reconciler must agree about what it may collect."""
    from reconciler.backends import is_namespace_gc_eligible, is_swarm_managed

    labels = one(tenant_docs, "Namespace")["metadata"]["labels"]
    assert is_swarm_managed(labels)
    assert is_namespace_gc_eligible(labels)


def test_pod_security_admission_enforces_restricted_by_default(tenant_docs):
    """The browser profile runs Chromium with its own sandbox off, on the
    argument that the isolation is the pod. That argument is only true while the
    pod controls are ENFORCED rather than audited, so this label is load-bearing
    for the one profile that runs on this path."""
    labels = one(tenant_docs, "Namespace")["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"


def test_lowering_enforcement_stays_possible_for_an_incident(tenant_docs):
    """`baseline` is an escape hatch, not a setting to leave in place -- and
    audit and warn stay at `restricted` when it is used, so lowering enforcement
    leaves a trail."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(["tenant", "--tenant", TENANT, "--pss-enforce", "baseline"])
    labels = one(documents(buffer.getvalue()), "Namespace")["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"


def test_the_dispatchers_gke_manifest_satisfies_restricted():
    """The namespace enforces `restricted`, so a dispatcher manifest that does
    not satisfy it produces Pending pods for every browser task -- a failure that
    reads as a scheduling problem. Checking the dispatcher's OWN manifest here is
    what makes a regression on that side fail in CI instead of in a cluster.

    `apps/scheduler` belongs to another track; this only reads it.
    """
    from scheduler.dispatch import CONTAINER_SECURITY_CONTEXT, POD_SECURITY_CONTEXT

    assert POD_SECURITY_CONTEXT["runAsNonRoot"] is True
    assert POD_SECURITY_CONTEXT["runAsUser"] == 10001
    assert POD_SECURITY_CONTEXT["seccompProfile"]["type"] == "RuntimeDefault"

    assert CONTAINER_SECURITY_CONTEXT["allowPrivilegeEscalation"] is False
    assert CONTAINER_SECURITY_CONTEXT["privileged"] is False
    assert CONTAINER_SECURITY_CONTEXT["runAsNonRoot"] is True
    assert CONTAINER_SECURITY_CONTEXT["capabilities"]["drop"] == ["ALL"]
    assert CONTAINER_SECURITY_CONTEXT["seccompProfile"]["type"] == "RuntimeDefault"

    # The Job template this directory owns must set the same fields, or the two
    # paths would be hardened differently for the same workload.
    spec = render_job("browser")["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    container = spec["containers"][0]["securityContext"]
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"]["drop"] == ["ALL"]


# ---------------------------------------------------------------------------
# Quota and limits
# ---------------------------------------------------------------------------


def test_the_quota_forbids_everything_a_worker_never_creates(tenant_docs):
    hard = one(tenant_docs, "ResourceQuota")["spec"]["hard"]
    for forbidden in (
        "count/services",
        "count/persistentvolumeclaims",
        "count/deployments.apps",
        "count/statefulsets.apps",
        "count/daemonsets.apps",
        "count/cronjobs.batch",
    ):
        assert hard[forbidden] == "0", forbidden
    assert int(hard["pods"]) > 0


def test_quota_requests_and_limits_are_the_same_numbers(tenant_docs):
    hard = one(tenant_docs, "ResourceQuota")["spec"]["hard"]
    for resource in ("cpu", "memory", "ephemeral-storage"):
        assert hard[f"requests.{resource}"] == hard[f"limits.{resource}"], resource


def test_the_limit_range_default_cannot_produce_a_burstable_pod(tenant_docs):
    """A default request below the default limit is the OOM-kill configuration.

    It is also invisible: a pod that omits resources entirely inherits both
    numbers from here, so if they differ, invariant 7 is broken for every pod
    this repository did not write.
    """
    container = next(
        item
        for item in one(tenant_docs, "LimitRange")["spec"]["limits"]
        if item["type"] == "Container"
    )
    assert container["default"] == container["defaultRequest"]


def test_the_limit_range_ceiling_fits_the_frozen_resource_classes(tenant_docs):
    container = next(
        item
        for item in one(tenant_docs, "LimitRange")["spec"]["limits"]
        if item["type"] == "Container"
    )
    largest_cpu = max(rc.cpu for rc in RESOURCE_CLASSES.values())
    largest_memory = max(rc.memory_gib for rc in RESOURCE_CLASSES.values())
    assert float(container["max"]["cpu"]) >= largest_cpu
    assert int(container["max"]["memory"].removesuffix("Gi")) >= largest_memory


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_no_service_account_mounts_a_kubernetes_api_token(tenant_docs):
    accounts = by_kind(tenant_docs, "ServiceAccount")
    assert accounts, "the tenant namespace must define its service accounts"
    for account in accounts:
        assert account.get("automountServiceAccountToken") is False, account["metadata"]["name"]


def test_the_default_service_account_is_disarmed_too(tenant_docs):
    """A pod created by hand during an incident is exactly when `default` gets
    used, so it cannot be left with a mounted token."""
    default = one(tenant_docs, "ServiceAccount", "default")
    assert default["automountServiceAccountToken"] is False


def test_the_service_account_the_dispatcher_asks_for_exists(tenant_docs):
    """`dispatch.py` sets serviceAccountName to sanitize_name("swarm", tenant).

    A pod naming a service account that does not exist stays Pending until its
    deadline expires, which reads as a scheduling problem rather than a missing
    object, so the name the dispatcher uses must be one of the ones created here.
    """
    names = {a["metadata"]["name"] for a in by_kind(tenant_docs, "ServiceAccount")}
    assert render.sanitize_name("swarm", TENANT) in names
    # ...and the one scripts/register-tenant.sh creates and binds.
    assert "swarm-worker" in names


def test_every_worker_service_account_is_bound_to_workload_identity(tenant_docs):
    for account in by_kind(tenant_docs, "ServiceAccount"):
        if account["metadata"]["name"] == "default":
            continue
        annotation = account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"]
        assert annotation.endswith(f"@{PROJECT}.iam.gserviceaccount.com")


def test_workers_are_granted_no_kubernetes_api_access_at_all(tenant_docs):
    # NAMED, because the namespace now holds three Roles: the worker's (empty,
    # below), and the control plane's swarm-dispatcher / swarm-reaper added
    # with rbac/dispatcher-rbac.yaml. An unnamed `one()` used to be
    # unambiguous and silently became "assert there is only one Role".
    role = one(tenant_docs, "Role", "swarm-worker")
    assert role["rules"] == [], "a worker needs nothing from the Kubernetes API"


def test_nothing_here_is_cluster_scoped(tenant_docs):
    kinds = {d["kind"] for d in tenant_docs}
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds


def test_the_role_binding_covers_every_account_a_pod_could_use(tenant_docs):
    binding = one(tenant_docs, "RoleBinding", "swarm-worker")
    subjects = {s["name"] for s in binding["subjects"]}
    assert render.sanitize_name("swarm", TENANT) in subjects
    assert "swarm-worker" in subjects


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def test_the_default_policy_denies_both_directions(tenant_docs):
    policy = one(tenant_docs, "NetworkPolicy", "swarm-default-deny")
    assert policy["spec"]["podSelector"] == {}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    # The absence of these keys is the denial. An empty list would also deny,
    # but a key present is a key someone appends to.
    assert "ingress" not in policy["spec"]
    assert "egress" not in policy["spec"]


def test_no_policy_ever_allows_ingress(tenant_docs):
    """A worker listens on nothing, so every inbound connection is a mistake."""
    for policy in by_kind(tenant_docs, "NetworkPolicy"):
        assert not policy["spec"].get("ingress"), policy["metadata"]["name"]


def test_egress_is_allowed_to_every_pod_not_just_labelled_ones(tenant_docs):
    """The dispatcher labels its pods `swarm-task`/`swarm-tenant` and nothing
    else. An egress policy keyed on any other label would match no pod, leave
    the default deny in force, and break every browser task with a DNS timeout.
    """
    policy = one(tenant_docs, "NetworkPolicy", "swarm-allow-worker-egress")
    assert policy["spec"]["podSelector"] == {}


def test_egress_reaches_dns_and_the_workload_identity_metadata_server(tenant_docs):
    policy = one(tenant_docs, "NetworkPolicy", "swarm-allow-worker-egress")
    rules = policy["spec"]["egress"]

    dns = [r for r in rules if any(p.get("port") == 53 for p in r.get("ports", []))]
    assert dns, "without DNS every hostname in the platform fails to resolve"
    protocols = {p["protocol"] for rule in dns for p in rule["ports"]}
    assert protocols == {"UDP", "TCP"}, "TCP 53 carries responses over 512 bytes"

    metadata = [
        rule
        for rule in rules
        if any(
            peer.get("ipBlock", {}).get("cidr") == "169.254.169.254/32"
            for peer in rule.get("to", [])
        )
    ]
    assert metadata, "Workload Identity tokens come from the metadata server"
    ports = {p["port"] for rule in metadata for p in rule["ports"]}
    assert 988 in ports, "988 is the GKE Workload Identity endpoint"


def test_egress_to_the_internet_excludes_the_cluster(tenant_docs):
    """The cross-tenant hop is pod-to-pod, and it is carved out here.

    NetworkPolicy has no selector for "not in this cluster", so an ipBlock with
    an except list is the only way to say it, and the service range has to be
    listed explicitly because GKE Autopilot's default (34.118.224.0/20) is
    public address space that no RFC1918 entry covers.
    """
    policy = one(tenant_docs, "NetworkPolicy", "swarm-allow-worker-egress")
    internet = next(
        rule
        for rule in policy["spec"]["egress"]
        if any(peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0" for peer in rule.get("to", []))
    )
    block = next(
        peer["ipBlock"] for peer in internet["to"] if peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"
    )
    excepted = set(block["except"])
    assert "10.0.0.0/8" in excepted          # the pod range, by default
    assert "34.118.224.0/20" in excepted     # the Autopilot service range
    assert "172.16.0.0/12" in excepted
    assert "192.168.0.0/16" in excepted
    assert "169.254.0.0/16" in excepted
    # Every excepted range must be a duplicate-free set; a repeated entry is a
    # validation error the API server raises only at apply time.
    assert len(block["except"]) == len(excepted)


# ---------------------------------------------------------------------------
# Worker Job templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["mock", "claude-code", "browser"])
def test_a_rendered_job_never_asks_for_spot_capacity(profile):
    job = render_job(profile)
    spec = job["spec"]["template"]["spec"]
    assert "tolerations" not in spec, "Spot and extended run time are incompatible"
    assert "nodeSelector" not in spec


@pytest.mark.parametrize("profile", ["mock", "claude-code", "browser"])
def test_a_rendered_job_sets_requests_equal_to_limits(profile):
    job = render_job(profile)
    rc = RESOURCE_CLASSES[RUNNER_PROFILES[profile].resource_class]
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == container["resources"]["limits"]
    # ...and the numbers come from the frozen catalogue, not from this file.
    assert container["resources"]["limits"]["cpu"] == str(int(rc.cpu))
    assert container["resources"]["limits"]["memory"] == f"{rc.memory_gib}Gi"
    assert container["resources"]["limits"]["ephemeral-storage"] == f"{rc.disk_gib}Gi"


@pytest.mark.parametrize("profile", ["mock", "claude-code", "browser"])
def test_a_rendered_job_never_overrides_the_container_command(profile):
    """The image's ENTRYPOINT starts the WORKER, which validates the fencing
    generation, restores a checkpoint and then starts the runner as a supervised
    child. Overriding `command` with the profile's command starts the runner
    directly -- no fencing check, no checkpointing, no heartbeat, no lease
    release. The profile's command is the runner's entrypoint, not the
    container's.
    """
    container = render_job(profile)["spec"]["template"]["spec"]["containers"][0]
    assert "command" not in container
    assert "args" not in container


@pytest.mark.parametrize("profile", ["mock", "browser"])
def test_a_rendered_job_refuses_kubernetes_level_retries(profile):
    """A retry is a new attempt with a new generation, minted by the scheduler.
    A Kubernetes retry re-runs the agent under the old one."""
    job = render_job(profile)
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


@pytest.mark.parametrize("profile", ["mock", "browser"])
def test_a_rendered_job_is_not_evictable_during_a_scale_down(profile):
    job = render_job(profile)
    annotation = "cluster-autoscaler.kubernetes.io/safe-to-evict"
    assert job["metadata"]["annotations"][annotation] == "false"
    assert job["spec"]["template"]["metadata"]["annotations"][annotation] == "false"


@pytest.mark.parametrize("profile", ["mock", "browser"])
def test_a_rendered_job_runs_non_root_with_no_capabilities(profile):
    spec = render_job(profile)["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    # uid 10001 is the user the runtime images create.
    assert spec["securityContext"]["runAsUser"] == 10001
    container = spec["containers"][0]
    security = container["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["privileged"] is False
    assert security["capabilities"]["drop"] == ["ALL"]
    assert security["readOnlyRootFilesystem"] is True


@pytest.mark.parametrize("profile", ["mock", "browser"])
def test_a_read_only_root_still_has_somewhere_to_write(profile):
    """readOnlyRootFilesystem is only safe if every path the toolchain writes to
    is mounted. npm, uv and git all write under $HOME."""
    spec = render_job(profile)["spec"]["template"]["spec"]
    mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert {"/workspace", "/tmp", "/home/swarm"} <= mounts
    volumes = {v["name"] for v in spec["volumes"]}
    assert {m["name"] for m in spec["containers"][0]["volumeMounts"]} <= volumes


@pytest.mark.parametrize("profile", ["mock", "browser"])
def test_a_rendered_job_mounts_no_api_token(profile):
    spec = render_job(profile)["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False


def test_the_browser_job_sizes_dev_shm():
    """Chromium's renderers talk through /dev/shm; the 64 MiB runtime default is
    what makes headless Chrome crash under load, and Cloud Run cannot size it --
    which is the whole reason the browser profile runs on GKE."""
    spec = render_job("browser")["spec"]["template"]["spec"]
    dshm = next(v for v in spec["volumes"] if v["name"] == "dshm")
    assert dshm["emptyDir"]["medium"] == "Memory"
    assert dshm["emptyDir"]["sizeLimit"] == "2Gi"
    assert any(m["mountPath"] == "/dev/shm" for m in spec["containers"][0]["volumeMounts"])


@pytest.mark.parametrize("profile", ["mock", "generic", "claude-code", "codex", "browser"])
def test_no_rendered_job_puts_a_provider_key_in_the_containers_environment(profile):
    """Not even the profiles that need one.

    Every process in the container runs as uid 10001, so a variable in the
    worker's environment is readable at /proc/1/environ by the runner child, by
    anything the agent spawns and by a `cat` in a generic task. Projecting the
    key here would hand it to all of them, and the env allowlist in
    `workspace.child_env` cannot take back what PID 1 was started with.

    The worker resolves the key itself, from Secret Manager, as the tenant's own
    GSA -- see `lifecycle._build_child_env` -- and puts it only into the
    environment of the single child that needs it.
    """
    container = render_job(profile)["spec"]["template"]["spec"]["containers"][0]
    names = [entry["name"] for entry in container["env"]]
    assert all("valueFrom" not in entry for entry in container["env"])
    for secret_name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GIT_TOKEN"):
        assert secret_name not in names


def test_a_rendered_job_names_no_kubernetes_secret_because_none_is_created():
    """`secretKeyRef` names a KUBERNETES Secret, and nothing in this repository
    creates one: a tenant's provider key lives in Secret Manager as
    `swarm-tenant-<tenant>-<provider>`, which the worker reads through Workload
    Identity. A reference to a Secret that does not exist is not a leak, it is a
    pod stuck in CreateContainerConfigError."""
    expected = Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz", created_at=utcnow()
    ).secret_name("anthropic")
    assert expected == f"swarm-tenant-{TENANT}-anthropic"
    for profile in ("claude-code", "browser"):
        rendered = yaml.safe_dump(render_job(profile))
        assert "secretKeyRef" not in rendered
        assert expected not in rendered


def test_the_mock_profile_needs_no_credential_at_all():
    """Every smoke, concurrency and quota test runs `mock`. It must work for a
    tenant that has registered no provider key, so its Job must reference no
    secret."""
    container = render_job("mock")["spec"]["template"]["spec"]["containers"][0]
    assert all("valueFrom" not in entry for entry in container["env"])
    assert RUNNER_PROFILES["mock"].provider is None


def test_no_rendered_job_injects_a_gcs_prefix_the_worker_does_not_read():
    """`WorkerConfig.from_env` never looks up GCS_PREFIX; the worker derives
    `tenants/<tenant>/tasks/<task>/attempts/<attempt>` from identifiers it
    already has. The value this template used to inject was `tenants/<tenant>`,
    three levels short -- a dead variable that pointed an operator chasing
    artifacts at the wrong path."""
    for profile in ("mock", "browser"):
        container = render_job(profile)["spec"]["template"]["spec"]["containers"][0]
        assert "GCS_PREFIX" not in [entry["name"] for entry in container["env"]]


def test_a_rendered_job_carries_the_identifiers_the_reconciler_reads():
    """The reconciler matches an execution to a lease through these variables.

    It reads them from the environment rather than from labels because a label
    value has been through name sanitisation: `task_9f3a` becomes `task-9f3a`,
    which matches no Firestore document, and an execution whose task cannot be
    found is treated as an orphan and terminated.
    """
    from reconciler.backends import ATTEMPT_ENV, GENERATION_ENV, TASK_ENV, TENANT_ENV

    container = render_job("browser")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env[TASK_ENV] == "task_9f3a"
    assert env[ATTEMPT_ENV] == "att_7b21"
    assert env[TENANT_ENV] == TENANT
    assert env[GENERATION_ENV] == "3"


def test_a_rendered_job_passes_no_image_command_or_sizing_from_a_caller():
    """Invariant 10, checked at the manifest: the only caller-derived values in
    the environment are identifiers."""
    container = render_job("browser")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"] for e in container["env"]}
    for forbidden in ("IMAGE", "COMMAND", "ARGS", "CPU", "MEMORY", "RESOURCE_CLASS", "BACKEND"):
        assert forbidden not in env


# ---------------------------------------------------------------------------
# Admission policy
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def policy_docs() -> list[dict[str, Any]]:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(["policies"])
    return documents(buffer.getvalue())


def test_every_policy_is_bound_and_scoped_to_swarm_namespaces(policy_docs):
    policies = {d["metadata"]["name"] for d in by_kind(policy_docs, "ValidatingAdmissionPolicy")}
    bindings = by_kind(policy_docs, "ValidatingAdmissionPolicyBinding")
    assert {b["spec"]["policyName"] for b in bindings} == policies
    for binding in bindings:
        selector = binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"]
        assert selector == {"app.kubernetes.io/part-of": "swarm"}, (
            "an unscoped binding would apply to other teams' namespaces in this "
            "shared cluster"
        )


def test_the_hard_constraints_deny_and_fail_closed(policy_docs):
    for name in ("swarm-worker-pod-constraints", "swarm-worker-job-constraints"):
        policy = one(policy_docs, "ValidatingAdmissionPolicy", name)
        assert policy["spec"]["failurePolicy"] == "Fail"
        binding = one(policy_docs, "ValidatingAdmissionPolicyBinding", name)
        assert binding["spec"]["validationActions"] == ["Deny"]


def test_the_pod_hardening_policy_denies_and_fails_closed(policy_docs):
    """It warned rather than denied while the dispatcher's manifest could not
    satisfy it. That manifest now sets both securityContext blocks and mounts no
    API token, so the check that the browser profile's `chromium_sandbox=False`
    argument rests on is a refusal rather than a log line."""
    policy = one(policy_docs, "ValidatingAdmissionPolicy", "swarm-worker-pod-hardening")
    assert policy["spec"]["failurePolicy"] == "Fail"
    binding = one(policy_docs, "ValidatingAdmissionPolicyBinding", "swarm-worker-pod-hardening")
    assert "Deny" in binding["spec"]["validationActions"]
    # Audit too: a pod created by a controller rather than by a person shows up
    # only in the API server's audit log.
    assert "Audit" in binding["spec"]["validationActions"]


def test_the_hardening_policy_still_checks_every_field_restricted_requires(policy_docs):
    policy = one(policy_docs, "ValidatingAdmissionPolicy", "swarm-worker-pod-hardening")
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])
    for field in (
        "automountServiceAccountToken",
        "allowPrivilegeEscalation",
        "capabilities",
        "runAsNonRoot",
        "seccompProfile",
    ):
        assert field in expressions


def test_no_policy_in_this_directory_is_advisory_any_more(policy_docs):
    """An admission policy that only warns is a control an operator believes
    they have. Every binding here denies."""
    for binding in by_kind(policy_docs, "ValidatingAdmissionPolicyBinding"):
        actions = binding["spec"]["validationActions"]
        assert "Deny" in actions, binding["metadata"]["name"]
    for policy in by_kind(policy_docs, "ValidatingAdmissionPolicy"):
        assert policy["spec"]["failurePolicy"] == "Fail", policy["metadata"]["name"]


def test_each_policy_matches_exactly_one_kind(policy_docs):
    """A policy's CEL is compiled against the schema of what it matches, so an
    expression reaching `spec.template.spec` cannot also type-check against a
    Pod. One kind per policy removes the question."""
    for policy in by_kind(policy_docs, "ValidatingAdmissionPolicy"):
        resources = {
            resource
            for rule in policy["spec"]["matchConstraints"]["resourceRules"]
            for resource in rule["resources"]
        }
        assert len(resources) == 1, policy["metadata"]["name"]


def test_spot_and_backoff_are_the_rules_that_deny(policy_docs):
    pods = one(policy_docs, "ValidatingAdmissionPolicy", "swarm-worker-pod-constraints")
    jobs = one(policy_docs, "ValidatingAdmissionPolicy", "swarm-worker-job-constraints")
    pod_expressions = " ".join(v["expression"] for v in pods["spec"]["validations"])
    job_expressions = " ".join(v["expression"] for v in jobs["spec"]["validations"])

    assert "gke-spot" in pod_expressions and "gke-spot" in job_expressions
    assert "gke-preemptible" in pod_expressions
    assert "backoffLimit" in job_expressions
    assert "hostPath" in pod_expressions
    # Quantity comparison, not string equality: "4" and "4000m" are the same
    # amount of CPU and different strings.
    assert "quantity(" in pod_expressions


def test_the_rendered_browser_job_satisfies_the_advisory_policy(policy_docs):
    """The template is what the advisories describe; if it did not pass them,
    flipping the binding to Deny could never be a one-line change."""
    spec = render_job("browser")["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    for container in spec["containers"]:
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert "ALL" in container["securityContext"]["capabilities"]["drop"]
        assert container["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        assert container["securityContext"]["runAsNonRoot"] is True


# ---------------------------------------------------------------------------
# The renderer itself
# ---------------------------------------------------------------------------


def test_an_unsubstituted_placeholder_is_a_hard_error():
    """`name: __NAMESPACE__` is a valid-looking string that would create a real
    namespace called `__NAMESPACE__`."""
    with pytest.raises(SystemExit):
        render.substitute("name: __NAMESPACE__\n", {})


#: Inputs chosen to separate a real copy of the dispatcher's `sanitize_name`
#: from a plausible one: every branch of it is here. The uppercase and dotted
#: cases exercise the character class, the long ones exercise truncation and the
#: appended digest (the part that stops two long tenant names colliding), the
#: leading-digit case exercises the `s` prefix, and the dash cases exercise the
#: collapse and the strip.
SANITIZE_MATRIX = (
    ("swarm", "eng"),
    ("task_9f3a",),
    ("swarm", "u-alice"),
    ("swarm", "job", "eng", "claude-code"),
    ("swarm", "", "eng"),
    ("Eng.Team",),
    ("eng--team",),
    ("--eng--",),
    ("9lives",),
    ("tenant_with_underscores", "and.dots"),
    ("a" * 80,),
    ("x" * 70, "y" * 70),
)


def test_the_renderer_agrees_with_the_dispatcher_about_names():
    """Both sanitise the same way; the service account name depends on it.

    This used to assert three hardcoded strings, which is not the same claim:
    it would have stayed green through any change made to BOTH copies, and
    through any change to the dispatcher alone. The dispatcher is called here so
    that a change on either side fails, which is the only version of this test
    that is about agreement.
    """
    assert render.sanitize_name("swarm", "eng") == "swarm-eng"
    assert render.sanitize_name("task_9f3a") == "task-9f3a"
    assert render.sanitize_name("swarm", "u-alice") == "swarm-u-alice"
    for parts in SANITIZE_MATRIX:
        assert render.sanitize_name(*parts) == dispatcher_sanitize_name(*parts), parts


def test_the_renderer_slugifies_with_the_frozen_character_class():
    """The reconciler reverse-maps a Job label to a Firestore id by re-slugging it
    (`reconciler/detect.py` `sanitised()`), so a renderer with its own idea of
    which characters survive produces attempts the reconciler cannot resolve --
    it then either reclaims a live one or misses an orphan.

    The source is read for the import because `re.compile` CACHES: a local
    `re.compile(r"[^a-z0-9-]+")` returns the very same object the frozen module
    compiled, so `render._NAME_SAFE is _TENANT_SAFE` stays true even after the
    import is replaced by a fourth copy. An identity assertion here would be a
    test that passes while the thing it is about has been undone.
    """
    assert render._NAME_SAFE.pattern == _TENANT_SAFE.pattern
    for raw in ("task_9f3a", "Eng.Team", "a b", "x/y", "u-alice", "9lives", "caf\u00e9"):
        assert render._NAME_SAFE.sub("-", raw.lower()) == _TENANT_SAFE.sub("-", raw.lower())

    source = (KUBERNETES / "render.py").read_text()
    assert "from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE" in source
    assert "_NAME_SAFE = re.compile" not in source


def test_the_renderer_enforces_the_brokers_account_label_rule():
    """`--account` is operator-supplied, and this file's whole purpose is to
    validate every interpolated value before it reaches the YAML.

    It carried the generic 63-character RFC1123 pattern while
    `quota_broker.accounts` enforces a 40-character one, so it accepted labels
    that could never have been registered -- a value the renderer waved through
    and the rest of the platform would refuse. The pattern object itself is the
    broker's now; these cases assert the consequence, so that swapping it back
    for a copy fails even if the copy starts out identical."""
    assert render._VALUE_PATTERNS["ACCOUNT_LABEL"] is render._ACCOUNT_LABEL

    longest_legal = "a" * 40
    assert validate_label(longest_legal) == longest_legal
    assert render.check_values({"ACCOUNT_LABEL": longest_legal})

    for refused in ("a" * 41, "a" * 63, "-leading", "trailing-", "Upper", "under_score", ""):
        with pytest.raises(AccountError):
            validate_label(refused)
        with pytest.raises(SystemExit):
            render.check_values({"ACCOUNT_LABEL": refused})


def _render_v2_job(*extra: str) -> dict[str, Any]:
    """The gvisor shape, which is the template that carries `__ACCOUNT_LABEL__`.

    `restricted` is refused for it on purpose (the v2 pod runs as root), so the
    namespace level is passed explicitly rather than left at the default.
    """
    import contextlib
    import io

    argv = [
        "job",
        "--tenant", TENANT,
        "--profile", "claude-code",
        "--task", "task_9f3a",
        "--attempt", "att_7b21",
        "--lease", "lease_c4",
        "--generation", "3",
        "--runtime", "gvisor",
        "--pss-enforce", "baseline",
        *extra,
    ]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(argv)
    docs = documents(buffer.getvalue())
    assert len(docs) == 1
    return docs[0]


def _account_annotation(doc: dict[str, Any]) -> str:
    for meta in (doc["spec"]["template"]["metadata"], doc["metadata"]):
        value = meta.get("annotations", {}).get("swarm.saga.xyz/account")
        if value is not None:
            return value
    raise AssertionError("the v2 job no longer records which account it is burning")


def test_a_rendered_job_records_an_account_the_broker_could_have_issued():
    """The default is `unassigned`, which has to satisfy the same rule: a default
    the broker would refuse is a manifest that only renders because nobody
    checked it. And a label past the broker's cap must not reach the YAML at
    all -- it did before this, because the renderer allowed 63 characters."""
    default = _account_annotation(_render_v2_job())
    assert default == "unassigned"
    assert validate_label(default) == default

    longest_legal = "a" * 40
    assert _account_annotation(_render_v2_job("--account", longest_legal)) == longest_legal

    with pytest.raises(SystemExit):
        _render_v2_job("--account", "a" * 41)


# ---------------------------------------------------------------------------
# The control plane's own access to a tenant namespace
# ---------------------------------------------------------------------------
#
# Without these the GKE backend cannot dispatch at all. On 2026-09-23 every
# `browser` task the platform had ever accepted -- seven, over two days --
# failed with `jobs.batch is forbidden`, and nothing in this suite would have
# noticed, because the namespace it renders had no control-plane RBAC in it to
# assert on.


def test_the_scheduler_may_create_jobs_in_a_tenant_namespace(tenant_docs):
    role = one(tenant_docs, "Role", "swarm-dispatcher")
    verbs = {v for rule in role["rules"] if "jobs" in rule["resources"] for v in rule["verbs"]}
    assert "create" in verbs, (
        "the scheduler cannot create a Job, which is the whole of dispatching to GKE"
    )
    assert "get" in verbs and "list" in verbs, "it must be able to read back what it created"


def test_the_scheduler_can_never_delete_a_job(tenant_docs):
    """Reaping is the reconciler's. Keeping them apart means a mistake in one
    is not an escalation into the other -- the same reasoning worker-rbac.yaml
    gives for its own pair, and the same split as the swarmGkeDispatcher /
    swarmGkeReaper IAM roles."""
    role = one(tenant_docs, "Role", "swarm-dispatcher")
    for rule in role["rules"]:
        assert "delete" not in rule["verbs"], f"swarm-dispatcher may delete {rule['resources']}"


def test_the_reconciler_may_delete_but_never_create(tenant_docs):
    role = one(tenant_docs, "Role", "swarm-reaper")
    verbs = {v for rule in role["rules"] if "jobs" in rule["resources"] for v in rule["verbs"]}
    assert "delete" in verbs, "the reconciler cannot reap a finished Job"
    for rule in role["rules"]:
        assert "create" not in rule["verbs"], f"swarm-reaper may create {rule['resources']}"


def test_the_control_plane_bindings_name_the_real_service_accounts(tenant_docs):
    """A RoleBinding to a subject that does not exist applies cleanly and
    grants nothing, so a typo here fails looking exactly like success."""
    for binding_name, expected in (
        ("swarm-dispatcher", f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com"),
        ("swarm-reaper", f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com"),
    ):
        binding = one(tenant_docs, "RoleBinding", binding_name)
        subjects = binding["subjects"]
        assert [s["name"] for s in subjects] == [expected]
        # `User`, not `ServiceAccount`: the scheduler runs on Cloud Run as a
        # Google identity and has no Kubernetes ServiceAccount of its own.
        assert {s["kind"] for s in subjects} == {"User"}


def test_the_namespace_the_renderer_builds_is_the_one_the_scheduler_dispatches_into(tenant_docs):
    """THE BUG THIS PINS. `render.py` used NAMESPACE_PREFIX = "swarm-" while
    apps/scheduler/scheduler/dispatch.py uses "swarm-tenant-{tenant}", so the
    provisioner created `swarm-eng` and the dispatcher wrote into
    `swarm-tenant-eng`. Kubernetes authorises before it resolves, so the
    missing namespace surfaced as `jobs.batch is forbidden` -- a 403 about
    permissions, never a 404 -- and sent three investigations at IAM."""
    from scheduler.dispatch import GkeTarget  # noqa: PLC0415

    template = GkeTarget.namespace_template
    expected = template.format(tenant=TENANT)
    namespace = one(tenant_docs, "Namespace")["metadata"]["name"]
    assert namespace == expected, (
        f"the renderer builds {namespace} and the scheduler dispatches into {expected}; "
        "a Job created into a namespace that does not exist is reported as forbidden, "
        "not as missing"
    )


def test_every_gsa_rbac_subject_is_also_bound_by_numeric_unique_id(tenant_docs):
    """THE EIGHT-MONTH BUG, AS A RULE.

    Every GKE dispatch this platform ever attempted failed with

        jobs.batch is forbidden: User "117405034245659033603" cannot create
        resource "jobs" in API group "batch" in the namespace "swarm-tenant-eng"

    while `swarm-dispatcher`'s RoleBinding named
    `swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com` and nothing
    else. Both were correct descriptions of the same identity and only one of
    them was the SUBJECT: the scheduler reaches the Kubernetes API with a Google
    OAuth access token (dispatch.py's `install_google_bearer_token`), and on that
    path GKE resolves the caller to the service account's numeric uniqueId. The
    RoleBinding applied cleanly, validated, diffed clean, and authorised nobody.

    Nothing offline could have caught that -- the manifest was valid and the
    identity existed. What CAN be caught offline is the asymmetry: a binding that
    names a Google service account by only ONE of its two names. That is what
    this asserts, for every RoleBinding the tenant render produces, so neither
    spelling can be dropped again.

    MUTATION: delete either `__SCHEDULER_UID__` or `__RECONCILER_UID__` subject
    from kubernetes/rbac/dispatcher-rbac.yaml. This names the binding and the
    account whose second spelling went missing.
    """
    gsa_suffix = ".iam.gserviceaccount.com"
    uid_by_account = {
        f"swarm-scheduler@{PROJECT}{gsa_suffix}": SCHEDULER_UID,
        f"swarm-reconciler@{PROJECT}{gsa_suffix}": RECONCILER_UID,
    }

    bindings = by_kind(tenant_docs, "RoleBinding")
    assert bindings, "the tenant render produced no RoleBinding; this test would check nothing"

    checked = 0
    for binding in bindings:
        names = {str(s.get("name", "")) for s in binding.get("subjects", [])}
        for account, uid in uid_by_account.items():
            if account not in names:
                continue
            checked += 1
            assert uid in names, (
                f"RoleBinding {binding['metadata']['name']} binds {account} by email "
                f"but not by its uniqueId {uid}. GKE presents a service account "
                f"authenticating with an OAuth access token by uniqueId, so this "
                f"binding applies cleanly and authorises nobody -- which is exactly "
                f"how every GKE dispatch failed before 2026-09-24."
            )

    # NOT AN EMPTY SWEEP. If the subject spellings change shape, the loop above
    # matches nothing and passes silently -- the failure mode this repository
    # keeps paying for. Two bindings name a control-plane account: swarm-dispatcher
    # (the scheduler) and swarm-reaper (the reconciler).
    assert checked == 2, (
        f"expected to check 2 control-plane bindings, checked {checked}. The subject "
        f"spellings changed and this assertion stopped looking at anything."
    )
