"""Workflow state: derived from the steps, written back, and checked for drift.

WHY THIS FILE EXISTS. Nothing in this platform ever advanced `Workflow.state`.
`Store.create_workflow` set it once at submission and `Store.cancel_workflow`
only ever touched `cancel_requested`, so every workflow ever created read QUEUED
forever -- `wf_bcdc9180e4fb4a209f31` read QUEUED while its three steps were
SUCCEEDED, FAILED and CANCELLED. The web UI worked around it by computing a
rollup from the steps' own tasks, which is why that screen said "0/3 done"
correctly while the heading beside it still said "queued". Anything that reads
what the API serves -- `sc`, the MCP bridge, a future consumer -- had no such
workaround.

THE SHAPE, which the owner chose deliberately and which is three parts, not two:

  * DERIVE, so the value a reader sees cannot go stale. `derive()` below is the
    single implementation, and `effective_state()` is what every read path
    serves.
  * WRITE, so the value is queryable. A derived field cannot answer "list my
    failed workflows" without loading every workflow's tasks.
  * DRIFT CHECK, because a written value and a derived value are two records of
    one fact, which is the defect shape this repository has spent days removing
    (a pool counter versus a lease sum; Firestore versus the API; a nav label
    versus a page heading). `drift_of()` reports a disagreement; nothing here
    resolves one silently.

WHERE THE DERIVATION LIVES. The pure half -- `derive()`, its precedence tables,
`drift_of()` and `effective_state()` -- is `swarm_rollup` (apps/workflow-rollup),
re-exported below under the names every caller already imports from here. It
moved there so the reconciler's workflow stall check (#616) applies the SAME
rule rather than a second copy of it: the scheduler and the reconciler images
cannot import this package, and `apps/common/swarm_common/` is frozen. What
stays here is the half bound to swarm-api's store and metrics: reading the step
tasks, writing the derived state back, and counting drift. The request to move
the derivation into the frozen contract itself is entry 7 in
docs/contract-change-requests.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.models import Workflow
from swarm_common.states import TERMINAL_STATES, TaskState

# Re-exported, not restated: see "WHERE THE DERIVATION LIVES" above.
from swarm_rollup import (  # noqa: F401
    _PENDING_PRECEDENCE,
    _TERMINAL_SEVERITY,
    SKIPPED_SUMMARY_KEY,
    UNKNOWN,
    StepReading,
    WorkflowRollup,
    _check_coverage,
    derive,
    derive_for,
    drift_of,
    effective_state,
    read_steps,
    skipped_task_ids,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Reading, deriving and writing back
# --------------------------------------------------------------------------

@dataclass
class RollupResult:
    """One workflow's rollup, its drift verdict, and whether it was written."""

    workflow: Workflow
    rollup: WorkflowRollup
    drift: dict[str, Any]
    written: bool = False
    #: The step tasks the read returned, by task id: the workflow list masks
    #: each step's input with its own task's masker (`codec.workflow_to_api`).
    step_tasks: dict[str, Any] = field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        """The fields `codec.workflow_to_api` merges into a workflow's JSON.

        No `stored_state` here: the codec serves that from the workflow document
        itself, so this object cannot become a second place the stored value is
        spelled.
        """
        return {
            "state": effective_state(self.rollup),
            "rollup": self.rollup.to_api(),
            "drift": self.drift,
        }


@dataclass
class SweepReport:
    """What a rollup sweep looked at. Truncation is reported, never hidden."""

    examined: int = 0
    written: int = 0
    agreed: int = 0
    disagreed: int = 0
    unknown: int = 0
    truncated: bool = False
    step_reads: int = 0
    step_read_budget_exhausted: bool = False

    def to_api(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "written": self.written,
            "agreed": self.agreed,
            "disagreed": self.disagreed,
            "unknown": self.unknown,
            # A sweep that stopped at its page limit has NOT seen every
            # workflow, and a caller that treats the counts above as a complete
            # census would be wrong in exactly the way Holders.tsx warns about.
            "truncated": self.truncated,
            "step_reads": self.step_reads,
            "step_read_budget_exhausted": self.step_read_budget_exhausted,
        }


class WorkflowRollups:
    """Reads the step tasks, derives, reports drift, and persists the cache.

    Constructed once in the composition root so a test builds it around the same
    in-memory Firestore the rest of the API uses -- no credentials, no emulator.
    """

    def __init__(self, *, store: Any, metrics: Any = None) -> None:
        self._store = store
        self._metrics = metrics

    # -- one workflow ----------------------------------------------------

    def for_workflow(
        self,
        workflow: Workflow,
        states: Mapping[str, TaskState],
        *,
        absent: Iterable[str] = (),
        persist: bool = True,
        skipped: Iterable[str] = (),
    ) -> RollupResult:
        rollup = derive_for(workflow, states, absent=absent, skipped=skipped)
        result = RollupResult(
            workflow=workflow, rollup=rollup, drift=drift_of(rollup, workflow.state)
        )
        self._record(result)
        if persist:
            result.written = self._persist(result)
        return result

    def for_workflow_from_tasks(
        self,
        workflow: Workflow,
        tasks: Iterable[Any],
        *,
        complete: bool = True,
        persist: bool = True,
    ) -> RollupResult:
        """Rollup from tasks the caller has already loaded.

        `complete=False` means the caller's task read was itself truncated, so a
        step missing from `tasks` was not necessarily absent -- it may simply not
        have been on the page. Passing it through as "not absent" is what makes
        the reason come out as `step_tasks_unread` rather than accusing the data
        of a fault the read cannot establish.
        """
        tasks = list(tasks)
        states = {t.id: t.state for t in tasks}
        absent = (
            [s.task_id for s in workflow.steps if s.task_id and s.task_id not in states]
            if complete
            else []
        )
        return self.for_workflow(
            workflow, states, absent=absent, persist=persist,
            skipped=skipped_task_ids(tasks),
        )

    # -- many workflows --------------------------------------------------

    def for_workflows(
        self,
        tenant_id: str,
        workflows: Sequence[Workflow],
        *,
        persist: bool = True,
    ) -> tuple[list[RollupResult], SweepReport]:
        read = self._store.workflow_step_states(tenant_id, workflows)
        report = SweepReport(
            step_reads=read.reads,
            step_read_budget_exhausted=bool(read.unread),
        )
        results: list[RollupResult] = []
        skipped = skipped_task_ids(read.tasks.values())
        for workflow in workflows:
            result = self.for_workflow(
                workflow, read.states, absent=read.absent, persist=persist,
                skipped=skipped,
            )
            result.step_tasks = {
                step.task_id: read.tasks[step.task_id]
                for step in workflow.steps
                if step.task_id and step.task_id in read.tasks
            }
            results.append(result)
            report.examined += 1
            if result.written:
                report.written += 1
            verdict = result.drift["agrees"]
            if verdict is None:
                report.unknown += 1
            elif verdict:
                report.agreed += 1
            else:
                report.disagreed += 1
        return results, report

    def sweep(
        self, tenant_id: str, *, limit: int
    ) -> tuple[list[RollupResult], SweepReport]:
        """Refresh the written cache for one tenant's non-terminal workflows.

        This is the leg that makes the cache converge for a workflow nobody has
        looked at -- the read paths converge the ones somebody has. It reuses
        `list_workflows`, so it needs no index beyond `workflows-tenant-created`,
        which matters because four of the five indexes this module already
        depends on are still missing from terraform (see the header of store.py).

        Already-terminal workflows are skipped: their steps are all terminal,
        nothing will move them again, and re-reading their tasks every sweep
        would make the sweep's cost grow with the tenant's entire history rather
        than with its live work.
        """
        page = self._store.list_workflows(tenant_id, limit=limit, submitted_by=None)
        live = [w for w in page.items if w.state not in TERMINAL_STATES]
        results, report = self.for_workflows(tenant_id, live, persist=True)
        report.truncated = page.next_page_token is not None
        return results, report

    # -- the write --------------------------------------------------------

    def _persist(self, result: RollupResult) -> bool:
        """Write the derived state back, when and only when it is safe to.

        Three refusals, each of which has a failure it prevents:

          * an INCOMPLETE rollup is never written. Overwriting a real state with
            the product of a failed read is the bug this whole file guards
            against, pointed at the database instead of at the screen.
          * an UNKNOWN state is never written. `codec.workflow_from_dict` calls
            `TaskState(data["state"])`, so a document carrying it would raise on
            every subsequent read -- one bad write would make the workflow
            permanently unreadable.
          * a value that already AGREES is never written, so the steady state
            costs zero Firestore writes and `updated_at` keeps meaning "the
            document changed" rather than "something looked at it".

        No transaction. Two concurrent readers derive from the same step
        documents and therefore write the same value; a reader that raced ahead
        writes a newer one, and the next read of the loser's value re-derives and
        converges. A compare-and-set here would buy nothing and would make every
        read path able to fail on contention.
        """
        if not result.rollup.complete or result.rollup.state == UNKNOWN:
            return False
        if result.drift["agrees"] is not False:
            return False
        state = TaskState(result.rollup.state)
        self._store.set_workflow_state(result.workflow.workflow_id, state)
        # `workflow.state` and `drift["stored"]` are deliberately LEFT at the
        # value that was read. They are the evidence that the cache was wrong,
        # and a response that showed the repaired value in both places would be
        # indistinguishable from one where nothing had ever drifted.
        result.drift["repaired"] = True
        return True

    def _record(self, result: RollupResult) -> None:
        """Report the disagreement BEFORE anything repairs it.

        The write below is a repair, and a repair that leaves no trace is a
        silent resolution -- which is the one thing the owner said this must not
        do. The counter and the log line are taken here, against the drift as it
        was observed, so a systemic disagreement is visible in a dashboard even
        though every individual instance is fixed a moment later.
        """
        verdict = result.drift["agrees"]
        if verdict is True:
            return
        direction = "unknown" if verdict is None else "disagree"
        if self._metrics is not None:
            try:
                self._metrics.workflow_state_drift.labels(direction=direction).inc()
            except Exception:  # pragma: no cover - a metric must never fail a read
                log.debug("could not record workflow state drift", exc_info=True)
        if verdict is None:
            log.info(
                "workflow %s state could not be checked: stored=%s reason=%s "
                "unreadable_steps=%s",
                result.workflow.workflow_id,
                result.workflow.state.value,
                result.rollup.reason,
                result.rollup.unreadable_steps,
            )
        else:
            log.warning(
                "workflow %s state drifted: stored=%s derived=%s reason=%s",
                result.workflow.workflow_id,
                result.workflow.state.value,
                result.rollup.state,
                result.rollup.reason,
            )
