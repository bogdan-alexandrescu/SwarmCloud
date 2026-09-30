"""verify-remote.sh, when a target's name contains a slash (#327 release run
36678195824, at 0ede37b).

Main's "acceptance (dev)" job runs `verify-remote.sh acceptance/mock
acceptance/generic ...` -- see test_release_acceptance_job.py, which pins that
those are exactly the arguments release.yml passes. The job failed before any
suite ran:

    ./scripts/verify-remote.sh: line 212: /tmp/swarm-verify-acceptance/mock.2283:
    No such file or directory

Cause: `out="${TMPDIR:-/tmp}/swarm-verify-${target}.$$"` builds the per-target
temp path directly from the target's name. For "acceptance/mock" that is
"/tmp/swarm-verify-acceptance/mock.$$", which points *inside* a directory
("swarm-verify-acceptance") that nothing ever creates -- the redirection into
it fails outright, so the target never runs and the script exits without ever
invoking `gcloud`.

This runs the real script against a fake `gcloud` on PATH (following the
pattern in test_access_token_self_impersonation.py: a recording shell stub,
no network, no credentials) with a slash-named target, and asserts it reaches
and executes `scripts/acceptance/mock.sh` instead of dying on the temp path.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VERIFY_REMOTE = REPO / "scripts" / "verify-remote.sh"

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gcloud.calls"
case "$1 $2 $3" in
  "run jobs execute")
    echo "fake execution ok"
    exit 0
    ;;
  *) echo "fake gcloud: unexpected: $*" >&2; exit 3 ;;
esac
"""

pytestmark = pytest.mark.skipif(
    not VERIFY_REMOTE.exists() or shutil.which("bash") is None,
    reason="verify-remote.sh and bash are required",
)


def _run_verify_remote(tmp_path: Path, *targets: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gcloud = bin_dir / "gcloud"
    fake_gcloud.write_text(FAKE_GCLOUD)
    fake_gcloud.chmod(fake_gcloud.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # A dedicated TMPDIR that verify-remote.sh's own temp path is built
    # under, so the test observes exactly the directory-that-does-not-exist
    # failure the release run hit, without touching the real /tmp.
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "TMPDIR": str(tmpdir),
        "NO_COLOR": "1",
        # A path that does not exist: load_env falls through to its
        # defaults (PROJECT_ID, REGION, ...) rather than reading the
        # operator's real .env.
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
    }

    proc = subprocess.run(
        ["bash", str(VERIFY_REMOTE), *targets],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    calls_file = tmp_path / "gcloud.calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return proc, calls


def test_a_slash_named_target_still_runs_and_reaches_its_wrapper(tmp_path) -> None:
    proc, calls = _run_verify_remote(tmp_path, "acceptance/mock")

    assert proc.returncode == 0, (
        f"verify-remote.sh failed on a slash-named target instead of running it:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "No such file or directory" not in proc.stderr, proc.stderr

    execute_calls = [c for c in calls if c.startswith("run jobs execute")]
    assert len(execute_calls) == 1, calls
    assert "--args scripts/acceptance/mock.sh" in execute_calls[0], (
        f"verify-remote.sh never reached scripts/acceptance/mock.sh: {calls}"
    )


def test_several_slash_named_targets_each_run_in_turn(tmp_path) -> None:
    proc, calls = _run_verify_remote(tmp_path, "acceptance/mock", "acceptance/generic")

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    execute_calls = [c for c in calls if c.startswith("run jobs execute")]
    assert len(execute_calls) == 2, calls
    assert any("--args scripts/acceptance/mock.sh" in c for c in execute_calls), calls
    assert any("--args scripts/acceptance/generic.sh" in c for c in execute_calls), calls
