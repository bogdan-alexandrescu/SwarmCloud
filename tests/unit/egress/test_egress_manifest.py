"""swarm-egress's Kubernetes objects and the one path to it
(docs/browser-egress.md §2.5, §3.1, §3.6; lane EGRESS-SVC, part of #1030).

  * kubernetes/swarm-system/swarm-egress.yaml: everything in swarm-system, one
    replica, requests == limits (invariant 7), no Spot (invariant 6), the
    restricted pod standard, no Kubernetes RBAC, and NO credential: the pod
    is given Secret Manager version references and nothing else;
  * its ingress: TCP 3128 from a pod labelled `swarm-egress: proxy` in a tenant
    namespace, one peer with both selectors, and from nothing else;
  * the tenant side, network-policies/allow-egress-proxy.yaml: only a pod
    with that label may open TCP 3128, only to the swarm-egress pods in
    swarm-system; and it is in render.py's TENANT_FILES, so it reaches every
    tenant namespace with the rest of the set;
  * the configuration terraform renders is the one swarm_egress.config reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "kubernetes" / "swarm-system" / "swarm-egress.yaml"
TENANT_POLICY = REPO / "kubernetes" / "network-policies" / "allow-egress-proxy.yaml"
EGRESS_TF = REPO / "terraform" / "infra" / "egress.tf"
PORT = 3128
POD_LABEL = {"swarm-egress": "proxy"}


def _rendered() -> list[dict[str, Any]]:
    text = TEMPLATE.read_text()
    for placeholder in set(re.findall(r"__[A-Z0-9_]+__", text)):
        text = text.replace(placeholder, "placeholder-value")
    return [d for d in yaml.safe_load_all(text) if d]


def _kind(docs, kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def test_everything_is_in_swarm_system_and_labelled():
    docs = _rendered()
    assert {d["kind"] for d in docs} >= {"ServiceAccount", "ConfigMap", "Service",
                                         "StatefulSet", "NetworkPolicy"}
    for doc in docs:
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "swarm-system", doc["kind"]  # namespace-prefix-exempt: the platform namespace, not a tenant
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    assert not [d for d in docs if d["kind"] in ("Role", "RoleBinding", "ClusterRole",
                                                  "ClusterRoleBinding", "Secret")]


def test_one_replica_requests_equal_limits_no_spot_restricted():
    (sts,) = _kind(_rendered(), "StatefulSet")
    assert sts["spec"]["replicas"] == 1
    pod = sts["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "swarm-egress"
    assert pod["automountServiceAccountToken"] is False
    assert "spot" not in yaml.safe_dump(pod).lower()
    assert pod["securityContext"]["runAsNonRoot"] is True
    for container in pod["containers"]:
        resources = container["resources"]
        assert resources["requests"] == resources["limits"], container["name"]
        sc = container["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]


def test_the_pod_holds_references_never_a_credential():
    (sts,) = _kind(_rendered(), "StatefulSet")
    (container,) = sts["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in container["env"]}
    for name in ("SWARM_EGRESS_KEY_SECRET", "SWARM_EGRESS_CREDENTIAL_SECRET"):
        assert "value" in env[name] and "valueFrom" not in env[name]
    template = TEMPLATE.read_text()
    assert "__EGRESS_KEY_VERSION__" in template and "__EGRESS_CREDENTIAL_VERSION__" in template
    assert "password" not in template.lower().replace("no password", "")
    assert not [v for v in sts["spec"]["template"]["spec"].get("volumes", [])
                if "secret" in v]


def test_only_a_labelled_tenant_browser_pod_reaches_it_on_3128():
    policies = [p for p in _kind(_rendered(), "NetworkPolicy")
                if p["spec"]["podSelector"].get("matchLabels") == {
                    "app.kubernetes.io/name": "swarm-egress"}]
    assert len(policies) == 1
    (policy,) = policies
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": PORT}]
    (peer,) = rule["from"]  # ONE peer: both selectors ANDed
    assert peer["podSelector"]["matchLabels"] == POD_LABEL
    ns = peer["namespaceSelector"]
    assert ns["matchLabels"] == {"app.kubernetes.io/part-of": "swarm"}
    assert ns["matchExpressions"] == [{"key": "swarm-tenant", "operator": "Exists"}]
    assert "ipBlock" not in yaml.safe_dump(policy)


def test_the_service_listens_on_3128_and_nothing_else():
    (svc,) = [s for s in _kind(_rendered(), "Service") if s["metadata"]["name"] == "swarm-egress"]
    assert [p["port"] for p in svc["spec"]["ports"]] == [PORT]
    assert svc["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_the_tenant_side_opens_3128_to_swarm_egress_only_from_a_labelled_pod():
    text = TENANT_POLICY.read_text().replace("__NAMESPACE__", "swarm-tenant-eng").replace(
        "__TENANT_ID__", "eng")
    (policy,) = [d for d in yaml.safe_load_all(text) if d]
    assert policy["metadata"]["name"] == "swarm-allow-egress-proxy"
    assert policy["spec"]["podSelector"] == {"matchLabels": POD_LABEL}
    assert policy["spec"]["policyTypes"] == ["Egress"]
    (rule,) = policy["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": PORT}]
    (peer,) = rule["to"]
    assert peer["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "swarm-system"}}
    assert peer["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "swarm-egress"}}


def test_the_tenant_policy_reaches_every_tenant_namespace():
    import importlib.util

    spec = importlib.util.spec_from_file_location("swarm_k8s_render_egress",
                                                  REPO / "kubernetes" / "render.py")
    assert spec and spec.loader
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    assert "network-policies/allow-egress-proxy.yaml" in render.TENANT_FILES


def test_the_port_is_the_servers():
    from swarm_egress.main import PORT as SERVER_PORT

    assert SERVER_PORT == PORT


def test_terraform_renders_the_configuration_swarm_egress_reads():
    """egress.tf writes the ConfigMap's JSON with exactly the keys
    swarm_egress.config accepts; an extra key would make it fail closed."""
    from swarm_egress.config import CONFIG_KEYS

    tf = EGRESS_TF.read_text()
    block = tf[tf.index("egress_config = {"):]
    block = block[:block.index("\n  }\n")]
    keys = set(re.findall(r"^\s{4}([a-z_]+)\s*=", block, re.M))
    assert keys == set(CONFIG_KEYS)


def test_the_workspace_deployer_may_write_the_tenant_policy():
    """A personal workspace's namespace is written by the workspace deployer,
    whose admission policy names every object the tenant render holds."""
    text = (REPO / "kubernetes" / "policies" / "workspace-provisioner-scope.yaml").read_text()
    assert '"swarm-allow-egress-proxy"' in text
