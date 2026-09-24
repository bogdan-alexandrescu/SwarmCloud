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

import argparse
import importlib.util
import re
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


def tenant_values(*argv: str) -> dict[str, str]:
    """The values `kubernetes/apply.sh --tenant eng` actually renders with.

    BUILT BY THE RENDERER, NOT BY THIS FILE. The dict used to be written out
    here, key by key, which made the suite a second definition of what a tenant
    namespace is -- and it had already drifted once: `NAMESPACE` read
    `f"swarm-{TENANT}"` while the scheduler dispatched into `swarm-tenant-eng`,
    so the tests asserted against the renderer's own mistake and agreed with
    it. A hand-written fixture also cannot notice a placeholder ADDED to a
    manifest: `render.substitute` would refuse the render, but only if some
    test rendered that file, and only the ones listed here ever were.

    Parsing the real arguments instead means every default in
    `add_tenant_arguments` is exercised, and a new placeholder is supplied (or
    fails loudly) exactly as it would be in production.
    """
    parser = argparse.ArgumentParser()
    render.add_tenant_arguments(parser)
    args = parser.parse_args(["--tenant", TENANT, "--project", PROJECT, *argv])
    return render.tenant_values(args)


@pytest.fixture(scope="module")
def tenant_docs() -> list[dict[str, Any]]:
    return documents(render.render_files(render.TENANT_FILES, tenant_values()))


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


def test_the_legacy_bound_service_account_still_exists(tenant_docs):
    """`swarm-worker` is the name every tenant registered so far is
    workload-identity-bound to, by `scripts/register-tenant.sh`. Removing it is
    a migration of its own, so it stays.

    This test used to assert `sanitize_name("swarm", tenant)` -- the spelling
    the dispatcher asked for BEFORE its KSA was renamed -- and its docstring
    still claimed that was what `dispatch.py` set. It was therefore asserting
    the presence of a name nothing wanted while the name the dispatcher
    actually names was absent. That check is now derived from the dispatcher
    itself, in
    `test_the_service_account_the_renderer_creates_is_the_one_the_dispatcher_names`.
    """
    names = {a["metadata"]["name"] for a in by_kind(tenant_docs, "ServiceAccount")}
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
    """Every ServiceAccount in the namespace that a pod could run as, DERIVED
    from what was rendered rather than listed again here.

    The empty `swarm-worker` Role is what makes a worker pod hold no Kubernetes
    API access at all. An account created but left out of the binding is not a
    loophole -- it is bound to nothing, so it has nothing -- but it is a
    divergence between the two files, and the list of names here had already
    gone stale once against `__KSA_NAME__`.
    """
    binding = one(tenant_docs, "RoleBinding", "swarm-worker")
    subjects = {s["name"] for s in binding["subjects"]}
    created = {
        a["metadata"]["name"]
        for a in by_kind(tenant_docs, "ServiceAccount")
        # `default` is deliberately not bound: no pod should run as it, and
        # binding it would grant whatever it is that nothing may have.
        if a["metadata"]["name"] != "default"
    }
    assert subjects == created, (
        f"rbac/worker-rbac.yaml binds {sorted(subjects)} but "
        f"service-accounts/worker-serviceaccount.yaml creates {sorted(created)}"
    )


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
    grants nothing, so a typo here fails looking exactly like success.

    THIS ASSERTION WAS WRITTEN FOR EXACTLY THE DEFECT THAT LATER HAPPENED, AND
    ITS LITERAL FORM BLOCKED THE FIX. It required the subject list to be
    `== [expected]` -- exactly one entry. On 2026-09-24 every GKE dispatch was
    found to be failing because the binding named the scheduler by EMAIL while
    GKE, for a service account authenticating with an OAuth access token,
    presents the caller as its numeric uniqueId. The fix binds both names, so the
    list is two entries, and this test went red on the change it had been written
    to demand.

    Re-pointed at the property it was always about: no subject that is not a
    known control-plane identity, and nothing but `User`. The COUNT was never the
    thing -- a second subject naming the same account cannot grant anything the
    first did not, because RBAC subjects are a list.

    ONE FACT PER TEST. How MANY subjects there are, and why, belongs to
    `test_an_unsupplied_unique_id_renders_a_duplicate_and_never_a_placeholder`,
    which pins the fallback. Keeping a count here as well would be a third copy
    of the same spec -- and the two tests stating it two ways is precisely why
    this file could not pass itself for one commit. (Reasoning from the
    mirrored-copy-audit lane, which reached this assertion independently.)

    AND THE SLOT COUNT IS ASSERTED ANYWAY, which is the one thing neither of the
    two lanes that re-pointed this independently had seen: with no uniqueId
    supplied, BOTH slots fall back to the email, so the SET of names is
    `{email}` whether there are two slots or one. A set-based assertion cannot
    see a DROPPED slot -- the fallback makes it look like a missing duplicate.
    Only the count can. (From the gke-incident-record lane.)
    """
    for binding_name, account in (
        ("swarm-dispatcher", f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com"),
        ("swarm-reaper", f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com"),
    ):
        binding = one(tenant_docs, "RoleBinding", binding_name)
        subjects = binding["subjects"]
        # BINDS SOMEBODY AT ALL, from the audit lane: an empty subject list is
        # a RoleBinding that validates, applies, and authorises nobody -- the
        # same failure as a wrong subject, reached by having none.
        assert subjects, f"{binding_name} binds nobody at all"
        names = [s["name"] for s in subjects]

        assert account in names, (
            f"{binding_name} does not name {account}; a binding to an identity "
            f"that does not exist applies cleanly and authorises nobody"
        )

        # THE SET IS CLOSED. Every subject must be either the account's email or
        # its numeric uniqueId -- nothing else may appear in a binding that
        # decides who can create a Job in a tenant's namespace. This is the half
        # of the original `== [expected]` that mattered, kept.
        uid = SCHEDULER_UID if binding_name == "swarm-dispatcher" else RECONCILER_UID
        unexpected = [n for n in names if n not in {account, uid}]
        assert not unexpected, (
            f"{binding_name} names {unexpected}, which is neither {account} nor "
            f"its uniqueId. Nothing else belongs in this binding."
        )

        # And no placeholder survived substitution -- a literal `__X__` subject
        # applies cleanly and grants nothing, the same failure by another route.
        assert not [n for n in names if "__" in n], (
            f"{binding_name} has an unsubstituted placeholder subject: {names}"
        )
        # THE SLOT COUNT, because the set of names cannot carry it. `tenant_docs`
        # supplies no uniqueIds, so both slots hold the email and a deleted slot
        # is invisible to any assertion about names. See the docstring.
        assert len(subjects) == 2, (
            f"RoleBinding {binding_name} has {len(subjects)} subject(s); it must have "
            f"one slot per spelling of its account -- the email and the numeric "
            f"uniqueId -- because GKE names the caller differently depending on how "
            f"it authenticated. See kubernetes/rbac/dispatcher-rbac.yaml."
        )
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


def test_every_gsa_rbac_subject_is_also_bound_by_numeric_unique_id():
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
    # RENDERED WITH THE uniqueIds SUPPLIED, which is what apply.sh does. The
    # module fixture renders with the DEFAULTS, and the default is the email --
    # so asserting this against that fixture would pass on a duplicate email
    # subject and prove nothing about the numeric form. Rendering explicitly here
    # is the difference between checking the fallback and checking the fix.
    tenant_docs = documents(
        render.render_files(
            render.TENANT_FILES,
            tenant_values(
                "--scheduler-uid", SCHEDULER_UID,
                "--reconciler-uid", RECONCILER_UID,
            ),
        )
    )

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

# ---------------------------------------------------------------------------
# The dispatcher RBAC, and the substitutions that make it real
# ---------------------------------------------------------------------------
#
# Everything below is about the SEAM rather than the objects: the objects are
# checked above. A RoleBinding whose subject is the literal string
# `__SCHEDULER_GSA__` applies cleanly, reports success, and grants nothing --
# so "the manifest is correct" and "the manifest that reached the cluster is
# correct" are different claims, and only the second one matters.


def test_every_placeholder_in_the_tenant_manifests_is_supplied_and_validated():
    """A placeholder with no value is a literal in a live object.

    THE MUTATION THIS CATCHES: delete `SCHEDULER_GSA` from `tenant_values()`
    (or from `_VALUE_PATTERNS`) and this fails, naming the placeholder and the
    file. Without it the first symptom is a scheduler that is still 403 after
    an apply that reported success -- indistinguishable from the RBAC never
    having been applied at all.
    """
    supplied = set(tenant_values())
    placeholders: dict[str, set[str]] = {}
    for name in render.TENANT_FILES:
        text = (KUBERNETES / name).read_text()
        for token in re.findall(r"__([A-Z0-9_]+)__", text):
            placeholders.setdefault(token, set()).add(name)

    # The file this is really about must actually be in the set, or this test
    # passes by checking nothing -- the failure mode section 4 of
    # check-contract-parity.sh calls out by name.
    assert "rbac/dispatcher-rbac.yaml" in render.TENANT_FILES
    for token in ("NAMESPACE", "TENANT_ID", "SCHEDULER_GSA", "RECONCILER_GSA"):
        assert token in placeholders, (
            f"__{token}__ is no longer in any tenant manifest; if the RBAC moved, "
            "point this test at it rather than letting it pass on an empty set"
        )

    missing = {t: sorted(f) for t, f in placeholders.items() if t not in supplied}
    assert not missing, f"placeholders with no value from tenant_values(): {missing}"

    unvalidated = sorted(t for t in placeholders if t not in render._VALUE_PATTERNS)
    assert not unvalidated, (
        f"placeholders interpolated with no pattern in _VALUE_PATTERNS: {unvalidated}. "
        "substitute() is a str.replace over YAML, so an unvalidated value is YAML "
        "injection into the objects that isolate tenants from one another."
    )


def test_a_missing_substitution_is_refused_rather_than_rendered(tenant_docs):
    """The renderer must fail, not emit `name: __SCHEDULER_GSA__`.

    A binding to a placeholder is a binding that silently grants nothing: the
    subject is a syntactically valid Kubernetes user name, so the RoleBinding
    applies, `kubectl get` shows it, and every dispatch keeps failing 403 --
    the failure that is already impossible to tell from a missing namespace.
    """
    values = tenant_values()
    values.pop("SCHEDULER_GSA")
    with pytest.raises(SystemExit) as raised:
        render.render_files(render.TENANT_FILES, values)
    assert "SCHEDULER_GSA" in str(raised.value)

    # And the happy path really does substitute it, so the assertion above is
    # not passing because the token was never in the file.
    binding = one(tenant_docs, "RoleBinding", "swarm-dispatcher")
    assert binding["subjects"][0]["name"].startswith("swarm-scheduler@")
    assert "__" not in binding["subjects"][0]["name"]


def test_an_unvalidated_placeholder_is_refused_before_it_reaches_the_yaml():
    """`check_values` refuses a key with no pattern rather than interpolating it."""
    values = tenant_values()
    values["BRAND_NEW_TOKEN"] = "anything"
    with pytest.raises(SystemExit) as raised:
        render.check_values(values)
    assert "BRAND_NEW_TOKEN" in str(raised.value)


def test_a_namespace_outside_the_platform_prefix_is_refused():
    """`--namespace swarm-eng` is one word away from re-creating the outage.

    `kubernetes/apply.sh` forwards unrecognised arguments straight to the
    renderer, so this flag can isolate a namespace the scheduler never writes
    to -- and the resulting dispatch failure is a 403 naming a permission, not
    a 404 naming the namespace.

    THE MUTATION THIS CATCHES: drop the prefix check in `tenant_values()` and
    the old spelling renders cleanly again.
    """
    with pytest.raises(SystemExit) as raised:
        tenant_values("--namespace", f"swarm-{TENANT}")
    assert render.NAMESPACE_PREFIX in str(raised.value)

    # A different namespace INSIDE the prefix is still allowed: the flag exists
    # for a namespace neither provisioning path derived, and closing it
    # entirely would be a different change from closing the outage.
    inside = tenant_values("--namespace", f"{render.NAMESPACE_PREFIX}{TENANT}-canary")
    assert inside["NAMESPACE"] == f"{render.NAMESPACE_PREFIX}{TENANT}-canary"


def test_the_service_account_the_renderer_creates_is_the_one_the_dispatcher_names(
    tenant_docs,
):
    """THE BUG THIS PINS, and it is the failure that comes AFTER the RBAC.

    Nothing in `terraform/` creates a Kubernetes object -- there is no
    kubernetes provider in this repository -- so the ServiceAccounts rendered
    here are the only ones a tenant namespace has. `dispatch.py` puts
    `serviceAccountName: swarm-agent-worker` in every GKE pod spec and
    `terraform/modules/tenancy` issues the Workload Identity binding for that
    same name, while this renderer created `swarm-worker` and `swarm-<tenant>`.

    A pod naming a ServiceAccount that does not exist is admitted and then
    never scheduled: the Job controller reports `serviceaccount ... not found`
    on the Job events and no pod ever appears, which reads as a scheduling
    problem rather than a missing object.

    THE MUTATION THIS CATCHES: put `DEFAULT_KSA_NAME` back to
    `sanitize_name("swarm", tenant)` and this fails.
    """
    from scheduler.dispatch import GkeJobDispatcher  # noqa: PLC0415

    class _Settings:
        worker_ksa_name = ""

    wanted = GkeJobDispatcher(_Settings()).ksa_for(
        Tenant(
            tenant_id=TENANT,
            kind="group",
            principal="eng@saga.xyz",
            created_at=utcnow(),
        )
    )
    created = {d["metadata"]["name"] for d in by_kind(tenant_docs, "ServiceAccount")}
    assert wanted in created, (
        f"the dispatcher runs its pods as {wanted!r} and this namespace only has "
        f"{sorted(created)}; the Job is created and the pod is never scheduled"
    )

    # And it carries the Workload Identity annotation, or the pod runs with no
    # Google identity at all and cannot read its tenant secret, its GCS prefix
    # or Firestore.
    account = one(tenant_docs, "ServiceAccount", wanted)
    annotation = account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"]
    assert annotation.startswith(f"swarm-agent-worker-{TENANT}@")
    assert account["automountServiceAccountToken"] is False


def test_an_unsupplied_unique_id_renders_a_duplicate_and_never_a_placeholder():
    """The fallback, asserted as the property that makes it safe.

    `--scheduler-uid` cannot be derived from the project id -- IAM assigns it --
    so render.py has to accept it as a flag and has to do SOMETHING when it is
    absent. Three options existed and two of them are traps:

      a fabricated number   a live RoleBinding subject naming an identity that
                            does not exist. Applies cleanly, reads as a grant,
                            authorises nobody. This is the exact failure the
                            email-only binding already caused.
      the raw placeholder   `__SCHEDULER_UID__` as a subject name. Same outcome,
                            and it is what `test_every_placeholder...` below
                            exists to stop.
      the email             a duplicate of the subject already present. RBAC
                            subjects are a list; a repeat authorises nothing new.

    The third is chosen, and this is what holds it there.

    MUTATION: change the fallback in `tenant_values` to a literal number, or
    remove it so the placeholder survives. Either way this names the subject.
    """
    docs = documents(render.render_files(render.TENANT_FILES, tenant_values()))
    bindings = by_kind(docs, "RoleBinding")
    assert bindings, "the tenant render produced no RoleBinding"

    seen = 0
    for binding in bindings:
        for subject in binding.get("subjects", []):
            name = str(subject.get("name", ""))
            assert "__" not in name, (
                f"RoleBinding {binding['metadata']['name']} has an unsubstituted "
                f"placeholder as a subject: {name!r}. It applies cleanly and grants "
                f"nothing."
            )
            # Anything numeric here would be a fabricated identity: nothing
            # supplied a uniqueId in this render.
            assert not name.isdigit(), (
                f"RoleBinding {binding['metadata']['name']} names a numeric subject "
                f"{name!r} although no uniqueId was supplied. A made-up uniqueId is "
                f"a subject that does not exist."
            )
            if name.endswith(".iam.gserviceaccount.com"):
                seen += 1

    assert seen >= 4, (
        f"expected at least 4 service-account subjects across the control-plane "
        f"bindings (two accounts, each bound twice by the fallback), saw {seen}. "
        f"Fewer means the second subject is not being rendered at all."
    )


def test_the_yaml_templates_and_the_scheduler_agree_on_the_container_environment():
    """TWO IMPLEMENTATIONS OF ONE JOB SPEC, and they had stopped agreeing.

    `apps/scheduler/scheduler/dispatch.py` builds the GKE Job manifest as a
    Python dict and says so in its own comment: it "mirrors
    kubernetes/worker-templates/worker-job-browser.yaml field for field". The
    YAML is what `render.py` renders, what `make lint` validates and what an
    operator reads during an incident. The Python is what actually dispatches.

    They disagreed on the one variable that decided whether a browser task could
    run at all. `SWARM_ARTIFACTS_DIR` was in neither, the runner fell back to
    `/artifacts`, and `readOnlyRootFilesystem: true` turned that into

        OSError: [Errno 30] Read-only file system: '/artifacts'

    Nothing could have caught it, because nothing compared the two. A template
    that is documented as mirroring another file, with no assertion holding it
    there, is a copy waiting to drift -- this repository's most expensive
    recurring defect, and this is the third instance of it found in two days
    (the namespace prefix and the RBAC subject were the others).

    ASSERTED AS THE ENV KEY SET, not the values: the values are per-task
    (TASK_ID, GENERATION) and the templates carry placeholders for them. What
    must hold is that neither side names a variable the other does not, because
    that is exactly the shape of the defect.

    SCOPED TO THE `worker` CONTAINER, which this test did NOT do when it was
    written, and that gap was already a live defect. `worker-job-v2.yaml` has an
    init container (`install-credential`) as well, and the commit that fixed the
    Errno 30 added `SWARM_ARTIFACTS_DIR` to the INIT container -- which writes
    one credential file and never touches an artifacts directory -- while the
    `worker` container, the one that runs the agent and creates the directory,
    had none. Reading the template as one document sees the name present and
    passes. "Set" and "set on the wrong container" are different facts, and on
    v2 the second one is not even loud: v2 leaves `readOnlyRootFilesystem`
    false on purpose, so `mkdir /artifacts` succeeds, the artifact tree lands on
    the container's writable layer instead of in the `workspace` emptyDir that
    carries `sizeLimit: __DISK__`, and the first large artifact set evicts the
    pod for ephemeral-storage pressure mid-attempt.

    MUTATION: delete `SWARM_ARTIFACTS_DIR` from `worker_env` in dispatch.py, or
    from any one of the three templates. This names the variable and the side it
    is missing from. MUTATION FOR THE SCOPING: move `SWARM_ARTIFACTS_DIR` in
    `worker-job-v2.yaml` from the `worker` container up into the
    `install-credential` init container -- which is where it actually was --
    and this fails naming v2. Before the scoping it passed.
    """
    import re as _re

    scheduler = (
        Path(__file__).resolve().parents[3]
        / "apps/scheduler/scheduler/dispatch.py"
    ).read_text()

    # The shared builder, sliced out so a mention elsewhere in the file cannot
    # satisfy this.
    start = scheduler.index("    env = {\n")
    end = scheduler.index("    return env", start)
    body = scheduler[start:end]
    from_python = set(_re.findall(r'^\s*"([A-Z][A-Z0-9_]*)":', body, _re.M))
    assert from_python, "could not read any env key out of dispatch.py's worker_env"

    # Every GKE template's container env, read the same way.
    templates = sorted((KUBERNETES / "worker-templates").glob("*.yaml"))
    assert templates, "no worker templates found; this test would check nothing"

    for template in templates:
        whole = template.read_text()
        # The `worker` container only, from its `- name: worker` entry to the
        # pod's `volumes:` key. An init container's environment is not the
        # agent's, and a variable that decides where the agent writes is only
        # set if it is set on the container that runs it.
        start = whole.find("\n        - name: worker\n")
        assert start >= 0, f"{template.name}: no container named worker"
        end = whole.find("\n      volumes:", start)
        text = whole[start:] if end < 0 else whole[start:end]
        names = set(_re.findall(r"^\s*- name: ([A-Z][A-Z0-9_]*)\s*$", text, _re.M))
        assert names, f"{template.name}: no container env names found"

        # A template may carry variables the scheduler does not set -- ones the
        # image needs and the scheduler has no opinion about, like
        # PLAYWRIGHT_BROWSERS_PATH. What it may NOT do is omit one the scheduler
        # relies on the container having.
        missing = {
            key
            for key in from_python
            if key in _TEMPLATE_RELEVANT and key not in names
        }
        assert not missing, (
            f"{template.name} does not set {sorted(missing)}, which "
            f"dispatch.py's worker_env does. The YAML is what `make lint` "
            f"validates and what an operator reads; the Python is what "
            f"dispatches. A variable in one and not the other is how "
            f"SWARM_ARTIFACTS_DIR went missing from both and stopped every "
            f"browser task from starting."
        )


#: The env keys whose ABSENCE from a template is a defect rather than a
#: difference. Deliberately narrow: most of `worker_env` is per-task identity
#: (TASK_ID, GENERATION, LEASE_ID) which the templates carry as `__TOKEN__`
#: placeholders under the same names, and asserting the whole set would make this
#: test fail on any placeholder rename rather than on a real divergence. These
#: are the ones that change where the runner WRITES or what it talks to.
_TEMPLATE_RELEVANT = frozenset({"SWARM_ARTIFACTS_DIR", "ARTIFACT_BUCKET"})


# ---------------------------------------------------------------------------
# The YAML and the dispatcher are ONE Job spec, asserted as one
# ---------------------------------------------------------------------------
#
# Incident wf_ebb3ab2d65664707a559, 2026-09-24. `worker-job.yaml` said in its
# header that `command` is ABSENT and why, and
# `test_a_rendered_job_never_overrides_the_container_command` above asserted
# it -- of the RENDERED YAML. Nothing dispatches the rendered YAML. The Job that
# actually reached the cluster is built in code by `GkeJobDispatcher._manifest`,
# which carried `command: list(profile.command)`, so every GKE pod ran the bare
# runner instead of the worker lifecycle: no fencing, no cancel, no heartbeat,
# no lease release. Five leases held every browser slot until an operator
# intervened.
#
# The two copies were documented as mirroring each other ("field for field",
# dispatch.py's POD_SECURITY_CONTEXT comment) and nothing held them there --
# the same shape as the namespace prefix, the RBAC subject and
# SWARM_ARTIFACTS_DIR before this. So instead of asserting the same property
# twice, once per copy, this renders both from the same inputs and asserts
# they agree on everything that changes what the pod DOES. A field added to one
# and not the other now fails here, naming the field.

_EVICT = "cluster-autoscaler.kubernetes.io/safe-to-evict"

#: Pod labels something selects on: the reconciler's managed-family selector
#: and its tenant/task lookup. Decorative labels (app.kubernetes.io/*) are not
#: compared; they differ and nothing reads them.
_SELECTED_POD_LABELS = ("managed-by", "swarm-tenant", "swarm-task")

#: The same identifiers on both sides, so per-task fields (the Job name, the
#: task label) are comparable.
_IDS = {"task_id": "task_9f3a", "attempt": "att_7b21", "lease_id": "lease_c4", "generation": 3}


def _gke_profiles() -> list[str]:
    from swarm_common.profiles import Backend, resolve_backend  # noqa: PLC0415

    return sorted(
        name
        for name, profile in RUNNER_PROFILES.items()
        if resolve_backend(profile) is Backend.GKE_AUTOPILOT
    )


def _rendered_job(profile_name: str) -> dict[str, Any]:
    return render_job(
        profile_name,
        task=_IDS["task_id"],
        attempt=_IDS["attempt"],
        lease=_IDS["lease_id"],
        generation=_IDS["generation"],
    )


def _dispatcher_job(profile_name: str) -> dict[str, Any]:
    """What `GkeJobDispatcher._manifest` sends, for the inputs `render_job` gets."""
    from datetime import timedelta  # noqa: PLC0415

    from scheduler.dispatch import GkeJobDispatcher, GkeTarget  # noqa: PLC0415
    from scheduler.settings import SchedulerSettings  # noqa: PLC0415
    from swarm_common.config import Settings  # noqa: PLC0415
    from swarm_common.models import Lease, Task  # noqa: PLC0415
    from swarm_common.states import TaskState  # noqa: PLC0415

    profile = RUNNER_PROFILES[profile_name]
    now = utcnow()
    settings = SchedulerSettings(
        core=Settings(project_id=PROJECT, artifact_bucket=f"{PROJECT}-swarm-artifacts"),
        project_id=PROJECT,
        region="us-central1",
        artifact_registry_host=f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images",
        worker_image_tag="latest",
    )
    tenant = Tenant(
        tenant_id=TENANT,
        kind="group",
        principal="eng@saga.xyz",
        created_at=now,
        service_account=f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
        namespace=f"swarm-tenant-{TENANT}",
    )
    task = Task(
        id=_IDS["task_id"],
        tenant_id=TENANT,
        created_at=now,
        updated_at=now,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        # render.py's default: `--timeout` unset means the profile's own.
        timeout_seconds=profile.timeout_seconds,
    )
    lease = Lease(
        lease_id=_IDS["lease_id"],
        task_id=_IDS["task_id"],
        attempt_id=_IDS["attempt"],
        tenant_id=TENANT,
        generation=_IDS["generation"],
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=now,
        dispatch_deadline=now + timedelta(minutes=5),
        expires_at=now + timedelta(minutes=2),
    )
    dispatcher = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=object()
    )
    return dispatcher._manifest(task=task, lease=lease, profile=profile, tenant=tenant)


def _behaviour(job: dict[str, Any]) -> dict[str, Any]:
    """Every field of a worker Job that changes what the pod does.

    Deliberately NOT compared: the container environment (per-task values, and
    `test_the_yaml_templates_and_the_scheduler_agree_on_the_container_environment`
    owns the names that matter), `imagePullPolicy` (the YAML's IfNotPresent is
    the API server's default for a non-`latest` tag), the image TAG (a deploy
    input, not a property of the spec), and decorative labels and annotations.
    """
    import json  # noqa: PLC0415

    metadata = job.get("metadata") or {}
    spec = job.get("spec") or {}
    template = spec.get("template") or {}
    template_meta = template.get("metadata") or {}
    pod = template.get("spec") or {}
    containers = pod.get("containers") or []
    worker = next((c for c in containers if c.get("name") == "worker"), {})
    image = str(worker.get("image", ""))
    return {
        "Job name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        f"Job annotation {_EVICT}": (metadata.get("annotations") or {}).get(_EVICT),
        # The one the cluster autoscaler actually reads. A Job's own annotations
        # are not copied onto its pods, so the Job-level one alone protects
        # nothing (GKE Autopilot extended run time is requested on the POD).
        f"pod annotation {_EVICT}": (template_meta.get("annotations") or {}).get(_EVICT),
        "pod selector labels": {
            key: (template_meta.get("labels") or {}).get(key) for key in _SELECTED_POD_LABELS
        },
        "backoffLimit": spec.get("backoffLimit"),
        "completions": spec.get("completions"),
        "parallelism": spec.get("parallelism"),
        "activeDeadlineSeconds": spec.get("activeDeadlineSeconds"),
        "ttlSecondsAfterFinished": spec.get("ttlSecondsAfterFinished"),
        "restartPolicy": pod.get("restartPolicy"),
        "serviceAccountName": pod.get("serviceAccountName"),
        "automountServiceAccountToken": pod.get("automountServiceAccountToken"),
        "terminationGracePeriodSeconds": pod.get("terminationGracePeriodSeconds"),
        "pod securityContext": pod.get("securityContext"),
        "tolerations": pod.get("tolerations"),
        "nodeSelector": pod.get("nodeSelector"),
        "initContainers": pod.get("initContainers"),
        "container names": [c.get("name") for c in containers],
        "worker command": worker.get("command"),
        "worker args": worker.get("args"),
        "worker image repository": image.rsplit("/", 1)[-1].split("@")[0].split(":")[0],
        "worker resources": worker.get("resources"),
        "worker securityContext": worker.get("securityContext"),
        "worker volumeMounts": sorted(
            (m.get("name"), m.get("mountPath")) for m in worker.get("volumeMounts") or []
        ),
        "volumes": sorted(json.dumps(v, sort_keys=True) for v in pod.get("volumes") or []),
    }


@pytest.mark.parametrize("profile", _gke_profiles())
def test_the_rendered_job_and_the_dispatched_job_are_the_same_job(profile):
    """ONE spec, two producers: they must agree on everything the pod does.

    Red on 2026-09-24 for two fields, both on the dispatcher's side:

      * `worker command` -- the override that replaced the lifecycle;
      * `pod annotation safe-to-evict` -- the dispatcher put
        `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` on the JOB
        only. The autoscaler reads it from the POD, and a Job's annotations are
        not propagated to the pods it creates, so every dispatched browser pod
        was evictable in a scale-down while the code's own comment said it was
        not. The YAML had it on both.

    MUTATIONS: re-add `"command"` to `_manifest`'s container; drop the pod
    template's `annotations`; change any securityContext field, volume, mount,
    grace period or retry setting on one side only. Each fails here naming the
    field.
    """
    from_yaml = _behaviour(_rendered_job(profile))
    from_code = _behaviour(_dispatcher_job(profile))
    differing = {
        key: {"yaml": from_yaml[key], "dispatcher": from_code[key]}
        for key in from_yaml
        if from_yaml[key] != from_code[key]
    }
    assert not differing, (
        f"the {profile!r} Job that render.py writes and the one "
        f"GkeJobDispatcher._manifest dispatches disagree on {sorted(differing)}: "
        f"{differing}. The YAML is what an operator reads and `make lint` checks; "
        f"the dispatcher's is what runs. See incident wf_ebb3ab2d65664707a559."
    )


def test_there_is_a_gke_profile_to_compare():
    """The parity test above is parametrised over the GKE profiles; if the
    catalogue ever had none, it would silently check nothing."""
    assert _gke_profiles(), "no runner profile resolves to GKE_AUTOPILOT"


@pytest.mark.parametrize("profile", sorted(RUNNER_PROFILES))
def test_neither_copy_of_the_job_overrides_the_image_entrypoint(profile):
    """For EVERY profile, both producers, because a profile on Cloud Run today is
    one catalogue edit from GKE.

    The image ENTRYPOINT is `tini -- python -m agent_worker`, the worker
    lifecycle. `RunnerProfile.command` is the lifecycle's CHILD argv; as a
    container `command` it replaces the lifecycle with the bare runner.
    """
    for source, job in (
        ("render.py", _rendered_job(profile)),
        ("GkeJobDispatcher._manifest", _dispatcher_job(profile)),
    ):
        for container in job["spec"]["template"]["spec"]["containers"]:
            assert "command" not in container, (
                f"{source} sets command={container['command']!r} on {container['name']!r} "
                f"for {profile!r}, replacing the worker lifecycle"
            )
            assert "args" not in container, f"{source} sets args on {container['name']!r}"


# ---------------------------------------------------------------------------
# The Job's deadline is a backstop, and it must fire AFTER the lifecycle's own
# ---------------------------------------------------------------------------
#
# Review of PR #31, 2026-09-24. Every template set `activeDeadlineSeconds` and
# the worker's TASK_TIMEOUT_SECONDS from the same `__TIMEOUT_SECONDS__`. The
# Job's deadline counts from Job creation -- node provisioning and the image
# pull included -- and the lifecycle's from its own start, minutes later, so the
# Job controller always SIGTERMed the pod first. The lifecycle's SIGTERM path
# parks the task as SCHEDULED_RETRY, which nothing promotes; its timeout path,
# which would have FAILED the task, never ran. The dispatcher's copy is held to
# the same property in tests/unit/control_plane/test_dispatch_manifests.py, and
# the parity test above holds the two copies equal.


def _rendered_jobs_for_the_deadline() -> list[tuple[str, dict[str, Any]]]:
    jobs = [(f"{name} (render.py)", render_job(name)) for name in sorted(RUNNER_PROFILES)]
    jobs.append(("claude-code (render.py --runtime gvisor)", _render_v2_job()))
    return jobs


def test_every_rendered_deadline_lets_the_lifecycle_time_out_on_its_own():
    """MUTATION: point `activeDeadlineSeconds` back at `__TIMEOUT_SECONDS__` in
    any one of the three templates."""
    import dataclasses  # noqa: PLC0415

    from agent_worker.config import WorkerConfig  # noqa: PLC0415
    from swarm_common.config import Settings  # noqa: PLC0415

    # The worker's own defaults, read from the dataclass rather than restated:
    # the part of its timeout path that runs AFTER its deadline.
    defaults = {f.name: f.default for f in dataclasses.fields(WorkerConfig)}
    after_deadline = (
        defaults["termination_grace_seconds"] + defaults["git_harvest_timeout_seconds"]
    )
    # The latest a lifecycle can start and still be running: the reconciler
    # reclaims any attempt with no heartbeat by the dispatch deadline.
    latest_start = Settings(project_id=PROJECT).dispatch_timeout_seconds

    checked = 0
    for where, job in _rendered_jobs_for_the_deadline():
        pod = job["spec"]["template"]["spec"]
        worker = next(c for c in pod["containers"] if c["name"] == "worker")
        env = {e["name"]: e.get("value") for e in worker.get("env") or []}
        task_timeout = int(env["TASK_TIMEOUT_SECONDS"])
        deadline = int(job["spec"]["activeDeadlineSeconds"])
        needed = task_timeout + latest_start + after_deadline
        assert deadline > task_timeout + int(pod["terminationGracePeriodSeconds"]), where
        assert deadline > needed, (
            f"{where}: activeDeadlineSeconds is {deadline}s, and the lifecycle's own "
            f"timeout path needs more than {needed}s from Job creation "
            f"({task_timeout}s + {latest_start}s latest start + {after_deadline}s to "
            "stop the child and harvest). The Job controller would SIGTERM it first "
            "and the task would PARK instead of failing."
        )
        checked += 1
    # Every profile plus the gvisor shape: all three templates were visited.
    assert checked == len(RUNNER_PROFILES) + 1


# ---------------------------------------------------------------------------
# A variable the browser image sets is set there and nowhere else
# ---------------------------------------------------------------------------
#
# Lane review, 2026-09-24. `worker-job-browser.yaml` carried its own
# `PLAYWRIGHT_BROWSERS_PATH` although `images/agent-runtime-browser/Dockerfile`
# already sets it with `ENV`, in the same file whose `RUN playwright install`
# puts the browser there. Two copies of one value is the shape
# docs/mirrored-values.md records as this repository's most expensive defect:
# the namespace prefix, the RBAC subject and SWARM_ARTIFACTS_DIR each drifted
# that way. This copy had in fact ALREADY diverged from the Job that runs --
# `GkeJobDispatcher._manifest` never set it, so the YAML an operator reads said
# something the dispatched pod did not.
#
# The image is the right single home: the path is only correct when stated next
# to the command that installs into it. Nothing a Job spec can say makes it more
# true, and a Job spec that says a different path makes it false. The runner
# child gets it because the lifecycle carries PLAYWRIGHT_BROWSERS_PATH by NAME
# from its own environment (#37, test_image_env_reaches_the_runner.py), and its
# own environment has it from the image -- no Job copy is involved.
#
# SCOPE: the browser Dockerfile's OWN `runtime` stage. The base image's ENV is
# not held to this here, and one of its variables would fail it today:
# WORKSPACE_ROOT, which agent-runtime-base sets and every template restates
# beside the volume mount it has to match. That one is an open question, not a
# settled exception.


def _browser_image_own_env() -> tuple[Path, dict[str, str]]:
    """The ENV `agent-runtime-browser`'s own stage sets, parsed ONCE in this suite.

    Borrowed from test_image_env_reaches_the_runner.py rather than written again:
    a second Dockerfile parser in a test about second copies would be one more
    thing to drift, and that one already skips heredoc bodies (the launch check
    is a `RUN python - <<'PY'` whose body starts `from playwright`, which a
    case-insensitive reader would otherwise take for a new FROM stage).
    """
    from test_image_env_reaches_the_runner import (  # noqa: PLC0415
        BROWSER_DOCKERFILE,
        _stage_env,
    )

    return BROWSER_DOCKERFILE, _stage_env(BROWSER_DOCKERFILE, "runtime")


def _every_worker_job() -> list[tuple[str, dict[str, Any]]]:
    """Every worker Job either producer writes, for every profile.

    Every profile rather than the GKE ones: a profile on Cloud Run today is one
    catalogue edit from GKE, and render.py renders a Job for all of them.
    """
    jobs = [(f"{name} (render.py)", _rendered_job(name)) for name in sorted(RUNNER_PROFILES)]
    jobs.append(("claude-code (render.py --runtime gvisor)", _render_v2_job()))
    jobs += [
        (f"{name} (GkeJobDispatcher._manifest)", _dispatcher_job(name))
        for name in sorted(RUNNER_PROFILES)
    ]
    return jobs


def test_the_browser_image_sets_where_its_browser_lives():
    """The anchor for the two tests below: if the image stopped setting it, they
    would pass by checking for a variable nothing defines."""
    dockerfile, env = _browser_image_own_env()
    assert env.get("PLAYWRIGHT_BROWSERS_PATH"), (
        f"{dockerfile.relative_to(REPO)} no longer sets "
        f"PLAYWRIGHT_BROWSERS_PATH with ENV (read: {sorted(env)}). It is the one "
        "place the browser bundle's path is stated, next to the `playwright "
        "install` that puts it there."
    )


def test_no_worker_job_restates_a_variable_the_browser_image_sets():
    """MUTATION: add `PLAYWRIGHT_BROWSERS_PATH` (or `PLAYWRIGHT_SKIP_BROWSER_GC`)
    to the `env:` of any container in any of the three templates, or to
    `worker_env` in dispatch.py. This fails naming the Job and the container."""
    dockerfile, image_env = _browser_image_own_env()
    assert image_env, f"read no ENV at all from {dockerfile}"

    jobs = _every_worker_job()
    restated: dict[str, list[str]] = {}
    containers_seen = 0
    for where, job in jobs:
        pod = job["spec"]["template"]["spec"]
        for container in [*(pod.get("initContainers") or []), *pod["containers"]]:
            containers_seen += 1
            names = {entry["name"] for entry in container.get("env") or []}
            clash = sorted(names & image_env.keys())
            if clash:
                restated[f"{where}, container {container['name']!r}"] = clash
    # Two producers for every profile, plus the gvisor shape.
    assert len(jobs) == 2 * len(RUNNER_PROFILES) + 1
    assert containers_seen >= len(jobs)
    assert not restated, (
        f"these Jobs set variables images/agent-runtime-browser/Dockerfile "
        f"already sets with ENV: {restated}. The image is the only place they "
        "are stated -- the path is where its own `playwright install` put the "
        "browser -- and a second copy is a value that can disagree with the "
        "first (docs/mirrored-values.md). The runner child gets it because the "
        "lifecycle carries it by name from the environment the image gave it."
    )


def test_the_browser_bundle_path_is_written_down_once():
    """The VALUE, not just the name: a Job that stopped naming the variable but a
    lifecycle that hard-coded the path into the child's environment would be the
    same second copy under a different spelling.

    Read from the Dockerfile rather than restated here, so this file is not a
    third copy. COMMENT LINES ARE NOT COUNTED: a comment that names the path
    (lifecycle.py's explanation of why the variable is carried does) can go
    stale, but it cannot send Playwright anywhere; a value in code or config
    can. MUTATION: write the path as a value into any Python module under apps/,
    any file under kubernetes/ or any Terraform file.
    """
    value = _browser_image_own_env()[1]["PLAYWRIGHT_BROWSERS_PATH"]
    skip = {"node_modules", ".terraform", "__pycache__", ".venv", "dist"}
    scanned = 0
    hits: list[str] = []
    for root, patterns in (
        (REPO / "apps", ("*.py",)),
        (KUBERNETES, ("*.yaml", "*.yml", "*.py", "*.sh", "*.rego")),
        (REPO / "terraform", ("*.tf", "*.tfvars")),
    ):
        for pattern in patterns:
            for path in sorted(root.rglob(pattern)):
                if skip.intersection(path.parts):
                    continue
                scanned += 1
                for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    if value in line and not line.lstrip().startswith(("#", "//")):
                        hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert scanned > 50, f"scanned only {scanned} files; the walk is not reaching the tree"
    assert not hits, (
        f"{value!r} is stated outside images/agent-runtime-browser/Dockerfile:\n  "
        + "\n  ".join(hits)
        + "\nRefer to the image's PLAYWRIGHT_BROWSERS_PATH instead of copying the path."
    )
