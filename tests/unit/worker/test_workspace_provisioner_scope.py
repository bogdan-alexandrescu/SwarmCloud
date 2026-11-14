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
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from swarm_common.identity import tenant_id_for_group, tenant_id_for_user

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
CALLS = REPO / "scripts" / "lib" / "workspace-calls.json"

_spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_P)
placeholder_deployment = _P.deployment()

PROJECT = _P.PROJECT
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
    "swarm-workspace-deployer-networkpolicies",
    "swarm-workspace-deployer-fqdnnetworkpolicies",
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
    "FQDNNetworkPolicy": ("networking.gke.io", "fqdnnetworkpolicies"),
}

#: The tenant's one hostname policy (render.py TENANT_FQDN_FILES), rendered only
#: where the cluster serves the kind (docs/runbooks/typesafe-egress.md).
FQDN_POLICY = "swarm-allow-typesafe-direct"


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
    named by uniqueId, as kubernetes/apply.sh renders it -- on a cluster that
    serves FQDNNetworkPolicy, so the render holds every object it ever can."""
    return _render(
        "tenant", "--tenant", PERSON, "--project", PROJECT, "--fqdn-network-policy",
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
    """The users the policy's `workspace-deployer` condition matches. The two
    content policies carry one more condition, on the namespace, and none
    other may carry any."""
    conditions = policy["spec"].get("matchConditions") or []
    names = [c["name"] for c in conditions]
    assert names in (["workspace-deployer"], ["workspace-deployer", "swarm-tenant-namespace"]), names
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
        ("networking.gke.io", "fqdnnetworkpolicies"): write,
    }


def test_the_hostname_policy_grant_names_the_one_policy(policy_docs, tenant_docs):
    """`get` and `patch` on FQDNNetworkPolicies name swarm-allow-typesafe-direct
    and nothing else; `create`, which RBAC cannot narrow by name, is alone in
    its rule, and the scope policy admits that one name. MUTATION: drop the
    resourceNames and the deployer may read and patch every hostname policy
    in the cluster, swarm-system's forwarder policy included."""
    role = _one(policy_docs, "ClusterRole", "swarm-workspace-deployer")
    fqdn = [r for r in role["rules"] if "fqdnnetworkpolicies" in r["resources"]]
    assert all(r["apiGroups"] == ["networking.gke.io"] and r["resources"] == ["fqdnnetworkpolicies"]
               for r in fqdn), fqdn
    named = [r for r in fqdn if "resourceNames" in r]
    unnamed = [r for r in fqdn if "resourceNames" not in r]
    assert [(sorted(r["verbs"]), r["resourceNames"]) for r in named] == [(["get", "patch"], [FQDN_POLICY])]
    assert [r["verbs"] for r in unnamed] == [["create"]], unnamed
    scope = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    assert _variable(scope, "names")["networking.gke.io/fqdnnetworkpolicies"] == [FQDN_POLICY]
    rendered = [d["metadata"]["name"] for d in tenant_docs if d["kind"] == "FQDNNetworkPolicy"]
    assert rendered == [FQDN_POLICY]


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
    # The hostname policy included: kubernetes/apply.sh renders it under the
    # job's call guard where the cluster serves the kind, and the guard admits
    # it by its one name (kube_kind_names), as the ClusterRole grants get and
    # patch on that name alone.
    assert kinds == set(guard["kube_kinds"])
    assert guard["kube_kind_names"] == {"FQDNNetworkPolicy": ["swarm-allow-typesafe-direct"]}
    apply_sh = (KUBERNETES / "apply.sh").read_text()
    assert "hostname policy  skipped: the workspace call guard" not in apply_sh


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
    """It matches every kind the deployer writes, which is safe only because no
    expression in it reads `object`, whose schema differs per kind. It does read
    `namespaceObject` (the workspace label), which is a Namespace whatever kind
    the request is for, so there is still no schema to disagree about."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    for expression in _expressions(policy):
        assert not re.search(r"\b(object|oldObject)\b", expression), expression


#: The verbs that write. A grant of any of these reaches admission, so the
#: scope policy has to match its resource; `get` never does.
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "escalate", "bind", "*"}


def _rbac_write_grants() -> set[tuple[str, str]]:
    """Every (API group, resource) a ClusterRole or Role in
    provisioner-rbac.yaml grants a write verb on, read from the file itself so a
    grant added there is seen here without anyone listing it."""
    granted: set[tuple[str, str]] = set()
    for doc in yaml.safe_load_all((KUBERNETES / "rbac" / "provisioner-rbac.yaml").read_text()):
        if not doc or doc["kind"] not in {"ClusterRole", "Role"}:
            continue
        for rule in doc.get("rules", []):
            if WRITE_VERBS & set(rule["verbs"]):
                granted |= {(g, r) for g in rule["apiGroups"] for r in rule["resources"]}
    return granted


def test_the_scope_policy_matches_every_resource_the_rbac_lets_it_write(policy_docs):
    """GKE Autopilot's admission webhook refuses a rule matching ("*", "*") or
    ("*", "*/*") (W5, measured 2026-10-11), so the scope policy lists its
    resources. The list is the fence only if it covers every write RBAC grants:
    a resource granted there and missing here is a write nothing scopes."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    rules = policy["spec"]["matchConstraints"]["resourceRules"]
    matched: set[tuple[str, str]] = set()
    for rule in rules:
        assert rule["operations"] == ["CREATE", "UPDATE", "DELETE"], rule
        assert rule["apiVersions"] == ["*"], rule
        for group in rule["apiGroups"]:
            assert "*" not in group, rule
            for resource in rule["resources"]:
                assert "*" not in resource, rule
                matched.add((group, resource))
    grants = _rbac_write_grants()
    assert grants, "provisioner-rbac.yaml grants no write: the reader is broken"
    assert grants <= matched, f"written but unscoped: {sorted(grants - matched)}"
    assert all("*" not in g and "*" not in r for g, r in grants), grants


def test_the_scope_policy_refuses_every_operation_but_create_and_update(policy_docs):
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    assert "request.operation == 'CREATE' || request.operation == 'UPDATE'" in _expressions(policy)


def _target_pattern(policy_docs: list[dict[str, Any]]) -> re.Pattern[str]:
    """The personal arm: the one `swarm-tenant-u-*` pattern, unchanged by the
    workspace label (its own tests are below)."""
    policy = _one(policy_docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope")
    personal = [v["expression"] for v in policy["spec"]["variables"] if v["name"] == "personal"]
    assert len(personal) == 1, personal
    found = re.fullmatch(r"variables\.target\.matches\('([^']+)'\)", personal[0].strip())
    assert found, personal[0]
    assert "variables.personal" in " ".join(_expressions(policy))
    return re.compile(found.group(1))


@pytest.mark.parametrize(
    "email", ["alice@example.com", "Bob.Smith@example.com", "a" * 80 + "@example.com", "o'neil+x@example.com"]
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
        f"{render.NAMESPACE_PREFIX}{tenant_id_for_group('eng@example.com')}",
        "swarm-system",
        "kube-system",
        "default",
        "swarm-tenant-u-",
        "swarm-tenant-u--",
        "xswarm-tenant-u-alice",
        _P.PEER["cluster"],
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


# ---------------------------------------------------------------------------
# The policies, EVALUATED: a group or service workspace (workspace-kinds §4.3)
# ---------------------------------------------------------------------------
#
# docs/design/workspace-kinds.md §3.1 and §4.3 (lane WK-CLUSTER, Q3 (a)): the
# deployer may now write a Namespace `swarm-tenant-<id>` for any id, but only
# when it carries `<prefix>/workspace: w-<6 hex>` on CREATE, only when
# the old object already carried it on UPDATE, and an object inside one only
# when the namespace carries it. The namespaces of the tenants terraform/infra
# declares are refused by name. The `swarm-tenant-u-*` arm is unchanged.
#
# Those are claims about what the API server does with a request, so they are
# tested by running the policies' own CEL against requests shaped like the
# AdmissionRequest kubectl sends -- not by reading the expressions for words.
# No CEL library is a dependency of this repository, so the evaluator below
# implements the subset these four policies use, with CEL's semantics where
# they matter here: `&&` and `||` absorb an error when the other side decides,
# reading an absent field is an error, and an error in a validation is a
# refusal (failurePolicy: Fail). Anything outside the subset raises
# CelUnsupported, which fails the test instead of reading as a refusal.


class CelError(Exception):
    """A CEL evaluation error: the API server refuses (failurePolicy Fail)."""


class CelUnsupported(Exception):
    """Outside the subset this evaluator implements: a bug in the test."""


_CEL_TOKEN = re.compile(
    r"""\s*(?:
      (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<num>\d+)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>&&|\|\||==|!=|<=|>=|[<>!?:()\[\]{},.+\-])
    )""",
    re.VERBOSE,
)


def _cel_tokens(text: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    at = 0
    text = text.rstrip()
    while at < len(text):
        match = _CEL_TOKEN.match(text, at)
        if not match or match.end() == at:
            raise CelUnsupported(f"cannot tokenize {text[at:at + 30]!r}")
        at = match.end()
        if match.group("str") is not None:
            raw = match.group("str")[1:-1]
            tokens.append(("lit", re.sub(r"\\(.)", r"\1", raw)))
        elif match.group("num") is not None:
            tokens.append(("lit", int(match.group("num"))))
        elif match.group("ident") is not None:
            tokens.append(("ident", match.group("ident")))
        else:
            tokens.append(("op", match.group("op")))
    tokens.append(("end", None))
    return tokens


class _CelParser:
    def __init__(self, text: str) -> None:
        self.tokens = _cel_tokens(text)
        self.at = 0

    def peek(self) -> tuple[str, Any]:
        return self.tokens[self.at]

    def take(self, value: Any = None) -> tuple[str, Any]:
        token = self.tokens[self.at]
        if value is not None and token[1] != value:
            raise CelUnsupported(f"expected {value!r}, got {token!r}")
        self.at += 1
        return token

    def accept(self, value: str) -> bool:
        if self.peek()[0] in ("op", "ident") and self.peek()[1] == value:
            self.at += 1
            return True
        return False

    def parse(self) -> Any:
        node = self.cond()
        if self.peek()[0] != "end":
            raise CelUnsupported(f"trailing {self.peek()!r}")
        return node

    def cond(self) -> Any:
        node = self.orr()
        if self.accept("?"):
            then = self.cond()
            self.take(":")
            return ("?", node, then, self.cond())
        return node

    def orr(self) -> Any:
        node = self.andd()
        while self.accept("||"):
            node = ("||", node, self.andd())
        return node

    def andd(self) -> Any:
        node = self.rel()
        while self.accept("&&"):
            node = ("&&", node, self.rel())
        return node

    def rel(self) -> Any:
        node = self.add()
        while self.peek()[1] in ("==", "!=", "<", "<=", ">", ">=", "in") and self.peek()[0] in ("op", "ident"):
            op = self.take()[1]
            node = (op, node, self.add())
        return node

    def add(self) -> Any:
        node = self.unary()
        while self.peek() in (("op", "+"), ("op", "-")):
            node = (self.take()[1], node, self.unary())
        return node

    def unary(self) -> Any:
        if self.accept("!"):
            return ("!", self.unary())
        return self.member()

    def args(self, close: str) -> list[Any]:
        items: list[Any] = []
        while not self.accept(close):
            items.append(self.cond())
            if not self.accept(","):
                self.take(close)
                break
        return items

    def member(self) -> Any:
        node = self.primary()
        while True:
            if self.accept("."):
                name = self.take()[1]
                if self.accept("("):
                    node = ("call", node, name, self.args(")"))
                else:
                    node = ("sel", node, name)
            elif self.accept("["):
                node = ("idx", node, self.cond())
                self.take("]")
            else:
                return node

    def primary(self) -> Any:
        kind, value = self.take()
        if kind == "lit":
            return ("lit", value)
        if kind == "ident":
            if value in ("true", "false"):
                return ("lit", value == "true")
            if value == "null":
                return ("lit", None)
            if self.accept("("):
                return ("call", None, value, self.args(")"))
            return ("ident", value)
        if value == "(":
            node = self.cond()
            self.take(")")
            return node
        if value == "[":
            return ("list", self.args("]"))
        if value == "{":
            pairs: list[tuple[Any, Any]] = []
            while not self.accept("}"):
                key = self.cond()
                self.take(":")
                pairs.append((key, self.cond()))
                if not self.accept(","):
                    self.take("}")
                    break
            return ("map", pairs)
        raise CelUnsupported(f"unexpected {value!r}")


_CEL_TYPES = {list: "list", dict: "map", str: "string", bool: "bool", int: "int", type(None): "null_type"}


def _cel_eval(node: Any, env: dict[str, Any]) -> Any:
    op = node[0]
    if op == "lit":
        return node[1]
    if op == "ident":
        name = node[1]
        if name in env:
            return env[name]
        if name in ("list", "map", "string", "bool", "int"):
            return name
        raise CelUnsupported(f"unknown identifier {name!r}")
    if op == "list":
        return [_cel_eval(n, env) for n in node[1]]
    if op == "map":
        return {_cel_eval(k, env): _cel_eval(v, env) for k, v in node[1]}
    if op in ("&&", "||"):
        decided = op == "||"
        try:
            left = _cel_eval(node[1], env)
        except CelError:
            right = _cel_eval(node[2], env)
            if right is decided:
                return decided
            raise
        if _bool(left) is decided:
            return decided
        return _bool(_cel_eval(node[2], env))
    if op == "?":
        return _cel_eval(node[2] if _bool(_cel_eval(node[1], env)) else node[3], env)
    if op == "!":
        return not _bool(_cel_eval(node[1], env))
    if op == "sel":
        target = _cel_eval(node[1], env)
        if not isinstance(target, dict) or node[2] not in target:
            raise CelError(f"no such key: {node[2]}")
        return target[node[2]]
    if op == "idx":
        target, key = _cel_eval(node[1], env), _cel_eval(node[2], env)
        try:
            return target[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise CelError(f"no such key: {key!r}") from exc
    if op in ("==", "!="):
        equal = _cel_eval(node[1], env) == _cel_eval(node[2], env)
        return equal if op == "==" else not equal
    if op == "in":
        item, container = _cel_eval(node[1], env), _cel_eval(node[2], env)
        if not isinstance(container, (list, dict)):
            raise CelError("`in` on a non-container")
        return item in container
    if op == "+":
        left, right = _cel_eval(node[1], env), _cel_eval(node[2], env)
        if type(left) is not type(right) or not isinstance(left, (str, list, int)):
            raise CelError("no matching overload for +")
        return left + right
    if op == "call":
        return _cel_call(node, env)
    raise CelUnsupported(f"operator {op!r}")


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise CelError(f"expected bool, got {value!r}")
    return value


def _cel_call(node: Any, env: dict[str, Any]) -> Any:
    _, target, name, args = node
    if target is None:
        if name == "has":
            (field,) = args
            if field[0] != "sel":
                raise CelUnsupported("has() takes a field selection")
            container = _cel_eval(field[1], env)
            if not isinstance(container, dict):
                raise CelError("has() on a non-message")
            return field[2] in container
        if name == "type":
            return _CEL_TYPES[type(_cel_eval(args[0], env))]
        if name == "size":
            return len(_cel_eval(args[0], env))
        if name == "string":
            value = _cel_eval(args[0], env)
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise CelError(f"string() of {value!r}")
            return str(value)
        raise CelUnsupported(f"function {name}()")
    if name in ("all", "exists", "map"):
        items = _cel_eval(target, env)
        if not isinstance(items, list):
            raise CelError(f"{name}() on a non-list")
        variable, body = args[0][1], args[1]
        results = [_cel_eval(body, {**env, variable: item}) for item in items]
        if name == "map":
            return results
        return all(_bool(r) for r in results) if name == "all" else any(_bool(r) for r in results)
    receiver = _cel_eval(target, env)
    values = [_cel_eval(a, env) for a in args]
    if name == "join":
        if not isinstance(receiver, list) or not all(isinstance(i, str) for i in receiver):
            raise CelError("join() on a non-list of strings")
        return values[0].join(receiver)
    if not isinstance(receiver, str):
        raise CelError(f"{name}() on a non-string")
    if name == "matches":
        return re.search(values[0], receiver) is not None
    if name == "startsWith":
        return receiver.startswith(values[0])
    if name == "endsWith":
        return receiver.endswith(values[0])
    raise CelUnsupported(f"method {name}()")


class _Variables(dict):
    """`variables.<name>`, evaluated on first read, in the policy's scope."""

    def __init__(self, policy: dict[str, Any], env: dict[str, Any]) -> None:
        super().__init__()
        self._sources = {v["name"]: v["expression"] for v in policy["spec"].get("variables", [])}
        self._env = env

    def __contains__(self, key: object) -> bool:
        return key in self._sources

    def __getitem__(self, key: str) -> Any:
        if not dict.__contains__(self, key):
            dict.__setitem__(self, key, _cel_eval(_CelParser(self._sources[key]).parse(), self._env))
        return dict.__getitem__(self, key)


def _matches_rules(policy: dict[str, Any], request: dict[str, Any]) -> bool:
    group = request["resource"].get("group", "")
    for rule in policy["spec"]["matchConstraints"]["resourceRules"]:
        if (
            group in rule["apiGroups"]
            and request["resource"]["resource"] in rule["resources"]
            and request["operation"] in rule["operations"]
        ):
            return True
    return False


def _verdict(
    policy: dict[str, Any],
    request: dict[str, Any],
    obj: dict[str, Any] | None,
    old: dict[str, Any] | None,
    namespace_object: dict[str, Any] | None,
) -> str | None:
    """None when this policy admits the request, else why it refuses."""
    if not _matches_rules(policy, request):
        return None
    env: dict[str, Any] = {
        "request": request, "object": obj, "oldObject": old, "namespaceObject": namespace_object,
    }
    env["variables"] = _Variables(policy, env)
    try:
        for condition in policy["spec"].get("matchConditions", []):
            if not _bool(_cel_eval(_CelParser(condition["expression"]).parse(), env)):
                return None
        for validation in policy["spec"]["validations"]:
            if not _bool(_cel_eval(_CelParser(validation["expression"]).parse(), env)):
                return validation["message"]
    except CelError as exc:
        return f"evaluation error (fails closed): {exc}"
    return None


def _request(
    operation: str, kind: str, name: str, namespace: str | None = None, user: str = DEPLOYER_GSA
) -> dict[str, Any]:
    group, resource = RESOURCES[kind]
    request: dict[str, Any] = {
        "operation": operation,
        "resource": {"resource": resource, "version": "v1"},
        "name": name,
        "userInfo": {"username": user},
    }
    # AdmissionRequest omits empty fields, which is why the policies use has().
    if group:
        request["resource"]["group"] = group
    if namespace is not None:
        request["namespace"] = namespace
    return request


def _refusals(
    docs: list[dict[str, Any]],
    operation: str,
    obj: dict[str, Any],
    *,
    old: dict[str, Any] | None = None,
    namespace_object: dict[str, Any] | None = None,
    user: str = DEPLOYER_GSA,
) -> list[str]:
    """Every deployer policy's refusal of writing `obj` as the deployer."""
    namespace = obj["metadata"].get("namespace")
    request = _request(operation, obj["kind"], obj["metadata"]["name"], namespace, user)
    found = []
    for name in DEPLOYER_POLICIES:
        reason = _verdict(_one(docs, "ValidatingAdmissionPolicy", name), request, obj,
                          old if operation == "UPDATE" else None,
                          namespace_object if namespace is not None else None)
        if reason is not None:
            found.append(f"{name}: {reason}")
    return found


_PLACEHOLDERS_SPEC = importlib.util.spec_from_file_location("placeholders", Path(__file__).resolve().parents[2] / "placeholders.py")
_PLACEHOLDERS = importlib.util.module_from_spec(_PLACEHOLDERS_SPEC)
_PLACEHOLDERS_SPEC.loader.exec_module(_PLACEHOLDERS)
#: The label-key prefix the suite renders with: the placeholder organisation's
#: swarm.<org_domain> (kubernetes/label_prefix.py; tests/unit/conftest.py).
LABEL_PREFIX = f"swarm.{_PLACEHOLDERS.ORG_DOMAIN}"
WORKSPACE_LABEL = f"{LABEL_PREFIX}/workspace"
WORKSPACE_ID = "w-000000"
GROUP = "research"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"


def _tfvars_tenants(path: Path) -> list[str]:
    """The keys of `tenants = { ... }` in a tfvars file, as `terraform fmt`
    lays it out: one `  <key> = {` line per tenant."""
    keys: list[str] = []
    inside = False
    for line in path.read_text().splitlines():
        if re.fullmatch(r"tenants\s*=\s*\{", line.strip()) and not line.startswith(" "):
            inside = True
            continue
        if inside and line.startswith("}"):
            break
        match = re.fullmatch(r"  ([a-z0-9][-a-z0-9]*)\s*=\s*\{", line) if inside else None
        if match:
            keys.append(match.group(1))
    return keys


@pytest.fixture(scope="module")
def dev_policy_docs() -> list[dict[str, Any]]:
    """The policies as kubernetes/apply.sh --policies renders them for dev."""
    return _render(
        "policies", "--project", PROJECT, "--scheduler-uid", SCHEDULER_UID,
        "--reconciler-uid", RECONCILER_UID, "--workspace-deployer-uid", DEPLOYER_UID,
        "--terraform-tfvars", str(DEV_TFVARS),
    )


def _tenant_render(tenant: str, *extra: str) -> list[dict[str, Any]]:
    return _render(
        "tenant", "--tenant", tenant, "--project", PROJECT,
        "--bound-ksa", render.DEFAULT_KSA_NAME,
        "--scheduler-uid", SCHEDULER_UID, "--reconciler-uid", RECONCILER_UID, *extra,
    )


@pytest.fixture(scope="module")
def group_docs() -> list[dict[str, Any]]:
    """A group workspace's namespace as the job's A7 renders it (§4.3)."""
    return _tenant_render(GROUP, "--workspace-id", WORKSPACE_ID)


def _namespace(name: str, labels: dict[str, str] | None = None) -> dict[str, Any]:
    """A Namespace carrying the labels the namespaces policy requires, plus `labels`."""
    tenant = name.removeprefix(render.NAMESPACE_PREFIX)
    base = {
        "app.kubernetes.io/part-of": "swarm",
        "swarm-tenant": tenant,
        "pod-security.kubernetes.io/enforce": "restricted",
    }
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": name, "labels": {**base, **(labels or {})}}}


def _account(namespace: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": render.DEFAULT_KSA_NAME, "namespace": namespace}}


LABELLED = {WORKSPACE_LABEL: WORKSPACE_ID}


# --- the render ------------------------------------------------------------


def test_the_workspace_id_labels_the_namespace_and_nothing_else(group_docs):
    """§6 WK-CLUSTER: `--workspace-id w-000000` puts the label on the Namespace
    and nowhere else; every other document renders as it does without it."""
    namespace = _one(group_docs, "Namespace", f"{render.NAMESPACE_PREFIX}{GROUP}")
    assert namespace["metadata"]["labels"][WORKSPACE_LABEL] == WORKSPACE_ID
    plain = _tenant_render(GROUP)
    assert len(plain) == len(group_docs)
    for labelled, unlabelled in zip(group_docs, plain):
        if labelled["kind"] == "Namespace":
            assert WORKSPACE_LABEL not in unlabelled["metadata"]["labels"], "the control"
            stripped = {k: v for k, v in labelled["metadata"]["labels"].items() if k != WORKSPACE_LABEL}
            assert stripped == unlabelled["metadata"]["labels"]
            continue
        assert labelled == unlabelled, labelled["metadata"]
        assert WORKSPACE_LABEL not in json.dumps(labelled), labelled["metadata"]


@pytest.mark.parametrize(
    "workspace_id", ["w-00000", "w-0000000", "w-ABCDEF", "x-000000", "w-000000\n    x: y", "w-00000g"]
)
def test_a_workspace_id_that_is_not_one_is_refused(workspace_id):
    with pytest.raises(SystemExit):
        _tenant_render(GROUP, "--workspace-id", workspace_id)


# --- the label arm -----------------------------------------------------------


def test_a_labelled_group_namespace_is_admitted_on_create_and_an_unlabelled_one_refused(dev_policy_docs):
    name = f"{render.NAMESPACE_PREFIX}{GROUP}"
    assert _refusals(dev_policy_docs, "CREATE", _namespace(name, LABELLED)) == []
    assert _refusals(dev_policy_docs, "CREATE", _namespace(name)), "created without the label"


def test_every_object_of_a_group_workspace_render_is_admitted(dev_policy_docs, group_docs):
    """The whole of A7 for a group, as the job applies it: the Namespace, then
    every object in it, judged with that Namespace as `namespaceObject`."""
    namespace = _one(group_docs, "Namespace", f"{render.NAMESPACE_PREFIX}{GROUP}")
    for doc in group_docs:
        assert _refusals(dev_policy_docs, "CREATE", doc, namespace_object=namespace) == [], doc["metadata"]
        assert _refusals(dev_policy_docs, "UPDATE", doc, old=doc, namespace_object=namespace) == [], (
            doc["metadata"]
        )


def test_every_object_of_a_personal_render_is_still_admitted(dev_policy_docs):
    """The control on the evaluator, and the `u-*` arm kept: today's personal
    render, unlabelled, is admitted object by object, as it is live."""
    docs = _tenant_render(PERSON)
    namespace = _one(docs, "Namespace", f"{render.NAMESPACE_PREFIX}{PERSON}")
    assert WORKSPACE_LABEL not in namespace["metadata"]["labels"]
    for doc in docs:
        assert _refusals(dev_policy_docs, "CREATE", doc, namespace_object=namespace) == [], doc["metadata"]


def test_the_personal_arm_still_admits_an_unlabelled_personal_namespace(dev_policy_docs):
    name = f"{render.NAMESPACE_PREFIX}u-bob"
    assert _refusals(dev_policy_docs, "CREATE", _namespace(name)) == []
    assert _refusals(dev_policy_docs, "UPDATE", _namespace(name), old=_namespace(name)) == []
    assert _refusals(dev_policy_docs, "CREATE", _account(name), namespace_object=_namespace(name)) == []


def test_an_update_adding_the_label_to_an_unlabelled_namespace_is_refused(dev_policy_docs):
    """A hand-made namespace cannot be adopted by labelling it."""
    name = f"{render.NAMESPACE_PREFIX}{GROUP}"
    assert _refusals(dev_policy_docs, "UPDATE", _namespace(name, LABELLED), old=_namespace(name))
    assert _refusals(dev_policy_docs, "UPDATE", _namespace(name), old=_namespace(name))
    # The control: a namespace the job created is re-applied (a retry).
    assert _refusals(
        dev_policy_docs, "UPDATE", _namespace(name, LABELLED), old=_namespace(name, LABELLED)
    ) == []


def test_the_label_never_changes_or_goes_once_written(dev_policy_docs):
    for name in (f"{render.NAMESPACE_PREFIX}{GROUP}", f"{render.NAMESPACE_PREFIX}u-bob"):
        old = _namespace(name, LABELLED)
        assert _refusals(dev_policy_docs, "UPDATE", _namespace(name, {WORKSPACE_LABEL: "w-ffffff"}), old=old), name
        assert _refusals(dev_policy_docs, "UPDATE", _namespace(name), old=old), name


@pytest.mark.parametrize("value", ["w-00000", "w-ABCDEF", "", "eng", "w-0000000"])
def test_a_label_that_is_not_a_workspace_id_is_refused(dev_policy_docs, value):
    name = f"{render.NAMESPACE_PREFIX}{GROUP}"
    assert _refusals(dev_policy_docs, "CREATE", _namespace(name, {WORKSPACE_LABEL: value}))


def test_an_object_in_an_unlabelled_namespace_is_refused(dev_policy_docs):
    """§6: refuses a namespaced object in `swarm-tenant-eng` (unlabelled); the
    same object in a labelled job-made namespace is the control."""
    eng = f"{render.NAMESPACE_PREFIX}eng"
    assert _refusals(dev_policy_docs, "CREATE", _account(eng), namespace_object=_namespace(eng))
    assert _refusals(dev_policy_docs, "UPDATE", _account(eng), old=_account(eng), namespace_object=_namespace(eng))
    hand_made = f"{render.NAMESPACE_PREFIX}{GROUP}"
    assert _refusals(dev_policy_docs, "CREATE", _account(hand_made), namespace_object=_namespace(hand_made))
    assert _refusals(
        dev_policy_docs, "CREATE", _account(hand_made), namespace_object=_namespace(hand_made, LABELLED)
    ) == []


def _terraform_namespaces(docs: list[dict[str, Any]]) -> list[str]:
    return _variable(_one(docs, "ValidatingAdmissionPolicy", "swarm-workspace-deployer-scope"), "terraform")


def test_the_rendered_terraform_list_is_the_environments_tenants(dev_policy_docs):
    keys = _tfvars_tenants(DEV_TFVARS)
    assert "eng" in keys and "smoke" in keys, f"the control: the reader found {keys}"
    assert sorted(_terraform_namespaces(dev_policy_docs)) == sorted(
        f"{render.NAMESPACE_PREFIX}{k}" for k in keys
    )


def test_an_offline_render_refuses_every_environments_tenants(policy_docs):
    """Without --terraform-tfvars (lint, CI, the Makefile) the list is the union
    of every environment's: a render that refuses more is safe to apply
    anywhere; one that refuses fewer is not."""
    expected = {
        f"{render.NAMESPACE_PREFIX}{k}"
        for path in sorted((REPO / "terraform" / "environments").glob("*/*.tfvars"))
        for k in _tfvars_tenants(path)
    }
    assert f"{render.NAMESPACE_PREFIX}eng" in expected
    assert set(_terraform_namespaces(policy_docs)) == expected


def test_each_terraform_namespace_is_refused_even_with_the_label(dev_policy_docs):
    names = _terraform_namespaces(dev_policy_docs)
    assert names, "the control: the rendered list names the Terraform tenants"
    for name in names:
        assert _refusals(dev_policy_docs, "CREATE", _namespace(name, LABELLED)), name
        assert _refusals(dev_policy_docs, "UPDATE", _namespace(name, LABELLED), old=_namespace(name, LABELLED)), name
        assert _refusals(
            dev_policy_docs, "CREATE", _account(name), namespace_object=_namespace(name, LABELLED)
        ), name


def test_a_tree_without_terraform_renders_an_empty_list_and_says_so(monkeypatch, capsys):
    """A copy of kubernetes/ alone (test_posture_is_per_container.py makes one)
    still renders for lint; what apply.sh sends always names a tfvars file."""
    monkeypatch.setattr(render, "TFVARS_GLOB", "no-such-directory/*/*.tfvars")
    assert render.terraform_namespaces([]) == "[]"
    assert "refuses no Terraform tenant" in capsys.readouterr().err


def test_a_terraform_tfvars_that_names_no_tenant_is_refused(tmp_path):
    empty = tmp_path / "x.tfvars"
    empty.write_text('project_id = "p"\n')
    with pytest.raises(SystemExit):
        _render("policies", "--terraform-tfvars", str(empty))


@pytest.mark.parametrize("name", ["swarm-system", "kube-system", "default", _P.PEER["cluster"], "swarm-tenant-"])
def test_no_namespace_outside_the_prefix_is_admitted_even_with_the_label(dev_policy_docs, name):
    namespace = _namespace(name, LABELLED)
    namespace["metadata"]["labels"]["swarm-tenant"] = name.removeprefix(render.NAMESPACE_PREFIX)
    assert _refusals(dev_policy_docs, "CREATE", namespace), name
    assert _refusals(dev_policy_docs, "CREATE", _account(name), namespace_object=namespace), name


def test_the_policies_judge_nobody_but_the_deployer(dev_policy_docs):
    """The control on matchConditions: the same refused write by anyone else is
    not this policy's to judge."""
    eng = f"{render.NAMESPACE_PREFIX}eng"
    assert _refusals(
        dev_policy_docs, "CREATE", _namespace(eng), user="someone@example.com"
    ) == []


# --- kubernetes/apply.sh ------------------------------------------------------

APPLY = KUBERNETES / "apply.sh"


@pytest.fixture(scope="module", autouse=True)
def _placeholder_deployment(tmp_path_factory):
    yield from placeholder_deployment.activate(tmp_path_factory)


def _apply(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "GOOGLE_", "CLOUDSDK_"))}
    # apply.sh has no default project (DAYONE-K8S-1) and refuses an unset one
    # first; these refusals are the next ones, so a project is given.
    env.update(SWARM_ENV_FILE="/nonexistent/swarm.env", NO_COLOR="1", KUBECONFIG="/nonexistent/kubeconfig",
               PROJECT_ID=PROJECT)
    # SWARM_* is stripped above, so the placeholder region, zone and never-touch list go back in.
    env.update(placeholder_deployment.child_env(PROJECT))
    return subprocess.run(
        ["bash", str(APPLY), *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--policies", "--workspace-id", WORKSPACE_ID],
        ["--swarm-system", "--workspace-id", WORKSPACE_ID],
        ["--tenant", GROUP, "--workspace-id", "w-ABCDEF"],
        ["--tenant", GROUP, "--workspace-id=w-1"],
    ],
    ids=["policies", "swarm-system", "bad-id", "bad-id-equals"],
)
def test_apply_refuses_a_workspace_id_it_cannot_use_before_contacting_anything(args):
    result = _apply(*args)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "--workspace-id" in output, output
    assert "kubectl  " not in output, "it reached the cluster checks"


def test_apply_renders_the_policies_with_this_environments_tfvars():
    """The policies apply.sh sends refuse this environment's Terraform tenants
    by name; it says so with the file it read."""
    text = APPLY.read_text()
    block = text[text.index('MANIFEST="$("${PYTHON}" "${HERE}/render.py" policies'):]
    block = block[: block.index(")\"")]
    assert '--terraform-tfvars "${TF_TFVARS}"' in block, block
    assert 'TF_TFVARS="$(tf_var_file)"' in text


def test_apply_passes_the_workspace_id_to_the_tenant_render_alone():
    text = APPLY.read_text()
    tenant = text[text.index('MANIFEST="$("${PYTHON}" "${HERE}/render.py" tenant'):]
    tenant = tenant[: tenant.index(")\"")]
    assert "WORKSPACE_ARGS" in tenant, tenant
    identity = text[text.index('IDENTITY="$("${PYTHON}" "${HERE}/render.py" identity'):]
    identity = identity[: identity.index(")\"")]
    assert "WORKSPACE_ARGS" not in identity, identity


# ---------------------------------------------------------------------------
# A network policy's content, not only its name (#3941)
#
# swarm-workspace-deployer-networkpolicies and -fqdnnetworkpolicies hold each
# policy the deployer writes to the render's selector, peers and ports, as
# CEL literals in a normal form (the policy file's header says which). These
# tests encode the render the way the CEL encodes the object, and hold the
# literals equal to it, so the two cannot drift.
# ---------------------------------------------------------------------------

NETPOL_POLICY = "swarm-workspace-deployer-networkpolicies"
FQDN_CONTENT_POLICY = "swarm-workspace-deployer-fqdnnetworkpolicies"
#: The cluster's own network, as kubernetes/apply.sh reads it from a cluster:
#: shaped like a GKE cluster's ranges, and nobody's.
CLUSTER_NETWORK = (
    "--pod-cidr", "10.200.0.0/14", "--service-cidr", "10.204.0.0/20",
    "--cluster-dns-ip", "10.204.0.10", "--node-local-dns-ip", "169.254.20.10",
    "--network-source", "cluster",
)


def _cidr(value: str, fixed: list[str]) -> str:
    """The render's own CIDRs as written; a cluster network value by shape."""
    if value in fixed:
        return value
    return "~host" if value.endswith("/32") else "~range"


def _selector(selector: dict[str, Any] | None) -> dict[str, str]:
    if selector is None:
        return {"~": "absent"}
    if "matchExpressions" in selector:
        return {"~": "matchExpressions"}
    return selector.get("matchLabels", {})


def _port(port: dict[str, Any]) -> dict[str, str]:
    return {
        "protocol": port.get("protocol", ""),
        "port": str(port["port"]) if "port" in port else "~all",
        "endPort": str(port["endPort"]) if "endPort" in port else "",
    }


def _peer_ip(peer: dict[str, Any], fixed: list[str]) -> dict[str, str]:
    block = peer.get("ipBlock")
    if block is None:
        return {"~": "absent"}
    return {"cidr": _cidr(block["cidr"], fixed),
            "except": ",".join(_cidr(e, fixed) for e in block.get("except", []))}


def _netpol_rules(rules: list[dict[str, Any]] | None, peers_key: str, fixed: list[str]) -> list[Any]:
    encoded = []
    for rule in rules or []:
        peers = rule.get(peers_key, [])
        encoded.append([
            [_selector(p.get("namespaceSelector")) for p in peers],
            [_selector(p.get("podSelector")) for p in peers],
            [_peer_ip(p, fixed) for p in peers],
            [_port(q) for q in rule.get("ports", [])],
        ])
    return encoded


def _fqdn_port(port: dict[str, Any]) -> dict[str, str]:
    return {"protocol": port.get("protocol", ""), "port": str(port["port"]) if "port" in port else "~all"}


def _fqdn_rules(rules: list[dict[str, Any]] | None) -> list[Any]:
    return [
        [[{"name": m.get("name", ""), "pattern": m.get("pattern", "")} for m in rule.get("matches", [])],
         [_fqdn_port(q) for q in rule.get("ports", [])]]
        for rule in rules or []
    ]


def _encoded_netpols(docs: list[dict[str, Any]], fixed: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {"selectors": {}, "types": {}, "ingress": {}, "egress": {}}
    for doc in docs:
        if doc["kind"] != "NetworkPolicy":
            continue
        name, spec = doc["metadata"]["name"], doc["spec"]
        found["selectors"][name] = _selector(spec["podSelector"])
        found["types"][name] = spec.get("policyTypes", [])
        found["ingress"][name] = _netpol_rules(spec.get("ingress"), "from", fixed)
        found["egress"][name] = _netpol_rules(spec.get("egress"), "to", fixed)
    return found


def _content_policy(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return _one(docs, "ValidatingAdmissionPolicy", name)


def _fixed_cidrs(docs: list[dict[str, Any]]) -> list[str]:
    return _variable(_content_policy(docs, NETPOL_POLICY), "fixed_cidrs")


def _template_cidrs() -> set[str]:
    """Every CIDR the tenant's network policy templates write literally, not
    through a cluster network placeholder."""
    found: set[str] = set()
    for name in render.TENANT_FILES:
        if not name.startswith("network-policies/"):
            continue
        for doc in yaml.safe_load_all((KUBERNETES / name).read_text()):
            for rule in (doc or {}).get("spec", {}).get("egress", []) + (doc or {}).get("spec", {}).get("ingress", []):
                for peer in rule.get("to", []) + rule.get("from", []):
                    block = peer.get("ipBlock") or {}
                    for value in [block.get("cidr")] + list(block.get("except", [])):
                        if value and "__" not in value:
                            found.add(value)
    return found


def test_the_fixed_cidr_list_is_every_cidr_the_templates_write_literally(policy_docs):
    """MUTATION: add a literal CIDR to allow-egress.yaml and the content policy
    refuses every worker egress policy; this names it first."""
    fixed = _fixed_cidrs(policy_docs)
    assert sorted(fixed) == sorted(_template_cidrs())
    assert "0.0.0.0/0" in fixed, "the control: the reader found the open range"
    for token in ("POD_CIDR", "SERVICE_CIDR"):
        assert render.OFFLINE_NETWORK[token] not in fixed, token
    for token in ("CLUSTER_DNS_IP", "NODE_LOCAL_DNS_IP"):
        assert f"{render.OFFLINE_NETWORK[token]}/32" not in fixed, token


def test_the_network_policy_literals_equal_the_render(policy_docs, tenant_docs):
    """Selector, policy types, peers and ports, per name, are the render's.
    MUTATION: change a port in network-policies/allow-judge-egress.yaml, or a
    label in the policy's `egress` literal, and this names the policy."""
    policy = _content_policy(policy_docs, NETPOL_POLICY)
    encoded = _encoded_netpols(tenant_docs, _fixed_cidrs(policy_docs))
    for variable in ("selectors", "types", "ingress", "egress"):
        assert _variable(policy, variable) == encoded[variable], variable
    scope = _content_policy(policy_docs, "swarm-workspace-deployer-scope")
    assert sorted(encoded["selectors"]) == sorted(_variable(scope, "names")["networking.k8s.io/networkpolicies"])


def test_the_network_policy_literals_do_not_depend_on_the_cluster_network(policy_docs):
    """The policies render has no cluster network; the tenant render applied
    to a cluster has one. Both encode alike, so the literal holds for both."""
    fixed = _fixed_cidrs(policy_docs)
    offline = _encoded_netpols(_tenant_render(PERSON), fixed)
    live = _encoded_netpols(_tenant_render(PERSON, *CLUSTER_NETWORK), fixed)
    assert offline == live
    worker = _one(_tenant_render(PERSON, *CLUSTER_NETWORK), "NetworkPolicy", "swarm-allow-worker-egress")
    assert "10.200.0.0/14" in json.dumps(worker), "the control: the render carries the cluster's range"


def test_the_fqdn_policy_literals_equal_the_render(policy_docs, tenant_docs):
    """The TypeSafe direct policy selects the TypeSafe direct pods only and
    names one host, on TCP 443."""
    policy = _content_policy(policy_docs, FQDN_CONTENT_POLICY)
    rendered = _one(tenant_docs, "FQDNNetworkPolicy", FQDN_POLICY)
    assert _variable(policy, "selectors") == {FQDN_POLICY: _selector(rendered["spec"].get("podSelector"))}
    assert _variable(policy, "egress") == {FQDN_POLICY: _fqdn_rules(rendered["spec"].get("egress"))}
    egress = _variable(policy, "egress")[FQDN_POLICY]
    assert len(egress) == 1 and len(egress[0][0]) == 1, egress
    assert egress[0][0][0]["pattern"] == "" and egress[0][0][0]["name"], egress
    assert egress[0][1] == [{"protocol": "TCP", "port": "443"}], egress


def test_the_content_policies_match_one_kind_in_the_tenant_namespaces_only(policy_docs):
    for name, resource in ((NETPOL_POLICY, ("networking.k8s.io", "networkpolicies")),
                           (FQDN_CONTENT_POLICY, ("networking.gke.io", "fqdnnetworkpolicies"))):
        policy = _content_policy(policy_docs, name)
        rules = policy["spec"]["matchConstraints"]["resourceRules"]
        assert [(r["apiGroups"], r["resources"], r["operations"]) for r in rules] == [
            ([resource[0]], [resource[1]], ["CREATE", "UPDATE"])
        ], name
        conditions = {c["name"]: c["expression"] for c in policy["spec"]["matchConditions"]}
        assert render.NAMESPACE_PREFIX in conditions["swarm-tenant-namespace"], name


def _tenant_namespace(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _one(docs, "Namespace", f"{render.NAMESPACE_PREFIX}{PERSON}")


def _content_refusals(docs: list[dict[str, Any]], obj: dict[str, Any], namespace_object: dict[str, Any],
                      operation: str = "CREATE", user: str = DEPLOYER_GSA) -> list[str]:
    return [r for r in _refusals(docs, operation, obj, old=obj, namespace_object=namespace_object, user=user)
            if r.startswith((NETPOL_POLICY + ":", FQDN_CONTENT_POLICY + ":"))]


@pytest.mark.parametrize("network", [(), CLUSTER_NETWORK], ids=["offline", "cluster"])
@pytest.mark.parametrize("operation", ["CREATE", "UPDATE"])
def test_every_rendered_network_policy_is_admitted(dev_policy_docs, network, operation):
    docs = _tenant_render(PERSON, "--fqdn-network-policy", *network)
    namespace = _tenant_namespace(docs)
    checked = [d for d in docs if d["kind"] in ("NetworkPolicy", "FQDNNetworkPolicy")]
    assert len(checked) == 9, "the control: eight network policies and the hostname policy"
    for doc in checked:
        assert _refusals(dev_policy_docs, operation, doc, old=doc, namespace_object=namespace) == [], (
            doc["metadata"]["name"]
        )


def _rendered(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return json.loads(json.dumps(_one(docs, kind, name)))


def _fqdn_mutations() -> list[tuple[str, Any]]:
    def selector(spec: dict[str, Any]) -> None:
        spec["podSelector"] = {"matchLabels": {"swarm-tenant": PERSON}}

    def no_selector(spec: dict[str, Any]) -> None:
        del spec["podSelector"]

    def host(spec: dict[str, Any]) -> None:
        spec["egress"][0]["matches"][0]["name"] = "exfil.example.net"

    def pattern(spec: dict[str, Any]) -> None:
        spec["egress"][0]["matches"].append({"pattern": "*.example.net"})

    def port(spec: dict[str, Any]) -> None:
        spec["egress"][0]["ports"][0]["port"] = 22

    def protocol(spec: dict[str, Any]) -> None:
        spec["egress"][0]["ports"][0]["protocol"] = "UDP"

    def all_ports(spec: dict[str, Any]) -> None:
        del spec["egress"][0]["ports"]

    def second_rule(spec: dict[str, Any]) -> None:
        spec["egress"].append({"matches": [{"name": "exfil.example.net"}], "ports": [{"protocol": "TCP", "port": 443}]})

    return [(f.__name__, f) for f in (selector, no_selector, host, pattern, port, protocol, all_ports, second_rule)]


@pytest.mark.parametrize("mutation", [m for _, m in _fqdn_mutations()], ids=[n for n, _ in _fqdn_mutations()])
def test_the_fqdn_policy_with_other_content_is_refused(dev_policy_docs, tenant_docs, mutation):
    namespace = _tenant_namespace(tenant_docs)
    doc = _rendered(tenant_docs, "FQDNNetworkPolicy", FQDN_POLICY)
    assert _content_refusals(dev_policy_docs, doc, namespace) == [], "the control"
    mutation(doc["spec"])
    for operation in ("CREATE", "UPDATE"):
        refusals = _content_refusals(dev_policy_docs, doc, namespace, operation)
        assert [r.split(":")[0] for r in refusals] == [FQDN_CONTENT_POLICY], (operation, refusals)


def _netpol_mutations() -> list[tuple[str, str, Any]]:
    def other_selector(spec: dict[str, Any]) -> None:
        spec["podSelector"] = {"matchLabels": {"swarm-live": "disabled"}}

    def every_pod(spec: dict[str, Any]) -> None:
        spec["podSelector"] = {}

    def selector_expression(spec: dict[str, Any]) -> None:
        spec["podSelector"]["matchExpressions"] = [{"key": "a", "operator": "Exists"}]

    def other_port(spec: dict[str, Any]) -> None:
        spec["egress"][0]["ports"][0]["port"] = 22

    def end_port(spec: dict[str, Any]) -> None:
        spec["egress"][0]["ports"][0]["endPort"] = 65535

    def all_ports(spec: dict[str, Any]) -> None:
        del spec["egress"][0]["ports"]

    def other_peer(spec: dict[str, Any]) -> None:
        spec["egress"][0]["to"][0]["podSelector"] = {"matchLabels": {"app.kubernetes.io/name": "swarm-api"}}

    def any_namespace(spec: dict[str, Any]) -> None:
        spec["egress"][0]["to"][0]["namespaceSelector"] = {}

    def no_namespace_selector(spec: dict[str, Any]) -> None:
        del spec["egress"][0]["to"][0]["namespaceSelector"]

    def extra_peer(spec: dict[str, Any]) -> None:
        spec["egress"][0]["to"].append({"ipBlock": {"cidr": "0.0.0.0/0"}})

    def extra_rule(spec: dict[str, Any]) -> None:
        spec["egress"].append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"protocol": "TCP", "port": 443}]})

    def ingress(spec: dict[str, Any]) -> None:
        spec["policyTypes"] = ["Ingress", "Egress"]
        spec["ingress"] = [{"from": [{"namespaceSelector": {}}]}]

    def no_egress_type(spec: dict[str, Any]) -> None:
        spec["policyTypes"] = ["Ingress"]

    def wider_block(spec: dict[str, Any]) -> None:
        spec["egress"][3]["to"][0]["ipBlock"]["cidr"] = "0.0.0.0/0"

    def dropped_except(spec: dict[str, Any]) -> None:
        spec["egress"][4]["to"][0]["ipBlock"]["except"].remove("10.0.0.0/8")

    def except_to_host(spec: dict[str, Any]) -> None:
        excepts = spec["egress"][4]["to"][0]["ipBlock"]["except"]
        excepts[excepts.index("10.0.0.0/8")] = "10.0.0.1/32"

    def dns_range(spec: dict[str, Any]) -> None:
        spec["egress"][1]["to"][0]["ipBlock"]["cidr"] = "0.0.0.0/0"

    def deny_opened(spec: dict[str, Any]) -> None:
        spec["egress"] = [{}]

    return [
        ("swarm-allow-live-egress", "other_selector", other_selector),
        ("swarm-allow-typesafe-fwd", "every_pod", every_pod),
        ("swarm-allow-judge-egress", "selector_expression", selector_expression),
        ("swarm-allow-judge-egress", "other_port", other_port),
        ("swarm-allow-egress-proxy", "end_port", end_port),
        ("swarm-allow-graph-service", "all_ports", all_ports),
        ("swarm-allow-typesafe-fwd", "other_peer", other_peer),
        ("swarm-allow-live-egress", "any_namespace", any_namespace),
        ("swarm-allow-egress-proxy", "no_namespace_selector", no_namespace_selector),
        ("swarm-allow-graph-service", "extra_peer", extra_peer),
        ("swarm-allow-worker-egress", "extra_rule", extra_rule),
        ("swarm-deny-cross-tenant-ingress", "ingress", ingress),
        ("swarm-default-deny", "no_egress_type", no_egress_type),
        ("swarm-allow-worker-egress", "wider_block", wider_block),
        ("swarm-allow-worker-egress", "dropped_except", dropped_except),
        ("swarm-allow-worker-egress", "except_to_host", except_to_host),
        ("swarm-allow-worker-egress", "dns_range", dns_range),
        ("swarm-default-deny", "deny_opened", deny_opened),
    ]


@pytest.mark.parametrize(
    ("name", "mutation"), [(n, m) for n, _, m in _netpol_mutations()], ids=[i for _, i, _ in _netpol_mutations()]
)
def test_a_network_policy_with_other_content_is_refused(dev_policy_docs, tenant_docs, name, mutation):
    namespace = _tenant_namespace(tenant_docs)
    doc = _rendered(tenant_docs, "NetworkPolicy", name)
    assert _content_refusals(dev_policy_docs, doc, namespace) == [], "the control"
    mutation(doc["spec"])
    for operation in ("CREATE", "UPDATE"):
        refusals = _content_refusals(dev_policy_docs, doc, namespace, operation)
        assert [r.split(":")[0] for r in refusals] == [NETPOL_POLICY], (operation, refusals)


def test_one_policys_content_under_another_name_is_refused(dev_policy_docs, tenant_docs):
    """The content is held per name: the worker egress spec renamed to the live
    path's name opens the internet to the live pods."""
    namespace = _tenant_namespace(tenant_docs)
    doc = _rendered(tenant_docs, "NetworkPolicy", "swarm-allow-worker-egress")
    doc["metadata"]["name"] = "swarm-allow-live-egress"
    assert [r.split(":")[0] for r in _content_refusals(dev_policy_docs, doc, namespace)] == [NETPOL_POLICY]


def test_the_content_policies_judge_nobody_else_and_no_other_namespace(dev_policy_docs, tenant_docs):
    """Another caller's write is not evaluated; nor is the deployer's outside a
    swarm-tenant-* namespace (the scope policy refuses that one by itself)."""
    namespace = _tenant_namespace(tenant_docs)
    doc = _rendered(tenant_docs, "NetworkPolicy", "swarm-allow-live-egress")
    doc["spec"]["podSelector"] = {}
    assert _content_refusals(dev_policy_docs, doc, namespace, user="system:admin") == []
    doc["metadata"]["namespace"] = "kube-system"
    other = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "kube-system", "labels": {}}}
    assert _content_refusals(dev_policy_docs, doc, other) == []
    assert _refusals(dev_policy_docs, "CREATE", doc, namespace_object=other), "the scope policy still refuses it"
