"""The shared graph service's Kubernetes objects (docs/design/graph-service.md
§2.1, §3.4; lane GS-INFRA, part of #1384).

  * kubernetes/swarm-system/swarm-graph.yaml: everything in swarm-system, two
    replicas that never all go at once (maxUnavailable 0, a PDB of one),
    requests == limits at §2.1's size (invariant 7), no Spot (invariant 6), the
    restricted pod standard, no Kubernetes RBAC, no Secret and NO KEY: the pod
    is given a Secret Manager version reference and the bucket name, and reads
    exactly the variables apps/swarm-graph/swarm_graph/main.py reads;
  * its ingress: TCP 8080 from any pod of a tenant namespace (§2.1: any agent
    pod may hold a graph ticket), one peer, and from nothing else; TCP 8081,
    the stats port, from swarm-api's Direct VPC egress range and from the
    swarm-graph pods themselves (a replica reads its peers' /v1/stats/local,
    BGD 2026-10-28), and from nothing else (docs/design/graph-answer-cache.md
    §5.3; lane CACHE-2-NETWORK). kubernetes/network_parity.py judges the
    policy with the same function the tests use;
  * swarm-api's way in: the internal load balancer `swarm-graph-stats` on the
    reserved address graph_service.tf renders, 8081 only;
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
STATS_PORT = 8081
STATS_PY = REPO / "apps" / "swarm-graph" / "swarm_graph" / "stats.py"
#: A swarm-api range and a load balancer address that appear nowhere else, so
#: a rendered value that matches them can only have come from the render.
FAKE_API_CIDR = "10.123.16.0/20"
FAKE_STATS_ADDRESS = "10.123.16.81"
GRAPH_POD_PEER = {"podSelector": {"matchLabels": APP}}
TENANT_PEER = {
    "namespaceSelector": {
        "matchLabels": {"app.kubernetes.io/part-of": "swarm"},
        "matchExpressions": [{"key": "swarm-tenant", "operator": "Exists"}],
    }
}


def _constant(path: Path, name: str) -> str:
    found = re.search(rf"^{name}\s*=\s*(.+)$", path.read_text(), re.M)
    assert found, f"{name} moved out of {path}"
    return found.group(1).strip()


def _rendered() -> list[dict[str, Any]]:
    text = TEMPLATE.read_text()
    text = text.replace("__GRAPH_CONFIG_JSON__", '{"enabled":false}')
    text = text.replace("__SWARM_API_EGRESS_CIDR__", FAKE_API_CIDR)
    text = text.replace("__GRAPH_STATS_ADDRESS__", FAKE_STATS_ADDRESS)
    for placeholder in set(re.findall(r"__[A-Z0-9_]+__", text)):
        text = text.replace(placeholder, "placeholder-value")
    return [d for d in yaml.safe_load_all(text) if d]


def _kind(docs, kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def _deployment() -> dict[str, Any]:
    (deploy,) = _kind(_rendered(), "Deployment")
    return deploy


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_module() -> Any:
    return _load("swarm_k8s_render_graph", KUBERNETES / "render.py")


def _parity() -> Any:
    return _load("swarm_k8s_parity_graph", KUBERNETES / "network_parity.py")


def _policy() -> dict[str, Any]:
    (policy,) = _kind(_rendered(), "NetworkPolicy")
    return policy


def _admitted(policy: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """Port -> every peer the policy admits to it. A rule with no ports or no
    peers would admit every port or every peer; this names that as None."""
    admitted: dict[int, list[dict[str, Any]]] = {}
    for rule in policy["spec"]["ingress"]:
        assert rule.get("ports"), f"a rule with no ports admits every port: {rule}"
        assert rule.get("from"), f"a rule with no peer admits every peer: {rule}"
        for port in rule["ports"]:
            assert port.get("protocol") == "TCP" and "endPort" not in port, port
            admitted.setdefault(port["port"], []).extend(rule["from"])
    return admitted


def _services() -> dict[str, dict[str, Any]]:
    return {s["metadata"]["name"]: s for s in _kind(_rendered(), "Service")}


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
    policy = _policy()
    assert policy["metadata"]["name"] == "swarm-graph-ingress"
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": APP}
    assert spec["policyTypes"] == ["Ingress"]
    # The tenant rule, exactly as it was: any pod of a tenant namespace (§2.1),
    # and only a tenant namespace -- a namespace selector alone, with both the
    # part-of label and swarm-tenant -- on 8080 and never 8081.
    tenant_rule = spec["ingress"][0]
    assert tenant_rule == {"from": [TENANT_PEER], "ports": [{"protocol": "TCP", "port": PORT}]}
    assert _admitted(policy)[PORT] == [TENANT_PEER]
    services = _services()
    service = services[NAME]
    assert service["spec"]["clusterIP"] == "None"
    assert service["spec"]["selector"] == APP
    assert [p["port"] for p in service["spec"]["ports"]] == [PORT]
    assert _constant(SERVER_PY, "PORT") == str(PORT)


def test_8081_is_admitted_only_from_swarm_api_and_the_graph_pods():
    """§5.3 and BGD's 2026-10-28 rule: swarm-api's Direct VPC egress range for
    GET /v1/stats, and the swarm-graph pods of this namespace for the peer
    fan-out to /v1/stats/local. Neither reaches 8080; no tenant reaches 8081."""
    admitted = _admitted(_policy())
    assert set(admitted) == {PORT, STATS_PORT}
    assert sorted(admitted[STATS_PORT], key=str) == sorted(
        [{"ipBlock": {"cidr": FAKE_API_CIDR}}, GRAPH_POD_PEER], key=str)
    # A bare podSelector is this namespace's pods; a namespaceSelector beside
    # it would widen the peer to other namespaces.
    for peer in admitted[STATS_PORT]:
        assert "namespaceSelector" not in peer, peer
        assert peer.get("ipBlock", {}).get("except") is None, peer
    assert TENANT_PEER not in admitted[STATS_PORT]
    assert {"ipBlock": {"cidr": FAKE_API_CIDR}} not in admitted[PORT]
    assert GRAPH_POD_PEER not in admitted[PORT]
    assert _constant(STATS_PY, "STATS_PORT") == str(STATS_PORT)
    # The swarm-api range is the one swarm-live-ingress already admits: the
    # same placeholder, filled by terraform from the same local.
    assert "cidr: __SWARM_API_EGRESS_CIDR__" in TEMPLATE.read_text()


def test_the_container_declares_both_ports():
    (container,) = _deployment()["spec"]["template"]["spec"]["containers"]
    assert [(p["name"], p["containerPort"]) for p in container["ports"]] == [
        ("http", PORT), ("stats", STATS_PORT)]


def test_the_stats_service_is_internal_and_exposes_8081_only():
    services = _services()
    assert set(services) == {NAME, "swarm-graph-stats"}
    stats = services["swarm-graph-stats"]
    assert stats["metadata"]["annotations"] == {"networking.gke.io/load-balancer-type": "Internal"}
    spec = stats["spec"]
    assert spec["type"] == "LoadBalancer"
    assert spec["selector"] == APP
    assert spec["loadBalancerIP"] == FAKE_STATS_ADDRESS
    assert [(p["protocol"], p["port"], p["targetPort"]) for p in spec["ports"]] == [
        ("TCP", STATS_PORT, STATS_PORT)]
    # The node, not the pod, is health-checked (as swarm-live-internal), so
    # the ingress policy needs no health-check range.
    assert spec["externalTrafficPolicy"] == "Cluster"
    assert "loadBalancerSourceRanges" not in spec


def test_the_parity_check_passes_the_rendered_policy():
    assert _parity().check_graph_ingress(_policy(), FAKE_API_CIDR) == []


@pytest.mark.parametrize("widen", [
    "tenant_gains_8081", "api_gains_8080", "pods_gain_8080", "peer_in_any_namespace",
    "api_range_differs", "pod_rule_dropped", "api_rule_dropped", "rule_with_no_ports",
])
def test_the_parity_check_refuses_every_widening_and_every_missing_rule(widen):
    policy = _policy()
    rules = policy["spec"]["ingress"]
    stats_port = {"protocol": "TCP", "port": STATS_PORT}
    api = {"ipBlock": {"cidr": FAKE_API_CIDR}}
    if widen == "tenant_gains_8081":
        rules[0]["ports"].append(stats_port)
    elif widen == "api_gains_8080":
        rules.append({"from": [api], "ports": [{"protocol": "TCP", "port": PORT}]})
    elif widen == "pods_gain_8080":
        rules[0]["from"].append(GRAPH_POD_PEER)
    elif widen == "peer_in_any_namespace":
        for rule in rules:
            for peer in rule["from"]:
                if peer == GRAPH_POD_PEER:
                    peer["namespaceSelector"] = {}
    elif widen == "api_range_differs":
        for rule in rules:
            for peer in rule["from"]:
                if "ipBlock" in peer:
                    peer["ipBlock"]["cidr"] = "10.0.0.0/8"
    elif widen == "pod_rule_dropped":
        policy["spec"]["ingress"] = [r for r in rules if GRAPH_POD_PEER not in r["from"]]
    elif widen == "api_rule_dropped":
        policy["spec"]["ingress"] = [r for r in rules if api not in r["from"]]
    elif widen == "rule_with_no_ports":
        rules.append({"from": [GRAPH_POD_PEER]})
    assert _parity().check_graph_ingress(policy, FAKE_API_CIDR), widen


def test_the_parity_check_runs_against_swarm_systems_policies(tmp_path, capsys):
    """`network_parity.py --graph-ingress`: swarm-graph-ingress as the API
    server holds it, against swarm-api's live range; a missing policy fails."""
    import json
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"swarm_api_egress_cidr": FAKE_API_CIDR, "source": "test"}))
    policies = tmp_path / "policies.json"
    policies.write_text(json.dumps({"items": [_policy()]}))
    parity = _parity()
    assert parity.main(["--graph-ingress", "--live", str(live), "--policies", str(policies)]) == 0
    assert "ok swarm-system/swarm-graph-ingress" in capsys.readouterr().out
    policies.write_text(json.dumps({"items": []}))
    assert parity.main(["--graph-ingress", "--live", str(live), "--policies", str(policies)]) == 1
    assert "swarm-system/swarm-graph-ingress does not exist" in capsys.readouterr().out


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
                            "__GRAPH_CONFIG_SHA256__", "__SWARM_API_EGRESS_CIDR__",
                            "__GRAPH_STATS_ADDRESS__"}
    tf = GRAPH_TF.read_text()
    for placeholder in placeholders:
        assert f'"{placeholder}"' in tf, placeholder
    assert "kubernetes/swarm-system/swarm-graph.yaml" in tf


def test_swarm_api_reads_the_ticket_key_through_the_one_merge():
    text = LOCALS_TF.read_text()
    found = re.search(r"^\s*api_secret_env\s*=\s*merge\(([^)]*)\)", text, re.M)
    assert found, "locals.tf merges swarm-api's secret references in api_secret_env"
    assert "local.graph_ticket_key_secret_env" in found.group(1)


def test_swarm_api_reads_the_stats_url_through_its_env_merge():
    """SWARM_GRAPH_STATS_URL reaches swarm-api through graph_service.tf's
    local, merged into service_env["swarm-api"] like the other switches."""
    text = LOCALS_TF.read_text()
    found = re.search(r'^\s*"swarm-api"\s*=\s*merge\(([^{]*)\{', text, re.M)
    assert found, 'locals.tf builds service_env["swarm-api"] with one merge'
    assert "local.api_graph_stats_env" in found.group(1)
    tf = GRAPH_TF.read_text()
    assert re.search(r'SWARM_GRAPH_STATS_URL\s*=\s*local\.graph_stats_serving \? '
                     r'"http://\$\{google_compute_address\.graph_stats\[0\]\.address\}:8081"', tf)


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
    # Only the TypeSafe forwarder's tenant policy (lane TS-MIGRATE-POD)
    # follows it, appended the same way.
    assert files[-2:] == (TENANT_POLICY_FILE, "network-policies/allow-typesafe-fwd.yaml")
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
