"""Two acceptance checks that read timing, and the timing they must not misread.

Owner decision 2026-10-06, from release 37413200995's acceptance (job
112131142414):

* mock.sh's "quota_exhausted parks with its retry_after" measured
  next_eligible_at from the `parked` event and saw 35 s against a retry_after
  of 90. The platform sets next_eligible_at = signal time + retry_after
  (agent_worker/quota.py `decide()`), and that run's `parked` event was
  written 53 s after the signal: a worker stall, not an early eligibility.
  The check now measures from the `quota_exhausted` event, which the worker
  writes in the park's own transaction, ahead of the `parked` event.
* lib.sh's `acc_require_private_repository` read curl's HTTP 000 -- curl's
  own failure: DNS, connect, TLS or timeout through the VPC NAT -- as a
  refusal after two tries, and failed the generic group twice. 000 is now
  transient (four tries, 5/15/30 s apart), and a refusal on it names egress
  and carries curl's own stderr.

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

TASK = "task_park0000000000000001"


def _clean_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("API_", "SWARM_", "GITHUB_", "GH_"))
    }
    env["NO_COLOR"] = "1"
    return env


def _bash(body: str) -> subprocess.CompletedProcess:
    script = f"""set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
die() {{ printf 'DIE %s\\n' "$*" >&2; exit 9; }}
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_case() {{ :; }}; t_info() {{ :; }}; info() {{ :; }}; step() {{ :; }}
source {shlex.quote(str(LIB))}
{body}
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


# ---------------------------------------------------------------------------
# mock.sh: next_eligible_at is measured from the quota signal, not the park
# ---------------------------------------------------------------------------


def _park_check(tmp_path: Path, events: list[dict], eligible_at: str) -> subprocess.CompletedProcess:
    doc = {
        "state": "PARKED",
        "park_reason": "PROVIDER_QUOTA_EXHAUSTED",
        "current_lease_id": None,
        "next_eligible_at": eligible_at,
    }
    (tmp_path / "doc.json").write_text(json.dumps(doc))
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    body = f"""
source {shlex.quote(str(MOCK))}
wait_for_state() {{ printf 'PARKED'; }}
task_doc() {{ cat {shlex.quote(str(tmp_path / 'doc.json'))}; }}
acc_events() {{ cat {shlex.quote(str(tmp_path / 'events.jsonl'))}; }}
acc_leases_released() {{ printf '1 lease(s) released'; }}
# The resume half is another check's; stop the function after the park.
acc_run_to_end() {{ return 1; }}
_mock_check_park_and_restore {TASK}
"""
    return _bash(body)


def _eligibility_lines(result: subprocess.CompletedProcess) -> list[str]:
    # Every line names the check, which says "quota_exhausted"; only the
    # eligibility verdict says "next_eligible_at".
    return [line for line in result.stdout.splitlines() if "next_eligible_at" in line]


def test_a_park_recorded_late_still_passes_when_eligibility_honours_retry_after(tmp_path):
    # Release 37413200995: the signal at :00, the park written 53 s later,
    # next_eligible_at = signal + 90 -- 37 s after the park.
    events = [
        {"type": "quota_exhausted", "at": "2026-10-06T10:00:00.250000Z", "detail": {"wait_seconds": 90}},
        {"type": "parked", "at": "2026-10-06T10:00:53.100000Z", "detail": {}},
    ]
    result = _park_check(tmp_path, events, "2026-10-06T10:01:30.250000+00:00")
    lines = _eligibility_lines(result)
    assert any(line.startswith("PASS") for line in lines), result.stdout + result.stderr
    assert not any(line.startswith("FAIL") for line in lines), result.stdout


def test_an_eligibility_genuinely_earlier_than_retry_after_fails(tmp_path):
    # Parked at once, but eligible 35 s after the signal: retry_after ignored.
    events = [
        {"type": "quota_exhausted", "at": "2026-10-06T10:00:00Z", "detail": {"wait_seconds": 90}},
        {"type": "parked", "at": "2026-10-06T10:00:01Z", "detail": {}},
    ]
    result = _park_check(tmp_path, events, "2026-10-06T10:00:35Z")
    lines = _eligibility_lines(result)
    assert any(line.startswith("FAIL") and "35s" in line for line in lines), result.stdout + result.stderr
    assert not any(line.startswith("PASS") for line in lines), result.stdout


def test_a_park_without_its_quota_signal_fails_rather_than_measuring_from_the_park(tmp_path):
    events = [{"type": "parked", "at": "2026-10-06T10:00:00Z", "detail": {}}]
    result = _park_check(tmp_path, events, "2026-10-06T10:01:30Z")
    lines = _eligibility_lines(result)
    assert any(line.startswith("FAIL") for line in lines), result.stdout + result.stderr
    assert not any(line.startswith("PASS") for line in lines), result.stdout


# ---------------------------------------------------------------------------
# lib.sh: curl's 000 is an egress failure, retried, and named as such
# ---------------------------------------------------------------------------

#: A curl that answers from a file, one line per call: `000` is curl's own
#: failure (it prints 000, says why on stderr and exits 28), anything else an
#: HTTP status. It drains stdin as real curl does with `-K -` (#678).
FAKE_CURL = r"""
curl() {
  local n code
  cat >/dev/null
  n=$(( $(cat "${FAKE_DIR}/count") + 1 ))
  printf '%s\n' "$n" >"${FAKE_DIR}/count"
  code="$(sed -n "${n}p" "${FAKE_DIR}/codes")"
  if [[ "${code}" == "000" ]]; then
    printf '000'
    printf 'curl: (28) Connection timed out after 30001 milliseconds\n' >&2
    return 28
  fi
  printf '%s' "${code}"
}
sleep() { printf '%s\n' "$1" >>"${FAKE_DIR}/slept"; }
"""


def _probe(tmp_path: Path, codes: list[str]) -> subprocess.CompletedProcess:
    (tmp_path / "codes").write_text("\n".join(codes) + "\n")
    (tmp_path / "count").write_text("0\n")
    body = f"""
FAKE_DIR={shlex.quote(str(tmp_path))}
{FAKE_CURL}
acc_require_private_repository
echo REACHED
"""
    return _bash(body)


def _calls(tmp_path: Path) -> int:
    return int((tmp_path / "count").read_text())


def _slept(tmp_path: Path) -> list[str]:
    path = tmp_path / "slept"
    return path.read_text().split() if path.exists() else []


def test_an_egress_failure_then_a_404_is_private(tmp_path):
    result = _probe(tmp_path, ["000", "404"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REACHED" in result.stdout
    assert _calls(tmp_path) == 2
    assert _slept(tmp_path) == ["5"]


def test_three_egress_failures_then_a_404_is_private(tmp_path):
    result = _probe(tmp_path, ["000", "000", "000", "404"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(tmp_path) == 4
    assert _slept(tmp_path) == ["5", "15", "30"]


def test_four_egress_failures_refuse_naming_egress_and_curls_own_words(tmp_path):
    result = _probe(tmp_path, ["000", "000", "000", "000", "404"])
    assert result.returncode == 9, result.stdout + result.stderr
    assert "REACHED" not in result.stdout
    assert _calls(tmp_path) == 4, "a fifth try is past the budget"
    assert _slept(tmp_path) == ["5", "15", "30"]
    assert "egress" in result.stderr
    assert "Connection timed out" in result.stderr, "curl's stderr must reach the refusal"
    assert "answered HTTP" not in result.stderr, "curl's own failure is not GitHub's answer"


def test_a_real_non_answer_is_still_asked_only_twice(tmp_path):
    result = _probe(tmp_path, ["503", "503", "404"])
    assert result.returncode == 9, result.stdout + result.stderr
    assert _calls(tmp_path) == 2
    assert "egress" not in result.stderr
    assert "HTTP 503" in result.stderr
