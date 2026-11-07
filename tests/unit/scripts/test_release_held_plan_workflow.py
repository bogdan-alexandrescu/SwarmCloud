"""The held IAM plan survives the state bucket becoming a secret.

docs/design/generic-repository.md, section 2.1: the project id, the state
bucket and the CI accounts are GitHub secrets, so GitHub masks them in every
log. GitHub also DROPS any job output whose value contains a secret. The dev
plan that changes IAM is held in the state bucket by release-apply's `held`
step and applied by release.yml's `infrastructure-iam` job (dev-iam) in
another job, so its location crosses a job boundary as an output: were it the
gs:// address, dev-iam would read it empty and the plan the owner reviewed
would never apply. The output is the path under the bucket, and dev-iam
rebuilds the address from its own TF_STATE_BUCKET.

These run the two steps themselves, with a fake gcloud, and model GitHub's
rule: every output that contains the bucket is dropped before the next job.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
ACTION = REPO / ".github" / "actions" / "release-apply" / "action.yml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
ACTIONS = sorted((REPO / ".github" / "actions").glob("*/action.yml"))

#: storage cp/rm against a directory standing in for Cloud Storage: gs://B/P
#: is $FAKE_GCS/B/P. Every call is logged, one per line.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GCLOUD_LOG}"
[[ "$1" == storage ]] || exit 2
verb="$2"; shift 2
args=()
for a in "$@"; do [[ "$a" == --* ]] || args+=("$a"); done
local_of() { case "$1" in gs://*) printf '%s/%s' "${FAKE_GCS}" "${1#gs://}" ;; *) printf '%s' "$1" ;; esac; }
case "${verb}" in
  cp) src="$(local_of "${args[0]}")"; dst="$(local_of "${args[1]}")"; mkdir -p "$(dirname "${dst}")"; cp "${src}" "${dst}" ;;
  rm) rm "$(local_of "${args[0]}")" ;;
  *) exit 2 ;;
esac
"""


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step(steps: list[dict], *, id: str | None = None, name: str | None = None) -> dict:
    for step in steps:
        if (id is not None and step.get("id") == id) or (name is not None and step.get("name") == name):
            return step
    raise AssertionError(f"no step id={id!r} name={name!r}")


def _outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def _secret_bucket() -> str:
    """A bucket name no test or doc could already hold, built at run time."""
    return f"swarm-tfstate-{secrets.token_hex(6)}"


@pytest.fixture()
def box(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD, encoding="utf-8")
    gcloud.chmod(0o755)
    tf_root = tmp_path / "work" / "terraform" / "infra"
    tf_root.mkdir(parents=True)
    (tf_root / "plan.tfplan").write_bytes(secrets.token_bytes(64))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_GCS": str(tmp_path / "gcs"),
        "FAKE_GCLOUD_LOG": str(tmp_path / "gcloud.log"),
        "TF_ROOT": str(tf_root),
        "ENVIRONMENT": "dev",
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "TF_STATE_BUCKET": _secret_bucket(),
    }
    (tmp_path / "output").write_text("", encoding="utf-8")
    return {"tmp": tmp_path, "env": env, "tf_root": tf_root}


def _run(script: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    # How GitHub runs a `shell: bash` step.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=60, check=False,
    )


def _held(box: dict) -> dict[str, str]:
    step = _step(_load(ACTION)["runs"]["steps"], id="held")
    proc = _run(step["run"], box["env"], box["tmp"])
    assert proc.returncode == 0, proc.stderr
    return _outputs(Path(box["env"]["GITHUB_OUTPUT"]))


def test_the_held_plan_s_output_survives_the_bucket_being_a_secret(box):
    """MUTATION: write the gs:// address to the output again and the path
    GitHub hands dev-iam is empty."""
    bucket = box["env"]["TF_STATE_BUCKET"]
    outputs = _held(box)
    # GitHub's rule: an output that contains a secret never leaves the job.
    surviving = {k: v for k, v in outputs.items() if bucket not in v}
    path = surviving.get("path", "")
    assert path, f"the held plan's path was dropped or never written: {sorted(outputs)}"
    assert re.fullmatch(r"plans/dev/4242-1/plan\.tfplan", path), path
    assert surviving.get("sha256"), outputs
    # The plan really went to the bucket, at that path.
    held = Path(box["env"]["FAKE_GCS"]) / bucket / path
    assert held.read_bytes() == (box["tf_root"] / "plan.tfplan").read_bytes()
    # The summary is on the run's page: it names the path, never the bucket.
    assert bucket not in Path(box["env"]["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")


def test_the_output_chain_carries_the_path_to_dev_iam():
    action = _load(ACTION)
    assert action["outputs"]["plan_path"]["value"] == "${{ steps.held.outputs.path }}"
    assert "plan_uri" not in action["outputs"]
    jobs = _load(RELEASE)["jobs"]
    assert jobs["infrastructure"]["outputs"]["plan_path"] == "${{ steps.stage.outputs.plan_path }}"
    iam_env = jobs["infrastructure-iam"]["env"]
    assert iam_env["PLAN_PATH"] == "${{ needs.infrastructure.outputs.plan_path }}"
    assert iam_env["TF_STATE_BUCKET"] == "${{ secrets.TF_STATE_BUCKET }}"


def test_dev_iam_rebuilds_the_address_and_applies_exactly_the_held_plan(box):
    """The two steps end to end: what `held` wrote, dev-iam fetches by the
    path that survived, checks by sha256 and drops."""
    bucket = box["env"]["TF_STATE_BUCKET"]
    outputs = {k: v for k, v in _held(box).items() if bucket not in v}
    reviewed = (box["tf_root"] / "plan.tfplan").read_bytes()
    (box["tf_root"] / "plan.tfplan").unlink()

    steps = _load(RELEASE)["jobs"]["infrastructure-iam"]["steps"]
    env = {**box["env"], "PLAN_PATH": outputs.get("path", ""), "PLAN_SHA256": outputs.get("sha256", "")}
    fetch = _run(_step(steps, name="fetch the plan the owner reviewed")["run"], env, box["tmp"])
    assert fetch.returncode == 0, fetch.stdout + fetch.stderr
    assert (box["tf_root"] / "plan.tfplan").read_bytes() == reviewed

    drop = _run(_step(steps, name="drop the held plan")["run"], env, box["tmp"])
    assert drop.returncode == 0, drop.stdout + drop.stderr
    assert not (Path(box["env"]["FAKE_GCS"]) / bucket / outputs["path"]).exists()
    log = Path(box["env"]["FAKE_GCLOUD_LOG"]).read_text(encoding="utf-8")
    assert f"gs://{bucket}/{outputs['path']}" in log


@pytest.mark.parametrize("plan_path", ["", "plans/", "../plan.tfplan"])
def test_dev_iam_never_drops_anything_but_a_held_plan(box, plan_path: str):
    """An empty path must not become the bucket's root."""
    steps = _load(RELEASE)["jobs"]["infrastructure-iam"]["steps"]
    env = {**box["env"], "PLAN_PATH": plan_path}
    drop = _run(_step(steps, name="drop the held plan")["run"], env, box["tmp"])
    assert drop.returncode != 0
    assert not Path(box["env"]["FAKE_GCLOUD_LOG"]).exists()


def _conditions(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "if":
                yield str(value)
            yield from _conditions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _conditions(item)


@pytest.mark.parametrize("path", WORKFLOWS + ACTIONS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_condition_reads_a_secret(path: Path):
    """GitHub refuses a workflow whose `if:` reads `secrets.*`; a value that
    gates a step is mapped into `env` first (ci-fix.yml's fixer account)."""
    assert not [c for c in _conditions(_load(path)) if "secrets." in c]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_output_is_a_secret(path: Path):
    """A job output that holds a secret reaches the next job empty."""
    for name, job in (_load(path).get("jobs") or {}).items():
        for key, value in (job.get("outputs") or {}).items():
            assert "secrets." not in str(value), f"{path.name}: {name}.outputs.{key}"


def test_the_ci_fixer_is_gated_on_its_mapped_account():
    job = _load(REPO / ".github" / "workflows" / "ci-fix.yml")["jobs"]["fix"]
    assert job["env"]["SWARM_CI_FIX_SA"] == "${{ secrets.SWARM_CI_FIX_SA }}"
    auth = _step(job["steps"], id="auth")
    assert auth["if"] == "${{ env.SWARM_CI_FIX_SA != '' }}"
    assert auth["with"]["service_account"] == "${{ env.SWARM_CI_FIX_SA }}"
