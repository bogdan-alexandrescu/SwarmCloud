"""The `merge` worker action: squash-merge the pull request a workflow opened (#295).

A step that runs no agent (`WorkerAction.MERGE`). Owner decisions of
2026-10-04 (recorded on #295; contract request 47) replaced the design's
GitHub-App merge with this one:

  * it uses the tenant's EXISTING `-git` token, read at merge time only;
  * it works in ANY repository a workflow runs on -- the workflow's own
    `repository_url`, which the signed spec covers; owner and repo are parsed
    from it, and nothing here names SwarmCloud's repository;
  * after the merge it closes every issue the pull request closes that is
    still open, because the App path did not (#569);
  * it is an opt-in final step, appended by swarm-api when the platform
    default or the job says so (`swarm_api.validation.plan_merge`).

WHICH PULL REQUEST. The signed `dispatch.merge_target` block names the TASK
that opened it (the integrator, or the one `direct-pr` step) and, when the
workflow has one, the review whose verdict file this step stages. The pull
request's number and pushed head are that task's recorded result: claims,
written under the tenant identity, so the live pull request is then checked
to be that task's own branch, from no fork, at exactly that head. Nothing is
read from a pointer the signed spec does not name.

A PULL REQUEST NO WORKFLOW OPENED (#352, owner decision 2026-10-07): a
`merge_pr` workflow's signed `merge_target` names no task but the pull
request's `number` and the `head_sha` the caller named, which swarm-api
checked was its head at submission (`continuation.resolve_merge_pr`). The
sha is pinned exactly as a pushed head is, and every check after it is the
same gate -- open, no fork, the base, the head, every required check green
there -- except the branch name, which no SwarmCloud task chose.

THE ORDER IS #219's (docs/merge-step.md §2.2): the reap and every check that
needs no credential first; then the token; then the forge's facts; then the
merge, pinned to the head; then the record and the issues. A refusal ends the
step FAILED with MERGE_REFUSED and its code in `result_summary.merge.refusal`.
Three facts are waits rather than verdicts -- a required check still pending,
no check reported at all on a branch that requires none, and GitHub not having
computed mergeability -- and PARK the step on CI_PENDING (lane MS2,
docs/merge-step.md "Revised 2026-10-06" §1): no lease, no pool count, the
attempt refunded up to MERGE_CI_MAX_WAKES (invariants 1, 3 and 4). swarm-api's
wake tick marks it once the checks settle, the scheduler promotes it on that
mark or at the fallback instant, and the next attempt reads every fact again.
The merge call itself is never resent: a merge whose answer was lost ends
MERGE_FAILED, never retried blindly.

A BRANCH THAT IS BEHIND a base requiring an up-to-date branch is updated by
GitHub, not merged (lane MS3, the owner's 2026-10-06 decision): behind the
same fencing recheck as the merge, `update-branch` with `expected_head_sha`,
then a CI_PENDING park at the new head, at most MERGE_MAX_BRANCH_UPDATES
times. The head is never read from the task document: every attempt walks
first parents from GitHub's live head back to the head the workflow pushed,
and accepts only GitHub's own merges of commits already on the base. MS3 also
splits `from_fork`, `base_not_default`, `protection_refused`,
`merge_conflict`, `checks_timeout` and `behind_too_often` out of the codes
they were folded into, and gives `_close_issues` the close script's page rule.

A BASE THAT MERGES ONLY THROUGH A MERGE QUEUE is enqueued, not merged (lane
C3H, the owner's 2026-10-06 decision for SwarmCloud's own main). GitHub
answers the REST merge 405 "Changes must be made through the merge queue";
then, behind the same fencing recheck, the step enqueues through GraphQL
`enqueuePullRequest` with the same token, pinned to the head the checks are
green at, records the entry in `merge_queued`, and parks CI_PENDING with the
code MERGE_QUEUED, as every MS2 wait does. swarm-api's wake tick reads the
queue for that park; the next attempt finds the pull request merged
(success, with the record and the issues), still queued (parked again), or
removed (`merge_dequeued`, with GitHub's reason). With no queue rule nothing
here runs: the 405 is the only way in.

THE GITHUB SPECIFICS ARE BEHIND `ForgeMerger`, with `GitHubMerger` its only
implementation. swarm-api refuses a repository whose host has no merger at
SUBMISSION (`validation.MERGE_FORGE_HOSTS`, held equal to
`MERGEABLE_HOSTS` by tests/unit/worker/test_merge_action.py), so the
`forge_unsupported` refusal below is a second line, not the first.

THE RULES `auto-merge.yml` ALSO STATES are the module-level functions
`title_is_placeholder`, `other_check_blocks` and `required_check_state`, so
tests/unit/scripts/test_auto_merge_workflow.py holds them to the gate's shell
from one table of cases (§8) until that workflow is retired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from swarm_common.models import EndCause, utcnow
from swarm_common.states import TERMINAL_STATES, TaskState

from . import forge as forge_mod
from . import verdict as verdict_mod
from .errors import ExitCode, InputUnavailable
from .post_verdict import (
    ActionContext,
    ActionOutcome,
    _outcome,
    _task_id,
    cancelled,
    forge_retry,
    pull_request_belongs,
    pull_request_number,
    refusal,
    unavailable,
)
from .control import MERGE_WAIT_METADATA_KEY
from .specverify import UpstreamSpecUnverified

#: The signed dispatch block naming what this step merges. swarm-api's
#: `validation.MERGE_TARGET_FIELD`, restated because the worker image carries no
#: control plane; tests/unit/worker/test_merge_action.py holds the two equal.
MERGE_TARGET_FIELD = "merge_target"

#: The worker's own placeholder title, compared case- and leading-whitespace-
#: insensitively: auto-merge.yml gate 1's rule.
PLACEHOLDER_TITLE_PREFIX = "[swarm] task_"
#: The worker's `pr-title.txt` rule (#214): the retired `[swarm] task_...`
#: shape ANYWHERE in the title, any spacing, any case. `lifecycle.
#: _RETIRED_TITLE_RE`, restated because lifecycle imports this module;
#: tests/unit/worker/test_merge_action.py holds the two equal.
RETIRED_TITLE_RE = re.compile(r"\[swarm\]\s*task_", re.IGNORECASE)

#: §5.2 3: what counts as green for a REQUIRED check. The owner's rule:
#: success or skipped, nothing else -- `neutral` included.
REQUIRED_GREEN = frozenset({"success", "skipped"})
#: What any OTHER check may conclude without holding the merge, and what
#: every check must conclude on a base branch that requires none:
#: auto-merge.yml gate 5's own tolerance plus nothing.
OTHER_TOLERATED = frozenset({"success", "skipped", "neutral"})

#: `mergeable: null` is read again at most twice, 2 s apart (§5.1): a bounded
#: pause in seconds, not a provider wait (invariant 4).
MERGEABLE_REREADS = 2
MERGEABLE_REREAD_SECONDS = 2.0

#: How many CI_PENDING parks give their attempt back (the await park's rule,
#: `control.ControlPlane.park_ci_pending`): waiting is not failing, so a slow
#: CI does not use up the step's attempts. Past the bound a wake counts like
#: any attempt, so a pull request whose CI never settles still ends at
#: `max_attempts`. 60, the design's figure: with swarm-api's wake tick marking
#: a park as soon as its checks settle, a park lasts about one CI run, and the
#: fallback below alone gives 60 x 15 min = 15 h of waiting, past the design's
#: 6 h `MERGE_CI_MAX_SECONDS`.
MERGE_CI_MAX_WAKES = 60

#: When the scheduler wakes a CI_PENDING park nobody marked: the park instant
#: plus this, as `next_eligible_at`. A dead wake tick, or a token swarm-api
#: cannot use, then costs a wake every 15 minutes -- one lease of a few
#: seconds -- instead of stranding the merge. Not shorter, the design's 900:
#: each fallback wake is a Job execution and a cold start, which the tick
#: exists to avoid.
MERGE_CI_FALLBACK_SECONDS = 900

#: How many times the step updates a branch that is behind before it refuses
#: `behind_too_often`. 3, the design's figure: each update costs a full CI run
#: (10-20 min here), and a base that moves faster than CI three times running
#: is a question for a person, not a loop. The first-parent walk accepts at
#: most this many of GitHub's base merges on top of the pushed head.
MERGE_MAX_BRANCH_UPDATES = 3

#: How long, from `merge_wait.first_parked_at`, the step waits for CI before
#: it refuses `checks_timeout`, naming what is still pending. 6 h, the
#: design's figure: about twenty of this repository's CI runs, so only a check
#: that never reports -- a required check no workflow produces, a stuck
#: runner -- reaches it.
MERGE_CI_MAX_SECONDS = 6 * 3600

#: The merge task's record of its CI-fix rounds, written only by swarm-api's
#: wake tick (`swarm_api.mergewake.MERGE_FIX_METADATA_KEY`, lane MS7):
#: `rounds`, oldest first, each `{round, head, claimed_at}` plus the round's
#: `workflow_id`/`task_id` once submitted, or an `error` when it was not.
#: Restated because the worker image carries no control plane;
#: tests/unit/worker/test_merge_fix_round.py holds the two equal. The
#: document is tenant-writable: a round's head is accepted only from its
#: task's SIGNED spec (`_fix_round_heads`), and a forged entry can at most
#: make the step wait, which `MERGE_CI_MAX_SECONDS` bounds.
MERGE_FIX_METADATA_KEY = "merge_fix"
#: The workflow's `metadata.merge_fix_rounds`, copied onto the merge task
#: (`swarm_api.validation.MERGE_FIX_ROUNDS_KEY`), and its ceiling
#: (`MERGE_FIX_ROUNDS_MAX`, the API's): a forged count is read under it.
MERGE_FIX_ROUNDS_KEY = "merge_fix_rounds"
MERGE_FIX_ROUNDS_MAX = 5

#: The park codes of a red reading a CI-fix round may still fix: rounds are
#: left and the tick has not claimed the next one yet, or one is running at
#: this head. Never `checks_failed` while either holds.
CI_FIX_PENDING = "ci_fix_pending"
CI_FIX_RUNNING = "ci_fix_running"

#: The park code after an update-branch whose new head GitHub had not made
#: within the bounded re-read: the call is asynchronous. swarm-api's tick
#: (`swarm_api.mergewake.BRANCH_UPDATE_PENDING`) then waits for the head to
#: move rather than reading the old head's checks, which were green.
BRANCH_UPDATE_PENDING = "branch_update_pending"

#: The park code of a pull request this step put in its base's merge queue:
#: swarm-api's wake tick reads the queue for it, not the checks
#: (`swarm_api.mergewake.MERGE_QUEUED`, held equal by
#: tests/unit/control_plane/test_merge_wake.py).
MERGE_QUEUED = "merge_queued"

#: Who commits GitHub's own merge commits -- `update-branch`, the web UI --
#: and signs them, so `verification.verified` is true. A two-parent commit
#: anyone else made can carry any tree, so the walk accepts only these.
GITHUB_COMMITTER_EMAIL = "noreply@github.com"

#: A 405/422 from the merge call that means the branch is behind: the
#: update row, not a refusal.
_OUT_OF_DATE = re.compile(r"out of date|not up to date|is behind", re.IGNORECASE)
#: A 405/422 (merge) or 422 (update-branch) that names a merge conflict.
_CONFLICT = re.compile(r"conflict", re.IGNORECASE)
#: A 405/422 from the merge call that means the base merges only through a
#: merge queue: GitHub's "Changes must be made through the merge queue",
#: alone (405) or under "Repository rule violations found" (422).
_MERGE_QUEUE = re.compile(r"merge queue", re.IGNORECASE)
#: A 403 that is GitHub's plan answer, not a missing right: a private
#: repository on a plan without rulesets (or branch protection) is answered
#: "Upgrade to GitHub Pro or make this repository public to enable this
#: feature." (measured on sagaxyz/ai-studio, 2026-10-10). Read as "the plan
#: has none", exactly as a 404 is; any other 403 still refuses.
_PLAN_LACKS_FEATURE = re.compile(
    r"upgrade to github pro|make this repository public to enable this feature",
    re.IGNORECASE,
)
#: Where GitHub's own error bodies point. A `documentation_url` elsewhere is
#: not GitHub's plan answer, whatever its message says.
_GITHUB_DOCS = re.compile(r"^https://docs\.github\.com/", re.IGNORECASE)

#: `RequiredChecks.source`: where the required checks came from, recorded as
#: the step's `required_checks_source`. The two `none_*` sources read nothing
#: required, so every check at the head must be green and one must exist.
CHECKS_FROM_RULESETS = "rulesets"
CHECKS_FROM_CLASSIC = "classic"
CHECKS_NONE_ALL_CHECKS = "none_all_checks"
CHECKS_NONE_PLAN_LIMITED = "none_plan_limited_all_checks"

#: The hosts `GitHubMerger` can merge on: github.com, where the tenant's
#: token may be sent at all (`forge.may_receive_forge_token`, #307).
MERGEABLE_HOSTS = forge_mod.GITHUB_HOSTS

#: The closing references one merge reads: one page, the close script's own
#: (`scripts/close-merged-issues.sh` `PAGE=100`). GitHub links far fewer; past
#: a page the page is closed and `issues_beyond_page` recorded, never a
#: silent partial close.
CLOSING_PAGE = 100

_SHA = re.compile(r"^[0-9a-f]{40}$")

_CLOSING_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    f" closingIssuesReferences(first: {CLOSING_PAGE}) {{"
    " totalCount nodes { number state repository { nameWithOwner } } } } } }"
)

_QUEUE_ENTRY = "mergeQueueEntry { id position state enqueuedAt }"

_ENQUEUE_MUTATION = (
    "mutation($pullRequestId: ID!, $expectedHeadOid: GitObjectID!) {"
    " enqueuePullRequest(input: {pullRequestId: $pullRequestId,"
    " expectedHeadOid: $expectedHeadOid}) {"
    f" {_QUEUE_ENTRY} }} }}"
)

#: Whether the pull request is in its base's merge queue, and the last reason
#: GitHub gave for taking it out.
_QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    f" isInMergeQueue {_QUEUE_ENTRY}"
    " timelineItems(last: 1, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {"
    " nodes { ... on RemovedFromMergeQueueEvent { reason createdAt } } } } } }"
)

# ---------------------------------------------------------------------------
# The rules auto-merge.yml also states (§8)
# ---------------------------------------------------------------------------


def title_is_placeholder(title: str) -> bool:
    """Gate 1's prefix, OR the worker's own `pr-title.txt` rule.

    Gate 1 refuses `[swarm] task_` after leading whitespace, in any case; the
    author's worker refuses a `pr-title.txt` carrying the retired shape
    anywhere, with any spacing (#214). The union is the worker's rule: STRICTER
    than gate 1 (`Fix [swarm] task_ handling` passes the gate and is refused
    here) -- §8 allows the merge to be the stricter, never the looser.
    """
    return (
        title.lstrip().lower().startswith(PLACEHOLDER_TITLE_PREFIX)
        or RETIRED_TITLE_RE.search(title) is not None
    )


def other_check_blocks(run: Mapping[str, Any]) -> bool:
    """A check at the head that holds the merge: not completed, or completed
    with a conclusion other than success, skipped or neutral. STRICTER than
    gate 5, which holds only on four conclusions (§8)."""
    if run.get("status") != "completed":
        return True
    return (run.get("conclusion") or "") not in OTHER_TOLERATED


def required_check_state(runs: list[Mapping[str, Any]]) -> str:
    """The runs of ONE required check: "green", "pending" (none, or not
    completed) or "failed"."""
    if not runs:
        return "pending"
    if any(run.get("status") != "completed" for run in runs):
        return "pending"
    if all((run.get("conclusion") or "") in REQUIRED_GREEN for run in runs):
        return "green"
    return "failed"


# ---------------------------------------------------------------------------
# The forge, behind one small interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequestFacts:
    number: int
    state: str
    merged: bool
    draft: bool
    title: str
    head_sha: str | None
    head_ref: str | None
    head_repo: str | None
    base_ref: str | None
    base_repo: str | None
    #: True, False, or None while GitHub has not computed it.
    mergeable: bool | None
    merge_commit_sha: str | None = None
    #: GitHub's `mergeable_state`: `behind` when the base requires an
    #: up-to-date branch and this one is not; `dirty` on a conflict.
    mergeable_state: str | None = None
    #: GraphQL's id for the pull request, which `enqueuePullRequest` takes.
    node_id: str | None = None


@dataclass(frozen=True)
class CheckFacts:
    """One check run, or one commit status read as a check run."""

    name: str
    status: str
    conclusion: str | None
    app_id: int | None = None

    def as_run(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "conclusion": self.conclusion}


@dataclass(frozen=True)
class MergeAnswer:
    merged: bool
    status: int
    sha: str | None = None
    message: str = ""


@dataclass(frozen=True)
class ClosingIssue:
    number: int
    #: GitHub's GraphQL issue state: OPEN or CLOSED.
    state: str
    #: `owner/name` of the repository the issue is in.
    repository: str


@dataclass(frozen=True)
class ClosingReferences:
    """One page of a pull request's closing references, and how many it has."""

    issues: tuple[ClosingIssue, ...] = ()
    total: int = 0


@dataclass(frozen=True)
class CommitFacts:
    sha: str
    parents: tuple[str, ...]
    committer_email: str | None
    verified: bool

    @property
    def by_github(self) -> bool:
        """Committed and signed by GitHub itself, as `update-branch` commits are."""
        return self.verified and (self.committer_email or "").lower() == GITHUB_COMMITTER_EMAIL


@dataclass(frozen=True)
class UpdateAnswer:
    status: int
    message: str = ""


@dataclass(frozen=True)
class RequiredChecks:
    """What the base branch requires. `checks` empty means it requires none."""

    checks: tuple[forge_mod.RequiredCheck, ...] = ()
    protected: bool = False
    #: Which rule the checks were read by: one of the `CHECKS_*` sources.
    source: str = CHECKS_NONE_ALL_CHECKS


@dataclass(frozen=True)
class QueueFacts:
    """Where a pull request stands in its base's merge queue."""

    queued: bool
    entry: str | None = None
    position: int | None = None
    state: str | None = None
    enqueued_at: str | None = None
    #: The reason GitHub gave the last time it removed the pull request.
    removed_reason: str | None = None

    def record(self, head: str) -> dict[str, Any]:
        return {"entry": self.entry, "position": self.position, "state": self.state,
                "enqueued_at": self.enqueued_at, "head": head}


@dataclass(frozen=True)
class EnqueueAnswer:
    enqueued: bool
    status: int
    entry: QueueFacts | None = None
    message: str = ""


class ForgeMerger(Protocol):
    """What the merge step asks of a forge. One implementation: `GitHubMerger`."""

    full_name: str

    def can_push(self) -> bool: ...

    def default_branch(self) -> str | None: ...

    def pull_request(self, number: int) -> PullRequestFacts: ...

    def required_checks(self, branch: str) -> RequiredChecks: ...

    def checks_at(self, sha: str) -> list[CheckFacts]: ...

    def commit(self, sha: str) -> CommitFacts: ...

    def on_base(self, base: str, sha: str) -> bool: ...

    def update_branch(self, number: int, *, expected_head_sha: str) -> UpdateAnswer: ...

    def merge(self, number: int, *, sha: str, title: str, message: str) -> MergeAnswer: ...

    def enqueue(self, number: int, *, node_id: str, expected_head_sha: str) -> EnqueueAnswer: ...

    def queue_state(self, number: int) -> QueueFacts: ...

    def comment(self, number: int, body: str) -> bool: ...

    def closing_issues(self, number: int) -> ClosingReferences: ...

    def close_issue(self, number: int, *, comment: str) -> bool: ...


class UnsupportedForge(ValueError):
    """The repository is on a host no `ForgeMerger` serves."""


def plan_lacks_feature(exc: BaseException) -> bool:
    """A 403 that is GitHub saying the repository's plan has no such feature
    (`_PLAN_LACKS_FEATURE`), with GitHub's documentation link if it gave one.
    Any other 403, and every other status, is not."""
    if getattr(exc, "status", None) != 403:
        return False
    if not _PLAN_LACKS_FEATURE.search(str(getattr(exc, "message", "") or "")):
        return False
    url = str(getattr(exc, "documentation_url", "") or "")
    return not url or _GITHUB_DOCS.search(url) is not None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


#: A commit status's `state`, as a check run's (status, conclusion).
_STATUS_AS_RUN = {
    "success": ("completed", "success"),
    "pending": ("in_progress", None),
    "failure": ("completed", "failure"),
    "error": ("completed", "failure"),
}


class GitHubMerger:
    """`ForgeMerger` over GitHub's REST and GraphQL APIs on api.github.com.

    Every request goes through `forge.PinnedForgeClient`: one host, the token
    in the Authorization header only, no redirect followed, GETs retried on a
    transient failure and nothing else resent.
    """

    def __init__(self, client: forge_mod.PinnedForgeClient, *, owner: str, repo: str) -> None:
        self._client = client
        self.owner = owner
        self.repo = repo
        self.full_name = f"{owner}/{repo}"
        self._base = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        self._repository: Mapping[str, Any] | None = None

    def __repr__(self) -> str:
        return f"GitHubMerger({self.full_name!r})"

    def _repository_doc(self) -> Mapping[str, Any]:
        if self._repository is None:
            self._repository = _mapping(self._client.get_ok(self._base))
        return self._repository

    def can_push(self) -> bool:
        return _mapping(self._repository_doc().get("permissions")).get("push") is True

    def default_branch(self) -> str | None:
        return _str(self._repository_doc().get("default_branch"))

    def pull_request(self, number: int) -> PullRequestFacts:
        data = _mapping(self._client.get_ok(f"{self._base}/pulls/{number}"))
        head = _mapping(data.get("head"))
        base = _mapping(data.get("base"))
        mergeable = data.get("mergeable")
        return PullRequestFacts(
            number=number,
            state=str(data.get("state") or ""),
            merged=data.get("merged") is True,
            draft=data.get("draft") is True,
            title=data.get("title") if isinstance(data.get("title"), str) else "",
            head_sha=_str(head.get("sha")),
            head_ref=_str(head.get("ref")),
            head_repo=_str(_mapping(head.get("repo")).get("full_name")),
            base_ref=_str(base.get("ref")),
            base_repo=_str(_mapping(base.get("repo")).get("full_name")),
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            merge_commit_sha=_str(data.get("merge_commit_sha")),
            mergeable_state=_str(data.get("mergeable_state")),
            node_id=_str(data.get("node_id")),
        )

    def required_checks(self, branch: str) -> RequiredChecks:
        """Every required check on `branch`: its rulesets' and its classic protection's.

        `rules/branches/{branch}` gives the rulesets' rules; the branch's own
        read gives classic protection, which needs only read access to see
        (`protection.required_status_checks`). Both, because a repository may
        use either, and a required check read from one alone would be missed.

        A 404, or a 403 that is GitHub's plan answer (`plan_lacks_feature`: a
        private repository on a plan without rulesets), reads as "none"; any
        other answer raises. `source` records which rule the step then used.
        """
        plan_limited = False
        try:
            rules = self._client.rules_for_branch(self.owner, self.repo, branch)
        except forge_mod.ForgeAnswered as exc:
            if plan_lacks_feature(exc):
                plan_limited = True
            elif getattr(exc, "status", None) != 404:
                raise
            rules = []
        found = {(c.context, c.app_id): c for c in forge_mod.required_status_checks(rules)}
        from_rulesets = bool(found)
        try:
            branch_doc = _mapping(
                self._client.get_ok(f"{self._base}/branches/{quote(branch, safe='')}")
            )
        except forge_mod.ForgeAnswered as exc:
            if not plan_lacks_feature(exc):
                raise
            plan_limited = True
            branch_doc = {}
        protection = _mapping(_mapping(branch_doc.get("protection")).get("required_status_checks"))
        for check in protection.get("checks") or []:
            check = _mapping(check)
            context = _str(check.get("context"))
            if context is not None:
                key = (context, _int(check.get("app_id")))
                found.setdefault(key, forge_mod.RequiredCheck(context, key[1]))
        for context in protection.get("contexts") or []:
            if isinstance(context, str) and context and not any(
                c == context for c, _ in found
            ):
                found[(context, None)] = forge_mod.RequiredCheck(context, None)
        protected = bool(rules) or branch_doc.get("protected") is True
        if from_rulesets:
            source = CHECKS_FROM_RULESETS
        elif found:
            source = CHECKS_FROM_CLASSIC
        elif plan_limited and not protected:
            source = CHECKS_NONE_PLAN_LIMITED
        else:
            source = CHECKS_NONE_ALL_CHECKS
        return RequiredChecks(checks=tuple(found.values()), protected=protected, source=source)

    def checks_at(self, sha: str) -> list[CheckFacts]:
        """Every check run, and every commit status, reported at `sha`."""
        out: list[CheckFacts] = []
        for run in self._client.paginate(f"{self._base}/commits/{sha}/check-runs",
                                         key="check_runs"):
            run = _mapping(run)
            name = _str(run.get("name"))
            if name is None:
                continue
            out.append(CheckFacts(
                name=name,
                status=str(run.get("status") or ""),
                conclusion=_str(run.get("conclusion")),
                app_id=_int(_mapping(run.get("app")).get("id")),
            ))
        combined = _mapping(self._client.get_ok(f"{self._base}/commits/{sha}/status"))
        for status in combined.get("statuses") or []:
            status = _mapping(status)
            name = _str(status.get("context"))
            if name is None:
                continue
            state, conclusion = _STATUS_AS_RUN.get(
                str(status.get("state") or ""), ("completed", "failure")
            )
            out.append(CheckFacts(name=name, status=state, conclusion=conclusion))
        return out

    def commit(self, sha: str) -> CommitFacts:
        data = _mapping(self._client.get_ok(f"{self._base}/commits/{quote(sha, safe='')}"))
        detail = _mapping(data.get("commit"))
        parents = tuple(
            p for p in (_str(_mapping(parent).get("sha")) for parent in data.get("parents") or [])
            if p is not None
        )
        return CommitFacts(
            sha=_str(data.get("sha")) or sha,
            parents=parents,
            committer_email=_str(_mapping(detail.get("committer")).get("email")),
            verified=_mapping(detail.get("verification")).get("verified") is True,
        )

    def on_base(self, base: str, sha: str) -> bool:
        """Whether `sha` is already on `base`: `compare` says the commit is
        behind the base, or is its tip."""
        data = _mapping(self._client.get_ok(
            f"{self._base}/compare/{quote(base, safe='')}...{quote(sha, safe='')}"
        ))
        return data.get("status") in ("behind", "identical")

    def update_branch(self, number: int, *, expected_head_sha: str) -> UpdateAnswer:
        """GitHub merges the base into the branch, if the head is still `expected_head_sha`."""
        answer = self._client.request(
            "PUT", f"{self._base}/pulls/{number}/update-branch",
            payload={"expected_head_sha": expected_head_sha},
        )
        return UpdateAnswer(status=answer.status, message=forge_mod._message_of(answer.data))

    def merge(self, number: int, *, sha: str, title: str, message: str) -> MergeAnswer:
        answer = self._client.request(
            "PUT", f"{self._base}/pulls/{number}/merge",
            payload={"merge_method": "squash", "sha": sha,
                     "commit_title": f"{title} (#{number})", "commit_message": message},
        )
        data = _mapping(answer.data)
        return MergeAnswer(
            merged=answer.status == 200 and data.get("merged") is True,
            status=answer.status,
            sha=_str(data.get("sha")),
            message=forge_mod._message_of(answer.data),
        )

    def enqueue(self, number: int, *, node_id: str, expected_head_sha: str) -> EnqueueAnswer:
        """Add the pull request to its base's merge queue, if its head is still
        `expected_head_sha`. GraphQL answers a refusal 200 with `errors`."""
        answer = self._client.request(
            "POST", "/graphql",
            payload={"query": _ENQUEUE_MUTATION,
                     "variables": {"pullRequestId": node_id,
                                   "expectedHeadOid": expected_head_sha}},
        )
        data = _mapping(answer.data)
        entry = _mapping(_mapping(_mapping(data.get("data")).get("enqueuePullRequest"))
                         .get("mergeQueueEntry"))
        if answer.status != 200 or data.get("errors") or not entry:
            return EnqueueAnswer(False, answer.status,
                                 message=_graphql_message(answer.data)
                                 or f"GitHub answered {answer.status} and enqueued nothing")
        return EnqueueAnswer(True, answer.status, _queue_entry(entry))

    def queue_state(self, number: int) -> QueueFacts:
        answer = self._client.request(
            "POST", "/graphql",
            payload={"query": _QUEUE_QUERY,
                     "variables": {"owner": self.owner, "name": self.repo, "number": number}},
        )
        data = _mapping(answer.data)
        pull = _mapping(_mapping(_mapping(data.get("data")).get("repository")).get("pullRequest"))
        if answer.status != 200 or data.get("errors") or not isinstance(
            pull.get("isInMergeQueue"), bool
        ):
            raise forge_mod.ForgeAnswered(answer.status, "/graphql",
                                          _graphql_message(answer.data)
                                          or "the merge queue could not be read")
        removals = _mapping(pull.get("timelineItems")).get("nodes") or []
        reason = _str(_mapping(removals[-1]).get("reason")) if removals else None
        if pull["isInMergeQueue"]:
            entry = _queue_entry(_mapping(pull.get("mergeQueueEntry")))
            return QueueFacts(True, entry.entry, entry.position, entry.state,
                              entry.enqueued_at, reason)
        return QueueFacts(False, removed_reason=reason)

    def comment(self, number: int, body: str) -> bool:
        answer = self._client.request(
            "POST", f"{self._base}/issues/{number}/comments", payload={"body": body}
        )
        return answer.status == 201

    def closing_issues(self, number: int) -> ClosingReferences:
        answer = self._client.request(
            "POST", "/graphql",
            payload={"query": _CLOSING_QUERY,
                     "variables": {"owner": self.owner, "name": self.repo, "number": number}},
        )
        data = _mapping(answer.data)
        if answer.status != 200 or data.get("errors"):
            raise forge_mod.ForgeAnswered(answer.status, "/graphql",
                                          forge_mod._message_of(answer.data)
                                          or "the closing references could not be read")
        pull = _mapping(_mapping(_mapping(data.get("data")).get("repository")).get("pullRequest"))
        references = _mapping(pull.get("closingIssuesReferences"))
        nodes = references.get("nodes") or []
        out: list[ClosingIssue] = []
        for node in nodes:
            node = _mapping(node)
            issue = _int(node.get("number"))
            repository = _str(_mapping(node.get("repository")).get("nameWithOwner"))
            if issue is None or issue <= 0 or repository is None:
                continue
            out.append(ClosingIssue(issue, str(node.get("state") or ""), repository))
        total = _int(references.get("totalCount"))
        return ClosingReferences(tuple(out), max(total or 0, len(nodes)))

    def close_issue(self, number: int, *, comment: str) -> bool:
        commented = self.comment(number, comment)
        answer = self._client.request(
            "PATCH", f"{self._base}/issues/{number}",
            payload={"state": "closed", "state_reason": "completed"},
        )
        return commented and answer.status == 200


def _queue_entry(entry: Mapping[str, Any]) -> QueueFacts:
    return QueueFacts(True, _str(entry.get("id")), _int(entry.get("position")),
                      _str(entry.get("state")), _str(entry.get("enqueuedAt")))


def _graphql_message(data: Any) -> str:
    """GraphQL's first error message, or REST's `message`, at most 300 characters."""
    errors = data.get("errors") if isinstance(data, Mapping) else None
    if isinstance(errors, list):
        for error in errors:
            message = _str(_mapping(error).get("message"))
            if message:
                return message[:300]
    return forge_mod._message_of(data)


def merger_for(
    repository_url: str | None,
    *,
    token: str,
    transport: forge_mod.Transport | None = None,
    retry: forge_mod.RetryPolicy | None = None,
) -> ForgeMerger:
    """The `ForgeMerger` for the workflow's repository, or `UnsupportedForge`."""
    ref = forge_mod.parse_repo(repository_url or "")
    if ref is None or ref.host not in MERGEABLE_HOSTS:
        raise UnsupportedForge(
            "the merge step merges on github.com only, and this workflow's repository "
            "is not there"
        )
    client = forge_mod.PinnedForgeClient(token=token, transport=transport, retry=retry)
    return GitHubMerger(client, owner=ref.owner, repo=ref.name)


def mergeable_repository(repository_url: str | None) -> str | None:
    """`owner/repo` when the merge step can act on this repository, else None."""
    ref = forge_mod.parse_repo(repository_url or "")
    if ref is None or ref.host not in MERGEABLE_HOSTS:
        return None
    return ref.full_name


# ---------------------------------------------------------------------------
# What the signed spec names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeTarget:
    #: The task that opened the pull request; None for a named one.
    pull_request: str | None
    #: The review whose verdict file this step staged, when there is one.
    review: str | None = None
    verdict_file: str | None = None
    #: The default branch the tenant registered the repository with, written
    #: by swarm-api at submission (lane MS1); None when it registered none.
    #: The `base_not_default` refusal reads it first, GitHub's own second.
    base: str | None = None
    #: A `merge_pr` pull request (#352): its number and the head to pin,
    #: both set, and `pull_request` None.
    number: int | None = None
    head_sha: str | None = None
    #: A merge-only continuation's (#900): the workflow the continued task
    #: `pull_request` belongs to, which swarm-api read from that task's record
    #: at submission and signed here. The opener's spec is verified against
    #: THIS workflow, not the merge's own; None for a merge inside the
    #: workflow that opened the pull request.
    pull_request_workflow: str | None = None


class TargetInvalid(ValueError):
    pass


def parse_merge_target(dispatch: Mapping[str, Any]) -> MergeTarget:
    raw = dispatch.get(MERGE_TARGET_FIELD)
    if not isinstance(raw, Mapping):
        raise TargetInvalid(
            f"this step's signed dispatch block has no {MERGE_TARGET_FIELD} block naming the "
            "task that opened the pull request"
        )
    unknown = sorted(
        str(k) for k in raw
        if k not in ("pull_request", "review", "verdict_file", "base", "number", "head_sha",
                     "pull_request_workflow")
    )
    if unknown:
        raise TargetInvalid(f"{MERGE_TARGET_FIELD} names {', '.join(unknown)}")
    base = raw.get("base")
    if "base" in raw and (not isinstance(base, str) or not base):
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.base is not a branch name")
    if "number" in raw or "head_sha" in raw:
        return _named_target(raw, base)
    pull_request = _task_id(raw.get("pull_request"))
    if pull_request is None:
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.pull_request is not a task id")
    review = raw.get("review")
    verdict_file = raw.get("verdict_file")
    if "pull_request_workflow" in raw:
        # A merge-only continuation's target (#900): the continued task and
        # its workflow, and no review -- `validation.merge_sources` gives a
        # continuation none. A workflow id has a task id's shape.
        workflow = _task_id(raw.get("pull_request_workflow"))
        if workflow is None:
            raise TargetInvalid(f"{MERGE_TARGET_FIELD}.pull_request_workflow is not a workflow id")
        if review is not None or verdict_file is not None:
            raise TargetInvalid(
                f"{MERGE_TARGET_FIELD} names another workflow's pull request and also a review"
            )
        return MergeTarget(pull_request, base=base, pull_request_workflow=workflow)
    if review is None and verdict_file is None:
        return MergeTarget(pull_request, base=base)
    if _task_id(review) is None or not isinstance(verdict_file, str) or not verdict_file:
        raise TargetInvalid(
            f"{MERGE_TARGET_FIELD} names a review without its verdict file, or one without a review"
        )
    return MergeTarget(pull_request, review, verdict_file, base=base)


def _named_target(raw: Mapping[str, Any], base: Any) -> MergeTarget:
    """A `merge_pr` target: a number and a head sha, and nothing a task names."""
    beside = sorted(k for k in ("pull_request", "review", "verdict_file") if k in raw)
    if beside:
        raise TargetInvalid(
            f"{MERGE_TARGET_FIELD} names a pull request by number and also {', '.join(beside)}"
        )
    number = _int(raw.get("number"))
    head = raw.get("head_sha")
    if number is None or number < 1:
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.number is not a pull request number")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise TargetInvalid(f"{MERGE_TARGET_FIELD}.head_sha is not a full commit sha")
    return MergeTarget(None, base=base, number=number, head_sha=head)


def _pushed_head(doc: Mapping[str, Any]) -> str | None:
    git = _mapping(_mapping(doc.get("result_summary")).get("git"))
    value = git.get("pushed_head")
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _branch_task(doc: Mapping[str, Any], task_id: str) -> str:
    """The task whose `swarm/<id>` branch the pull request is on.

    The opener's own, or -- for a continuation -- the task it continues,
    read from its SIGNED dispatch block, never from its recorded branch.
    """
    block = _mapping(_mapping(doc.get("metadata")).get("dispatch"))
    return _task_id(block.get("continues")) or task_id


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    ctx: ActionContext
    summary: dict[str, Any] = field(default_factory=dict)

    def refuse(self, code: str, message: str) -> ActionOutcome:
        return refusal(self.summary, EndCause.MERGE_REFUSED, code, message)

    def wait(self, code: str, message: str, *, head: str, pull_request: int,
             pending: list[str]) -> ActionOutcome:
        """A fact that may change by itself: park CI_PENDING, holding nothing.

        Not a retry: the lifecycle writes the park, the refund and the
        fallback instant in one fenced transaction
        (`control.ControlPlane.park_ci_pending`), releases the lease and exits
        75. `head` is the head the checks were read at, which swarm-api's wake
        tick reads them at again.
        """
        self.summary["wait"] = {"code": code, "message": message}
        return _outcome(
            TaskState.PARKED, None, self.summary, f"{code}: {message}",
            exit_code=ExitCode.PARKED,
            ci_wait={"code": code, "head": head, "pull_request": pull_request,
                     "pending": sorted(pending)},
        )


def run_merge(ctx: ActionContext) -> ActionOutcome:
    """Merge the workflow's pull request, or refuse with the first failed check."""
    run = _Run(ctx, {"action": "merge", "merged_by_this_task": False})
    summary = run.summary

    # ---- §2.2 1 and 4: no credential in a process that is dumpable, or
    # beside a process that survived the reap.
    if ctx.unprotected:
        return run.refuse("worker_unprotected", ctx.unprotected)
    survivors = ctx.reap()
    if survivors:
        return run.refuse("processes_alive", f"{len(survivors)} process(es) survived the reap")

    # ---- §2.2 5: every claim that needs no credential.
    try:
        target = parse_merge_target(ctx.dispatch)
    except TargetInvalid as exc:
        return run.refuse("merge_target_invalid", str(exc))
    if target.pull_request is None:
        return _merge_named(run, target)
    summary["pull_request_task"] = target.pull_request
    if target.review is not None:
        summary["review_task"] = target.review

    try:
        opener = ctx.fetch_upstream(target.pull_request)
        _verify_opener(ctx, target, opener)
        if target.review is not None:
            ctx.verify_upstream(target.review, ctx.fetch_upstream(target.review))
    except UpstreamSpecUnverified as exc:
        return refusal(summary, EndCause.MERGE_REFUSED, "spec_unverified", str(exc),
                       spec_check=exc.spec_check())
    except InputUnavailable as exc:
        summary["refusal"] = {"code": "upstream_unreadable", "message": str(exc)}
        return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc))

    if target.review is not None:
        staged = ctx.staged.get(target.verdict_file or "")
        if staged is None:
            return run.refuse("verdict_unreadable",
                              f"the review's {target.verdict_file} was not staged")
        try:
            verdict = verdict_mod.read_verdict(
                staged, task_id=target.review, filename=target.verdict_file or ""
            )
        except InputUnavailable as exc:
            return run.refuse("verdict_unreadable", str(exc))
        summary["verdict"] = verdict.verdict
        if verdict.verdict != "MERGE":
            return run.refuse(
                "verdict_not_merge",
                f"the review's verdict is {verdict.verdict}, not MERGE"
                + (f" ({len(verdict.findings)} finding(s))" if verdict.findings else ""),
            )

    number = pull_request_number(opener)
    if number is None:
        return run.refuse("pull_request_unknown",
                          f"task {target.pull_request} recorded no pull request")
    pinned = _pushed_head(opener)
    if pinned is None:
        return run.refuse("head_unknown",
                          f"task {target.pull_request} recorded no pushed head to pin")
    summary["pull_request"] = number
    summary["pinned"] = pinned
    # The step's own record of its CI-fix rounds, and the heads they pushed:
    # task-store reads that need no credential, so before the token.
    own = _own_doc(ctx)
    fix_heads = _fix_round_heads(ctx, own, opener, target.pull_request)
    return _merge_with_token(
        run, number=number, pinned=pinned,
        branch=f"{ctx.branch_prefix}{_branch_task(opener, target.pull_request)}",
        own=own, fix_heads=fix_heads,
    )


def _verify_opener(ctx: ActionContext, target: MergeTarget, opener: Mapping[str, Any]) -> None:
    """The opener's signed spec, against the workflow IT belongs to (#900).

    Inside one workflow that is this step's own, as for every upstream. A
    merge-only continuation merges a task of an EARLIER workflow -- an issue
    run's integrator, or its last CI-fix round -- so its spec is verified
    against the workflow the merge's own signed target names, which the
    opener's signed `workflow_id` must then equal: a forged or edited spec
    is still `signature_mismatch`, and an honest spec of any other workflow
    still `workflow_mismatch`. Authority is bound separately, by this
    step's own signed spec: a continuation's dispatch block `continues` the
    branch the opener's pull request is on, or the target is not this
    continuation's to merge (`workflow_mismatch` too, fail closed).
    swarm-api bound the target to the issue run's record before signing it
    (`issueci.merge_target_bound`).
    """
    assert target.pull_request is not None
    if target.pull_request_workflow is None:
        ctx.verify_upstream(target.pull_request, opener)
        return
    ctx.verify_upstream(target.pull_request, opener, of_workflow=target.pull_request_workflow)
    if _task_id(ctx.dispatch.get("continues")) != _branch_task(opener, target.pull_request):
        raise UpstreamSpecUnverified(target.pull_request, "workflow_mismatch")


def _merge_named(run: _Run, target: MergeTarget) -> ActionOutcome:
    """A `merge_pr` target (#352): no opener to read, so straight to the token.

    The pinned head is the sha the caller named, signed; no CI-fix round can
    exist (swarm-api refuses `merge_fix_rounds` beside `merge_pr`), and the
    branch is whatever the pull request's author called it."""
    number, pinned = target.number, target.head_sha
    if number is None or pinned is None:
        return run.refuse("merge_target_invalid",
                          f"{MERGE_TARGET_FIELD} names neither a task nor a pull request")
    run.summary["pull_request"] = number
    run.summary["pinned"] = pinned
    return _merge_with_token(run, number=number, pinned=pinned,
                             branch=None, own=_own_doc(run.ctx), fix_heads=[])


def _merge_with_token(run: _Run, *, number: int, pinned: str, branch: str | None,
                      own: Mapping[str, Any], fix_heads: list[tuple[str, str]]) -> ActionOutcome:
    """§2.2 6-10: read the token, build the merger, and act with it. `branch`
    None skips the branch-name check, and only that, for a named pull request."""
    ctx, summary = run.ctx, run.summary
    repository = mergeable_repository(ctx.repository_url)
    if repository is None:
        return run.refuse("forge_unsupported",
                          "the merge step merges on github.com only, and this workflow's "
                          "repository is not there")
    summary["repository"] = repository

    # ---- §2.2 6-7: the token, at merge time only.
    from .secrets import CredentialMissing

    if ctx.read_git_token is None:
        return run.refuse("credential_unreadable", "this worker has no forge credential reader")
    try:
        token = ctx.read_git_token()
    except CredentialMissing as exc:
        return _outcome(TaskState.PARKED, None, summary, str(exc), credential_missing=exc)
    except Exception as exc:  # noqa: BLE001 - never the value; the type is enough
        summary["refusal"] = {"code": "credential_unreadable",
                              "message": f"the forge token could not be read ({type(exc).__name__})"}
        return _outcome(TaskState.FAILED, EndCause.CANNOT_START, summary,
                        f"credential_unreadable: {type(exc).__name__}")
    ctx.register_secret(token)
    try:
        merger = merger_for(ctx.repository_url, token=token, transport=ctx.transport,
                            retry=forge_retry(ctx))
    except UnsupportedForge as exc:
        return run.refuse("forge_unsupported", str(exc))
    finally:
        del token
    try:
        return _with_forge(run, merger, number=number, pinned=pinned,
                           branch=branch, own=own, fix_heads=fix_heads)
    except forge_mod.ForgeRedirectRefused as exc:
        cause = EndCause.MERGE_FAILED if summary.get("merge_called") else EndCause.MERGE_REFUSED
        return refusal(summary, cause, exc.code, str(exc))
    except forge_mod.ForgeUnavailable as exc:
        if summary.get("merge_called"):
            # Never resent: whether it merged is GitHub's to say, and the
            # next reader of this pull request will see.
            return refusal(summary, EndCause.MERGE_FAILED, "merge_unanswered",
                           f"the merge call did not answer ({exc}); whether it merged is "
                           "unknown, so it is not sent again")
        return unavailable(summary, EndCause.MERGE_FAILED, str(exc), exc.retry_after_seconds)
    except forge_mod.ForgeError as exc:
        status = getattr(exc, "status", None)
        if status in (401, 403, 404) and not summary.get("merge_called"):
            return run.refuse("token_lacks_rights",
                              f"the tenant's token cannot read {repository} as needed ({exc})")
        cause = EndCause.MERGE_FAILED if summary.get("merge_called") else EndCause.MERGE_REFUSED
        return refusal(summary, cause, getattr(exc, "code", "forge_refused"), str(exc))
    finally:
        del merger


def _with_forge(run: _Run, merger: ForgeMerger, *, number: int, pinned: str,
                branch: str | None, own: Mapping[str, Any] | None = None,
                fix_heads: list[tuple[str, str]] | None = None) -> ActionOutcome:
    """§2.2 8-10 and §5, with the token in hand. Raises the forge's errors.

    `pinned` is the head the opening step pushed; `fix_heads` the heads this
    workflow's CI-fix rounds pushed (`_fix_round_heads`). The head this
    attempt acts at is GitHub's live head, accepted only through `_accept`.
    `own` is the step's own document, for its CI-fix rounds.
    """
    own = own or {}
    fix_heads = fix_heads or []
    ctx, summary = run.ctx, run.summary
    if ctx.recheck():
        return cancelled(summary)
    if not merger.can_push():
        return run.refuse("token_lacks_rights",
                          f"the tenant's token cannot write to {merger.full_name}")

    pr = merger.pull_request(number)
    summary["base"] = pr.base_ref
    # The registered default first (swarm-api wrote it into the signed
    # target, MS1), GitHub's own when the tenant registered none. Before the
    # merged row: a merge into another branch closes nothing, as the close
    # script says, so it is not this step's success either.
    default = parse_merge_target(ctx.dispatch).base or merger.default_branch()
    if not default or pr.base_ref != default:
        return run.refuse("base_not_default",
                          f"pull request #{number} targets {pr.base_ref}, not the repository's "
                          f"default branch {default or '(unreadable)'}")
    if pr.merged:
        if pr.head_sha is not None and _accept(
            merger, head=pr.head_sha, pinned=pinned, fix_heads=fix_heads, base=default
        ) is not None:
            if _queued_by_this_step(own):
                # The merge queue merged what this step enqueued (C3H): this
                # step's merge, recorded as the direct merge's is.
                summary["merged_by_this_task"] = True
                summary["merged_through_queue"] = True
                summary["merge_commit"] = pr.merge_commit_sha
                _record(summary, merger, number,
                        queued_provenance(ctx, merger, number=number, head=pr.head_sha,
                                          base=default))
                _close_issues(run, merger, number)
                return _outcome(TaskState.SUCCEEDED, None, summary, "")
            # A lost attempt that merged, or another merger after this step's
            # update: the merge stands, and the issues it closes are closed
            # below as if this attempt had made it.
            summary["already_merged"] = True
            summary["merge_commit"] = pr.merge_commit_sha
            _close_issues(run, merger, number)
            return _outcome(TaskState.SUCCEEDED, None, summary, "")
        return run.refuse("merged_at_other_head",
                          f"pull request #{number} was merged at {pr.head_sha}, not {pinned}")
    if pr.state != "open":
        return run.refuse("pull_request_closed", f"pull request #{number} is closed, not merged")
    if pr.head_repo is None or pr.head_repo.lower() != (pr.base_repo or "").lower():
        return run.refuse("from_fork",
                          f"pull request #{number} comes from {pr.head_repo or 'an unknown fork'}, "
                          f"not {merger.full_name}")
    belongs = branch is None or pull_request_belongs(
        {"head": {"ref": pr.head_ref, "repo": {"full_name": pr.head_repo}},
         "base": {"repo": {"full_name": pr.base_repo}}},
        author_branch=branch,
    )
    if not belongs:
        return run.refuse("pull_request_not_this_workflows",
                          f"pull request #{number} is not {branch} from {merger.full_name}")
    if pr.draft:
        return run.refuse("draft", f"pull request #{number} is a draft")
    head = pr.head_sha
    accepted = None if head is None else _accept(
        merger, head=head, pinned=pinned, fix_heads=fix_heads, base=default
    )
    if head is None or accepted is None:
        return run.refuse("head_moved",
                          f"pull request #{number}'s head is {head}, not {pinned}, "
                          "the head this workflow pushed, nor one of its CI-fix rounds' "
                          "heads, nor GitHub's own update of either")
    updates, pushed_by = accepted
    summary["head"] = head
    summary["updates"] = updates
    if pushed_by is not None:
        summary["head_pushed_by"] = pushed_by
    if title_is_placeholder(pr.title):
        return run.refuse("title_placeholder",
                          "the pull request's title is the worker's placeholder")

    def wait(code: str, message: str, pending: list[str]) -> ActionOutcome:
        waited = _ci_wait_age(ctx)
        if waited is not None:
            return run.refuse("checks_timeout",
                              f"CI has not settled in {int(waited // 60)} min, past "
                              f"{MERGE_CI_MAX_SECONDS // 3600} h: {message}")
        return run.wait(code, message, head=head, pull_request=number, pending=pending)

    if _queued_by_this_step(own):
        # An earlier attempt enqueued it (C3H). Open and unmerged at an
        # accepted head: the queue still has it, or took it out.
        return _in_queue(run, merger, number=number, head=head, base=default, wait=wait)

    # The checks, at the head this attempt acts at.
    required = merger.required_checks(pr.base_ref or "")
    checks = merger.checks_at(head)
    summary["required_checks"] = sorted({c.context for c in required.checks})
    summary["required_checks_source"] = required.source
    pending: list[str] = []
    failed: list[str] = []
    if required.checks:
        for check in required.checks:
            mine = [c.as_run() for c in checks if c.name == check.context
                    and (check.app_id is None or c.app_id == check.app_id)]
            state = required_check_state(mine)
            if state == "pending":
                pending.append(check.context)
            elif state == "failed":
                failed.append(f"{check.context} ("
                              + ", ".join(sorted({str(r['conclusion']) for r in mine})) + ")")
    else:
        # No protection, or protection that requires no check, or a plan that
        # has neither rulesets nor protection to read (`required.source`
        # `none_plan_limited_all_checks`): every check reported at the head
        # must be green, and there must be one.
        if not checks:
            return wait("no_checks",
                        f"{pr.base_ref} requires no check and none has reported at "
                        f"{head}; the merge needs at least one green check", [])
        for check in checks:
            if other_check_blocks(check.as_run()):
                label = f"{check.name} ({check.conclusion or check.status})"
                (pending if check.status != "completed" else failed).append(label)
    if failed:
        return _red(run, own, head=head, failed=failed, wait=wait)
    if pending:
        return wait("checks_pending", f"at {head}: " + ", ".join(sorted(pending)), pending)

    rereads = 0
    while pr.mergeable is None and rereads < MERGEABLE_REREADS:
        rereads += 1
        ctx.sleep(MERGEABLE_REREAD_SECONDS)
        pr = merger.pull_request(number)
    if pr.head_sha != head:
        return run.refuse("head_moved",
                          f"pull request #{number}'s head moved to {pr.head_sha} while its "
                          f"mergeability was read at {head}")
    if pr.mergeable is False or pr.mergeable_state == "dirty":
        return run.refuse("merge_conflict",
                          f"pull request #{number} does not merge cleanly into {pr.base_ref}")
    if pr.mergeable is None:
        return wait("mergeability_unknown",
                    f"GitHub had not computed mergeability after {MERGEABLE_REREADS} rereads", [])
    if pr.mergeable_state == "behind":
        return _update_branch(run, merger, number=number, head=head, updates=updates,
                              base=default)

    # ---- §2.2 8: fencing and cancel, immediately before the call.
    if ctx.recheck():
        return cancelled(summary)

    # ---- §5.3: the merge, squashed, pinned to the head the checks are green at.
    message = provenance(ctx, merger, number=number, pinned=head)
    summary["merge_called"] = True
    answer = merger.merge(number, sha=head, title=pr.title, message=message)
    if not answer.merged:
        detail = f": {answer.message}" if answer.message else ""
        if answer.status == 409:
            return run.refuse("head_moved", f"GitHub answered 409: the head is no longer {head}")
        if answer.status in (405, 422):
            if _MERGE_QUEUE.search(answer.message):
                # Answered, so nothing was merged: the base merges only
                # through its merge queue, and the enqueue is a different
                # call, not the merge sent again.
                summary.pop("merge_called")
                summary["merge_answered"] = {"status": answer.status, "message": answer.message}
                return _enqueue(run, merger, number=number, node_id=pr.node_id, head=head,
                                base=default, wait=wait)
            if _OUT_OF_DATE.search(answer.message):
                # Answered, so nothing was merged: the update row, which is a
                # different call, not the merge sent again.
                summary.pop("merge_called")
                summary["merge_answered"] = {"status": answer.status, "message": answer.message}
                return _update_branch(run, merger, number=number, head=head, updates=updates,
                                      base=default)
            if _CONFLICT.search(answer.message):
                return run.refuse("merge_conflict",
                                  f"GitHub refused the merge ({answer.status}){detail}")
            return run.refuse("protection_refused",
                              f"GitHub's branch protection refused the merge "
                              f"({answer.status}){detail}")
        if answer.status in (401, 403, 404):
            return run.refuse("token_lacks_rights",
                              f"GitHub refused the merge ({answer.status}){detail}")
        return refusal(summary, EndCause.MERGE_FAILED, "forge_refused",
                       f"the merge call answered {answer.status}{detail}")
    summary["merged_by_this_task"] = True
    summary["merge_commit"] = answer.sha

    # ---- §5.4: the record, then the issues. The merge stands whatever these
    # answer; each failure is recorded, never raised.
    _record(summary, merger, number, message)
    _close_issues(run, merger, number)
    return _outcome(TaskState.SUCCEEDED, None, summary, "")


def _record(summary: dict[str, Any], merger: ForgeMerger, number: int, message: str) -> None:
    """The comment naming the task that merged the pull request; a failure is recorded."""
    try:
        summary["recorded_on_pull_request"] = merger.comment(number, message)
    except forge_mod.ForgeError as exc:
        summary["recorded_on_pull_request"] = False
        summary["recorded_on_pull_request_reason"] = str(exc)[:300]


# ---------------------------------------------------------------------------
# A base that merges only through a merge queue (lane C3H)
# ---------------------------------------------------------------------------


def _queued_by_this_step(own: Mapping[str, Any]) -> bool:
    """Whether this step's last park was in the merge queue.

    `merge_wait.code` on the step's own document, written by the fenced park.
    The document is tenant-writable, so a forged code can at worst make the
    step read the queue and refuse `merge_dequeued`, which changes nothing on
    the forge -- `_ci_wait_age`'s rule.
    """
    wait = _mapping(_mapping(own.get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    return wait.get("code") == MERGE_QUEUED


def _queued_wait(run: _Run, state: QueueFacts, *, number: int, head: str, base: str,
                 wait: Any) -> ActionOutcome:
    run.summary["merge_queued"] = state.record(head)
    where = f" at position {state.position}" if state.position is not None else ""
    return wait(MERGE_QUEUED,
                f"pull request #{number} is in {base}'s merge queue{where} at {head} "
                f"(entry {state.entry or 'unnamed'}, {state.state or 'state unknown'})", [])


def _enqueue(run: _Run, merger: ForgeMerger, *, number: int, node_id: str | None, head: str,
             base: str, wait: Any) -> ActionOutcome:
    """GitHub's merge queue takes the pull request at `head`; then park on it.

    Behind the same fencing recheck as the merge: a superseded worker
    enqueues nothing (invariant 5). A refusal from GitHub is read against the
    queue once -- a lost answer, an operator or auto-merge may have queued it
    first -- and is `merge_queue_refused` only when it is not there.
    """
    ctx, summary = run.ctx, run.summary
    if node_id is None:
        return run.refuse("merge_queue_refused",
                          f"{base} merges through a merge queue, and GitHub gave no id for "
                          f"pull request #{number} to enqueue it by")
    if ctx.recheck():
        return cancelled(summary)
    answer = merger.enqueue(number, node_id=node_id, expected_head_sha=head)
    state = answer.entry
    if not answer.enqueued or state is None:
        state = merger.queue_state(number)
        if not state.queued:
            return run.refuse("merge_queue_refused",
                              f"{base} merges through a merge queue, and GitHub refused to "
                              f"add pull request #{number} to it: {answer.message}")
    return _queued_wait(run, state, number=number, head=head, base=base, wait=wait)


def _in_queue(run: _Run, merger: ForgeMerger, *, number: int, head: str, base: str,
              wait: Any) -> ActionOutcome:
    """The wake of a queued park: still queued parks again; out of the queue
    unmerged refuses with GitHub's reason, and is never enqueued again -- a
    merge group that failed would fail again."""
    state = merger.queue_state(number)
    if state.queued:
        return _queued_wait(run, state, number=number, head=head, base=base, wait=wait)
    return run.refuse("merge_dequeued",
                      f"pull request #{number} left {base}'s merge queue unmerged: "
                      f"{state.removed_reason or 'GitHub recorded no reason'}")


def queued_provenance(ctx: ActionContext, merger: ForgeMerger, *, number: int,
                      head: str | None, base: str) -> str:
    """The pull request comment for a merge the queue made (§5.4). No attribution."""
    return "\n".join([
        f"Merged by SwarmCloud task {ctx.task_id} (workflow {ctx.workflow_id}, "
        f"attempt {ctx.attempt_id}) into {merger.full_name}#{number},",
        f"through {base}'s merge queue, which this task enqueued at {head}, the head "
        "the workflow pushed or GitHub's update of it onto the base, with every "
        "required check green there.",
    ])


def _updates_onto(merger: ForgeMerger, *, head: str, pushed: str, base: str) -> int | None:
    """How many of GitHub's base merges lead from `pushed` to `head`, or None.

    The first-parent walk (docs/merge-step.md "Revised 2026-10-06" §1): from
    the live head, each step down must be a two-parent commit GitHub itself
    committed and signed, whose second parent is already on `base`; its first
    parent is the next step. It must reach `pushed` within
    MERGE_MAX_BRANCH_UPDATES steps. A fact about GitHub, never about the
    tenant-writable task document, so a lost attempt's update is re-derived
    here and a forged count changes nothing. Anything else is `head_moved`.
    """
    steps = 0
    while head != pushed:
        if steps >= MERGE_MAX_BRANCH_UPDATES:
            return None
        try:
            commit = merger.commit(head)
            if len(commit.parents) != 2 or not commit.by_github:
                return None
            if not merger.on_base(base, commit.parents[1]):
                return None
        except forge_mod.ForgeAnswered as exc:
            # A commit or comparison GitHub does not have is not an update.
            if getattr(exc, "status", None) in (404, 422):
                return None
            raise
        head = commit.parents[0]
        steps += 1
    return steps


def _update_branch(run: _Run, merger: ForgeMerger, *, number: int, head: str, updates: int,
                   base: str) -> ActionOutcome:
    """GitHub merges the base into a branch that is behind; then park at the new head.

    A write to the forge, so the fencing and cancel recheck runs first and a
    stale worker raises out of it with no call made (invariant 5).
    `expected_head_sha` is the head the checks were read at: GitHub refuses
    the update if anyone pushed in between.
    """
    ctx, summary = run.ctx, run.summary
    if updates >= MERGE_MAX_BRANCH_UPDATES:
        return run.refuse("behind_too_often",
                          f"pull request #{number} is behind {base} again after {updates} "
                          f"updates (at most {MERGE_MAX_BRANCH_UPDATES}): the base moves "
                          "faster than CI")
    if ctx.recheck():
        return cancelled(summary)
    answer = merger.update_branch(number, expected_head_sha=head)
    detail = f": {answer.message}" if answer.message else ""
    if answer.status == 422:
        if _CONFLICT.search(answer.message):
            return run.refuse("merge_conflict",
                              f"GitHub could not merge {base} into #{number}{detail}")
        return run.refuse("head_moved",
                          f"GitHub refused to update #{number} at {head}{detail}")
    if answer.status in (401, 403, 404):
        return run.refuse("token_lacks_rights",
                          f"GitHub refused to update #{number} ({answer.status}){detail}")
    if answer.status not in (200, 202):
        return run.refuse("forge_refused",
                          f"the update-branch call answered {answer.status}{detail}")
    # GitHub makes the merge commit ASYNCHRONOUSLY: the 202 only says it will.
    # The new head is read again a bounded few times (invariant 4: seconds,
    # not a provider wait). A head that moved is parked at, and the tick reads
    # the checks there. One that has not is parked at as it is, with
    # BRANCH_UPDATE_PENDING, on which the tick waits for the head to move
    # rather than reading this head's checks -- green, since it was only
    # behind -- and the fallback instant covers an update GitHub never makes.
    # Either way nothing merges here: the old head is never merged after an
    # update was asked for, and the next wake re-reads everything.
    new_head = merger.pull_request(number).head_sha
    rereads = 0
    while (not new_head or new_head == head) and rereads < MERGEABLE_REREADS:
        rereads += 1
        ctx.sleep(MERGEABLE_REREAD_SECONDS)
        new_head = merger.pull_request(number).head_sha
    if not new_head or new_head == head:
        summary["branch_updated"] = {"from": head, "to": None, "updates": updates + 1}
        return run.wait(BRANCH_UPDATE_PENDING,
                        f"GitHub accepted the update of #{number} at {head} and had not made "
                        f"it after {MERGEABLE_REREADS} rereads; the step waits for the new head",
                        head=head, pull_request=number, pending=[])
    summary["branch_updated"] = {"from": head, "to": new_head, "updates": updates + 1}
    return run.wait("branch_updated",
                    f"GitHub merged {base} into #{number} at {head}; the checks run again "
                    f"at {new_head}",
                    head=new_head, pull_request=number, pending=[])


def _accept(merger: ForgeMerger, *, head: str, pinned: str,
            fix_heads: list[tuple[str, str]], base: str) -> tuple[int, str | None] | None:
    """(GitHub's base merges on top, the CI-fix round's task or None), or None.

    The live head is accepted when it is the pinned head or a head one of
    this workflow's CI-fix rounds pushed (`fix_heads`, newest first), itself
    or under GitHub's own base merges (`_updates_onto`). Exact matches are
    tried first, so the common case reads nothing more from GitHub.
    """
    candidates: list[tuple[str | None, str]] = [(None, pinned), *fix_heads]
    for task_id, pushed in candidates:
        if head == pushed:
            return 0, task_id
    for task_id, pushed in candidates:
        updates = _updates_onto(merger, head=head, pushed=pushed, base=base)
        if updates is not None:
            return updates, task_id
    return None


def _own_doc(ctx: ActionContext) -> Mapping[str, Any]:
    """The step's own task document, or {} when it cannot be read."""
    try:
        return _mapping(ctx.fetch_upstream(ctx.task_id))
    except Exception:  # noqa: BLE001 - an unreadable record has no rounds
        return {}


def _fix_rounds(own: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rounds = _mapping(_mapping(own.get("metadata")).get(MERGE_FIX_METADATA_KEY)).get("rounds")
    return [r for r in rounds if isinstance(r, Mapping)] if isinstance(rounds, list) else []


def _rounds_asked(own: Mapping[str, Any]) -> int:
    value = _mapping(own.get("metadata")).get(MERGE_FIX_ROUNDS_KEY)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, MERGE_FIX_ROUNDS_MAX)


def _fix_round_heads(ctx: ActionContext, own: Mapping[str, Any], opener: Mapping[str, Any],
                     target: str) -> list[tuple[str, str]]:
    """(task, pushed head) of each CI-fix round of this merge, newest first (lane MS7).

    The rounds are named by the tenant-writable `merge_fix` record, so each
    is held to facts the record cannot forge, the rule swarm-api's
    `issueci._pushing_task` applies, rechecked here: the round's task is
    read through the tenant-gated upstream read, is this tenant's, its
    signed spec verifies, and its SIGNED dispatch block `continues` the same
    root as the signed target -- the branch the pull request is on. Its
    pushed head is its own `result_summary`, as the opener's is. A round
    that fails any of these is not a candidate, and its head is `head_moved`.
    """
    root = _branch_task(opener, target)
    heads: list[tuple[str, str]] = []
    for entry in reversed(_fix_rounds(own)):
        task_id = _task_id(entry.get("task_id"))
        if task_id is None or task_id in (target, root, ctx.task_id):
            continue
        try:
            doc = _mapping(ctx.fetch_upstream(task_id))
            ctx.verify_upstream(task_id, doc)
        except Exception:  # noqa: BLE001 - unreadable or unsigned: not a round's head
            continue
        if doc.get("tenant_id") != ctx.tenant_id:
            continue
        block = _mapping(_mapping(doc.get("metadata")).get("dispatch"))
        if _task_id(block.get("continues")) != root:
            continue
        pushed = _pushed_head(doc)
        if pushed is not None:
            heads.append((task_id, pushed))
    return heads


def _ended(ctx: ActionContext, task_id: str) -> bool:
    """Whether a round's task has ended; one that cannot be read has, for this step."""
    try:
        state = _mapping(ctx.fetch_upstream(task_id)).get("state")
        return TaskState(state) in TERMINAL_STATES
    except Exception:  # noqa: BLE001 - nothing left to wait on
        return True


def _red(run: _Run, own: Mapping[str, Any], *, head: str, failed: list[str],
         wait: Any) -> ActionOutcome:
    """A failing required check at `head`: a CI-fix round's to fix, or `checks_failed`.

    docs/merge-step.md "Revised 2026-10-06" §1: with rounds left the tick
    hands the red reading to a round first, so the refusal only runs once
    the rounds are spent or there were none. A round running at this head,
    or one due that the tick has not claimed yet, parks (`wait`, which
    `MERGE_CI_MAX_SECONDS` bounds). A round that ended at this head without
    moving it, or was never submitted, is spent.
    """
    message = f"at {head}: " + ", ".join(sorted(failed))
    rounds = _fix_rounds(own)
    last = rounds[-1] if rounds else {}
    if rounds and last.get("head") == head:
        number = last.get("round")
        error = last.get("error")
        if error:
            return run.refuse("checks_failed",
                              f"{message}; CI fix round {number} was not run: {str(error)[:300]}")
        task_id = _task_id(last.get("task_id"))
        if task_id is None:
            return wait(CI_FIX_RUNNING, f"{message}; CI fix round {number} is being submitted", [])
        if not _ended(run.ctx, task_id):
            return wait(CI_FIX_RUNNING, f"{message}; CI fix round {number} ({task_id}) is running",
                        [])
        return run.refuse("checks_failed",
                          f"{message}, after CI fix round {number} ({task_id}) ended at this head")
    asked = _rounds_asked(own)
    if len(rounds) < asked:
        return wait(CI_FIX_PENDING,
                    f"{message}; CI fix round {len(rounds) + 1} of {asked} is due", [])
    if asked:
        message += f", after {len(rounds)} CI fix round(s)"
    return run.refuse("checks_failed", message)


def _ci_wait_age(ctx: ActionContext) -> float | None:
    """Seconds since this step first parked for CI, when past MERGE_CI_MAX_SECONDS.

    `merge_wait.first_parked_at` on the step's own document, written by the
    fenced park (`control.ControlPlane.park_ci_pending`). The document is
    tenant-writable, so a forged instant can at worst end the step early as a
    refusal, which changes nothing on the forge; an unreadable one waits on,
    and `max_attempts` still bounds the wakes.
    """
    try:
        doc = ctx.fetch_upstream(ctx.task_id)
    except Exception:  # noqa: BLE001 - an unreadable record is no timeout
        return None
    wait = _mapping(_mapping(_mapping(doc).get("metadata")).get(MERGE_WAIT_METADATA_KEY))
    first = wait.get("first_parked_at")
    if not isinstance(first, datetime):
        return None
    try:
        waited = (utcnow() - first).total_seconds()
    except TypeError:  # a naive instant: not one the park wrote
        return None
    return waited if waited > MERGE_CI_MAX_SECONDS else None


def _close_issues(run: _Run, merger: ForgeMerger, number: int) -> None:
    """Close every still-open issue the pull request closes (#569).

    GitHub's own `closingIssuesReferences`: the issues its closing keywords
    (and manual links) name. A `part of #N` pull request names none, so it
    closes none. An issue in another repository is left alone -- the token's
    reach there is not this step's to assume -- and recorded as such. The
    rules of `scripts/close-merged-issues.sh`, held equal to it by
    tests/unit/worker/test_merge_action.py, page rule included: past one page
    the page is closed and `issues_beyond_page` recorded.
    """
    summary = run.summary
    closed: list[int] = []
    already: list[int] = []
    elsewhere: list[str] = []
    failed: list[dict[str, Any]] = []
    try:
        references = merger.closing_issues(number)
    except forge_mod.ForgeError as exc:
        summary["issues_unread"] = str(exc)[:300]
        return
    note = f"Closed by #{number}, merged by SwarmCloud task {run.ctx.task_id}"
    for issue in references.issues:
        if issue.repository.lower() != merger.full_name.lower():
            elsewhere.append(f"{issue.repository}#{issue.number}")
            continue
        if issue.state.upper() != "OPEN":
            already.append(issue.number)
            continue
        try:
            if merger.close_issue(issue.number, comment=note):
                closed.append(issue.number)
            else:
                failed.append({"number": issue.number, "reason": "the forge refused"})
        except forge_mod.ForgeError as exc:
            failed.append({"number": issue.number, "reason": str(exc)[:200]})
    summary["issues_closed"] = closed
    if already:
        summary["issues_already_closed"] = already
    if elsewhere:
        summary["issues_in_other_repositories"] = elsewhere
    if failed:
        summary["issues_not_closed"] = failed
    if references.total > len(references.issues):
        summary["issues_beyond_page"] = {"total": references.total,
                                         "listed": len(references.issues)}


def provenance(ctx: ActionContext, merger: ForgeMerger, *, number: int, pinned: str) -> str:
    """The squash commit's body and the pull request comment (§5.4). No attribution."""
    return "\n".join([
        f"Merged by SwarmCloud task {ctx.task_id} (workflow {ctx.workflow_id}, "
        f"attempt {ctx.attempt_id}) into {merger.full_name}#{number},",
        f"squashed at {pinned}, the head the workflow pushed or GitHub's update of it "
        "onto the base, with every required check green there.",
    ])
