"""`scripts/verify-remote.sh` must say WHY a target failed, not only that it did.

WHY THIS FILE EXISTS
--------------------
Release 36038727721 ran the smoke suite inside the VPC and the release log said
this, in full, about why the deploy was red:

    Executing job failed
    ERROR: (gcloud.run.jobs.execute) The execution failed.
    View details about this execution by running:
    gcloud run jobs executions describe swarm-verify-m9prt
     fail smoke-test FAILED (exit 1)

The suite's own transcript -- 9 of 11 passed, and the two failures were "could
not read the runner-profile catalogue" and "the backend matrix visited 0
backends" -- went to Cloud Logging, which the release log does not show. The
diagnosis took a second person with a second tool and the execution name copied
out by hand.

verify-remote.sh now names the execution from gcloud's own output and prints
that execution's stdout/stderr tail from Cloud Logging, oldest first, through
`redact`. These cases drive the real script with a fake `gcloud` on PATH that
plays both `run jobs execute --wait` and `logging read`, and applies the
filter it is given -- so the audit events that share the execution's label are
only kept out of the transcript if the script's filter keeps them out.

WHAT IT CANNOT PROVE: that the real Cloud Logging returns entries in this
shape. The entries are copied from the real swarm-verify-m9prt execution
(read with `gcloud logging read` on 2026-09-24), trimmed, plus one line
carrying a token-shaped string to prove the redaction.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify-remote.sh"

PROJECT = "swarm-test-project"
REGION = "us-central1"
EXECUTION = "swarm-verify-m9prt"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="verify-remote.sh needs bash and jq",
)

#: gcloud, as far as verify-remote.sh uses it.
#:
#:   run jobs execute swarm-verify ... --args scripts/<target>.sh --wait
#:       fails, in gcloud's exact words, for a target in FAKE_FAILING_TARGETS;
#:       FAKE_EXECUTE_NAMES_NO_EXECUTION fails before an execution exists.
#:   logging read FILTER --project P --order desc --limit N --format json
#:       answers FAKE_LOG_ENTRIES (newest first, as Cloud Logging does),
#:       keeping only entries whose execution label the filter names and --
#:       when the filter has one -- whose logName contains the `logName:"..."`
#:       substring. FAKE_LOG_READ=denied refuses; =empty answers [].
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >>"${FAKE_GCLOUD_LOG}"

if [[ "$1 $2 $3" == "run jobs execute" ]]; then
  target=""; prev=""
  for a in "$@"; do [[ "${prev}" == "--args" ]] && target="${a}"; prev="${a}"; done
  echo "Creating execution..."
  if [[ -n "${FAKE_EXECUTE_NAMES_NO_EXECUTION:-}" ]]; then
    echo "ERROR: (gcloud.run.jobs.execute) PERMISSION_DENIED: Permission 'run.jobs.run' denied on resource" >&2
    exit 1
  fi
  if grep -qxF "${target}" "${FAKE_FAILING_TARGETS}"; then
    cat >&2 <<EOF
Provisioning resources.....done
Starting execution.....
Running execution.....
Executing job failed
ERROR: (gcloud.run.jobs.execute) The execution failed.
View details about this execution by running:
gcloud run jobs executions describe ${FAKE_EXECUTION}

Or visit https://console.cloud.google.com/run/jobs/executions/details/${FAKE_REGION}/${FAKE_EXECUTION}?project=209012342332
EOF
    exit 1
  fi
  echo "Execution [swarm-verify-ok000] has successfully completed."
  exit 0
fi

if [[ "$1 $2" == "logging read" ]]; then
  filter="$3"; limit=1000; prev=""
  for a in "$@"; do [[ "${prev}" == "--limit" ]] && limit="${a}"; prev="${a}"; done
  case "${FAKE_LOG_READ:-ok}" in
    denied)
      echo "ERROR: (gcloud.logging.read) PERMISSION_DENIED: Permission 'logging.logEntries.list' denied on resource (or it may not exist)." >&2
      exit 1 ;;
    empty)
      echo '[]'; exit 0 ;;
  esac
  execution="$(printf '%s' "${filter}" | sed -nE 's/.*"run\.googleapis\.com\/execution_name"="([^"]+)".*/\1/p')"
  lname="$(printf '%s' "${filter}" | sed -nE 's/.*logName:"([^"]+)".*/\1/p')"
  jq --arg e "${execution}" --arg l "${lname}" --argjson n "${limit}" '
    [ .[] | select(.labels["run.googleapis.com/execution_name"] == $e)
          | select($l == "" or (.logName | contains($l))) ] | .[:$n]' "${FAKE_LOG_ENTRIES}"
  exit 0
fi

echo "the fake gcloud has no answer for: $*" >&2
exit 2
"""

TRIPWIRE = r"""#!/usr/bin/env bash
echo "$(basename "$0") must not be reached by verify-remote.sh" >&2
exit 127
"""

#: Oldest first, as the suite wrote them. The fake serves them newest first.
TRANSCRIPT = [
    ("stderr", "== Smoke test: saga-agents-staging / dev =="),
    ("stderr", "[5] Baseline capacity before submitting"),
    ("stderr", "  PASS baseline captured"),
    ("stderr", "  FAIL could not read the runner-profile catalogue; this suite cannot know which backends exist"),
    ("stderr", "       header = \"Authorization: Bearer ya29.a0AfB_byC-leaked-access-token-value\""),
    ("stderr", "[6] Every execution backend was exercised"),
    ("stderr", "  FAIL the backend matrix visited 0 backends; nothing below proves any dispatch path works"),
    ("stderr", " fail smoke: 2 of 11 failed in 266s"),
    ("varlog/system", "Container called exit(1)."),
]
AUDIT_MARKER = "AUDIT-EVENT-MUST-NOT-BE-PRINTED"


def _entries(execution: str = EXECUTION) -> list[dict]:
    rows = []
    for i, (stream, text) in enumerate(TRANSCRIPT):
        rows.append({
            "logName": f"projects/{PROJECT}/logs/run.googleapis.com%2F{stream.replace('/', '%2F')}",
            "labels": {"run.googleapis.com/execution_name": execution},
            "resource": {"type": "cloud_run_job", "labels": {"job_name": "swarm-verify"}},
            "timestamp": f"2026-09-24T18:36:{10 + i:02d}Z",
            "textPayload": text,
        })
    # Shares the execution's label, as the real system_event does; the
    # script's filter must keep it out.
    rows.append({
        "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Fsystem_event",
        "labels": {"run.googleapis.com/execution_name": execution},
        "resource": {"type": "cloud_run_job", "labels": {"job_name": "swarm-verify"}},
        "timestamp": "2026-09-24T18:40:43Z",
        "protoPayload": {"methodName": AUDIT_MARKER},
    })
    # Another execution's transcript, which must not be read as this one's.
    rows.append({
        "logName": f"projects/{PROJECT}/logs/run.googleapis.com%2Fstderr",
        "labels": {"run.googleapis.com/execution_name": "swarm-verify-other"},
        "resource": {"type": "cloud_run_job", "labels": {"job_name": "swarm-verify"}},
        "timestamp": "2026-09-24T18:40:44Z",
        "textPayload": "SOMEBODY ELSES EXECUTION",
    })
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    return rows


def _run(tmp: Path, *targets: str, failing: tuple[str, ...] = (),
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
    (bin_dir / "curl").write_text(TRIPWIRE)
    for path in bin_dir.iterdir():
        path.chmod(0o755)

    (tmp / "failing").write_text("".join(f"scripts/{t}.sh\n" for t in failing))
    (tmp / "entries.json").write_text(json.dumps(_entries()))
    (tmp / "gcloud.log").write_text("")
    (tmp / "tmp").mkdir(exist_ok=True)

    env_file = tmp / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nREGION={REGION}\nENVIRONMENT=dev\n")
    env_file.chmod(0o600)

    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "SWARM_ENV_FILE": str(env_file),
        "NO_COLOR": "1",
        "TMPDIR": str(tmp / "tmp"),
        "SWARM_VERIFY_LOG_WAIT_SECONDS": "0",
        "FAKE_GCLOUD_LOG": str(tmp / "gcloud.log"),
        "FAKE_FAILING_TARGETS": str(tmp / "failing"),
        "FAKE_LOG_ENTRIES": str(tmp / "entries.json"),
        "FAKE_EXECUTION": EXECUTION,
        "FAKE_REGION": REGION,
    })
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(SCRIPT), *targets],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    proc.transcript = proc.stdout + proc.stderr  # type: ignore[attr-defined]
    proc.calls = (tmp / "gcloud.log").read_text().splitlines()  # type: ignore[attr-defined]
    return proc


def _explain(proc) -> str:
    calls = "\n".join(proc.calls)
    return f"exit {proc.returncode}\n--- transcript ---\n{proc.transcript}\n--- gcloud calls ---\n{calls}"


def _log_reads(proc) -> list[str]:
    return [c for c in proc.calls if c.startswith("logging read")]


def test_a_failed_target_prints_the_executions_own_log(tmp_path):
    """The two FAIL lines that took a second person to find, in the release log."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",))
    out = proc.transcript
    assert proc.returncode != 0, _explain(proc)
    assert "smoke-test FAILED" in out, _explain(proc)
    first = "FAIL could not read the runner-profile catalogue"
    second = "FAIL the backend matrix visited 0 backends"
    summary = "smoke: 2 of 11 failed"
    for line in (first, second, summary, "Container called exit(1)."):
        assert line in out, f"missing from the release log: {line!r}\n" + _explain(proc)
    # Oldest first: the transcript reads the way the suite wrote it.
    assert out.index(first) < out.index(second) < out.index(summary), _explain(proc)
    # This execution only, and not its audit events.
    assert AUDIT_MARKER not in out, _explain(proc)
    assert "SOMEBODY ELSES EXECUTION" not in out, _explain(proc)
    reads = _log_reads(proc)
    assert len(reads) == 1, _explain(proc)
    assert EXECUTION in reads[0] and "swarm-verify" in reads[0], _explain(proc)
    assert f"--project {PROJECT}" in reads[0], _explain(proc)


def test_the_log_tail_is_redacted(tmp_path):
    """A test transcript is exactly where a token turns up."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",))
    assert "leaked-access-token-value" not in proc.transcript, _explain(proc)
    assert "ya29.********" in proc.transcript, _explain(proc)


def test_a_refused_log_read_is_reported_and_the_target_still_fails(tmp_path):
    """No logging.logEntries.list: say so, name the fix, and keep the verdict."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_LOG_READ": "denied"})
    out = proc.transcript
    assert proc.returncode != 0, _explain(proc)
    assert "smoke-test FAILED" in out, _explain(proc)
    assert f"could not read the log of execution {EXECUTION}" in out, _explain(proc)
    assert "PERMISSION_DENIED" in out, _explain(proc)
    assert "logging.logEntries.list" in out, _explain(proc)
    # The same lines, by hand, for whoever does hold the role.
    assert "gcloud logging read" in out, _explain(proc)


def test_an_empty_log_is_retried_then_reported_as_empty(tmp_path):
    """Ingestion lags the job; an empty first answer is not the final answer."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_LOG_READ": "empty"})
    assert proc.returncode != 0, _explain(proc)
    assert len(_log_reads(proc)) == 3, _explain(proc)
    assert f"execution {EXECUTION} has no stdout/stderr" in proc.transcript, _explain(proc)


def test_a_failure_before_any_execution_reads_no_log(tmp_path):
    """gcloud refused to create an execution: there is no log to go looking for."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_EXECUTE_NAMES_NO_EXECUTION": "1"})
    assert proc.returncode != 0, _explain(proc)
    assert _log_reads(proc) == [], _explain(proc)
    assert "gcloud named no execution for smoke-test" in proc.transcript, _explain(proc)


def test_a_passing_target_reads_no_log_and_passes(tmp_path):
    proc = _run(tmp_path, "smoke-test")
    assert proc.returncode == 0, _explain(proc)
    assert _log_reads(proc) == [], _explain(proc)


def test_every_target_still_runs_after_one_fails(tmp_path):
    """Printing a log must not stop the gate before the remaining targets."""
    proc = _run(tmp_path, "smoke-test", "concurrency-test", failing=("smoke-test",))
    executes = [c for c in proc.calls if c.startswith("run jobs execute")]
    assert len(executes) == 2, _explain(proc)
    assert "1 of 2 targets failed: smoke-test" in proc.transcript, _explain(proc)
    assert proc.returncode != 0, _explain(proc)
