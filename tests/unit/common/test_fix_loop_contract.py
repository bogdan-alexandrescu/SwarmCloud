# source-text: contract - locals.tf's fix-loop entry and profiles_without_a_job must mirror RUNNER_PROFILES, and the scheduler's _parked_window reasons and loop.py's _park_control_plane must keep the FIX_ROUNDS_RUNNING park
"""Contract requests 124-126: the fix loop's frozen contract (FL-CONTRACT, part of archived issue 2509).

docs/design/fix-loop.md §7 requests A and B, accepted by the owner on 2026-10-27
(Q1, Q2 and Q10 of §9). What is held here:

  * the `fix-loop` entry is `control_plane`, is available (contract request
    151, applied again 2026-11-23 on the owner's decision), Terraform makes
    no Job for it (`profiles_without_a_job`), and its Terraform mirror states
    what the Python entry does;
  * `ParkReason.FIX_ROUNDS_RUNNING` exists, rides on PARKED and so holds no
    capacity (invariant 1), and no scheduler sweep promotes it: the scheduler
    promotes a park only through a sweep that names its reason
    (`_parked_window(ParkReason.X)`), or through PREWARM_REASONS;
  * `scripts/lib/check-contract-parity.sh` still passes with the new member,
    and the member reaches the generated `states.json` the console is held to
    (`scripts/api_constants/states.py`; the console half is
    apps/swarm-ui/src/__tests__/contract.parity.test.ts, part of #72);
  * the requests are filed: 124 and 125 accepted and applied, and 126 (the end
    cause Q3 (c) needs) accepted by the owner on 2026-10-27 and applied: the frozen
    `EndCause` has `FIX_LOOP_EXHAUSTED`, appended, the outcome ledger classes
    it as its own failure class, and it reaches the generated `states.json`,
    whose `--check` fails CI until the console's `END_CAUSES` follows.

MUTATION: set `available=False` (with a reason) on `fix-loop` and
`test_fix_loop_is_available` fails; drop "fix-loop" from
`profiles_without_a_job` in locals.tf and
`test_terraform_makes_no_job_for_fix_loop` fails; add
`ParkReason.FIX_ROUNDS_RUNNING` to PREWARM_REASONS and
`test_no_scheduler_sweep_promotes_fix_rounds_running` fails; make the states
group publish `park_reason` without the member and
`test_the_contract_parity_script_passes` fails; take `FIX_LOOP_EXHAUSTED`
out of `EndCause` (or put "Not applied" back on request 126's row) and
`test_request_c_is_accepted_and_applied` fails.
"""

from __future__ import annotations

import importlib.util
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
SCHEDULER = REPO / "apps" / "scheduler" / "scheduler"
PARITY = REPO / "scripts" / "lib" / "check-contract-parity.sh"
STATES_GROUP = REPO / "scripts" / "api_constants" / "states.py"


def _published_states() -> dict:
    """What the `states` constants group publishes to the console's
    states.json (`scripts/gen_api_types.py --check` holds the file to it)."""
    spec = importlib.util.spec_from_file_location("fix_loop_states_group", STATES_GROUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


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


def test_fix_loop_is_available():
    """Contract request 151, applied again 2026-11-23 on the owner's decision:
    `available=True` and its `disabled_reason` removed, and nothing else about
    the entry changed."""
    profile = RUNNER_PROFILES["fix-loop"]
    assert profile.available is True
    assert profile.disabled_reason == ""
    assert profile.control_plane is True


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
    # FL-SCHED parks a READY fix loop on it (docs/design/fix-loop.md §4.7), so
    # the scheduler names it once: the park `_park_control_plane` writes. That
    # no other module names it, by member or by value, and loop.py only once, is
    # the rule fix-rounds-running-only-in-the-control-plane-park
    # (scripts/source_text_rules/services.py); this holds where the one is.
    loop = (SCHEDULER / "loop.py").read_text(encoding="utf-8")
    park = loop[loop.index("def _park_control_plane"):]
    park = park[:park.index("\n    def ")]
    assert "ParkReason.FIX_ROUNDS_RUNNING" in park


def test_the_contract_parity_script_passes():
    """Every shell, jq and Terraform restatement the script checks carries the
    new member, and the member reaches the park reasons published to the
    console, which contract.parity.test.ts holds the ParkReason union to."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_")}
    result = subprocess.run(
        ["bash", str(PARITY)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, report[-6000:]
    published = _published_states()["park_reason"]
    assert published == [m.value for m in ParkReason]
    assert ParkReason.FIX_ROUNDS_RUNNING.value in published


def test_request_c_is_accepted_and_applied():
    """Q3 (c) ends a loop FAILED `fix_loop_exhausted`. the owner accepted request 126
    on 2026-10-27 ('accept-126'), so the frozen enum has the member, appended
    after request 118's `budget_exceeded` so no reader's order moves.

    MUTATION: drop `FIX_LOOP_EXHAUSTED` from `EndCause` and the enum asserts
    fail.
    """
    assert EndCause.FIX_LOOP_EXHAUSTED.value == "fix_loop_exhausted"
    assert EndCause("fix_loop_exhausted") is EndCause.FIX_LOOP_EXHAUSTED
    causes = list(EndCause)
    assert causes[-1] is EndCause.FIX_LOOP_EXHAUSTED
    assert causes[-2] is EndCause.BUDGET_EXCEEDED


def test_the_new_end_cause_reaches_the_console_constants():
    """The end causes published to the console's states.json are the frozen
    enum in its order, the new member last: contract.parity.test.ts holds the
    console's `END_CAUSES` to that list, in order."""
    published = _published_states()["end_cause"]
    assert published == [c.value for c in EndCause]
    assert published[-1] == EndCause.FIX_LOOP_EXHAUSTED.value
