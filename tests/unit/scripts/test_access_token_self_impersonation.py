"""`access_token` in scripts/lib/common.sh, when SWARM_IMPERSONATE_SA is the
account gcloud is already signed in as (#273).

`ci-fix.yml` used to reach the swarm API by impersonating SWARM_CI_FIX_SA from
the deployer's Workload Identity token. terraform/bootstrap/ci_fix.tf changed
that: the workflow now federates directly AS SWARM_CI_FIX_SA, and the deployer
takes no part. But the workflow still exports SWARM_IMPERSONATE_SA set to that
same service account, so `access_token` ran

    gcloud auth print-access-token --impersonate-service-account=<itself>

which asks gcloud to let the active account impersonate itself. gcloud refuses
that without roles/iam.serviceAccountTokenCreator granted on the account BY
ITSELF -- a grant nobody holds and self-impersonation should never need one
for.

Nothing here touches the network: `access_token` is sourced out of common.sh
and called directly, against a fake `gcloud` on PATH that records every
invocation.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"

SA = "swarm-ci-fixer@example.iam.gserviceaccount.com"

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gcloud.calls"
case "$1 $2" in
  "config get-value") printf '%s' "${ACTIVE_ACCOUNT}" ;;
  "auth print-access-token") printf 'ya29.fake-access-token' ;;
  *) echo "fake gcloud: unexpected: $*" >&2; exit 3 ;;
esac
"""

pytestmark = pytest.mark.skipif(
    not COMMON.exists() or shutil.which("bash") is None,
    reason="common.sh and bash are required",
)


def _access_token(tmp_path: Path, active_account: str) -> tuple[str, list[str]]:
    """Source common.sh and call the real `access_token`.

    Returns the token it printed and the list of `gcloud` invocations the fake
    binary recorded, each as the shell-joined argv it was called with.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gcloud = bin_dir / "gcloud"
    fake_gcloud.write_text(FAKE_GCLOUD)
    fake_gcloud.chmod(fake_gcloud.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "ACTIVE_ACCOUNT": active_account,
        "NO_COLOR": "1",
        # A path that does not exist: load_env (run unconditionally when
        # common.sh is sourced) then falls through to its defaults rather
        # than reading the operator's real .env.
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "SWARM_IMPERSONATE_SA": SA,
    }

    proc = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"; access_token', "bash"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"access_token failed:\n{proc.stdout}{proc.stderr}"

    calls_file = tmp_path / "gcloud.calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return proc.stdout, calls


def test_impersonating_a_different_account_keeps_the_flag(tmp_path) -> None:
    """The ordinary case: a laptop operator, or any caller whose active
    account is not the one it wants a token for, still impersonates it."""
    token, calls = _access_token(tmp_path, active_account="someone@example.com")
    assert token == "ya29.fake-access-token"
    impersonate_calls = [c for c in calls if "--impersonate-service-account" in c]
    assert len(impersonate_calls) == 1, calls
    assert f"--impersonate-service-account={SA}" in impersonate_calls[0]


def test_impersonating_the_active_account_itself_drops_the_flag(tmp_path) -> None:
    """The regression: ci-fix.yml federates directly as SWARM_CI_FIX_SA and
    also exports SWARM_IMPERSONATE_SA=SWARM_CI_FIX_SA. Asking gcloud to let
    that account impersonate itself is refused without
    roles/iam.serviceAccountTokenCreator on itself; the fix is to recognise
    this case and mint the token for the active account directly."""
    token, calls = _access_token(tmp_path, active_account=SA)
    assert token == "ya29.fake-access-token"
    impersonate_calls = [c for c in calls if "--impersonate-service-account" in c]
    assert not impersonate_calls, (
        f"access_token asked gcloud to impersonate its own active account: {calls}"
    )


def test_the_comparison_is_case_insensitive(tmp_path) -> None:
    """IAM service account emails are not case-sensitive; gcloud can echo
    back a different case than SWARM_IMPERSONATE_SA was set with."""
    token, calls = _access_token(tmp_path, active_account=SA.upper())
    assert token == "ya29.fake-access-token"
    impersonate_calls = [c for c in calls if "--impersonate-service-account" in c]
    assert not impersonate_calls, (
        f"a case difference alone caused self-impersonation: {calls}"
    )
