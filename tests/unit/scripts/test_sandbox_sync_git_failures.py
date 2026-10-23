"""sandbox-sync.sh says which git call failed, and why (#1822).

Accept run 39682791678 (2026-10-21 22:00Z) printed "ok ... already matches
this commit", then `Process completed with exit code 128` and nothing else:
step 3's `git log --diff-filter=A` ran inside `$(...)` and its `git ls-remote`
sent stderr to /dev/null and piped into awk, both under `set -e` and pipefail
with no `|| die`. So the run could not say which call failed or why, and the
acceptance groups never ran.

These run the real script, offline, against a local bare repository standing
in for the sandbox (git's `url.<base>.insteadOf`, set through the
environment, sends the script's github.com URL there) and a curl that answers
the fixture-issue read from a file. A `git` on PATH in front of the real one
fails exactly one call, the way a network error would, with a credential in
its stderr: the die line must name the call, carry git's last stderr line,
and carry it through `redact`.

Every fake credential is built at run time (CLAUDE.md, "Nothing you add may
look like a credential").
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from .test_acceptance_target import FAKE_GH_CURL, SANDBOX, _clean_env, _gh_key

ROOT = Path(__file__).resolve().parents[3]
SYNC = ROOT / "scripts" / "acceptance" / "sandbox-sync.sh"
FIXTURES = ROOT / "tests" / "acceptance" / "fixtures"
BASE_BRANCH = "acceptance/git-tools-base"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("jq") is None, reason="git and jq are required")

#: Fails the one git call whose arguments include FAKE_GIT_FAIL, as git does
#: when the network goes (exit 128, a `fatal:` line last), with the leaked URL
#: in its stderr. Every other call is the real git.
FAKE_GIT = r"""#!/usr/bin/env bash
if [[ -n "${FAKE_GIT_FAIL:-}" ]]; then
  for arg in "$@"; do
    if [[ "${arg}" == "${FAKE_GIT_FAIL}" ]]; then
      printf 'error: RPC failed; curl 56 recv failure\n' >&2
      printf 'fatal: unable to access %s: The requested URL returned error: 403\n' "${FAKE_GIT_LEAK}" >&2
      exit 128
    fi
  done
fi
exec "${REAL_GIT}" "$@"
"""


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def _sandbox(tmp_path: Path) -> tuple[Path, str]:
    """A bare sandbox whose main is: one commit without the fixtures, then one
    adding this checkout's fixtures. Returns it and the first commit, which is
    where the base branch must end up."""
    bare = tmp_path / "sandbox.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--quiet", "--bare", str(bare))
    _git(tmp_path, "init", "--quiet", str(seed))
    (seed / "README.md").write_text("sandbox\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "first")
    first = _git(seed, "rev-parse", "HEAD")
    shutil.copytree(FIXTURES, seed / "tests" / "acceptance" / "fixtures")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "fixtures")
    _git(seed, "push", "--quiet", str(bare), "HEAD:refs/heads/main")
    return bare, first


def _sync(tmp_path: Path, *, fail: str = "") -> tuple[subprocess.CompletedProcess, Path, str, str]:
    bare, first = _sandbox(tmp_path)
    gh = tmp_path / "gh"
    gh.mkdir()
    (gh / _gh_key(f"repos/{SANDBOX}/issues/1")).write_text(json.dumps(
        {"number": 1, "title": "sandbox-probe.sh is the acceptance suite's fixture issue", "body": ""}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("curl", FAKE_GH_CURL), ("git", FAKE_GIT)):
        fake = bin_dir / name
        fake.write_text(text)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    token = "t" + secrets.token_hex(12)
    leaked = "ghp_" + secrets.token_hex(18)
    env = _clean_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path), TMPDIR=str(tmp_path),
        FAKE_DIR=str(tmp_path), SWARM_ENV_FILE=str(tmp_path / "no.env"),
        SWARM_ACCEPTANCE_GITHUB_API="http://gh.invalid", SWARM_ACCEPTANCE_GITHUB_TOKEN=token,
        REAL_GIT=shutil.which("git") or "git", FAKE_GIT_FAIL=fail,
        FAKE_GIT_LEAK=f"https://x-access-token:{leaked}@github.com/{SANDBOX}.git/",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.file://{bare}.insteadOf",
        GIT_CONFIG_VALUE_0=f"https://x-access-token@github.com/{SANDBOX}.git")
    env = {k: v for k, v in env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    result = subprocess.run(["bash", str(SYNC)], cwd=ROOT, env=env, capture_output=True, text=True,
                            timeout=120, check=False)
    return result, bare, first, leaked


def test_the_harness_reaches_step_3_and_points_the_base_branch(tmp_path):
    """The control: with no call failing, the same harness runs all three steps,
    so a failure below is the failed call's and not the harness's."""
    result, bare, first, _ = _sync(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"pointed {BASE_BRANCH}" in result.stderr, result.stderr
    assert _git(tmp_path, "--git-dir", str(bare), "rev-parse", f"refs/heads/{BASE_BRANCH}") == first


@pytest.mark.parametrize(("fail", "names"), [
    ("ls-remote", f"could not read {BASE_BRANCH}"),
    ("--diff-filter=A", "could not find the commit that added"),
])
def test_a_failing_step_3_git_call_dies_naming_the_call_and_git_s_reason(tmp_path, fail, names):
    result, _, _, leaked = _sync(tmp_path, fail=fail)
    # MUTATION: drop the `|| die` on that call -- set -e ends the script with git's 128 instead.
    assert result.returncode == 1, f"exit {result.returncode}:\n{result.stderr}"
    # MUTATION: a die that does not say which call failed -- the line is not there.
    assert names in result.stderr, result.stderr
    # MUTATION: keep `2>/dev/null` (or drop the stderr from the message) -- git's reason is lost.
    assert "The requested URL returned error: 403" in result.stderr, result.stderr
    # MUTATION: print git's stderr without `redact` -- the credential in it reaches the log.
    assert leaked not in result.stderr + result.stdout
    assert "already matches this commit" in result.stderr or "pushed tests/acceptance/fixtures" in result.stderr, \
        "the failure was not in step 3"
