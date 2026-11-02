"""Acceptance cancels only the swarm-verify executions an accept run started
(#1822 comment 31, part of #1822).

THE DEFECT. accept.yml's `quiesce` job listed EVERY running swarm-verify
execution and cancelled each, and each suite job's `if: cancelled()` step
cancelled every running one created after the job started. An operator's
`make verify-remote` running at the same time was cancelled with them, and
verify-remote.sh then reported its non-zero exit as "FAILED", which read as a
regression.

THE FIX, held here:

* verify-remote.sh marks every execution it starts with the environment
  override `SWARM_VERIFY_ORIGIN` (`gcloud run jobs execute` has no label
  flag; `--update-env-vars` is the override it does take, and the execution
  records it in its template): `operator` unless the caller says otherwise,
  and accept.yml says `accept-<run id>` for the whole workflow;
* the quiesce step cancels only executions whose origin names ANOTHER accept
  run and prints every other one as skipped; each cleanup step cancels only
  its own run's, created since the job started;
* on a non-zero exit verify-remote.sh describes the execution and reports a
  cancelled one as CANCELLED, apart from FAILED.

The accept.yml steps are run here as written, by bash, against a fake
`gcloud` on PATH: no network, no credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from .test_release_reuses_ci_images import _code, _workflow

REPO = Path(__file__).resolve().parents[3]
VERIFY_REMOTE = REPO / "scripts" / "verify-remote.sh"
ORIGIN = "SWARM_VERIFY_ORIGIN"
#: The jobs that run swarm-verify executions and so clean up after a cancel.
SUITE_JOBS = ("smoke", "acceptance", "acceptance-2", "tenant-isolation", "live-browser")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="the steps and verify-remote.sh need bash and jq",
)

#: The fake gcloud. `executions list` prints FAKE_LIST (a JSON file) in the
#: format asked for (JSON, or the value() columns), `executions cancel` and every call are recorded, `execute` exits
#: FAKE_EXECUTE_RC naming FAKE_EXECUTION, `executions describe` prints
#: FAKE_DESCRIBE.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gcloud.calls"
case "$1 $2 $3 ${4:-}" in
  "run jobs executions list")
    case "$*" in
      *"value(metadata.name,metadata.creationTimestamp)"*)
        jq -r '.[] | "\(.metadata.name)\t\(.metadata.creationTimestamp)"' "${FAKE_LIST}" ;;
      *"value(metadata.name)"*) jq -r '.[].metadata.name' "${FAKE_LIST}" ;;
      *"--format json"*) cat "${FAKE_LIST}" ;;
      *) echo "fake gcloud: unexpected format: $*" >&2; exit 3 ;;
    esac ;;
  "run jobs executions cancel") printf '%s\n' "$5" >>"${FAKE_DIR}/cancelled" ;;
  "run jobs executions describe") cat "${FAKE_DESCRIBE}" ;;
  "run jobs execute "*)
    if [[ "${FAKE_EXECUTE_RC:-0}" != "0" ]]; then
      echo "ERROR: (gcloud.run.jobs.execute) The execution failed."
      echo "View details about this execution by running:"
      echo "gcloud run jobs executions describe ${FAKE_EXECUTION}"
      exit "${FAKE_EXECUTE_RC}"
    fi
    echo "fake execution ok" ;;
  "logging read "*) echo "[]" ;;
  *) echo "fake gcloud: unexpected: $*" >&2; exit 3 ;;
esac
"""


def _execution(name: str, created: str, origin: str | None) -> dict:
    env = [{"name": "SWARM_ENVIRONMENT", "value": "dev"}]
    if origin is not None:
        env.append({"name": ORIGIN, "value": origin})
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "spec": {"template": {"spec": {"containers": [{"args": ["scripts/smoke-test.sh"], "env": env}]}}},
        "status": {"runningCount": 1},
    }


#: Four running executions: an older accept run's, this run's (started after
#: the job), an operator's, and one started before the origin existed.
RUNNING = [
    _execution("swarm-verify-old11", "2026-10-28T10:00:05Z", "accept-111"),
    _execution("swarm-verify-mine2", "2026-10-28T10:05:00Z", "accept-222"),
    _execution("swarm-verify-oper3", "2026-10-28T10:05:01Z", "operator"),
    _execution("swarm-verify-bare4", "2026-10-28T10:05:02Z", None),
]


def _bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gcloud"
    fake.write_text(FAKE_GCLOUD)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    listing = tmp_path / "list.json"
    listing.write_text(json.dumps(RUNNING))
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        "PATH": f"{_bin(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmpdir),
        "NO_COLOR": "1",
        "FAKE_DIR": str(tmp_path),
        "FAKE_LIST": str(listing),
        "REGION": "us-central1",
        "PROJECT_ID": "swarm-test-project",
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "SWARM_VERIFY_LOG_WAIT_SECONDS": "0",
    }
    env.update(extra)
    return env


def _cancelled(tmp_path: Path) -> list[str]:
    path = tmp_path / "cancelled"
    return path.read_text().split() if path.exists() else []


def _jobs() -> dict:
    return _workflow("accept.yml")["jobs"]


def _cancel_steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps") or [] if "gcloud run jobs executions cancel" in _code(s.get("run", ""))]


def _run_step(step: dict, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    """The step's run: as GitHub runs it (`bash -e`), with its own env: and
    the workflow's SWARM_VERIFY_ORIGIN for run 222."""
    env = _env(tmp_path, **{ORIGIN: "accept-222", "GITHUB_RUN_ID": "222"})
    for key, value in (step.get("env") or {}).items():
        env[key] = str(value)
    env.update(extra)
    return subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# accept.yml
# ---------------------------------------------------------------------------


def test_every_execution_an_accept_run_starts_is_marked_with_its_run_id():
    """MUTATION: drop the workflow-level origin, or set it in one job only. An
    execution started without it reads as an operator's and is never
    cancelled by the next accept run's quiesce."""
    workflow = _workflow("accept.yml")
    assert workflow["env"][ORIGIN] == "accept-${{ github.run_id }}"
    for job_id, job in workflow["jobs"].items():
        assert ORIGIN not in (job.get("env") or {}), f"{job_id} overrides the run's origin"
        for step in job.get("steps") or []:
            code = _code(step.get("run", ""))
            assert "gcloud run jobs execute " not in code, (
                f"{job_id} executes a job directly: go through verify-remote.sh, which marks it")
            if "verify-remote.sh" in code:
                assert ORIGIN not in (step.get("env") or {}), f"{job_id} overrides the run's origin"


def test_the_quiesce_step_cancels_only_other_accept_runs_executions(tmp_path):
    """MUTATION: list and cancel every running execution again (today's main):
    the operator's and the unmarked execution are cancelled and this fails."""
    step = next(s for s in _jobs()["quiesce"]["steps"] if "swarm-verify executions still running" in str(s.get("name")))
    proc = _run_step(step, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _cancelled(tmp_path) == ["swarm-verify-old11"]
    out = proc.stdout + proc.stderr
    for name in ("swarm-verify-oper3", "swarm-verify-bare4"):
        assert f"skipped swarm-verify execution {name}" in out, out


@pytest.mark.parametrize("job_id", SUITE_JOBS)
def test_a_cleanup_step_cancels_only_its_own_runs_executions(job_id, tmp_path):
    """MUTATION: drop the origin test from a cleanup step. Every execution
    created after the job started -- the operator's among them -- is
    cancelled, not only this run's."""
    steps = _cancel_steps(_jobs()[job_id])
    assert len(steps) == 1, job_id
    proc = _run_step(steps[0], tmp_path, STARTED="2026-10-28T10:01:00Z")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _cancelled(tmp_path) == ["swarm-verify-mine2"], job_id


# ---------------------------------------------------------------------------
# verify-remote.sh
# ---------------------------------------------------------------------------


def _verify(tmp_path: Path, *targets: str, **extra: str) -> subprocess.CompletedProcess:
    describe = tmp_path / "describe.json"
    if not describe.exists():
        describe.write_text("{}")
    env = _env(tmp_path, FAKE_DESCRIBE=str(describe), FAKE_EXECUTION="swarm-verify-x7k2q", **extra)
    env.pop("PROJECT_ID")
    env.pop("REGION")
    return subprocess.run(["bash", str(VERIFY_REMOTE), *targets], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)


def _executes(tmp_path: Path) -> list[str]:
    return [line for line in (tmp_path / "gcloud.calls").read_text().splitlines()
            if line.startswith("run jobs execute ")]


def test_an_operators_execution_is_marked_operator(tmp_path):
    """MUTATION: execute without the override. Nothing tells an accept run's
    quiesce that the execution is not its own to cancel."""
    proc = _verify(tmp_path, "smoke-test")
    assert proc.returncode == 0, proc.stderr
    (call,) = _executes(tmp_path)
    assert f"--update-env-vars {ORIGIN}=operator" in call, call


def test_an_accept_runs_execution_carries_its_run_id(tmp_path):
    proc = _verify(tmp_path, "smoke-test", **{ORIGIN: "accept-222"})
    assert proc.returncode == 0, proc.stderr
    (call,) = _executes(tmp_path)
    assert f"--update-env-vars {ORIGIN}=accept-222" in call, call


@pytest.mark.parametrize("origin", ["accept-", "accept-12,X=1", "Operator", "accept-1 2"])
def test_a_malformed_origin_is_refused_before_anything_runs(origin, tmp_path):
    """A comma would add an environment variable of the caller's choosing to
    the execution; anything else would be unselectable by the quiesce."""
    proc = _verify(tmp_path, "smoke-test", **{ORIGIN: origin})
    assert proc.returncode != 0
    calls = tmp_path / "gcloud.calls"
    assert not calls.exists() or not _executes(tmp_path)
    assert ORIGIN in proc.stderr


def test_a_cancelled_execution_reads_cancelled_not_failed(tmp_path):
    """MUTATION: report every non-zero exit as FAILED (today's main)."""
    (tmp_path / "describe.json").write_text(json.dumps({
        "metadata": {"name": "swarm-verify-x7k2q"},
        "status": {"cancelledCount": 1, "failedCount": 0, "conditions": [
            {"type": "Completed", "status": "False", "reason": "Cancelled",
             "message": "The execution was cancelled."}]},
    }))
    proc = _verify(tmp_path, "smoke-test", FAKE_EXECUTE_RC="1")
    assert proc.returncode != 0
    assert "smoke-test CANCELLED" in proc.stderr, proc.stderr
    assert "smoke-test FAILED" not in proc.stderr, proc.stderr
    assert "The execution was cancelled." in proc.stderr
    assert "cancelled: smoke-test" in proc.stderr, proc.stderr


def test_a_failed_execution_still_reads_failed(tmp_path):
    (tmp_path / "describe.json").write_text(json.dumps({
        "metadata": {"name": "swarm-verify-x7k2q"},
        "status": {"failedCount": 1, "conditions": [
            {"type": "Completed", "status": "False", "reason": "NonZeroExitCode",
             "message": "Task swarm-verify-x7k2q-task0 failed with exit code 1."}]},
    }))
    proc = _verify(tmp_path, "smoke-test", FAKE_EXECUTE_RC="1")
    assert proc.returncode != 0
    assert "smoke-test FAILED (exit 1)" in proc.stderr, proc.stderr
    assert "CANCELLED" not in proc.stderr
