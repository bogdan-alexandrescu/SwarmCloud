"""What `scripts/bootstrap.sh` says about the Terraform state bucket.

Driven end to end with a fake `gcloud` on PATH, in a throwaway copy of
`scripts/`. Nothing is created, no credentials are used, and the real repository
is never written to.

THE PROPERTY. The state bucket is the one thing whose loss is not recoverable by
replay -- without it terraform no longer knows which resources in a SHARED
project are ours -- so bootstrap reports whether object versioning is on. The
check was:

    gcloud storage buckets describe ... --format='value(versioning.enabled)' \
      2>/dev/null | grep -qi true

which is the same collapse `_shared_probe` exists to prevent one file over: a
describe that FAILED -- expired session, missing permission, API not enabled --
produces no "true" on stdout, so the script printed

    object versioning is OFF on gs://<bucket>; a corrupted state file would be
    unrecoverable

about a bucket it had never managed to read, with the real reason already in
/dev/null. `TF_STATE_BUCKET` is overridable and bootstrap.sh has a branch for it
being a bucket shared with another team, so that false sentence can be printed
about theirs.

The three cases below are the three answers that have to stay distinguishable:
versioning on, versioning off, and could not look.

The fake fails only the versioning read and lets the existence read succeed. That
is the shape that reaches the check under test; the point being asserted is what
the script says when a read does not answer, not which of the several reasons a
read does not answer.
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

pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "bootstrap.sh").exists()
    or shutil.which("jq") is None
    or shutil.which("curl") is None,
    reason="bootstrap.sh, jq and curl are all required",
)


def _fake_gcloud(versioning: str) -> str:
    """A gcloud whose versioning read answers `versioning`.

    `describe --format='value(name)'` (does the bucket exist) always succeeds, so
    the script does not try to create anything. The versioning read is the one
    the cases differ on.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'args="$*"\n'
        'case "${args}" in\n'
        "  *versioning.enabled*)\n"
        f"{versioning}\n"
        "    ;;\n"
        '  *"storage buckets describe"*) echo "'
        + BUCKET
        + '" ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n"
    )


ANSWERS_TRUE = '    echo "True"'
ANSWERS_FALSE = '    echo ""'
# gcloud's real shape for a dead session, and the exit status is the same 1 a
# genuine "versioning is off" would have produced through the old pipeline.
CANNOT_LOOK = """    cat >&2 <<'MSG'
ERROR: (gcloud.storage.buckets.describe) There was a problem refreshing your current auth tokens: Reauthentication required.
Please run:
  $ gcloud auth login
MSG
    exit 1"""


def _bootstrap(tmp: Path, fake_gcloud: str) -> subprocess.CompletedProcess:
    """Run bootstrap.sh in a sandbox repository built under `tmp`.

    A COPY of scripts/, because common.sh derives REPO_ROOT from its own path:
    running the real one would write `.env` into the working tree and init
    terraform in it. The sandbox has an empty `terraform/infra`, so both
    terraform branches take their "no .tf files yet" path and no terraform
    binary is needed.
    """
    sandbox = tmp / "repo"
    sandbox.mkdir()
    shutil.copytree(SCRIPTS, sandbox / "scripts")
    (sandbox / ".env").write_text("")  # present, so nothing is copied over it
    (sandbox / "terraform" / "infra").mkdir(parents=True)
    tfvars = sandbox / "terraform" / "environments" / "dev"
    tfvars.mkdir(parents=True)
    (tfvars / "dev.tfvars").write_text("")

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(fake_gcloud)
    gcloud.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
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
    env["NO_COLOR"] = "1"

    return subprocess.run(
        [str(sandbox / "scripts" / "bootstrap.sh"), "--skip-prereq"],
        cwd=sandbox, env=env, capture_output=True, text=True, timeout=180,
    )


def test_versioning_on_is_reported_as_on(tmp_path) -> None:
    proc = _bootstrap(tmp_path, _fake_gcloud(ANSWERS_TRUE))
    transcript = proc.stdout + proc.stderr

    assert proc.returncode == 0, transcript
    assert "object versioning is on" in transcript, transcript
    assert "is OFF" not in transcript, transcript


def test_versioning_off_is_still_reported_as_off(tmp_path) -> None:
    """Separating the cases must not disarm the warning that was there."""
    proc = _bootstrap(tmp_path, _fake_gcloud(ANSWERS_FALSE))
    transcript = proc.stdout + proc.stderr

    assert proc.returncode == 0, transcript
    assert "is OFF on gs://" in transcript, transcript
    assert "unrecoverable" in transcript, transcript
    assert "could NOT read" not in transcript, (
        "gcloud answered; this is a measurement, not a failure to look:\n" + transcript
    )


def test_a_read_that_did_not_answer_is_not_reported_as_versioning_off(tmp_path) -> None:
    """The regression this file exists for.

    Nothing about this run establishes anything about the bucket. The script may
    not state a fact about it, and it must surface the reason -- which is the
    operator's actual next action.
    """
    proc = _bootstrap(tmp_path, _fake_gcloud(CANNOT_LOOK))
    transcript = proc.stdout + proc.stderr

    assert "could NOT read the versioning setting" in transcript, transcript
    assert "is OFF" not in transcript, (
        "a lookup that did not answer must never be rendered as a measurement:\n"
        + transcript
    )
    assert "object versioning is on" not in transcript, transcript
    assert "Reauthentication required" in transcript, (
        "stderr was discarded, so the operator cannot see that this is auth:\n"
        + transcript
    )
    assert "gcloud auth login" in transcript, transcript
    # Not fatal: an unreadable versioning flag is not a reason to refuse to
    # bootstrap, and the old code did not treat it as one either.
    assert proc.returncode == 0, transcript
