"""The acceptance group `tenant-isolation` passes only on the probe's own PASS lines and an exit of 0.

Owner decision 2026-10-20 (lane ISO-ACCEPT-WIRE, part of #1457).
scripts/acceptance/groups/tenant-isolation.sh submits
tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py as
one `generic` pytest agent and judges each check from the probe's
`ISO-ACCEPT-RESULT {json}` lines: stdout's tail on SUCCEEDED, last_error
(stderr's tail) otherwise. Run here with lib.sh's helpers stubbed, so no
deployment is needed: what is pinned is the judging, not the platform.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "tenant-isolation.sh"
PROBE = ROOT / "tests" / "acceptance" / "fixtures" / "tenant-isolation" / "test_cross_tenant_firestore.py"

_HARNESS = r'''
set -euo pipefail
source "$GROUP"
step() { :; }
redact() { cat; }
cancel_all() { echo "CANCELLED $1"; }
ACC_REPOSITORY_URL=sandbox ACC_REF=main ACC_RUN_ID=run ACC_TIMEOUT=1
acc_repo_extra() { echo '{}'; }
acc_submit() {
  printf '%s\n' "$3" >"$WORK/input.json"
  [[ "$FAKE_SUBMIT" == ok ]] || return 1
  printf -v "$1" '%s' task_1
}
acc_settle() { echo "$FAKE_STATE"; }
acc_output() { cat "$WORK/stdout"; }
task_field() { cat "$WORK/last_error"; }
acc_check() { ACC_CHECK="$1"; }
acc_pass() { echo "PASS ${ACC_CHECK}: $1"; }
acc_fail() { echo "FAIL ${ACC_CHECK}: $1"; }
run_tenant_isolation
'''


def _probe():
    spec = importlib.util.spec_from_file_location("iso_accept_probe_for_group", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _lines(verdicts: dict[str, bool]) -> str:
    probe = _probe()
    return "..".join(
        "\n" + probe.result_line(probe.Verdict(c, ok, f"{'denied' if ok else 'allowed by IAM'}: {c}")) + "\n"
        for c, ok in verdicts.items()
    )


def _run(tmp_path: Path, state: str, stdout: str = "", last_error: str = "", submit: str = "ok") -> list[str]:
    (tmp_path / "stdout").write_text(stdout)
    (tmp_path / "last_error").write_text(last_error)
    done = subprocess.run(
        ["bash", "-c", _HARNESS], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GROUP": str(GROUP), "WORK": str(tmp_path),
             "FAKE_STATE": state, "FAKE_SUBMIT": submit},
    )
    assert done.returncode == 0, done.stderr
    return [line for line in done.stdout.splitlines() if line.startswith(("PASS", "FAIL"))]


ALL = {"settings": True, "tenant": True, "lease": True}


def test_three_pass_lines_on_succeeded_pass_every_check_and_the_input_opts_the_probe_in(tmp_path):
    verdicts = _run(tmp_path, "SUCCEEDED", stdout=_lines(ALL) + "3 passed in 0.4s\n")
    assert [v.split(":", 1)[0] for v in verdicts] == ["PASS tenant-isolation"] * 3, verdicts
    sent = json.loads((tmp_path / "input.json").read_text())
    # MUTATION: start the agent anywhere but the probe's directory -- the probe skips.
    assert sent["command"] == "pytest"
    assert sent["working_directory"] == "repo/tests/acceptance/fixtures/tenant-isolation"
    assert sent["paths"] == ["repo/tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py"]


def test_a_red_probe_fails_the_check_it_names_from_last_error(tmp_path):
    verdicts = _run(tmp_path, "FAILED", last_error="pytest exited 1\n" + _lines({**ALL, "settings": False}))
    # MUTATION: read stdout_tail on FAILED -- the runner keeps only stderr there.
    assert verdicts[0].startswith("FAIL tenant-isolation: settings"), verdicts
    assert "allowed by IAM: settings" in verdicts[0]
    # A PASS line on a FAILED agent is still a FAIL: only exit 0 is a proof.
    assert all(v.startswith("FAIL") for v in verdicts), verdicts


@pytest.mark.parametrize("state", ["HELD:QUEUED:a closed pool", "PARKED:CREDENTIAL_MISSING", "TIMEOUT"])
def test_a_probe_agent_that_did_not_run_is_a_failure_never_a_skip(tmp_path, state):
    verdicts = _run(tmp_path, state)
    # MUTATION: hand the state to acc_settled_or_skip -- a held probe would read green.
    assert len(verdicts) == 3 and all(v.startswith("FAIL") for v in verdicts), verdicts
    assert "not measured" in verdicts[0]


def test_succeeded_without_result_lines_is_a_failure(tmp_path):
    verdicts = _run(tmp_path, "SUCCEEDED", stdout="3 skipped in 0.1s\n")
    # MUTATION: pass on SUCCEEDED alone -- a probe that skipped exits 0 too.
    assert len(verdicts) == 3 and all("no result line" in v for v in verdicts), verdicts


def test_a_refused_submission_fails_every_check(tmp_path):
    verdicts = _run(tmp_path, "SUCCEEDED", submit="refused")
    assert verdicts == [v for v in verdicts if v.endswith("not submitted")] and len(verdicts) == 3, verdicts
