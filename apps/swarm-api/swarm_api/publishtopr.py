"""`publish_to_pr`: a lane that lands on an existing pull request (#2183).

WHY (owner approval, BGD, 2026-10-24). A follow-up lane -- a re-review, a fix
round, the owner's answers applied to a mock-up -- could only open a
REPLACEMENT pull request: `repository_ref` of the old branch gave an empty
diff, and `continues_task` takes a workflow of one `direct-pr` step. On
2026-10-23 ten lanes opened replacements for PRs 2164-2168 and 2170-2174,
which were closed by hand. A workflow spec (or a single task) may now name
the pull request, `publish_to_pr: <N>`, and its lane's commits are pushed
onto that pull request's branch, as a fast-forward, with no pull request
opened.

WHAT A CALLER SENDS IS A NUMBER, NEVER A BRANCH. A positive integer, a pull
request of the spec's OWN repository. `owner/repo#N` or the pull request's
URL are accepted too, and refused unless they name that same repository: a
number in another repository would be pull request N of the wrong one.
Invariant 10 is untouched: no image, command or backend parameter travels
with it.

WHAT IS CHECKED HERE, AT SUBMISSION, before anything is written -- each a
422 `invalid_dispatch` (`DispatchOptionError`):

  * the shape (`check_workflow_shape`, `check_task_shape`): `direct-pr` (one
    step that runs an agent: two would race to the same branch) or
    `integrate` (its integrator is the one step that publishes); a
    repository; no `continues_task` or `merge_pr` (each names another pull
    request), no `repository_ref` (the pull request's head is the ref), no
    `carrier: branches` (it pushes the step's own branch) and no `merge` step
    (it merges the pull request a task OPENED; this lane opens none);
  * the pull request, read ONCE from GitHub with the slot the submitter's
    work runs with (`forge.ForgeTokens`), as `continuation.resolve_merge_pr`
    reads one: it exists, is OPEN and not merged, its base and head are both
    the spec's repository (not a fork), and its head branch is
    `swarm/<task id>` for a task of THE SUBMITTING TENANT -- a branch this
    tenant's lane published. Another tenant's task is refused in the same
    words as one that does not exist, as `Store.get_task` does.

WHAT IS SIGNED. The dispatch block of every step carries
`publish_to_pr: {"number": N, "task_id": "<that task>"}` (`DISPATCH_FIELD`),
inside the block the step-spec signature covers, so the worker reads both
from the verified document and derives the branch from the task id with its
own prefix. The worker reads the pull request AGAIN before the clone and
before the push (`agent_worker.publishpr`): a pull request closed, merged or
moved since this read is refused there, never force-pushed.

NOT A FROZEN-CONTRACT FIELD. `Task` has no field for it; it lives in
`metadata.dispatch`, where `strategy` and `continues` live, and the request
to type it is in docs/contract-change-requests.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import NotFound
from .forgewrite import ForgeWriteNotFound, GitHubWriter
from .store import Store
from .validation import DispatchOptionError, IssueRef, is_merge_step, merge_repository

#: The dispatch block's key; the worker spells it `agent_worker.publishpr.FIELD`.
DISPATCH_FIELD = "publish_to_pr"

#: The worker's branch prefix (`GIT_BRANCH_PREFIX`, default `swarm/`), as
#: `cifix.BRANCH_PREFIX` restates it.
BRANCH_PREFIX = "swarm/"

#: A task id as `swarm_common.models.new_id("task")` mints one.
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{20}$")

#: The largest pull request number accepted: GitHub's are far below it, and
#: a bound keeps a caller's value out of every arithmetic surprise.
MAX_NUMBER = 10**9

#: The strategies a `publish_to_pr` lane may have, in the order refusals name them.
STRATEGIES = ("direct-pr", "integrate")

#: `owner/repo#N`, or the pull request's GitHub URL.
_REFERENCE_RE = re.compile(
    r"^(?:https://github\.com/)?(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)"
    r"(?:#|/pull/)(?P<number>[0-9]{1,10})/?$"
)


@dataclass(frozen=True)
class Target:
    """A `publish_to_pr` pull request, as `resolve` checked it."""

    number: int
    #: The task whose `swarm/<task_id>` branch is the pull request's head.
    task_id: str

    def to_metadata(self) -> dict[str, Any]:
        return {"number": self.number, "task_id": self.task_id}


def _refuse(message: str, **detail: Any) -> DispatchOptionError:
    return DispatchOptionError(message, detail={"publish_to_pr": detail.pop("value", None),
                                                **detail})


def parse_number(value: Any, repository_url: str | None) -> int | None:
    """The pull request number `value` names in the spec's repository, or None
    when it names none. Refuses a value that is not a positive integer, and a
    reference to a pull request of another repository."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _refuse("publish_to_pr is a pull request number: a positive integer.",
                      value=str(value)[:80])
    if isinstance(value, int):
        number = value
    else:
        match = _REFERENCE_RE.match(value.strip())
        if match is None:
            raise _refuse(
                "publish_to_pr is a pull request number: a positive integer (or "
                "owner/repo#N naming the spec's own repository).",
                value=value[:200],
            )
        ours = merge_repository(repository_url)
        named = (match["owner"], match["repo"].removesuffix(".git"))
        if ours is None or (named[0].lower(), named[1].lower()) != (
                ours[0].lower(), ours[1].lower()):
            raise _refuse(
                f"publish_to_pr names a pull request of {named[0]}/{named[1]}, which is "
                f"not this spec's repository ({repository_url or 'none named'}); a lane "
                "publishes only onto a pull request of its own repository.",
                value=value[:200], repository_url=repository_url,
            )
        number = int(match["number"])
    if number < 1 or number > MAX_NUMBER:
        raise _refuse("publish_to_pr is a pull request number: a positive integer.",
                      value=number)
    return number


def _common_shape(*, number: int, strategy: str, carrier: str, repository_url: str | None,
                  repository_ref: str | None) -> None:
    if strategy not in STRATEGIES:
        raise _refuse(
            f"publish_to_pr pushes onto an existing pull request's branch, which a "
            f"{STRATEGIES[0]!r} or {STRATEGIES[1]!r} lane does; this one asked for "
            f"{strategy!r}.",
            value=number, strategy=strategy,
        )
    if carrier == "branches":
        raise _refuse(
            "publish_to_pr pushes the lane's work onto the pull request's branch; "
            "carrier 'branches' pushes each step's own branch instead. Remove carrier.",
            value=number, carrier=carrier,
        )
    if not merge_repository(repository_url):
        raise _refuse(
            "publish_to_pr names a pull request of the spec's own repository, and this "
            "spec names no GitHub repository_url.",
            value=number, repository_url=repository_url,
        )
    if repository_ref:
        raise _refuse(
            "publish_to_pr starts the lane from the pull request's head; a "
            "repository_ref beside it could only disagree with it. Remove repository_ref.",
            value=number, repository_ref=repository_ref,
        )


def check_task_shape(spec: Any, number: int) -> None:
    """Every refusal of a single task's `publish_to_pr` that needs no read."""
    strategy = (spec.strategy or "").strip()
    if strategy != STRATEGIES[0]:
        raise _refuse(
            f"a single task publishes onto a pull request under {STRATEGIES[0]!r}; this "
            f"one asked for {strategy!r}.",
            value=number, strategy=strategy,
        )
    _common_shape(number=number, strategy=strategy, carrier=(spec.carrier or "").strip(),
                  repository_url=spec.repository_url, repository_ref=spec.repository_ref)


def refuse_another_pull_request(spec: Any) -> None:
    """Refuse `publish_to_pr` beside `continues_task` or `merge_pr`, each of
    which names another pull request. Called before either is resolved, so
    the refusal names the conflict rather than whatever resolving the other
    would have refused first."""
    if spec.publish_to_pr is None:
        return
    number = spec.publish_to_pr
    if spec.continues_task is not None:
        raise _refuse(
            "publish_to_pr names a pull request and continues_task names the task whose "
            "pull request to continue: two pull requests. Send one or the other.",
            value=number, continues_task=spec.continues_task,
        )
    if spec.merge_pr is not None:
        raise _refuse(
            "publish_to_pr names the pull request to push onto and merge_pr one to "
            "merge: two pull requests. Send one or the other.",
            value=number,
        )


def check_workflow_shape(spec: Any, number: int, *, repository_url: str | None,
                         agent_steps: Sequence[str]) -> None:
    """Every refusal of a workflow's `publish_to_pr` that needs no read.

    `agent_steps` are the step ids that run an agent, the steps that push.
    """
    strategy = (spec.strategy or "").strip()
    refuse_another_pull_request(spec)
    _common_shape(number=number, strategy=strategy, carrier=(spec.carrier or "").strip(),
                  repository_url=repository_url, repository_ref=spec.repository_ref)
    merges = [s.step_id for s in spec.steps if is_merge_step(s.runner_profile)]
    if merges:
        raise _refuse(
            "publish_to_pr opens no pull request, and a merge step merges the pull "
            "request a step of its workflow opened. Remove the merge step (and "
            "metadata.merge).",
            value=number, steps=merges,
        )
    if strategy == STRATEGIES[0] and len(agent_steps) != 1:
        raise _refuse(
            f"under {STRATEGIES[0]!r} every step publishes, and onto one branch all but "
            "the first would be refused as a moved head. Send one step, or use "
            f"{STRATEGIES[1]!r}, whose integrator is the one step that publishes.",
            value=number, steps=list(agent_steps),
        )


def resolve(
    number: int,
    *,
    store: Store,
    tenant: Any,
    repository_url: str,
    tokens: Any,
    writer: GitHubWriter,
    person: str | None = None,
) -> Target:
    """Read pull request `number` of `repository_url` and check it (module docstring)."""
    pair = merge_repository(repository_url)
    if pair is None:  # pragma: no cover - the shape checks refused it first
        raise _refuse("publish_to_pr needs a GitHub repository.", value=number)
    owner, repo = pair
    full_name = f"{owner}/{repo}"
    detail = {"value": number, "repository": full_name}
    token = tokens.token_for(tenant, person=person)
    try:
        pull = writer.read_pull(IssueRef(owner=owner, repo=repo, number=number), number, token)
    except ForgeWriteNotFound:
        raise _refuse(
            f"#{number} is not a pull request in {full_name}, or your tenant's forge "
            "credential cannot see it.",
            **detail,
        ) from None
    finally:
        del token
    if pull.merged:
        raise _refuse(f"pull request {full_name}#{number} is already merged.", **detail)
    if pull.state != "open":
        raise _refuse(
            f"pull request {full_name}#{number} is closed; a lane publishes only onto an "
            "open pull request. Reopen it first.",
            **detail,
        )
    if pull.base_repo and pull.base_repo.lower() != full_name.lower():
        raise _refuse(f"pull request #{number} is in {pull.base_repo}, not {full_name}.",
                      **detail, base_repository=pull.base_repo)
    if not pull.head_repo or pull.head_repo.lower() != full_name.lower():
        raise _refuse(
            f"pull request {full_name}#{number} comes from a fork "
            f"({pull.head_repo or 'an unknown repository'}); a lane pushes only to a "
            f"branch of {full_name} itself.",
            **detail, head_repository=pull.head_repo,
        )
    not_ours = _refuse(
        f"pull request {full_name}#{number} is on {pull.head_ref[:120]!r}, which is not a "
        f"{BRANCH_PREFIX}task_... branch a lane of your tenant published.",
        **detail,
    )
    task_id = pull.head_ref[len(BRANCH_PREFIX):] if pull.head_ref.startswith(BRANCH_PREFIX) else ""
    if not TASK_ID_RE.match(task_id):
        raise not_ours
    try:
        # Unfiltered by submitter, as `resolve_continuation` reads a task: a
        # follow-up lane lands on a pull request another member's lane opened.
        store.get_task(tenant.tenant_id, task_id, submitted_by=None)
    except NotFound:
        raise not_ours from None
    return Target(number=number, task_id=task_id)
