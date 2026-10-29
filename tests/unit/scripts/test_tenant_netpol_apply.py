"""Every tenant namespace gets the full tenant NetworkPolicy set, from the release
(lane H1-NETPOL).

Measured 2026-10-11: the live browser needed `swarm-allow-live-egress`
(kubernetes/network-policies/allow-live-egress.yaml) in each tenant namespace,
no release applied it, and the operator applied it by hand to eng, smoke and
u-sw-c90291. `scripts/apply-tenant-network-policies.sh` now re-applies the set
to every namespace labelled `swarm-tenant`; these tests hold:

  * the SET: what the script applies equals the policy files on disk that are
    meant for a tenant (namespaced `__NAMESPACE__`), so a new tenant policy
    file that is not wired into kubernetes/render.py TENANT_FILES fails here;
  * the RENDER: only NetworkPolicies, only in the namespace asked for, and the
    helper's YAML-free summary agrees with a YAML parse;
  * the SWEEP, against a fake cluster: every labelled namespace (a terraform
    tenant and a workspace tenant alike) is applied, read back and swept by
    the parity check; a dry run writes nothing; another team's cluster, a
    labelled namespace outside `swarm-tenant-`, and an empty sweep are refused.

No cluster, no credentials, no network: `gcloud` and `kubectl` are fakes on
PATH, as in test_cluster_network_is_read_not_typed.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "apply-tenant-network-policies.sh"
HELPER = REPO / "scripts" / "tenant_netpol.py"
POLICY_DIR = REPO / "kubernetes" / "network-policies"

PROJECT = "saga-agents-staging"
LOCATION = "us-central1"
CLUSTER_REF = f"gke_{PROJECT}_{LOCATION}_swarm-autopilot"
FOREIGN_REF = f"gke_{PROJECT}_{LOCATION}-a_agents-staging"

#: A network that appears nowhere in the repository, so a rendered value that
#: matches it can only have been READ from the (fake) cluster.
NETWORK = {
    "pod_cidr": "10.160.0.0/14",
    "service_cidr": "10.164.0.0/20",
    "cluster_dns_ip": "10.164.0.10",
    "node_local_dns_ip": "169.254.21.10",
}

#: The three namespaces the operator patched by hand on 2026-10-11.
TENANTS = {"swarm-tenant-eng": "eng", "swarm-tenant-smoke": "smoke", "swarm-tenant-u-sw-c90291": "u-sw-c90291"}

LEAKY = (
    "PROJECT_ID", "REGION", "ZONE", "ENVIRONMENT", "GKE_CLUSTER", "GKE_LOCATION",
    "KUBECTL", "SWARM_KUBECTL", "KUBECONFIG", "SWARM_ENV_FILE", "GITHUB_ACTIONS",
    "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "PYTHON_BIN",
)


def tenant_policy_files_on_disk() -> set[str]:
    """Every file in kubernetes/network-policies/ whose objects go into a TENANT
    namespace: the template namespaces them `__NAMESPACE__`. swarm-system's file
    names `swarm-system` and is applied by `apply.sh --swarm-system` instead."""
    found = set()
    for path in sorted(POLICY_DIR.glob("*.y*ml")):
        namespaces = {
            (doc.get("metadata") or {}).get("namespace")
            for doc in yaml.safe_load_all(path.read_text())
            if doc
        }
        if "__NAMESPACE__" in namespaces:
            found.add(f"network-policies/{path.name}")
    return found


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in LEAKY}
    # common.sh has no default project any more: name the one the suite models.
    env["PROJECT_ID"] = "saga-agents-staging"
    env.update(PYTHON_BIN=sys.executable, NO_COLOR="1")
    env.update(extra)
    return env


# --- the set ----------------------------------------------------------------


def test_the_script_applies_every_tenant_policy_file_and_nothing_else(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--list-files"],
        cwd=REPO,
        env=clean_env(SWARM_ENV_FILE=str(tmp_path / "no-such.env"), TMPDIR=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    applied = [line for line in result.stdout.splitlines() if line]
    assert len(applied) == len(set(applied)), applied
    on_disk = tenant_policy_files_on_disk()
    assert set(applied) == on_disk, (
        "the policy set scripts/apply-tenant-network-policies.sh applies to every tenant "
        f"namespace is {sorted(applied)}, but the tenant policy files in "
        f"kubernetes/network-policies/ are {sorted(on_disk)}. A tenant policy that is not in "
        "kubernetes/render.py TENANT_FILES reaches no namespace -- the 2026-10-11 "
        "swarm-allow-live-egress miss."
    )
    # The control: the directory scan can tell the two kinds of file apart.
    assert "network-policies/allow-live-egress.yaml" in on_disk
    assert "network-policies/swarm-system-live.yaml" not in on_disk


# --- the render -------------------------------------------------------------


def render(tmp_path: Path, tenant: str, namespace: str) -> tuple[str, list[list[str]]]:
    summary = tmp_path / f"{namespace}.tsv"
    result = subprocess.run(
        [
            sys.executable, str(HELPER), "render", "--tenant", tenant, "--namespace", namespace,
            "--pod-cidr", NETWORK["pod_cidr"], "--service-cidr", NETWORK["service_cidr"],
            "--cluster-dns-ip", NETWORK["cluster_dns_ip"],
            "--node-local-dns-ip", NETWORK["node_local_dns_ip"],
            "--network-source", f"gke/{PROJECT}/{LOCATION}/swarm-autopilot",
            "--summary", str(summary),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, [line.split("\t") for line in summary.read_text().splitlines()]


@pytest.mark.parametrize("namespace", sorted(TENANTS))
def test_the_render_is_only_network_policies_in_that_namespace(tmp_path, namespace):
    manifest, rows = render(tmp_path, TENANTS[namespace], namespace)
    docs = [doc for doc in yaml.safe_load_all(manifest) if doc]
    parsed = [[d["kind"], d["metadata"]["name"], d["metadata"]["namespace"]] for d in docs]
    assert rows == parsed, "the helper's summary disagrees with a YAML parse of its render"
    assert {kind for kind, _, _ in rows} == {"NetworkPolicy"}
    assert {ns for _, _, ns in rows} == {namespace}
    names = {name for _, name, _ in rows}
    assert {"swarm-default-deny", "swarm-allow-worker-egress", "swarm-allow-live-egress"} <= names
    for doc in docs:
        assert doc["metadata"]["labels"]["swarm-tenant"] == TENANTS[namespace]
    # The cluster's network was rendered, not a default.
    assert NETWORK["pod_cidr"] in manifest


def test_the_render_refuses_without_the_clusters_network(tmp_path):
    result = subprocess.run(
        [sys.executable, str(HELPER), "render", "--tenant", "eng", "--summary", str(tmp_path / "s")],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode != 0
    assert "offline-render-not-for-apply" in result.stderr
    assert result.stdout == ""


# --- the sweep, against a fake cluster ---------------------------------------

FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) cat "${d}/context" ;;
  *"config view"*) cat "${d}/cluster-ref" ;;
  *"get service kube-dns"*) cat "${d}/kube-dns-ip" ;;
  *"get daemonset node-local-dns"*) cat "${d}/node-local-dns.json" ;;
  *"get namespaces -l swarm-tenant"*) cat "${d}/namespaces.json" ;;
  *"get networkpolicies --all-namespaces"*)
    "${FAKE_PYTHON:?}" - "${d}/applied" <<'PY'
import json, pathlib, sys, yaml
items = [doc for p in sorted(pathlib.Path(sys.argv[1]).glob("*.yaml"))
         for doc in yaml.safe_load_all(p.read_text())
         if doc and doc["metadata"]["name"] == "swarm-allow-worker-egress"]
print(json.dumps({"kind": "List", "items": items}))
PY
    ;;
  *"get networkpolicies "*)
    # get networkpolicies NAME --namespace NS -o name
    set -- $*
    while [[ "$1" != "networkpolicies" ]]; do shift; done
    name="$2"; ns="$4"
    # getfail/<ns>.<name> holds how many more gets of that policy fail with an
    # error that is not NotFound (a transient API-server answer); -1 is forever.
    fail="${d}/getfail/${ns}.${name}"
    if [[ -f "${fail}" ]]; then
      left="$(cat "${fail}")"
      if [[ "${left}" != 0 ]]; then
        [[ "${left}" == -1 ]] || echo $((left - 1)) >"${fail}"
        echo "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout" >&2; exit 1
      fi
    fi
    if grep -qx "${ns}	${name}" "${d}/present" 2>/dev/null; then
      echo "networkpolicy.networking.k8s.io/${name}"
    else
      echo "Error from server (NotFound)" >&2; exit 1
    fi ;;
  *"diff -f "*) exit 0 ;;
  *"apply --dry-run=server -f "*) echo "server dry run" ;;
  *"apply -f "*)
    f="${*: -1}"
    mkdir -p "${d}/applied"
    n=$(ls "${d}/applied" | wc -l)
    cp "${f}" "${d}/applied/${n}.yaml"
    "${FAKE_PYTHON:?}" - "${f}" "${d}/drop" >>"${d}/present" <<'PY'
import pathlib, sys, yaml
# A name in `drop` is accepted by the apply but never reaches the cluster.
drop = pathlib.Path(sys.argv[2])
dropped = set(drop.read_text().split()) if drop.exists() else set()
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc['metadata']['name'] not in dropped:
        print(f"{doc['metadata']['namespace']}\t{doc['metadata']['name']}")
PY
    echo "applied" ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/gcloud.log"
case "$*" in
  *"auth list"*) echo operator@example.com ;;
  *"container clusters describe"*) cat "${d}/describe.json" ;;
  *) printf 'fake gcloud: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""


def namespace_list(labelled: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "List",
        "items": [
            {"metadata": {"name": ns, "labels": {"swarm-tenant": tenant, "managed-by": "swarm-terraform"}}}
            for ns, tenant in labelled.items()
        ],
    }


class Cluster:
    def __init__(self, tmp_path: Path, labelled: dict[str, str]) -> None:
        self.tmp = tmp_path
        self.dir = tmp_path / "cluster"
        self.bin = tmp_path / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.dir / "context").write_text("swarm-dev")
        (self.dir / "cluster-ref").write_text(CLUSTER_REF)
        (self.dir / "kube-dns-ip").write_text(NETWORK["cluster_dns_ip"])
        (self.dir / "describe.json").write_text(json.dumps({
            "clusterIpv4Cidr": NETWORK["pod_cidr"],
            "servicesIpv4Cidr": NETWORK["service_cidr"],
            "addonsConfig": {"dnsCacheConfig": {"enabled": True}},
        }))
        (self.dir / "node-local-dns.json").write_text(json.dumps({
            "spec": {"template": {"spec": {"containers": [{
                "name": "node-cache",
                "args": ["-localip", f"{NETWORK['node_local_dns_ip']},{NETWORK['cluster_dns_ip']}"],
            }]}}}
        }))
        (self.dir / "namespaces.json").write_text(json.dumps(namespace_list(labelled)))

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = clean_env(
            PATH=os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
            SWARM_KUBECTL=str(self.bin / "kubectl"),
            SWARM_ENV_FILE=str(self.tmp / "no-such.env"),
            KUBECONFIG=str(self.tmp / "no-such-kubeconfig"),
            FAKE_CLUSTER_DIR=str(self.dir),
            FAKE_PYTHON=sys.executable,
            TMPDIR=str(self.tmp),
        )
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=REPO, env=env,
            capture_output=True, text=True, timeout=300, check=False,
        )

    def applied(self) -> list[dict[str, Any]]:
        root = self.dir / "applied"
        if not root.exists():
            return []
        return [doc for p in sorted(root.glob("*.yaml")) for doc in yaml.safe_load_all(p.read_text()) if doc]

    def log(self, binary: str) -> str:
        path = self.dir / f"{binary}.log"
        return path.read_text() if path.exists() else ""


def test_confirm_applies_the_full_set_to_every_labelled_namespace(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    result = cluster.run("--confirm")
    assert result.returncode == 0, result.stderr
    docs = cluster.applied()
    assert {d["kind"] for d in docs} == {"NetworkPolicy"}
    expected_names = {d["metadata"]["name"] for d in yaml.safe_load_all(render(tmp_path, "eng", "swarm-tenant-eng")[0]) if d}
    for namespace in TENANTS:
        names = {d["metadata"]["name"] for d in docs if d["metadata"]["namespace"] == namespace}
        assert names == expected_names, (namespace, names)
    assert {d["metadata"]["namespace"] for d in docs} == set(TENANTS)
    assert "visited 3 namespace(s)" in result.stderr
    # Read back by name, then the parity sweep over every namespace.
    assert "get networkpolicies swarm-allow-live-egress --namespace swarm-tenant-u-sw-c90291" in cluster.log("kubectl")
    assert "get networkpolicies --all-namespaces" in cluster.log("kubectl")
    assert "every applied egress policy matches" in result.stderr


def test_a_second_run_applies_the_same_objects(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    assert cluster.run("--confirm").returncode == 0
    first = cluster.applied()
    assert cluster.run("--confirm").returncode == 0
    assert cluster.applied() == first + first


def test_a_dry_run_writes_nothing(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    result = cluster.run()
    assert result.returncode == 0, result.stderr
    assert cluster.applied() == []
    assert "dry run over 3 namespace(s)" in result.stderr
    assert "apply --dry-run=server" in cluster.log("kubectl")


def test_another_teams_cluster_is_refused_before_anything_is_read(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    (cluster.dir / "context").write_text("swarm-dev")
    (cluster.dir / "cluster-ref").write_text(FOREIGN_REF)
    result = cluster.run("--confirm")
    assert result.returncode != 0
    assert "deny-list" in result.stderr
    assert cluster.applied() == []
    assert "get namespaces" not in cluster.log("kubectl")
    assert "describe" not in cluster.log("gcloud")


def test_a_foreign_cluster_flag_is_refused(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    result = cluster.run("--cluster", "agents-staging", "--confirm")
    assert result.returncode != 0
    assert cluster.applied() == []


def test_a_labelled_namespace_outside_the_prefix_is_refused_and_fails_the_run(tmp_path):
    cluster = Cluster(tmp_path, {**TENANTS, "kube-system": "eng"})
    result = cluster.run("--confirm")
    assert result.returncode != 0
    assert "kube-system" in result.stderr
    assert "kube-system" not in {d["metadata"]["namespace"] for d in cluster.applied()}
    # The others were still applied.
    assert {d["metadata"]["namespace"] for d in cluster.applied()} == set(TENANTS)


def test_no_labelled_namespace_is_a_failure_not_a_clean_sweep(tmp_path):
    cluster = Cluster(tmp_path, {})
    result = cluster.run("--confirm")
    assert result.returncode != 0
    assert "no namespace is labelled swarm-tenant" in result.stderr


# --- the read-back: only NotFound is missing (#2185) -------------------------
#
# Release 40034319802 (2026-10-23) failed its deploy job on
# swarm-allow-typesafe-fwd in swarm-tenant-u-devops: the apply reported it
# `unchanged`, one read-back get failed, and the script counted ANY failed get
# as "not found". These hold the three answers apart.

READBACK_NS = "swarm-tenant-u-sw-c90291"
READBACK_POLICY = "swarm-allow-live-egress"


def fail_gets(cluster: Cluster, times: int) -> None:
    (cluster.dir / "getfail").mkdir(exist_ok=True)
    (cluster.dir / "getfail" / f"{READBACK_NS}.{READBACK_POLICY}").write_text(str(times))


def test_one_transient_read_back_failure_is_retried_and_the_run_passes(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    fail_gets(cluster, 1)
    result = cluster.run("--confirm")
    assert result.returncode == 0, result.stderr
    assert "not found on read-back" not in result.stderr
    assert "could not be read back" not in result.stderr
    gets = [
        line for line in cluster.log("kubectl").splitlines()
        if f"get networkpolicies {READBACK_POLICY} --namespace {READBACK_NS} " in line
    ]
    # The control: the failure was served, and the get was asked again.
    assert len(gets) == 2, gets
    assert (cluster.dir / "getfail" / f"{READBACK_NS}.{READBACK_POLICY}").read_text().strip() == "0"


def test_a_read_back_that_keeps_failing_is_unreadable_not_missing(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    fail_gets(cluster, -1)
    result = cluster.run("--confirm")
    assert result.returncode != 0
    assert f"{READBACK_NS}: applied, but could not be read back: {READBACK_POLICY}" in result.stderr
    assert "i/o timeout" in result.stderr
    assert "not found on read-back" not in result.stderr
    gets = [
        line for line in cluster.log("kubectl").splitlines()
        if f"get networkpolicies {READBACK_POLICY} --namespace {READBACK_NS} " in line
    ]
    assert len(gets) == 3, gets
    assert f"failed: {READBACK_NS}" in result.stderr
    # The other namespaces still passed their read-back.
    assert "1 of 3 tenant namespace(s) failed" in result.stderr


def test_a_policy_truly_absent_after_the_apply_is_still_not_found(tmp_path):
    cluster = Cluster(tmp_path, TENANTS)
    (cluster.dir / "drop").write_text(READBACK_POLICY + "\n")
    result = cluster.run("--confirm")
    assert result.returncode != 0
    for namespace in TENANTS:
        assert f"{namespace}: applied, but not found on read-back: {READBACK_POLICY}" in result.stderr
    assert "could not be read back" not in result.stderr
    # NotFound is an answer, not a failure: it is not retried.
    gets = [
        line for line in cluster.log("kubectl").splitlines()
        if f"get networkpolicies {READBACK_POLICY} --namespace {READBACK_NS} " in line
    ]
    assert len(gets) == 1, gets
