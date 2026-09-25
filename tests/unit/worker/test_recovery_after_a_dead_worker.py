"""A worker dies mid-attempt. What happens next, end to end, and how long it takes.

MEASURED, on 2026-09-22: a task read DISPATCHED for about twenty minutes against
an execution that was gone, and nothing in the platform said so. It recovered --
the reconciler fenced the generation, released the lease and the task ran again
under a later one -- but the recovery was watched rather than asserted, and the
twenty minutes were discovered rather than predicted.

Two separate things were missing, and this file is both of them.

**THE CHAIN WAS NEVER RUN THROUGH.** Every link had its own test:
`test_reconciler_safety.py` proves the repair ordering, `test_fencing.py` proves
a stale worker exits without running the agent, and the worker suites prove an
attempt succeeds. Nothing ran them in sequence against one document store, so
nothing asserted that the state the reconciler leaves behind is a state a later
generation can actually start from. That is the same shape as every other defect
of the last three days: both ends built, the seam never executed.

**THE DURATION WAS NEVER A NUMBER.** "It recovers eventually" is not an
operational promise. The bound is fully determined by configuration -- the
reconciler's grace thresholds and how often its tick fires -- and both halves
live in different files that nobody had ever compared. `test_the_recovery_bound`
below computes it from the two sources and pins it, so a change to either shows
up as a changed number rather than as a longer outage.

WHY THIS IS NOT ALSO AN IN-VPC CHECK, said plainly. Killing a worker on the
deployed platform needs `run.executions.cancel`, which `swarm-verify` does not
hold; the identity is read-only by design (terraform/infra/verify.tf) and
whether to widen it is the same open question that keeps race-test from running
-- see the top of scripts/race-test.sh. There is no caller-reachable way to make
a worker die uncleanly, and there should not be: every path a task can request
(cancel, timeout, failure, quota park) ends in the worker tidying up after
itself, which is the opposite of the case under test. So the kill happens here,
at the lowest seam that can be injected, and this docstring says so rather than
letting a green suite imply the live platform was tested.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import ExplodingChildProcess, FakeBackend, FakeFirestore, FakeTransactionRunner

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger as build_reconciler_logger
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt, seed_tenant

DEAD_GENERATION = 3
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider=None,
    resource_class="standard",
    runner_profile="mock",
    backend="CLOUD_RUN_JOB",
)


@pytest.fixture
def reconciler_config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def kill_the_worker(
    db: FakeFirestore,
    *,
    task_id: str = "task_1",
    lease_id: str = "lease_dead",
    attempt_id: str = "att_dead",
    silent_seconds: int = 600,
) -> None:
    """Leave behind exactly what a worker that was killed leaves behind.

    Not a simulation of a crash but its RESIDUE, which is the only thing the
    reconciler can see: a task in RUNNING, a lease nobody has released whose
    heartbeat stopped, an attempt naming an execution, and a pool slot still
    reserved. A worker SIGKILLed between two heartbeats leaves precisely this
    and nothing else -- there is no cleanup path, which is the whole reason the
    reconciler exists.
    """
    now = utcnow()
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id, "tenant_id": TENANT, "state": TaskState.RUNNING.value,
            "runner_profile": "mock", "resource_class": "standard",
            "input": {"prompt": "the work the dead worker was doing",
                      "steps": 1, "sleep_seconds": 0.05,
                      "artifact_name": "recovered.txt",
                      "artifact_text": "written by the generation that finished\n"},
            "current_generation": DEAD_GENERATION, "current_lease_id": lease_id,
            "attempt_count": 1, "max_attempts": 3, "updated_at": now,
            "cancel_requested": False, "timeout_seconds": 600,
        },
    )
    db.seed(
        f"leases/{lease_id}",
        {
            "lease_id": lease_id, "task_id": task_id, "attempt_id": attempt_id,
            "tenant_id": TENANT, "generation": DEAD_GENERATION,
            "pools": POOLS, "units": 1, "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(seconds=silent_seconds + 60),
            "dispatch_deadline": now - timedelta(seconds=silent_seconds + 60)
            + timedelta(seconds=300),
            "expires_at": now - timedelta(seconds=silent_seconds) + timedelta(seconds=120),
            "heartbeat_at": now - timedelta(seconds=silent_seconds),
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{attempt_id}",
        {
            "attempt_id": attempt_id, "task_id": task_id, "tenant_id": TENANT,
            "generation": DEAD_GENERATION, "lease_id": lease_id,
            "backend": "CLOUD_RUN_JOB",
            "created_at": now - timedelta(seconds=silent_seconds + 60),
            "started_at": now - timedelta(seconds=silent_seconds + 30),
            "execution_name": (
                "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1"
            ),
        },
    )
    for pool in POOLS:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 1,
                                  "enabled": True, "updated_at": now})
    seed_tenant(db)


def reconcile(db: FakeFirestore, config: ReconcilerConfig, *, executions: list[Any] | None = None):
    """One real reconciler pass. The execution list is what the backend reports."""
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(executions=list(executions or []), journal=db.writes)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


def test_a_dead_worker_is_fenced_released_requeued_and_a_later_generation_finishes(
    db, store, worker_factory, reconciler_config, monkeypatch
):
    """The whole recovery, in the order it actually happens.

    Four claims, and each one is a different component:

      1. the reconciler notices and repairs -- generation fenced, lease
         released, slot returned, task back to READY;
      2. the DEAD worker, if it was only wedged rather than gone, exits without
         running the agent. This is invariant 5 and the only unrecoverable
         failure in the execution plane: two agents on one task, one set of
         credentials, one repository;
      3. a later generation starts from the state the reconciler left and runs
         to completion;
      4. it produces its output. A task that reaches SUCCEEDED having produced
         nothing is the failure mode this repository spent three days on, so
         "it recovered" is asserted against the artifact rather than the state.
    """
    kill_the_worker(db)

    # 1. The reconciler's pass. The execution is GONE, which is what a killed
    #    worker's execution looks like once Cloud Run has reaped it.
    report = reconcile(db, reconciler_config, executions=[])

    assert report.outcomes, "the reconciler found nothing to repair"
    outcome = report.outcomes[0]
    assert outcome.kind in ("stale_lease", "missing_execution")
    assert outcome.released is True
    assert outcome.repaired_to == TaskState.READY.value

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value
    assert task["current_generation"] == DEAD_GENERATION + 1, (
        "the generation was not bumped, so the dead worker is not fenced and "
        "could still write to this task"
    )
    assert db.doc("leases/lease_dead")["released_at"] is not None
    for pool in POOLS:
        assert db.doc(f"pools/{pool}")["active"] == 0, pool
    assert EventType.GENERATION_FENCED.value in db.event_types("task_1")

    # 2. The dead worker turns out not to be dead. It must exit without running
    #    the agent -- any construction of a child process here is the test
    #    failing, not an assertion afterwards.
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    stale, _config, _exporter = worker_factory(
        task_id="task_1", attempt_id="att_dead", lease_id="lease_dead",
        generation=DEAD_GENERATION,
    )
    assert stale.run() == ExitCode.GENERATION_FENCED
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value, (
        "the fenced worker wrote a terminal state over a task that had already "
        "been requeued"
    )
    monkeypatch.undo()

    # 3. The scheduler admits it again: a new lease and a new attempt at the
    #    generation the reconciler minted. Seeded rather than run, because the
    #    scheduler is a separate service -- what is under test here is that the
    #    worker can start from what the reconciler left.
    seed_attempt(
        db,
        task_id="task_1",
        attempt_id="att_recovered",
        lease_id="lease_recovered",
        generation=DEAD_GENERATION + 1,
        state=TaskState.LEASED,
        task_input=dict(task["input"]),
    )
    recovered, _config, _exporter = worker_factory(
        task_id="task_1", attempt_id="att_recovered", lease_id="lease_recovered",
        generation=DEAD_GENERATION + 1,
    )
    assert recovered.run() == ExitCode.OK

    finished = db.doc("tasks/task_1")
    assert finished["state"] == TaskState.SUCCEEDED.value

    # 4. It produced what it was asked for, under the RECOVERED attempt's own
    #    prefix -- so a downstream step's input_from resolves against the
    #    attempt that actually succeeded, not the one that died.
    names = {entry["name"] for entry in finished["result_summary"]["artifacts"]}
    assert "recovered.txt" in names, sorted(names)
    key = (f"tenants/{TENANT}/tasks/task_1/attempts/att_recovered/artifacts/"
           "recovered.txt")
    assert store.download_bytes(key).decode("utf-8") == (
        "written by the generation that finished\n"
    )

    # And the slot the dead worker held is not held twice.
    for pool in POOLS:
        assert db.doc(f"pools/{pool}")["active"] == 0, pool


def test_the_repair_does_not_burn_the_tasks_remaining_attempts(
    db, reconciler_config
):
    """A worker the platform killed must not cost the task one of its three tries.

    `attempt_count` is what decides between requeueing and dead-lettering, so a
    reconciler that incremented it would turn three infrastructure interruptions
    into a permanently failed task -- and the task's own code would be blamed.
    """
    kill_the_worker(db)
    before = db.doc("tasks/task_1")["attempt_count"]
    reconcile(db, reconciler_config, executions=[])
    assert db.doc("tasks/task_1")["attempt_count"] == before


def test_a_task_out_of_attempts_is_failed_rather_than_looping(db, reconciler_config):
    """The other side of the same decision, so the fix above cannot overshoot.

    Requeueing unconditionally is an infinite loop that holds a slot every time
    round; the bound has to stay.
    """
    kill_the_worker(db)
    db.doc("tasks/task_1")["attempt_count"] = 3
    report = reconcile(db, reconciler_config, executions=[])
    assert report.outcomes[0].repaired_to == TaskState.FAILED.value
    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value
    for pool in POOLS:
        assert db.doc(f"pools/{pool}")["active"] == 0, pool


# ---------------------------------------------------------------------------
# how long
# ---------------------------------------------------------------------------


def test_a_lease_still_within_its_grace_is_left_completely_alone(db, reconciler_config):
    """The threshold from below, which is the expensive direction to get wrong.

    A reconciler that reaps a healthy worker between two heartbeats kills live
    agents; that is worse than recovering slowly. `silent_seconds` here is
    inside `heartbeat_grace_seconds`, and nothing may happen.
    """
    kill_the_worker(db, silent_seconds=30)
    report = reconcile(db, reconciler_config, executions=[])
    assert report.outcomes == [], f"a live worker was reaped: {report.outcomes}"
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_dead")["released_at"] is None
    assert db.doc("pools/global")["active"] == 1


def test_a_lease_past_its_grace_is_repaired_on_the_very_next_pass(db, reconciler_config):
    """And from above: one pass, not two.

    If detection needed a second pass the bound below would be wrong by a whole
    tick, and the bound is the number an operator is given.
    """
    kill_the_worker(db, silent_seconds=reconciler_config.heartbeat_grace_seconds + 30)
    report = reconcile(db, reconciler_config, executions=[])
    assert len(report.outcomes) == 1
    assert report.outcomes[0].repaired_to == TaskState.READY.value


def test_the_recovery_bound_is_computed_from_the_deployment_and_not_from_memory():
    """How long a killed worker costs, as a number, from the two sources that set it.

    The two halves have never been in the same place:

      * the grace thresholds are `ReconcilerConfig` defaults, in
        apps/reconciler/reconciler/config.py;
      * how often a pass runs is `reconciler_schedule` in
        terraform/modules/scheduler/variables.tf.

    Multiplying them out is the whole answer, and reading them together is how
    the disagreement below was found. This test owns the arithmetic so that
    changing either file changes a number a person can see.
    """
    import re

    config = ReconcilerConfig(
        project_id="p", region="r", firestore_database="swarm", enable_gke=False
    )

    terraform = Path(__file__).resolve().parents[3] / "terraform" / "modules" / "scheduler"
    variables = (terraform / "variables.tf").read_text()
    match = re.search(
        r'variable\s+"reconciler_schedule"\s*\{[^}]*default\s*=\s*"([^"]+)"',
        variables,
        re.S,
    )
    assert match, "reconciler_schedule no longer has a literal default in variables.tf"
    cron = match.group(1)

    minutes = re.fullmatch(r"\*/(\d+) \* \* \* \*", cron)
    assert minutes, (
        f"the reconciler tick is now {cron!r}, which this arithmetic cannot "
        "read. Update the bound below by hand and say what it became."
    )
    tick_seconds = int(minutes.group(1)) * 60

    # The two cases, and they are not the same length.
    stale_lease = config.heartbeat_grace_seconds + tick_seconds
    missing_execution = config.missing_execution_grace_seconds + tick_seconds

    # 150 and 360 since the tick went from */5 to */1 (#198's follow-up):
    # they were 390 and 600.
    assert stale_lease == 150, (
        f"the time to notice a dead worker is now {stale_lease}s "
        f"({config.heartbeat_grace_seconds}s grace + a {tick_seconds}s tick), "
        "not 150s. That is the number the runbook gives an operator."
    )
    assert missing_execution == 360, (
        f"the time to notice a dispatch that never started is now "
        f"{missing_execution}s, not 360s."
    )
    # Re-admission and a cold start come after all of that, which is why the
    # observed end-to-end recovery on 2026-09-22 was around twenty minutes and
    # not six and a half. The bound asserted here is time-to-REPAIR; the
    # remainder belongs to the scheduler and to the image pull.
    assert config.lease_timeout_seconds < stale_lease, (
        "a lease that expires after the reconciler has already called it dead "
        "is two different clocks disagreeing about the same worker"
    )


def test_the_pass_retention_comment_still_matches_the_deployed_tick():
    """A documented number that stopped being true, asserted so it cannot again.

    `ReconcilerConfig.pass_retention_hours` explains itself with "A pass runs
    every minute, so this is 10,080 documents a week at the default". The
    deployed tick is `*/5 * * * *`, so it is 2,016 -- five times fewer. Nobody
    is harmed by the retention being generous, but the sentence is the only
    statement in the repository of how often the reconciler runs, and it is the
    sentence somebody will use to work out why a recovery took as long as it
    did.

    It was a strict xfail while the tick was */5. The tick is */1 now, so the
    comment is true, and this holds the two together.

    The comment is read with its line breaks and `#:` prefixes folded away.
    The sentence wraps after "every", so the old search never found the word
    after it, and the strict xfail was satisfied by "the retention comment no
    longer states how often a pass runs" rather than by the mismatch it
    documented.
    """
    import inspect
    import re

    source = re.sub(r"\s*\n\s*#:?\s*", " ", inspect.getsource(ReconcilerConfig))
    claim = re.search(r"A pass runs every (\w+)", source)
    assert claim, "the retention comment no longer states how often a pass runs"

    terraform = Path(__file__).resolve().parents[3] / "terraform" / "modules" / "scheduler"
    cron = re.search(
        r'variable\s+"reconciler_schedule"\s*\{[^}]*default\s*=\s*"([^"]+)"',
        (terraform / "variables.tf").read_text(),
        re.S,
    ).group(1)

    stated = claim.group(1)
    every_minute = cron in ("* * * * *", "*/1 * * * *")
    assert (stated == "minute") == every_minute, (
        f"apps/reconciler/reconciler/config.py says a pass runs every "
        f"{stated!r}; terraform/modules/scheduler/variables.tf schedules the "
        f"tick as {cron!r}. The retention arithmetic in that comment "
        "(10,080 documents a week) is wrong by the same factor, and that "
        "sentence is the only place this repository says how often the "
        "reconciler runs."
    )
