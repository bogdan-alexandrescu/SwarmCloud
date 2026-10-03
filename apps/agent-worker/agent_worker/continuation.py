"""Which branch a `direct-pr` step clones and pushes: its own, or the one it continues (#263).

swarm-api writes `metadata.dispatch.continues` -- a TASK ID -- on the one step
of a `direct-pr` workflow submitted with `continues_task`, after checking that
the task is the caller's own and resolving it to the root of any chain
(`swarm_api.continuation`). The CI fixer uses it so a fix lands on the pull
request that went red, rather than opening a second one stacked on it.

The branch is DERIVED here from that id, with the prefix this worker pushes
under, exactly as `_publish_git` derives an integrator's upstream branches.
Nothing in a task document is ever used as a branch name, so the value is held
to the shape swarm-api mints before it becomes one; anything else was not
written by swarm-api, and the attempt fails rather than push somewhere a
caller chose. The push itself is unchanged: `push_branch` still refuses a
branch outside the prefix and the default branch, and never forces, so a
branch that moved while the fix ran is reported, not overwritten.

WHICH TASKS CAN BE CONTINUED is swarm-api's rule, not this file's: a
`direct-pr` task, or (since #454's CI loop) an `integrate` workflow's
integrator. Nothing here needs to tell them apart, because both pushed
`<prefix><their own task id>` through `publish_branch` below -- the
integrator merges its contributors into that branch and opens the workflow's
one pull request from it -- so the branch derived from the continued id is
the branch that task pushed either way. What IS checked here is the
continuation's OWN strategy: only a `direct-pr` step carries `continues`.

Kept out of lifecycle.py on purpose; lifecycle calls two functions from here.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import WorkerError

#: The shape `swarm_common.models.new_id("task")` mints; the parity test
#: (tests/unit/worker/test_continue_swarm_branch.py) holds the two together.
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{20}$")

#: The one strategy that pushes a branch named for a task and opens its PR.
CONTINUABLE_STRATEGY = "direct-pr"


def continued_task(metadata: Any) -> str | None:
    """The task id whose branch this attempt continues, or None.

    Raises WorkerError for a `continues` key swarm-api could not have written:
    not a task id, or beside a strategy other than `direct-pr`.
    """
    if not isinstance(metadata, dict):
        return None
    block = metadata.get("dispatch")
    if not isinstance(block, dict) or "continues" not in block:
        return None
    value = block.get("continues")
    if not isinstance(value, str) or not TASK_ID_RE.match(value):
        raise WorkerError(
            "metadata.dispatch.continues is not a task id; refusing to derive a "
            "branch from it"
        )
    strategy = block.get("strategy")
    if strategy != CONTINUABLE_STRATEGY:
        raise WorkerError(
            f"metadata.dispatch.continues is set beside strategy {strategy!r}; only "
            f"{CONTINUABLE_STRATEGY!r} continues a branch"
        )
    return value


def clone_ref(metadata: Any, branch_prefix: str) -> str | None:
    """The ref to clone: the continued branch, or None for the caller's own ref."""
    task_id = continued_task(metadata)
    return f"{branch_prefix}{task_id}" if task_id else None


def publish_branch(metadata: Any, branch_prefix: str, own_task_id: str) -> str:
    """The branch to push: the continued one, or `<prefix><this task's id>`."""
    task_id = continued_task(metadata)
    return f"{branch_prefix}{task_id or own_task_id}"
