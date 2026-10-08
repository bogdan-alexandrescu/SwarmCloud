"""The workspace deployer's cluster access, and the policy that scopes it.

docs/workspaces.md §2.3, lane W5. `swarm-workspace-deployer` holds a
ClusterRole (kubernetes/rbac/provisioner-rbac.yaml) because RBAC cannot narrow
`create` by name, and four ValidatingAdmissionPolicies
(kubernetes/policies/workspace-provisioner-scope.yaml) narrow it back to
`swarm-tenant-u-*` and to exactly what the tenant render produces.

That policy RESTATES the render -- the three Roles' rules, whom each RoleBinding
binds, the kinds and names a tenant namespace holds -- because CEL cannot read
render.py. A restatement drifts, and here drift is not cosmetic in either
direction: a policy narrower than the render refuses a person's onboarding, and
one wider than it is the deployer's `escalate` with nothing behind it. So these
tests read the literals out of the rendered policy and hold them to the render.

They also hold the policy's matched users to the ClusterRoleBinding's subjects.
A spelling of the deployer bound there and not matched here would be the
ClusterRole with the scope check switched off.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from swarm_common.identity import tenant_id_for_group, tenant_id_for_user

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
CALLS = REPO / "scripts" / "lib" / "workspace-calls.json"

PROJECT = "saga-agents-staging"
SCHEDULER_UID = "117405034245659033603"
RECONCILER_UID = "108023754768362642341"
#: Shaped like a uniqueId; not any real account's.
DEPLOYER_UID = "100000000000000000042"
DEPLOYER_GSA = f"swarm-workspace-deployer@{PROJECT}.iam.gserviceaccount.com"
PERSON = "u-alice"

DEPLOYER_POLICIES = (
    "swarm-workspace-deployer-scope",
    "swarm-workspace-deployer-namespaces",
    "swarm-workspace-deployer-roles",
    "swarm-workspace-deployer-rolebindings",
)

#: kind -> (API group, resource), for the kinds a tenant render holds.
RESOURCES = {
    "Namespace": ("", "namespaces"),
    "ServiceAccount": ("", "serviceaccounts"),
    "ResourceQuota": ("", "resourcequotas"),
    "LimitRange": ("", "limitranges"),
    "NetworkPolicy": ("networking.k8s.io", "networkpolicies"),
    "Role": ("rbac.authorization.k8s.io", "roles"),
    "RoleBinding": ("rbac.authorization.k8s.io", "rolebindings"),
}


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_w5", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load_renderer()


def _render(*argv: str) -> list[dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        assert render.main(list(argv)) == 0
    return [d for d in yaml.safe_load_all(buffer.getvalue()) if d]


def _policies(deployer_uid: str = DEPLOYER_UID) -> list[dict[str, Any]]:
    argv = ["policies", "--project", PROJECT, "--scheduler-uid", SCHEDULER_UID,
            "--reconciler-uid", RECONCILER_UID]
    if deployer_uid:
        argv += ["--workspace-deployer-uid", deployer_uid]
    return _render(*argv)


@pytest.fixture(scope="module")
def policy_docs() -> list[dict[str, Any]]:
    return _policies()


@pytest.fixture(scope="module")
def tenant_docs() -> list[dict[str, Any]]:
    """A person's namespace as the deployer will apply it: both worker KSAs
    bound (the older one is rendered only where it is) and the control plane
    named by uniqueId, as kubernetes/apply.sh renders it."""
    return _render(
        "tenant", "--tenant", PERSON, "--project", PROJECT,
        "--bound-ksa", render.DEFAULT_KSA_NAME, "--bound-ksa", render.LEGACY_KSA_NAME,
        "--scheduler-uid", SCHEDULER_UID, "--reconciler-uid", RECONCILER_UID,
    )


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected one {kind}/{name}, found {len(found)}"
    return found[0]


def _variable(policy: dict[str, Any], name: str) -> Any:
    """A policy variable written as a JSON-shaped CEL literal, parsed."""
    for variable in policy["spec"].get("variables", []):
        if variable["name"] == name:
            return json.loads(variable["expression"])
    raise AssertionError(f"{policy['metadata']['name']} has no variable {name!r}")


def _matched_users(policy: dict[str, Any]) -> list[str]:
    conditions = policy["spec"].get("matchConditions") or []
    assert len(conditions) == 1, policy["metadata"]["name"]
    match = re.fullmatch(r"request\.userInfo\.username in (\[.*\])", conditions[0]["expression"].strip())
    assert match, conditions[0]["expression"]
    return json.loads(match.group(1))


def _expressions(policy: dict[str, Any]) -> list[str]:
    spec = policy["spec"]
    return (
        [v["expression"] for v in spec.get("variables", [])]
        + [v["expression"] for v in spec["validations"]]
    )


# ---------------------------------------------------------------------------
# The parity the brief asks for: the policy's literals are the render
# ---------------------------------------------------------------------------


def test_the_policys_role_literals_equal_the_render(policy_docs, tenant_docs):
    """MUTATION: add a verb to swarm-dispatcher in rbac/dispatcher-rbac.yaml, or
    edit the `expected` literal in the policy, and this names the Role."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-roles")
    expected = _variable(policy, "expected")
    rendered = {
        role["metadata"]["name"]: [
            [rule["apiGroups"], rule["resources"], rule["verbs"]] for rule in role.get("rules") or []
        ]
        for role in tenant_docs
        if role["kind"] == "Role"
    }
    assert set(rendered) == {"swarm-worker", "swarm-dispatcher", "swarm-reaper"}, (
        "the control: the render holds the three Roles this policy is about"
    )
    assert expected == rendered


def test_no_rendered_role_carries_what_the_policy_refuses(tenant_docs):
    """The policy refuses resourceNames and nonResourceURLs, and reads each
    rule's apiGroups, resources and verbs; a render that used any of those
    differently would be refused on the person's first onboarding."""
    for role in (d for d in tenant_docs if d["kind"] == "Role"):
        for rule in role.get("rules") or []:
            assert set(rule) == {"apiGroups", "resources", "verbs"}, (role["metadata"]["name"], rule)


def test_the_policys_rolebinding_subjects_equal_the_render(policy_docs, tenant_docs):
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-rolebindings")
    users = _variable(policy, "users")
    bindings = [d for d in tenant_docs if d["kind"] == "RoleBinding"]
    rendered: dict[str, list[str]] = {}
    for binding in bindings:
        ref = binding["roleRef"]
        assert (ref["apiGroup"], ref["kind"]) == ("rbac.authorization.k8s.io", "Role"), binding
        if ref["name"] == "swarm-worker":
            # What the policy requires of the worker Role's bindings.
            for subject in binding["subjects"]:
                assert subject["kind"] == "ServiceAccount", subject
                assert subject["namespace"] == binding["metadata"]["namespace"], subject
            continue
        assert all(s["kind"] == "User" for s in binding["subjects"]), binding
        rendered.setdefault(ref["name"], []).extend(s["name"] for s in binding["subjects"])
    assert set(rendered) == {"swarm-dispatcher", "swarm-reaper"}, "the control: both are rendered"
    assert {k: sorted(v) for k, v in users.items()} == {k: sorted(v) for k, v in rendered.items()}
    # Not vacuous: the uniqueIds really are in both, which is the spelling that
    # would be refused if apply.sh rendered the policy without them.
    assert SCHEDULER_UID in users["swarm-dispatcher"]
    assert RECONCILER_UID in users["swarm-reaper"]


def test_the_policys_kind_and_name_map_equals_the_render(policy_docs, tenant_docs):
    """Every namespaced object a person's render holds, and nothing else, by
    `group/resource`. The workspace job renders without --spec-verify-keys, so
    the spec-verify-keys ConfigMap is not among them."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    names = _variable(policy, "names")
    rendered: dict[str, set[str]] = {}
    for doc in tenant_docs:
        if doc["kind"] == "Namespace":
            continue
        group, resource = RESOURCES[doc["kind"]]
        assert doc["metadata"]["namespace"] == f"{render.NAMESPACE_PREFIX}{PERSON}", doc["metadata"]
        rendered.setdefault(f"{group}/{resource}", set()).add(doc["metadata"]["name"])
    assert {k: set(v) for k, v in names.items()} == rendered


# ---------------------------------------------------------------------------
# The policy matches exactly whom the RBAC binds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("deployer_uid", [DEPLOYER_UID, ""], ids=["uniqueId", "email-only"])
def test_every_policy_matches_exactly_the_subjects_the_rbac_binds(deployer_uid):
    """With the uniqueId and without it (apply.sh falls back to the email
    before the bootstrap apply has created the account): the same value reaches
    both documents, so the deployer is bound-and-scoped or not bound at all."""
    docs = _policies(deployer_uid)
    expected = {DEPLOYER_GSA, deployer_uid or DEPLOYER_GSA}
    for kind, name in (
        ("ClusterRoleBinding", "swarm-workspace-deployer"),
        ("RoleBinding", "swarm-workspace-deployer-dns"),
    ):
        binding = _one(docs, kind, name)
        assert all(s["kind"] == "User" for s in binding["subjects"]), binding["subjects"]
        assert {s["name"] for s in binding["subjects"]} == expected, name
    for name in DEPLOYER_POLICIES:
        policy = _one(docs, "ValidatingAdmissionPolicy", name)
        assert set(_matched_users(policy)) == expected, name


def test_a_deployer_uid_that_is_not_one_is_refused():
    with pytest.raises(SystemExit):
        _policies("someone-else@example.com")
    with pytest.raises(SystemExit):
        _policies('1", "2')


def test_nothing_but_the_deployer_policies_carries_a_deployer_subject(policy_docs):
    """The ClusterRole is bound once; no other object here names the deployer."""
    for doc in policy_docs:
        if doc["kind"] in ("ClusterRoleBinding", "RoleBinding"):
            assert doc["metadata"]["name"].startswith("swarm-workspace-deployer"), doc["metadata"]


# ---------------------------------------------------------------------------
# The RBAC itself
# ---------------------------------------------------------------------------


def test_the_cluster_role_is_the_grant_section_2_3_lists(policy_docs):
    role = _one(policy_docs, "ClusterRole", "swarm-workspace-deployer")
    granted: dict[tuple[str, str], set[str]] = {}
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                granted.setdefault((group, resource), set()).update(rule["verbs"])
    write = {"get", "create", "patch"}
    assert granted == {
        ("", "namespaces"): write,
        ("", "serviceaccounts"): write,
        ("", "resourcequotas"): write,
        ("", "limitranges"): write,
        ("networking.k8s.io", "networkpolicies"): write,
        ("rbac.authorization.k8s.io", "roles"): write | {"escalate", "bind"},
        ("rbac.authorization.k8s.io", "rolebindings"): write,
    }


def test_the_cluster_role_never_deletes_and_never_touches_a_workload_or_secret(policy_docs):
    role = _one(policy_docs, "ClusterRole", "swarm-workspace-deployer")
    for rule in role["rules"]:
        assert not {"delete", "deletecollection", "*", "list", "watch", "update"} & set(rule["verbs"]), rule
        assert "*" not in rule["apiGroups"] and "*" not in rule["resources"], rule
        assert not {"secrets", "pods", "jobs", "clusterroles", "clusterrolebindings"} & set(rule["resources"]), rule


def test_bind_names_exactly_the_three_roles_everywhere_they_are_listed(policy_docs):
    """The ClusterRole's `bind` resourceNames, the roles policy's keys, and the
    call guard's kube_role_names (W3) are one list in three places."""
    role = _one(policy_docs, "ClusterRole", "swarm-workspace-deployer")
    bind = [r for r in role["rules"] if "bind" in r["verbs"]]
    assert len(bind) == 1 and bind[0]["verbs"] == ["bind"], bind
    roles_policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-roles")
    guard = json.loads(CALLS.read_text())
    assert set(bind[0]["resourceNames"]) == set(_variable(roles_policy, "expected"))
    assert set(bind[0]["resourceNames"]) == set(guard["kube_role_names"])
    # `escalate` cannot be narrowed on create (the request has no name), so it
    # is alone in its rule and the policy is what bounds it.
    escalate = [r for r in role["rules"] if "escalate" in r["verbs"]]
    assert len(escalate) == 1 and escalate[0]["verbs"] == ["escalate"], escalate
    assert escalate[0]["resources"] == ["roles"]


def test_the_policy_and_the_call_guard_allow_the_same_kinds(policy_docs):
    scope = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    guard = json.loads(CALLS.read_text())
    plural = {f"{g}/{r}": kind for kind, (g, r) in RESOURCES.items()}
    kinds = {plural[key] for key in _variable(scope, "names")} | {"Namespace"}
    assert kinds == set(guard["kube_kinds"])


def test_the_kube_system_role_reads_two_named_objects_and_nothing_else(policy_docs):
    role = _one(policy_docs, "Role", "swarm-workspace-deployer-dns")
    assert role["metadata"]["namespace"] == "kube-system"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["services"], "resourceNames": ["kube-dns"], "verbs": ["get"]},
        {"apiGroups": ["apps"], "resources": ["daemonsets"], "resourceNames": ["node-local-dns"], "verbs": ["get"]},
    ]
    binding = _one(policy_docs, "RoleBinding", "swarm-workspace-deployer-dns")
    assert binding["metadata"]["namespace"] == "kube-system"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "swarm-workspace-deployer-dns",
    }


def test_every_object_carries_the_terraform_marker(policy_docs):
    for doc in policy_docs:
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform", doc["metadata"]["name"]


# ---------------------------------------------------------------------------
# The policies' shape
# ---------------------------------------------------------------------------


def test_the_scope_policy_is_applied_before_the_rbac_it_scopes():
    """kubectl applies a render in document order. RBAC first would leave a
    moment in which the deployer may create any namespace."""
    files = list(render.POLICY_FILES)
    assert files.index("policies/workspace-provisioner-scope.yaml") < files.index("rbac/provisioner-rbac.yaml")


def test_the_deployer_policies_fail_closed_deny_and_are_scoped_by_caller(policy_docs):
    for name in DEPLOYER_POLICIES:
        policy = _one(policy_docs, "ValidatingAdmissionPolicy", name)
        assert policy["spec"]["failurePolicy"] == "Fail", name
        binding = _one(policy_docs, "ValidatingAdmissionPolicyBinding", name)
        assert binding["spec"]["policyName"] == name
        assert "Deny" in binding["spec"]["validationActions"], name
        # A namespaceSelector would let the deployer escape by writing a
        # namespace without the selected label.
        assert "matchResources" not in binding["spec"], name


def test_the_scope_policy_reads_the_request_alone(policy_docs):
    """It matches every kind, which is safe only because no expression in it
    reads `object`, whose schema differs per kind."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    rules = policy["spec"]["matchConstraints"]["resourceRules"]
    assert rules == [{"apiGroups": ["*"], "apiVersions": ["*"], "operations": ["*"], "resources": ["*", "*/*"]}]
    for expression in _expressions(policy):
        assert not re.search(r"\b(object|oldObject)\b", expression), expression


def test_the_scope_policy_refuses_every_operation_but_create_and_update(policy_docs):
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    assert "request.operation == 'CREATE' || request.operation == 'UPDATE'" in _expressions(policy)


def _target_pattern(policy_docs: list[dict[str, Any]]) -> re.Pattern[str]:
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    found = [
        m.group(1)
        for e in _expressions(policy)
        for m in [re.search(r"variables\.target\.matches\('([^']+)'\)", e)]
        if m
    ]
    assert len(found) == 1, found
    return re.compile(found[0])


@pytest.mark.parametrize(
    "email", ["alice@saga.xyz", "Bob.Smith@saga.xyz", "a" * 80 + "@saga.xyz", "o'neil+x@saga.xyz"]
)
def test_every_personal_namespace_the_contract_derives_is_in_scope(policy_docs, email):
    """The frozen contract derives a person's tenant (`tenant_id_for_user`);
    render.py prefixes it. Every such namespace must pass the scope check, or
    that person's onboarding is refused by the API server."""
    namespace = f"{render.NAMESPACE_PREFIX}{tenant_id_for_user(email)}"
    assert _target_pattern(policy_docs).fullmatch(namespace), namespace


@pytest.mark.parametrize(
    "namespace",
    [
        f"{render.NAMESPACE_PREFIX}{tenant_id_for_group('eng@saga.xyz')}",
        "swarm-system",
        "kube-system",
        "default",
        "swarm-tenant-u-",
        "swarm-tenant-u--",
        "xswarm-tenant-u-alice",
        "agents-staging",
        "",
    ],
)
def test_no_other_namespace_is_in_scope(policy_docs, namespace):
    assert not _target_pattern(policy_docs).fullmatch(namespace), namespace


def test_the_namespace_policy_holds_the_labels_the_worker_policies_select_on(policy_docs, tenant_docs):
    """The rendered namespace satisfies the label checks, and the label they
    require is the one pod-security.yaml's bindings select on."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-namespaces")
    expressions = " ".join(_expressions(policy))
    labels = _one(tenant_docs, "Namespace", f"{render.NAMESPACE_PREFIX}{PERSON}")["metadata"]["labels"]
    assert labels["app.kubernetes.io/part-of"] == "swarm"
    assert f"{render.NAMESPACE_PREFIX}{labels['swarm-tenant']}" == f"{render.NAMESPACE_PREFIX}{PERSON}"
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    for label in ("app.kubernetes.io/part-of", "swarm-tenant", "pod-security.kubernetes.io/enforce"):
        assert f"'{label}'" in expressions, label
    selectors = {
        json.dumps(b["spec"]["matchResources"]["namespaceSelector"]["matchLabels"], sort_keys=True)
        for b in policy_docs
        if b["kind"] == "ValidatingAdmissionPolicyBinding" and "matchResources" in b["spec"]
    }
    assert selectors == {json.dumps({"app.kubernetes.io/part-of": "swarm"})}
