"""The smoke suite's "Capacity was returned" check counts only the smoke's OWN tasks.

THE DEFECT THIS PINS. Release 36977343635 (2026-10-02, deploy and smoke) failed
13/14 with only

    [12] Capacity was returned -- timed out after 120s waiting for: capacity to
    return to 0 / FAIL tasks holding capacity after completion: 1 > 0

while the same run printed `PASS lease ... released` for the smoke task's own
lease. The check counted every task in the WHOLE tenant in a capacity-holding
state against a baseline read before the smoke submitted anything. The smoke
runs in tenant `eng`, and the owner's SwarmCloud lanes run in `eng` at the same
time, so any lane that started during the smoke made the check fail -- a
property of somebody else's work, reported as the platform failing to return
capacity.

The check now asks only about the tasks the smoke submitted: each must be out
of CONCURRENCY_STATES and every lease its events name must be released. The
property is kept: a smoke task that still holds capacity after completion
fails it, and a read that did not answer is never a pass.

The helpers are sourced by bash exactly as the suite sources them, with the
Firestore reads replaced by functions defined after the libraries, and the
check's own block is extracted from scripts/smoke-test.sh and run, so the test
cannot pass against a copy of it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / "scripts" / "smoke-test.sh"

# Stand-ins for the Firestore reads. STATES / LEASES / RELEASED are
# "id=value" words; a task or lease not listed reads as a failed read.
# holding_capacity and fs_count answer for the WHOLE tenant: they report other
# tenants' lanes holding capacity, which the check must not count.
STUBS = r'''
lookup() {
  local key="$1" table="$2" pair
  for pair in ${table}; do
    if [[ "${pair%%=*}" == "${key}" ]]; then printf '%s' "${pair#*=}"; return 0; fi
  done
  return 1
}
task_state() { lookup "$1" "${STATES}"; }
task_lease_ids() {
  local v; v="$(lookup "$1" "${LEASES}")" || return 1
  [[ "${v}" == "-" ]] || printf '%s\n' "${v//,/$'\n'}"
}
lease_released_at() { lookup "$1" "${RELEASED}"; }
holding_capacity() { printf '7'; }
fs_count() { printf '7'; }
wait_until() { shift 2; "$@"; }
'''


def run(snippet: str, *, states="", leases="", released=""):
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env.pop("SWARM_ENV_FILE", None)
    env.update(STATES=states, LEASES=leases, RELEASED=released)
    script = (
        f'source "{ROOT}/scripts/lib/common.sh"; '
        f'source "{ROOT}/scripts/lib/testlib.sh"; '
        + STUBS
        + snippet
    )
    return subprocess.run(
        ["bash", "-c", script, "smoke-own-capacity"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# -- the helpers ---------------------------------------------------------------

def test_finished_tasks_with_released_leases_hold_nothing_whatever_the_tenant_holds():
    """The release's case: the smoke's task is done and its lease is back,
    while other lanes in the tenant hold capacity (holding_capacity says 7)."""
    r = run(
        'own_capacity_holders t1 t2; printf "rc=%s\\n" "$?"; '
        'own_capacity_released t1 t2 && echo released',
        states="t1=SUCCEEDED t2=CANCELLED",
        leases="t1=l1 t2=-",
        released="l1=2026-10-02T10:00:00Z",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["rc=0", "released"], r.stdout


def test_a_smoke_task_still_in_a_holding_state_is_reported():
    r = run(
        'own_capacity_holders t1 t2; own_capacity_released t1 t2 || echo held',
        states="t1=SUCCEEDED t2=RUNNING",
        leases="t1=l1 t2=l2",
        released="l1=2026-10-02T10:00:00Z l2=null",
    )
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "t2 state RUNNING" in lines, lines
    assert "t2 lease l2" in lines, lines
    assert lines[-1] == "held", lines


def test_a_finished_task_whose_lease_was_not_released_is_reported():
    """The terminal state is written before the lease is released; a lease
    that never comes back is exactly the leak this check exists to catch."""
    r = run(
        'own_capacity_holders t1; own_capacity_released t1 || echo held',
        states="t1=SUCCEEDED",
        leases="t1=l1,l9",
        released="l1=2026-10-02T10:00:00Z l9=null",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["t1 lease l9", "held"], r.stdout


def test_every_capacity_holding_state_counts():
    for state in ("LEASED", "DISPATCHED", "STARTING", "RUNNING"):
        r = run(
            'own_capacity_released t1 || echo held',
            states=f"t1={state}",
            leases="t1=-",
        )
        assert r.stdout.strip() == "held", (state, r.stdout, r.stderr)


def test_a_failed_read_is_never_released():
    cases = [
        dict(states="", leases="t1=-"),                       # task unreadable
        dict(states="t1=MISSING", leases="t1=-"),             # task absent
        dict(states="t1=SUCCEEDED", leases=""),               # events unreadable
        dict(states="t1=SUCCEEDED", leases="t1=l1"),          # lease unreadable
        dict(states="t1=SUCCEEDED", leases="t1=l1", released="l1=missing"),
    ]
    for case in cases:
        r = run('own_capacity_holders t1 && echo answered; '
                'own_capacity_released t1 && echo released', **case)
        assert "answered" not in r.stdout, (case, r.stdout)
        assert "released" not in r.stdout, (case, r.stdout)


def test_no_task_ids_is_not_a_pass():
    """An empty list proves nothing; it must not read as 'nothing held'."""
    r = run('own_capacity_holders && echo answered; own_capacity_released && echo released')
    assert "answered" not in r.stdout and "released" not in r.stdout, r.stdout


# -- the check itself, extracted from smoke-test.sh ----------------------------

def _capacity_block() -> str:
    text = SMOKE.read_text()
    m = re.search(r'^t_case "Capacity was returned"\n(.*?)^# Every lease the task held', text, re.S | re.M)
    assert m, "could not find the 'Capacity was returned' check in scripts/smoke-test.sh"
    return m.group(0).rsplit("# Every lease", 1)[0]


def _run_block(ids: str, **kw):
    snippet = f"SMOKE_TASK_IDS=({ids})\n" + _capacity_block() + '\nprintf "failed=%s passed=%s\\n" "${TESTS_FAILED}" "${TESTS_PASSED}"\n'
    return run(snippet, **kw)


def test_the_check_ignores_capacity_held_by_other_work_in_the_tenant():
    block = _capacity_block()
    assert "holding_capacity" not in block and "BASE_HOLDING" not in block, block
    r = _run_block("t1 t2", states="t1=SUCCEEDED t2=SUCCEEDED", leases="t1=l1 t2=l2",
                   released="l1=2026-10-02T10:00:00Z l2=2026-10-02T10:01:00Z")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "failed=0 passed=1", r.stdout + r.stderr


def test_the_check_fails_when_a_smoke_task_holds_capacity_after_completion():
    r = _run_block("t1 t2", states="t1=SUCCEEDED t2=SUCCEEDED", leases="t1=l1 t2=l2",
                   released="l1=2026-10-02T10:00:00Z l2=null")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "failed=1 passed=0", r.stdout + r.stderr
    assert "smoke tasks holding capacity after completion" in r.stderr, r.stderr
    assert "t2 lease l2" in r.stderr, r.stderr


def test_the_check_fails_on_a_failed_read_and_on_no_tasks():
    r = _run_block("t1", states="", leases="t1=-")
    assert r.stdout.strip().splitlines()[-1] == "failed=1 passed=0", r.stdout + r.stderr
    r = _run_block("")
    assert r.stdout.strip().splitlines()[-1] == "failed=1 passed=0", r.stdout + r.stderr


def test_every_task_the_smoke_submits_is_tracked():
    """A task the smoke submits but does not track is one whose leaked
    capacity this check would never see."""
    text = SMOKE.read_text()
    assigned = set(re.findall(r'(\w+)="\$\(submit_task ', text))
    assigned |= set(re.findall(r"(\w+)=\"\$\(jq -r '\.id // \.task_id", text))
    assert {"TASK_ID", "B_TASK_ID", "evil_id"} <= assigned, assigned
    tracked = set(re.findall(r'smoke_track_task "\$\{(\w+)\}"', text))
    assert assigned <= tracked, f"submitted but not tracked: {assigned - tracked}"
    # The browser fixture task is submitted inside testlib.sh and handed back.
    assert "FIXTURE_TASK_ID" in tracked, tracked
