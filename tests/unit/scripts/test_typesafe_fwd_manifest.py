"""The TypeSafe forwarder's Kubernetes objects (docs/design/workspace-secrets.md
§3.5; lane SEC-INJECT-A, part of #1347).

  * kubernetes/swarm-system/swarm-typesafe-fwd.yaml: everything in swarm-system,
    one replica, requests == limits (invariant 7), no Spot (invariant 6), the
    restricted pod standard, no Kubernetes RBAC, no Secret and NO KEY: the pod
    is given a Secret Manager version reference and names, nothing else;
  * its ingress: TCP 8080 from a pod labelled `swarm-typesafe: inject` in a
    tenant namespace, one peer with both selectors, and from nothing else;
  * the placeholders are exactly the ones terraform/infra/typesafe_fwd.tf
    fills, and the setting it renders has exactly the keys §3.5 specifies for
    the forwarder (an unknown key there fails closed).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "kubernetes" / "swarm-system" / "swarm-typesafe-fwd.yaml"
FWD_TF = REPO / "terraform" / "infra" / "typesafe_fwd.tf"
DESIGN = REPO / "docs" / "design" / "workspace-secrets.md"
PORT = 8080
POD_LABEL = {"swarm-typesafe": "inject"}
NAME = "swarm-typesafe-fwd"
#: The forwarder's setting, as design §3.5 specifies it for SEC-INJECT-B.
CONFIG_KEYS = ("enabled", "tenants", "requests_per_minute", "max_request_bytes",
               "max_response_seconds")


def _rendered() -> list[dict[str, Any]]:
    text = TEMPLATE.read_text()
    for placeholder in set(re.findall(r"__[A-Z0-9_]+__", text)):
        text = text.replace(placeholder, "placeholder-value")
    return [d for d in yaml.safe_load_all(text) if d]


def _kind(docs, kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def test_everything_is_in_swarm_system_labelled_and_holds_no_rbac_or_secret():
    docs = _rendered()
    assert {d["kind"] for d in docs} == {"Namespace", "ServiceAccount", "NetworkPolicy",
                                         "ConfigMap", "Service", "Deployment"}
    for doc in docs:
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "swarm-system", doc["kind"]  # namespace-prefix-exempt: the platform namespace, not a tenant
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform"


def test_one_replica_requests_equal_limits_no_spot_restricted():
    (deploy,) = _kind(_rendered(), "Deployment")
    assert deploy["spec"]["replicas"] == 1
    assert deploy["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    pod = deploy["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == NAME
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert "nodeSelector" not in pod and "tolerations" not in pod and "affinity" not in pod
    assert "spot" not in TEMPLATE.read_text().lower()
    containers = pod["containers"] + pod.get("initContainers", [])
    assert containers
    for container in containers:
        resources = container["resources"]
        assert resources["requests"] == resources["limits"], container["name"]
        assert set(resources["requests"]) >= {"cpu", "memory"}
        ctx = container["securityContext"]
        assert ctx["allowPrivilegeEscalation"] is False
        assert ctx["readOnlyRootFilesystem"] is True
        assert ctx["capabilities"]["drop"] == ["ALL"]


def test_the_requests_equal_limits_check_can_fail():
    """The control: a burstable container is caught by the comparison above."""
    (deploy,) = _kind(_rendered(), "Deployment")
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    container["resources"]["limits"]["cpu"] = "500m"
    assert container["resources"]["requests"] != container["resources"]["limits"]


def test_only_a_labelled_tenant_pod_reaches_it_on_8080():
    (policy,) = _kind(_rendered(), "NetworkPolicy")
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": NAME}}
    assert spec["policyTypes"] == ["Ingress"]
    (rule,) = spec["ingress"]
    (peer,) = rule["from"]
    # ONE peer with BOTH selectors: two peers would be an OR, admitting every
    # pod of a tenant namespace or a labelled pod anywhere.
    assert set(peer) == {"namespaceSelector", "podSelector"}
    assert peer["podSelector"] == {"matchLabels": POD_LABEL}
    ns = peer["namespaceSelector"]
    assert ns["matchLabels"] == {"app.kubernetes.io/part-of": "swarm"}
    assert ns["matchExpressions"] == [{"key": "swarm-tenant", "operator": "Exists"}]
    assert rule["ports"] == [{"protocol": "TCP", "port": PORT}]
    (service,) = _kind(_rendered(), "Service")
    assert [p["port"] for p in service["spec"]["ports"]] == [PORT]
    (deploy,) = _kind(_rendered(), "Deployment")
    (container,) = deploy["spec"]["template"]["spec"]["containers"]
    assert [p["containerPort"] for p in container["ports"]] == [PORT]


def test_the_pod_is_given_references_and_names_never_a_key():
    (deploy,) = _kind(_rendered(), "Deployment")
    (container,) = deploy["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in container["env"]}
    assert set(env) == {"SWARM_TYPESAFE_FWD_KEY_SECRET", "SWARM_TYPESAFE_FWD_PROJECT",
                        "SWARM_TYPESAFE_FWD_CONFIG"}
    assert all("valueFrom" not in e for e in env.values())
    assert "envFrom" not in container
    text = TEMPLATE.read_text()
    assert "TYPESAFE_API_KEY" not in text and "secretKeyRef" not in text


def test_the_placeholders_are_the_ones_terraform_fills():
    placeholders = set(re.findall(r"__[A-Z0-9_]+__", TEMPLATE.read_text()))
    assert placeholders == {"__SWARM_TYPESAFE_FWD_IMAGE__", "__TYPESAFE_FWD_KEY_VERSION__",
                            "__PROJECT_ID__", "__TYPESAFE_FWD_CONFIG_JSON__",
                            "__TYPESAFE_FWD_CONFIG_SHA256__"}
    tf = FWD_TF.read_text()
    for placeholder in placeholders:
        assert f'"{placeholder}"' in tf, placeholder
    assert 'kubernetes/swarm-system/swarm-typesafe-fwd.yaml' in tf


def test_the_rendered_setting_has_exactly_the_specified_keys():
    tf = FWD_TF.read_text()
    block = re.search(r"typesafe_fwd_config = \{\n(.*?)\n  \}", tf, re.S)
    assert block, "typesafe_fwd.tf renders local.typesafe_fwd_config"
    keys = tuple(re.findall(r"^\s+([a-z_]+)\s+=", block.group(1), re.M))
    assert keys == CONFIG_KEYS
    section = DESIGN.read_text().split("### 3.5", 1)[1].split("### 3.6", 1)[0]
    for key in CONFIG_KEYS:
        assert f"`{key}`" in section, key
