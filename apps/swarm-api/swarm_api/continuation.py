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
  * The task must have pushed a branch of its own name AND opened a pull
    request from it: a `direct-pr` task, or an `integrate` workflow's
    INTEGRATOR. The integrator publishes exactly as a `direct-pr` task does --
    `<prefix><its own task id>`, through the same `publish_branch` in the
    worker (agent_worker/continuation.py), merging its contributors into that
    branch first -- and the one pull request `integrate` produces is opened
    from it. So continuing it derives the branch the integrator pushed, and
    #454's CI loop can fix an issue run's pull request on that pull request.
    An `integrate` CONTRIBUTOR is refused: its branch is merged by the
    integrator and has no pull request of its own, so a fix pushed there
    reaches nobody.
  * A continuation-scoped account (`member_scope == "continuation"`, the CI
    fixer's service account) reaches the same two: a `direct-pr` task, as
    contract request 30 accepted, and an `integrate` workflow's integrator,
    owner decision 2026-10-06. Every SwarmCloud lane pull request is opened
    by an integrator (the workflow's final fix step), so a fixer confined to
    `direct-pr` tasks fixed none of them: PR #740's first real run was
    refused here. The integrator is told apart by the `role` the API itself
    recorded in the task's dispatch block (`validation.py`, a reserved key no
    caller can write), never by anything the request carries, and only in
    the caller's own tenant (the first rule). A contributor stays refused,
    as for a member. A continuation of a continuation resolves to its root,
    and for this account the root is checked again to be one of the two, so
    no chain reaches a branch without a pull request.
  * The workflow must be `direct-pr` and have ONE step. Two steps pushing to
    one branch race to a non-fast-forward, and the loser's work is lost. A
    `merge` step (contract request 47) pushes nothing and is not counted,
    and a workflow of ONE `merge` step alone merges the continued task's
    pull request at the head that task pushed (a member's only: the issue
    run's merge once CI is green, `issueci`).
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
from .validation import DISPATCH_METADATA_KEY, DispatchOptionError, is_merge_step

#: The shape `swarm_common.models.new_id("task")` mints. Checked here rather
#: than as a schema pattern so a malformed id is refused with the same code and
#: words as a missing one, and so a value read back from a STORED task is held
#: to it too before it becomes the root of a new continuation.
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{20}$")

#: The one strategy whose task pushes `swarm/<task id>` and opens its PR from
#: it, and the one a continuation itself must be.
CONTINUABLE_STRATEGY = "direct-pr"

#: The other task that does: an `integrate` workflow's integrator (#454).
INTEGRATE_STRATEGY = "integrate"
INTEGRATOR_ROLE = "integrator"


def _has_own_pull_request(block: dict[str, Any]) -> bool:
    """Whether a task's dispatch block names one that pushed `<prefix><its id>`
    and opened its pull request from it: `direct-pr`, or the integrator."""
    strategy = block.get("strategy")
    if strategy == CONTINUABLE_STRATEGY:
        return True
    return strategy == INTEGRATE_STRATEGY and block.get("role") == INTEGRATOR_ROLE


@dataclass(frozen=True)
class Continuation:
    #: The task whose branch the fix pushes to: the root of any chain.
    root_task_id: str
    #: The continued task's repository, which the fix step clones.
    repository_url: str
    #: The task `continues_task` named, as checked: what a merge-only
    #: continuation merges (`validation.merge_sources`), at the head IT pushed.
    task_id: str = ""


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
    store: Store, tenant_id: str, spec: WorkflowCreate, *, continuation_scoped: bool = False
) -> Continuation | None:
    """Check `spec.continues_task` and resolve it, or return None when absent.

    `continuation_scoped` is True for a continuation-scoped caller (module
    docstring): it may push to a `direct-pr` task's or an integrator's
    branch, and may not submit a merge-only continuation.

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
    pushing = [s for s in spec.steps if not is_merge_step(s.runner_profile)]
    # A merge-only continuation -- ONE `merge` step and nothing that pushes --
    # merges the continued task's pull request (`validation.merge_sources`).
    merge_only = len(spec.steps) == 1 and not pushing
    if merge_only and continuation_scoped:
        # A merge is a wider power than the push request 30 accepted for the
        # continuation-scoped CI fixer: a member's only.
        raise DispatchOptionError(
            "a continuation-scoped account continues a pull request's branch; it "
            "does not merge one. A merge-only continuation is a tenant member's.",
            detail={"continues_task": requested},
        )
    if len(pushing) != 1 and not merge_only:
        raise DispatchOptionError(
            "continues_task takes a workflow of exactly one step: every step would "
            "push to the same branch, and all but the first would be refused as a "
            "non-fast-forward.",
            detail={"continues_task": requested, "steps": len(pushing)},
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
        # submitted_by=None: UNFILTERED, deliberately. This check is "is this
        # task in the caller's tenant and continuable", not "did the caller
        # submit it" -- a continuation-scoped account's entire purpose is to
        # continue a PULL REQUEST SOMEONE ELSE SUBMITTED (the ci-fix account
        # fixes another member's red CI, #263). Narrowing this read to the
        # caller's own submissions would make the feature continue nothing.
        # `submission_scope` (deps.py) still narrows every READ route that
        # serves data back to a continuation-scoped caller; this call is not
        # one of those routes.
        task = store.get_task(tenant_id, requested, submitted_by=None)
    except NotFound:
        raise not_found from None

    block = _dispatch_block(task.metadata)
    if not _has_own_pull_request(block):
        strategy = block.get("strategy") or "collect"
        what = (
            f"an {INTEGRATE_STRATEGY!r} {block.get('role') or 'contributor'}, whose branch "
            "its integrator merges and which opens no pull request of its own"
            if strategy == INTEGRATE_STRATEGY
            else f"dispatched with strategy {strategy!r}, which pushes no branch of its own"
        )
        raise DispatchOptionError(
            f"task {requested!r} was {what}; only a {CONTINUABLE_STRATEGY!r} task or an "
            f"{INTEGRATE_STRATEGY!r} workflow's {INTEGRATOR_ROLE} has a branch to continue.",
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
    if root != task.id and continuation_scoped:
        try:
            root_block = _dispatch_block(
                store.get_task(tenant_id, root, submitted_by=None).metadata
            )
        except NotFound:
            root_block = {}
        if not _has_own_pull_request(root_block):
            raise DispatchOptionError(
                f"task {requested!r} continues a task that opened no pull request in your "
                f"tenant; a continuation-scoped account continues only a "
                f"{CONTINUABLE_STRATEGY!r} task or an {INTEGRATE_STRATEGY!r} workflow's "
                f"{INTEGRATOR_ROLE}.",
                detail={"continues_task": requested},
            )
    return Continuation(root_task_id=root, repository_url=task.repository_url, task_id=task.id)
