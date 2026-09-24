"""`scripts/bootstrap.sh --target` applies one change out of terraform/bootstrap.

WHY THIS FILE EXISTS
--------------------
terraform/bootstrap is applied by the owner, and more than one change can be
pending in it at once. On 2026-09-24 the owner's state still held
roles/storage.admin unconditioned, so `make bootstrap` for the swarm-verify log
view (verify_logs.tf) would ALSO have applied wif.tf's storage.admin scoping --
a change wif.tf says is applied only between releases.

docs/ci.md first offered a hand-written `terraform -chdir=... plan -target=...`
for that. On the owner's workstation bare `terraform` is Homebrew's 1.3.6,
which refuses this root (`required_version >= 1.9.0`); the pinned 1.16.2 in
~/.local/bin comes second on PATH. The scripts resolve it through `tf`
(common.sh, `terraform_bin`), and a runbook step that goes around the scripts
also goes around their init and their typed confirmation.

So the targeted apply is a flag on the script that already does the rest.

THE PROPERTY
------------
* every `--target ADDRESS` reaches the bootstrap PLAN as its own `-target=`
  argument, and the apply is of that saved plan -- so what the owner reviewed is
  what is applied;
* with no `--target`, no `-target=` appears anywhere: the default is still the
  whole root;
* `--target` with no address is refused before terraform runs.

The terraform on PATH is a recorder, reached through SWARM_TERRAFORM exactly as
the pinned binary would be. Nothing is planned or applied, no credentials are
used, and the real repository is never written to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

PROJECT = "swarm-test-project"
BUCKET = f"swarm-tfstate-{PROJECT}"

VIEW = "google_logging_log_view.verify"
GRANT = "google_project_iam_member.deployer_reads_verify_logs[0]"

pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "bootstrap.sh").exists()
    or shutil.which("jq") is None
    or shutil.which("curl") is None,
    reason="bootstrap.sh, jq and curl are all required",
)

# Every call on its own line, arguments separated by a unit separator so an
# address with brackets or spaces survives intact.
FAKE_TERRAFORM = """#!/usr/bin/env bash
set -euo pipefail
{
  first=1
  for a in "$@"; do
    if [[ $first -eq 0 ]]; then printf '\\037'; fi
    printf '%s' "$a"
    first=0
  done
  printf '\\n'
} >> "${FAKE_TF_LOG}"
exit 0
"""

# The bucket exists and is versioned, so the script creates nothing and the
# only branch that differs between these cases is the bootstrap layer.
FAKE_GCLOUD = f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *versioning.enabled*) echo "True" ;;
  *"storage buckets describe"*) echo "{BUCKET}" ;;
  *) : ;;
esac
exit 0
"""


def _bootstrap(tmp: Path, *args: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """Run bootstrap.sh in a sandbox repository and return every terraform call.

    A COPY of scripts/, because common.sh derives REPO_ROOT from its own path.
    The sandbox's terraform/bootstrap holds one .tf file so the bootstrap layer
    runs; terraform/infra is empty so the environment init takes its "no .tf
    files yet" path.
    """
    sandbox = tmp / "repo"
    sandbox.mkdir()
    shutil.copytree(SCRIPTS, sandbox / "scripts")
    (sandbox / ".env").write_text("")
    (sandbox / "terraform" / "bootstrap").mkdir(parents=True)
    (sandbox / "terraform" / "bootstrap" / "main.tf").write_text("# sandbox\n")
    (sandbox / "terraform" / "infra").mkdir(parents=True)
    tfvars = sandbox / "terraform" / "environments" / "dev"
    tfvars.mkdir(parents=True)
    (tfvars / "dev.tfvars").write_text("")

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    terraform = bin_dir / "terraform-recorder"
    terraform.write_text(FAKE_TERRAFORM)
    terraform.chmod(0o755)
    log = tmp / "terraform-calls"
    log.write_text("")

    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\n"
        "REGION=us-central1\n"
        "ENVIRONMENT=dev\n"
        "FIRESTORE_DATABASE=swarm\n"
    )
    env_file.chmod(0o600)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["SWARM_TERRAFORM"] = str(terraform)
    env["FAKE_TF_LOG"] = str(log)
    env["NO_COLOR"] = "1"
    # The typed "apply" needs a TTY; --yes is the script's own way past it and
    # is what these runs pass. SWARM_ASSUME_YES is never inherited.
    env.pop("SWARM_ASSUME_YES", None)

    proc = subprocess.run(
        [str(sandbox / "scripts" / "bootstrap.sh"), "--skip-prereq", *args],
        cwd=sandbox, env=env, capture_output=True, text=True, timeout=180,
        stdin=subprocess.DEVNULL,
    )
    calls = [line.split("\x1f") for line in log.read_text().splitlines() if line]
    return proc, calls


def _verb(call: list[str]) -> str:
    """The terraform subcommand, skipping global options such as -chdir."""
    return next(a for a in call if not a.startswith("-"))


def test_each_target_reaches_the_bootstrap_plan_and_the_apply_is_of_that_plan(tmp_path) -> None:
    proc, calls = _bootstrap(tmp_path, "--yes", "--target", VIEW, "--target", GRANT)
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript

    plans = [c for c in calls if _verb(c) == "plan"]
    applies = [c for c in calls if _verb(c) == "apply"]
    assert len(plans) == 1, f"expected one bootstrap plan, terraform saw: {calls}\n{transcript}"
    assert len(applies) == 1, f"expected one bootstrap apply, terraform saw: {calls}\n{transcript}"

    plan = plans[0]
    assert f"-target={VIEW}" in plan, f"the view's address never reached the plan: {plan}"
    assert f"-target={GRANT}" in plan, f"the grant's address never reached the plan: {plan}"
    assert len([a for a in plan if a.startswith("-target=")]) == 2, (
        f"exactly the two addresses asked for, nothing added: {plan}"
    )

    out = [a for a in plan if a.startswith("-out=")]
    assert out, f"the targeted plan was not saved, so the apply could not be of it: {plan}"
    saved = out[0].removeprefix("-out=")
    assert applies[0][-1] == saved, (
        f"the apply must be of the saved, targeted plan ({saved}), not a fresh one: {applies[0]}"
    )
    assert not [a for a in applies[0] if a.startswith("-target=")], (
        f"a saved plan carries its targets; the apply should not restate them: {applies[0]}"
    )

    # The owner reads this before typing "apply", so it must say the plan is partial.
    assert VIEW in transcript and GRANT in transcript, transcript


def test_with_no_target_the_whole_root_is_planned(tmp_path) -> None:
    proc, calls = _bootstrap(tmp_path, "--yes")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript

    plans = [c for c in calls if _verb(c) == "plan"]
    assert len(plans) == 1, f"expected one bootstrap plan, terraform saw: {calls}\n{transcript}"
    targeted = [a for c in calls for a in c if a.startswith("-target")]
    assert not targeted, f"a plain bootstrap must plan the whole root, and this one targeted {targeted}"


def test_a_target_with_no_address_is_refused_before_terraform_runs(tmp_path) -> None:
    proc, calls = _bootstrap(tmp_path, "--yes", "--target")
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert "--target needs a resource address" in transcript, transcript
    assert calls == [], f"terraform ran although the arguments were refused: {calls}"
