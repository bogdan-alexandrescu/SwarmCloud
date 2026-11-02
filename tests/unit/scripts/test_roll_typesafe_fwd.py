"""Every release rolls swarm-typesafe-fwd to the image it built (lane TS-ROLL,
part of #1347; docs/design/workspace-secrets.md §3.5, "Still to do",
SEC-INJECT-B what is left).

terraform/infra/typesafe_fwd.tf renders `swarm_typesafe_fwd_manifest` -- the
Deployment swarm-system/swarm-typesafe-fwd on swarm-autopilot, with the
release's swarm-typesafe-fwd digest -- and is null until
typesafe_forwarder.serve. Nothing applied it. `scripts/roll-typesafe-fwd.sh`
does, through `kubernetes/apply.sh --swarm-typesafe-fwd`, then waits for
`rollout status deploy/swarm-typesafe-fwd` and proves the forwarder's pods run
the digest the manifest names. These tests hold, against a fake cluster,
terraform and gcloud:

  * not served is a notice and exit 0, with nothing applied and neither
    gcloud nor the cluster asked anything -- whether terraform reports the
    null output as "not found" (it leaves a null output out of state, PR
    1402) or lists it with a null value; that is every release in dev today;
  * a state terraform cannot read is a failure, not "off";
  * the shared `agents-staging` cluster -- by flag, by context, or by
    terraform's own output -- is refused before anything is written;
  * a manifest is applied through apply.sh, then rolled, then read back;
  * a pod on another digest fails the roll;
  * apply.sh --swarm-typesafe-fwd itself refuses a manifest with an unfilled
    placeholder, RBAC, a Secret or a foreign namespace, and does nothing when
    the output is null.

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

from .test_live_netpol import LEAKY
from .test_live_relay_rollout import FAKE_GCLOUD

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "roll-typesafe-fwd.sh"
APPLY = REPO / "kubernetes" / "apply.sh"
TEMPLATE = REPO / "kubernetes" / "swarm-system" / "swarm-typesafe-fwd.yaml"
OUTPUT = "swarm_typesafe_fwd_manifest"

PROJECT = "saga-agents-staging"
LOCATION = "us-central1"
CLUSTER = "swarm-autopilot"
CLUSTER_REF = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"
FOREIGN_REF = f"gke_{PROJECT}_{LOCATION}-a_agents-staging"

#: Built rather than written out as one literal.
DIGEST = "sha256:" + "5" * 64
STALE_DIGEST = "sha256:" + "0" * 64
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-typesafe-fwd@{DIGEST}"


def _fwd_manifest(image: str = IMAGE) -> str:
    """kubernetes/swarm-system/swarm-typesafe-fwd.yaml with every placeholder
    filled, as terraform/infra/typesafe_fwd.tf fills them
    (test_typesafe_fwd_manifest.py holds the template; this only needs
    plausible values)."""
    config = json.dumps({"enabled": False, "tenants": [], "requests_per_minute": 60,
                         "max_request_bytes": 2097152, "max_response_seconds": 120})
    values = {
        "__SWARM_TYPESAFE_FWD_IMAGE__": image,
        "__TYPESAFE_FWD_KEY_VERSION__": f"projects/{PROJECT}/secrets/swarm-typesafe-fwd-key/versions/latest",
        "__PROJECT_ID__": PROJECT,
        "__TYPESAFE_FWD_CONFIG_SHA256__": "c" * 64,
        "__TYPESAFE_FWD_CONFIG_JSON__": config,
    }
    text = TEMPLATE.read_text()
    for key, value in values.items():
        text = text.replace(key, value)
    assert "__" not in "".join(line for line in text.splitlines() if not line.lstrip().startswith("#")), \
        "a placeholder the test does not fill"
    return text


# kubectl, answering for the swarm-typesafe-fwd Deployment from what was applied.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
py="${FAKE_PYTHON:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
deployment() {
  "${py}" - "${d}/applied.yaml" <<'PY'
import json, sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc["kind"] == "Deployment" and doc["metadata"]["name"] == "swarm-typesafe-fwd":
        print(json.dumps(doc))
PY
}
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) cat "${d}/context" ;;
  *"config view"*) cat "${d}/cluster-entry" ;;
  *"get deployment swarm-typesafe-fwd"*)
    [[ -f "${d}/applied.yaml" ]] || { echo 'Error from server (NotFound): deployments.apps "swarm-typesafe-fwd" not found' >&2; exit 1; }
    deployment ;;
  *"get pods"*"-o json"*)
    if [[ -f "${d}/pod-image-id" ]]; then image_id="$(cat "${d}/pod-image-id")"; else
      image_id="$(deployment | "${py}" -c 'import json,sys; print(json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["image"])')"; fi
    "${py}" - "${image_id}" <<'PY'
import json, sys
pod = {"metadata": {"name": "swarm-typesafe-fwd-new-a"},
       "status": {"containerStatuses": [{"name": "forwarder", "imageID": sys.argv[1]}]}}
print(json.dumps({"items": [pod]}))
PY
    ;;
  *"get pods"*) printf 'NAME                        READY   STATUS\nswarm-typesafe-fwd-new-a    0/1     CrashLoopBackOff\n' ;;
  *"rollout status deploy/swarm-typesafe-fwd"*)
    if [[ -f "${d}/rollout-fails" ]]; then echo 'error: timed out waiting for the condition' >&2; exit 1; fi
    printf 'deployment "swarm-typesafe-fwd" successfully rolled out\n' ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"apply --dry-run=server"*) cat >/dev/null; printf 'deployment.apps/swarm-typesafe-fwd configured (server dry run)\n' ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *"apply -f -"*)
    cat >"${d}/applied.yaml"
    printf 'deployment.apps/swarm-typesafe-fwd configured\n' ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

# terraform, as the real one behaves: a null output is NOT in state, so a
# named read of it fails with 'Output "..." not found'; the unnamed
# `output -json` lists the outputs state holds, without the null one. The
# `null-listed` marker lists it with a null value instead, the other shape
# "not served" may take.
FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
py="${FAKE_PYTHON:?}"
printf '%s\n' "$*" >>"${d}/terraform.log"
case "$*" in
  *" init "*) printf 'Terraform has been successfully initialized!\n' ;;
  *"output -raw swarm_typesafe_fwd_manifest"*|*"output -json swarm_typesafe_fwd_manifest"*)
    if [[ -f "${d}/state-unreadable" ]]; then printf 'Error: Failed to load state\n' >&2; exit 1; fi
    if [[ ! -f "${d}/fwd.yaml" ]]; then
      printf 'Error: Output "swarm_typesafe_fwd_manifest" not found\n' >&2
      exit 1
    fi
    case "$*" in
      *"-raw"*) cat "${d}/fwd.yaml" ;;
      *) "${py}" -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read()))' "${d}/fwd.yaml" ;;
    esac ;;
  *"output -json")
    if [[ -f "${d}/state-unreadable" ]]; then printf 'Error: Failed to load state\n' >&2; exit 1; fi
    "${py}" - "${d}" <<'PY'
import json, os, sys
d = sys.argv[1]
outs = {"gke_cluster_name": {"sensitive": False, "type": "string", "value": "swarm-autopilot"}}
f = os.path.join(d, "fwd.yaml")
if os.path.exists(f):
    outs["swarm_typesafe_fwd_manifest"] = {"sensitive": False, "type": "string", "value": open(f).read()}
elif os.path.exists(os.path.join(d, "null-listed")):
    outs["swarm_typesafe_fwd_manifest"] = {"sensitive": False, "type": "string", "value": None}
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
        (self.tmp / "tfdata").mkdir()
        self.serve(True)

    def serve(self, on: bool, manifest: str | None = None) -> None:
        if on:
            (self.dir / "fwd.yaml").write_text(manifest if manifest is not None else _fwd_manifest())
        else:
            (self.dir / "fwd.yaml").unlink(missing_ok=True)

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

    def apply(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run("--swarm-typesafe-fwd", "--context", "swarm-dev", "--cluster", CLUSTER, *args, script=APPLY)

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
        "roll-typesafe-fwd.sh must source common.sh for the deny-list and kubectl_bin"
    assert any("kubectl_bin" in line for line in body)
    assert any("SHARED_DENY_LIST" in line for line in body)
    assert any("--swarm-typesafe-fwd" in line for line in body)
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize("shape", ["missing", "null"])
def test_not_served_is_a_notice_and_exit_0_with_nothing_applied(tmp_path, shape):
    """'missing' is what terraform does with a null output (PR 1402): the
    named read fails "not found". 'null' lists it with a null value."""
    # CONTROL: served, the same release invocation applies and rolls.
    served = Cluster(tmp_path / "served")
    control = served.run("--tf-init", "--get-credentials", "--confirm")
    assert control.returncode == 0, control.stdout + control.stderr
    assert served.applied() and "rollout status" in served.log("kubectl")

    cluster = Cluster(tmp_path / "off")
    cluster.serve(False)
    if shape == "null":
        (cluster.dir / "null-listed").write_text("")
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "typesafe_forwarder.serve is off" in out, out
    assert not cluster.applied()
    # Neither gcloud nor the cluster is asked anything while not served.
    assert cluster.log("gcloud") == "", cluster.log("gcloud")
    assert cluster.log("kubectl") == "", cluster.log("kubectl")


def test_a_state_terraform_cannot_read_is_a_failure_not_off(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.serve(False)
    (cluster.dir / "state-unreadable").write_text("")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert f"could not read terraform's {OUTPUT}" in out, out
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
    assert "get-credentials" not in cluster.log("gcloud")
    assert "rollout" not in cluster.log("kubectl")


def test_a_manifest_is_applied_through_apply_sh_then_rolled_then_read_back(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (cluster.dir / "applied.yaml").read_text().strip() == _fwd_manifest().strip()

    terraform = cluster.log("terraform").splitlines()
    assert _first(terraform, "init") < _first(terraform, f"output -raw {OUTPUT}")
    gcloud = cluster.log("gcloud")
    assert f"container clusters get-credentials {CLUSTER} --project {PROJECT} --location {LOCATION}" in gcloud, gcloud

    calls = cluster.log("kubectl").splitlines()
    applied = _first(calls, "apply -f -")
    assert f"--context {CLUSTER_REF}" in calls[applied], calls[applied]
    rollout = _first(calls, "rollout status deploy/swarm-typesafe-fwd")
    assert applied < rollout, calls
    assert "-n swarm-system" in calls[rollout] and "--timeout=600s" in calls[rollout], calls[rollout]
    pods = [i for i, line in enumerate(calls) if "get pods" in line]
    assert pods and pods[-1] > rollout, calls
    assert "app.kubernetes.io/name=swarm-typesafe-fwd" in calls[pods[-1]]
    assert DIGEST in out and "1 pod(s)" in out, out


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
    # CONTROL: the unmodified fixture's pod runs the manifest's digest and passes.
    control_cluster = Cluster(tmp_path / "control")
    control = control_cluster.run("--confirm")
    assert control.returncode == 0, control.stdout + control.stderr

    cluster = Cluster(tmp_path / "stale")
    (cluster.dir / "pod-image-id").write_text(
        f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-typesafe-fwd@{STALE_DIGEST}")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "swarm-typesafe-fwd-new-a" in out and STALE_DIGEST in out and DIGEST in out, out
    assert "1 of 1" in out, out


def test_a_roll_that_does_not_finish_fails_and_shows_the_pods(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "rollout-fails").write_text("")
    result = cluster.run("--confirm", "--timeout", "30")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "did not finish" in out and "CrashLoopBackOff" in out, out


def test_an_image_not_pinned_by_digest_is_refused_before_any_apply(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.serve(True, _fwd_manifest(f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-typesafe-fwd:dev"))
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not pinned by digest" in out, out
    assert not cluster.applied()


# --- kubernetes/apply.sh --swarm-typesafe-fwd, the branch the roll drives ------

def test_apply_sh_swarm_typesafe_fwd_applies_terraforms_manifest_alone(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.apply("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (cluster.dir / "applied.yaml").read_text().strip() == _fwd_manifest().strip()
    # Only the forwarder's output: not the relay's, not the graph's, not
    # swarm-api's range.
    assert "swarm_live_manifest" not in cluster.log("terraform")
    assert "swarm_graph_manifest" not in cluster.log("terraform")
    assert "run services describe" not in cluster.log("gcloud")


@pytest.mark.parametrize("shape", ["missing", "null"])
def test_apply_sh_swarm_typesafe_fwd_with_a_null_output_applies_nothing(tmp_path, shape):
    cluster = Cluster(tmp_path)
    cluster.serve(False)
    if shape == "null":
        (cluster.dir / "null-listed").write_text("")
    result = cluster.apply("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "typesafe_forwarder.serve is off" in out, out
    assert not cluster.applied()
    assert "apply" not in cluster.log("kubectl")


def test_apply_sh_swarm_typesafe_fwd_fails_on_a_state_it_cannot_read(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.serve(False)
    (cluster.dir / "state-unreadable").write_text("")
    result = cluster.apply("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "Failed to load state" in out and "nothing was applied" in out, out
    assert not cluster.applied()


@pytest.mark.parametrize("defect", ["placeholder", "namespace", "rbac", "secret"])
def test_apply_sh_swarm_typesafe_fwd_refuses_a_manifest_that_reaches_outside(tmp_path, defect):
    # CONTROL: the unmodified manifest applies.
    control_cluster = Cluster(tmp_path / "control")
    control = control_cluster.apply("--confirm")
    assert control.returncode == 0 and control_cluster.applied(), control.stdout + control.stderr

    manifest = _fwd_manifest()
    if defect == "placeholder":
        manifest = manifest.replace(IMAGE, "__SWARM_TYPESAFE_FWD_IMAGE__")
    elif defect == "namespace":
        manifest = manifest.replace("  namespace: swarm-system\n", "  namespace: kube-system\n", 1)
    elif defect == "rbac":
        manifest += "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: x\n"
    else:
        manifest += "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n  namespace: swarm-system\n"
    assert manifest != _fwd_manifest()
    cluster = Cluster(tmp_path / "defect")
    cluster.serve(True, manifest)
    result = cluster.apply("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "nothing was applied" in out.lower(), out
    if defect == "placeholder":
        assert "__SWARM_TYPESAFE_FWD_IMAGE__" in out, out
    assert not cluster.applied()


def test_the_roll_stops_on_apply_sh_refusing_a_placeholder(tmp_path):
    """The roll script reads the image line first; a placeholder elsewhere is
    apply.sh's to refuse, and the roll then neither waits nor reads back."""
    manifest = _fwd_manifest().replace(
        f"projects/{PROJECT}/secrets/swarm-typesafe-fwd-key/versions/latest", "__TYPESAFE_FWD_KEY_VERSION__")
    cluster = Cluster(tmp_path)
    cluster.serve(True, manifest)
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "__TYPESAFE_FWD_KEY_VERSION__" in out and "was not rolled" in out, out
    assert not cluster.applied()
    assert "rollout" not in cluster.log("kubectl")


def test_apply_sh_swarm_typesafe_fwd_refuses_agents_staging(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "cluster-entry").write_text(FOREIGN_REF)
    result = cluster.apply("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "agents-staging" in out and "deny-list" in out, out
    assert not cluster.applied()
    assert OUTPUT not in cluster.log("terraform"), "the output was read for a refused cluster"


def test_apply_sh_swarm_typesafe_fwd_takes_no_tenant(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--swarm-typesafe-fwd", "--tenant", "eng", "--confirm", script=APPLY)
    assert result.returncode != 0
    assert "--swarm-typesafe-fwd" in result.stderr and "--tenant" in result.stderr, result.stderr
    assert cluster.log("kubectl") == ""
