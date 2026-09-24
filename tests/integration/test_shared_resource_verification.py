"""What `scripts/destroy.sh --verify-shared` says about ANOTHER TEAM'S resources.

Driven end to end with a fake `gcloud` on PATH. Nothing is created, no credentials
are used, and the flag itself is read-only.

THE PROPERTY, and why it is worth a file of its own:

    saga-agents-staging holds a live GKE cluster, a VPC, three buckets and twelve
    service accounts that belong to promptlab, crawler, aipipeline,
    external-secrets and tournament-digest. The last thing `make destroy` does is
    assert they all survived. Every one of those lookups was

        gcloud ... >/dev/null 2>&1

    so a non-zero exit meant "deleted" -- and a non-zero exit is ALSO what an
    expired session, a disabled API, a missing permission, a network blip and a
    wrong --location produce. The script then printed

        SHARED RESOURCES ARE MISSING AFTER DESTROY: gke/agents-staging network/...
        This should be impossible -- the plan assertions passed. Escalate immediately.

    naming fifteen of another team's resources as deleted, with the real reason
    already discarded into /dev/null, at the one moment when nobody can tell
    whether it is true. `gcloud auth login` was the fix; a page was the outcome.

The three cases below are the three answers that have to stay distinguishable:
present (0), provably gone (3), could not look (4).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "destroy.sh"

PROJECT = "swarm-test-project"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or shutil.which("jq") is None,
    reason="destroy.sh and jq are both required",
)


#: Everything the neighbours own answers "present". `clusters list` prints the
#: name it matched; the describes print theirs.
GCLOUD_ALL_PRESENT = r"""#!/usr/bin/env bash
set -euo pipefail
args="$*"
case "${args}" in
  *"container clusters list"*)        echo "agents-staging" ;;
  *"compute networks describe"*)      echo "a-network" ;;
  *"storage buckets describe"*)       echo "a-bucket" ;;
  *"iam service-accounts describe"*)  echo "sa@example.iam.gserviceaccount.com" ;;
  *)                                  : ;;
esac
exit 0
"""

#: The session is dead. This is gcloud's real shape for it, and the exit status
#: is the same 1 a genuinely absent resource produces -- which is the entire
#: point: only the stderr tells them apart.
GCLOUD_EXPIRED_SESSION = r"""#!/usr/bin/env bash
set -euo pipefail
cat >&2 <<'MSG'
ERROR: (gcloud.container.clusters.list) There was a problem refreshing your current auth tokens: Reauthentication required.
Please run:
  $ gcloud auth login
MSG
exit 1
"""

#: One bucket really is gone; everything else is fine. gcloud ANSWERED here.
GCLOUD_ONE_BUCKET_DELETED = r"""#!/usr/bin/env bash
set -euo pipefail
args="$*"
case "${args}" in
  *"saga-agents-files-staging"*)
    echo "ERROR: (gcloud.storage.buckets.describe) gs://saga-agents-files-staging not found: 404." >&2
    exit 1
    ;;
  *"container clusters list"*)        echo "agents-staging" ;;
  *"compute networks describe"*)      echo "a-network" ;;
  *"storage buckets describe"*)       echo "a-bucket" ;;
  *"iam service-accounts describe"*)  echo "sa@example.iam.gserviceaccount.com" ;;
  *)                                  : ;;
esac
exit 0
"""


def _verify(tmp: Path, fake_gcloud: str) -> subprocess.CompletedProcess:
    """Run `destroy.sh --verify-shared` against a fake gcloud."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    path = bin_dir / "gcloud"
    path.write_text(fake_gcloud)
    path.chmod(0o755)

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
        [str(SCRIPT), "--verify-shared"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )


def test_every_neighbour_present_is_a_clean_pass(tmp_path) -> None:
    proc = _verify(tmp_path, GCLOUD_ALL_PRESENT)
    transcript = proc.stdout + proc.stderr

    assert proc.returncode == 0, transcript
    assert "other teams are unaffected" in transcript, transcript
    # The cluster is found by `clusters list --filter`, not by a describe that
    # has to guess a zone: a wrong --location is a NOT_FOUND for a live cluster.
    assert "clusters describe" not in transcript


def test_a_dead_session_is_not_reported_as_their_resources_being_deleted(tmp_path) -> None:
    """The regression this file exists for.

    Everything about this run is a failure to LOOK. Nothing in the output may
    claim a resource is gone, and the exit code must not be the one that means
    "escalate, their cluster was deleted".
    """
    proc = _verify(tmp_path, GCLOUD_EXPIRED_SESSION)
    transcript = proc.stdout + proc.stderr

    assert proc.returncode == 4, (
        "a failure to look must have its own exit code, distinct from 0 (verified "
        f"present) and 3 (verified gone):\n{transcript}"
    )
    assert "COULD NOT VERIFY" in transcript, transcript
    assert "ARE GONE" not in transcript, (
        "an unanswered lookup must never be rendered as a deletion:\n" + transcript
    )
    assert "MISSING AFTER DESTROY" not in transcript, transcript
    # The reason has to survive, or the operator is sent to the wrong problem.
    assert "Reauthentication required" in transcript, (
        "stderr was discarded, so the operator cannot see that this is auth:\n"
        + transcript
    )
    assert "gcloud auth login" in transcript, transcript


def test_a_resource_that_really_is_gone_still_escalates(tmp_path) -> None:
    """The other half: separating the cases must not disarm the alarm.

    gcloud answered NOT_FOUND for one of their buckets. That is the emergency the
    check was built for and it keeps its own exit code.
    """
    proc = _verify(tmp_path, GCLOUD_ONE_BUCKET_DELETED)
    transcript = proc.stdout + proc.stderr

    assert proc.returncode == 3, transcript
    assert "ARE GONE" in transcript, transcript
    assert "bucket/saga-agents-files-staging" in transcript, transcript
    assert "Escalate immediately" in transcript, transcript
    # ... and it must not be confused with the "could not look" case.
    assert "COULD NOT VERIFY" not in transcript, transcript


def test_the_verification_names_every_deny_listed_neighbour(tmp_path) -> None:
    """A check that silently stopped covering a resource is a check that lies.

    The deny-list in scripts/lib/common.sh is the inventory of what belongs to
    other teams; the post-destroy proof has to speak about all of it, or the
    resources it dropped are the ones nobody would notice losing.
    """
    proc = _verify(tmp_path, GCLOUD_ALL_PRESENT)
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript

    common = (REPO / "scripts" / "lib" / "common.sh").read_text()
    deny_block = common.split("SHARED_DENY_LIST=(", 1)[1].split(")", 1)[0]
    entries = [
        line.strip().strip('"')
        for line in deny_block.splitlines()
        if line.strip().startswith('"')
    ]

    # Subnets are covered by the PLAN guard (destroy-guard.jq matches them on the
    # network field), not by a post-apply describe, and `default` is the shared
    # network already checked under its own name.
    skip = {"agents-staging-subnet", "gke-agents-staging-88280d02-pe-subnet"}
    missing = []
    for entry in entries:
        if entry in skip:
            continue
        # Service accounts are listed by full email; the check names the local part.
        name = entry.split("@", 1)[0]
        if name not in transcript:
            missing.append(entry)

    assert not missing, (
        "these deny-listed neighbours are never looked at after a destroy: "
        + ", ".join(missing)
    )
