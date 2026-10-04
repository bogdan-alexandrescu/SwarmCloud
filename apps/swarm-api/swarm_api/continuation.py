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
  * An integrator is continuable by a tenant MEMBER only -- the issue run's
    fix round submits as the run's creator -- and NOT by a
    continuation-scoped account (`member_scope == "continuation"`, the CI
    fixer's service account). That account's reach was reviewed and
    accepted by the owner as "any `direct-pr` task"
    (docs/contract-change-requests.md, request 30); letting it reach every
    integrate pull request too would widen what a stolen fixer token can
    push to without that decision. Checked on the ROOT as well, so the
    account cannot reach an integrator through a member's continuation of
    one.
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
    store: Store, tenant_id: str, spec: WorkflowCreate, *, allow_integrator: bool = True
) -> Continuation | None:
    """Check `spec.continues_task` and resolve it, or return None when absent.

    `allow_integrator` is False for a continuation-scoped caller (module
    docstring): only a `direct-pr` root is continuable then.

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
    if not allow_integrator and block.get("strategy") == INTEGRATE_STRATEGY:
        raise _integrator_refused(requested)
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
    if root != task.id and not allow_integrator:
        try:
            root_block = _dispatch_block(
                store.get_task(tenant_id, root, submitted_by=None).metadata
            )
        except NotFound:
            root_block = {}
        if root_block.get("strategy") != CONTINUABLE_STRATEGY:
            raise _integrator_refused(requested)
    return Continuation(root_task_id=root, repository_url=task.repository_url)


def _integrator_refused(requested: str) -> DispatchOptionError:
    return DispatchOptionError(
        f"task {requested!r} continues an {INTEGRATE_STRATEGY!r} workflow's "
        f"{INTEGRATOR_ROLE}, which a continuation-scoped account may not continue: "
        f"its reach is {CONTINUABLE_STRATEGY!r} tasks only. A member of the tenant "
        "can submit this continuation.",
        detail={"continues_task": requested},
    )
