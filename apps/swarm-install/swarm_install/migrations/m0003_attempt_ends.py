"""Migration 0003: close the attempts a fence, a reclaim or a failed dispatch
superseded and nobody ended (#630; was scripts/backfill-attempt-ends.sh).

Until #630 the superseded attempt kept no `completed_at` for ever, and every
figure read from attempts counted it as running. An attempt is closed only
when it has no `completed_at` and its task is at a LATER generation
(`current_generation > generation`): nothing may run for it any more
(invariant 5). An attempt at its task's current generation is never touched
(the reconciler's lost_after_finish rule owns it), nor one whose task is gone
or whose generation is not a number.

The end is the moment it was superseded, as near as the documents say: the
earliest `created_at` of the task's attempts at a later generation, else its
lease's `released_at`, else the task's `updated_at`. `error` is written only
where it is empty, naming the cause `superseded`; `exit_code` is never
written (none is known). Every write is conditional on the attempt's update
time, so a worker that records its own end in between keeps its account.

An `additive` migration: the writers have recorded the end in the same
transaction since #630, so the running code never reopens what this closes.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import ADDITIVE, Documents, MigrationConflict, Outcome

ATTEMPTS = "attempts"
TASKS = "tasks"
LEASES = "leases"
CAUSE = "superseded"


def _number(value: Any) -> int | None:
    return value if type(value) is int else None


def _order(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class AttemptEnds:
    number = 3
    name = "attempt_ends"
    phase = ADDITIVE
    explain = "superseded attempts that never recorded an end are closed at the moment they were superseded"

    def _plan(self, db: Documents) -> list[dict[str, Any]]:
        attempts = list(db.list(ATTEMPTS))
        by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for _path, data, _ in attempts:
            if isinstance(data.get("task_id"), str):
                by_task[data["task_id"]].append(data)
        tasks: dict[str, dict[str, Any] | None] = {}
        rows = []
        for path, data, update_time in attempts:
            task_id, generation = data.get("task_id"), _number(data.get("generation"))
            if data.get("completed_at") is not None or not isinstance(task_id, str) or generation is None:
                continue
            if task_id not in tasks:
                found = db.get(f"{TASKS}/{task_id}")
                tasks[task_id] = found[0] if found else None
            task = tasks[task_id]
            current = _number((task or {}).get("current_generation"))
            if task is None or current is None or current <= generation:
                continue
            later = [a.get("created_at") for a in by_task[task_id]
                     if (_number(a.get("generation")) or 0) > generation and a.get("created_at") is not None]
            end, source = (min(later, key=_order), "next_attempt") if later else (None, "")
            if end is None and isinstance(data.get("lease_id"), str):
                lease = db.get(f"{LEASES}/{data['lease_id']}")
                if lease and lease[0].get("released_at") is not None:
                    end, source = lease[0]["released_at"], "lease_released"
            if end is None and task.get("updated_at") is not None:
                end, source = task["updated_at"], "task_updated"
            rows.append({"path": path, "update_time": update_time, "end": end, "source": source,
                         "has_error": data.get("error") is not None, "task_generation": current})
        return rows

    def pending(self, db: Documents) -> list[str]:
        """Superseded open attempts that have an end time to close them at."""
        return [row["path"] for row in self._plan(db) if row["end"] is not None]

    def apply(self, db: Documents) -> Outcome:
        changed = refused = undated = 0
        for row in self._plan(db):
            if row["end"] is None:
                undated += 1
                continue
            fields: dict[str, Any] = {"completed_at": row["end"]}
            if not row["has_error"]:
                fields["error"] = (f"{CAUSE}: closed by swarm upgrade migration 0003 (#630); task at generation "
                                   f"{row['task_generation']}, end from {row['source']}")
            try:
                db.update(row["path"], fields, update_time=row["update_time"])
                changed += 1
            except MigrationConflict:
                refused += 1
        return Outcome(changed=changed, refused=refused,
                       detail=f"{undated} superseded with no end time recorded, left open" if undated else "")


MIGRATION = AttemptEnds()
