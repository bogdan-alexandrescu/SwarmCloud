"""The decision-api tailnet egress: one unprivileged pod, one Service, one
destination, and only labelled pods reach it (lane JUDGE-NET; owner decisions
2026-10-14; docs/runbooks/tailscale-egress.md).

  * kubernetes/swarm-system/tailscale-egress.yaml: everything in swarm-system,
    Autopilot- and `restricted`-admissible (no privileged container, no
    capability, no TUN, no hostNetwork), USERSPACE networking, the image
    pinned by version AND digest, requests == limits (invariant 7), no Spot
    (invariant 6), no RBAC, no Secret and no credential in the file;
  * the forwarder dials exactly the tailnet name on 443, from its command line;
  * one Service, `decision-api`, on 443;
  * who reaches it: TCP 8443 from a pod labelled `swarm-judge: internal` in a
    tenant namespace -- one peer, both selectors -- in the manifest's own
    ingress policy and in the tenant side, network-policies/
    allow-judge-egress.yaml, which render.py puts in every tenant namespace;
  * kubernetes/apply.sh --swarm-system applies it after the relay and
    swarm-egress, refuses a manifest that reaches past swarm-system or carries
    RBAC, a Secret or a placeholder, and under --confirm ONLY copies the OAuth
    secret from Secret Manager into swarm-system/operator-oauth without
    printing it. A refused cluster never gets that far.

No cluster, no credentials, no network: `gcloud`, `kubectl` and `terraform`
are fakes on PATH. Every fake secret is built at runtime.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
APPLY = KUBERNETES / "apply.sh"
TEMPLATE = KUBERNETES / "swarm-system" / "tailscale-egress.yaml"
TENANT_POLICY_FILE = "network-policies/allow-judge-egress.yaml"
TENANT_POLICY = KUBERNETES / TENANT_POLICY_FILE
TAILSCALE_TF = REPO / "terraform" / "infra" / "tailscale.tf"
RELAY_TEMPLATE = KUBERNETES / "swarm-system" / "swarm-live.yaml"

TARGET = "decision-api-staging.taile0bda9.ts.net"
POD_LABEL = {"swarm-judge": "internal"}
APP = {"app.kubernetes.io/name": "decision-api-egress"}
FORWARD_PORT = 8443
SERVICE_PORT = 443
#: ghcr.io/tailscale/tailscale v1.104.1, the multi-arch index digest read from
#: ghcr.io on 2026-10-14. Assembled, not written as one literal.
IMAGE = "ghcr.io/tailscale/tailscale:v1.104.1@sha256:" + (
    "87c55d6be56ad85b3d8bceca5f87f73d" "e288c609131460dd1be1874c250cc41d"
)


def _cluster_ip() -> str:
    """terraform/infra/tailscale.tf's local.decision_api_cluster_ip: the
    Service's fixed clusterIP, stated there once."""
    (ip,) = re.findall(r'^\s*decision_api_cluster_ip\s*=\s*"([^"]+)"', TAILSCALE_TF.read_text(), re.M)
    return ip


def _rendered(text: str | None = None) -> list[dict[str, Any]]:
    """The manifest as terraform/infra/tailscale.tf renders it."""
    text = (text if text is not None else TEMPLATE.read_text()).replace("__TAILNET_FQDN__", TARGET)
    text = text.replace("__DECISION_API_CLUSTER_IP__", _cluster_ip())
    return [d for d in yaml.safe_load_all(text) if d]


def _kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def _pod(docs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    (deployment,) = _kind(docs or _rendered(), "Deployment")
    return deployment["spec"]["template"]["spec"]


def _containers(pod: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in pod["containers"] + pod.get("initContainers", [])}


# --- the manifest --------------------------------------------------------------


def test_the_template_has_two_placeholders_and_terraform_fills_both():
    assert set(re.findall(r"__[A-Z0-9_]+__", TEMPLATE.read_text())) == {
        "__TAILNET_FQDN__", "__DECISION_API_CLUSTER_IP__"}
    tf = TAILSCALE_TF.read_text()
    assert '"__TAILNET_FQDN__", var.tailscale_egress_target' in tf
    assert '"__DECISION_API_CLUSTER_IP__", local.decision_api_cluster_ip' in tf
    assert f'default     = "{TARGET}"' in tf


def test_everything_is_in_swarm_system_labelled_and_holds_no_rbac_or_secret():
    docs = _rendered()
    assert {d["kind"] for d in docs} == {"Namespace", "ServiceAccount", "NetworkPolicy", "Service", "Deployment"}
    for doc in docs:
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "swarm-system", doc["kind"]  # namespace-prefix-exempt: the platform namespace, not a tenant
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    (namespace,) = _kind(docs, "Namespace")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_the_namespace_is_labelled_like_the_other_manifests_that_create_it():
    """Three manifests create swarm-system; they must not fight over it."""
    def labels(path: Path) -> dict[str, str]:
        text = re.sub(r"__[A-Z0-9_]+__", "x", path.read_text())
        (ns,) = [d for d in yaml.safe_load_all(text) if d and d["kind"] == "Namespace"]
        return ns["metadata"]["labels"]

    ours = labels(TEMPLATE)
    assert ours == labels(KUBERNETES / "swarm-system" / "swarm-egress.yaml")
    assert ours == labels(RELAY_TEMPLATE)


def test_the_pod_is_admissible_on_autopilot_and_under_restricted():
    """No privileged container, no added capability, no host namespace, no
    TUN device: the reason the operator's own egress proxy is not used."""
    pod = _pod()
    assert not pod.get("hostNetwork") and not pod.get("hostPID") and not pod.get("hostIPC")
    assert "initContainers" not in pod
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] != 0
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod["automountServiceAccountToken"] is False
    for volume in pod["volumes"]:
        assert "hostPath" not in volume, volume["name"]
    for name, container in _containers(pod).items():
        sc = container["securityContext"]
        assert sc.get("privileged", False) is False, name
        assert sc["allowPrivilegeEscalation"] is False, name
        assert sc["readOnlyRootFilesystem"] is True, name
        assert sc["capabilities"] == {"drop": ["ALL"]}, name
    text = TEMPLATE.read_text()
    assert "NET_ADMIN" not in text.replace("NET_ADMIN only with", "")
    assert "/dev/net/tun" not in text


def test_tailscale_runs_in_userspace_with_state_in_memory():
    env = {e["name"]: e.get("value") for e in _containers(_pod())["tailscale"]["env"]}
    assert env["TS_USERSPACE"] == "true"
    # Explicitly empty: unset, containerboot keeps state in a Kubernetes Secret
    # and the pod would need RBAC.
    assert env["TS_KUBE_SECRET"] == ""
    assert "TS_STATE_DIR" not in env
    # Kernel-mode egress settings containerboot refuses in userspace.
    for name in ("TS_TAILNET_TARGET_FQDN", "TS_TAILNET_TARGET_IP", "TS_DEST_IP", "TS_ROUTES"):
        assert name not in env, name
    assert env["TS_EXTRA_ARGS"] == "--advertise-tags=tag:swarmcloud"
    assert env["TS_SOCKET"] == "/run/tailscale/tailscaled.sock"


def test_the_oauth_secret_is_a_file_from_operator_oauth_never_an_environment_value():
    pod = _pod()
    tailscale = _containers(pod)["tailscale"]
    env = {e["name"]: e for e in tailscale["env"]}
    assert env["TS_CLIENT_SECRET"] == {"name": "TS_CLIENT_SECRET", "value": "file:/oauth/client_secret"}
    assert not [e for e in tailscale["env"] if "valueFrom" in e and "secretKeyRef" in e["valueFrom"]]
    (oauth,) = [v for v in pod["volumes"] if v["name"] == "oauth"]
    assert oauth["secret"]["secretName"] == "operator-oauth"
    assert oauth["secret"]["items"] == [{"key": "client_secret", "path": "client_secret"}]
    assert oauth["secret"].get("optional", False) is False
    mounts = {m["name"]: m for m in tailscale["volumeMounts"]}
    assert mounts["oauth"]["mountPath"] == "/oauth" and mounts["oauth"]["readOnly"] is True
    # The forwarder never sees it.
    assert "oauth" not in {m["name"] for m in _containers(pod)["forwarder"]["volumeMounts"]}


def test_both_containers_run_the_pinned_image():
    for name, container in _containers(_pod()).items():
        assert container["image"] == IMAGE, name


def test_the_forwarder_dials_exactly_the_target_on_443():
    command = _containers(_pod())["forwarder"]["command"]
    assert command[:7] == ["/bin/busybox", "nc", "-lk", "-p", str(FORWARD_PORT), "-e", "/usr/local/bin/tailscale"]
    # `-e` takes the rest of the line: the program's own arguments, fixed.
    assert command[7:] == ["--socket=/run/tailscale/tailscaled.sock", "nc", TARGET, "443"]


def test_one_replica_requests_equal_limits_no_spot():
    docs = _rendered()
    (deployment,) = _kind(docs, "Deployment")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod = _pod(docs)
    assert "spot" not in yaml.safe_dump(pod).lower()
    for name, container in _containers(pod).items():
        resources = container["resources"]
        assert resources["requests"] == resources["limits"], name


def test_one_service_decision_api_on_443_to_the_forwarder():
    (service,) = _kind(_rendered(), "Service")
    assert service["metadata"]["name"] == "decision-api"
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert service["spec"]["selector"] == APP
    assert service["spec"]["ports"] == [
        {"name": "https", "protocol": "TCP", "port": SERVICE_PORT, "targetPort": FORWARD_PORT}
    ]
    assert service["metadata"]["annotations"]["swarm.saga.xyz/tailnet-fqdn"] == TARGET
    ports = [p["containerPort"] for c in _pod()["containers"] for p in c.get("ports", [])]
    assert ports == [FORWARD_PORT]


def test_the_service_clusterip_is_rendered_from_the_one_value():
    """Judging pods pin the tailnet name to this address (hostAliases, #1380),
    so the Service carries it fixed, from terraform's one statement of it --
    never written into the template itself."""
    (service,) = _kind(_rendered(), "Service")
    assert service["spec"]["clusterIP"] == _cluster_ip() == "10.48.6.120"
    assert _cluster_ip() not in TEMPLATE.read_text()
    assert "clusterIP: __DECISION_API_CLUSTER_IP__" in TEMPLATE.read_text()


# --- who reaches it ------------------------------------------------------------


def _ingress_policy(docs: list[dict[str, Any]]) -> dict[str, Any]:
    policies = [p for p in _kind(docs, "NetworkPolicy") if p["spec"]["podSelector"].get("matchLabels") == APP]
    assert len(policies) == 1
    return policies[0]


def judge_ingress_problems(policy: dict[str, Any]) -> list[str]:
    """What is wrong with the forwarder's ingress: anything but TCP 8443 from
    a `swarm-judge: internal` pod in a tenant namespace, as ONE peer."""
    problems = []
    spec = policy.get("spec", {})
    if spec.get("podSelector") != {"matchLabels": APP}:
        problems.append("selects something other than the forwarder")
    if spec.get("policyTypes") != ["Ingress"]:
        problems.append("is not an ingress policy")
    rules = spec.get("ingress") or []
    if len(rules) != 1:
        return problems + [f"has {len(rules)} rules, not one"]
    (rule,) = rules
    if rule.get("ports") != [{"protocol": "TCP", "port": FORWARD_PORT}]:
        problems.append("admits a port other than TCP 8443")
    peers = rule.get("from") or []
    if len(peers) != 1:
        return problems + [f"has {len(peers)} peers, not one (two would be an OR)"]
    (peer,) = peers
    if set(peer) != {"namespaceSelector", "podSelector"}:
        problems.append("the peer is not both selectors")
    if peer.get("podSelector") != {"matchLabels": POD_LABEL}:
        problems.append("admits pods not labelled swarm-judge: internal")
    if peer.get("namespaceSelector") != {
        "matchLabels": {"app.kubernetes.io/part-of": "swarm"},
        "matchExpressions": [{"key": "swarm-tenant", "operator": "Exists"}],
    }:
        problems.append("admits namespaces that are not tenant namespaces")
    return problems


def test_only_a_labelled_tenant_pod_reaches_the_forwarder_on_8443():
    assert judge_ingress_problems(_ingress_policy(_rendered())) == []


def _split_peer(spec: dict[str, Any]) -> None:
    (peer,) = spec["ingress"][0]["from"]
    spec["ingress"][0]["from"] = [{"namespaceSelector": peer["namespaceSelector"]},
                                  {"podSelector": peer["podSelector"]}]


def _ingress_mutants(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    import copy

    def mutate(edit):
        p = copy.deepcopy(policy)
        edit(p["spec"])
        return p

    return {
        "every-pod-in-tenants": mutate(lambda s: s["ingress"][0]["from"][0].pop("podSelector")),
        "or-of-two-peers": mutate(_split_peer),
        "any-namespace": mutate(lambda s: s["ingress"][0]["from"][0].update(namespaceSelector={})),
        "another-port": mutate(lambda s: s["ingress"][0]["ports"].append({"protocol": "TCP", "port": 9002})),
        "a-second-rule": mutate(lambda s: s["ingress"].append({"from": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]})),
        "another-label": mutate(lambda s: s["ingress"][0]["from"][0].update(
            podSelector={"matchLabels": {"swarm-judge": "typesafe"}})),
    }


def test_the_ingress_check_refuses_every_widening():
    for name, mutant in _ingress_mutants(_ingress_policy(_rendered())).items():
        assert judge_ingress_problems(mutant), f"the check passed a widened policy: {name}"


def _tenant_policy() -> dict[str, Any]:
    text = TENANT_POLICY.read_text().replace("__NAMESPACE__", "swarm-tenant-eng").replace("__TENANT_ID__", "eng")
    (policy,) = [d for d in yaml.safe_load_all(text) if d]
    return policy


def test_the_tenant_side_opens_8443_to_the_forwarder_only_from_a_labelled_pod():
    policy = _tenant_policy()
    assert policy["metadata"]["name"] == "swarm-allow-judge-egress"
    assert policy["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": POD_LABEL}
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
            "ports": [{"protocol": "TCP", "port": FORWARD_PORT}],
        }
    ]


def test_the_two_sides_name_the_same_pods_label_and_port():
    """The tenant policy's destination is the forwarder the manifest runs, and
    the manifest admits the label the tenant policy selects."""
    docs = _rendered()
    (deployment,) = _kind(docs, "Deployment")
    assert deployment["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == APP["app.kubernetes.io/name"]
    tenant = _tenant_policy()["spec"]
    ingress = _ingress_policy(docs)["spec"]["ingress"][0]
    assert tenant["podSelector"] == ingress["from"][0]["podSelector"]
    assert tenant["egress"][0]["ports"] == ingress["ports"]


@functools.cache
def _render() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_judge", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_tenant_policy_reaches_every_tenant_namespace_last():
    files = _render().TENANT_FILES
    # Only the graph service's tenant policy (lane GS-INFRA) and the TypeSafe
    # forwarder's (lane TS-MIGRATE-POD) follow it, appended the same way.
    assert files[-3:] == (TENANT_POLICY_FILE, "network-policies/allow-graph-service.yaml",
                          "network-policies/allow-typesafe-fwd.yaml")


def test_the_workspace_deployer_may_write_the_tenant_policy():
    text = (KUBERNETES / "policies" / "workspace-provisioner-scope.yaml").read_text()
    assert '"swarm-allow-judge-egress"' in text


# --- apply.sh --swarm-system ---------------------------------------------------

FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) printf 'swarm-dev' ;;
  *"config view"*)
    if [[ -f "${d}/cluster-entry" ]]; then cat "${d}/cluster-entry"; else printf 'gke_saga-agents-staging_us-central1_swarm-autopilot'; fi ;;
  *"get namespace swarm-system"*) printf 'namespace/swarm-system\n' ;;
  *"get networkpolicies"*) cat "${d}/applied.json" ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"apply --dry-run=server"*) cat >/dev/null; printf 'networkpolicy.networking.k8s.io/x created (server dry run)\n' ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *"apply -f -"*)
    cat >"${d}/applied.yaml"
    python3 -c 'import json,sys,yaml; print(json.dumps({"items":[d for d in yaml.safe_load_all(open(sys.argv[1])) if d]}))' \
      "${d}/applied.yaml" >"${d}/applied.json"
    printf 'deployment.apps/decision-api-egress created\n' ;;
  *"create secret generic"*)
    # --from-file=client_secret=<path>: record the mode of the file it read.
    path=""
    for a in "$@"; do case "${a}" in --from-file=client_secret=*) path="${a#--from-file=client_secret=}" ;; esac; done
    stat -c '%a' "${path}" >"${d}/secret-file-mode"
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: operator-oauth\n  namespace: swarm-system\ndata:\n  client_secret: %s\n' \
      "$(base64 <"${path}" | tr -d '\n')" ;;
  *"label --local -f"*)
    file=""
    prev=""
    for a in "$@"; do [[ "${prev}" == "-f" ]] && file="${a}"; prev="${a}"; done
    awk '{ print } /^metadata:$/ { print "  labels:"; print "    managed-by: swarm-terraform" }' "${file}" ;;
  *"apply -f "*)
    file=""
    prev=""
    for a in "$@"; do [[ "${prev}" == "-f" ]] && file="${a}"; prev="${a}"; done
    cp "${file}" "${d}/secret-applied.yaml"
    printf 'secret/operator-oauth configured\n' ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/gcloud.log"
case "$*" in
  *"auth list"*) : ;;
  *"run services describe swarm-api"*) cat "${d}/service.json" ;;
  *"compute networks subnets describe"*) cat "${d}/subnet.json" ;;
  *"secrets versions access latest --secret swarm-tailscale-oauth"*)
    if [[ ! -f "${d}/oauth-value" ]]; then
      printf 'ERROR: (gcloud.secrets.versions.access) NOT_FOUND: Secret Version [latest] not found\n' >&2
      exit 1
    fi
    out=""
    for a in "$@"; do case "${a}" in --out-file=*) out="${a#--out-file=}" ;; esac; done
    cat "${d}/oauth-value" >"${out}" ;;
  *) printf 'fake gcloud: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/terraform.log"
case "$*" in
  *"output -raw swarm_live_manifest"*) cat "${d}/relay.yaml" ;;
  *"output -json live_relay"*) cat "${d}/live_relay.json" ;;
  *"output -raw tailscale_egress_manifest"*)
    if [[ ! -f "${d}/tailscale.yaml" ]]; then
      printf 'Error: Unsupported value for raw output\n' >&2
      exit 1
    fi
    cat "${d}/tailscale.yaml" ;;
  *"output -raw swarm_egress_manifest"*)
    printf 'Error: Unsupported value for raw output\n' >&2
    exit 1 ;;
  *) printf 'fake terraform: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

LEAKY = (
    "PROJECT_ID", "REGION", "ZONE", "ENVIRONMENT", "GKE_CLUSTER", "GKE_LOCATION", "API_SERVICE",
    "KUBECTL", "SWARM_KUBECTL", "KUBECONFIG", "SWARM_ENV_FILE", "GITHUB_ACTIONS",
    "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "SWARM_TERRAFORM", "TF_DATA_DIR",
    "TF_STATE_BUCKET", "TF_STATE_PREFIX",
)
FAKE_API_CIDR = "10.123.16.0/20"
FAKE_SUBNET = "swarm-fake-subnet"


def _relay_manifest() -> str:
    values = {
        "__SWARM_LIVE_IMAGE__": "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:" + "7" * 64,
        "__PROJECT_ID__": "saga-agents-staging",
        "__ARTIFACT_BUCKET__": "saga-agents-staging-swarm-artifacts",
        "__LIVE_KEY_VERSION__": "projects/saga-agents-staging/secrets/swarm-live-key/versions/latest",
        "__SWARM_LIVE_ILB_IP__": "10.123.16.42",
        "__SWARM_API_EGRESS_CIDR__": FAKE_API_CIDR,
    }
    text = RELAY_TEMPLATE.read_text()
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def _oauth_value() -> str:
    return "tskey-" + "client-" + secrets.token_hex(6) + "-" + secrets.token_hex(20)


@pytest.fixture
def cluster(tmp_path: Path) -> dict[str, Any]:
    d = tmp_path / "cluster"
    b = tmp_path / "bin"
    d.mkdir()
    b.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM)):
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    interfaces = [{"network": "swarm-vpc", "subnetwork": f"projects/saga-agents-staging/regions/us-central1/subnetworks/{FAKE_SUBNET}"}]
    (d / "service.json").write_text(json.dumps({"spec": {"template": {"metadata": {"annotations": {
        "run.googleapis.com/network-interfaces": json.dumps(interfaces)}}}}}))
    (d / "subnet.json").write_text(json.dumps({"name": FAKE_SUBNET, "ipCidrRange": FAKE_API_CIDR}))
    (d / "relay.yaml").write_text(_relay_manifest())
    (d / "live_relay.json").write_text(json.dumps({"enabled": True, "port": 8080, "swarm_api_egress_cidr": FAKE_API_CIDR}))
    (d / "tailscale.yaml").write_text(
        TEMPLATE.read_text().replace("__TAILNET_FQDN__", TARGET)
        .replace("__DECISION_API_CLUSTER_IP__", _cluster_ip()))
    value = _oauth_value()
    (d / "oauth-value").write_text(value + "\n")
    (tmp_path / "tfdata").mkdir()
    env = {k: v for k, v in os.environ.items() if k not in LEAKY}
    # common.sh has no default project any more: name the one the suite models.
    env["PROJECT_ID"] = "saga-agents-staging"
    env.update(
        PATH=f"{b}:{env.get('PATH', '')}",
        FAKE_CLUSTER_DIR=str(d),
        SWARM_KUBECTL=str(b / "kubectl"),
        SWARM_TERRAFORM=str(b / "terraform"),
        TF_DATA_DIR=str(tmp_path / "tfdata"),
        SWARM_ENV_FILE=str(tmp_path / "no.env"),
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
    )
    return {"dir": d, "env": env, "value": value, "tmp": tmp_path}


def _apply(cluster: dict[str, Any], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(APPLY), "--swarm-system", *argv],
        capture_output=True, text=True, env=cluster["env"], timeout=120, check=False,
    )


def _names(text: str) -> list[tuple[str, str]]:
    return [(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(text) if d]


def _gcloud_log(cluster: dict[str, Any]) -> str:
    path = cluster["dir"] / "gcloud.log"
    return path.read_text() if path.exists() else ""


def test_a_dry_run_previews_the_tailnet_egress_and_never_reads_the_secret(cluster):
    result = _apply(cluster)
    assert result.returncode == 0, result.stderr
    order = _names((cluster["dir"] / "applied-dry-run.yaml").read_text())
    assert ("Deployment", "decision-api-egress") in order and ("Service", "decision-api") in order
    # After the relay and the policies: the tailnet egress is last.
    assert order.index(("StatefulSet", "swarm-live")) < order.index(("Deployment", "decision-api-egress"))
    assert order[-1] == ("Deployment", "decision-api-egress")
    assert "secrets versions access" not in _gcloud_log(cluster)
    assert not (cluster["dir"] / "secret-applied.yaml").exists()
    assert "operator-oauth" in result.stdout + result.stderr


def test_confirm_applies_then_syncs_the_secret_without_printing_it(cluster):
    result = _apply(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    applied = (cluster["dir"] / "applied.yaml").read_text()
    assert ("Deployment", "decision-api-egress") in _names(applied)
    # The Secret never travels in the previewed, diffed manifest.
    assert "kind: Secret" not in applied
    assert "access latest --secret swarm-tailscale-oauth --project saga-agents-staging" in _gcloud_log(cluster)
    secret = yaml.safe_load((cluster["dir"] / "secret-applied.yaml").read_text())
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "operator-oauth"
    assert secret["metadata"]["namespace"] == "swarm-system"  # namespace-prefix-exempt: the platform namespace, not a tenant
    assert secret["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    import base64

    assert base64.b64decode(secret["data"]["client_secret"]).decode().strip() == cluster["value"]
    # The value, and its base64, are in no line of output.
    assert cluster["value"] not in out
    assert secret["data"]["client_secret"] not in out
    # It went through a file only its owner can read, and the file is gone.
    assert (cluster["dir"] / "secret-file-mode").read_text().strip() == "600"
    assert not list(cluster["tmp"].glob("swarm-tailscale-oauth.*")), "the secret's work directory was left behind"
    assert "its value was never printed" in out


def test_without_a_tailscale_manifest_nothing_about_the_tailnet_happens(cluster):
    (cluster["dir"] / "tailscale.yaml").unlink()
    result = _apply(cluster, "--confirm")
    assert result.returncode == 0, result.stderr
    assert "enable_tailscale_egress is off" in result.stdout + result.stderr
    assert ("Deployment", "decision-api-egress") not in _names((cluster["dir"] / "applied.yaml").read_text())
    assert "secrets versions access" not in _gcloud_log(cluster)
    assert not (cluster["dir"] / "secret-applied.yaml").exists()


def test_a_missing_secret_version_fails_the_run_and_says_how_to_store_it(cluster):
    (cluster["dir"] / "oauth-value").unlink()
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert "scripts/create-secrets.sh --tailscale-oauth --stdin" in result.stderr, result.stderr
    assert not (cluster["dir"] / "secret-applied.yaml").exists()


def test_a_value_that_is_not_an_oauth_client_secret_is_refused_unprinted(cluster):
    wrong = "tskey-" + "auth-" + secrets.token_hex(16)
    (cluster["dir"] / "oauth-value").write_text(wrong)
    result = _apply(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not a Tailscale OAuth client secret" in result.stderr, result.stderr
    assert wrong not in out and wrong[:13] not in out.replace("tskey-client-", "")
    assert not (cluster["dir"] / "secret-applied.yaml").exists()


def test_the_shared_cluster_is_refused_before_anything_is_read(cluster):
    (cluster["dir"] / "cluster-entry").write_text("gke_saga-agents-staging_us-central1-a_agents-staging")
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert "agents-staging" in result.stderr and "deny-list" in result.stderr, result.stderr
    assert not (cluster["dir"] / "terraform.log").exists()
    assert "secrets versions access" not in _gcloud_log(cluster)
    assert not (cluster["dir"] / "applied.yaml").exists()


@pytest.mark.parametrize(
    "mutate, said",
    [
        (lambda t: t.replace("namespace: swarm-system", "namespace: default", 1), "other than swarm-system"),
        (lambda t: t + "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: x\n  namespace: swarm-system\n", "RBAC"),
        (lambda t: t + "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: operator-oauth\n  namespace: swarm-system\n", "carries a Secret"),
        (lambda t: t.replace(TARGET, "__TAILNET_FQDN__", 1), "placeholder"),
    ],
    ids=["another-namespace", "rbac", "secret", "placeholder"],
)
def test_apply_refuses_a_tailscale_manifest_that_reaches_past_its_lane(cluster, mutate, said):
    path = cluster["dir"] / "tailscale.yaml"
    path.write_text(mutate(path.read_text()))
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert said in result.stderr, result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()
    assert "secrets versions access" not in _gcloud_log(cluster)


def test_apply_and_terraform_name_the_same_two_secrets():
    script = APPLY.read_text()
    assert 'TAILSCALE_OAUTH_GSM_NAME="swarm-tailscale-oauth"' in script
    assert 'TAILSCALE_K8S_OBJECT="operator-oauth"' in script
    tf = TAILSCALE_TF.read_text()
    assert 'secret_id = "swarm-tailscale-oauth"' in tf
    assert 'tailscale_k8s_object      = "operator-oauth"' in tf
