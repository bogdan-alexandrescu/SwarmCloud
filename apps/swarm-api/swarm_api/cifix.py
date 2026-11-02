"""The CI fixer's record on a fix step, its per-PR cap, and its post-back (#263).

`scripts/ci-fix.sh` submits ONE fix step for a red `swarm/<task>` pull
request: a one-step `direct-pr` workflow that `continues_task` the red
branch's task (`continuation.py`), with `metadata.ci_fix` naming the pull
request, the red run, its head and `attempt` of `max_attempts`. The script
comments on the pull request when it submits. Before this module nothing said
how the step ENDED: the pull request went quiet, and whoever looked at it had
to find the task in the console to learn whether a fix was pushed.

TWO HALVES.

AT SUBMISSION (`stamp`), inside `Service.submit_workflow`'s counted refusals:

  * `metadata.ci_fix` is accepted only on the WORKFLOW (a step's own copy
    would override it on one task only) and only beside `continues_task`:
    without a continuation there is no red branch, so no pull request whose
    head it can be held to.
  * Its fields are typed and bounded, and the keys this module writes --
    `postback` and the post-back's own record -- are refused from a caller,
    so a `pending` stamp is always the API's.
  * THE CAP HOLDS HERE TOO. The script counts its own attempt comments, which
    anyone with write access can delete; this counts the tenant's fix tasks
    for that pull request of that branch (`metadata.ci_fix.pull_request` and
    `metadata.dispatch.continues`, two equality filters served by the built-in
    single-field indexes) and refuses the one past `max_attempts`. The ceiling
    on `max_attempts` is the script's own reasoning: a third red run after two
    fixes aimed at the exact failure is a fixer that is not converging.

AFTER THE STEP ENDS (`postback_tenant`), on the per-tenant `merge_wake` tick
(`routes/admin.py`, terraform/modules/scheduler/jobs.tf), as the rollup
sweeper, every minute:

  * One query for the named tenant's tasks stamped `pending`. A task not yet
    terminal is left alone: it holds whatever capacity it holds, and this
    reads nothing for it (invariants 1 and 3 are the scheduler's).
  * For a terminal one, with that tenant's `-git` token
    (`ctx.forge_tokens.token_for`, read at most once per tick, only when one
    is due, dropped at the end -- invariant 9), through the same
    `forgewrite.GitHubWriter` the issue run writes with: the pull request is
    read and must be open on the head branch `swarm/<the continued task>` --
    the branch the fix pushed to. Then ONE comment, found first by its marker
    so a write whose record was lost is not repeated.
  * The comment is the task id, its console link, its state and one
    paragraph: what was pushed, or why nothing was, and the agent's own
    account cut to a paragraph. Every text the agent or the error wrote goes
    through `issuecomments.prose` (a code span, `code`) with the token as a
    known literal: redacted, an HTML opener backslash-escaped rather than
    turned into an entity GitHub would print, mentions and closing keywords
    broken. No log
    is quoted; the console has it.
  * It says when the per-PR cap is reached, so a reader knows the next red
    run gets the script's stop comment and no fix.
  * A write that fails is retried every `RETRY_SECONDS`, up to
    `MAX_POSTBACK_FAILURES`, then given up with its code. A pull request on
    another branch, or a task with no continuation, is given up at once:
    retrying cannot change either.

WHAT IS WRITTEN on the task: `metadata.ci_fix.postback` and its record
(`posted_at`, `comment_id`, or `postback_code` and the failure count), in a
transaction guarded on the task still being this tenant's and still
`pending`. Nothing else, and the task never moves.

ONE LIVE FIX PER FAILURE (epic #1334). Measured 2026-10-15 ~01:50Z on PR
1338: two of the script's fix tasks ran for one red run at one head
(task_fcdff76b, then task_41ae250f as "attempt 2"), because the cap counts
attempts and a second delivery of one failure is simply the next attempt.
So `stamp` takes a CLAIM on (tenant, repository, pull request, head, run) in
`CLAIMS`, in ONE Firestore transaction with the read of the claim already
there. While the task holding it is not terminal -- found by
`metadata.ci_fix.claim`, or, for a claim whose task is not written yet (the
submission is still being built, or was refused after `stamp`), for
`CLAIM_GRACE_SECONDS` -- the request is answered 409 `ci_fix_in_flight`
naming the live task and its workflow, and no task is made. The claim
document is the serialisation point: two requests racing through `stamp`
both read it in a transaction, and only one writes it.

ONE FIXER PER PULL REQUEST (CI-FIXER-GATE, part of #1822). Before the cap
and the claim, `stamp` reads the workflow of the task whose branch the pull
request is on; while that workflow's merge step is not terminal, the request
is answered 409 `ci_fix_lane_owns_pr` naming the merge step: the lane runs
its own CI-fix rounds, and a standalone fix beside them lost the race for the
branch (`_refuse_if_lane_owns` has the measurement). An issue run owns its
pull request the same way (#2216): its compiled workflow has no merge step,
but while it is not terminal it reads CI and submits its own rounds from
`issueci`, so a non-terminal issue run of the tenant whose `pr_task_id` is
the continuation's root -- or one of whose `ci_fix_workflows` is the root's
workflow -- is answered 409 `ci_fix_lane_owns_pr` naming the run. The reads
are the API's; the fixer's continuation-scoped identity never gets them.

The same refusal holds while a CONFLICT FIX on that pull request is live
(#2456): a non-terminal task continuing the same branch task with
pr-shepherd's `metadata.pr_shepherd` or a merge step's `merge_conflict_round`
naming it. Measured 2026-10-26 on PR #2381: a conflict fix and a CI fix both
cloned 15cc7ea and the later push was rejected non-fast-forward. pr-shepherd
holds its side the same way (`live_pr_writer`, read from `_plan_one`), so
whichever fixer comes second stands down.

ONE WRITER PER PULL REQUEST, FOR EVERY PUSHING REQUEST (#2535). The gates
above read only a `metadata.ci_fix` request; anything else went straight
through. Measured 2026-10-26 on PR #2473: wf_3bde5fa9's `implement` step,
pushing onto task_577523f9's branch with `publish_to_pr`, was admitted at
19:23:00Z, three seconds after the CI fixer task_b0abb81d started on the same
red head 377c3010; both fixed the same test, and the step's publish was
refused `head_moved`. So a pushing request that carries no writer mark of its
own (`PR_WRITER_KINDS`, each with its own gate) is answered 409
`pr_fix_in_flight` naming the live fix, its workflow and kind: a
`continues_task` one while `live_pr_writer` finds one on the pull request its
branch task recorded (`result_summary.git.pull_request.number`), a
`publish_to_pr` one while `_live_writer_on_pull` finds one on that number in
its repository. A merge-only request and a branch task with no recorded pull
request are admitted as before.

The other order holds too (#2583): a `metadata.ci_fix` request is refused
409 `ci_fix_lane_owns_pr` with `plain_continuation` while a plain
continuation of the lane is live -- a non-terminal task of the tenant that
continues the branch task or pushes onto it with `publish_to_pr`, is not a
merge step and carries no writer mark of its own (`live_plain_continuation`).
Measured 2026-10-27 on PR #2570: the CI fixer's task_7d270954 was admitted
two minutes after implement task_8017fba2 (wf_1a460a31) started on the same
red head 7e55a8d, pushed f4f93dc first, and the implement step's publish was
refused `head_moved`.

And a plain `continues_task` request is refused 409 `ci_fix_lane_owns_pr`
with `plain_continuation` while the continued lane's merge step is not
terminal (#2685), naming the merge step and its workflow
(`_refuse_if_lane_merge_live`); merge-only requests and requests with a
writer's own mark keep their own gates. Measured 2026-10-28 on PR #2617: the
plain lane task_7ff30c32 (wf_85f651ab) was admitted while merge step
task_943ec5f2 was live, pushed 5bc405a before the merge step's ci-fix:1
round task_99b8e1e0, whose push was rejected non-fast-forward, and the merge
step refused `head_moved`.

EVERY FIX ROUND'S HEAD, AGAINST ITS PULL REQUEST (epic #1334). Measured
2026-10-15 02:16Z: HANG-LEASE's merge step refused PR 1338 `head_moved` at
the head a fix task of ANOTHER workflow pushed; a merge step knew only its
own rounds and the `ci_fix` query. `stamp` marks each fix round it sees --
`metadata.ci_fix`, a merge step's `merge_fix_round`, a conflict fix round's
`merge_conflict_round`, each beside `continues_task` -- with
`FIX_HEAD_FIELD`, `pending`. The same tick, once that task is terminal,
appends what it pushed to `FIX_HEADS_FIELD` on the task whose
`swarm/<id>` branch the pull request is on (its signed `continues`), keyed
by the repository and the pull request's number, never by a workflow; the
merge step (`agent_worker.merge._recorded_fix_heads`) reads it there and
holds every task it names to its signed spec before its head counts. The
record is an index, never the proof.

A FIX ROUND RUNS WITH ITS LANE'S TERRITORY (CIFIX-TERRITORY, part of #1499).
Measured 2026-10-17 on PR 1485: a PR-side round edited
`apps/swarm-mcp/swarm_mcp/sc.py`, outside lane ISO-WORKER-CORE, and turned a
key refusal into exit 0; CI caught it, and nothing had told the fixer where
the lane ended. `stamp` copies `inputs.territory` from the task whose branch
the pull request is on -- its stored record, read by id under the tenant,
never the failing log or an agent's output -- onto each pushing step of a
`ci_fix` or `merge_fix_round` workflow, replacing any the caller sent, and
appends `laneterritory.round_brief` to its prompt. The worker's publish then
labels a push that leaves it, as it does a lane's. A warning, never a
refusal: BGD's decision for lanes (#1468, 2026-10-19). A lane that declares
none gives a round as before. A conflict round is left out: it merges the
base into the branch, so its push changes every path the base changed since,
and the lane's territory would flag main's own work.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TERMINAL_STATES

from .codec import agent_console_url
from .continuation import TASK_ID_RE, Continuation
from .errors import ApiError, Conflict, NotFound
from . import laneterritory, publishtopr
from .forgewrite import GitHubWriter
from .issueci import NO_CHANGE_MARKER
from .issuecomments import bounded, code, prose
from .issueruns import RUNS_COLLECTION, TERMINAL_RUN_STATES, IssueRun
from .refusals import refuse
from .repositories import parse_repository
from .rollup import SKIPPED_SUMMARY_KEY
from .schemas import WorkflowCreate
from .validation import (
    CONFLICT_ROUND_METADATA_KEY,
    DISPATCH_METADATA_KEY,
    PROMPT_INPUT_KEY,
    DispatchOptionError,
    IssueRef,
    is_merge_step,
)

log = logging.getLogger(__name__)

#: `metadata.ci_fix`, as scripts/ci-fix.sh sends it.
CI_FIX_METADATA_FIELD = "ci_fix"

#: What a caller may send in it. Everything else is refused, the post-back's
#: own record above all.
CALLER_FIELDS = frozenset({"pull_request", "run_id", "head_sha", "attempt", "max_attempts"})

#: The post-back's states, in `metadata.ci_fix.postback`. Only this module
#: writes them.
POSTBACK_FIELD = "postback"
PENDING = "pending"
POSTED = "posted"
GAVE_UP = "gave_up"

#: THREE. scripts/ci-fix.sh's MAX_FIX_ATTEMPTS is two, and its test refuses a
#: cap above three: a third red run after fixes aimed at the exact failure is
#: a fixer that is not converging, and more attempts spend a slot and an
#: account's quota on commits someone has to unpick. One above the script's
#: own cap leaves room to raise it once without an API release.
MAX_ATTEMPTS_CEILING = 3

#: When a script sends no `max_attempts` (one from before this field): the
#: script's cap at the time it was written.
DEFAULT_MAX_ATTEMPTS = 2

#: The worker's branch prefix (`GIT_BRANCH_PREFIX`, default `swarm/`), the
#: one scripts/ci-fix.sh's TASK_BRANCH_RE accepts. The comment goes only to a
#: pull request whose head is `swarm/<the continued task>`.
BRANCH_PREFIX = "swarm/"

#: A failed write is retried no sooner than this: the tick runs every minute,
#: and GitHub answering 5xx for one minute usually answers 5xx for the next.
RETRY_SECONDS = 300
#: Twelve failures, five minutes apart: an hour of GitHub or credential
#: trouble. Past that the record says why and the console still shows the
#: outcome; a comment two hours late on a pull request someone has since
#: looked at is noise.
MAX_POSTBACK_FAILURES = 12

#: The agent's account, cut to one paragraph of this many characters.
MAX_OUTCOME_CHARS = 700
MAX_ERROR_CHARS = 400

TASKS = "tasks"

#: One document per failure a PR-side fix claimed (see the module docstring):
#: `{tenant, repository, pull request, head, run}` hashed into the id.
CLAIMS = "ci_fix_claims"
#: Where the claim's id rides on the fix task: `metadata.ci_fix.claim`, the
#: field the liveness read asks for. Never a caller's (`CALLER_FIELDS`).
CLAIM_FIELD = "claim"
#: How long a claim whose task is not written yet counts as live. A
#: submission builds and writes its tasks in well under a second after
#: `stamp`; two minutes covers a slow one, and a submission refused after
#: `stamp` (a grant, the graph check) holds its failure no longer than that.
CLAIM_GRACE_SECONDS = 120

#: The marker `stamp` puts on a fix round's task: `{kind, pull_request,
#: state}`, `state` `pending` until the tick records what it pushed.
FIX_HEAD_FIELD = "pr_fix_head"
#: The record on the task whose `swarm/<id>` branch the pull request is on:
#: a list of `{repository, pull_request, task_id, workflow_id, head, kind,
#: recorded_at}`, oldest first. Restated as
#: `agent_worker.merge.PR_FIX_HEADS_FIELD`, held equal by
#: tests/unit/worker/test_merge_recorded_fix_heads.py.
FIX_HEADS_FIELD = "pull_request_fix_heads"
#: The marker's states after `pending`.
RECORDED = "recorded"
NOTHING_PUSHED = "nothing_pushed"
UNADDRESSABLE = "unaddressable"
#: Entries kept per branch task, newest last. A branch sees at most
#: MAX_ATTEMPTS_CEILING PR-side fixes and the merge step's own rounds
#: (two conflict, a few CI); twenty is all of them with room, and the merge
#: reads no more than `merge.PR_FIX_HEADS_READ` of them.
FIX_HEADS_KEPT = 20
#: The metadata keys that make a `continues_task` workflow a fix round: the
#: PR-side fixer's, and the merge step's CI-fix and conflict rounds
#: (`mergewake._on_red` / `_on_conflict`; `merge_fix_round` is written there
#: as a literal).
ROUND_KINDS = (CI_FIX_METADATA_FIELD, "merge_fix_round", CONFLICT_ROUND_METADATA_KEY)
#: pr-shepherd's conflict fix's mark, `metadata.pr_shepherd` (`{run_id,
#: repository, pull_request, head_sha, base_ref}`). Restated here because
#: `schedtypes.pr_shepherd` imports this module; held equal to
#: `pr_shepherd.METADATA_KEY` by tests/unit/control_plane/test_cifix_conflict_fix_live.py.
CONFLICT_FIX_METADATA_KEY = "pr_shepherd"
#: The marks `live_pr_writer` reads, each a `pull_request` under `metadata.<kind>`.
PR_WRITER_KINDS = ROUND_KINDS + (CONFLICT_FIX_METADATA_KEY,)
#: Keys only this module writes; refused from any caller.
API_KEYS = (FIX_HEAD_FIELD, FIX_HEADS_FIELD)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


def outcome_marker(task_id: str) -> str:
    """The hidden first line of the outcome comment for one fix task.

    Never `<!-- swarm-ci-fix:attempt -->`: that is the one marker the
    script's cap counts, and an outcome is not an attempt.
    """
    if not TASK_ID_RE.match(task_id):
        raise ValueError("a fix task id is task_ and 20 hex digits")
    return f"<!-- swarm-ci-fix:outcome:{task_id} -->"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _repository(url: Any) -> str | None:
    """`owner/repo`, lowercased, of a GitHub repository URL; None otherwise."""
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    if matched is None:
        return None
    return f"{matched.group(1)}/{matched.group(2)}".lower()


class CiFixInFlight(Conflict):
    """409: this failure already has a live CI-fix task; `detail` names it."""

    code = "ci_fix_in_flight"


class CiFixLaneOwnsPr(Conflict):
    """409: the continued task's workflow has a live merge step, which runs
    this pull request's CI-fix rounds itself, or another writer of the pull
    request -- an issue run, a conflict fix, a plain continuation, or another
    CI fix at another head or run (#2763) -- is live; `detail` names it."""

    code = "ci_fix_lane_owns_pr"


class PrFixInFlight(Conflict):
    """409: a plain continuation's pull request has a live CI fix or conflict
    fix pushing to its branch; `detail` names it."""

    code = "pr_fix_in_flight"


# --------------------------------------------------------------------------
# at submission
# --------------------------------------------------------------------------

def _refuse(message: str, **detail: Any) -> DispatchOptionError:
    return DispatchOptionError(message, detail={"metadata": CI_FIX_METADATA_FIELD, **detail})


def _checked_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise _refuse("metadata.ci_fix is an object: pull_request, run_id, head_sha, attempt, max_attempts.")
    unknown = sorted(str(k) for k in record if k not in CALLER_FIELDS)
    if unknown:
        raise _refuse(
            "metadata.ci_fix takes pull_request, run_id, head_sha, attempt and "
            "max_attempts; the post-back's record is the API's to write.",
            keys=unknown,
        )
    pull_request = _positive_int(record.get("pull_request"))
    attempt = _positive_int(record.get("attempt"))
    if pull_request is None or attempt is None:
        raise _refuse("metadata.ci_fix needs pull_request and attempt, each a whole number from 1.")
    max_attempts = record.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if _positive_int(max_attempts) is None or max_attempts > MAX_ATTEMPTS_CEILING:
        raise _refuse(
            f"metadata.ci_fix.max_attempts is 1 to {MAX_ATTEMPTS_CEILING}: past that a "
            "fixer that is not converging only spends quota.",
        )
    if attempt > max_attempts:
        raise _refuse(
            f"metadata.ci_fix.attempt {attempt} is past max_attempts {max_attempts}.",
            attempt=attempt, max_attempts=max_attempts,
        )
    checked: dict[str, Any] = {
        "pull_request": pull_request, "attempt": attempt, "max_attempts": max_attempts,
    }
    if "run_id" in record:
        run_id = _positive_int(record["run_id"])
        if run_id is None:
            raise _refuse("metadata.ci_fix.run_id is the red run's id, a whole number.")
        checked["run_id"] = run_id
    if "head_sha" in record:
        head = record["head_sha"]
        if not isinstance(head, str) or not _SHA.fullmatch(head):
            raise _refuse("metadata.ci_fix.head_sha is the red commit's full 40-hex sha.")
        checked["head_sha"] = head
    return checked


def _attempts_made(db: Any, tenant_id: str, root: str, pull_request: int, ceiling: int) -> int:
    """The tenant's fix tasks already submitted for this pull request of this branch."""
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter(f"metadata.{CI_FIX_METADATA_FIELD}.pull_request", "==", pull_request))
        .where(filter=FieldFilter(f"metadata.{DISPATCH_METADATA_KEY}.continues", "==", root))
        .limit(ceiling + 1)
    )
    workflows = set()
    for snap in query.stream():
        doc = snap.to_dict() or {}
        if doc.get("tenant_id") != tenant_id:
            continue
        # One attempt is one workflow; a workflow's steps share its metadata.
        workflows.add(doc.get("workflow_id") or doc.get("id"))
    return len(workflows)


def _refuse_api_keys(metadata: Mapping[str, Any], **where: Any) -> None:
    sent = [key for key in API_KEYS if key in metadata]
    if sent:
        raise _refuse(
            "metadata.pr_fix_head and metadata.pull_request_fix_heads are the API's "
            "record of what a fix round pushed; a caller never sends them.",
            keys=sent, **where,
        )


def _fix_head_marker(metadata: Mapping[str, Any], continuation: Continuation | None,
                     ci_fix_pull_request: int | None = None) -> dict[str, Any] | None:
    """The `FIX_HEAD_FIELD` marker for a fix round's workflow, or None for any other."""
    if continuation is None:
        return None
    for kind in ROUND_KINDS:
        if kind not in metadata:
            continue
        number = (ci_fix_pull_request if kind == CI_FIX_METADATA_FIELD
                  else _positive_int(_mapping(metadata[kind]).get("pull_request")))
        if number is not None:
            return {"kind": kind, "pull_request": number, "state": PENDING}
    return None


def _claim_key(tenant_id: str, repository: str, record: Mapping[str, Any]) -> str:
    raw = "\n".join([tenant_id, repository, str(record["pull_request"]),
                      str(record["head_sha"]), str(record["run_id"])])
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _live_holder(txn: Any, db: Any, tenant_id: str, held: Mapping[str, Any],
                 now: datetime) -> dict[str, Any] | None:
    """The task holding claim `held` while it is live, as `{task_id, workflow_id}`; else None."""
    holder = held.get("claim_id")
    if not isinstance(holder, str) or not holder:
        return None
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter(f"metadata.{CI_FIX_METADATA_FIELD}.{CLAIM_FIELD}", "==", holder))
        .limit(5)
    )
    terminal = {s.value for s in TERMINAL_STATES}
    found = [snap.to_dict() or {} for snap in txn.get(query)]
    found = [doc for doc in found if doc.get("tenant_id") == tenant_id]
    for doc in found:
        if doc.get("state") not in terminal:
            return {"task_id": doc.get("id"), "workflow_id": doc.get("workflow_id")}
    if found:
        return None
    # No task yet: the claimant is still submitting, or was refused after
    # `stamp`. Live for the grace window only; a future stamp is not honoured.
    claimed = held.get("claimed_at")
    if isinstance(claimed, datetime) and timedelta(0) <= now - claimed < timedelta(
        seconds=CLAIM_GRACE_SECONDS
    ):
        return {"task_id": None, "workflow_id": None}
    return None


def _claim_repository(url: str) -> str:
    """The `repository` a claim is written and read under."""
    return _repository(url) or url.lower()


class _Reads:
    """`_live_holder`'s reads outside a transaction: `get(query)` is the query's own."""

    @staticmethod
    def get(query: Any) -> Any:
        return query.get()


#: The most claims one (pull request, head) is read for: one per red run at
#: that head, and a head is red in a handful of runs at most.
LIVE_CLAIMS_READ = 10


def live_claim(db: Any, tenant_id: str, repository_url: str, pull_request: int,
               head_sha: str, now: datetime) -> dict[str, Any] | None:
    """The live PR-side fix holding a claim on (repository, pull request, head),
    of any run, as `{task_id, workflow_id}`; None when there is none.

    The claim's liveness is `stamp`'s own (`_live_holder`): its task is not
    terminal, or no task is written yet and the claim is inside
    `CLAIM_GRACE_SECONDS`. Read with equality filters on the claim's fields,
    never by rebuilding its id. For the merge step's own CI-fix round
    (`mergewake._on_red`), which must not start a second agent on a failure
    a PR-side fix is already on.
    """
    query = (
        db.collection(CLAIMS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("repository", "==", _claim_repository(repository_url)))
        .where(filter=FieldFilter("pull_request", "==", pull_request))
        .where(filter=FieldFilter("head_sha", "==", head_sha))
        .limit(LIVE_CLAIMS_READ)
    )
    for snap in query.stream():
        held = snap.to_dict() or {}
        if held.get("tenant_id") != tenant_id:
            continue
        live = _live_holder(_Reads(), db, tenant_id, held, now)
        if live is not None:
            return live
    return None


#: The most tasks one (branch task, mark, pull request) is read for. A pull
#: request sees at most MAX_ATTEMPTS_CEILING PR-side fixes, one conflict fix
#: per head and a merge step's few rounds; twenty is all of them with room,
#: and a live one is the newest, so a read cut at twenty still finds it.
LIVE_WRITERS_READ = 20


def live_pr_writer(db: Any, tenant_id: str, root_task_id: str, pull_request: int, *,
                   kinds: tuple[str, ...], now: datetime | None = None) -> dict[str, Any] | None:
    """A live fix that pushes to `pull_request`'s branch -- the `swarm/<root_task_id>`
    branch -- as `{task_id, workflow_id, kind}`; None when there is none.

    ONE WRITER PER PULL REQUEST (#2456). A fix is a non-terminal task of this
    tenant that continues `root_task_id` (`metadata.dispatch.continues`) and
    carries one of `kinds` naming `pull_request`: `metadata.<kind>.pull_request`
    for each of `PR_WRITER_KINDS`. One read per kind, equality filters only
    (tenant, continuation, pull request -- `_attempts_made`'s shape, served by
    the built-in single-field indexes, no composite index), so a branch whose
    finished continuations outnumber the limit still shows its live one. Every
    filter is checked again in the application: a fake or an index mistake
    must not move the tenant boundary (invariant 9).

    For `ci_fix`, a claim whose task is not written yet counts too while it
    is inside `CLAIM_GRACE_SECONDS` (`_live_holder`): a fixer still
    submitting when the shepherd fires. Claims are read by tenant, branch
    task and pull request, of any head and run.
    """
    unknown = [kind for kind in kinds if kind not in PR_WRITER_KINDS]
    if unknown:
        raise ValueError(f"no pull-request writer mark {unknown}")
    terminal = {s.value for s in TERMINAL_STATES}
    for kind in kinds:
        query = (
            db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter(f"metadata.{DISPATCH_METADATA_KEY}.continues", "==", root_task_id))
            .where(filter=FieldFilter(f"metadata.{kind}.pull_request", "==", pull_request))
            .limit(LIVE_WRITERS_READ)
        )
        for snap in query.stream():
            doc = snap.to_dict() or {}
            metadata = _mapping(doc.get("metadata"))
            if (doc.get("tenant_id") == tenant_id
                    and _mapping(metadata.get(DISPATCH_METADATA_KEY)).get("continues") == root_task_id
                    and _mapping(metadata.get(kind)).get("pull_request") == pull_request
                    and doc.get("state") not in terminal):
                return {"task_id": str(doc.get("id") or snap.id),
                        "workflow_id": doc.get("workflow_id"), "kind": kind}
    if CI_FIX_METADATA_FIELD not in kinds:
        return None
    moment = now or datetime.now(timezone.utc)
    claims = (
        db.collection(CLAIMS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("root_task_id", "==", root_task_id))
        .where(filter=FieldFilter("pull_request", "==", pull_request))
        .limit(LIVE_CLAIMS_READ)
    )
    for snap in claims.stream():
        held = snap.to_dict() or {}
        if (held.get("tenant_id") != tenant_id or held.get("root_task_id") != root_task_id
                or held.get("pull_request") != pull_request):
            continue
        live = _live_holder(_Reads(), db, tenant_id, held, moment)
        if live is not None:
            return {**live, "kind": CI_FIX_METADATA_FIELD}
    return None


def _claim(store: Any, tenant_id: str, continuation: Continuation,
           record: Mapping[str, Any]) -> str | None:
    """Take this failure's claim, or refuse `CiFixInFlight` naming the live fix
    (through `refusals.refuse`: switched off, None and no claim is taken).

    None when the record names no head or no run: then nothing says two
    requests are the same failure, and only the per-PR cap applies.
    """
    if "head_sha" not in record or "run_id" not in record:
        return None
    db = store.db
    clock = getattr(store, "_now", None)
    now = clock() if callable(clock) else datetime.now(timezone.utc)
    repository = _claim_repository(continuation.repository_url)
    ref = db.collection(CLAIMS).document(_claim_key(tenant_id, repository, record))
    claim_id = uuid.uuid4().hex
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any] | None:
        snap = _snapshot(txn.get(ref))
        held = snap.to_dict() if snap.exists else None
        if held is not None and held.get("tenant_id") == tenant_id:
            live = _live_holder(txn, db, tenant_id, held, now)
            if live is not None:
                return live
        txn.set(ref, {
            "tenant_id": tenant_id, "repository": repository,
            "pull_request": record["pull_request"], "head_sha": record["head_sha"],
            "run_id": record["run_id"], "root_task_id": continuation.root_task_id,
            "claim_id": claim_id, "claimed_at": now,
        })
        return None

    live = _apply(transaction)
    if live is not None:
        task = live.get("task_id")
        # A new refusal ships report-only (refusals.py): off, the request goes
        # on as it did before the claim existed, holding no claim of its own.
        refuse(CiFixInFlight(
            f"pull request #{record['pull_request']}'s red run {record['run_id']} at "
            f"{str(record['head_sha'])[:12]} already has a live CI-fix task"
            + (f" ({task})" if task else ", still being submitted")
            + "; this request is answered with that one, and no second task is made.",
            detail={"metadata": CI_FIX_METADATA_FIELD, "task_id": task,
                    "workflow_id": live.get("workflow_id"),
                    "pull_request": record["pull_request"], "head_sha": record["head_sha"],
                    "run_id": record["run_id"]},
        ))
        return None
    return claim_id


#: The runner profiles of a merge step (`validation.is_merge_step`): today
#: `merge` alone. Each is one equality read, never a scan of the workflow.
MERGE_PROFILES = tuple(sorted(name for name in RUNNER_PROFILES if is_merge_step(name)))
#: A workflow has one merge step; a few more covers a resubmitted one.
MERGE_STEPS_READ = 5


def _lane_workflow(db: Any, tenant_id: str, continuation: Continuation) -> str:
    """The workflow of the task whose `swarm/<id>` branch the pull request is
    on -- the continuation's root -- or "" for a task in no workflow."""
    if continuation.task_id == continuation.root_task_id or not continuation.task_id:
        return continuation.workflow_id
    snap = db.collection(TASKS).document(continuation.root_task_id).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    if doc.get("tenant_id") != tenant_id:
        return ""
    workflow = doc.get("workflow_id")
    return workflow if isinstance(workflow, str) else ""


def _live_merge_step(db: Any, tenant_id: str, workflow_id: str) -> str | None:
    """The id of `workflow_id`'s merge step while it is not terminal; else None."""
    terminal = {s.value for s in TERMINAL_STATES}
    for profile in MERGE_PROFILES:
        query = (
            db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("workflow_id", "==", workflow_id))
            .where(filter=FieldFilter("runner_profile", "==", profile))
            .limit(MERGE_STEPS_READ)
        )
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if (doc.get("tenant_id") == tenant_id and doc.get("workflow_id") == workflow_id
                    and is_merge_step(doc.get("runner_profile"))
                    and doc.get("state") not in terminal):
                return str(doc.get("id") or snap.id)
    return None


#: A run has one pull request, and a task is the `pr_task_id` of one run; a
#: few more covers a re-run of the same issue that adopted the same task.
ISSUE_RUNS_READ = 5


def _live_issue_run(db: Any, tenant_id: str, continuation: Continuation,
                    workflow_id: str) -> IssueRun | None:
    """The tenant's non-terminal issue run that owns the pull request on the
    continuation's root -- its `pr_task_id` is the root, or the root's
    workflow (`workflow_id`, "" for none) is one of its `ci_fix_workflows`
    -- or None.

    Equality and `array_contains` filters only, so Firestore serves both
    queries by merging the automatic single-field indexes: no composite
    index, and nothing to fail while one builds after a release. Tenant and
    state are checked again in the application, as `IssueRuns.live` does.
    """
    runs = db.collection(RUNS_COLLECTION).where(filter=FieldFilter("tenant_id", "==", tenant_id))
    queries = [runs.where(filter=FieldFilter("pr_task_id", "==", continuation.root_task_id))]
    if workflow_id:
        queries.append(runs.where(filter=FieldFilter("ci_fix_workflows", "array_contains", workflow_id)))
    for query in queries:
        for snap in query.limit(ISSUE_RUNS_READ).stream():
            data = snap.to_dict() or {}
            try:
                run = IssueRun.from_firestore(data)
            except (KeyError, TypeError, ValueError):
                log.warning("issue run %s is unreadable; not counted as owning a pull request",
                            data.get("id") or snap.id)
                continue
            if run.tenant_id != tenant_id or run.state in TERMINAL_RUN_STATES:
                continue
            if run.pr_task_id == continuation.root_task_id or (
                    workflow_id and workflow_id in run.ci_fix_workflows):
                return run
    return None


def _refuse_if_lane_owns(store: Any, tenant_id: str, continuation: Continuation,
                         record: Mapping[str, Any]) -> None:
    """Refuse `CiFixLaneOwnsPr` while the lane's merge step is live, or while
    a non-terminal issue run owns the pull request (through
    `refusals.refuse`: switched off, the request goes on as before).

    ONE FIXER PER PULL REQUEST (CI-FIXER-GATE, part of #1822). A lane whose
    workflow has a merge step runs its own CI-fix rounds on its pull request
    (`mergewake._on_red`). In 72 h to 2026-10-21 the standalone fixer
    (scripts/ci-fix.sh) ran 91 times beside them and failed 34, 22 on a push
    rejected because the lane's round had moved the branch first. The script
    cannot see the merge step -- its continuation-scoped identity reads only
    what it submitted (`deps.submission_scope`) -- so the API decides here,
    with its own reads, and the fixer gains none. Every non-terminal state
    is live: a merge step QUEUED or PARKED on its CI wait is the one whose
    round starts next. A task in no workflow, a workflow with no merge step
    and a merge step that has finished are fixed as before.

    AN ISSUE RUN OWNS ITS PULL REQUEST TOO (#2216). An issue run's compiled
    workflow has no merge step: the run reads CI and submits its own CI-fix
    rounds from `issueci` while CHECKING/FIXING, so the merge-step read never
    fired for its pull request. Measured 2026-10-23: on #2145 the PR-side
    fixer task_f08e3de5 ran 30 minutes on 2a34c62 and was rejected
    non-fast-forward, the run's round task_7313192a having pushed first; on
    #2147 task_75ae734c started 64 s after the run's round task_2b64bf8e and
    ended published_nothing while that round fixed the failure. So a
    non-terminal issue run of this tenant whose `pr_task_id` is the root, or
    one of whose `ci_fix_workflows` is the root's workflow, refuses too --
    every non-terminal state, since an APPROVED or RUNNING run reaches
    CHECKING next. The run's own rounds carry `metadata.issue_run`, never
    `metadata.ci_fix`, so they never reach this gate. A terminal run's pull
    request is fixed as before.
    """
    workflow = _lane_workflow(store.db, tenant_id, continuation)
    merge_task = _live_merge_step(store.db, tenant_id, workflow) if workflow else None
    if merge_task is not None:
        refuse(CiFixLaneOwnsPr(
            f"pull request #{record['pull_request']} belongs to a lane whose merge step "
            f"({merge_task}) is live and runs its CI-fix rounds itself; no standalone fix is made.",
            detail={"metadata": CI_FIX_METADATA_FIELD, "task_id": merge_task,
                    "workflow_id": workflow, "pull_request": record["pull_request"]},
        ))
        return
    run = _live_issue_run(store.db, tenant_id, continuation, workflow)
    if run is None:
        return
    refuse(CiFixLaneOwnsPr(
        f"pull request #{record['pull_request']} belongs to issue run {run.id} "
        f"({run.state.value}), which runs its own CI-fix rounds; no standalone fix is made.",
        detail={"metadata": CI_FIX_METADATA_FIELD, "issue_run_id": run.id,
                "issue_run_state": run.state.value, "pull_request": record["pull_request"],
                "task_id": continuation.root_task_id},
    ))


def _refuse_if_conflict_fix_live(store: Any, tenant_id: str, continuation: Continuation,
                                 record: Mapping[str, Any]) -> None:
    """Refuse `CiFixLaneOwnsPr` while a conflict fix on this pull request is live
    (through `refusals.refuse`: switched off, the request goes on as before).

    ONE WRITER PER PULL REQUEST (#2456). Measured 2026-10-26 on PR #2381:
    pr-shepherd's conflict fix (task_2239a84774bf4bceaeb5, wf_eb9eb92c,
    01:23Z) and a scripts/ci-fix.sh fix (task_2f5b70aa, wf_8bed3d10, 01:21Z)
    both cloned 15cc7ea; the CI fix pushed 9c722e47 first and the conflict
    fix's push was rejected non-fast-forward. A conflict fix -- pr-shepherd's
    (`CONFLICT_FIX_METADATA_KEY`) or a merge step's conflict round -- merges
    the base into the same branch, so the CI fix waits for it: CI runs again
    at the head it pushes, and a red run there gets its own fix. The existing
    refusal, because it is the same rule and scripts/ci-fix.sh already reads
    it as a skip with no comment; `conflict_fix` in the detail says which.
    """
    writer = live_pr_writer(
        store.db, tenant_id, continuation.root_task_id, record["pull_request"],
        kinds=(CONFLICT_FIX_METADATA_KEY, CONFLICT_ROUND_METADATA_KEY),
    )
    if writer is None:
        return
    refuse(CiFixLaneOwnsPr(
        f"pull request #{record['pull_request']} has a live conflict fix ({writer['task_id']}) "
        "merging its base into the branch; no CI fix is made beside it.",
        detail={"metadata": CI_FIX_METADATA_FIELD, "task_id": writer["task_id"],
                "workflow_id": writer["workflow_id"], "pull_request": record["pull_request"],
                "conflict_fix": True},
    ))


def _same_failure(other: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """Whether `other` (a live fix's `metadata.ci_fix`, or its claim) is on the
    failure `record` names: `record` carries both a head and a run and `other`
    has the same two. A record that lacks either names no failure `_claim`
    can answer (it takes no claim), so it is never the live fix's own failure
    and `_refuse_if_ci_fix_live` refuses it beside any live fix (#2763)."""
    keys = ("head_sha", "run_id")
    return (all(key in record for key in keys)
            and all(other.get(key) == record[key] for key in keys))


def _refuse_if_ci_fix_live(store: Any, tenant_id: str, continuation: Continuation,
                           record: Mapping[str, Any]) -> None:
    """Refuse `CiFixLaneOwnsPr` while another CI fix of this pull request is
    live, at any head and from any run (through `refusals.refuse`: switched
    off, the request goes on as before).

    ONE WRITER PER PULL REQUEST, CI FIX BESIDE CI FIX (#2763). `_claim` keys
    a failure by (tenant, repository, pull request, head, run), so another
    head or another run was another failure and got a second fix beside the
    first. Measured on PR #2730: scripts/ci-fix.sh's attempt 1
    (task_f11210cf) was RUNNING on bb31584 when attempt 2 (task_af11c3ba) was
    admitted at 62a6fbac; attempt 2 pushed 9e193de0 first and attempt 1's
    publish conflicted. So the second fix waits: the live fix's push runs CI
    again at its head, and a red run there gets its own fix. Two reads,
    equality filters only: non-terminal tasks of the tenant continuing the
    branch task with `metadata.ci_fix.pull_request` this one
    (`live_pr_writer`'s read), and claims by (tenant, branch task, pull
    request) through `_live_holder`, so a racing fix whose task is not written
    yet counts while inside `CLAIM_GRACE_SECONDS`. Every filter is checked
    again in the application (invariant 9). A live fix of the very same
    failure is passed over: `_claim` answers that one `ci_fix_in_flight`, as
    before. The refusal is `ci_fix_lane_owns_pr`, which ships on and
    scripts/ci-fix.sh reads as a skip, not `ci_fix_in_flight`, which ships
    report-only and is off in production, where it would refuse nothing;
    `ci_fix` in the detail says which.
    """
    db = store.db
    root = continuation.root_task_id
    pull_request = record["pull_request"]
    terminal = {s.value for s in TERMINAL_STATES}
    live: dict[str, Any] | None = None
    tasks = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter(f"metadata.{DISPATCH_METADATA_KEY}.continues", "==", root))
        .where(filter=FieldFilter(f"metadata.{CI_FIX_METADATA_FIELD}.pull_request", "==", pull_request))
        .limit(LIVE_WRITERS_READ)
    )
    for snap in tasks.stream():
        doc = snap.to_dict() or {}
        metadata = _mapping(doc.get("metadata"))
        fix = _mapping(metadata.get(CI_FIX_METADATA_FIELD))
        if (doc.get("tenant_id") != tenant_id
                or _mapping(metadata.get(DISPATCH_METADATA_KEY)).get("continues") != root
                or fix.get("pull_request") != pull_request
                or doc.get("state") in terminal or _same_failure(fix, record)):
            continue
        live = {"task_id": str(doc.get("id") or snap.id), "workflow_id": doc.get("workflow_id"),
                "head_sha": fix.get("head_sha")}
        break
    if live is None:
        clock = getattr(store, "_now", None)
        now = clock() if callable(clock) else datetime.now(timezone.utc)
        claims = (
            db.collection(CLAIMS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("root_task_id", "==", root))
            .where(filter=FieldFilter("pull_request", "==", pull_request))
            .limit(LIVE_CLAIMS_READ)
        )
        for snap in claims.stream():
            held = snap.to_dict() or {}
            if (held.get("tenant_id") != tenant_id or held.get("root_task_id") != root
                    or held.get("pull_request") != pull_request or _same_failure(held, record)):
                continue
            holder = _live_holder(_Reads(), db, tenant_id, held, now)
            if holder is not None:
                live = {**holder, "head_sha": held.get("head_sha")}
                break
    if live is None:
        return
    task = live["task_id"]
    head = live["head_sha"]
    refuse(CiFixLaneOwnsPr(
        f"pull request #{pull_request} has a live CI fix"
        + (f" ({task})" if task else ", still being submitted,")
        + (f" at {str(head)[:12]}" if head else "")
        + " pushing to its branch; no second fix is made beside it, and CI runs again "
        "at the head that fix pushes.",
        detail={"metadata": CI_FIX_METADATA_FIELD, "task_id": task,
                "workflow_id": live["workflow_id"], "pull_request": pull_request,
                "head_sha": head if isinstance(head, str) else None, "ci_fix": True},
    ))


#: A plain continuation's own issue-run mark (`issueci`, `issueruns` write it
#: as a literal): the run's rounds have the issue run's gate, not this one.
ISSUE_RUN_METADATA_KEY = "issue_run"
#: The most tasks one (tenant, branch task) is read for by each of
#: `live_plain_continuation`'s two reads. Unordered, so a live row must not
#: hide behind finished ones: a branch task is continued by at most
#: MAX_ATTEMPTS_CEILING PR-side fixes, its merge step's few rounds, one
#: conflict fix per head and the lanes its owner points at it, and fifty --
#: `PUBLISH_WRITERS_READ`'s room -- is all of them with room.
CONTINUATIONS_READ = 50


def live_plain_continuation(db: Any, tenant_id: str, root_task_id: str) -> dict[str, Any] | None:
    """A live plain continuation that pushes to the `swarm/<root_task_id>`
    branch, as `{task_id, workflow_id, via}`; None when there is none.

    A plain continuation is a non-terminal task of this tenant whose
    `metadata.dispatch.continues` is the root (`via: "continues"`) or whose
    signed `metadata.dispatch.publish_to_pr.task_id` is (`via:
    "publish_to_pr"`), that is not a merge step (it pushes nothing) and
    carries none of `PR_WRITER_KINDS` nor `metadata.issue_run`: each of those
    writers has its own gate, and `live_pr_writer` is theirs. Two reads,
    equality filters only (tenant, and the dispatch field -- the built-in
    single-field indexes, no composite index), each held to
    `CONTINUATIONS_READ`. Every filter is checked again in the application: a
    fake or an index mistake must not move the tenant boundary (invariant 9).
    """
    terminal = {s.value for s in TERMINAL_STATES}
    dispatch_path = f"metadata.{DISPATCH_METADATA_KEY}"
    reads = (
        ("continues", f"{dispatch_path}.continues"),
        (publishtopr.DISPATCH_FIELD, f"{dispatch_path}.{publishtopr.DISPATCH_FIELD}.task_id"),
    )
    for via, path in reads:
        query = (
            db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter(path, "==", root_task_id))
            .limit(CONTINUATIONS_READ)
        )
        for snap in query.stream():
            doc = snap.to_dict() or {}
            metadata = _mapping(doc.get("metadata"))
            dispatch = _mapping(metadata.get(DISPATCH_METADATA_KEY))
            named = (dispatch.get("continues") if via == "continues"
                     else _mapping(dispatch.get(publishtopr.DISPATCH_FIELD)).get("task_id"))
            if (doc.get("tenant_id") != tenant_id or named != root_task_id
                    or doc.get("state") in terminal
                    or is_merge_step(doc.get("runner_profile"))
                    or any(kind in metadata for kind in PR_WRITER_KINDS)
                    or ISSUE_RUN_METADATA_KEY in metadata):
                continue
            return {"task_id": str(doc.get("id") or snap.id),
                    "workflow_id": doc.get("workflow_id"), "via": via}
    return None


def _refuse_if_continuation_live(store: Any, tenant_id: str, continuation: Continuation,
                                 record: Mapping[str, Any]) -> None:
    """Refuse `CiFixLaneOwnsPr` while a plain continuation of the lane pushes
    to this pull request's branch (through `refusals.refuse`: switched off,
    the request goes on as before).

    ONE WRITER PER PULL REQUEST, THE OTHER ORDER (#2583). #2535 holds a plain
    continuation submitted while a fix is live (`_refuse_if_fix_live`); a fix
    submitted while a plain continuation is live went through. Measured
    2026-10-27 on PR #2570: the CI fixer's attempt 2 (task_7d270954,
    wf_68cfa3f0) was admitted at 16:26:01Z, two minutes after the `implement`
    step task_8017fba2 (wf_1a460a31, 16:24:06Z) started on the same red head
    7e55a8d; both fixed the same test, the fixer pushed f4f93dc first, and the
    step's 88-minute publish was refused `head_moved`. So the CI fix waits:
    the lane's push moves the head, CI runs again there, and a red run at
    that head gets its own fix. A continuation carrying a writer's own mark
    (`PR_WRITER_KINDS`, `metadata.issue_run`) is left to that writer's gate
    -- the merge-step, issue-run and conflict-fix reads above, and `_claim`
    -- so this read neither duplicates nor widens them. The existing refusal,
    because it is the same rule and scripts/ci-fix.sh already reads it as a
    skip with no comment; `plain_continuation` in the detail says which.
    """
    lane = live_plain_continuation(store.db, tenant_id, continuation.root_task_id)
    if lane is None:
        return
    refuse(CiFixLaneOwnsPr(
        f"pull request #{record['pull_request']} has a live continuation of its lane "
        f"({lane['task_id']}) pushing to the same branch; the CI fix waits for it, and CI "
        "runs again at the head it pushes. No CI fix is made beside it.",
        detail={"metadata": CI_FIX_METADATA_FIELD, "task_id": lane["task_id"],
                "workflow_id": lane["workflow_id"], "pull_request": record["pull_request"],
                "plain_continuation": True},
    ))


def _branch_pull_request(db: Any, tenant_id: str, continuation: Continuation) -> int | None:
    """The number of the pull request the continuation's root opened, from its
    own record (`result_summary.git.pull_request.number`, the worker's), read
    by id under the tenant; None when it recorded none, is another tenant's,
    or the number is not a positive int. `mergewake._held_pull_request`'s
    read, restated: mergewake imports this module."""
    snap = db.collection(TASKS).document(continuation.root_task_id).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    if doc.get("tenant_id") != tenant_id:
        return None
    git = _mapping(_mapping(doc.get("result_summary")).get("git"))
    return _positive_int(_mapping(git.get("pull_request")).get("number"))


#: The most tasks one (tenant, mark, pull request number) is read for when
#: the branch task is not known yet (`publish_to_pr`, below): the number
#: alone, of every repository the tenant fixes. Each repository's pull
#: request sees `LIVE_WRITERS_READ` at most; fifty is a few repositories'
#: worth, and every row is checked against the spec's repository in the
#: application.
PUBLISH_WRITERS_READ = 50


def _live_writer_on_pull(db: Any, tenant_id: str, repository_url: str, pull_request: int,
                         now: datetime) -> dict[str, Any] | None:
    """`live_pr_writer`'s answer for a pull request named by number and
    repository, before its branch task is known: a non-terminal task of this
    tenant with any `PR_WRITER_KINDS` mark naming `pull_request` in
    `repository_url`, or a live claim on it; else None.

    For a `publish_to_pr` workflow, whose branch task `service` reads from
    GitHub only after `stamp`. Equality filters on tenant and the mark's
    number (the built-in single-field indexes, no composite index); the
    repository and every filter are checked again here (invariant 9).
    """
    terminal = {s.value for s in TERMINAL_STATES}
    repository = _claim_repository(repository_url)
    for kind in PR_WRITER_KINDS:
        query = (
            db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter(f"metadata.{kind}.pull_request", "==", pull_request))
            .limit(PUBLISH_WRITERS_READ)
        )
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if (doc.get("tenant_id") == tenant_id
                    and _mapping(_mapping(doc.get("metadata")).get(kind)).get("pull_request")
                    == pull_request
                    and isinstance(doc.get("repository_url"), str)
                    and _claim_repository(doc["repository_url"]) == repository
                    and doc.get("state") not in terminal):
                return {"task_id": str(doc.get("id") or snap.id),
                        "workflow_id": doc.get("workflow_id"), "kind": kind}
    claims = (
        db.collection(CLAIMS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("repository", "==", repository))
        .where(filter=FieldFilter("pull_request", "==", pull_request))
        .limit(LIVE_CLAIMS_READ)
    )
    for snap in claims.stream():
        held = snap.to_dict() or {}
        if (held.get("tenant_id") != tenant_id or held.get("repository") != repository
                or held.get("pull_request") != pull_request):
            continue
        live = _live_holder(_Reads(), db, tenant_id, held, now)
        if live is not None:
            return {**live, "kind": CI_FIX_METADATA_FIELD}
    return None


def _refuse_if_fix_live(store: Any, tenant_id: str, continuation: Continuation | None,
                        spec: WorkflowCreate) -> None:
    """Refuse `PrFixInFlight` while a CI fix or conflict fix pushes to the
    branch this plain continuation -- or this `publish_to_pr` workflow --
    would push to (through `refusals.refuse`: switched off, the request goes
    on as before).

    ONE WRITER PER PULL REQUEST, for every pushing request (#2535). Measured
    2026-10-26 on PR #2473: wf_3bde5fa9's `implement` step (19:23:00Z, with
    `publish_to_pr` 2473 onto task_577523f9's branch) was admitted three
    seconds after the CI fixer task_b0abb81d (wf_cd10182a) started on the
    same red head 377c3010; both fixed one test, the fixer pushed 6260cff5
    first, and the step's publish was refused `head_moved`. A `continues_task`
    request pushes to the same branch the same way. A merge-only request
    pushes nothing, and a request carrying a writer's own mark has that
    writer's gate (`_claim` and the two above for `ci_fix`,
    `mergewake.live_claim` for a merge step's rounds, `live_pr_writer` in
    pr-shepherd's `_plan_one`), so neither is read here. A continuation whose
    root recorded no pull request has none to hold.
    """
    if all(is_merge_step(step.runner_profile) for step in spec.steps):
        return
    if any(kind in spec.metadata for kind in PR_WRITER_KINDS):
        return
    clock = getattr(store, "_now", None)
    now = clock() if callable(clock) else datetime.now(timezone.utc)
    if continuation is not None:
        number = _branch_pull_request(store.db, tenant_id, continuation)
        if number is None:
            return
        writer = live_pr_writer(store.db, tenant_id, continuation.root_task_id, number,
                                kinds=PR_WRITER_KINDS, now=now)
    else:
        # `publish_to_pr` (#2183) beside `continues_task` is refused before
        # `stamp`; a malformed one is refused by `service` after it.
        try:
            number = publishtopr.parse_number(spec.publish_to_pr, spec.repository_url)
        except DispatchOptionError:
            return
        if number is None or not spec.repository_url:
            return
        writer = _live_writer_on_pull(store.db, tenant_id, spec.repository_url, number, now)
    if writer is None:
        return
    task = writer.get("task_id")
    refuse(PrFixInFlight(
        f"pull request #{number} has a live {writer['kind']} fix "
        + (f"({task})" if task else "still being submitted")
        + " pushing to its branch; this workflow was not made. Resubmit it once "
        "that fix ends: its push moves the head, and CI runs again there.",
        detail={"task_id": task, "workflow_id": writer.get("workflow_id"),
                "kind": writer["kind"], "pull_request": number},
    ))


def _refuse_if_lane_merge_live(store: Any, tenant_id: str, continuation: Continuation | None,
                               spec: WorkflowCreate) -> None:
    """Refuse `CiFixLaneOwnsPr` while the lane this plain continuation pushes
    to has a live merge step (through `refusals.refuse`: switched off, the
    request goes on as before).

    ONE WRITER PER PULL REQUEST, THE MERGE STEP'S SIDE (#2685). The merge-step
    read (`_refuse_if_lane_owns`) held only a `metadata.ci_fix` request.
    Measured 2026-10-28 on PR #2617: the plain lane task_7ff30c32
    (wf_85f651ab, 'CIFIX-2617') was admitted while the lane's merge step
    task_943ec5f2 was live; the merge step then submitted its ci-fix:1 round
    task_99b8e1e0 beside it, the plain lane pushed 5bc405a first, the round's
    push was rejected non-fast-forward (f-046), and the merge step refused
    `head_moved` at a head none of its rounds had pushed (f-045). So a plain
    continuation waits while the merge step is in any non-terminal state, as
    a standalone fix does. A merge-only request pushes nothing, and a request
    carrying a writer's own mark (`PR_WRITER_KINDS`, `metadata.issue_run`) --
    the merge step's own CI, conflict and pick rounds among them -- has that
    writer's gate, so none is read here. A `publish_to_pr` workflow's branch
    task is known only after `stamp`, so it is not read here either.
    """
    if continuation is None:
        return
    if all(is_merge_step(step.runner_profile) for step in spec.steps):
        return
    if (any(kind in spec.metadata for kind in PR_WRITER_KINDS)
            or ISSUE_RUN_METADATA_KEY in spec.metadata):
        return
    workflow = _lane_workflow(store.db, tenant_id, continuation)
    merge_task = _live_merge_step(store.db, tenant_id, workflow) if workflow else None
    if merge_task is None:
        return
    number = _branch_pull_request(store.db, tenant_id, continuation)
    pull = f"pull request #{number}" if number is not None else "this pull request"
    detail: dict[str, Any] = {"task_id": merge_task, "workflow_id": workflow,
                              "root_task_id": continuation.root_task_id,
                              "plain_continuation": True}
    if number is not None:
        detail["pull_request"] = number
    refuse(CiFixLaneOwnsPr(
        f"{pull}'s lane has a live merge step ({merge_task}) that runs its own fix rounds "
        "on the branch; this continuation was not made. Resubmit it once that merge step "
        "ends or is cancelled.",
        detail=detail,
    ))


#: The fix rounds that run with their lane's territory (module docstring):
#: the PR-side fixer's and the merge step's CI-fix round, never a conflict round.
TERRITORY_ROUND_KINDS = (CI_FIX_METADATA_FIELD, "merge_fix_round")


def lane_territory(db: Any, tenant_id: str, continuation: Continuation) -> list[str] | None:
    """The entries the lane's own task record declares in `inputs.territory`
    -- the continuation's root, the task whose `swarm/<id>` branch the pull
    request is on -- or None when it declares none, is another tenant's, or
    is malformed (the API has refused a malformed one at submit since
    TERRITORY-WARN; a record from before is read as none, and logged)."""
    snap = db.collection(TASKS).document(continuation.root_task_id).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    if doc.get("tenant_id") != tenant_id:
        return None
    task_input = doc.get("input")
    if not isinstance(task_input, Mapping) or laneterritory.TERRITORY_INPUT_KEY not in task_input:
        return None
    raw = task_input[laneterritory.TERRITORY_INPUT_KEY]
    try:
        laneterritory.parse(raw)
    except laneterritory.TerritoryError as exc:
        log.warning("lane %s declares a malformed territory; its fix round runs without one: %s",
                    continuation.root_task_id, str(exc)[:200])
        return None
    return list(raw)


def _with_lane_territory(store: Any, tenant_id: str, spec: WorkflowCreate,
                         continuation: Continuation | None) -> WorkflowCreate:
    """`spec` with its lane's territory on every pushing step and named in its
    prompt, for a `TERRITORY_ROUND_KINDS` round; else unchanged."""
    if continuation is None or not any(kind in spec.metadata for kind in TERRITORY_ROUND_KINDS):
        return spec
    entries = lane_territory(store.db, tenant_id, continuation)
    if entries is None:
        return spec
    brief = laneterritory.round_brief(entries)
    steps = []
    for step in spec.steps:
        if is_merge_step(step.runner_profile):
            steps.append(step)
            continue
        # The lane's, over any the caller sent: a round never widens it.
        given = {**step.input, laneterritory.TERRITORY_INPUT_KEY: list(entries)}
        prompt = given.get(PROMPT_INPUT_KEY)
        if isinstance(prompt, str):
            given[PROMPT_INPUT_KEY] = f"{prompt.rstrip()}\n\n{brief}\n"
        steps.append(step.model_copy(update={"input": given}))
    return spec.model_copy(update={"steps": steps})


def stamp(store: Any, tenant_id: str, spec: WorkflowCreate,
          continuation: Continuation | None) -> WorkflowCreate:
    """`spec` with `metadata.ci_fix` checked, counted, claimed and stamped `pending`,
    any fix round marked for the head record (`FIX_HEAD_FIELD`), and a CI-fix
    round given its lane's territory (`_with_lane_territory`).

    Unchanged when there is neither. Raises `DispatchOptionError` (422
    `invalid_dispatch`) for every refusal, before anything is written,
    `CiFixLaneOwnsPr` (409) while the continued task's workflow has a live
    merge step, a non-terminal issue run of the tenant owns the pull request
    (`_refuse_if_lane_owns`), a conflict fix on the pull request is live (on
    2026-10-26 PR #2381 got both a conflict fix and a CI fix at 15cc7ea, and
    the second push was rejected) or a plain continuation of the lane pushes
    to its branch (`_refuse_if_continuation_live`: on 2026-10-27 PR #2570's
    fixer was admitted two minutes after a `publish_to_pr` implement step
    started at 7e55a8d, and the step's publish was refused `head_moved`) or
    another CI fix of the pull request is live at another head or run
    (`_refuse_if_ci_fix_live`: on PR #2730 attempt 2 was admitted at 62a6fbac
    beside attempt 1 RUNNING on bb31584, and attempt 1's publish conflicted), and
    `CiFixInFlight` (409) when the failure
    already has a live fix. A continuation or `publish_to_pr` workflow with no
    `metadata.ci_fix` that pushes and carries no writer mark of its own is
    refused `PrFixInFlight` (409) while a CI fix or conflict fix on its pull
    request is live (on 2026-10-26 PR #2473 got a CI fix and a
    `publish_to_pr` step at 377c3010, and the second publish was refused
    `head_moved`), and such a continuation is refused `CiFixLaneOwnsPr` (409,
    `plain_continuation`) while its lane's merge step is live
    (`_refuse_if_lane_merge_live`: on 2026-10-28 PR #2617's plain lane
    task_7ff30c32 pushed 5bc405a beside merge step task_943ec5f2, whose
    ci-fix:1 round was rejected non-fast-forward and which refused
    `head_moved`).
    """
    for step in spec.steps:
        if CI_FIX_METADATA_FIELD in (step.metadata or {}):
            raise _refuse(
                "metadata.ci_fix belongs to the workflow, not a step: it names the "
                "pull request the one fix step pushes to.",
                step_id=step.step_id,
            )
        _refuse_api_keys(step.metadata or {}, step_id=step.step_id)
    _refuse_api_keys(spec.metadata)
    if CI_FIX_METADATA_FIELD not in spec.metadata:
        # First, so a refused request writes nothing (#2535).
        _refuse_if_fix_live(store, tenant_id, continuation, spec)
        # And the lane's live merge step, which runs its own rounds (#2685).
        _refuse_if_lane_merge_live(store, tenant_id, continuation, spec)
        marker = _fix_head_marker(spec.metadata, continuation)
        if marker is None:
            return spec
        spec = _with_lane_territory(store, tenant_id, spec, continuation)
        return spec.model_copy(update={"metadata": {**spec.metadata, FIX_HEAD_FIELD: marker}})
    if continuation is None:
        raise _refuse(
            "metadata.ci_fix records a fix for a red pull request's branch, which "
            "only a workflow with continues_task pushes to.",
        )
    record = _checked_record(spec.metadata[CI_FIX_METADATA_FIELD])
    # Before the cap and the claim: a lane's live merge step or a live issue
    # run is the reason, whatever the count, and a refused request holds no claim.
    _refuse_if_lane_owns(store, tenant_id, continuation, record)
    # The same place, the same reason: a live conflict fix on this pull request (#2456).
    _refuse_if_conflict_fix_live(store, tenant_id, continuation, record)
    # And a live plain continuation of the lane pushing to the branch (#2583).
    _refuse_if_continuation_live(store, tenant_id, continuation, record)
    # And another live CI fix of the pull request, at any head or run (#2763).
    _refuse_if_ci_fix_live(store, tenant_id, continuation, record)
    made = _attempts_made(
        store.db, tenant_id, continuation.root_task_id, record["pull_request"], MAX_ATTEMPTS_CEILING,
    )
    if made >= record["max_attempts"]:
        raise _refuse(
            f"pull request #{record['pull_request']} has had {made} fix attempt(s); the cap "
            f"is {record['max_attempts']}. A person has to look at it now.",
            attempts=made, max_attempts=record["max_attempts"],
        )
    # After the cap, so a request the cap refuses holds no claim.
    claim = _claim(store, tenant_id, continuation, record)
    if claim is not None:
        record[CLAIM_FIELD] = claim
    record[POSTBACK_FIELD] = PENDING
    metadata = {**spec.metadata, CI_FIX_METADATA_FIELD: record}
    marker = _fix_head_marker(metadata, continuation, record["pull_request"])
    if marker is not None:
        metadata[FIX_HEAD_FIELD] = marker
    spec = _with_lane_territory(store, tenant_id, spec, continuation)
    return spec.model_copy(update={"metadata": metadata})


# --------------------------------------------------------------------------
# the comment
# --------------------------------------------------------------------------

def _paragraph(text: Any, limit: int, literals: tuple[str, ...]) -> str:
    """The first paragraph of `text`, neutralised, as prose, on one line."""
    if not isinstance(text, str) or not text.strip():
        return ""
    first = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    return prose(first, limit, literals=literals, one_line=True)


def _what_happened(doc: Mapping[str, Any], branch: str, literals: tuple[str, ...]) -> str:
    state = str(doc.get("state") or "")
    summary = _mapping(doc.get("result_summary"))
    git = _mapping(summary.get("git"))
    pushed = git.get("pushed_head")
    if state == "SUCCEEDED":
        if isinstance(pushed, str) and _SHA.fullmatch(pushed):
            return (
                f"The fix was pushed to `{branch}` as `{pushed[:12]}`; CI runs again on it, "
                "and this pull request's checks say whether it worked."
            )
        if summary.get(NO_CHANGE_MARKER) is True or isinstance(summary.get(SKIPPED_SUMMARY_KEY), Mapping):
            return (
                "The step changed nothing, so nothing was pushed: the agent found no change "
                "on this branch that fixes the failure."
            )
        return f"The step succeeded and the console records no push to `{branch}`."
    if state == "CANCELLED":
        return "The step was cancelled; nothing it did was pushed."
    cause = doc.get("end_cause")
    cause_text = f" ({code(str(cause), 80, literals=literals)})" if cause else ""
    error = _paragraph(doc.get("last_error"), MAX_ERROR_CHARS, literals)
    return f"The step ended {state}{cause_text}; nothing it did was pushed." + (
        f" Its error: {error}" if error else ""
    )


def render_outcome(doc: Mapping[str, Any], record: Mapping[str, Any], root: str, *,
                   console_origin: str | None, literals: tuple[str, ...] = ()) -> str:
    """The outcome comment for one terminal fix task."""
    task_id = str(doc.get("id") or "")
    state = str(doc.get("state") or "")
    attempt = _positive_int(record.get("attempt")) or 1
    cap = _positive_int(record.get("max_attempts")) or DEFAULT_MAX_ATTEMPTS
    branch = f"{BRANCH_PREFIX}{root}"
    lines = [
        outcome_marker(task_id),
        f"**CI fixer: attempt {attempt} of {cap} ended {state}.**",
        "",
        f"- fix task: `{task_id}`",
    ]
    console = agent_console_url(console_origin, task_id)
    if console:
        lines.append(f"- console: {console}")
    lines.append(f"- state: `{state}`")
    lines.append("")
    paragraph = _what_happened(doc, branch, literals)
    account = _paragraph(
        _mapping(_mapping(doc.get("result_summary")).get("runner")).get("summary"),
        MAX_OUTCOME_CHARS, literals,
    )
    if account:
        paragraph += f" The agent's account: {account}"
    lines.append(paragraph)
    lines.append("")
    if attempt >= cap:
        lines.append(
            f"This was the last attempt: the cap of {cap} attempts per pull request is "
            "reached, and no further attempt will be made. If CI is red again, a person "
            "has to look."
        )
    else:
        lines.append(f"If CI is red again, the fixer makes attempt {attempt + 1} of {cap}.")
    return bounded("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------

@dataclass
class PostbackReport:
    """One tick over one tenant's pending post-backs."""

    posted: int = 0
    waiting: int = 0
    skipped: int = 0
    failed: int = 0
    truncated: bool = False
    #: Fix rounds whose pushed head this tick recorded against their pull request.
    heads_recorded: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "posted": self.posted,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "failed": self.failed,
            "truncated": self.truncated,
            "heads_recorded": self.heads_recorded,
        }

    def fail(self, task_id: str, code: str) -> None:
        self.failed += 1
        self.failures.append({"task_id": task_id, "code": code})


class _GiveUp(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _target(doc: Mapping[str, Any], record: Mapping[str, Any]) -> tuple[IssueRef, str]:
    """(the pull request, the continued task) the fix was for, or `_GiveUp`."""
    root = _mapping(_mapping(doc.get("metadata")).get(DISPATCH_METADATA_KEY)).get("continues")
    if not isinstance(root, str) or not TASK_ID_RE.match(root):
        raise _GiveUp("not_a_continuation")
    number = _positive_int(record.get("pull_request"))
    url = doc.get("repository_url")
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    if number is None or matched is None:
        raise _GiveUp("postback_unaddressable")
    try:
        owner, repo = parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        raise _GiveUp("postback_unaddressable") from None
    return IssueRef(owner=owner, repo=repo, number=number), root


def _due(record: Mapping[str, Any], now: datetime) -> bool:
    """No failed write in the last RETRY_SECONDS. A future stamp is ignored:
    the document is tenant-writable, and that would stop the retries."""
    last = record.get("postback_checked_at")
    if not isinstance(last, datetime) or last > now:
        return True
    return now - last >= timedelta(seconds=RETRY_SECONDS)


def _record(db: Any, tenant_id: str, task_id: str, patch: dict[str, Any]) -> bool:
    """Merge `patch` into `metadata.ci_fix`, guarded; True only if written."""
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            return False
        metadata = dict(_mapping(data.get("metadata")))
        record = dict(_mapping(metadata.get(CI_FIX_METADATA_FIELD)))
        if record.get(POSTBACK_FIELD) != PENDING:
            return False
        record.update(patch)
        metadata[CI_FIX_METADATA_FIELD] = record
        txn.update(ref, {"metadata": metadata})
        return True

    return bool(_apply(transaction))


def _record_head(db: Any, tenant_id: str, task_id: str, now: datetime) -> str | None:
    """Record terminal fix round `task_id`'s pushed head on its branch's task; the
    marker's new state, or None when there was nothing to do. One transaction,
    guarded on both documents being this tenant's and the marker `pending`."""
    ref = db.collection(TASKS).document(task_id)
    terminal = {s.value for s in TERMINAL_STATES}
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> str | None:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id or data.get("state") not in terminal:
            return None
        metadata = dict(_mapping(data.get("metadata")))
        marker = dict(_mapping(metadata.get(FIX_HEAD_FIELD)))
        if marker.get("state") != PENDING:
            return None
        root = _mapping(metadata.get(DISPATCH_METADATA_KEY)).get("continues")
        number = _positive_int(marker.get("pull_request"))
        repository = _repository(data.get("repository_url"))
        pushed = _mapping(_mapping(data.get("result_summary")).get("git")).get("pushed_head")
        root_ref = root_data = None
        if isinstance(root, str) and TASK_ID_RE.match(root) and root != task_id:
            root_ref = db.collection(TASKS).document(root)
            root_snap = _snapshot(txn.get(root_ref))
            root_data = root_snap.to_dict() if root_snap.exists else None
        if (root_ref is None or root_data is None or root_data.get("tenant_id") != tenant_id
                or number is None or repository is None):
            state = UNADDRESSABLE
        elif not isinstance(pushed, str) or not _SHA.fullmatch(pushed):
            state = NOTHING_PUSHED
        else:
            state = RECORDED
            root_metadata = dict(_mapping(root_data.get("metadata")))
            existing = root_metadata.get(FIX_HEADS_FIELD)
            heads = [entry for entry in (existing if isinstance(existing, list) else [])
                     if isinstance(entry, Mapping) and entry.get("task_id") != task_id]
            heads.append({
                "repository": repository, "pull_request": number, "task_id": task_id,
                "workflow_id": data.get("workflow_id"), "head": pushed,
                "kind": marker.get("kind"), "recorded_at": now,
            })
            root_metadata[FIX_HEADS_FIELD] = heads[-FIX_HEADS_KEPT:]
            txn.update(root_ref, {"metadata": root_metadata})
        marker["state"] = state
        marker["checked_at"] = now
        metadata[FIX_HEAD_FIELD] = marker
        txn.update(ref, {"metadata": metadata})
        return state

    return _apply(transaction)


def record_fix_heads(db: Any, tenant_id: str, *, limit: int, now: datetime,
                     report: PostbackReport) -> None:
    """One pass over `tenant_id`'s fix rounds marked `pending`: each terminal one's
    pushed head recorded against its pull request (module docstring)."""
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter(f"metadata.{FIX_HEAD_FIELD}.state", "==", PENDING))
        .limit(limit + 1)
    )
    snaps = list(query.stream())
    if len(snaps) > limit:
        report.truncated = True
    terminal = {s.value for s in TERMINAL_STATES}
    for snap in snaps[:limit]:
        doc = snap.to_dict() or {}
        # Asked again: another tenant's round is never written about.
        if doc.get("tenant_id") != tenant_id or doc.get("state") not in terminal:
            continue
        if _record_head(db, tenant_id, snap.id, now) == RECORDED:
            report.heads_recorded += 1
            log.info("ci-fix head record %s tenant=%s: recorded", snap.id, tenant_id)


def _post(writer: GitHubWriter, doc: Mapping[str, Any], record: Mapping[str, Any],
          token: str, origin: str | None) -> int:
    """Post (or find) the outcome comment; its id, or `_GiveUp` / ApiError."""
    ref, root = _target(doc, record)
    pull = writer.read_pull(ref, ref.number, token)
    if pull.head_ref != f"{BRANCH_PREFIX}{root}":
        raise _GiveUp("pull_request_mismatch")
    marker = outcome_marker(str(doc.get("id") or ""))
    existing = writer.find_comment(ref, marker, token)
    if existing is not None:
        return existing.id
    body = render_outcome(doc, record, root, console_origin=origin, literals=(token,))
    return writer.create_comment(ref, body, token).id


def postback_tenant(ctx: Any, tenant_id: str, *, limit: int) -> PostbackReport:
    """One tick over `tenant_id`'s terminal fix steps not yet reported; see the module docstring."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    db = ctx.store.db
    now = ctx.now()
    writer: GitHubWriter = ctx.forge_writer
    origin = getattr(ctx.settings, "console_url", "") or None
    report = PostbackReport()
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter(f"metadata.{CI_FIX_METADATA_FIELD}.{POSTBACK_FIELD}", "==", PENDING))
        .limit(limit + 1)
    )
    docs = [snap.to_dict() or {} for snap in query.stream()]
    report.truncated = len(docs) > limit
    terminal = {s.value for s in TERMINAL_STATES}
    token = ""
    token_error = ""
    try:
        for doc in docs[:limit]:
            # The query filtered on it; asked again, so another tenant's task
            # is never read, or written about, with this tenant's token.
            if doc.get("tenant_id") != tenant_id:
                continue
            task_id = str(doc.get("id") or "")
            if doc.get("state") not in terminal:
                report.waiting += 1
                continue
            record = _mapping(_mapping(doc.get("metadata")).get(CI_FIX_METADATA_FIELD))
            if not _due(record, now):
                report.skipped += 1
                continue
            try:
                _target(doc, record)
            except _GiveUp as refused:
                report.fail(task_id, refused.code)
                _record(db, tenant_id, task_id, {POSTBACK_FIELD: GAVE_UP, "postback_code": refused.code})
                continue
            if token_error:
                report.fail(task_id, token_error)
                continue
            if not token:
                try:
                    token = ctx.forge_tokens.token_for(tenant)
                except Exception as exc:
                    token_error = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                    log.warning("ci-fix post-back tenant=%s: no forge token (%s)", tenant_id, token_error)
                    report.fail(task_id, token_error)
                    continue
            try:
                comment_id = _post(writer, doc, record, token, origin)
            except _GiveUp as refused:
                report.fail(task_id, refused.code)
                _record(db, tenant_id, task_id, {POSTBACK_FIELD: GAVE_UP, "postback_code": refused.code})
                continue
            except Exception as exc:
                code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                failures = (_positive_int(record.get("postback_failures")) or 0) + 1
                log.warning("ci-fix post-back %s tenant=%s: not posted (%s, %d)",
                            task_id, tenant_id, code, failures)
                report.fail(task_id, code)
                patch: dict[str, Any] = {
                    "postback_failures": failures, "postback_checked_at": now, "postback_code": code,
                }
                if failures >= MAX_POSTBACK_FAILURES:
                    patch[POSTBACK_FIELD] = GAVE_UP
                _record(db, tenant_id, task_id, patch)
                continue
            if _record(db, tenant_id, task_id,
                       {POSTBACK_FIELD: POSTED, "posted_at": now, "comment_id": comment_id}):
                report.posted += 1
                log.info("ci-fix post-back %s tenant=%s: posted", task_id, tenant_id)
    finally:
        token = ""
    # No token: the record is the platform's own, read and written in Firestore.
    record_fix_heads(db, tenant_id, limit=limit, now=now, report=report)
    return report
