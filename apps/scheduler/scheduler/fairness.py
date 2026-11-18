"""Ordering: starvation aging, then round-robin across tenants.

Two separate mechanisms, solving two separate problems, and it matters that they
are separate.

AGING solves starvation WITHIN a tenant. A low-priority task that has waited an
hour should eventually overtake a freshly submitted medium-priority one. The
bonus is capped (`aging_max_bonus`) so aging cannot invert the priority scheme
permanently -- an urgent task must still outrank an ancient trivial one.

ROUND-ROBIN solves starvation ACROSS tenants, and it is the reason a tenant
cannot take the whole platform by submitting ten thousand priority-100 tasks.
Ordering purely by priority would do exactly that, because priority is a value
the caller chooses. So tenants take turns: one task each per round, in a fixed
rotation. Priority and aging decide which of a tenant's OWN tasks goes first and
which tenant opens the rotation; they never decide how many turns a tenant gets.

The interleave is a pure function of its inputs so the fairness property can be
tested without Firestore, a clock, or a dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from swarm_common.models import Task


@dataclass(frozen=True)
class AgingConfig:
    interval_seconds: int = 60
    step: int = 1
    max_bonus: int = 50

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.max_bonus < 0:
            raise ValueError("max_bonus cannot be negative")


def aging_bonus(task: Task, now: datetime, config: AgingConfig) -> int:
    waited = (now - task.created_at).total_seconds()
    if waited <= 0:
        return 0
    return min(config.max_bonus, int(waited // config.interval_seconds) * config.step)


def effective_priority(task: Task, now: datetime, config: AgingConfig) -> int:
    return task.priority + aging_bonus(task, now, config)


def _task_sort_key(task: Task, now: datetime, config: AgingConfig) -> tuple:
    # Highest effective priority first, then oldest first, then id for a total
    # order (two tasks can share a timestamp; a non-total order would make the
    # drain sequence non-deterministic and the fairness test flaky).
    return (-effective_priority(task, now, config), task.created_at, task.id)


def group_by_tenant(tasks: Iterable[Task]) -> dict[str, list[Task]]:
    grouped: dict[str, list[Task]] = {}
    for task in tasks:
        grouped.setdefault(task.tenant_id, []).append(task)
    return grouped


def round_robin_order(
    tasks: Sequence[Task],
    *,
    now: datetime,
    config: AgingConfig,
    active_by_tenant: dict[str, int] | None = None,
) -> list[Task]:
    """Interleave tenants' queues so no tenant can monopolise the scheduler.

    `active_by_tenant` (leases a tenant currently holds) breaks the tie for who
    opens the rotation: a tenant with nothing running goes before a tenant with
    ten running tasks, which is what makes a small tenant's first task start
    promptly even while a large tenant is mid-flood.
    """
    if not tasks:
        return []

    active = active_by_tenant or {}
    grouped = group_by_tenant(tasks)
    for queue in grouped.values():
        queue.sort(key=lambda t: _task_sort_key(t, now, config))

    # Rotation order is computed ONCE and then held fixed for the whole
    # interleave. Recomputing it every round would let the highest-priority
    # tenant re-take the front of the line each time, which is priority
    # ordering wearing a round-robin costume.
    def tenant_key(tenant_id: str) -> tuple:
        head = grouped[tenant_id][0]
        return (
            active.get(tenant_id, 0),
            -effective_priority(head, now, config),
            head.created_at,
            tenant_id,
        )

    rotation = sorted(grouped, key=tenant_key)
    ordered: list[Task] = []
    index = 0
    remaining = sum(len(q) for q in grouped.values())
    while remaining:
        for tenant_id in rotation:
            queue = grouped[tenant_id]
            if index < len(queue):
                ordered.append(queue[index])
                remaining -= 1
        index += 1
    return ordered
