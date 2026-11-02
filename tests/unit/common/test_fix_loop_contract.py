"""Contract requests 124-126: the fix loop's frozen contract (FL-CONTRACT, part of #2509).

docs/design/fix-loop.md §7 requests A and B, accepted by BGD on 2026-10-27
(Q1, Q2 and Q10 of §9). What is held here:

  * the `fix-loop` entry is `control_plane`, ships unavailable with a reason,
    and Terraform makes no Job for it (`profiles_without_a_job`), and its
    Terraform mirror states what the Python entry does;
  * `ParkReason.FIX_ROUNDS_RUNNING` exists, rides on PARKED and so holds no
    capacity (invariant 1), and no scheduler sweep promotes it: the scheduler
    promotes a park only through a sweep that names its reason
    (`_parked_window(ParkReason.X)`), or through PREWARM_REASONS;
  * `scripts/lib/check-contract-parity.sh` still passes with the new member;
  * the requests are filed: 124 and 125 accepted and applied, and 126 (the end
    cause Q3 (c) needs) accepted by BGD on 2026-10-27 and applied: the frozen
    `EndCause` has `FIX_LOOP_EXHAUSTED`, appended, the outcome ledger classes
    it as its own failure class, and the console's `EndCause` list carries it.

MUTATION: set `available=True` on `fix-loop` and
`test_fix_loop_ships_unavailable_with_a_reason` fails; drop "fix-loop" from
`profiles_without_a_job` in locals.tf and
`test_terraform_makes_no_job_for_fix_loop` fails; add
`ParkReason.FIX_ROUNDS_RUNNING` to PREWARM_REASONS and
`test_no_scheduler_sweep_promotes_fix_rounds_running` fails; drop the member
from `PARK_REASONS` in apps/swarm-ui/src/types.ts and
`test_the_contract_parity_script_passes` fails; take `FIX_LOOP_EXHAUSTED`
out of `EndCause` (or put "Not applied" back on request 126's row) and
`test_request_c_is_accepted_and_applied` fails.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from swarm_common.models import EndCause
from swarm_common.profiles import RUNNER_PROFILES, Backend
from swarm_common.states import (
    CONCURRENCY_STATES,
    PENDING_STATES,
    TERMINAL_STATES,
    ParkReason,
    TaskState,
    can_transition,
    holds_capacity,
)

REPO = Path(__file__).resolve().parents[3]
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"
REQUESTS = REPO / "docs" / "contract-change-requests.md"
SCHEDULER = REPO / "apps" / "scheduler" / "scheduler"
PARITY = REPO / "scripts" / "lib" / "check-contract-parity.sh"


# --------------------------------------------------------------------------
# Request A (124): the fix-loop entry
# --------------------------------------------------------------------------


def test_fix_loop_is_a_control_plane_profile_that_runs_nothing():
    profile = RUNNER_PROFILES["fix-loop"]
    assert profile.name == "fix-loop"
    assert profile.control_plane is True
    assert profile.runner_argv == ()
    assert profile.worker_action is None
    assert profile.provider is None
    assert profile.secrets == ()
    assert dict(profile.secret_slots) == {}
    assert dict(profile.inputs) == {}
    assert profile.live_browser is False
    assert profile.backend is Backend.CLOUD_RUN_JOB


def test_fix_loop_ships_unavailable_with_a_reason():
    """As await-input did, until FL-ACCEPT passes (design §7 request A)."""
    profile = RUNNER_PROFILES["fix-loop"]
    assert profile.available is False
    assert profile.disabled_reason.strip()


def _profiles_without_a_job() -> set[str]:
    text = LOCALS_TF.read_text(encoding="utf-8")
    match = re.search(r"^\s*profiles_without_a_job\s*=\s*\[([^\]]*)\]", text, re.M)
    assert match, "profiles_without_a_job not found in terraform/infra/locals.tf"
    return set(re.findall(r'"([a-z0-9-]+)"', match.group(1)))


def test_terraform_makes_no_job_for_fix_loop():
    without = _profiles_without_a_job()
    assert "fix-loop" in without
    control_plane = {name for name, p in RUNNER_PROFILES.items() if p.control_plane}
    assert control_plane == {"await-input", "fix-loop"}
    assert control_plane <= without, sorted(control_plane - without)


def test_terraform_mirrors_fix_loop():
    text = LOCALS_TF.read_text(encoding="utf-8")
    entry = re.search(r'^\s*"fix-loop"\s*=\s*\{(.*?)\n\s*\}', text, re.M | re.S)
    assert entry, "fix-loop is not in terraform/infra/locals.tf runner_profiles"
    body = entry.group(1)
    profile = RUNNER_PROFILES["fix-loop"]
    assert re.search(rf'image\s*=\s*"{profile.image}"', body)
    assert re.search(rf'resource_class\s*=\s*"{profile.resource_class}"', body)
    assert re.search(r'backend\s*=\s*"CLOUD_RUN_JOB"', body)
    assert re.search(r"provider\s*=\s*null", body)
    assert re.search(r"secret_env\s*=\s*\{\}", body)
    assert re.search(rf"timeout_seconds\s*=\s*{profile.timeout_seconds}\b", body)


# --------------------------------------------------------------------------
# Request B (125): the park reason
# --------------------------------------------------------------------------


def test_fix_rounds_running_is_a_park_reason():
    assert ParkReason("FIX_ROUNDS_RUNNING") is ParkReason.FIX_ROUNDS_RUNNING
    assert ParkReason.FIX_ROUNDS_RUNNING is not ParkReason.HUMAN_REQUIRED
    assert ParkReason.FIX_ROUNDS_RUNNING is not ParkReason.AWAITING_INPUT


def test_fix_rounds_running_is_in_no_capacity_set():
    """Invariant 1: the reason rides on PARKED, which costs nothing."""
    reason = ParkReason.FIX_ROUNDS_RUNNING.value
    for states in (CONCURRENCY_STATES, PENDING_STATES, TERMINAL_STATES):
        assert reason not in {s.value for s in states}
    assert TaskState.PARKED in PENDING_STATES
    assert TaskState.PARKED not in CONCURRENCY_STATES
    assert not holds_capacity(TaskState.PARKED)


def test_swarm_api_can_end_the_park_on_edges_that_already_exist():
    """PARKED -> SUCCEEDED / FAILED / CANCELLED, all already allowed."""
    for to in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
        assert can_transition(TaskState.PARKED, to), to


def test_no_scheduler_sweep_promotes_fix_rounds_running():
    """The scheduler's promotable set does not hold it.

    The scheduler promotes a park through a sweep that names its reason, or
    early through PREWARM_REASONS. The control: the same read finds the
    reasons the scheduler does promote, so a read that matched nothing would
    fail here rather than pass vacuously.
    """
    from scheduler.loop import PREWARM_REASONS

    assert ParkReason.FIX_ROUNDS_RUNNING not in PREWARM_REASONS
    named: set[str] = set()
    for path in sorted(SCHEDULER.glob("*.py")):
        named |= set(re.findall(r"_parked_window\(\s*ParkReason\.([A-Z_]+)", path.read_text(encoding="utf-8")))
    assert {"SCHEDULED_RETRY", "DEPENDENCY_INCOMPLETE", "CI_PENDING"} <= named, named
    assert "FIX_ROUNDS_RUNNING" not in named
    for path in sorted(SCHEDULER.glob("*.py")):
        assert "FIX_ROUNDS_RUNNING" not in path.read_text(encoding="utf-8"), path


def test_the_contract_parity_script_passes():
    """Every shell, jq and TypeScript restatement the script checks carries the
    new member: the console's ParkReason union is one of them."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_")}
    result = subprocess.run(
        ["bash", str(PARITY)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, report[-6000:]
    members = len(list(ParkReason))
    assert re.search(rf"ok types\.ts ParkReason: {members} member", report), report[-6000:]


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def _row(number: str) -> str:
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith(f"| {number} |")]
    assert len(rows) == 1, rows
    return rows[0]


def test_requests_a_and_b_are_filed_as_accepted_and_applied():
    text = REQUESTS.read_text(encoding="utf-8")
    for number, letter in (("124", "fix loop request A"), ("125", "fix loop request B")):
        row = _row(number)
        assert "FL-CONTRACT" in row and letter in row
        # "accepted by the owner" is the mark scripts/lib/check-frozen-contract.sh
        # looks for in an added line; without it the swarm_common diff is refused.
        assert "ACCEPTED by the owner (BGD) 2026-10-27 (Q1, Q2, Q10" in row and "APPLIED" in row
        assert re.search(rf"^## {number}\. .*\(FL-CONTRACT, {letter}\)$", text, re.M)


def test_request_c_is_accepted_and_applied():
    """Q3 (c) ends a loop FAILED `fix_loop_exhausted`. BGD accepted request 126
    on 2026-10-27 ('accept-126'), so the frozen enum has the member, appended
    after request 118's `budget_exceeded` so no reader's order moves.

    MUTATION: drop `FIX_LOOP_EXHAUSTED` from `EndCause` and the enum asserts
    fail; leave request 126's row saying "Not applied" and the record asserts
    fail; drop "accepted by the owner" from the row and
    scripts/lib/check-frozen-contract.sh would refuse the swarm_common diff.
    """
    assert EndCause.FIX_LOOP_EXHAUSTED.value == "fix_loop_exhausted"
    assert EndCause("fix_loop_exhausted") is EndCause.FIX_LOOP_EXHAUSTED
    causes = list(EndCause)
    assert causes[-1] is EndCause.FIX_LOOP_EXHAUSTED
    assert causes[-2] is EndCause.BUDGET_EXCEEDED
    row = _row("126")
    assert "fix loop request C" in row
    assert "ACCEPTED by the owner (BGD) 2026-10-27" in row and "APPLIED" in row
    assert "Not applied" not in row
    text = REQUESTS.read_text(encoding="utf-8")
    section = text.split("## 126. ", 1)[1].split("\n## ", 1)[0]
    assert "**Status:** ACCEPTED by the owner (BGD) 2026-10-27" in section


def test_the_console_names_the_new_end_cause():
    """The parity script holds the console's `EndCause` union and `END_CAUSES`
    to the frozen enum; this pins that it counted the new member."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_")}
    result = subprocess.run(
        ["bash", str(PARITY)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, report[-6000:]
    members = len(list(EndCause))
    assert re.search(rf"ok types\.ts END_CAUSES: {members} member", report), report[-6000:]
