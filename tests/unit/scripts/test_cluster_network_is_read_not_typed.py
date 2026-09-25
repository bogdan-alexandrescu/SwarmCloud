"""The egress policy's network values come from the cluster, and are checked
against it.

THE INCIDENT. On 2026-09-24 a GKE worker (task_1f699ef4cdbb4cc79c16) ran for
390 s and printed nothing. The tenant egress policy allowed DNS only to the
kube-dns PODS; swarm-autopilot runs Cloud DNS with NodeLocal DNSCache, so a
pod's queries go to the hostNetwork `node-local-dns` agent on 169.254.20.10 and
on the kube-dns Service IP, and no rule allowed either. Every lookup was
dropped silently. Underneath that sat the latent defect (RC3): the policy had
been rendered from the renderer's DEFAULTS -- pods 10.0.0.0/8, services
34.118.224.0/20 -- because neither `kubernetes/apply.sh` nor
`scripts/register-tenant.sh` ever passed the real ranges (10.44.0.0/14 and
10.48.0.0/20).

WHAT IS UNDER TEST HERE is the shipped shell, not a restatement of it:

  * `kubernetes/apply.sh` reads the pod range, the service range, the kube-dns
    Service IP and the node-local DNS address from the cluster and renders
    THOSE -- driven end to end against a fake `gcloud` and a fake `kubectl` that
    describe a cluster whose values appear nowhere in the repository;
  * `scripts/lib/check-cluster-network-parity.sh` fails when an applied policy
    differs from the live cluster, passes when it matches, refuses an empty
    sweep, and in CI -- no credentials -- skips with a notice rather than
    silently.

No real cluster, no credentials and no network are touched: both binaries are
shell scripts written into a temporary directory and put first on PATH.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
APPLY = REPO / "kubernetes" / "apply.sh"
PARITY = REPO / "scripts" / "lib" / "check-cluster-network-parity.sh"
CHECKER = REPO / "kubernetes" / "network_parity.py"

#: The context scripts/configure-kubectl.sh writes. apply.sh refuses the raw
#: gcloud name for this cluster because it contains `agents-staging` -- the
#: other team's cluster -- as a substring of the project id.
CONTEXT = "swarm-dev"
CLUSTER_REF = "gke_saga-agents-staging_us-central1_swarm-autopilot"
SOURCE = "gke/saga-agents-staging/us-central1/swarm-autopilot"

#: A cluster whose network appears nowhere in this repository, so a rendered
#: value that matches it can only have been READ.
FAKE = {
    "pod_cidr": "10.160.0.0/14",
    "service_cidr": "10.164.0.0/20",
    "cluster_dns_ip": "10.164.0.10",
    "node_local_dns_ip": "169.254.21.10",
}

#: swarm-autopilot as measured on 2026-09-24 (gcloud container clusters
#: describe; kubectl -n kube-system get service kube-dns; the node-local-dns
#: DaemonSet's -localip). Used ONLY to replay the incident below.
INCIDENT_CLUSTER = {
    "pod_cidr": "10.44.0.0/14",
    "service_cidr": "10.48.0.0/20",
    "cluster_dns_ip": "10.48.0.10",
    "node_local_dns_ip": "169.254.20.10",
}

#: `swarm-allow-worker-egress` in swarm-tenant-eng as it was live on
#: 2026-09-24 (generation 1, applied 02:56:03Z), read with kubectl get -o json.
#: This is the object the silent worker ran under.
INCIDENT_POLICY: dict[str, Any] = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {
        "name": "swarm-allow-worker-egress",
        "namespace": "swarm-tenant-eng",
        "labels": {"managed-by": "swarm-terraform", "swarm-tenant": "eng"},
    },
    "spec": {
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [
            {
                "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                        },
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    }
                ],
            },
            {
                "ports": [{"port": 988, "protocol": "TCP"}, {"port": 80, "protocol": "TCP"}],
                "to": [{"ipBlock": {"cidr": "169.254.169.254/32"}}],
            },
            {
                "ports": [{"port": 443, "protocol": "TCP"}],
                "to": [
                    {"ipBlock": {"cidr": "199.36.153.4/30"}},
                    {"ipBlock": {"cidr": "199.36.153.8/30"}},
                ],
            },
            {
                "ports": [
                    {"port": 443, "protocol": "TCP"},
                    {"port": 80, "protocol": "TCP"},
                    {"port": 22, "protocol": "TCP"},
                ],
                "to": [
                    {
                        "ipBlock": {
                            "cidr": "0.0.0.0/0",
                            "except": [
                                "10.0.0.0/8",
                                "34.118.224.0/20",
                                "172.16.0.0/12",
                                "192.168.0.0/16",
                                "169.254.0.0/16",
                                "100.64.0.0/10",
                            ],
                        }
                    }
                ],
            },
        ],
    },
}

FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\nKustomize Version: v5.8.1\n' ;;
  *"config current-context"*) cat "${d}/context" ;;
  *"config view"*) cat "${d}/cluster-ref" ;;
  *"get service kube-dns"*) cat "${d}/kube-dns-ip" ;;
  *"get daemonset node-local-dns"*) cat "${d}/node-local-dns.json" ;;
  *"get networkpolicies"*) cat "${d}/policies.json" ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/gcloud.log"
case "$*" in
  *"auth list"*) if [[ -f "${d}/account" ]]; then cat "${d}/account"; fi ;;
  *"iam service-accounts describe swarm-scheduler@"*) echo 117405034245659033603 ;;
  *"iam service-accounts describe swarm-reconciler@"*) echo 108023754768362642341 ;;
  *"container clusters describe"*) cat "${d}/describe.json" ;;
  *) printf 'fake gcloud: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

#: Environment that must not leak from the machine running the suite into the
#: scripts under test: each would redirect them at a real project or cluster.
LEAKY = (
    "PROJECT_ID", "REGION", "ZONE", "ENVIRONMENT", "GKE_CLUSTER", "GKE_LOCATION",
    "KUBECTL", "SWARM_KUBECTL", "KUBECONFIG", "SWARM_ENV_FILE", "GITHUB_ACTIONS",
    "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS",
)


def describe(network: dict[str, str], *, node_local: bool = True) -> dict[str, Any]:
    """The fields of `gcloud container clusters describe` the resolver reads,
    shaped as the live output was on 2026-09-24."""
    return {
        "name": "swarm-autopilot",
        "clusterIpv4Cidr": network["pod_cidr"],
        "servicesIpv4Cidr": network["service_cidr"],
        "ipAllocationPolicy": {
            "clusterIpv4CidrBlock": network["pod_cidr"],
            "servicesIpv4CidrBlock": network["service_cidr"],
            "useIpAliases": True,
        },
        "addonsConfig": {"dnsCacheConfig": {"enabled": node_local}},
        "networkConfig": {
            "datapathProvider": "ADVANCED_DATAPATH",
            "dnsConfig": {"clusterDns": "CLOUD_DNS", "clusterDnsScope": "CLUSTER_SCOPE"},
        },
    }


def node_local_dns(network: dict[str, str]) -> dict[str, Any]:
    """The node-local-dns DaemonSet, reduced to what matters: the node-cache
    container's `-localip`, which lists the addresses the agent answers on."""
    return {
        "kind": "DaemonSet",
        "metadata": {"name": "node-local-dns", "namespace": "kube-system"},
        "spec": {
            "template": {
                "spec": {
                    "hostNetwork": True,
                    "containers": [
                        {
                            "name": "node-cache",
                            "args": [
                                "-localip",
                                f"{network['node_local_dns_ip']},{network['cluster_dns_ip']}",
                                "-conf",
                                "/etc/Corefile",
                            ],
                        },
                        {"name": "nodelocaldns-metrics-collector"},
                    ],
                }
            }
        },
    }


class Cluster:
    """A fake cluster on disk, and the two fake binaries that read it."""

    def __init__(self, tmp_path: Path, network: dict[str, str]) -> None:
        self.dir = tmp_path / "cluster"
        self.bin = tmp_path / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        self.tmp = tmp_path
        for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        (self.dir / "context").write_text(CONTEXT)
        (self.dir / "cluster-ref").write_text(CLUSTER_REF)
        (self.dir / "kube-dns-ip").write_text(network["cluster_dns_ip"])
        (self.dir / "describe.json").write_text(json.dumps(describe(network)))
        (self.dir / "node-local-dns.json").write_text(json.dumps(node_local_dns(network)))
        self.policies([])

    def signed_in(self) -> None:
        (self.dir / "account").write_text("operator@example.com\n")

    def policies(self, items: list[dict[str, Any]]) -> None:
        (self.dir / "policies.json").write_text(
            json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        )

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in LEAKY}
        env.update(
            PATH=os.pathsep.join(
                [str(self.bin), str(Path(sys.executable).parent), os.environ.get("PATH", "")]
            ),
            SWARM_KUBECTL=str(self.bin / "kubectl"),
            SWARM_ENV_FILE=str(self.tmp / "no-such.env"),
            KUBECONFIG=str(self.tmp / "no-such-kubeconfig"),
            PYTHON_BIN=sys.executable,
            FAKE_CLUSTER_DIR=str(self.dir),
            TMPDIR=str(self.tmp),
            NO_COLOR="1",
        )
        env.update(extra)
        return env

    def run(self, script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=REPO,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def log(self, binary: str) -> str:
        path = self.dir / f"{binary}.log"
        return path.read_text() if path.exists() else ""


def _egress(manifest: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(manifest) if d]
    matches = [
        d for d in docs
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "swarm-allow-worker-egress"
    ]
    assert len(matches) == 1, f"expected one egress policy in the render, found {len(matches)}"
    return matches[0]


def _ip_peers(policy: dict[str, Any]) -> set[str]:
    return {
        peer["ipBlock"]["cidr"]
        for rule in policy["spec"]["egress"]
        for peer in rule.get("to", [])
        if "ipBlock" in peer
    }


def _render(network: dict[str, str], tenant: str = "eng") -> dict[str, Any]:
    """The egress policy the real renderer produces for `network`."""
    out = subprocess.run(
        [
            sys.executable, str(REPO / "kubernetes" / "render.py"), "tenant", "--tenant", tenant,
            "--pod-cidr", network["pod_cidr"],
            "--service-cidr", network["service_cidr"],
            "--cluster-dns-ip", network["cluster_dns_ip"],
            "--node-local-dns-ip", network["node_local_dns_ip"],
            "--network-source", SOURCE,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return _egress(out.stdout)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_network_parity", CHECKER)
    assert spec and spec.loader, f"{CHECKER} is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# kubernetes/apply.sh renders what the cluster reports
# ---------------------------------------------------------------------------


def test_apply_renders_the_network_the_cluster_reports(tmp_path):
    """A dry run against the fake cluster renders the fake cluster's values.

    THE DEFECT THIS PINS: apply.sh passed no network value at all, so every
    tenant policy was rendered from the renderer's defaults whatever cluster it
    was applied to, and nothing ever told the renderer where DNS lives.
    """
    cluster = Cluster(tmp_path, FAKE)
    result = cluster.run(APPLY, "--tenant", "eng")
    assert result.returncode == 0, result.stderr

    rendered = cluster.dir / "applied-dry-run.yaml"
    assert rendered.exists(), "apply.sh did not validate a render"
    policy = _egress(rendered.read_text())

    peers = _ip_peers(policy)
    for key in ("cluster_dns_ip", "node_local_dns_ip"):
        assert f"{FAKE[key]}/32" in peers, (
            f"the rendered policy has no ipBlock for the cluster's {key} ({FAKE[key]}); "
            "DNS on a NodeLocal DNSCache cluster is dropped without it"
        )
    excepts = {
        e for rule in policy["spec"]["egress"] for peer in rule.get("to", [])
        for e in peer.get("ipBlock", {}).get("except", [])
    }
    assert {FAKE["pod_cidr"], FAKE["service_cidr"]} <= excepts, (
        f"the rendered internet rule does not except the cluster's ranges: {sorted(excepts)}"
    )
    annotations = policy["metadata"].get("annotations") or {}
    assert annotations.get("swarm.saga.xyz/network-source") == SOURCE

    # Read from the cluster the context points at, through the context.
    gcloud = cluster.log("gcloud")
    assert "container clusters describe swarm-autopilot" in gcloud
    assert "--location us-central1" in gcloud and "--project saga-agents-staging" in gcloud
    kubectl = cluster.log("kubectl")
    assert "-n kube-system get service kube-dns" in kubectl
    assert "-n kube-system get daemonset node-local-dns" in kubectl


@pytest.mark.parametrize(
    "flag", ["--pod-cidr", "--service-cidr", "--cluster-dns-ip", "--node-local-dns-ip", "--network-source"]
)
def test_apply_refuses_a_network_value_on_the_command_line(tmp_path, flag):
    """A value passed by hand is the value that goes stale. apply.sh forwards
    unknown flags to the renderer, so without a refusal `--pod-cidr 10.0.0.0/8`
    re-creates RC3 one flag at a time."""
    cluster = Cluster(tmp_path, FAKE)
    result = cluster.run(APPLY, "--tenant", "eng", flag, "10.0.0.0/8")
    assert result.returncode != 0, f"apply.sh accepted {flag} from the command line"
    assert "read from the cluster" in result.stderr
    assert not (cluster.dir / "applied-dry-run.yaml").exists()


#: Abbreviations of the five network flags, each with a value the renderer
#: would ACCEPT against FAKE -- canonical, non-overlapping, the DNS IP inside
#: the service range -- so a render that goes through is the typed value
#: winning, not a validation error that happens to fail the same test.
ABBREVIATED_NETWORK_FLAGS = [
    (["--pod-cid", "10.200.0.0/14"], "--pod-cidr"),
    (["--pod-cid=10.200.0.0/14"], "--pod-cidr"),
    (["--pod", "10.200.0.0/14"], "--pod-cidr"),
    (["--service-c", "10.164.0.0/16"], "--service-cidr"),
    (["--cluster-dns", "10.164.0.53"], "--cluster-dns-ip"),
    (["--cluster-dns=10.164.0.53"], "--cluster-dns-ip"),
    (["--node-local", "169.254.99.10"], "--node-local-dns-ip"),
    (["--no", "169.254.99.10"], "--node-local-dns-ip"),
    (["--network-s", "gke/typed/by/hand"], "--network-source"),
]


@pytest.mark.parametrize(
    "args,flag", ABBREVIATED_NETWORK_FLAGS, ids=[a[0] for a, _ in ABBREVIATED_NETWORK_FLAGS]
)
def test_apply_refuses_an_abbreviated_network_flag(tmp_path, args, flag):
    """The refusal above matched only the full spellings. Python's argparse
    accepts any unambiguous prefix of a long option, and apply.sh appends what
    it forwards AFTER the values it read -- so `--pod-cid 10.200.0.0/14` reached
    the renderer as a second `--pod-cidr` and the last one won: the typed value
    replaced the cluster's, which is RC3 again with one letter missing."""
    cluster = Cluster(tmp_path, FAKE)
    result = cluster.run(APPLY, "--tenant", "eng", *args)
    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"apply.sh accepted {args[0]!r}, an abbreviation of {flag}; the render went through:\n{output}"
    )
    # Named, not merely non-zero: the refusal must be apply.sh's own, naming the
    # flag the abbreviation would have become.
    assert "read from the cluster" in result.stderr, output
    assert flag in result.stderr, output
    assert not (cluster.dir / "applied-dry-run.yaml").exists()


@pytest.mark.parametrize(
    "args", [["--cluster=swarm-autopilot"], ["--context=swarm-dev"], ["--tenant=eng"]]
)
def test_apply_reads_its_own_flags_in_equals_form(tmp_path, args):
    """apply.sh matched only `--cluster NAME`, so `--cluster=NAME` fell through
    to the renderer -- where argparse read `--cluster` as an abbreviation of
    `--cluster-dns-ip` and tried to render the cluster's NAME as its DNS
    address. Each of apply.sh's value flags is its own in either spelling."""
    cluster = Cluster(tmp_path, FAKE)
    base = [] if args[0].startswith("--tenant") else ["--tenant", "eng"]
    result = cluster.run(APPLY, *base, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = cluster.dir / "applied-dry-run.yaml"
    assert rendered.exists(), "apply.sh did not validate a render"
    peers = _ip_peers(_egress(rendered.read_text()))
    assert f"{FAKE['cluster_dns_ip']}/32" in peers, sorted(peers)


def test_the_renderer_accepts_no_abbreviated_flag():
    """The second half of the same hole, closed where it opens: render.py must
    not expand a prefix into a flag at all. With abbreviation on, a caller that
    forwards arguments -- apply.sh, or the next wrapper someone writes -- has to
    enumerate every prefix of every flag it means to withhold, and apply.sh's
    list proved that nobody does."""
    result = subprocess.run(
        [
            sys.executable, str(REPO / "kubernetes" / "render.py"), "tenant", "--tenant", "eng",
            "--pod-cid", FAKE["pod_cidr"],
            "--service-c", FAKE["service_cidr"],
            "--cluster-dns", FAKE["cluster_dns_ip"],
            "--node-local", FAKE["node_local_dns_ip"],
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 2, (
        "render.py expanded abbreviated network flags into the real ones and rendered:\n"
        + result.stdout[:400]
    )
    assert "unrecognized arguments" in result.stderr, result.stderr


def test_apply_refuses_when_the_node_local_dns_address_cannot_be_read(tmp_path):
    """No guessing: a missing DaemonSet is a refusal, not a typed 169.254.20.10."""
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "node-local-dns.json").unlink()
    result = cluster.run(APPLY, "--tenant", "eng")
    assert result.returncode != 0, "apply.sh rendered without the node-local DNS address"
    assert "node-local-dns" in result.stderr
    assert not (cluster.dir / "applied-dry-run.yaml").exists()


def test_apply_refuses_a_cluster_without_nodelocal_dnscache(tmp_path):
    """The policy's DNS rule is written for NodeLocal DNSCache, which GKE
    documents as always on for Autopilot. A cluster reporting it off is not the
    cluster the policy was written for."""
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "describe.json").write_text(json.dumps(describe(FAKE, node_local=False)))
    result = cluster.run(APPLY, "--tenant", "eng")
    assert result.returncode != 0, "apply.sh rendered for a cluster with NodeLocal DNSCache off"
    assert "dnsCacheConfig" in result.stderr
    assert not (cluster.dir / "applied-dry-run.yaml").exists()


def test_apply_refuses_when_the_cluster_cannot_be_described(tmp_path):
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "describe.json").unlink()
    result = cluster.run(APPLY, "--tenant", "eng")
    assert result.returncode != 0, "apply.sh rendered without describing the cluster"
    assert not (cluster.dir / "applied-dry-run.yaml").exists()


# ---------------------------------------------------------------------------
# scripts/lib/check-cluster-network-parity.sh
# ---------------------------------------------------------------------------


def test_parity_is_skipped_with_a_notice_when_there_are_no_credentials(tmp_path):
    """CI has no cloud credentials. The check must say it did not run -- a
    GitHub `::notice` -- rather than pass as if it had compared something."""
    cluster = Cluster(tmp_path, FAKE)
    result = cluster.run(PARITY, GITHUB_ACTIONS="true")
    assert result.returncode == 0, result.stderr
    assert "::notice" in result.stdout, "the skip was silent"
    assert "skipped" in (result.stdout + result.stderr).lower()
    assert cluster.log("kubectl") == "" or "get " not in cluster.log("kubectl"), (
        "the check read the cluster without credentials"
    )


def test_parity_without_credentials_fails_when_live_is_required(tmp_path):
    cluster = Cluster(tmp_path, FAKE)
    result = cluster.run(PARITY, "--require-live")
    assert result.returncode != 0, "--require-live accepted a skipped check"
    # A positive signal, not just a non-zero exit: a MISSING script also exits
    # non-zero, and this test passed on the commit before the script existed.
    assert "NOT checked" in result.stderr, result.stdout + result.stderr


def test_parity_fails_on_the_policy_the_silent_worker_ran_under(tmp_path):
    """Replayed against the live cluster's measured values, the incident policy
    must fail, and must name the two DNS addresses it could not reach."""
    cluster = Cluster(tmp_path, INCIDENT_CLUSTER)
    cluster.signed_in()
    cluster.policies([INCIDENT_POLICY])
    result = cluster.run(PARITY)
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"the incident policy passed parity:\n{output}"
    assert "10.48.0.10" in output and "169.254.20.10" in output, output
    assert "swarm-tenant-eng" in output


def test_parity_passes_on_a_policy_rendered_from_the_live_cluster(tmp_path):
    cluster = Cluster(tmp_path, FAKE)
    cluster.signed_in()
    policy = _render(FAKE)
    cluster.policies([policy])
    result = cluster.run(PARITY)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "compared 1 " in output, f"the check did not report what it compared:\n{output}"


def test_parity_fails_when_the_policy_was_rendered_for_another_network(tmp_path):
    """The mirrored-copy rule: a rendered value that differs from the live one
    fails, even where the policy would still isolate (a pod range inside 10/8
    is excepted either way) -- because the next difference may not."""
    cluster = Cluster(tmp_path, FAKE)
    cluster.signed_in()
    cluster.policies([_render({**FAKE, "pod_cidr": "10.168.0.0/14"})])
    result = cluster.run(PARITY)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "pod-cidr" in output and "10.168.0.0/14" in output, output


def test_parity_refuses_an_empty_sweep(tmp_path):
    """No policy found is not agreement. It is nothing compared."""
    cluster = Cluster(tmp_path, FAKE)
    cluster.signed_in()
    cluster.policies([])
    result = cluster.run(PARITY)
    assert result.returncode != 0, "an empty sweep passed"
    # Named, not merely non-zero: a missing script exits non-zero too.
    assert "nothing was compared" in result.stdout + result.stderr, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# kubernetes/network_parity.py, the comparison itself
# ---------------------------------------------------------------------------


def test_the_checker_names_every_rendered_value_that_differs():
    checker = _load_checker()
    policy = _render(FAKE)
    assert checker.check_policy(policy, FAKE) == []

    other = {
        "pod_cidr": "10.180.0.0/14",
        "service_cidr": "10.184.0.0/20",
        "cluster_dns_ip": "10.184.0.10",
        "node_local_dns_ip": "169.254.22.10",
    }
    problems = "\n".join(checker.check_policy(policy, other))
    for value in other.values():
        assert value in problems, f"{value} is not named in:\n{problems}"


def test_the_checker_judges_the_spec_not_only_the_annotations():
    """Annotations say what a policy was rendered FOR; the spec is what is
    enforced. A policy whose annotations match but whose DNS rule is gone must
    still fail."""
    checker = _load_checker()
    policy = _render(FAKE)
    policy["spec"]["egress"] = [
        rule for rule in policy["spec"]["egress"]
        if not any(
            peer.get("ipBlock", {}).get("cidr") == f"{FAKE['node_local_dns_ip']}/32"
            for peer in rule.get("to", [])
        )
    ]
    problems = "\n".join(checker.check_policy(policy, FAKE))
    assert FAKE["node_local_dns_ip"] in problems, problems
