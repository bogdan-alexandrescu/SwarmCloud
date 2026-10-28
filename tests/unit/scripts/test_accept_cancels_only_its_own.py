"""Acceptance cancels only the swarm-verify executions an accept run started (#1822).

Measured 2026-10-21 (epic #1822, comment 31): an operator's `make
verify-remote` execution was cancelled in flight by a release's acceptance.
accept.yml's quiesce listed every swarm-verify execution with
`status.runningCount>0` and cancelled each, whoever had started it, and the
`if: cancelled()` steps cancelled every running execution created after their
job started -- an operator's included. verify-remote.sh then printed the
cancelled target as `FAILED`, which reads as a regression.

`gcloud run jobs execute` takes no labels (its only per-execution overrides
are --args, --update-env-vars, --tasks and --task-timeout), so the mark is
the environment override SWARM_VERIFY_ORIGIN: `accept-run-<run id>` for an
accept run, `operator` otherwise. accept.yml's quiesce and its
`if: cancelled()` steps filter on it; their own scripts are read out of
accept.yml and run here against a fake gcloud.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
VERIFY_REMOTE = REPO / "scripts" / "verify-remote.sh"
ACCEPT = REPO / ".github" / "workflows" / "accept.yml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None,
                                reason="bash and jq are required")

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gcloud.calls"
case "$1 $2 $3 ${4:-}" in
  "run jobs execute "*)
    if [[ "${FAKE_EXECUTE}" == ok ]]; then echo "fake execution ok"; exit 0; fi
    echo "X Executing job failed. Run: gcloud run jobs executions describe swarm-verify-abc12"
    exit 1 ;;
  "run jobs executions list") cat "${FAKE_DIR}/executions.json" ;;
  "run jobs executions describe") cat "${FAKE_DIR}/describe.json" ;;
  "run jobs executions cancel") echo "cancelled $5" ;;
  "logging read "*) echo "[]" ;;
  *) echo "fake gcloud: unexpected: $*" >&2; exit 3 ;;
esac
"""


def _execution(name: str, origin: str | None, created: str = "2026-10-28T10:00:00.000000Z") -> dict:
    env = [{"name": "SWARM_ACCEPTANCE_TENANT", "value": "smoke"}]
    if origin is not None:
        env.append({"name": "SWARM_VERIFY_ORIGIN", "value": origin})
    return {"metadata": {"name": name, "creationTimestamp": created},
            "spec": {"template": {"spec": {"containers": [{"image": "x", "env": env}]}}},
            "status": {"runningCount": 1}}


def _fake_gcloud(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(gcloud.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run(tmp_path: Path, *args: str, env_extra: dict | None = None,
         executions: list | None = None, describe: dict | None = None,
         execute: str = "ok") -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = _fake_gcloud(tmp_path)
    (tmp_path / "executions.json").write_text(json.dumps(executions or []))
    (tmp_path / "describe.json").write_text(json.dumps(describe or {}))
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "FAKE_DIR": str(tmp_path), "TMPDIR": str(tmpdir), "NO_COLOR": "1",
           "FAKE_EXECUTE": execute, "SWARM_VERIFY_LOG_WAIT_SECONDS": "0",
           "SWARM_ENV_FILE": str(tmp_path / "no.env")}
    env.update(env_extra or {})
    proc = subprocess.run(["bash", str(VERIFY_REMOTE), *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)
    calls_file = tmp_path / "gcloud.calls"
    return proc, calls_file.read_text().splitlines() if calls_file.exists() else []


def _cancelled(calls: list[str]) -> list[str]:
    return [c.split()[4] for c in calls if c.startswith("run jobs executions cancel")]


# ---------------------------------------------------------------------------
# verify-remote.sh marks its executions
# ---------------------------------------------------------------------------


def test_an_operators_execution_is_marked_operator(tmp_path) -> None:
    proc, calls = _run(tmp_path, "smoke-test")
    assert proc.returncode == 0, proc.stderr
    (execute,) = [c for c in calls if c.startswith("run jobs execute")]
    assert "--update-env-vars SWARM_VERIFY_ORIGIN=operator" in execute, execute


def test_an_accept_runs_execution_is_marked_with_its_run_id(tmp_path) -> None:
    proc, calls = _run(tmp_path, "acceptance/mock", env_extra={"SWARM_VERIFY_ORIGIN": "accept-run-4242"})
    assert proc.returncode == 0, proc.stderr
    (execute,) = [c for c in calls if c.startswith("run jobs execute")]
    assert "SWARM_VERIFY_ORIGIN=accept-run-4242" in execute, execute
    assert "--args scripts/acceptance/mock.sh" in execute, execute


def test_a_malformed_origin_is_refused_before_anything_runs(tmp_path) -> None:
    proc, calls = _run(tmp_path, "smoke-test", env_extra={"SWARM_VERIFY_ORIGIN": "accept run,X=1"})
    assert proc.returncode != 0
    assert not [c for c in calls if c.startswith("run jobs execute")], calls


# ---------------------------------------------------------------------------
# A cancelled execution is reported as cancelled, not as failed
# ---------------------------------------------------------------------------


def test_a_cancelled_execution_is_reported_cancelled_not_failed(tmp_path) -> None:
    describe = {"metadata": {"name": "swarm-verify-abc12"},
                "status": {"cancelledCount": 1, "failedCount": 0,
                           "conditions": [{"type": "Completed", "status": "False", "reason": "Cancelled",
                                           "message": "The execution was cancelled."}]}}
    proc, _ = _run(tmp_path, "smoke-test", describe=describe, execute="fail")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "smoke-test CANCELLED" in out, out
    assert "smoke-test FAILED" not in out, out
    assert "The execution was cancelled." in out, out


def test_a_failed_execution_is_still_reported_failed(tmp_path) -> None:
    describe = {"metadata": {"name": "swarm-verify-abc12"},
                "status": {"failedCount": 1,
                           "conditions": [{"type": "Completed", "status": "False", "reason": "NonZeroExitCode",
                                           "message": "Task swarm-verify-abc12-task0 failed."}]}}
    proc, _ = _run(tmp_path, "smoke-test", describe=describe, execute="fail")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "smoke-test FAILED (exit 1)" in out, out
    assert "CANCELLED" not in out, out


# ---------------------------------------------------------------------------
# accept.yml: every execution is marked, and each cancel is filtered by it.
# The step scripts are read out of the workflow and run against the fake.
# ---------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(ACCEPT.read_text())


def _steps() -> list[tuple[str, dict]]:
    return [(name, step) for name, job in _workflow()["jobs"].items() for step in job.get("steps") or []]


def _quiesce_run() -> str:
    (step,) = [s for s in _workflow()["jobs"]["quiesce"]["steps"]
               if "swarm-verify executions still running" in str(s.get("name", ""))]
    return step["run"]


def _cancel_steps() -> list[tuple[str, dict]]:
    return [(j, s) for j, s in _steps() if str(s.get("if", "")).replace(" ", "") == "${{cancelled()}}"]


def _run_step(tmp_path: Path, script: str, executions: list, env_extra: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = _fake_gcloud(tmp_path)
    (tmp_path / "gcloud.calls").unlink(missing_ok=True)
    (tmp_path / "executions.json").write_text(json.dumps(executions))
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "FAKE_DIR": str(tmp_path), "FAKE_EXECUTE": "ok", "REGION": "us-central1", "PROJECT_ID": "p"}
    env.update(env_extra)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    proc = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)
    calls_file = tmp_path / "gcloud.calls"
    return proc, calls_file.read_text().splitlines() if calls_file.exists() else []


def test_accept_marks_every_execution_with_its_run_id() -> None:
    env = _workflow()["env"]
    assert env.get("SWARM_VERIFY_ORIGIN") == "accept-run-${{ github.run_id }}", env
    # Nothing in the file executes swarm-verify except through verify-remote.sh,
    # which sends the mark (test_an_accept_runs_execution_is_marked_with_its_run_id).
    for job, step in _steps():
        assert "jobs execute" not in str(step.get("run", "")), (job, step.get("name"))


def test_every_cancel_in_accept_filters_on_the_mark() -> None:
    cancelling = [(j, s) for j, s in _steps() if "executions cancel" in str(s.get("run", ""))]
    assert len(cancelling) >= 6, [j for j, _ in cancelling]
    for job, step in cancelling:
        assert 'select(.name == "SWARM_VERIFY_ORIGIN")' in step["run"], (job, step.get("name"))


def test_the_quiesce_cancels_only_other_accept_runs_and_names_what_it_skipped(tmp_path) -> None:
    executions = [_execution("swarm-verify-acc01", "accept-run-111"),
                  _execution("swarm-verify-op001", "operator"),
                  _execution("swarm-verify-old01", None),
                  _execution("swarm-verify-self1", "accept-run-999")]
    proc, calls = _run_step(tmp_path, _quiesce_run(), executions, {"SWARM_VERIFY_ORIGIN": "accept-run-999"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _cancelled(calls) == ["swarm-verify-acc01"], calls
    assert "skipped swarm-verify execution swarm-verify-op001 (operator)" in out, out
    assert "skipped swarm-verify execution swarm-verify-old01 (unmarked)" in out, out
    assert "cancelled 1 running swarm-verify execution(s); left 2 not started by an accept run" in out, out


def test_every_cancel_step_cancels_only_this_runs_executions_since_its_job_started(tmp_path) -> None:
    executions = [_execution("swarm-verify-mine1", "accept-run-999", "2026-10-28T10:05:00.000000Z"),
                  _execution("swarm-verify-early", "accept-run-999", "2026-10-28T09:55:00.000000Z"),
                  _execution("swarm-verify-op001", "operator", "2026-10-28T10:06:00.000000Z"),
                  _execution("swarm-verify-old01", None, "2026-10-28T10:06:30.000000Z"),
                  _execution("swarm-verify-other", "accept-run-111", "2026-10-28T10:07:00.000000Z")]
    steps = _cancel_steps()
    assert len(steps) >= 5, [j for j, _ in steps]
    for job, step in steps:
        proc, calls = _run_step(tmp_path, step["run"], executions,
                                {"SWARM_VERIFY_ORIGIN": "accept-run-999", "STARTED": "2026-10-28T10:00:00Z"})
        assert proc.returncode == 0, (job, proc.stderr)
        assert _cancelled(calls) == ["swarm-verify-mine1"], (job, calls)


def test_the_roles_probes_settings_reach_the_job_only_when_set(tmp_path) -> None:
    # The swarm-verify job reads no environment of the runner's: what the
    # roles probe needs (scripts/acceptance/groups/roles.sh) goes in the
    # execution's own overrides, and an unset one is not sent as empty.
    proc, calls = _run(tmp_path, "acceptance/groups/roles",
                       env_extra={"SWARM_ACCEPTANCE_ROLES_TENANT": "roles-probe"})
    assert proc.returncode == 0, proc.stderr
    (execute,) = [c for c in calls if c.startswith("run jobs execute")]
    assert "SWARM_ACCEPTANCE_ROLES_TENANT=roles-probe" in execute, execute
    assert "SWARM_ACCEPTANCE_ROLES_ADMIN_SA" not in execute, execute
