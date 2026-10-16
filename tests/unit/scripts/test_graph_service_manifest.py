"""The shared graph service's Kubernetes objects (docs/design/graph-service.md
§2.1, §3.4; lane GS-INFRA, part of #1384).

  * kubernetes/swarm-system/swarm-graph.yaml: everything in swarm-system, two
    replicas that never all go at once (maxUnavailable 0, a PDB of one),
    requests == limits at §2.1's size (invariant 7), no Spot (invariant 6), the
    restricted pod standard, no Kubernetes RBAC, no Secret and NO KEY: the pod
    is given a Secret Manager version reference and the bucket name, and reads
    exactly the variables apps/swarm-graph/swarm_graph/main.py reads;
  * its ingress: TCP 8080 from any pod of a tenant namespace (§2.1: any agent
    pod may hold a graph ticket), one peer, and from nothing else;
  * the kill switch: a ConfigMap whose rendered setting has exactly the keys
    swarm_graph.main.CONFIG_KEYS accepts, mounted as a directory (never
    subPath) at the path main.py defaults to, and shipped OFF;
  * the tenant side, network-policies/allow-graph-service.yaml: every pod of a
    tenant namespace may reach the swarm-graph pods on 8080, and nothing else,
    and render.py puts it in every tenant namespace;
  * the placeholders are exactly the ones terraform/infra/graph_service.tf
    fills.

Offline: the template is read, never applied.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
TEMPLATE = KUBERNETES / "swarm-system" / "swarm-graph.yaml"
TENANT_POLICY = KUBERNETES / "network-policies" / "allow-graph-service.yaml"
TENANT_POLICY_FILE = "network-policies/allow-graph-service.yaml"
GRAPH_TF = REPO / "terraform" / "infra" / "graph_service.tf"
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"
MAIN_PY = REPO / "apps" / "swarm-graph" / "swarm_graph" / "main.py"
SERVER_PY = REPO / "apps" / "swarm-graph" / "swarm_graph" / "server.py"
CACHE_PY = REPO / "apps" / "swarm-graph" / "swarm_graph" / "cache.py"
NAME = "swarm-graph"
APP = {"app.kubernetes.io/name": NAME}
PORT = 8080


def _constant(path: Path, name: str) -> str:
    found = re.search(rf"^{name}\s*=\s*(.+)$", path.read_text(), re.M)
    assert found, f"{name} moved out of {path}"
    return found.group(1).strip()


def _rendered() -> list[dict[str, Any]]:
    text = TEMPLATE.read_text()
    text = text.replace("__GRAPH_CONFIG_JSON__", '{"enabled":false}')
    for placeholder in set(re.findall(r"__[A-Z0-9_]+__", text)):
        text = text.replace(placeholder, "placeholder-value")
    return [d for d in yaml.safe_load_all(text) if d]


def _kind(docs, kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def _deployment() -> dict[str, Any]:
    (deploy,) = _kind(_rendered(), "Deployment")
    return deploy


def _render_module() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_graph", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- swarm-system ------------------------------------------------------------


def test_everything_is_in_swarm_system_labelled_and_holds_no_rbac_or_secret():
    docs = _rendered()
    assert {d["kind"] for d in docs} == {"Namespace", "ServiceAccount", "NetworkPolicy",
                                         "ConfigMap", "Service", "Deployment",
                                         "PodDisruptionBudget"}
    for doc in docs:
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "swarm-system", doc["kind"]  # namespace-prefix-exempt: the platform namespace, not a tenant
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform", doc["kind"]
    (ns,) = _kind(docs, "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    (sa,) = _kind(docs, "ServiceAccount")
    assert sa["metadata"]["name"] == NAME
    assert sa["automountServiceAccountToken"] is False


def test_two_replicas_never_all_unavailable():
    deploy = _deployment()
    assert deploy["spec"]["replicas"] == 2
    rolling = deploy["spec"]["strategy"]["rollingUpdate"]
    assert rolling == {"maxSurge": 1, "maxUnavailable": 0}
    (pdb,) = _kind(_rendered(), "PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"] == {"matchLabels": APP}
    assert deploy["spec"]["selector"] == {"matchLabels": APP}


def test_requests_equal_limits_at_the_designed_size_no_spot_restricted():
    pod = _deployment()["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == NAME
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    ctx = pod["securityContext"]
    assert ctx["runAsNonRoot"] is True and ctx["runAsUser"] == 10001
    assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}
    assert "nodeSelector" not in pod and "tolerations" not in pod
    assert "spot" not in TEMPLATE.read_text().lower()
    containers = pod["containers"] + pod.get("initContainers", [])
    assert len(containers) == 1
    for container in containers:
        resources = container["resources"]
        assert resources["requests"] == resources["limits"], container["name"]
        # §2.1, and the memory GS-SVC's sizing assertion holds its caches under.
        assert resources["requests"] == {"cpu": "1", "memory": "3Gi", "ephemeral-storage": "1Gi"}
        sec = container["securityContext"]
        assert sec["allowPrivilegeEscalation"] is False
        assert sec["readOnlyRootFilesystem"] is True
        assert sec["capabilities"]["drop"] == ["ALL"]
    assert _constant(CACHE_PY, "MEMORY_LIMIT_BYTES") == "3 * 1024 * MiB"


def test_the_requests_equal_limits_check_can_fail():
    """The control: a burstable container is caught by the comparison above."""
    container = _deployment()["spec"]["template"]["spec"]["containers"][0]
    container["resources"]["limits"]["memory"] = "4Gi"
    assert container["resources"]["requests"] != container["resources"]["limits"]


def test_only_a_tenant_namespace_reaches_it_on_8080():
    docs = _rendered()
    (policy,) = _kind(docs, "NetworkPolicy")
    assert policy["metadata"]["name"] == "swarm-graph-ingress"
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": APP}
    assert spec["policyTypes"] == ["Ingress"]
    (rule,) = spec["ingress"]
    (peer,) = rule["from"]
    # Any pod of a tenant namespace (§2.1), and only a tenant namespace: a
    # namespace selector alone, with both the part-of label and swarm-tenant.
    assert set(peer) == {"namespaceSelector"}
    ns = peer["namespaceSelector"]
    assert ns["matchLabels"] == {"app.kubernetes.io/part-of": "swarm"}
    assert ns["matchExpressions"] == [{"key": "swarm-tenant", "operator": "Exists"}]
    assert rule["ports"] == [{"protocol": "TCP", "port": PORT}]
    (service,) = _kind(docs, "Service")
    assert service["spec"]["clusterIP"] == "None"
    assert service["spec"]["selector"] == APP
    assert [p["port"] for p in service["spec"]["ports"]] == [PORT]
    (container,) = _deployment()["spec"]["template"]["spec"]["containers"]
    assert [p["containerPort"] for p in container["ports"]] == [PORT]
    assert _constant(SERVER_PY, "PORT") == str(PORT)


def test_the_pod_is_given_references_and_names_never_a_key():
    (container,) = _deployment()["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in container["env"]}
    # Exactly what swarm_graph.main.settings() reads.
    wanted = {
        _constant(MAIN_PY, "KEY_ENV").strip('"'),
        _constant(MAIN_PY, "BUCKET_ENV").strip('"'),
        _constant(MAIN_PY, "CONFIG_ENV").strip('"'),
    }
    assert set(env) == wanted == {"SWARM_GRAPH_KEY_SECRET", "SWARM_GRAPH_BUCKET", "SWARM_GRAPH_CONFIG"}
    assert all("valueFrom" not in e for e in env.values())
    assert "envFrom" not in container
    text = TEMPLATE.read_text()
    # The key's VERSION REFERENCE and the bucket's name, filled by terraform.
    assert re.search(r"name: SWARM_GRAPH_KEY_SECRET\n\s+value: __GRAPH_TICKET_KEY_VERSION__\n", text)
    assert re.search(r"name: SWARM_GRAPH_BUCKET\n\s+value: __GRAPH_BUCKET__\n", text)
    assert "secretKeyRef" not in text and "kind: Secret\n" not in text


def test_the_kill_switch_is_a_mounted_directory_with_exactly_its_keys():
    docs = _rendered()
    (config,) = _kind(docs, "ConfigMap")
    assert config["metadata"]["name"] == "swarm-graph-config"
    assert set(config["data"]) == {"config.json"}
    pod = _deployment()["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    env = {e["name"]: e["value"] for e in container["env"]}
    default = _constant(MAIN_PY, "DEFAULT_CONFIG").strip('"')
    assert env["SWARM_GRAPH_CONFIG"] == default == "/etc/swarm-graph/config.json"
    (mount,) = container["volumeMounts"]
    assert mount["mountPath"] == str(Path(default).parent)
    assert mount["readOnly"] is True
    # A subPath mount is never refreshed by the kubelet: the switch would need
    # a restart to take effect.
    assert "subPath" not in mount
    (volume,) = pod["volumes"]
    assert volume["name"] == mount["name"]
    assert volume["configMap"]["name"] == "swarm-graph-config"
    annotations = _deployment()["spec"]["template"]["metadata"]["annotations"]
    assert "swarm.saga.xyz/config-sha256" in annotations


def test_the_rendered_setting_has_exactly_the_keys_the_service_accepts():
    tf = GRAPH_TF.read_text()
    block = re.search(r"graph_service_config = \{\n(.*?)\n  \}", tf, re.S)
    assert block, "graph_service.tf renders local.graph_service_config"
    keys = tuple(re.findall(r"^\s+([a-z_]+)\s+=", block.group(1), re.M))
    assert keys == ("enabled",)
    assert _constant(MAIN_PY, "CONFIG_KEYS") == '("enabled",)'


def test_the_placeholders_are_the_ones_terraform_fills():
    placeholders = set(re.findall(r"__[A-Z0-9_]+__", TEMPLATE.read_text()))
    assert placeholders == {"__SWARM_GRAPH_IMAGE__", "__GRAPH_TICKET_KEY_VERSION__",
                            "__GRAPH_BUCKET__", "__GRAPH_CONFIG_JSON__",
                            "__GRAPH_CONFIG_SHA256__"}
    tf = GRAPH_TF.read_text()
    for placeholder in placeholders:
        assert f'"{placeholder}"' in tf, placeholder
    assert "kubernetes/swarm-system/swarm-graph.yaml" in tf


def test_swarm_api_reads_the_ticket_key_through_the_one_merge():
    text = LOCALS_TF.read_text()
    found = re.search(r"^\s*api_secret_env\s*=\s*merge\(([^)]*)\)", text, re.M)
    assert found, "locals.tf merges swarm-api's secret references in api_secret_env"
    assert "local.graph_ticket_key_secret_env" in found.group(1)


def test_no_secret_value_is_ever_terraform_s():
    text = GRAPH_TF.read_text()
    assert "google_secret_manager_secret_version" not in text
    assert "secret_data" not in text


# --- the tenant side -----------------------------------------------------------


def _tenant_policy() -> dict[str, Any]:
    (policy,) = [d for d in yaml.safe_load_all(TENANT_POLICY.read_text()) if d]
    return policy


def test_the_tenant_side_opens_8080_to_the_graph_service_and_nothing_else():
    policy = _tenant_policy()
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["name"] == "swarm-allow-graph-service"
    assert policy["metadata"]["namespace"] == "__NAMESPACE__"
    assert policy["metadata"]["labels"] == {"managed-by": "swarm-terraform",
                                            "swarm-tenant": "__TENANT_ID__"}
    spec = policy["spec"]
    # Every pod: any agent pod may hold a graph ticket (§2.1), so the
    # dispatcher sets no new label.
    assert spec["podSelector"] == {}
    assert spec["policyTypes"] == ["Egress"]
    assert "ingress" not in spec
    assert spec["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "swarm-system"}},
                    "podSelector": {"matchLabels": APP},
                }
            ],
            "ports": [{"protocol": "TCP", "port": PORT}],
        }
    ]


def test_the_two_sides_name_the_same_pods_and_port():
    tenant = _tenant_policy()["spec"]["egress"][0]
    (ingress,) = _kind(_rendered(), "NetworkPolicy")
    assert tenant["to"][0]["podSelector"]["matchLabels"] == ingress["spec"]["podSelector"]["matchLabels"]
    assert tenant["ports"] == ingress["spec"]["ingress"][0]["ports"]
    labels = _deployment()["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == NAME


def test_render_puts_the_tenant_side_in_every_tenant_namespace_last():
    files = _render_module().TENANT_FILES
    assert files[-1] == TENANT_POLICY_FILE
    assert files.count(TENANT_POLICY_FILE) == 1


def test_the_workspace_deployer_may_write_the_tenant_side():
    """A personal workspace is applied by the workspace deployer, whose scope
    policy names every object the tenant render holds; without the name every
    `u-*` onboarding is refused at the API server."""
    text = (KUBERNETES / "policies" / "workspace-provisioner-scope.yaml").read_text()
    assert '"swarm-allow-graph-service"' in text


@pytest.mark.parametrize("tenant", ["eng", "u-alice"])
def test_a_tenant_render_holds_the_policy_in_its_own_namespace(tenant):
    import argparse

    render = _render_module()
    parser = argparse.ArgumentParser()
    render.add_tenant_arguments(parser)
    values = render.tenant_values(parser.parse_args(["--tenant", tenant]))
    docs = [d for d in yaml.safe_load_all(render.render_files((TENANT_POLICY_FILE,), values)) if d]
    (policy,) = docs
    assert policy["metadata"]["namespace"] == values["NAMESPACE"]
    assert policy["metadata"]["labels"]["swarm-tenant"] == tenant


# --- the image the manifest names ----------------------------------------------


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_image_terraform_renders(script):
    """graph_service.tf renders swarm_graph_manifest only from an image_refs
    digest, and image_refs is written from what these two built and promoted:
    an image missing from either is a service the switch could never deploy."""
    targets = _targets(script)
    assert NAME in targets, targets
    assert targets.index(NAME) == targets.index("swarm-typesafe-fwd") + 1, targets
    assert 'lookup(var.image_refs, "swarm-graph", "")' in GRAPH_TF.read_text()
    assert (REPO / "apps" / NAME / "Dockerfile").is_file()


@pytest.mark.parametrize("command", [["tenant", "--tenant", "lint"], ["swarm-system"]])
def test_an_offline_render_with_its_warning_captured_is_still_yaml(command):
    """The worker's pre-publish checkov writes render.py's stdout AND stderr to
    the file it scans. The offline-network warning must read as a YAML comment
    there, or the whole tenant render is unparseable and checkov reports every
    job pod as having no NetworkPolicy (CKV2_K8S_6)."""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, str(KUBERNETES / "render.py"), *command],
                          cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout
    assert "render: WARNING" in proc.stdout  # the control: the warning was captured
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert docs and all(d["kind"] == "NetworkPolicy" or command[0] == "tenant" for d in docs)
    if command[0] == "tenant":
        names = {d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy"}
        assert {"swarm-default-deny", "swarm-allow-graph-service"} <= names
