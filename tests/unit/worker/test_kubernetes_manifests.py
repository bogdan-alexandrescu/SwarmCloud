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

from swarm_common.models import Tenant, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"

TENANT = "eng"
PROJECT = "saga-agents-staging"


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
            "NAMESPACE": f"swarm-{TENANT}",
            "KSA_NAME": render.sanitize_name("swarm", TENANT),
            "GSA_EMAIL": f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
            "PROJECT_ID": PROJECT,
            "REGION": "us-central1",
            "PSS_ENFORCE": "baseline",
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


def test_pod_security_admission_is_labelled_on_the_namespace(tenant_docs):
    labels = one(tenant_docs, "Namespace")["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    # The target level is audited and warned about today so the gap is visible.
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"


def test_restricted_enforcement_is_one_flag_away(tenant_docs):
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(["tenant", "--tenant", TENANT, "--pss-enforce", "restricted"])
    ns = one(documents(buffer.getvalue()), "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


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
    role = one(tenant_docs, "Role")
    assert role["rules"] == [], "a worker needs nothing from the Kubernetes API"


def test_nothing_here_is_cluster_scoped(tenant_docs):
    kinds = {d["kind"] for d in tenant_docs}
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds


def test_the_role_binding_covers_every_account_a_pod_could_use(tenant_docs):
    binding = one(tenant_docs, "RoleBinding")
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


def test_a_provider_profile_projects_the_tenants_own_secret():
    """The secret name must match what Secret Manager holds for this tenant, and
    it lives in this tenant's namespace, so another tenant's pod cannot mount it.
    """
    container = render_job("claude-code")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    key = env["ANTHROPIC_API_KEY"]
    expected = Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz", created_at=utcnow()
    ).secret_name("anthropic")
    assert key["valueFrom"]["secretKeyRef"]["name"] == expected


def test_the_mock_profile_needs_no_credential_at_all():
    """Every smoke, concurrency and quota test runs `mock`. It must work for a
    tenant that has registered no provider key, so its Job must reference no
    secret."""
    container = render_job("mock")["spec"]["template"]["spec"]["containers"][0]
    assert all("valueFrom" not in entry for entry in container["env"])
    assert RUNNER_PROFILES["mock"].provider is None


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


def test_the_advisories_warn_and_fail_open(policy_docs):
    policy = one(policy_docs, "ValidatingAdmissionPolicy", "swarm-worker-hardening-advisories")
    assert policy["spec"]["failurePolicy"] == "Ignore", "an advisory must never block a pod"
    binding = one(
        policy_docs, "ValidatingAdmissionPolicyBinding", "swarm-worker-hardening-advisories"
    )
    assert set(binding["spec"]["validationActions"]) == {"Warn", "Audit"}


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


def test_the_renderer_agrees_with_the_dispatcher_about_names():
    """Both sanitise the same way; the service account name depends on it."""
    assert render.sanitize_name("swarm", "eng") == "swarm-eng"
    assert render.sanitize_name("task_9f3a") == "task-9f3a"
    assert render.sanitize_name("swarm", "u-alice") == "swarm-u-alice"
