"""swarm-voice runs as a one-replica StatefulSet in swarm-system, behind an egress
policy for OpenAI and Anthropic only (docs/design/voice-it.md §3.4, §7,
section 10 phase one; lane VOICE-STATEFULSET, part of #2688).

Held here, from the phase-one row and CONTRACT.md:

  * kubernetes/swarm-system/swarm-voice.yaml is ONE replica of a StatefulSet
    (never two copies of the sidebands through a rollout), on on-demand
    capacity (no Spot, invariant 6), whose memory and ephemeral storage
    request equals the limit and whose CPU limit is at most
    CPU_BURST_MAX_RATIO times the request (invariant 7);
  * kubernetes/network-policies/swarm-system-voice.yaml admits only swarm-api's
    egress range in, and out only DNS, the metadata server and, by name, OpenAI,
    Anthropic, swarm-api and the two Google endpoints the credential and the
    caller check need. Nothing else, and no open CIDR;
  * the image ships: `swarm-voice` is in ALL_TARGETS of build-images.sh and
    push-images.sh, and apps/swarm-voice/Dockerfile runs the service as the
    manifest's uid, installed by hash.

terraform/infra/voice.tf's rendering of both files is tests/terraform/voice.tftest.hcl's.
WHAT THIS CANNOT PROVE: that the image builds (the build-check job builds it) or
that the cluster admits the FQDNNetworkPolicy (a cluster setting; the PR asks).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from swarm_common.profiles import CPU_BURST_MAX_RATIO

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "kubernetes" / "swarm-system" / "swarm-voice.yaml"
POLICY = REPO / "kubernetes" / "network-policies" / "swarm-system-voice.yaml"
DOCKERFILE = REPO / "apps" / "swarm-voice" / "Dockerfile"
SELECTOR = {"app.kubernetes.io/name": "swarm-voice"}

# The hosts swarm-voice may open TCP 443 to, and the code that needs each.
HOSTS = {
    "api.openai.com",  # swarm_voice/live.py OPENAI_API, OPENAI_WS
    "auth.openai.com",  # swarm_voice/credential.py TOKEN_URL
    "api.anthropic.com",  # the planner, phase two
    "__SWARM_API_HOST__",  # swarm-api, rendered by voice.tf
    "secretmanager.googleapis.com",  # credential.py SECRET_MANAGER
    "www.googleapis.com",  # google.oauth2.id_token's certificates
}


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _one(path: Path, kind: str, name: str | None = None) -> dict:
    found = [d for d in _docs(path) if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]
    assert len(found) == 1, f"{path.name}: {len(found)} {kind} {name or ''}"
    return found[0]


def _quantity(value: object) -> float:
    """A Kubernetes quantity as a number: cpu in cores, bytes for Mi/Gi."""
    text = str(value)
    units = {"m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30}
    for suffix, factor in units.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def _container() -> dict:
    pod = _one(MANIFEST, "StatefulSet", "swarm-voice")["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1 and not pod.get("initContainers")
    return pod["containers"][0]


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


# --- the StatefulSet -----------------------------------------------------------


def test_swarm_voice_is_one_replica_of_a_statefulset_in_swarm_system():
    statefulset = _one(MANIFEST, "StatefulSet")
    assert statefulset["metadata"]["name"] == "swarm-voice"
    assert statefulset["metadata"]["namespace"] == "swarm-system"  # namespace-prefix-exempt: the platform namespace, not a tenant
    assert statefulset["spec"]["replicas"] == 1
    assert statefulset["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == "swarm-voice"
    assert not [d for d in _docs(MANIFEST) if d["kind"] in ("Deployment", "HorizontalPodAutoscaler", "ReplicaSet")]


def test_memory_and_ephemeral_storage_request_equals_limit():
    resources = _container()["resources"]
    for name in ("memory", "ephemeral-storage"):
        assert name in resources["requests"] and name in resources["limits"], name
        assert _quantity(resources["requests"][name]) == _quantity(resources["limits"][name]), name


def test_cpu_request_is_at_most_the_limit_and_the_limit_at_most_the_burst_ratio():
    resources = _container()["resources"]
    request = _quantity(resources["requests"]["cpu"])
    limit = _quantity(resources["limits"]["cpu"])
    assert 0 < request <= limit <= CPU_BURST_MAX_RATIO * request


def test_no_spot_capacity():
    pod = _one(MANIFEST, "StatefulSet")["spec"]["template"]["spec"]
    text = yaml.safe_dump(pod)
    assert "spot" not in text.lower() and "preemptible" not in text.lower()


def test_the_pod_acts_as_its_own_account_and_checks_its_caller():
    account = _one(MANIFEST, "ServiceAccount", "swarm-voice")
    assert account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == "__SWARM_VOICE_GSA__"
    assert account["automountServiceAccountToken"] is False
    pod = _one(MANIFEST, "StatefulSet")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "swarm-voice"
    env = {e["name"]: e["value"] for e in _container()["env"]}
    # main.google_caller_check refuses to start without either.
    assert env["SWARM_VOICE_AUDIENCE"] == "__SWARM_VOICE_AUDIENCE__"
    assert env["SWARM_VOICE_CALLERS"] == "__SWARM_VOICE_CALLERS__"
    assert env["PORT"] == "8080" and env["PROJECT_ID"] == "__PROJECT_ID__"


def test_every_object_is_labelled_managed_by_swarm_terraform():
    for path in (MANIFEST, POLICY):
        for doc in _docs(path):
            assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform", (path.name, doc["kind"])


# --- the network ----------------------------------------------------------------


def test_only_swarm_apis_egress_range_reaches_it():
    ingress = [
        d for d in _docs(POLICY)
        if d["kind"] == "NetworkPolicy" and "Ingress" in d["spec"].get("policyTypes", [])
    ]
    assert len(ingress) == 1
    spec = ingress[0]["spec"]
    assert spec["podSelector"] == {"matchLabels": SELECTOR}
    assert spec["ingress"] == [
        {"from": [{"ipBlock": {"cidr": "__SWARM_API_EGRESS_CIDR__"}}], "ports": [{"protocol": "TCP", "port": 8080}]}
    ]


def test_the_egress_policy_isolates_the_pod_and_admits_only_dns_and_the_metadata_server():
    egress = _one(POLICY, "NetworkPolicy", "swarm-voice-egress")["spec"]
    assert egress["podSelector"] == {"matchLabels": SELECTOR}
    assert egress["policyTypes"] == ["Egress"]
    destinations = []
    for rule in egress["egress"]:
        for peer in rule["to"]:
            if "ipBlock" in peer:
                assert "except" not in peer["ipBlock"]
                destinations.append(peer["ipBlock"]["cidr"])
            else:
                destinations.append(peer["podSelector"]["matchLabels"]["k8s-app"])
    assert sorted(destinations) == ["169.254.169.254/32", "kube-dns"]
    ports = {(p["protocol"], p["port"]) for rule in egress["egress"] for p in rule["ports"]}
    assert ports == {("UDP", 53), ("TCP", 53), ("TCP", 80)}


def test_the_egress_hosts_are_openai_anthropic_swarm_api_and_googles_two():
    hosts = _one(POLICY, "FQDNNetworkPolicy", "swarm-voice-egress-hosts")
    assert hosts["apiVersion"] == "networking.gke.io/v1alpha1"
    assert hosts["metadata"]["namespace"] == "swarm-system"  # namespace-prefix-exempt: the platform namespace, not a tenant
    assert hosts["spec"]["podSelector"] == {"matchLabels": SELECTOR}
    (rule,) = hosts["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    names = [m["name"] for m in rule["matches"]]
    assert all(set(m) == {"name"} for m in rule["matches"]), "no wildcard pattern"
    assert sorted(names) == sorted(HOSTS)


def test_no_policy_selects_another_pod_or_namespace():
    for doc in _docs(POLICY):
        assert doc["metadata"]["namespace"] == "swarm-system"  # namespace-prefix-exempt: the platform namespace, not a tenant
        assert doc["spec"]["podSelector"] == {"matchLabels": SELECTOR}, doc["metadata"]["name"]


# --- the image -----------------------------------------------------------------


def test_the_release_builds_and_promotes_swarm_voice():
    assert "swarm-voice" in _targets("build-images.sh")
    assert _targets("build-images.sh") == _targets("push-images.sh")


def test_the_image_runs_the_service_as_the_manifests_uid_installed_by_hash():
    text = re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text())
    for arg in ("PYTHON_IMAGE", "UV_IMAGE"):
        value = re.search(rf"^ARG {arg}=(\S+)$", text, re.M)
        assert value and re.search(r"@sha256:[0-9a-f]{64}$", value.group(1)), arg
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_voice.main"]' in text
    assert "--project /src/apps/swarm-voice" in text and "--require-hashes" in text
    assert re.search(r"--no-index\s+--find-links\s+/wheels\s+--no-deps\s+swarm-common\s+swarm-voice", text)
    uid = re.search(r"useradd --uid (\d+) --gid (\d+)", text)
    context = _one(MANIFEST, "StatefulSet")["spec"]["template"]["spec"]["securityContext"]
    assert uid and str(context["runAsUser"]) == uid.group(1) == str(context["runAsGroup"]) != "0"
