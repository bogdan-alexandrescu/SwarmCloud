"""mock.sh's short rate limit check counts the second attempt from its documents.

THE FALSE FAILURE (dev acceptance, 2026-11-20). "a short rate limit is retried
in place, then the task succeeds" read the task's `attempt_count` and reported
"attempts=1 was_resumed=true". The platform did what it is designed to do: the
worker retried the 1 s retry-after in place, the mock refused every retry of
its first attempt, the attempt parked, and a second attempt restored the
checkpoint and finished. A PROVIDER_QUOTA_EXHAUSTED park REFUNDS its attempt
(`agent_worker.control.ControlPlane.park`, the owner 2026-10-27, "Refund,
bounded"), so `attempt_count` went back to 0 and the second lease took it to
1. The check now counts the task's attempt documents, which every attempt
writes, refunded or not.

These run the real shell against fakes: no platform, no network, no
credentials.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "scripts" / "acceptance" / "lib.sh"
MOCK = ROOT / "scripts" / "acceptance" / "groups" / "mock.sh"

TASK = "task_retry000000000000001"
CHECK_LINE = "a second attempt ran and resumed"

#: What the 2026-11-20 run's task held: refunded back to one, resumed.
REFUNDED_DOC = {
    "state": "SUCCEEDED",
    "attempt_count": 1,
    "metadata": {"quota_park_resumes": 1},
}
RETRYING = [{"type": "retrying", "at": "2026-11-20T10:00:01Z", "detail": {}}]


def _clean_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("API_", "SWARM_", "GITHUB_", "GH_"))
    }
    env["NO_COLOR"] = "1"
    # The placeholder deployment's sandbox: config.sh has no default for it.
    env["SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL"] = os.environ["SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL"]
    return env


def _retry_check(
    tmp_path: Path,
    *,
    attempts: list[dict] | None,
    was_resumed: bool,
    doc: dict = REFUNDED_DOC,
) -> subprocess.CompletedProcess:
    (tmp_path / "doc.json").write_text(json.dumps(doc))
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in RETRYING))
    (tmp_path / "output.json").write_text(json.dumps({"was_resumed": was_resumed}))
    if attempts is None:
        fake_attempts = "task_attempts() { return 1; }"
    else:
        (tmp_path / "attempts.jsonl").write_text("".join(json.dumps(a) + "\n" for a in attempts))
        fake_attempts = f"task_attempts() {{ cat {shlex.quote(str(tmp_path / 'attempts.jsonl'))}; }}"
    script = f"""set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
die() {{ printf 'DIE %s\\n' "$*" >&2; exit 9; }}
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_case() {{ :; }}; t_info() {{ :; }}; info() {{ :; }}; step() {{ :; }}
source {shlex.quote(str(LIB))}
source {shlex.quote(str(MOCK))}
acc_run_to_end() {{ printf -v "$1" '%s' SUCCEEDED; }}
task_doc() {{ cat {shlex.quote(str(tmp_path / 'doc.json'))}; }}
task_field() {{ task_doc "$1" | jq -r "$2"; }}
acc_events() {{ cat {shlex.quote(str(tmp_path / 'events.jsonl'))}; }}
acc_output() {{ jq -r "$2" {shlex.quote(str(tmp_path / 'output.json'))}; }}
{fake_attempts}
_mock_check_retry {TASK}
"""
    # stdin is an empty pipe, so a fake that drains it never waits on a tty.
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=_clean_env(),
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _verdicts(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in result.stdout.splitlines() if CHECK_LINE in line or "attempt documents" in line]


def _attempt(n: int) -> dict:
    return {"attempt_id": f"att_{n}", "task_id": TASK, "created_at": f"2026-11-20T10:0{n}:00Z"}


def test_a_refunded_park_then_a_resumed_second_attempt_passes(tmp_path):
    # The 2026-11-20 run: attempt_count 1 after the refund, two attempt documents.
    result = _retry_check(tmp_path, attempts=[_attempt(1), _attempt(2)], was_resumed=True)
    lines = _verdicts(result)
    assert any(line.startswith("PASS") for line in lines), result.stdout + result.stderr
    assert not any(line.startswith("FAIL") for line in lines), result.stdout


def test_one_attempt_fails_even_when_the_runner_says_it_resumed(tmp_path):
    # An in-place restart that found its own state is not a second attempt.
    result = _retry_check(tmp_path, attempts=[_attempt(1)], was_resumed=True)
    lines = _verdicts(result)
    assert any(line.startswith("FAIL") and "attempt documents=1" in line for line in lines), (
        result.stdout + result.stderr
    )
    assert not any(line.startswith("PASS") for line in lines), result.stdout


def test_a_second_attempt_that_did_not_resume_fails(tmp_path):
    result = _retry_check(tmp_path, attempts=[_attempt(1), _attempt(2)], was_resumed=False)
    lines = _verdicts(result)
    assert any(line.startswith("FAIL") and "was_resumed=false" in line for line in lines), (
        result.stdout + result.stderr
    )
    assert not any(line.startswith("PASS") for line in lines), result.stdout


def test_an_unreadable_attempts_query_fails_rather_than_counting_nothing(tmp_path):
    result = _retry_check(tmp_path, attempts=None, was_resumed=True)
    lines = _verdicts(result)
    assert any(line.startswith("FAIL") and "attempt documents" in line for line in lines), (
        result.stdout + result.stderr
    )
    assert not any(line.startswith("PASS") for line in lines), result.stdout
