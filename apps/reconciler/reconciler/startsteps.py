"""The pod's start-up steps, read off its status, for the console's progress bar (STARTUP-API).

WHAT THIS IS FOR. The owner, 2026-10-13: "we should put a progress bar when
the browser agent is starting up the browser so you get a sense of progress as
it comes up", with "Stages + real pod steps". A GKE attempt's task reads
DISPATCHED for most of its start-up, and the pod's own progress -- scheduled,
pulling its image, pulled, container started -- was visible to nobody. The
reconciler already reads that pod for `Task.start_problem` (F1-POD,
`startproblem`), so the same read now also yields these steps, and each is
written once per attempt as a `startup_progress` event (contract request 74).

WHAT THE STEPS ARE, AND WHERE EACH TIME COMES FROM. The pod's STATUS, never
Kubernetes Events: the `swarm-reaper` Role this reconciler holds grants `pods`
and `jobs`, not `events`, and the status carries times that do not depend on
when a pass happened to look. Each step's `at` is the pod's own time, so a
pass that runs a minute late still records when the step happened.

  * `pod_scheduled` -- the `PodScheduled` condition turned True.
  * `image_pulling` -- kubelet began preparing the pod's containers: the
    `PodReadyToStartContainers` condition (the sandbox is up; pulls follow at
    once), else kubelet's acknowledgement `status.start_time`. The status has
    no "pull started" time; this is the nearest one it has.
  * `image_pulled` -- the FIRST container of the pod started, init and native
    sidecar included (kubelet starts a container only once its image is
    present). On a browser pod that is the `display` sidecar, whose image is
    the worker's.
  * `container_started` -- the first regular (non-init) container started:
    the worker container, which a browser pod starts only once the sidecar's
    startup probe has passed.

A later step implies the earlier ones happened, but a step is reported only
with a time the pod gave for it; one the status cannot date is left out --
and ONLY that one (LIVE-UNUSED, #1030): a `PodScheduled` condition with no
transition time used to end the read, so a pod that had started all its
containers reported no step at all. A pod placed on a node that is already
up, with its image already there, reads exactly like any other: the
scheduler still sets `PodScheduled`, and kubelet still dates every
container's start, so `image_pulled` is the first start even though nothing
was pulled. On the console a step that never arrives before a later one is
drawn "skipped (already ready)" (StartupProgress.tsx).

IT IS A RECORD, NEVER A DECISION: nothing here changes a task's state, a lease
or a pool (CONTRACT invariants 1-3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from swarm_common.states import TaskState

from .startproblem import StartProblem, _get

#: The pod's steps, in order. The worker's three follow them
#: (`agent_worker.handoff.GATE_STARTUP_STEPS`), and swarm-api serves all seven
#: (`swarm_api.startup.STARTUP_STEPS`); tests/unit/common/
#: test_startup_progress_contract.py holds the three restatements to one list.
POD_STARTUP_STEPS: tuple[str, ...] = (
    "pod_scheduled",
    "image_pulling",
    "image_pulled",
    "container_started",
)

#: The states a step may be written in. RUNNING too: a worker that starts in
#: seconds reaches RUNNING between two passes, and the pod's times are still
#: there to read.
STARTUP_STEP_STATES = frozenset({TaskState.DISPATCHED, TaskState.STARTING, TaskState.RUNNING})

#: How long after its lease was admitted a RUNNING task's pod is still read for
#: steps. Bounds the extra `pods list` to the start-up it describes: a browser
#: attempt is up in a few minutes, and a step not seen by then is not coming.
RUNNING_STEPS_WINDOW_SECONDS = 900


@dataclass(frozen=True)
class StartRead:
    """One read of a Job's pod: why it is not starting, and the steps it has reached."""

    problem: StartProblem | None
    steps: dict[str, datetime] = field(default_factory=dict)


def startup_event_id(attempt_id: str, step: str) -> str:
    """The step's event document id: one per attempt and step, so a re-read cannot write it twice."""
    return f"evt_startup_{attempt_id}_{step}"


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _condition_time(status: Any, kind: str) -> datetime | None:
    for condition in list(_get(status, "conditions") or []):
        if _get(condition, "type") == kind and str(_get(condition, "status")) == "True":
            return _utc(_get(condition, "last_transition_time"))
    return None


def _scheduled(status: Any) -> bool:
    """Whether the pod is on a node: its `PodScheduled` condition is True, dated or not."""
    return any(_get(c, "type") == "PodScheduled" and str(_get(c, "status")) == "True"
               for c in list(_get(status, "conditions") or []))


def _started_at(container_status: Any) -> datetime | None:
    """When a container started, from whichever state records it."""
    state = _get(container_status, "state")
    for name in ("running", "terminated"):
        started = _utc(_get(_get(state, name), "started_at"))
        if started is not None:
            return started
    return _utc(_get(_get(_get(container_status, "last_state"), "terminated"), "started_at"))


def pod_start_steps(pod: Any) -> dict[str, datetime]:
    """The steps one pod has reached, each with the pod's own time for it."""
    status = _get(pod, "status")
    steps: dict[str, datetime] = {}
    if not _scheduled(status):
        return steps
    scheduled = _condition_time(status, "PodScheduled")
    if scheduled is not None:
        steps["pod_scheduled"] = scheduled
    preparing = _condition_time(status, "PodReadyToStartContainers") or _utc(_get(status, "start_time"))
    if preparing is not None:
        steps["image_pulling"] = preparing
    init = [_started_at(s) for s in list(_get(status, "init_container_statuses") or [])]
    regular = [_started_at(s) for s in list(_get(status, "container_statuses") or [])]
    every = [t for t in (*init, *regular) if t is not None]
    if every:
        steps["image_pulled"] = min(every)
    started = [t for t in regular if t is not None]
    if started:
        steps["container_started"] = min(started)
    return steps


def pods_start_steps(pods: list[Any]) -> dict[str, datetime]:
    """The steps of a Job's newest pod. With `backoffLimit: 0` there is one."""
    if not pods:
        return {}
    newest = max(
        pods,
        key=lambda pod: _utc(_get(_get(pod, "metadata"), "creation_timestamp"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    return pod_start_steps(newest)
