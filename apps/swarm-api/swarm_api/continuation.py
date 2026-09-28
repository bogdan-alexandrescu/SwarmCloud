"""A workflow that pushes to an existing swarm branch instead of its own (#263).

WHY THIS EXISTS. A `direct-pr` step pushes `swarm/<its own task id>` and opens
a pull request from it. That is right for new work and wrong for a FIX: when a
SwarmCloud pull request goes red in CI, the fix has to land on that pull
request's branch, or it arrives as a second pull request stacked on the first
and the red one stays red. Before this, the only way to do that was an
operator pushing from a laptop (#248 and #249, 2026-09-28).

WHAT A CALLER SENDS IS A TASK ID, NEVER A BRANCH. `WorkflowCreate.continues_task`
names the task whose branch went red. The branch is derived from that id by
the worker, with the same prefix it pushed under -- exactly as the integrator
derives its contributors' branches -- so nothing a caller sends can point a
push at an arbitrary ref. Invariant 10 is untouched: no image, command or
backend parameter travels with it, and the fix step names its runner profile
by name like every other step.

THE RULES, and why each is a refusal rather than a quiet adjustment:

  * The task must be the CALLER'S TENANT's. A branch is pushed with the
    tenant's own forge token (invariant 9), so continuing another tenant's task
    would be a cross-tenant write. A task in another tenant is refused in the
    same words as one that does not exist, as `Store.get_task` does, so the
    refusal is not an enumeration oracle.
  * The task must have been `direct-pr`: that is the only strategy whose task
    pushes a branch of its own name. (`integrate` pushes contributors'
    branches, but its pull request is the integrator's, and the integrator's
    branch is a `direct-pr`-shaped one only in name.)
  * The workflow must be `direct-pr` and have ONE step. Two steps pushing to
    one branch race to a non-fast-forward, and the loser's work is lost.
  * No `repository_ref`. The continued branch IS the ref; a second one could
    only disagree with it.
  * The repository is the continued task's. A different one is refused; an
    omitted one is inherited, so a caller does not have to restate it.
  * A continuation of a continuation continues the ORIGINAL branch: the fix's
    own task id names a branch nothing ever pushed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import NotFound
from .schemas import WorkflowCreate
from .store import Store
from .validation import DISPATCH_METADATA_KEY, DispatchOptionError

#: The shape `swarm_common.models.new_id("task")` mints. Checked here rather
#: than as a schema pattern so a malformed id is refused with the same code and
#: words as a missing one, and so a value read back from a STORED task is held
#: to it too before it becomes the root of a new continuation.
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{20}$")

#: The one strategy whose task pushes `swarm/<task id>` and opens its PR from it.
CONTINUABLE_STRATEGY = "direct-pr"


@dataclass(frozen=True)
class Continuation:
    #: The task whose branch the fix pushes to: the root of any chain.
    root_task_id: str
    #: The continued task's repository, which the fix step clones.
    repository_url: str


def _same_repository(a: str, b: str) -> bool:
    """`https://github.com/o/r`, `.../o/r/` and `.../o/r.git` are one repository."""

    def norm(url: str) -> str:
        url = url.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[: -len(".git")]
        return url.lower()

    return norm(a) == norm(b)


def _dispatch_block(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    block = metadata.get(DISPATCH_METADATA_KEY)
    return block if isinstance(block, dict) else {}


def resolve_continuation(
    store: Store, tenant_id: str, spec: WorkflowCreate
) -> Continuation | None:
    """Check `spec.continues_task` and resolve it, or return None when absent.

    Raises `DispatchOptionError` (422 `invalid_dispatch`) for every refusal,
    before anything is written.
    """
    requested = spec.continues_task
    if requested is None:
        return None

    strategy = (spec.strategy or "").strip()
    if strategy != CONTINUABLE_STRATEGY:
        raise DispatchOptionError(
            f"continues_task pushes to an existing pull request's branch, which only "
            f"strategy {CONTINUABLE_STRATEGY!r} does; this workflow asked for "
            f"{strategy!r}.",
            detail={"continues_task": requested, "strategy": strategy},
        )
    if len(spec.steps) != 1:
        raise DispatchOptionError(
            "continues_task takes a workflow of exactly one step: every step would "
            "push to the same branch, and all but the first would be refused as a "
            "non-fast-forward.",
            detail={"continues_task": requested, "steps": len(spec.steps)},
        )
    if spec.repository_ref:
        raise DispatchOptionError(
            "continues_task checks out the continued task's own branch; a "
            "repository_ref beside it could only disagree with it. Send one or the "
            "other.",
            detail={"continues_task": requested, "repository_ref": spec.repository_ref},
        )

    # The same sentence for "missing" and "another tenant's", by construction:
    # `Store.get_task` raises the same NotFound for both, and every refusal
    # below that could only be reached with a task in hand is about the task's
    # own shape, which the caller already owns.
    not_found = DispatchOptionError(
        f"continues_task {requested!r} is not a task in your tenant.",
        detail={"continues_task": requested},
    )
    if not TASK_ID_RE.match(requested):
        raise not_found
    try:
        task = store.get_task(tenant_id, requested)
    except NotFound:
        raise not_found from None

    block = _dispatch_block(task.metadata)
    if block.get("strategy") != CONTINUABLE_STRATEGY:
        raise DispatchOptionError(
            f"task {requested!r} was dispatched with strategy "
            f"{block.get('strategy') or 'collect'!r}, which pushes no branch of its "
            f"own; only a {CONTINUABLE_STRATEGY!r} task has a branch to continue.",
            detail={"continues_task": requested},
        )
    if not task.repository_url:
        raise DispatchOptionError(
            f"task {requested!r} has no repository, so it has no branch to continue.",
            detail={"continues_task": requested},
        )
    if spec.repository_url and not _same_repository(spec.repository_url, task.repository_url):
        raise DispatchOptionError(
            "continues_task pushes to the continued task's repository; this "
            "workflow names a different one. Omit repository_url to inherit it.",
            detail={"continues_task": requested},
        )

    upstream = block.get("continues")
    root = upstream if isinstance(upstream, str) and TASK_ID_RE.match(upstream) else task.id
    return Continuation(root_task_id=root, repository_url=task.repository_url)
