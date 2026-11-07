"""Every release rolls the live relay to the image it built (lane OPS-RELAY, part
of #1030).

MEASURED 2026-10-12 (visual QA pass 5, task_8be4d2c60e834faca479): the release
never redeployed swarm-live, the StatefulSet `swarm-system/swarm-live` on
swarm-autopilot. Its manifest is rendered by Terraform (terraform/infra/live.tf,
output `swarm_live_manifest`, with the release's swarm-live digest) and was
applied only by `kubernetes/apply.sh --swarm-system --confirm`, by hand. Twice
that day the relay ran a pre-change image until the operator applied it, and the
console's origin/device stayed "Not reported".

`scripts/roll-live-relay.sh` is what the release now runs after the digests are
verified. These tests hold:

  * the SCRIPT, against a fake cluster, terraform and gcloud: it applies through
    `kubernetes/apply.sh --swarm-system --confirm` (not a second copy of it), so
    the relay manifest checks and the live-ingress parity check run; it then
    waits for `rollout status` and proves the relay's pods run the digest the
    manifest names; a stale pod, a rollout that does not finish, and the shared
    `agents-staging` cluster -- by flag, by context, or by terraform's own
    output -- fail it; a dry run writes nothing; a relay switched off
    (enable_live_browser false) is a notice, not a failure;
  * the WORKFLOWS: release.yml's and hotfix.yml's deploy jobs name the step and
    the script, after the digests are verified and before the smoke test, on
    the project's synced Python.

SwarmCloud lanes cannot push .github/workflows/, so the workflow change ships as
a patch the operator applies (release-yml.patch in the lane's artifacts); the
workflow tests are red until it is applied.

No cluster, no credentials, no network: every binary is a fake on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from .test_live_netpol import FAKE_API_CIDR, FAKE_SUBNET, LEAKY, _relay_manifest, _service
from .test_release_reuses_ci_images import _workflow

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "roll-live-relay.sh"

PROJECT = "saga-agents-staging"
LOCATION = "us-central1"
CLUSTER = "swarm-autopilot"
CLUSTER_REF = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"
FOREIGN_REF = f"gke_{PROJECT}_{LOCATION}-a_agents-staging"

#: The digest _relay_manifest() renders, built rather than written out.
DIGEST = "sha256:" + "7" * 64
STALE_DIGEST = "sha256:" + "3" * 64

STEP = "roll the live relay to this release's image"
INVOCATION = "./scripts/roll-live-relay.sh"


# --- the script, against a fake cluster ---------------------------------------

FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
py="${FAKE_PYTHON:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) cat "${d}/context" ;;
  *"config view"*) cat "${d}/cluster-entry" ;;
  *"get namespace swarm-system"*) [[ -f "${d}/applied.yaml" ]] || exit 1; printf 'namespace/swarm-system\n' ;;
  *"get networkpolicies"*) cat "${d}/applied.json" ;;
  *"get statefulset swarm-live"*)
    [[ -f "${d}/applied.yaml" ]] || { echo 'Error from server (NotFound): statefulsets.apps "swarm-live" not found' >&2; exit 1; }
    "${py}" - "${d}/applied.yaml" <<'PY'
import json, sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc["kind"] == "StatefulSet":
        print(json.dumps(doc))
PY
    ;;
  *"get pods"*"-o json"*)
    if [[ -f "${d}/pod-image-id" ]]; then image_id="$(cat "${d}/pod-image-id")"; else
      image_id="$("${py}" - "${d}/applied.yaml" <<'PY'
import sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc["kind"] == "StatefulSet":
        print(doc["spec"]["template"]["spec"]["containers"][0]["image"])
PY
)"; fi
    "${py}" -c 'import json,sys; print(json.dumps({"items": [{"metadata": {"name": "swarm-live-0"}, "status": {"containerStatuses": [{"name": "relay", "imageID": sys.argv[1]}]}}]}))' "${image_id}" ;;
  *"get pods"*) printf 'NAME           READY   STATUS\nswarm-live-0   0/1     Pending\n' ;;
  *"exec "*"swarm_live.status"*)
    # The relay's counts: line N of relay-status for the Nth read, the last repeating.
    [[ -f "${d}/relay-status" ]] || { echo 'error: unable to upgrade connection' >&2; exit 1; }
    n=0; [[ -f "${d}/status-reads" ]] && n="$(cat "${d}/status-reads")"
    printf '%s\n' "$((n + 1))" >"${d}/status-reads"
    "${py}" -c 'import sys; L=open(sys.argv[1]).read().splitlines(); print(L[min(int(sys.argv[2]), len(L) - 1)])' \
      "${d}/relay-status" "${n}" ;;
  *"rollout status statefulset/swarm-live"*)
    if [[ -f "${d}/rollout-fails" ]]; then echo 'error: timed out waiting for the condition' >&2; exit 1; fi
    printf 'partitioned roll out complete: 1 new pods have been updated...\n' ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"apply --dry-run=server"*) cat >/dev/null; printf 'statefulset.apps/swarm-live configured (server dry run)\n' ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *"apply -f -"*)
    cat >"${d}/applied.yaml"
    "${py}" -c 'import json,sys,yaml; print(json.dumps({"items":[d for d in yaml.safe_load_all(open(sys.argv[1])) if d]}))' \
      "${d}/applied.yaml" >"${d}/applied.json"
    printf 'statefulset.apps/swarm-live configured\n' ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/gcloud.log"
case "$*" in
  *"auth list"*) : ;;
  *"components install gke-gcloud-auth-plugin"*) : ;;
  *"container clusters get-credentials"*) : ;;
  *"run services describe swarm-api"*) cat "${d}/service.json" ;;
  *"compute networks subnets describe"*) cat "${d}/subnet.json" ;;
  *) printf 'fake gcloud: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/terraform.log"
case "$*" in
  *" init "*) printf 'Terraform has been successfully initialized!\n' ;;
  *"output -raw swarm_live_manifest"*)
    if [[ ! -f "${d}/relay.yaml" ]]; then
      printf 'Error: Unsupported value for raw output\n' >&2
      exit 1
    fi
    cat "${d}/relay.yaml" ;;
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
        (self.dir / "relay.yaml").write_text(_relay_manifest())
        # apply.sh --swarm-system refuses an uninitialised terraform/infra
        # first; the fake `init` writes nothing, so its data directory exists.
        (self.tmp / "tfdata").mkdir()
        self.relay(enabled=True)

    def relay(self, *, enabled: bool) -> None:
        (self.dir / "live_relay.json").write_text(json.dumps(
            {"enabled": enabled, "port": 8080, "swarm_api_egress_cidr": FAKE_API_CIDR}))
        if not enabled:
            (self.dir / "relay.yaml").unlink(missing_ok=True)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in (*LEAKY, "PYTHON_BIN", "TF_STATE_BUCKET")}
        # common.sh has no default project any more: name the one the suite models.
        env["PROJECT_ID"] = "saga-agents-staging"
        env.update(
            PATH=os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
            FAKE_CLUSTER_DIR=str(self.dir),
            FAKE_PYTHON=sys.executable,
            PYTHON_BIN=sys.executable,
            SWARM_KUBECTL=str(self.bin / "kubectl"),
            SWARM_TERRAFORM=str(self.bin / "terraform"),
            TF_DATA_DIR=str(self.tmp / "tfdata"),
            SWARM_ENV_FILE=str(self.tmp / "no.env"),
            KUBECONFIG=str(self.tmp / "no-such-kubeconfig"),
            HOME=str(self.tmp),
            TMPDIR=str(self.tmp),
            NO_COLOR="1",
        )
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=REPO, env=env,
            capture_output=True, text=True, timeout=300, check=False,
        )

    def running(self, digest: str) -> None:
        """A relay already deployed, its StatefulSet and pod on `digest`."""
        (self.dir / "applied.yaml").write_text(_relay_manifest().replace(DIGEST, digest))

    def statuses(self, *counts: tuple[int, int]) -> None:
        """What the relay's /healthz says on each read: (viewers, controlled)."""
        (self.dir / "relay-status").write_text("".join(
            json.dumps({"ok": True, "tunnels": 1, "viewers": v, "controlled": c}) + "\n" for v, c in counts))

    def forget_calls(self) -> None:
        for name in ("kubectl.log", "status-reads"):
            (self.dir / name).unlink(missing_ok=True)

    def log(self, binary: str) -> str:
        path = self.dir / f"{binary}.log"
        return path.read_text() if path.exists() else ""

    def applied(self) -> bool:
        return (self.dir / "applied.yaml").exists()


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _first(lines: list[str], needle: str) -> int:
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert hits, f"no call containing {needle!r} in {lines}"
    return hits[0]


def test_confirm_applies_through_apply_sh_then_waits_for_the_rollout(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert cluster.applied()
    calls = _lines(cluster.log("kubectl"))
    applied = _first(calls, "apply -f -")
    # apply.sh's live-ingress parity check ran, against the range it read.
    parity = _first(calls, "get networkpolicies -n swarm-system -o json")
    assert f"matches {FAKE_API_CIDR}" in out, out
    rollout = _first(calls, "rollout status statefulset/swarm-live")
    assert applied < parity < rollout, calls
    assert "--namespace swarm-system" in calls[rollout] and "--timeout=" in calls[rollout], calls[rollout]
    assert f"--context {CLUSTER_REF}" in calls[rollout] or "--context swarm-dev" in calls[rollout], calls[rollout]
    # Then the pods are read, and they run the manifest's digest.
    assert rollout < _first(calls, "get pods"), calls
    assert DIGEST in out, out


def test_the_relay_manifest_is_applied_by_apply_sh_and_not_restated():
    text = SCRIPT.read_text()
    assert "kubernetes/apply.sh" in text and "--swarm-system" in text
    body = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("apply -f" in line for line in body), "the script applies a manifest itself; apply.sh owns that"
    assert any("source" in line and "lib/common.sh" in line for line in body)
    assert any("kubectl_bin" in line for line in body)
    assert os.access(SCRIPT, os.X_OK)


def test_the_release_path_inits_terraform_and_fetches_credentials_for_terraforms_cluster(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    assert result.returncode == 0, result.stdout + result.stderr
    terraform = _lines(cluster.log("terraform"))
    init = _first(terraform, "init")
    assert "-input=false" in terraform[init]
    assert f"-backend-config=bucket=swarm-tfstate-{PROJECT}" in terraform[init], terraform[init]
    assert "-backend-config=prefix=infra/dev" in terraform[init], terraform[init]
    assert init < _first(terraform, "output -raw swarm_live_manifest")
    gcloud = cluster.log("gcloud")
    assert f"container clusters get-credentials {CLUSTER} --project {PROJECT} --location {LOCATION}" in gcloud, gcloud
    # get-credentials writes gcloud's own context name, and the script uses it.
    assert f"--context {CLUSTER_REF}" in cluster.log("kubectl")


def test_a_dry_run_previews_and_neither_applies_nor_waits(tmp_path):
    cluster = Cluster(tmp_path)
    result = cluster.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not cluster.applied()
    assert (cluster.dir / "applied-dry-run.yaml").exists()
    assert "rollout status" not in cluster.log("kubectl")


def test_a_pod_still_on_the_old_image_fails_the_rollout(tmp_path):
    """The 2026-10-12 state: the manifest names the new digest, the relay runs
    the old one. That is a failure, not a pass."""
    cluster = Cluster(tmp_path)
    (cluster.dir / "pod-image-id").write_text(
        f"us-central1-docker.pkg.dev/{PROJECT}/swarm-images/swarm-live@{STALE_DIGEST}")
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "swarm-live-0" in out and STALE_DIGEST in out and DIGEST in out, out


def test_a_rollout_that_does_not_finish_fails_and_shows_the_pods(tmp_path):
    cluster = Cluster(tmp_path)
    (cluster.dir / "rollout-fails").write_text("")
    result = cluster.run("--confirm", "--timeout", "30")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--timeout=30s" in cluster.log("kubectl")
    assert "did not finish" in out, out
    assert "Pending" in out, out


def test_a_relay_switched_off_is_a_notice_not_a_failure(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.relay(enabled=False)
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "no relay to roll" in out, out
    assert not cluster.applied()
    assert "get-credentials" not in cluster.log("gcloud")
    assert "rollout" not in cluster.log("kubectl")


def test_enabled_without_a_manifest_is_a_failure(tmp_path):
    """live_relay says enabled but there is no manifest: the release built no
    swarm-live digest. That is not "nothing to roll"."""
    cluster = Cluster(tmp_path)
    (cluster.dir / "relay.yaml").unlink()
    result = cluster.run("--confirm")
    assert result.returncode != 0
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


# --- the workflows ------------------------------------------------------------


def _deploy_steps(workflow: str) -> list[dict[str, Any]]:
    return _workflow(workflow)["jobs"]["deploy"]["steps"]


def _index(steps: list[dict[str, Any]], name: str) -> int:
    names = [s.get("name") for s in steps]
    assert name in names, f"the deploy job has no step named {name!r}"
    return names.index(name)


@pytest.mark.parametrize("workflow", ["release.yml", "hotfix.yml"])
def test_the_deploy_job_rolls_the_relay(workflow):
    steps = _deploy_steps(workflow)
    at = _index(steps, STEP)
    step = steps[at]
    run = str(step.get("run", ""))
    assert INVOCATION in run, run
    for flag in ("--confirm", "--get-credentials", "--tf-init"):
        assert flag in run, f"{workflow}: the relay step does not pass {flag}"
    # After the digests are proven, before anything dispatches.
    assert at > _index(steps, "verify every service, job and worker image runs the pinned digests")
    assert at > _index(steps, "apply the tenant network policies to every tenant namespace")
    assert at < _index(steps, "smoke test (inside the VPC)")
    when = str(step.get("if", ""))
    # `_workflow` flattens release-verify, so `steps.verify.outputs.superseded`
    # reads as the expression the action declares for it.
    assert "steps.verify.outcome == 'success'" in when and "superseded == 'true') != 'true'" in when, when
    assert "!cancelled()" in when, when
    # A relay that did not roll fails the release: that is the defect this fixes.
    assert not step.get("continue-on-error"), f"{workflow}: a relay that did not roll would pass the release"


@pytest.mark.parametrize("workflow", ["release.yml", "hotfix.yml"])
def test_the_relay_step_runs_on_the_projects_synced_python_and_terraform(workflow):
    steps = _deploy_steps(workflow)
    at = _index(steps, STEP)
    before = steps[:at]
    assert any(str(s.get("uses", "")).startswith("hashicorp/setup-terraform@") for s in before)
    # apply.sh runs render.py and network_parity.py on ${PYTHON_BIN:-python3}.
    # The tenant-namespace setup is switched by vars.APPLY_TENANT_NAMESPACES;
    # the relay's is not, so its own setup must not be either.
    uv = [s for s in before if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")
          and "APPLY_TENANT_NAMESPACES" not in str(s.get("if", ""))]
    sync = [s for s in before if "uv sync --frozen" in str(s.get("run", ""))
            and "APPLY_TENANT_NAMESPACES" not in str(s.get("if", ""))]
    assert uv and sync, f"{workflow}: no unswitched uv setup and sync before the relay step"
    assert ".venv/bin/python" in str((steps[at].get("env") or {}).get("PYTHON_BIN", ""))


# --- RELAY-ROLL: only a new image rolls it, never under someone's hands -------
#
# MEASURED 2026-10-12/14: the relay restarted 40 times in ~48 h, because every
# release rolled it, and each roll dropped every live session and control.
# MUTATIONS: drop the digest comparison (the first test applies); drop the
# defer loop (the defer tests see no exec and no "deferring").


def test_a_relay_already_on_the_rendered_digest_is_not_rolled_and_both_digests_are_printed(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.running(DIGEST)
    cluster.statuses((0, 0))
    result = cluster.run("--tf-init", "--get-credentials", "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    calls = cluster.log("kubectl")
    assert "apply -f -" not in calls and "rollout status" not in calls, calls
    assert "--dry-run" not in calls, "apply.sh ran for a relay that is not rolled"
    assert "not rolled" in out and "already runs" in out, out
    assert f"rendered digest  {DIGEST}" in out and f"running digest   {DIGEST}" in out, out


def test_a_relay_on_another_digest_is_rolled_and_both_digests_are_printed(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.running(STALE_DIGEST)
    cluster.statuses((0, 0))
    result = cluster.run("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert f"rendered digest  {DIGEST}" in out and f"running digest   {STALE_DIGEST}" in out, out
    calls = _lines(cluster.log("kubectl"))
    assert _first(calls, "swarm_live.status") < _first(calls, "apply -f -") < _first(calls, "rollout status")
    assert "nobody is watching or in control" in out, out


def test_force_rolls_a_relay_already_on_the_rendered_digest(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.running(DIGEST)
    cluster.statuses((0, 0))
    result = cluster.run("--confirm", "--force")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "rollout status" in cluster.log("kubectl")


def test_the_roll_waits_while_a_person_is_in_control_then_rolls_once_they_leave(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.running(STALE_DIGEST)
    cluster.statuses((1, 1), (1, 0), (0, 0))
    result = cluster.run("--confirm", "--defer-timeout", "30", "--defer-poll", "1")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (cluster.dir / "status-reads").read_text().strip() == "3"
    assert "deferring the roll: 1 viewer(s) connected, 1 session(s) in a person's control" in out, out
    assert "nobody is watching or in control" in out and "rolling anyway" not in out, out
    calls = _lines(cluster.log("kubectl"))
    status = [i for i, line in enumerate(calls) if "swarm_live.status" in line]
    assert len(status) == 3 and status[-1] < _first(calls, "apply -f -"), calls
    # Read inside the relay's own pod and container, through the API's exec.
    assert all("exec swarm-live-0" in calls[i] and "-c relay" in calls[i]
               and "--namespace swarm-system" in calls[i] for i in status), calls


def test_the_roll_waits_a_bounded_time_then_rolls_anyway_and_says_why(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.running(STALE_DIGEST)
    cluster.statuses((2, 1))
    result = cluster.run("--confirm", "--defer-timeout", "2", "--defer-poll", "1")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "rolling anyway" in out and "2 viewer(s)" in out, out
    assert (cluster.dir / "status-reads").read_text().strip() == "3"  # at 0 s, 1 s and 2 s
    assert "rollout status" in cluster.log("kubectl")


def test_an_unreadable_count_is_said_and_does_not_block_the_roll(tmp_path):
    cluster = Cluster(tmp_path)
    cluster.running(STALE_DIGEST)
    result = cluster.run("--confirm", "--defer-poll", "1")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "could not read the relay's counts" in out, out
    assert "rollout status" in cluster.log("kubectl")


def test_the_default_wait_fits_the_release_steps_timeout():
    """release.yml gives the step 20 minutes; the wait plus the rollout must fit."""
    text = SCRIPT.read_text()
    defer = int(next(line.split("=")[1] for line in text.splitlines() if line.startswith("DEFER_TIMEOUT=")))
    rollout = int(next(line.split("=")[1] for line in text.splitlines() if line.startswith("TIMEOUT=")))
    assert 0 < defer and defer + rollout < 20 * 60

