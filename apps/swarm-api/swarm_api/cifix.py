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
    through `issuecomments.neutral` with the token as a known literal:
    redacted, `<`/`>` escaped, mentions and closing keywords broken. No log
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
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES

from .codec import agent_console_url
from .continuation import TASK_ID_RE, Continuation
from .errors import ApiError, NotFound
from .forgewrite import GitHubWriter
from .issueci import NO_CHANGE_MARKER
from .issuecomments import bounded, neutral
from .repositories import parse_repository
from .rollup import SKIPPED_SUMMARY_KEY
from .schemas import WorkflowCreate
from .validation import DISPATCH_METADATA_KEY, DispatchOptionError, IssueRef

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


def stamp(store: Any, tenant_id: str, spec: WorkflowCreate,
          continuation: Continuation | None) -> WorkflowCreate:
    """`spec` with `metadata.ci_fix` checked, counted and stamped `pending`.

    Unchanged when there is no `ci_fix`. Raises `DispatchOptionError` (422
    `invalid_dispatch`) for every refusal, before anything is written.
    """
    for step in spec.steps:
        if CI_FIX_METADATA_FIELD in (step.metadata or {}):
            raise _refuse(
                "metadata.ci_fix belongs to the workflow, not a step: it names the "
                "pull request the one fix step pushes to.",
                step_id=step.step_id,
            )
    if CI_FIX_METADATA_FIELD not in spec.metadata:
        return spec
    if continuation is None:
        raise _refuse(
            "metadata.ci_fix records a fix for a red pull request's branch, which "
            "only a workflow with continues_task pushes to.",
        )
    record = _checked_record(spec.metadata[CI_FIX_METADATA_FIELD])
    made = _attempts_made(
        store.db, tenant_id, continuation.root_task_id, record["pull_request"], MAX_ATTEMPTS_CEILING,
    )
    if made >= record["max_attempts"]:
        raise _refuse(
            f"pull request #{record['pull_request']} has had {made} fix attempt(s); the cap "
            f"is {record['max_attempts']}. A person has to look at it now.",
            attempts=made, max_attempts=record["max_attempts"],
        )
    record[POSTBACK_FIELD] = PENDING
    return spec.model_copy(
        update={"metadata": {**spec.metadata, CI_FIX_METADATA_FIELD: record}}
    )


# --------------------------------------------------------------------------
# the comment
# --------------------------------------------------------------------------

def _paragraph(text: Any, limit: int, literals: tuple[str, ...]) -> str:
    """The first paragraph of `text`, neutralised and on one line."""
    if not isinstance(text, str) or not text.strip():
        return ""
    first = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    return neutral(first, limit, literals=literals, one_line=True)


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
    cause_text = f" (`{neutral(str(cause), 80, literals=literals, one_line=True)}`)" if cause else ""
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
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "posted": self.posted,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "failed": self.failed,
            "truncated": self.truncated,
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
    return report
