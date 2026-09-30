"""`scripts/verify-destroy-guard.sh` exits 2 when the guard misbehaves, 1 when it cannot check (#173).

The script's header and docs/runbooks/destroy-guard-real-plan-proof.md both
promise two different failures:

    Exit 1 = something could not be checked (which is a failure, not a skip).
    Exit 2 = the guard did not behave as required.

The code ended in `die`, which is `exit 1`, so a guard that let a deny-listed
neighbour through and a plan that could not be read were the same number, and
anyone triaging by exit code was sent to "could not check".

Both cases run the real script from a throwaway copy of scripts/. The guard
case replaces `scripts/lib/plan-guard.sh` with one that allows every plan --
the misbehaviour the proof exists to catch -- and feeds it the real recorded
destroy plan. The control feeds the real guard a plan that deletes nothing,
which the script must refuse as uncheckable: without it, "exits 2" could be a
script that exits 2 for everything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify-destroy-guard.sh"
FIXTURE = REPO / "tests" / "integration" / "fixtures" / "destroy-plan-dev-real.json"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or not SCRIPT.exists() or not FIXTURE.exists(),
    reason="jq, scripts/verify-destroy-guard.sh and the recorded plan are all required",
)

GUARD_THAT_ALLOWS_EVERYTHING = """#!/usr/bin/env bash
# Stands in for scripts/lib/plan-guard.sh: says yes to every plan.
exit 0
"""


def _copy(tmp_path: Path, *, broken_guard: bool) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    if broken_guard:
        guard = root / "scripts" / "lib" / "plan-guard.sh"
        guard.write_text(GUARD_THAT_ALLOWS_EVERYTHING)
        guard.chmod(0o755)
    project = json.loads(FIXTURE.read_text())["_recording"]["project"]
    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={project}\nREGION=us-central1\nENVIRONMENT=dev\nFIRESTORE_DATABASE=swarm\n")
    env_file.chmod(0o600)
    env = dict(os.environ)
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    return root, env


def _run(root: Path, env: dict[str, str], plan: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(root / "scripts" / "verify-destroy-guard.sh"), "--environment", "dev", "--plan", str(plan)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
    )


def test_a_guard_that_refuses_nothing_exits_2(tmp_path: Path) -> None:
    root, env = _copy(tmp_path, broken_guard=True)
    proc = _run(root, env, FIXTURE)
    transcript = proc.stdout + proc.stderr
    assert "the destroy guard does not behave as required" in transcript, transcript
    assert proc.returncode == 2, (
        f"a guard that let every deny-listed neighbour through exited {proc.returncode}; "
        "the header and the runbook promise 2 for that and 1 only for 'could not check'\n"
        + transcript[-3000:]
    )


def test_a_plan_that_cannot_be_judged_exits_1(tmp_path: Path) -> None:
    root, env = _copy(tmp_path, broken_guard=False)
    empty = tmp_path / "empty-plan.json"
    empty.write_text(json.dumps({"format_version": "1.2", "resource_changes": []}))
    proc = _run(root, env, empty)
    transcript = proc.stdout + proc.stderr
    assert "deletes nothing" in transcript, transcript
    assert proc.returncode == 1, transcript
