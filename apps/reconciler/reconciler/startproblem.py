"""Why a GKE pod is not starting, read off its container statuses (F1-POD).

WHAT THIS IS FOR. On 2026-10-11 between 20:58 and 21:05 PDT every
claude-code-browser pod's native sidecar crash-looped (`CrashLoopBackOff`,
"No module named agent_worker.livegate.__main__") for about three minutes.
The task page read only "dispatched" / "account not assigned yet", and after
the cancel "never ran", although the pod had started. Nothing read a pod's
container statuses until its Job had ENDED (`GkeBackend.termination`), and a
crash-looping sidecar never ends a Job: the worker container just waits.

So while a task is DISPATCHED or STARTING, the reconciler reads its pod
(`GkeBackend.start_problem`) and records what it finds on the task, as
`metadata.start_problem` -- a platform key, reserved at the API beside
`disk_evictions`, because the frozen `Task` has no field for it. It is cleared
when the pod reads clean or the task reaches RUNNING.

IT IS A RECORD, NEVER A DECISION. Nothing here changes a task's state, a lease
or a pool (CONTRACT invariants 1-3): a pod in `ImagePullBackOff` is still
holding the capacity it was admitted with, and the rules that release a lease
(`detect.py`) judge it exactly as before. This only says why.

THE MESSAGE IS KUBELET'S, about a tenant's pod. It is bounded here to one
printable line of `MAX_START_MESSAGE_CHARS`, the way `detect._one_line` bounds
a cause, and masked when it is SERVED, by the task's masker, as `last_error`
is (`swarm_api.codec.start_problem_to_api`). The reconciler does not depend on
`swarm-redaction`, and the masker that knows the task's own literals lives in
swarm-api.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from swarm_common.states import TaskState

#: Where the record lives on the task. Reserved at the API
#: (`swarm_api.validation.START_PROBLEM_METADATA_KEY`) so no caller can plant
#: one; tests/unit/reconciler/test_pod_start_problem.py holds the two equal.
START_PROBLEM_KEY = "start_problem"

#: kubelet's Waiting reasons that mean a container is failing to start, not
#: merely on its way (`ContainerCreating`, `PodInitializing` are on their way).
START_PROBLEM_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)

#: The states a pod is read in. Before DISPATCHED there is no pod; from
#: RUNNING on, the worker has started, so whatever the pod went through it
#: started -- and the record is cleared.
START_PROBLEM_STATES = frozenset({TaskState.DISPATCHED, TaskState.STARTING})

#: The bound on the recorded message: one line in the console's header.
MAX_START_MESSAGE_CHARS = 300


@dataclass(frozen=True)
class StartProblem:
    """The worst container of a pod that is not starting."""

    container: str
    reason: str
    restarts: int
    message: str
    #: kubelet acknowledged the pod (`status.start_time`), or a container in
    #: it ran or restarted. What lets the console say "started, did not reach
    #: running" rather than "never ran" after a cancel.
    pod_started: bool

    def record(self, *, attempt_id: str | None, seen_at: datetime) -> dict[str, Any]:
        """The task's `metadata.start_problem`."""
        return {
            "container": self.container,
            "reason": self.reason,
            "restarts": self.restarts,
            "message": self.message,
            "pod_started": self.pod_started,
            "attempt_id": attempt_id,
            "seen_at": seen_at,
        }


def start_problem_of(metadata: Any) -> dict[str, Any] | None:
    """The record a task's `metadata` holds, or None. Total: a pass never raises on a document."""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(START_PROBLEM_KEY)
    return dict(value) if isinstance(value, dict) else None


def _get(obj: Any, name: str) -> Any:
    """An attribute of a kubernetes client object, or a key of the same shape as a dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _one_line(text: Any) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    collapsed = " ".join(cleaned.split())
    if len(collapsed) > MAX_START_MESSAGE_CHARS:
        collapsed = collapsed[: MAX_START_MESSAGE_CHARS - 3].rstrip() + "..."
    return collapsed


def _container_problem(status: Any) -> tuple[tuple[int, int], StartProblem] | None:
    """One container's start problem and its rank, or None when it has none."""
    state = _get(status, "state")
    waiting = _get(state, "waiting")
    waiting_reason = str(_get(waiting, "reason") or "")
    restarts = _int(_get(status, "restart_count"))
    failing = waiting_reason in START_PROBLEM_REASONS
    if not failing and restarts == 0:
        return None
    last = _get(_get(status, "last_state"), "terminated")
    reason = waiting_reason if failing else str(_get(last, "reason") or "Restarted")
    exit_code = _get(last, "exit_code")
    words = str(_get(waiting, "message") or _get(last, "message") or "").strip()
    if exit_code is not None:
        words = f"{words} (last exit {exit_code})" if words else f"last exit {exit_code}"
    problem = StartProblem(
        container=str(_get(status, "name") or "?"),
        reason=reason,
        restarts=restarts,
        message=_one_line(words),
        pod_started=False,
    )
    # A container stuck in a start failure outranks one that restarted and
    # now runs; between equals, the one that restarted more.
    return (1 if failing else 0, restarts), problem


def _ran(status: Any) -> bool:
    state = _get(status, "state")
    return (
        _get(state, "running") is not None
        or _get(state, "terminated") is not None
        or _get(_get(status, "last_state"), "terminated") is not None
        or _int(_get(status, "restart_count")) > 0
    )


def pod_start_problem(pod: Any) -> StartProblem | None:
    """The worst start problem among a pod's containers, or None when it is starting normally.

    Init containers first, because a native sidecar (an init container with
    `restartPolicy: Always`, as `livegate` is) reports there, not under
    `container_statuses` -- which is where the 2026-10-11 crash loop was.
    """
    status = _get(pod, "status")
    statuses = [
        *list(_get(status, "init_container_statuses") or []),
        *list(_get(status, "container_statuses") or []),
    ]
    ranked = [found for found in (_container_problem(s) for s in statuses) if found is not None]
    if not ranked:
        return None
    _, worst = max(ranked, key=lambda item: item[0])
    started = _get(status, "start_time") is not None or any(_ran(s) for s in statuses)
    return StartProblem(
        container=worst.container,
        reason=worst.reason,
        restarts=worst.restarts,
        message=worst.message,
        pod_started=started,
    )


def pods_start_problem(pods: list[Any]) -> StartProblem | None:
    """The worst start problem across a Job's pods. With `backoffLimit: 0` there is one."""
    found = [p for p in (pod_start_problem(pod) for pod in pods) if p is not None]
    if not found:
        return None
    return max(found, key=lambda p: (p.reason in START_PROBLEM_REASONS, p.restarts))
