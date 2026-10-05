"""Bring the issue's comments, and the pull request's keyword, up to the run (#454).

`sync_issue(ctx, run)` is called after every move of a run (routes/runs.py)
and does at most two writes: the PLAN comment, posted once the run has a plan
and edited whenever what it renders changes (an edit, a new revision, the
approval), and the ONE STATUS comment, edited in place whenever the run's
rendered status changes. `sync_pull_request` records the run's pull request
and writes the `Closes #N` / `part of #N` block into its body; the CI loop
calls it.

THE RUN'S TRUTH IS FIRESTORE, NOT THE COMMENT. Nothing here raises to its
caller and nothing here moves a run. A forge failure -- no credential, a 403
for a token without `issues: write`, GitHub down -- is logged by its code and
recorded on the run as `writeback_error` (redacted, bounded), and the run goes
on exactly as it would have. The same write is not retried for
`RETRY_SECONDS`, so a run the console polls every few seconds does not read
the tenant's secret and call GitHub on every poll while the credential is
wrong.

WHAT IS SKIPPED. Each comment's rendered text is digested and the digest
stored (`last_plan_posted`, `last_status_posted`); a sync whose texts digest
the same writes nothing, and reads no token. The status text carries no time
for exactly this reason.

ONE COMMENT, NOT TWO. The id GitHub gave a comment is stored with an
`IssueRuns.patch`. When there is no stored id -- the first write, or a write
that succeeded and a patch that did not -- the issue's comments are searched
for this run's marker first (`GitHubWriter.find_comment`), and only if none
is found is a comment posted. Two syncs racing past that search can both
post; the patch keeps whichever id was stored first and the loser deletes the
comment it posted.

ALREADY ON MAIN (#646). A run that ended DONE with `outcome:
already_on_main` gets a third comment, the build's VERIFICATION table
(`render_verification_comment`), posted once and edited if its text changes,
and then -- only when `issuecomments.closes_issue` says every planned
requirement is met on main -- the issue is CLOSED as completed
(`GitHubWriter.close_issue`), once: `issue_closed` is recorded only by a
close that worked. The close is never attempted before the table is on the
issue, because the table is the evidence it cites. Both use the tenant's
credential below, never anything an agent holds. A failed write is recorded
and retried exactly as a comment's is: a read of the run (`GET
/v1/runs/{id}`) syncs it, a DONE run included.

INVARIANT 9. The token is the run's OWN tenant's (`ctx.store.get_tenant(
run.tenant_id)`, then `ctx.forge_tokens`), read only when there is something
to write, held in this frame, passed to redaction as a known literal, and
dropped before return. Nothing a caller sent selects it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Sequence

from swarm_common.models import Tenant

from .errors import ApiError
from .forgewrite import CommentRef, ForgeWriteNotFound, GitHubWriter
from .issuecomments import (
    PLAN_KIND,
    STATUS_KIND,
    VERIFICATION_KIND,
    apply_keyword_block,
    body_digest,
    closes_issue,
    keyword_block,
    marker,
    neutralise_closing_keywords,
    render_plan_comment,
    render_status_comment,
    render_verification_comment,
)
from .issueruns import IssueRun, IssueRuns, failure_text

log = logging.getLogger(__name__)

#: How long a failed write is not retried, unless what it would write changes.
RETRY_SECONDS = 300


def _tenant(ctx: Any, tenant_id: str) -> Tenant:
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise ApiError(f"tenant {tenant_id!r} is not registered; its forge credential cannot be named")
    return tenant


def _upsert(
    writer: GitHubWriter, run: IssueRun, kind: str, stored_id: int | None, body: str, token: str,
) -> tuple[CommentRef, bool]:
    """Edit the stored comment, else adopt ours by marker, else post. (comment, created)."""
    if stored_id is not None:
        try:
            return writer.edit_comment(run.issue, stored_id, body, token), False
        except ForgeWriteNotFound:
            # Deleted on GitHub. Look for another of ours, then post again.
            log.info("issue run %s: %s comment %s is gone", run.id, kind, stored_id)
    found = writer.find_comment(run.issue, marker(run.id, kind), token, login=run.forge_login)
    if found is not None:
        return writer.edit_comment(run.issue, found.id, body, token), False
    return writer.create_comment(run.issue, body, token), True


def _record_failure(
    runs: IssueRuns, run: IssueRun, error: Exception, *, attempt: str, now: Any,
    changes: dict[str, Any] | Callable[[IssueRun], dict[str, Any]],
) -> IssueRun:
    code = error.code if isinstance(error, ApiError) else type(error).__name__
    message = error.message if isinstance(error, ApiError) else (
        f"the write-back failed ({type(error).__name__})"
    )
    log.warning("issue run %s tenant=%s: write-back failed (%s)", run.id, run.tenant_id, code)
    def _patch(current: IssueRun) -> dict[str, Any]:
        return {
            **(changes(current) if callable(changes) else changes),
            "writeback_error": failure_text(f"{code}: {message}"),
            "writeback_failed_at": now,
            "writeback_attempt": attempt,
        }

    try:
        return runs.patch(run.tenant_id, run.id, _patch)
    except Exception as exc:
        log.warning("issue run %s: write-back failure not recorded (%s)", run.id, type(exc).__name__)
        return run


def sync_issue(ctx: Any, run: IssueRun) -> IssueRun:
    """The issue's plan and status comments, brought up to `run`. Never raises."""
    try:
        return _sync_issue(ctx, run)
    except Exception as exc:
        # `_sync_issue` records its own failures; this is the net under it.
        log.warning("issue run %s: write-back skipped (%s)", run.id, type(exc).__name__)
        return run


def _sync_issue(ctx: Any, run: IssueRun) -> IssueRun:
    origin = getattr(ctx.settings, "console_url", "") or None
    plan_text = render_plan_comment(run, console_origin=origin) if run.plan else None
    status_text = render_status_comment(run, console_origin=origin)
    plan_hash = body_digest(plan_text) if plan_text is not None else None
    status_hash = body_digest(status_text)
    want_plan = plan_hash is not None and plan_hash != run.last_plan_posted
    want_status = status_hash != run.last_status_posted
    verification_text = render_verification_comment(run)
    verification_hash = body_digest(verification_text) if verification_text is not None else None
    want_verification = (
        verification_hash is not None and verification_hash != run.last_verification_posted
    )
    want_close = closes_issue(run) and run.issue_closed is not True
    if not (want_plan or want_status or want_verification or want_close):
        return run
    attempt = body_digest(
        f"{plan_hash if want_plan else ''}|{status_hash if want_status else ''}"
        f"|{verification_hash if want_verification else ''}|{'close' if want_close else ''}"
    )
    now = ctx.now()
    if (
        run.writeback_error
        and run.writeback_attempt == attempt
        and run.writeback_failed_at is not None
        and now - run.writeback_failed_at < timedelta(seconds=RETRY_SECONDS)
    ):
        return run

    runs = IssueRuns(ctx.db, now=ctx.now)
    writer: GitHubWriter = ctx.forge_writer
    changes: dict[str, Any] = {}
    created: dict[str, int] = {}
    token = ""
    try:
        token = ctx.forge_tokens.token_for(_tenant(ctx, run.tenant_id))
        literals = (token,)
        if want_plan:
            comment, fresh = _upsert(
                writer, run, PLAN_KIND, run.plan_comment_id,
                render_plan_comment(run, console_origin=origin, literals=literals), token,
            )
            changes["plan_comment_id"] = comment.id
            changes["last_plan_posted"] = plan_hash
            if comment.login:
                changes["forge_login"] = comment.login
            if fresh:
                created["plan_comment_id"] = comment.id
        if want_status:
            comment, fresh = _upsert(
                writer, run, STATUS_KIND, run.status_comment_id,
                render_status_comment(run, console_origin=origin, literals=literals), token,
            )
            changes["status_comment_id"] = comment.id
            changes["last_status_posted"] = status_hash
            if comment.login:
                changes["forge_login"] = comment.login
            if fresh:
                created["status_comment_id"] = comment.id
        if want_verification:
            comment, fresh = _upsert(
                writer, run, VERIFICATION_KIND, run.verification_comment_id,
                render_verification_comment(run, literals=literals) or "", token,
            )
            changes["verification_comment_id"] = comment.id
            changes["last_verification_posted"] = verification_hash
            if comment.login:
                changes["forge_login"] = comment.login
            if fresh:
                created["verification_comment_id"] = comment.id
        # The table is on the issue -- written now, or by an earlier sync --
        # before the issue is closed on its evidence.
        if want_close:
            writer.close_issue(run.issue, token)
            changes["issue_closed"] = True
            log.info("issue run %s tenant=%s: closed %s, already on main",
                     run.id, run.tenant_id, run.issue.short)
    except Exception as exc:
        token = ""
        return _record_failure(runs, run, exc, attempt=attempt, now=now, changes=changes)

    try:
        def _patch(current: IssueRun) -> dict[str, Any]:
            return {
                **_keep_first(changes, run, current),
                "writeback_error": None,
                "writeback_failed_at": None,
                "writeback_attempt": None,
            }

        stored = runs.patch(run.tenant_id, run.id, _patch)
        _delete_losers(writer, run, created, stored, token)
        return stored
    finally:
        token = ""


def _keep_first(changes: dict[str, Any], read: IssueRun, current: IssueRun) -> dict[str, Any]:
    """`changes`, except a comment id ANOTHER sync stored while this one ran is kept.

    "Another sync" is an id that is not the one this sync started from: the
    id this sync replaced (a comment deleted on GitHub) is not a rival.
    """
    kept = dict(changes)
    for name in ("plan_comment_id", "status_comment_id", "verification_comment_id"):
        theirs = getattr(current, name)
        if (
            name in kept and theirs is not None and theirs != kept[name]
            and theirs != getattr(read, name)
        ):
            kept[name] = theirs
    return kept


def _delete_losers(
    writer: GitHubWriter, run: IssueRun, created: dict[str, int], stored: IssueRun, token: str
) -> None:
    """A comment this sync posted that lost the race to a stored id is deleted."""
    for name, comment_id in created.items():
        if getattr(stored, name) != comment_id:
            try:
                writer.delete_comment(run.issue, comment_id, token)
            except Exception as exc:
                log.warning(
                    "issue run %s: duplicate comment %s not deleted (%s)",
                    run.id, comment_id, type(exc).__name__,
                )


def keyword_mark(closes: bool) -> str:
    """What `pull_request.keyword_written` holds once that block is on the pull request."""
    return "closes" if closes else "part_of"


def sync_pull_request(
    ctx: Any,
    run: IssueRun,
    number: int,
    *,
    closes: bool,
    unmet: Sequence[str] = (),
    extra: Callable[[IssueRun], dict[str, Any]] | None = None,
) -> IssueRun:
    """Record the run's pull request and write its keyword block. Never raises.

    Reads the pull request with the run's tenant's token (its head sha is
    what the CI loop pins), writes `keyword_block` into its body when the
    body does not already say exactly that, and stores `pull_request`
    `{number, url, head_sha}` plus whatever `extra` adds (`checks`,
    `merged`). Then the status comment follows, through `sync_issue`.

    THE KEYWORD IS RECORDED ONLY WHEN IT WAS WRITTEN. A write that worked
    stores `pull_request.keyword_written` as `keyword_mark(closes)`; a write
    that failed clears it. Until it reads right, the pull request may still
    carry a closing keyword an agent wrote (or, from a worker older than
    this change, the "Fixes #N" title), which closes the issue on a squash
    merge whatever the review found, so the CI loop writes the block again
    on every CHECKING visit and does not call a run DONE before it is
    written (`issueci.keyword_pending`).
    """
    runs = IssueRuns(ctx.db, now=ctx.now)
    token = ""
    try:
        token = ctx.forge_tokens.token_for(_tenant(ctx, run.tenant_id))
        writer: GitHubWriter = ctx.forge_writer
        pull = writer.read_pull(run.issue, number, token)
        block = keyword_block(run, closes=closes, unmet=unmet, literals=(token,))
        body = apply_keyword_block(pull.body, run, block)
        # The title cannot close the issue either. The worker's own title
        # says "part of #N" (`agent_worker.lifecycle._title_from_issue_input`;
        # a worker before that change wrote "Fixes #N"), but an agent's
        # `pr-title.txt` may carry a keyword, and a squash merge writes the
        # title into a commit on the base branch, which GitHub reads for
        # closing keywords. Only issue runs are retitled here.
        title = neutralise_closing_keywords(pull.title)
        if body != pull.body or title != pull.title:
            writer.edit_pull_body(
                run.issue, number, body, token, title=title if title != pull.title else None
            )
        record = {
            **(run.pull_request or {}),
            "number": pull.number,
            "url": pull.url,
            "head_sha": pull.head_sha,
            "merged": pull.merged,
            "keyword_written": keyword_mark(closes),
        }
    except Exception as exc:
        token = ""
        return _record_failure(
            runs, run, exc, attempt=f"pull:{int(number)}", now=ctx.now(),
            changes=lambda current: (
                {} if current.pull_request is None
                else {"pull_request": {**current.pull_request, "keyword_written": None}}
            ),
        )
    finally:
        token = ""
    try:
        stored = runs.patch(run.tenant_id, run.id, lambda current: {
            "pull_request": {**record, **(extra(current) if extra else {})},
        })
    except Exception as exc:
        log.warning("issue run %s: pull request not recorded (%s)", run.id, type(exc).__name__)
        return run
    return sync_issue(ctx, stored)
