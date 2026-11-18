"""verify-remote.sh hands the generic smoke fixture to the swarm-verify execution
(part of the dev acceptance issue, 2026-11-18).

scripts/smoke-test.sh runs inside the swarm-verify Cloud Run job, where there is
no GITHUB_REPOSITORY and no git origin, so scripts/lib/testlib.sh's generic
fixture could never resolve and the smoke stopped on "set
SWARM_SMOKE_FIXTURE_REPOSITORY". verify-remote.sh knows the repository in CI,
so it passes it in with `--update-env-vars`, as the https URL testlib uses
verbatim, with no userinfo; a caller's own choice is left alone.

Runs the real script against a fake `gcloud` on PATH, as
test_verify_remote_slash_targets.py does: no network, no credentials.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

_PLACEHOLDERS_SPEC = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_PLACEHOLDERS = importlib.util.module_from_spec(_PLACEHOLDERS_SPEC)
_PLACEHOLDERS_SPEC.loader.exec_module(_PLACEHOLDERS)
#: REGION, ZONE and a never-touch list served for the project a child names
#: (common.sh has no default for either); see tests/unit/scripts/placeholder_deployment.py.
placeholder_deployment = _PLACEHOLDERS.deployment()
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


def _run_verify_remote(tmp_path: Path, *targets: str, extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
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
        # A path that does not exist: load_env reads nothing rather than
        # the operator's real .env. PROJECT_ID, REGION and ZONE have no
        # default, so the test names the placeholder deployment's.
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "PROJECT_ID": placeholder_deployment.PROJECT,
        **placeholder_deployment.child_env(placeholder_deployment.PROJECT),
        **(extra_env or {}),
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


SETTING = "SWARM_SMOKE_FIXTURE_REPOSITORY"
SLUG = "example-org/example-repo"


def _execution_env(calls: list[str]) -> dict[str, str]:
    execute = [c for c in calls if c.startswith("run jobs execute")]
    assert len(execute) == 1, calls
    flag = execute[0].split("--update-env-vars ", 1)[1].split(" --wait", 1)[0]
    return dict(pair.split("=", 1) for pair in flag.split(","))


def test_the_execution_carries_the_repository_when_github_repository_is_set(tmp_path) -> None:
    """MUTATION: stop forwarding it and the job's smoke dies on the unset fixture again."""
    proc, calls = _run_verify_remote(tmp_path, "smoke-test", extra_env={"GITHUB_REPOSITORY": SLUG})

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    env = _execution_env(calls)
    assert env[SETTING] == f"https://github.com/{SLUG}.git", env
    assert "@" not in env[SETTING], "userinfo must never reach the job"
    assert "SWARM_SMOKE_FIXTURE_REF" not in env, env


def test_the_execution_carries_no_fixture_when_neither_is_set(tmp_path) -> None:
    proc, calls = _run_verify_remote(tmp_path, "smoke-test")

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert SETTING not in _execution_env(calls)
    assert SETTING not in "\n".join(calls)


def test_a_callers_own_fixture_wins_over_github_repository(tmp_path) -> None:
    fork = "https://github.com/example-org/example-fork.git"
    proc, calls = _run_verify_remote(
        tmp_path, "smoke-test", extra_env={"GITHUB_REPOSITORY": SLUG, SETTING: fork}
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert SETTING not in _execution_env(calls), "the job's own value is the template's, not ours to override"


def test_a_github_repository_that_is_not_owner_repo_is_refused(tmp_path) -> None:
    """MUTATION: drop the check and a comma or userinfo in it adds variables to the execution."""
    proc, calls = _run_verify_remote(
        tmp_path, "smoke-test", extra_env={"GITHUB_REPOSITORY": "user:pw@example.invalid/x,EVIL=1"}
    )

    assert proc.returncode != 0
    assert "GITHUB_REPOSITORY must be owner/repo" in proc.stderr, proc.stderr
    assert calls == []
