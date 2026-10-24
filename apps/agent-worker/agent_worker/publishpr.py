"""A lane that publishes onto an existing pull request's branch (`publish_to_pr`, #2183).

WHY (owner approval, BGD, 2026-10-24): a follow-up lane -- a re-review, a fix
round, the owner's answers applied to a mock-up -- had to start from main,
merge the old branch, and open a REPLACEMENT pull request, which someone then
closed by hand. Ten lanes did that on 2026-10-23 for PRs 2164-2168 and
2170-2174. Fixing a pull request in place was possible only from the laptop.

WHAT SWARM-API SIGNS. `metadata.dispatch.publish_to_pr` is
`{"number": N, "task_id": "task_<20 hex>"}`. swarm-api wrote it at submission
(`swarm_api.publishtopr`), after reading pull request N of the spec's own
repository with the tenant's credential and finding it open, not from a fork,
and on `swarm/<task_id>`, where `task_id` is a task of the submitting tenant.
It is inside the dispatch block, so the step-spec signature covers it, and
the worker reads it only from the verified document. The branch is DERIVED
from `task_id` with this worker's own prefix, never read as a name, exactly as
`continuation.py` derives a continued branch.

WHAT THE WORKER CHECKS, TWICE. Before the clone, and again before the push,
the pull request is read from GitHub (`forge.read_pull_request`) and held to
`refusal` below: open, not merged, its head in the task's own repository, on
the branch the signed `task_id` derives. Anything else fails the step with
the reason and runs nothing -- the agent never starts, nothing is pushed.

WHERE THE WORK STARTS AND LANDS. A step that clones the workflow's ref
clones the pull request's head COMMIT instead (a step that `builds_on` an
upstream step still clones that step's branch, which stands on the same head).
The head it read at the start is recorded, in the checkpoint manifest too, so
a resumed attempt keeps the same one. The step that opens the pull request --
a `direct-pr` step, an `integrate` integrator -- pushes onto the pull
request's branch instead of its own, opens nothing, and only fast-forwards:
`head_moved` refuses a head that is not the one the step started from, or
that the lane's HEAD does not descend from, and the push itself is leased on
that same sha, so a head moved between the read and the push is refused by
the forge. Never a force.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import WorkerError
from .forge import PullHead

#: The dispatch block's key, as `swarm_api.publishtopr.DISPATCH_FIELD` writes it.
FIELD = "publish_to_pr"

#: The shape `swarm_common.models.new_id("task")` mints, as `continuation.py`
#: holds a continued task id to it.
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{20}$")

#: A full commit sha, as GitHub reports a pull request's head.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: The words a moved head is refused with; the issue (#2183) and the
#: owner's brief name them, so a reader can grep a run's record for them.
HEAD_MOVED = "head moved"


@dataclass(frozen=True)
class Target:
    """The pull request a step publishes onto, as swarm-api signed it."""

    number: int
    #: The task whose `<prefix><task_id>` branch is the pull request's head.
    task_id: str

    def branch(self, prefix: str) -> str:
        return f"{prefix}{self.task_id}"

    def to_record(self, prefix: str) -> dict[str, Any]:
        return {"number": self.number, "branch": self.branch(prefix)}


def target(metadata: Any) -> Target | None:
    """The signed `publish_to_pr` of a task's metadata, or None when absent.

    Raises WorkerError for a value swarm-api could not have written: a number
    that is not a positive integer, a task id of the wrong shape, or the key
    beside a `continues` or a `single-pr` role, which choose a branch of
    their own.
    """
    if not isinstance(metadata, dict):
        return None
    block = metadata.get("dispatch")
    if not isinstance(block, dict) or FIELD not in block:
        return None
    raw = block.get(FIELD)
    number = raw.get("number") if isinstance(raw, dict) else None
    task_id = raw.get("task_id") if isinstance(raw, dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise WorkerError(
            "metadata.dispatch.publish_to_pr names no pull request number; refusing "
            "to publish onto a pull request it does not name"
        )
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise WorkerError(
            "metadata.dispatch.publish_to_pr names no task id; the pull request's "
            "branch cannot be derived from it, so nothing was cloned or pushed"
        )
    if block.get("continues") or block.get("pr_role"):
        raise WorkerError(
            "metadata.dispatch.publish_to_pr is set beside continues or pr_role, "
            "which choose another branch; refusing both"
        )
    return Target(number=number, task_id=task_id)


def refusal(pull: PullHead, *, target: Target, repository: str, branch_prefix: str) -> str | None:
    """Why the step may not start from, or publish onto, `pull`; None when it may.

    `repository` is `owner/repo` of the task's own repository, as the forge
    reported it. Compared without case, as GitHub compares them.
    """
    name = f"pull request #{target.number} of {repository}"
    if pull.number != target.number:
        return f"the forge answered for #{pull.number}, not {name}"
    if pull.merged:
        return f"{name} is already merged; a lane publishes only onto an open pull request"
    if pull.state != "open":
        return f"{name} is closed; a lane publishes only onto an open pull request"
    if not pull.base_repo or pull.base_repo.lower() != repository.lower():
        return f"{name} is in {pull.base_repo or 'an unknown repository'}, not {repository}"
    if not pull.head_repo or pull.head_repo.lower() != repository.lower():
        return (
            f"{name} comes from a fork ({pull.head_repo or 'an unknown repository'}); "
            f"a lane pushes only to a branch of {repository} itself"
        )
    expected = target.branch(branch_prefix)
    if pull.head_ref != expected:
        return (
            f"{name}'s head branch is {pull.head_ref[:120]!r}, not {expected!r}, the "
            "branch this tenant's lane published that swarm-api signed into this step"
        )
    if not _SHA_RE.match(pull.head_sha):
        return f"the forge reported no head commit for {name}"
    return None


def head_moved(*, started: str | None, now: str, number: int) -> str | None:
    """Why the push would not fast-forward the head this step started from, or None."""
    if not started or not _SHA_RE.match(started):
        return (
            f"{HEAD_MOVED}: the head of pull request #{number} this step started from "
            "is not recorded, so a fast-forward onto it cannot be proved; nothing was pushed"
        )
    if now != started:
        return (
            f"{HEAD_MOVED}: pull request #{number} was at {started[:12]} when this "
            f"step started and is at {now[:12]} now; nothing was pushed and nothing "
            "was forced. Run the lane again from the new head"
        )
    return None
