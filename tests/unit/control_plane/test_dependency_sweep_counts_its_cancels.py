"""A cancel the dependency sweep makes is a cancel the drain reports.

Incident wf_ebb3ab2d65664707a559, 2026-09-24T07:40:11Z. The UI cancelled
`check-6`; on the next drain the dependency sweep at the top of `drain()`
(`Scheduler._promote_dependencies`) found `synthesis` PARKED behind it and
cancelled it -- correctly, and visibly in the task's own events
(`failed_parents=[check-6]`). The drain that did it finished at 07:40:11.608Z
and reported

    cancelled: 0

because the sweep's cancel path incremented nothing. `_admit_one` counts the
cancels it makes; the sweep, which makes the same cancel for the same reason,
did not. An operator reading the scheduler's own summary line -- the one place
that says what a drain changed -- was told nothing had been cancelled at the
moment a workflow step was.

The metric had the same hole, worse: there was no cancel counter at all, on
any path. `swarm_scheduler_cancelled_total{reason}` now counts every cancel a
drain makes, and the report and the metric are asserted to agree, the same way
`test_a_dependency_free_promotion_is_counted_in_the_metric_too` holds the
promotion counters together.
"""

from __future__ import annotations

from .conftest import seed_pool, seed_task, seed_tenant


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _metric(scheduler, reason: str) -> float:
    rendered = scheduler.metrics.render()[0].decode("utf-8")
    needle = f'swarm_scheduler_cancelled_total{{reason="{reason}"}} '
    for line in rendered.splitlines():
        if line.startswith(needle):
            return float(line[len(needle):])
    return 0.0


def test_a_parked_dependent_of_a_cancelled_parent_is_counted_when_swept(db, make_scheduler):
    """THE MEASURED CASE: the parent was CANCELLED by the user, the dependent
    was PARKED behind it, and the sweep cancelled the dependent."""
    _base(db)
    seed_task(db, task_id="task_check6", tenant_id="eng", state="CANCELLED")
    seed_task(
        db,
        task_id="task_synthesis",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_check6",),
    )

    scheduler = make_scheduler()
    report = scheduler.drain()

    assert db.docs["tasks/task_synthesis"]["state"] == "CANCELLED"
    assert report.cancelled == 1, (
        f"the sweep cancelled task_synthesis and the drain reported "
        f"cancelled={report.cancelled}; this is the 07:40:11Z `cancelled: 0`"
    )
    assert _metric(scheduler, "failed_parent") == 1.0


def test_the_report_and_the_metric_agree_across_every_cancel_path(db, make_scheduler):
    """Three cancels, three paths, one total.

    * the sweep cancels a PARKED dependent of a FAILED parent;
    * `_admit_one` cancels a READY task whose parent FAILED;
    * `_admit_one` cancels a READY task whose cancel was requested.
    """
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="FAILED")
    seed_task(
        db,
        task_id="task_parked_child",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_parent",),
    )
    seed_task(
        db, task_id="task_ready_child", tenant_id="eng", depends_on=("task_parent",)
    )
    seed_task(db, task_id="task_flagged", tenant_id="eng", cancel_requested=True)

    scheduler = make_scheduler()
    report = scheduler.drain()

    for task_id in ("task_parked_child", "task_ready_child", "task_flagged"):
        assert db.docs[f"tasks/{task_id}"]["state"] == "CANCELLED", task_id
    assert report.cancelled == 3, report.to_dict()
    assert _metric(scheduler, "failed_parent") == 2.0
    assert _metric(scheduler, "cancel_requested") == 1.0


def test_a_sweep_that_cancels_nothing_counts_nothing(db, make_scheduler):
    """The regression guard in the other direction: a promotion is not a
    cancel, and a still-running parent leaves the dependent PARKED."""
    _base(db)
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_running", tenant_id="eng", state="RUNNING")
    seed_task(
        db,
        task_id="task_promoted",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_done",),
    )
    seed_task(
        db,
        task_id="task_waiting",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_running",),
    )

    scheduler = make_scheduler()
    report = scheduler.drain()

    assert report.cancelled == 0
    assert report.promoted_dependencies == 1
    assert _metric(scheduler, "failed_parent") == 0.0
    assert db.docs["tasks/task_waiting"]["state"] == "PARKED"
