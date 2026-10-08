"""acc_settle follows a dependency park up to the ancestor that will never run.

THE DEFECT (release 37324226899, first acceptance run in tenant smoke,
2026-10-05). Workflow wf_70090d2e9075481cb756's implement step parked on
CREDENTIAL_MISSING within 2 s. `_wf_check_integrate` waits on the LAST step,
fix, which sat PARKED on DEPENDENCY_INCOMPLETE behind it -- not
CREDENTIAL_MISSING, and with no blocked_by -- so `acc_settle` never saw a
reason to stop, waited its full 900 s and reported FAIL "did not finish
within 900s (last state PARKED)" for what run.sh's header promises is a SKIP.

These run lib.sh's real waiting code against a fake Firestore: `task_doc`
reads a per-task SEQUENCE of documents from a directory, advancing one
document per read, so a task can be shown running and then finishing. No
platform, no network, no credentials.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "scripts" / "acceptance" / "lib.sh"
WORKFLOW = ROOT / "scripts" / "acceptance" / "groups" / "workflow.sh"

IMPLEMENT = "task_implement0000000001"
REVIEW = "task_review000000000002"
FIX = "task_fix00000000000003"


def _parked(reason: str, depends_on: list[str] | None = None, **extra) -> dict:
    doc = {"state": "PARKED", "park_reason": reason, "attempt_count": 0, "depends_on": depends_on or []}
    doc.update(extra)
    return doc


def _dep(parent: str) -> dict:
    return _parked("DEPENDENCY_INCOMPLETE", [parent])


def _state(state: str, depends_on: list[str] | None = None, attempts: int = 1) -> dict:
    return {"state": state, "attempt_count": attempts, "depends_on": depends_on or []}


def _write_docs(tmp_path: Path, docs: dict[str, list[dict]]) -> Path:
    store = tmp_path / "docs"
    store.mkdir()
    for task, sequence in docs.items():
        (store / f"{task}.json").write_text(json.dumps(sequence))
    return store


def _run(tmp_path: Path, docs: dict[str, list[dict]], body: str, *, timeout: int = 900,
         admit_wait: int = 300, limit: int = 60) -> tuple[subprocess.CompletedProcess, float]:
    store = _write_docs(tmp_path, docs)
    script = f"""set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
STORE={shlex.quote(str(store))}
die() {{ printf 'DIE %s\\n' "$*"; exit 9; }}
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_case() {{ :; }}; t_info() {{ :; }}; info() {{ :; }}
cancel_all() {{ printf 'CANCEL %s\\n' "$*" >>"${{STORE}}/cancelled"; }}
# One read advances the task one document; the last one repeats.
task_doc() {{
  local n=0 count
  [[ -f "${{STORE}}/$1.json" ]] || {{ printf 'null'; return 0; }}
  [[ ! -f "${{STORE}}/$1.n" ]] || n="$(cat "${{STORE}}/$1.n")"
  printf '%s' "$(( n + 1 ))" >"${{STORE}}/$1.n"
  count="$(jq 'length' "${{STORE}}/$1.json")"
  [[ "${{n}}" -lt "${{count}}" ]] || n=$(( count - 1 ))
  jq -c ".[${{n}}]" "${{STORE}}/$1.json"
}}
task_state() {{ task_doc "$1" | jq -r '.state // "MISSING"'; }}
task_field() {{ task_doc "$1" | jq -r "$2"; }}
source {shlex.quote(str(LIB))}
{body}
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "SWARM_", "GITHUB_", "GH_"))}
    env.update(
        NO_COLOR="1",
        SWARM_ACCEPTANCE_TIMEOUT=str(timeout),
        SWARM_ACCEPTANCE_ADMIT_WAIT=str(admit_wait),
        SWARM_POLL_INTERVAL_SECONDS="0.05",
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=limit, check=False
    )
    return result, time.monotonic() - started


def _reads(tmp_path: Path, task: str) -> int:
    counter = tmp_path / "docs" / f"{task}.n"
    return int(counter.read_text()) if counter.exists() else 0


WAIT_ON_FIX = f"""
if acc_run_to_end state {FIX}; then echo "RAN ${{state}}"; else echo "NOT_RUN ${{state}}"; fi
"""


def test_a_root_parked_on_credential_missing_skips_the_last_step_quickly_and_names_the_root(tmp_path):
    docs = {
        IMPLEMENT: [_parked("CREDENTIAL_MISSING")],
        REVIEW: [_dep(IMPLEMENT)],
        FIX: [_dep(REVIEW)],
    }
    # A 900 s timeout, as in the release: before the fix this waited all of it.
    result, took = _run(tmp_path, docs, WAIT_ON_FIX, limit=30)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert took < 20, f"took {took:.1f}s: the wait did not follow the dependency park"
    assert "NOT_RUN" in out and "RAN" not in out, out
    assert "FAIL" not in out, out
    skip = [line for line in out.splitlines() if line.startswith("SKIP")]
    assert len(skip) == 1, out
    assert IMPLEMENT in skip[0], skip[0]
    assert "CREDENTIAL_MISSING" in skip[0], skip[0]
    assert f"[{FIX}]" in skip[0], skip[0]


def test_a_root_held_on_a_manual_pause_skips_the_last_step_after_the_admit_wait(tmp_path):
    docs = {
        IMPLEMENT: [_parked("MANUAL_PAUSE")],
        REVIEW: [_dep(IMPLEMENT)],
        FIX: [_dep(REVIEW)],
    }
    result, took = _run(tmp_path, docs, WAIT_ON_FIX, admit_wait=0, limit=30)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert took < 20, took
    skip = [line for line in out.splitlines() if line.startswith("SKIP")]
    assert len(skip) == 1 and "FAIL" not in out, out
    assert IMPLEMENT in skip[0] and "MANUAL_PAUSE" in skip[0], skip[0]


def test_a_root_that_runs_normally_still_waits_for_the_last_step(tmp_path):
    docs = {
        IMPLEMENT: [_state("RUNNING")] * 3 + [_state("SUCCEEDED")],
        REVIEW: [_dep(IMPLEMENT)] * 3 + [_state("RUNNING", [IMPLEMENT])] * 2 + [_state("SUCCEEDED", [IMPLEMENT])],
        FIX: [_dep(REVIEW)] * 8 + [_state("RUNNING", [REVIEW])] * 2 + [_state("SUCCEEDED", [REVIEW])],
    }
    result, _ = _run(tmp_path, docs, WAIT_ON_FIX, limit=30)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "RAN SUCCEEDED" in out, out
    assert "SKIP" not in out and "FAIL" not in out, out
    assert _reads(tmp_path, FIX) >= 11, "the wait ended before fix reached SUCCEEDED"


def test_a_genuine_timeout_behind_a_running_root_still_fails(tmp_path):
    docs = {
        IMPLEMENT: [_state("RUNNING")],
        REVIEW: [_dep(IMPLEMENT)],
        FIX: [_dep(REVIEW)],
    }
    result, _ = _run(tmp_path, docs, WAIT_ON_FIX, timeout=2, admit_wait=0, limit=30)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "NOT_RUN PARKED" in out, out
    assert "SKIP" not in out, out
    assert "FAIL" in out and "did not finish within 2s (last state PARKED)" in out, out


def test_a_parent_that_parked_for_quota_after_running_is_waited_on_not_skipped(tmp_path):
    # A quota park on an ancestor that HAS held a lease is the platform working:
    # it will resume. Only "will never run" ends the wait early.
    docs = {
        IMPLEMENT: [_parked("PROVIDER_QUOTA_EXHAUSTED", attempt_count=1)],
        REVIEW: [_dep(IMPLEMENT)],
        FIX: [_dep(REVIEW)],
    }
    result, _ = _run(tmp_path, docs, WAIT_ON_FIX, timeout=2, admit_wait=0, limit=30)
    out = result.stdout
    assert "SKIP" not in out, out
    assert "did not finish within 2s" in out, out


def test_the_integrate_check_skips_naming_implement_when_implement_has_no_credential(tmp_path):
    wf = {
        "workflow": {
            "workflow_id": "wf_acceptance",
            "steps": [
                {"step_id": "implement", "task_id": IMPLEMENT},
                {"step_id": "review", "task_id": REVIEW},
                {"step_id": "fix", "task_id": FIX},
            ],
        }
    }
    docs = {
        IMPLEMENT: [_parked("CREDENTIAL_MISSING")],
        REVIEW: [_dep(IMPLEMENT)],
        FIX: [_dep(REVIEW)],
    }
    body = f"""
acc_close_pr() {{ :; }}
workflow_step_task() {{ jq -r --arg s "$2" '.workflow.steps[] | select(.step_id == $s) | .task_id // empty' <<<"$1"; }}
source {shlex.quote(str(WORKFLOW))}
_wf_check_integrate {shlex.quote(json.dumps(wf))}
echo DONE
"""
    result, took = _run(tmp_path, docs, body, limit=30)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    assert "DONE" in out, out
    assert took < 20, took
    assert "FAIL" not in out, out
    skip = [line for line in out.splitlines() if line.startswith("SKIP")]
    assert len(skip) == 1 and IMPLEMENT in skip[0] and "CREDENTIAL_MISSING" in skip[0], out
