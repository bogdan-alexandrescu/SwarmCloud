"""Every release rolls swarm-graph to the image it built (lane GS-ROLL, part of
#1384; docs/design/graph-service.md §2.1, §8.2 step 2).

terraform/infra/graph_service.tf renders `swarm_graph_manifest` -- the
Deployment swarm-system/swarm-graph on swarm-autopilot, with the release's
swarm-graph digest -- and is null until graph_service.serve. Nothing applied it.
`scripts/roll-graph.sh` does, through `kubernetes/apply.sh --swarm-graph`, then
waits for `rollout status deploy/swarm-graph` and proves the service's pods run
the digest the manifest names. These tests hold, against a fake cluster,
terraform and gcloud:

  * not served (a null output) is a notice and exit 0, with nothing applied
    and neither gcloud nor the cluster asked anything: that is every release
    while the switch is off;
  * the shared `agents-staging` cluster -- by flag, by context, or by
    terraform's own output -- is refused before anything is written;
  * a manifest is applied through apply.sh, then rolled, then read back;
  * a pod on another digest fails the roll; one being deleted does not count;
  * apply.sh --swarm-graph itself refuses a manifest that reaches outside
    swarm-system, and does nothing when the output is null;
  * after applying, apply.sh --swarm-graph --confirm reads swarm-api's egress
    range from its service and runs `network_parity.py --graph-ingress`
    against swarm-graph-ingress as the cluster holds it; a stale range, a
    missing policy or a widened one fails the apply (CACHE-FOLLOWUPS, part
    of #1835).

Each refusal or failure test asserts the opposite outcome on the unmodified
fixture first (the CONTROL), so a fixture that could never succeed cannot make
a refusal test pass.

No cluster, no credentials, no network: every binary is a fake on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .test_live_netpol import LEAKY, _service
from .test_live_relay_rollout import FAKE_GCLOUD

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "roll-graph.sh"
APPLY = REPO / "kubernetes" / "apply.sh"
TEMPLATE = REPO / "kubernetes" / "swarm-system" / "swarm-graph.yaml"

PROJECT = "saga-agents-staging"
LOCATION = "us-central1"
CLUSTER = "swarm-autopilot"
CLUSTER_REF = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"
FOREIGN_REF = f"gke_{PROJECT}_{LOCATION}-a_agents-staging"

#: Built rather than written out as one literal.
DIGEST = "sha256:" + "7" * 64
STALE_DIGEST = "sha256:" + "0" * 64
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-graph@{DIGEST}"
#: swarm-api's Direct VPC egress: the subnet its service names, and that
#: subnet's range -- the one _graph_manifest renders into swarm-graph-ingress.
API_SUBNET = "swarm-fake-subnet"
API_CIDR = "10.10.0.0/20"


def _graph_manifest(image: str = IMAGE) -> str:
    """kubernetes/swarm-system/swarm-graph.yaml with every placeholder filled,
    as terraform/infra/graph_service.tf fills them (graph_service_iam.tftest.hcl
    and test_graph_service_manifest.py hold that rendering; this only needs
    plausible values)."""
    config = json.dumps({"enabled": False})
    values = {
        "__SWARM_GRAPH_IMAGE__": image,
        "__GRAPH_TICKET_KEY_VERSION__": f"projects/{PROJECT}/secrets/swarm-graph-ticket-key/versions/latest",
        "__GRAPH_BUCKET__": f"{PROJECT}-swarm-artifacts",
        "__GRAPH_CONFIG_SHA256__": "c" * 64,
        "__GRAPH_CONFIG_JSON__": config,
        "__GRAPH_STATS_ADDRESS__": "10.10.0.81",
        "__SWARM_API_EGRESS_CIDR__": API_CIDR,
    }
    text = TEMPLATE.read_text()
    for key, value in values.items():
        text = text.replace(key, value)
    assert "__" not in "".join(line for line in text.splitlines() if not line.lstrip().startswith("#")), \
        "a placeholder the test does not fill"
    return text


# kubectl, answering for the swarm-graph Deployment from what was applied.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
py="${FAKE_PYTHON:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
deployment() {
  "${py}" - "${d}/applied.yaml" <<'PY'
import json, sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc["kind"] == "Deployment" and doc["metadata"]["name"] == "swarm-graph":
        print(json.dumps(doc))
PY
}
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) cat "${d}/context" ;;
  *"config view"*) cat "${d}/cluster-entry" ;;
  *"get deployment swarm-graph"*)
    [[ -f "${d}/applied.yaml" ]] || { echo 'Error from server (NotFound): deployments.apps "swarm-graph" not found' >&2; exit 1; }
    deployment ;;
  *"get pods"*"-o json"*)
    if [[ -f "${d}/pod-image-id" ]]; then image_id="$(cat "${d}/pod-image-id")"; else
      image_id="$(deployment | "${py}" -c 'import json,sys; print(json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["image"])')"; fi
    old=""
    [[ -f "${d}/terminating-image-id" ]] && old="$(cat "${d}/terminating-image-id")"
    "${py}" - "${image_id}" "${old}" <<'PY'
import json, sys
def pod(name, image_id, deleting=False):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-16T00:00:00Z"
    return {"metadata": meta, "status": {"containerStatuses": [{"name": "graph", "imageID": image_id}]}}
items = [pod("swarm-graph-new-a", sys.argv[1]), pod("swarm-graph-new-b", sys.argv[1])]
if sys.argv[2]:
    items.append(pod("swarm-graph-old-a", sys.argv[2], deleting=True))
print(json.dumps({"items": items}))
PY
    ;;
  *"get pods"*) printf 'NAME                 READY   STATUS\nswarm-graph-new-a    0/1     CrashLoopBackOff\n' ;;
  *"get networkpolicies"*)
    if [[ -f "${d}/policies.json" ]]; then cat "${d}/policies.json"; exit 0; fi
    "${py}" - "${d}/applied.yaml" <<'PY'
import json, sys, yaml
docs = [doc for doc in yaml.safe_load_all(open(sys.argv[1])) if doc and doc["kind"] == "NetworkPolicy"]
print(json.dumps({"items": docs}))
PY
    ;;
  *"rollout status deploy/swarm-graph"*)
    if [[ -f "${d}/rollout-fails" ]]; then echo 'error: timed out waiting for the condition' >&2; exit 1; fi
    printf 'deployment "swarm-graph" successfully rolled out\n' ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"apply --dry-run=server"*) cat >/dev/null; printf 'deployment.apps/swarm-graph configured (server dry run)\n' ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *"apply -f -"*)
    cat >"${d}/applied.yaml"
    printf 'deployment.apps/swarm-graph configured\n' ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

# terraform, as the real one behaves: a null output is NOT in state, so a
# named read of it (`-raw` or `-json`) fails with 'Output "..." not found'
# (release 38867328505 went red on that); the unnamed `output -json` lists the
# outputs state holds, without the null one.
FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
py="${FAKE_PYTHON:?}"
printf '%s\n' "$*" >>"${d}/terraform.log"
case "$*" in
  *" init "*) printf 'Terraform has been successfully initialized!\n' ;;
  *"output -raw swarm_graph_manifest"*|*"output -json swarm_graph_manifest"*)
    if [[ -f "${d}/state-unreadable" ]]; then printf 'Error: Failed to load state\n' >&2; exit 1; fi
    if [[ ! -f "${d}/graph.yaml" ]]; then
      printf 'Error: Output "swarm_graph_manifest" not found\n' >&2
      exit 1
    fi
    case "$*" in
      *"-raw"*) cat "${d}/graph.yaml" ;;
      *) "${py}" -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read()))' "${d}/graph.yaml" ;;
    esac ;;
  *"output -json")
    if [[ -f "${d}/state-unreadable" ]]; then printf 'Error: Failed to load state\n' >&2; exit 1; fi
    "${py}" - "${d}" <<'PY'
import json, os, sys
d = sys.argv[1]
outs = {"gke_cluster_name": {"sensitive": False, "type": "string", "value": "swarm-autopilot"}}
g = os.path.join(d, "graph.yaml")
if os.path.exists(g):
    outs["swarm_graph_manifest"] = {"sensitive": False, "type": "string", "value": open(g).read()}
print(json.dumps(outs))
PY
    ;;
  *"output -raw gke_cluster_name"*) cat "${d}/gke-cluster" ;;
  *) printf 'fake terraform: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""


class Cluster:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.dir = tmp_path / "cluster"
        self.bin = tmp_path / "bin"
        self.dir.mkdir(parents=True)
        self.bin.mkdir()
        for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.dir / "context").write_text("swarm-dev")
        (self.dir / "cluster-entry").write_text(CLUSTER_REF)
        (self.dir / "gke-cluster").write_text(CLUSTER)
        (self.dir / "service.json").write_text(json.dumps(
            _service(f"projects/{PROJECT}/regions/{LOCATION}/subnetworks/{API_SUBNET}")))
        self.api_range(API_CIDR)
        (self.tmp / "tfdata").mkdir()
        self.serve(True)

    def serve(self, on: bool, manifest: str | None = None) -> None:
        if on:
            (self.dir / "graph.yaml").write_text(manifest if manifest is not None else _graph_manifest())
        else:
            (self.dir / "graph.yaml").unlink(missing_ok=True)

    def api_range(self, cidr: str) -> None:
        """The range swarm-api's subnet reports to `subnets describe`."""
        (self.dir / "subnet.json").write_text(json.dumps({"name": API_SUBNET, "ipCidrRange": cidr}))

    def run(self, *args: str, script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in (*LEAKY, "PYTHON_BIN")}
        env.update(
            PATH=os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
            FAKE_CLUSTER_DIR=str(self.dir),
            FAKE_PYTHON=sys.executable,
            SWARM_KUBECTL=str(self.bin / "kubectl"),
            SWARM_TERRAFORM=str(self.bin / "terraform"),
            TF_DATA_DIR=str(self.tmp / "tfdata"),
            SWARM_ENV_FILE=str(self.tmp / "no.env"),
            # apply.sh has no default project (DAYONE-K8S-1): the one this fake
            # cluster's context and identities name is given, as an operator would.
            PROJECT_ID="saga-agents-staging",
            KUBECONFIG=str(self.tmp / "no-such-kubeconfig"),
            HOME=str(self.tmp),
            TMPDIR=str(self.tmp),
            NO_COLOR="1",
        )
        return subprocess.run(
            ["bash", str(script), *args], cwd=REPO, env=env,
            capture_output=True, text=True, timeout=300, check=False,
        )

    def log(self, binary: str) -> str:
        path = self.dir / f"{binary}.log"
        return path.read_text() if path.exists() else ""

    def applied(self) -> bool:
        return (self.dir / "applied.yaml").exists()


def _first(lines: list[str], needle: str) -> int:
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert hits, f"no call containing {needle!r} in {lines}"
    return hits[0]


def test_the_script_is_an_executable_strict_script_on_common_sh():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    body = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert body[0] == "set -euo pipefail", body[:2]
    assert any("lib/common.sh" in line and line.startswith("source ") for line in body), \
        "roll-graph.sh must source common.sh for the deny-list and kubectl_bin"
    assert any("kubectl_bin" in line for line in body)
    assert any("SHARED_DENY_LIST" in line for line in body)
    assert os.access(SCRIPT, os.X_OK)


def test_not_served_is_a_notice_and_exit_0_with_nothing_applied(tmp_path):
    # CONTROL: served, the same release invocation applies and rolls.
    served = Cluster(tmp_path / "served")
    control = served.run("--tf-init", "--get-credentials", "--confirm")
    assert control.returncode == 0, control.stdout + control.stderr
    assert served.applied() and "rollout status" in served.log("kubectl")

    cluster = Cluster(tmp_path / "off")
    cluster.serve(False)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "graph_service.serve is off" in out, out
    assert not cluster.applied()
    assert not (cluster.dir / "applied-dry-run.yaml").exists()
    # Neither gcloud nor the cluster is asked anything while the switch is off.
    assert cluster.log("gcloud") == "", cluster.log("gcloud")
    assert cluster.log("kubectl") == "", cluster.log("kubectl")


def test_a_state_terraform_cannot_read_is_a_failure_not_off(tmp_path):
    """`-raw` fails for a null AND for an unreadable state; only the null is
    "not served"."""
    cluster = Cluster(tmp_path)
    cluster.serve(False)
    (cluster.dir / "state-unreadable").write_text("")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "could not read terraform's swarm_graph_manifest" in out, out
    assert "Failed to load state" in out, out
    assert not cluster.applied()


@pytest.mark.parametrize("how", ["flag", "context", "terraform"])
def test_the_shared_agents_staging_cluster_is_refused_before_any_apply(tmp_path, how):
    """agents-staging is another team's live cluster (CLAUDE.md rule 2),
    refused by common.sh's deny-list whichever way it arrives."""
    # CONTROL: the same fixture, pointed at our cluster, applies.
    ours = Cluster(tmp_path / "ours")
    control = ours.run("--tf-init", "--get-credentials", "--confirm")
    assert control.returncode == 0 and ours.applied(), control.stdout + control.stderr

    cluster = Cluster(tmp_path / "theirs")
    if how == "flag":
        result = cluster.run("--cluster", "agents-staging", "--confirm")
    elif how == "context":
        (cluster.dir / "cluster-entry").write_text(FOREIGN_REF)
        result = cluster.run("--confirm")
    else:
        (cluster.dir / "gke-cluster").write_text("agents-staging")
        result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "agents-staging" in out and "deny-list" in out, out
    assert not cluster.applied()
    assert not (cluster.dir / "applied-dry-run.yaml").exists()
    assert "get-credentials" not in cluster.log("gcloud")
    assert "rollout" not in cluster.log("kubectl")


def test_a_manifest_is_applied_through_apply_sh_then_rolled_then_read_back(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert cluster.applied()
    assert (cluster.dir / "applied.yaml").read_text().strip() == _graph_manifest().strip()

    terraform = cluster.log("terraform").splitlines()
    assert _first(terraform, "init") < _first(terraform, "output -raw swarm_graph_manifest")
    gcloud = cluster.log("gcloud")
    assert f"container clusters get-credentials {CLUSTER} --project {PROJECT} --location {LOCATION}" in gcloud, gcloud

    calls = cluster.log("kubectl").splitlines()
    applied = _first(calls, "apply -f -")
    assert f"--context {CLUSTER_REF}" in calls[applied], calls[applied]
    rollout = _first(calls, "rollout status deploy/swarm-graph")
    assert applied < rollout, calls
    assert "-n swarm-system" in calls[rollout] and "--timeout=600s" in calls[rollout], calls[rollout]
    pods = [i for i, line in enumerate(calls) if "get pods" in line]
    assert pods and pods[-1] > rollout, calls
    assert "app.kubernetes.io/name=swarm-graph" in calls[pods[-1]]
    assert DIGEST in out and "2 pod(s)" in out, out


def test_a_dry_run_previews_and_neither_applies_nor_waits(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run()
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "dry run only" in out
    assert not cluster.applied()
    assert (cluster.dir / "diff.yaml").exists()
    assert "rollout" not in cluster.log("kubectl")


def test_a_pod_on_another_digest_fails_the_roll(tmp_path):
    # CONTROL: the unmodified fixture's pods run the manifest's digest and pass.
    control_cluster = Cluster(tmp_path / "control")
    control = control_cluster.run("--confirm")
    assert control.returncode == 0, control.stdout + control.stderr

    cluster = Cluster(tmp_path / "stale")
    (cluster.dir / "pod-image-id").write_text(
        f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-graph@{STALE_DIGEST}")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "swarm-graph-new-a" in out and STALE_DIGEST in out and DIGEST in out, out
    assert "2 of 2" in out, out


def test_an_old_pod_still_terminating_is_not_counted(tmp_path):
    """After `rollout status` the old ReplicaSet's pods may still be in their
    grace period; they carry deletionTimestamp and are not what serves."""
    cluster = Cluster(tmp_path)
    (cluster.dir / "terminating-image-id").write_text(
        f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-graph@{STALE_DIGEST}")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "swarm-graph-old-a" not in out, out


def test_a_roll_that_does_not_finish_fails_and_shows_the_pods(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "rollout-fails").write_text("")
    result = cluster.run("--confirm", "--timeout", "30")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "did not finish" in out and "CrashLoopBackOff" in out, out


def test_an_image_not_pinned_by_digest_is_refused_before_any_apply(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.serve(True, _graph_manifest(f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-graph:dev"))
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not pinned by digest" in out, out
    assert not cluster.applied()


# --- kubernetes/apply.sh --swarm-graph, the branch roll-graph.sh drives -------

def test_apply_sh_swarm_graph_applies_terraforms_manifest_alone(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--swarm-graph", "--context", "swarm-dev", "--cluster", CLUSTER, "--confirm", script=APPLY)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (cluster.dir / "applied.yaml").read_text().strip() == _graph_manifest().strip()
    # Only the graph service's output, not the relay's.
    assert "swarm_live_manifest" not in cluster.log("terraform")


def test_apply_sh_swarm_graph_with_a_null_output_applies_nothing(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.serve(False)
    result = cluster.run("--swarm-graph", "--context", "swarm-dev", "--cluster", CLUSTER, "--confirm", script=APPLY)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "graph_service.serve is off" in out, out
    assert not cluster.applied()
    assert "apply" not in cluster.log("kubectl")


@pytest.mark.parametrize("defect", ["namespace", "rbac", "secret", "placeholder"])
def test_apply_sh_swarm_graph_refuses_a_manifest_that_reaches_outside(tmp_path, defect):
    manifest = _graph_manifest()
    if defect == "namespace":
        manifest = manifest.replace("  namespace: swarm-system\n", "  namespace: kube-system\n", 1)
    elif defect == "rbac":
        manifest += "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: x\n"
    elif defect == "secret":
        manifest += "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n  namespace: swarm-system\n"
    else:
        manifest = manifest.replace(IMAGE, "__SWARM_GRAPH_IMAGE__")
    assert manifest != _graph_manifest()
    cluster = Cluster(tmp_path)
    cluster.serve(True, manifest)
    result = cluster.run("--swarm-graph", "--context", "swarm-dev", "--cluster", CLUSTER, "--confirm", script=APPLY)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "nothing was applied" in out.lower(), out
    assert not cluster.applied()


def test_apply_sh_swarm_graph_refuses_agents_staging(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "cluster-entry").write_text(FOREIGN_REF)
    result = cluster.run("--swarm-graph", "--context", "swarm-dev", "--cluster", CLUSTER, "--confirm", script=APPLY)
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "agents-staging" in out and "deny-list" in out, out
    assert not cluster.applied()
    assert "swarm_graph_manifest" not in cluster.log("terraform"), "the output was read for a refused cluster"


def test_apply_sh_swarm_graph_takes_no_tenant(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--swarm-graph", "--tenant", "eng", "--confirm", script=APPLY)
    assert result.returncode != 0
    assert "--swarm-graph" in result.stderr and "--tenant" in result.stderr, result.stderr
    assert cluster.log("kubectl") == ""


# --- apply.sh --swarm-graph --confirm compares swarm-graph-ingress (CACHE-FOLLOWUPS) ---

def _apply_graph(cluster: Cluster, *extra: str) -> subprocess.CompletedProcess[str]:
    return cluster.run("--swarm-graph", "--context", "swarm-dev", "--cluster", CLUSTER, *extra, script=APPLY)


def _graph_ingress_policy() -> dict:
    return next(
        doc for doc in yaml.safe_load_all(_graph_manifest())
        if doc and doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "swarm-graph-ingress"
    )


def test_apply_sh_swarm_graph_confirm_reads_swarm_apis_range_then_runs_the_graph_parity_check(tmp_path):
    cluster = Cluster(tmp_path)
    result = _apply_graph(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    # The range is READ from swarm-api's service, as --swarm-system reads it.
    gcloud = cluster.log("gcloud")
    assert f"run services describe swarm-api --project {PROJECT}" in gcloud, gcloud
    assert f"compute networks subnets describe {API_SUBNET}" in gcloud, gcloud
    # The policies are read back AFTER the apply, from swarm-system, through
    # the checked context, and judged by --graph-ingress.
    calls = cluster.log("kubectl").splitlines()
    applied = _first(calls, "apply -f -")
    read_back = _first(calls, "get networkpolicies")
    assert applied < read_back, calls
    assert "-n swarm-system" in calls[read_back], calls[read_back]
    assert calls[read_back].startswith("--context swarm-dev "), calls[read_back]
    assert calls[applied].startswith("--context swarm-dev "), calls[applied]
    assert f"swarm-system/swarm-graph-ingress matches {API_CIDR}" in out, out


def test_apply_sh_swarm_graph_dry_run_reads_no_range_and_compares_nothing(tmp_path):
    cluster = Cluster(tmp_path)
    result = _apply_graph(cluster)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "run services describe" not in cluster.log("gcloud")
    assert "get networkpolicies" not in cluster.log("kubectl")
    assert "--graph-ingress" in out, "the preview does not say --confirm compares the ingress"


def test_a_stale_swarm_api_range_fails_the_graph_apply_with_its_reason(tmp_path):
    # CONTROL: swarm-api's subnet reports the range terraform rendered; passes.
    control = Cluster(tmp_path / "control")
    assert _apply_graph(control, "--confirm").returncode == 0
    cluster = Cluster(tmp_path / "stale")
    cluster.api_range("10.20.0.0/20")
    result = _apply_graph(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "FAIL swarm-system/swarm-graph-ingress" in out, out
    assert "does not match swarm-api's 10.20.0.0/20" in out, out
    assert cluster.applied(), "the parity check runs after the apply"


def test_a_widened_graph_ingress_fails_the_apply(tmp_path):
    # CONTROL: the policy exactly as rendered passes.
    control = Cluster(tmp_path / "control")
    (control.dir / "policies.json").write_text(json.dumps({"items": [_graph_ingress_policy()]}))
    assert _apply_graph(control, "--confirm").returncode == 0
    # A second port on the tenant rule: every widening is a failure, never
    # "allows at least".
    policy = _graph_ingress_policy()
    policy["spec"]["ingress"][0]["ports"].append({"protocol": "TCP", "port": 8081})
    cluster = Cluster(tmp_path / "widened")
    (cluster.dir / "policies.json").write_text(json.dumps({"items": [policy]}))
    result = _apply_graph(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "FAIL swarm-system/swarm-graph-ingress" in out, out


def test_a_missing_graph_ingress_fails_the_apply(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "policies.json").write_text(json.dumps({"items": []}))
    result = _apply_graph(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "swarm-system/swarm-graph-ingress does not exist" in out, out


def test_an_unreadable_swarm_api_range_fails_the_graph_apply_and_says_nothing_was_compared(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "service.json").write_text(json.dumps(_service(None)))
    result = _apply_graph(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "subnetwork" in out and "its ingress was not compared" in out, out
    assert "get networkpolicies" not in cluster.log("kubectl")
