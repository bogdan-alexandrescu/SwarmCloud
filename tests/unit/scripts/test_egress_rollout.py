"""Every release rolls swarm-egress to the image it built (lane EGRESS-BUILD,
part of #1030; docs/browser-egress.md §9.3).

terraform/infra/egress.tf renders `swarm_egress_manifest` -- the StatefulSet
swarm-system/swarm-egress on swarm-autopilot, with the release's swarm-egress
digest -- and `kubernetes/apply.sh --swarm-system` applies it after the relay.
Nothing waited for it to roll or read back what it runs. `scripts/roll-egress.sh`
does, through the same implementation as the relay's roll
(`scripts/roll-live-relay.sh --component egress`), so the cluster checks and the
shared-project deny-list exist once. These tests hold:

  * the SCRIPT, against a fake cluster, terraform and gcloud: it applies through
    `kubernetes/apply.sh --swarm-system --confirm`, waits for
    `rollout status statefulset/swarm-egress`, and proves the forwarder's pods
    run the digest the manifest names; a stale pod fails it; egress switched
    off (enable_browser_egress false) is a notice, not a failure; enabled with
    no manifest (no swarm-egress digest) is a failure; and the shared
    `agents-staging` cluster -- by flag, by context, or by terraform's own
    output -- is refused before anything is written;
  * the WORKFLOW: release.yml's deploy job runs it after the relay's roll and
    before the smoke test. SwarmCloud lanes cannot push .github/workflows/, so
    that change ships as a separate `workflow:` commit the operator pushes;
    the workflow test is red until it lands.

No cluster, no credentials, no network: every binary is a fake on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_live_netpol import FAKE_API_CIDR, FAKE_SUBNET, LEAKY, _relay_manifest, _service
from .test_live_relay_rollout import FAKE_GCLOUD
from .test_release_reuses_ci_images import _workflow

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "roll-egress.sh"
TEMPLATE = REPO / "kubernetes" / "swarm-system" / "swarm-egress.yaml"

PROJECT = "saga-agents-staging"
LOCATION = "us-central1"
CLUSTER = "swarm-autopilot"
CLUSTER_REF = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"
FOREIGN_REF = f"gke_{PROJECT}_{LOCATION}-a_agents-staging"

#: Built rather than written out as one literal.
DIGEST = "sha256:" + "8" * 64
STALE_DIGEST = "sha256:" + "0" * 64
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-egress@{DIGEST}"

STEP = "roll swarm-egress to this release's image"
RELAY_STEP = "roll the live relay to this release's image"


def _egress_manifest() -> str:
    """kubernetes/swarm-system/swarm-egress.yaml with every placeholder filled,
    as terraform/infra/egress.tf fills them (egress.tftest.hcl holds that
    rendering; this only needs plausible values)."""
    config = json.dumps({"enabled": True, "tenants": ["u-bogdan"], "country": "us"})
    values = {
        "__SWARM_EGRESS_IMAGE__": IMAGE,
        "__EGRESS_KEY_VERSION__": f"projects/{PROJECT}/secrets/swarm-egress-key/versions/latest",
        "__EGRESS_CREDENTIAL_VERSION__": f"projects/{PROJECT}/secrets/swarm-egress-credential/versions/latest",
        "__EGRESS_CONFIG_SHA256__": "c" * 64,
        "__EGRESS_CONFIG_JSON__": config,
    }
    text = TEMPLATE.read_text()
    for key, value in values.items():
        text = text.replace(key, value)
    assert "__" not in "".join(line for line in text.splitlines() if not line.lstrip().startswith("#")), \
        "a placeholder the test does not fill"
    return text


# kubectl, answering for whichever StatefulSet it is asked about by name.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
py="${FAKE_PYTHON:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
statefulset() {
  "${py}" - "${d}/applied.yaml" "$1" <<'PY'
import json, sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == sys.argv[2]:
        print(json.dumps(doc))
PY
}
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) cat "${d}/context" ;;
  *"config view"*) cat "${d}/cluster-entry" ;;
  *"get namespace swarm-system"*) [[ -f "${d}/applied.yaml" ]] || exit 1; printf 'namespace/swarm-system\n' ;;
  *"get networkpolicies"*) cat "${d}/applied.json" ;;
  *"get statefulset swarm-egress"*)
    [[ -f "${d}/applied.yaml" ]] || { echo 'Error from server (NotFound): statefulsets.apps "swarm-egress" not found' >&2; exit 1; }
    statefulset swarm-egress ;;
  *"get pods"*"-o json"*)
    if [[ -f "${d}/pod-image-id" ]]; then image_id="$(cat "${d}/pod-image-id")"; else
      image_id="$(statefulset swarm-egress | "${py}" -c 'import json,sys; print(json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["image"])')"; fi
    "${py}" -c 'import json,sys; print(json.dumps({"items": [{"metadata": {"name": "swarm-egress-0"}, "status": {"containerStatuses": [{"name": "egress", "imageID": sys.argv[1]}]}}]}))' "${image_id}" ;;
  *"get pods"*) printf 'NAME             READY   STATUS\nswarm-egress-0   0/1     Pending\n' ;;
  *"rollout status statefulset/swarm-egress"*)
    if [[ -f "${d}/rollout-fails" ]]; then echo 'error: timed out waiting for the condition' >&2; exit 1; fi
    printf 'partitioned roll out complete: 1 new pods have been updated...\n' ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"apply --dry-run=server"*) cat >/dev/null; printf 'statefulset.apps/swarm-egress configured (server dry run)\n' ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *"apply -f -"*)
    cat >"${d}/applied.yaml"
    "${py}" -c 'import json,sys,yaml; print(json.dumps({"items":[d for d in yaml.safe_load_all(open(sys.argv[1])) if d]}))' \
      "${d}/applied.yaml" >"${d}/applied.json"
    printf 'statefulset.apps/swarm-egress configured\n' ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/terraform.log"
raw() {
  if [[ ! -f "${d}/$1" ]]; then
    printf 'Error: Unsupported value for raw output\n' >&2
    exit 1
  fi
  cat "${d}/$1"
}
case "$*" in
  *" init "*) printf 'Terraform has been successfully initialized!\n' ;;
  *"output -raw swarm_live_manifest"*) raw relay.yaml ;;
  *"output -raw swarm_egress_manifest"*) raw egress.yaml ;;
  *"output -json browser_egress"*) cat "${d}/browser_egress.json" ;;
  *"output -json live_relay"*) cat "${d}/live_relay.json" ;;
  *"output -raw gke_cluster_name"*) cat "${d}/gke-cluster" ;;
  *) printf 'fake terraform: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""


class Cluster:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.dir = tmp_path / "cluster"
        self.bin = tmp_path / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.dir / "context").write_text("swarm-dev")
        (self.dir / "cluster-entry").write_text(CLUSTER_REF)
        (self.dir / "gke-cluster").write_text(CLUSTER)
        (self.dir / "service.json").write_text(json.dumps(
            _service(f"projects/{PROJECT}/regions/{LOCATION}/subnetworks/{FAKE_SUBNET}")))
        (self.dir / "subnet.json").write_text(json.dumps({"name": FAKE_SUBNET, "ipCidrRange": FAKE_API_CIDR}))
        # apply.sh --swarm-system applies swarm-egress after the relay, so dev
        # has both (terraform/environments/dev/dev.tfvars).
        (self.dir / "relay.yaml").write_text(_relay_manifest())
        (self.dir / "live_relay.json").write_text(json.dumps(
            {"enabled": True, "port": 8080, "swarm_api_egress_cidr": FAKE_API_CIDR}))
        (self.tmp / "tfdata").mkdir()
        self.egress(enabled=True)

    def egress(self, *, enabled: bool) -> None:
        (self.dir / "browser_egress.json").write_text(json.dumps(
            {"enabled": enabled, "switch_on": enabled,
             "namespace": "swarm-system",  # namespace-prefix-exempt: the platform namespace, not a tenant
             "service_account": "swarm-egress", "port": 3128}))
        if enabled:
            (self.dir / "egress.yaml").write_text(_egress_manifest())
        else:
            (self.dir / "egress.yaml").unlink(missing_ok=True)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in (*LEAKY, "PYTHON_BIN", "TF_STATE_BUCKET")}
        env.update(
            PATH=os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
            FAKE_CLUSTER_DIR=str(self.dir),
            FAKE_PYTHON=sys.executable,
            PYTHON_BIN=sys.executable,
            SWARM_KUBECTL=str(self.bin / "kubectl"),
            SWARM_TERRAFORM=str(self.bin / "terraform"),
            TF_DATA_DIR=str(self.tmp / "tfdata"),
            SWARM_ENV_FILE=str(self.tmp / "no.env"),
            # common.sh has no default project (DAYONE-DESAGA-2): name the one
            # CLUSTER_REF and the deny-list cases are built on.
            PROJECT_ID=PROJECT,
            KUBECONFIG=str(self.tmp / "no-such-kubeconfig"),
            HOME=str(self.tmp),
            TMPDIR=str(self.tmp),
            NO_COLOR="1",
        )
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=REPO, env=env,
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


def test_the_script_is_an_executable_entry_to_the_one_roll_implementation():
    text = SCRIPT.read_text()
    body = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert body[0] == "set -euo pipefail", body[:2]
    assert any("roll-live-relay.sh" in line and "--component egress" in line for line in body), body
    assert not any("apply -f" in line or "kubectl" in line for line in body), \
        "roll-egress.sh restates the roll; roll-live-relay.sh owns it"
    assert os.access(SCRIPT, os.X_OK)


def test_confirm_applies_through_apply_sh_then_waits_for_the_forwarder(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert cluster.applied()
    calls = cluster.log("kubectl").splitlines()
    applied = _first(calls, "apply -f -")
    rollout = _first(calls, "rollout status statefulset/swarm-egress")
    assert applied < rollout, calls
    assert "--namespace swarm-system" in calls[rollout] and "--timeout=" in calls[rollout], calls[rollout]
    assert rollout < _first(calls, "get pods"), calls
    assert "app.kubernetes.io/name=swarm-egress" in calls[_first(calls, "get pods")]
    # It waited on swarm-egress, not on the relay.
    assert "rollout status statefulset/swarm-live" not in cluster.log("kubectl")
    assert DIGEST in out, out
    assert "swarm_egress_manifest" in cluster.log("terraform")


def test_the_release_path_inits_terraform_and_fetches_credentials_for_terraforms_cluster(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    assert result.returncode == 0, result.stdout + result.stderr
    terraform = cluster.log("terraform").splitlines()
    assert _first(terraform, "init") < _first(terraform, "output -raw swarm_egress_manifest")
    gcloud = cluster.log("gcloud")
    assert f"container clusters get-credentials {CLUSTER} --project {PROJECT} --location {LOCATION}" in gcloud, gcloud


def test_a_pod_still_on_the_old_image_fails_the_roll(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "pod-image-id").write_text(
        f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-egress@{STALE_DIGEST}")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "swarm-egress-0" in out and STALE_DIGEST in out and DIGEST in out, out


def test_a_roll_that_does_not_finish_fails_and_shows_the_pods(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "rollout-fails").write_text("")
    result = cluster.run("--confirm", "--timeout", "30")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "did not finish" in out and "Pending" in out, out


def test_egress_switched_off_is_a_notice_not_a_failure(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.egress(enabled=False)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "enable_browser_egress is off" in out and "no swarm-egress to roll" in out, out
    assert not cluster.applied()
    assert "get-credentials" not in cluster.log("gcloud")
    assert "rollout" not in cluster.log("kubectl")


def test_enabled_without_a_manifest_is_a_failure(tmp_path):
    """browser_egress says enabled but there is no manifest: the release built
    no swarm-egress digest. That is not "nothing to roll"."""
    cluster = Cluster(tmp_path)
    (cluster.dir / "egress.yaml").unlink()
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "swarm-egress digest" in out, out
    assert not cluster.applied()


@pytest.mark.parametrize("how", ["flag", "context", "terraform"])
def test_the_shared_agents_staging_cluster_is_refused_before_anything_is_written(tmp_path, how):
    """agents-staging is another team's live cluster (CLAUDE.md rule 2),
    refused by common.sh's deny-list whichever way it arrives."""
    cluster = Cluster(tmp_path)
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
    assert "run services describe" not in cluster.log("gcloud"), "apply.sh ran for a refused cluster"


def test_an_unknown_component_is_refused():
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "roll-live-relay.sh"), "--component", "agents-staging"],
        env={**{k: v for k, v in os.environ.items() if k not in LEAKY}, "PROJECT_ID": PROJECT, "NO_COLOR": "1"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode != 0
    assert "--component" in proc.stderr, proc.stderr


def test_the_release_rolls_swarm_egress_after_the_relay():
    steps = _workflow("release.yml")["jobs"]["deploy"]["steps"]
    names = [s.get("name") for s in steps]
    assert STEP in names, f"the deploy job has no step named {STEP!r}"
    at = names.index(STEP)
    step = steps[at]
    run = str(step.get("run", ""))
    assert "./scripts/roll-egress.sh" in run, run
    for flag in ("--confirm", "--get-credentials", "--tf-init"):
        assert flag in run, f"the egress step does not pass {flag}"
    # After the relay (apply.sh applies swarm-egress after it), before the smoke test.
    assert at > max(i for i, n in enumerate(names) if n == RELAY_STEP)
    assert at < names.index("smoke test (inside the VPC)")
    when = str(step.get("if", ""))
    assert "steps.verify.outcome == 'success'" in when and "!cancelled()" in when, when
    assert not step.get("continue-on-error"), "a forwarder that did not roll would pass the release"
    assert ".venv/bin/python" in str((step.get("env") or {}).get("PYTHON_BIN", ""))
