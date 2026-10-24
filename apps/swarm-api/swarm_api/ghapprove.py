"""GitHub-comment approvals: `/swarmcloud approve <digest>` (docs/schedules.md §4.9, SD9, lane S12).

A member may approve a pending plan by commenting on its issue, and a pending
merge by commenting on its pull request, with the command the plan comment
and the merge request show. The schedule tick's advance step calls `poll`
once a minute (`schedulefire.Ticker._advance_live`).

A COMMENT IS ONLY EVER A REQUEST. It becomes the same approval the console
makes: a plan through `routes.runs._approve`, the one function every plan
approval goes through (the digest, the hold and the schedule's approvers are
checked there), and a merge through `approvals.decide` with
`approvals.merge_move`, after the same `check_hold` and `check_gate_approver`
the inbox's approve route makes. So the digest check, the hold check, the
second-person rule and the audit are the console's, and two comments racing
the console's approve make one transition: the loser meets `already_decided`.

THE POLL IS BOUNDED BY THE INBOX. It reads comments only on the threads of
PLANNED issue runs (the inbox's projected `plan` and `hold` items) and of
pending `merge` records, at most `POLL_PER_TICK` threads a tick and
`PER_TENANT_PER_TICK` of one tenant's, rotating by the minute so a long inbox
is read in turns. A subject with no pending approval is never read. Each
thread is read from its cursor (`approval_comment_cursors/{id}`: the last
comment id seen and its time), so a comment is considered once. The
cursor lives in its own document because a projected plan has no approval
document to carry it (§4.5: issue runs join the inbox without moving, and
`IssueRuns.patch` refuses fields outside PATCHABLE_FIELDS).

THE LOGIN IS MAPPED TO A MEMBER, AND THE MAPPING IS VERIFIED. The member is
the one whose ACTIVE onboarding connection (`forge_connections`, written by
`forgeapp._store` from the `GET /user` that the member's own authorisation
bought) holds the comment author's login, case-insensitively, in the item's
tenant. Where the connection stored GitHub's numeric user id, the comment's
author id must equal it too: a login can be renamed and reused, an id
cannot. `git_tokens.forge_login` and `issue_runs.forge_login` are never read
for this: the first is whatever token was stored, the second a comment's
author (docs/schedules.md §0.1, check 6). A comment is accepted only when:

  * its author is a GitHub `User`, not a bot, and not an App acting as one;
  * its author is not the login the platform's own comments are written
    with (the tenant's forge credential): a comment made with the tenant's
    credential is the platform's, or an agent's, never a member's act;
  * it has not been edited (`updated_at == created_at`);
  * the member is still a member of the item's tenant, asked of the
    directory now;
  * the member's role in that workspace is `member` or above, read fresh
    (docs/design/workspace-roles.md §4.8, `approvals.require_decider`): a
    `viewer`'s comment is answered with `role_member_required` once that
    code's switch is on, and goes through with a report-only log line while
    it is off. The reply names the code and never the workspace's admins:
    a GitHub thread is not the roster every member reads;
  * its digest is the item's current digest;
  * the member satisfies §4.6 for the item: the hold's approvers, the
    schedule's approvers and the second-person rule.

WHAT A COMMENT CANNOT DO. It cannot type a confirmation, so a one-person
tenant's own `second_member` hold is approved in the console, and a comment
on it is answered so. It cannot reject. A comment that approves nothing gets
ONE reply saying why (the cursor is written before the reply, so a retry
never answers twice); a bot's comment gets none, so two automations cannot
answer each other forever.

THE AUDIT records `via: github_comment`, the comment id, the login and the
member it resolved to, in the scheduled item's `schedule_audit` entry.

INVARIANT 9. The token is the item's OWN tenant's (`ctx.forge_tokens`), read
once per tenant per poll, passed to nothing but the forge client, and
dropped on return. Nothing a comment says selects a tenant, a token or an
item: the item is found first, and the comment is read from its thread.

INVARIANT 1. An approval holds nothing while it waits, and a comment changes
nothing until it approves: a plan's workflow is then submitted QUEUED
through `SubmissionService`, a merge's continuation through
`issueci._merge` on the run's next visit, exactly as from the console.

`/answer <n> <option>`: THE OWNER ANSWERS AN AGENT'S BLOCKING QUESTION ON ITS
ISSUE (docs/design/agent-autonomy.md §3.9, owner decision Q5, lane AU-7).
An agent parked HUMAN_REQUIRED on a blocking question
(`agent_questions.parked_wait`) whose task belongs to an issue run gets one
comment on that run's issue (`render_questions`, posted once per wait under
its marker): each question with its options, recommended first, which ones
block, and the exact reply syntax, naming the submitter by the GitHub login
of their onboarding connection when they have one (never their address).
The same poll then reads that thread from the wait's cursor. A reply whose
first non-blank line is `/answer <n> <option>` is taken only when the same
checks an approval's comment meets hold -- a GitHub `User`, not an app, not
the platform's own login, not edited, mapped by `member_for` to a member of
the task's tenant who is still one, at the role `member` or above -- AND the
member may answer this task by `agent_questions.answer_refusal` (its
submitter, or an admin), the wait is still open, `<n>` names one of its
questions and `<option>` is exactly one of that question's labels. Then it
is `agent_questions.answer_question`, the console's own path, with `via:
github-comment`: stored, history kept, and the last blocking answer
returns the agent to READY. Any other `/answer` is IGNORED AND RECORDED
(`answer_comment_ignored`): a log line and an entry in the wait's `ignored`
list naming the comment, the login and the reason, never acted on and
never replied to. A wait past 72 hours is dead-lettered by the same poll
(`agent_questions.expire_question_waits`). An AWAITING_INPUT step is not
polled here: it has no agent and is answered through `awaitinput` alone.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote

from google.cloud.firestore_v1.base_query import FieldFilter

from . import approvals, schedaudit
from .errors import ApiError, NotFound, RoleMemberRequired, RoleUnavailable, UpstreamUnavailable
from .forgeapp import ACTIVE, CONNECTIONS, connection_id_for
from .forgewrite import ForgeWriteNotFound, GitHubWriter
from .issuecomments import APPROVE_COMMAND, neutralise_mentions
from .issueruns import (
    APPROVERS_SECOND_MEMBER,
    RUNS_COLLECTION,
    InvalidRunTransition,
    IssueRun,
    IssueRuns,
    RunState,
)
from .validation import IssueRef

log = logging.getLogger(__name__)

#: The audit's `via` for an approval made by a comment (§4.9).
VIA = "github_comment"
#: The command, as the plan comment (issuecomments) and the merge request show it.
COMMAND = APPROVE_COMMAND
ALREADY_DECIDED = "already decided"
#: The first non-blank line of a comment, exactly the command and one digest.
#: A quoted reply (`> /swarmcloud ...`) or the command mid-sentence is not one.
_COMMAND_LINE = re.compile(r"^/swarmcloud[ \t]+approve[ \t]+(\S{1,200})[ \t]*$", re.IGNORECASE)
#: Every comment the platform writes opens with this, so the poll never reads
#: its own plan comment, merge request or reply as a member's command.
_OUR_MARKER = "<!-- swarmcloud"

CURSORS = "approval_comment_cursors"

#: Threads read per tick, and per tenant per tick (§5.6: one tenant's inbox
#: cannot take another's turn). Each thread is one to `MAX_PAGES` GETs on the
#: tenant's own token; a tick with 20 threads makes at most 60.
POLL_PER_TICK = 20
PER_TENANT_PER_TICK = 5
#: Candidates read per kind per tick: the inbox's own bound.
SCAN = approvals.INBOX_SCAN
#: Pages of new comments read on one thread per tick; the rest are next tick's.
MAX_PAGES = 3
PAGE_SIZE = 100
#: What a 403 on the read names: the credential already writes these comments.
ISSUES_READ = "issues: read"


# --------------------------------------------------------------------------
# What the comments say
# --------------------------------------------------------------------------


def command_text(digest: str) -> str:
    return f"{COMMAND} {digest}"


def approval_marker(approval_id: str) -> str:
    return f"<!-- swarmcloud-approval:{approval_id} -->"


def render_merge_request(approval_id: str, run: IssueRun, head_sha: str, verdict: str | None) -> str:
    """The merge request on the pull request (§4.9): what waits, and the exact command."""
    digest = approvals.merge_digest_text(approvals.merge_digest(head_sha, verdict))
    hold = run.approval_hold or {}
    lines = [
        approval_marker(approval_id),
        f"**SwarmCloud: this pull request is green at `{head_sha[:12]}` and waits for a merge approval.**",
        "",
        "Approve it in the console's Approvals inbox, or comment exactly:",
        "",
        f"    {command_text(digest)}",
        "",
        "A comment approves only when its author is a current member of the tenant who connected "
        "this GitHub account in onboarding, and only while the pull request is still at this head: "
        "a push makes this request stale.",
    ]
    if hold.get("code"):
        lines += ["", f"The run is held `{hold.get('code')}`: only the hold's approvers may approve."]
    lines += ["", f"_Written by SwarmCloud for its run `{run.id}`._"]
    return "\n".join(lines) + "\n"


def parse_command(body: Any) -> str | None:
    """The digest a comment's first non-blank line commands, or None."""
    if not isinstance(body, str) or body.startswith(_OUR_MARKER):
        return None
    for line in body.splitlines():
        if line.strip():
            found = _COMMAND_LINE.match(line.strip())
            return found.group(1) if found else None
    return None


def _reply_text(approval_id: str, comment_id: int, message: str) -> str:
    return (f"{approval_marker(approval_id)}\n"
            f"SwarmCloud: comment {int(comment_id)} approved nothing. {neutralise_mentions(message)}\n")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _aware(moment: Any) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(moment, datetime):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _github_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cursor_id(approval_id: str) -> str:
    return "cur_" + hashlib.sha256(approval_id.encode()).hexdigest()[:24]


@dataclasses.dataclass
class Item:
    """One pending approval with a GitHub thread, as the poll reads it."""

    approval_id: str
    tenant_id: str
    kind: str  # approvals.PLAN (a projected plan or hold) or approvals.MERGE
    run: IssueRun
    thread: IssueRef
    digest: str
    requested_at: datetime | None
    record: Mapping[str, Any] | None = None


@dataclasses.dataclass
class Comment:
    id: int
    body: str
    login: str
    user_id: int | None
    user_type: str
    via_app: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_github(cls, data: Any) -> "Comment | None":
        if not isinstance(data, dict) or _int(data.get("id")) is None:
            return None
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return cls(
            id=int(data["id"]),
            body=data.get("body") if isinstance(data.get("body"), str) else "",
            login=str(user.get("login") or ""),
            user_id=_int(user.get("id")),
            user_type=str(user.get("type") or ""),
            via_app=bool(data.get("performed_via_github_app")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class Refused(Exception):
    """A command comment that approves nothing. `reply` False: answered with silence."""

    def __init__(self, message: str, *, reply: bool = True, retry: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.reply = reply
        #: The answer could not be decided now (the directory did not answer):
        #: the cursor stays before this comment, and the next tick reads it again.
        self.retry = retry


# --------------------------------------------------------------------------
# The login, mapped to a member and verified (§4.9)
# --------------------------------------------------------------------------


def member_for(db: Any, tenant_id: str, login: str, user_id: int | None) -> str | None:
    """The member whose ACTIVE onboarding connection in `tenant_id` is this GitHub account.

    The login matches case-insensitively; where the connection stored GitHub's
    user id, the comment's author id must equal it as well. Two members
    claiming one account is no answer: None.
    """
    wanted = (login or "").strip().lower()
    if not wanted:
        return None
    query = (
        db.collection(CONNECTIONS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .limit(2000)
    )
    found: set[str] = set()
    for snap in query.stream():
        doc = snap.to_dict() or {}
        if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE:
            continue
        if str(doc.get("forge_login") or "").strip().lower() != wanted:
            continue
        stored_id = _int(doc.get("forge_user_id"))
        if stored_id is not None and stored_id != user_id:
            continue
        email = str(doc.get("user") or "").strip().lower()
        if email:
            found.add(email)
    return found.pop() if len(found) == 1 else None


class _NotMember(Exception):
    pass


def _member_auth(ctx: Any, tenant_id: str, email: str, comment_id: int) -> Any:
    """The member's AuthContext, built from documents and NOW a member (schedulefire.member_auth)."""
    from .schedulefire import member_auth

    auth = member_auth(ctx, tenant_id=tenant_id, email=email, subject=f"github-comment:{comment_id}",
                       not_member=_NotMember)
    owner = (getattr(ctx.settings, "platform_owner", "") or "").strip().lower()
    # PLATFORM_OWNER approves an `owner_only` item as a member of its tenant
    # (§4.6); member_auth has just asked the directory that it is one.
    return dataclasses.replace(auth, is_owner=bool(owner) and auth.email == owner)


# --------------------------------------------------------------------------
# One comment
# --------------------------------------------------------------------------


def _verify(ctx: Any, item: Item, comment: Comment, digest: str, own_logins: set[str]) -> Any:
    """The member's AuthContext when every §4.9 condition holds; Refused otherwise."""
    if comment.user_type != "User" or comment.login.lower().endswith("[bot]"):
        raise Refused("a bot's comment approves nothing", reply=False)
    if comment.via_app:
        raise Refused("a comment written through an app approves nothing; comment as yourself on GitHub")
    if comment.login.strip().lower() in own_logins:
        raise Refused("this account writes SwarmCloud's own comments for the tenant, so a comment from it "
                      "is not a member's approval; approve in the console")
    if not comment.created_at or comment.updated_at != comment.created_at:
        raise Refused("an edited comment approves nothing; post the command again in a new comment")
    email = member_for(ctx.db, item.tenant_id, comment.login, comment.user_id)
    if email is None:
        raise Refused("no member of this tenant has connected this GitHub account in onboarding")
    try:
        auth = _member_auth(ctx, item.tenant_id, email, comment.id)
    except _NotMember:
        raise Refused("the member this GitHub account belongs to is no longer a member of the tenant") from None
    except UpstreamUnavailable:
        raise Refused("membership could not be asked of the directory", reply=False, retry=True) from None
    _require_member(ctx, auth)
    if digest != item.digest:
        raise Refused(f"its digest is not the current one; to approve what waits now, comment "
                      f"`{command_text(item.digest)}`")
    hold = item.run.approval_hold or {}
    if hold.get("approvers") == APPROVERS_SECOND_MEMBER and approvals.one_person(ctx, item.tenant_id):
        raise Refused("a one-person workspace approves its own held run only in the console, with a typed "
                      "confirmation naming the matched paths")
    return auth


def _require_member(ctx: Any, auth: Any) -> None:
    """The role floor the console's approve asks (`approvals.require_decider`),
    for the member the comment resolved to. The reply is constant text with
    the code in it: the 403's own message names up to three admins by
    address, which belongs in the console, not on a forge thread."""
    try:
        approvals.require_decider(ctx, auth)
    except RoleMemberRequired as exc:
        role = (exc.detail or {}).get("role") or "below member"
        raise Refused(f"{RoleMemberRequired.code}: the member this GitHub account belongs to holds the "
                      f"role {role} in this workspace, and only a member or above approves; ask an admin "
                      "of the workspace to change it") from None
    except RoleUnavailable:
        raise Refused("the member's role could not be read now", reply=False, retry=True) from None


def _audit_detail(item: Item, comment: Comment, email: str) -> dict[str, Any]:
    return {"via": VIA, "comment_id": comment.id, "login": comment.login, "member": email}


def _approve_plan(ctx: Any, item: Item, auth: Any, comment: Comment) -> None:
    """The inbox's approve of a projected plan (`routes.approvals._approve_projected`), by comment."""
    from .issuesync import sync_issue
    from .routes import approvals as approval_routes
    from .routes import runs as run_routes

    run = IssueRuns(ctx.db, now=ctx.now).get(item.tenant_id, item.run.id)
    if run.state != RunState.PLANNED:
        raise Refused(ALREADY_DECIDED, reply=False)
    try:
        moved = run_routes._approve(ctx, auth, item.tenant_id, run, digest=item.digest, by=auth.email)
    except InvalidRunTransition:
        raise Refused(ALREADY_DECIDED, reply=False) from None
    approval_routes._audit_run(ctx, run, schedaudit.APPROVED, auth.email, {
        "approval_id": item.approval_id,
        "kind": approvals.HOLD if run.approval_hold else approvals.PLAN,
        "subject": {"run_id": run.id}, "digest": item.digest,
        "hold": (run.approval_hold or {}).get("code"), "confirmed": False,
        **_audit_detail(item, comment, auth.email),
    })
    sync_issue(ctx, moved)


def _approve_merge(ctx: Any, item: Item, auth: Any, comment: Comment) -> None:
    """The inbox's approve of a `merge` record (`routes.approvals.approve`), by comment."""
    record = item.record or {}
    schedule = approvals.read_schedule(ctx.db, item.tenant_id, (record.get("subject") or {}).get("schedule_id"))
    run = IssueRuns(ctx.db, now=ctx.now).get(item.tenant_id, item.run.id)
    if run.approval_hold:
        approvals.check_hold(ctx, run, email=auth.email, is_owner=auth.is_owner)
    # A hold is checked above IN ADDITION to the schedule's approvers.
    approvals.check_gate_approver(
        ctx, item.tenant_id, ((schedule or {}).get("gate") or {}).get("approvers"),
        email=auth.email, is_owner=auth.is_owner, schedule=schedule, merge_tier=True,
    )
    from .routes.runs import owner_resigner

    now = ctx.now()
    approvals.decide(ctx.db, now, item.tenant_id, item.approval_id, approve=True, by=auth.email,
                     digest=item.digest,
                     move=approvals.merge_move(ctx.db, now, by=auth.email, resign=owner_resigner(ctx)),
                     audit_detail=_audit_detail(item, comment, auth.email))


def handle(ctx: Any, item: Item, comment: Comment, own_logins: set[str]) -> bool:
    """One command comment: True when it approved. Refused when it approved nothing."""
    digest = parse_command(comment.body)
    if digest is None:
        return False
    auth = _verify(ctx, item, comment, digest, own_logins)
    try:
        if item.kind == approvals.MERGE:
            _approve_merge(ctx, item, auth, comment)
        else:
            _approve_plan(ctx, item, auth, comment)
    except ApiError as exc:
        if exc.code in ("already_decided", "invalid_transition"):
            raise Refused(ALREADY_DECIDED, reply=False) from None
        if isinstance(exc, UpstreamUnavailable):
            raise Refused("the approval could not be made now", reply=False, retry=True) from None
        # Our own refusal sentences (§4.6's approvers, a stale digest, an
        # expired item): constant text, never a forge answer or a token.
        raise Refused(exc.message) from None
    log.info("approval %s tenant=%s approved by github comment %s (member resolved)",
             item.approval_id, item.tenant_id, comment.id)
    return True


# --------------------------------------------------------------------------
# One thread
# --------------------------------------------------------------------------


def _read_cursor(db: Any, item: Item) -> dict[str, Any]:
    snap = db.collection(CURSORS).document(_cursor_id(item.approval_id)).get()
    doc = snap.to_dict() if snap.exists else None
    if doc is None or doc.get("tenant_id") != item.tenant_id or doc.get("approval_id") != item.approval_id:
        return {"last_comment_id": 0, "since": item.requested_at}
    return doc


def _write_cursor(db: Any, item: Item, last_id: int, since: Any, now: datetime) -> None:
    db.collection(CURSORS).document(_cursor_id(item.approval_id)).set({
        "approval_id": item.approval_id,
        "tenant_id": item.tenant_id,
        "thread": item.thread.short,
        "last_comment_id": int(last_id),
        "since": since,
        "polled_at": now,
    })


def _list_comments(writer: GitHubWriter, thread: IssueRef, token: str, since: datetime | None) -> list[Comment]:
    """The thread's comments, oldest first, from `since` (GitHub filters on `updated_at`)."""
    what = f"the comments on {thread.short}"
    url = writer._url(thread, f"issues/{int(thread.number)}/comments")
    query = f"?per_page={PAGE_SIZE}"
    if since is not None:
        query += "&since=" + quote(_github_iso(since), safe="")
    out: list[Comment] = []
    for page in range(1, MAX_PAGES + 1):
        entries = writer._call("GET", f"{url}{query}&page={page}", token, what, needs=ISSUES_READ)
        if not isinstance(entries, list):
            break
        out += [c for c in (Comment.from_github(e) for e in entries) if c is not None]
        if len(entries) < PAGE_SIZE:
            break
    return sorted(out, key=lambda c: c.id)


def poll_thread(ctx: Any, item: Item, token: str) -> dict[str, int]:
    """Read the thread's new comments, act on each command once, move the cursor."""
    writer: GitHubWriter = ctx.forge_writer
    now = ctx.now()
    cursor = _read_cursor(ctx.db, item)
    last_id = int(cursor.get("last_comment_id") or 0)
    since = _aware(cursor.get("since"))
    ours = {c for c in (item.run.plan_comment_id, item.run.status_comment_id,
                        item.run.verification_comment_id, (item.record or {}).get("request_comment_id"))
            if c is not None}
    own_logins = {str(x).strip().lower() for x in (item.run.forge_login,
                                                    (item.record or {}).get("request_comment_login")) if x}
    counts = {"read": 0, "approved": 0, "refused": 0}
    for comment in _list_comments(writer, item.thread, token, since):
        if comment.id <= last_id or comment.id in ours:
            continue
        counts["read"] += 1
        try:
            approved = handle(ctx, item, comment, own_logins)
        except Refused as refused:
            if refused.retry:
                break
            # The cursor moves past the comment BEFORE the reply: a reply is
            # posted at most once, even when this tick dies after posting it.
            last_id, since = comment.id, _aware(comment.created_at) or since
            _write_cursor(ctx.db, item, last_id, since, now)
            if refused.message == ALREADY_DECIDED:
                break
            counts["refused"] += 1
            if refused.reply:
                _reply(writer, item, comment, refused.message, token)
            continue
        last_id, since = comment.id, _aware(comment.created_at) or since
        if approved:
            counts["approved"] += 1
            break
    _write_cursor(ctx.db, item, last_id, since, now)
    return counts


def _reply(writer: GitHubWriter, item: Item, comment: Comment, message: str, token: str) -> None:
    try:
        writer.create_comment(item.thread, _reply_text(item.approval_id, comment.id, message), token)
    except ApiError as exc:
        log.warning("approval %s: reply to github comment %s not posted (%s)",
                    item.approval_id, comment.id, exc.code)


# --------------------------------------------------------------------------
# The poll (the tick's advance step)
# --------------------------------------------------------------------------


def _pr_ref(run: IssueRun, number: Any) -> IssueRef | None:
    number = _int(number)
    return IssueRef(owner=run.issue.owner, repo=run.issue.repo, number=number) if number else None


def candidates(ctx: Any) -> list[Item]:
    """Every pending approval with a GitHub thread, across tenants, in a stable order."""
    db = ctx.db
    items: list[Item] = []
    planned = db.collection(RUNS_COLLECTION).where(
        filter=FieldFilter("state", "==", RunState.PLANNED.value)).limit(SCAN)
    for snap in planned.stream():
        run = IssueRun.from_firestore(snap.to_dict() or {})
        if run.state != RunState.PLANNED or not run.plan_digest:
            continue
        items.append(Item(approval_id=approvals.PROJECTED_PREFIX + run.id, tenant_id=run.tenant_id,
                          kind=approvals.PLAN, run=run, thread=run.issue, digest=run.plan_digest,
                          requested_at=approvals._planned_at(run)))
    pending = db.collection(approvals.COLLECTION).where(
        filter=FieldFilter("state", "==", approvals.PENDING)).limit(SCAN)
    for snap in pending.stream():
        doc = snap.to_dict() or {}
        if doc.get("state") != approvals.PENDING or doc.get("kind") != approvals.MERGE:
            continue
        subject = doc.get("subject") or {}
        try:
            run = IssueRuns(db, now=ctx.now).get(str(doc.get("tenant_id")), str(subject.get("run_id")))
        except NotFound:
            continue
        thread = _pr_ref(run, subject.get("pr") or (run.pull_request or {}).get("number"))
        digest = doc.get("digest")
        if thread is None or not isinstance(digest, Mapping):
            continue
        items.append(Item(approval_id=str(doc["approval_id"]), tenant_id=run.tenant_id, kind=approvals.MERGE,
                          run=run, thread=thread, digest=approvals.merge_digest_text(digest),
                          requested_at=_aware(doc.get("requested_at")), record=doc))
    items.sort(key=lambda i: i.approval_id)
    return items


def pick(items: list[Item], minute: int) -> list[Item]:
    """`POLL_PER_TICK` threads, `PER_TENANT_PER_TICK` of a tenant's, starting where the minute says."""
    if not items:
        return []
    start = (minute * POLL_PER_TICK) % len(items)
    rotated = items[start:] + items[:start]
    picked: list[Item] = []
    per_tenant: dict[str, int] = {}
    for item in rotated:
        if per_tenant.get(item.tenant_id, 0) >= PER_TENANT_PER_TICK:
            continue
        per_tenant[item.tenant_id] = per_tenant.get(item.tenant_id, 0) + 1
        picked.append(item)
        if len(picked) >= POLL_PER_TICK:
            break
    return picked


def poll(ctx: Any, *, out_of_time: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """The schedule tick's comment poll. Never raises: the comments are not the tick's work."""
    counts = {"threads": 0, "read": 0, "approved": 0, "refused": 0, "errors": 0}
    try:
        now = ctx.now()
        items = pick(candidates(ctx), int(_aware(now).timestamp() // 60))
    except Exception as exc:  # noqa: BLE001
        log.warning("github comment approvals: the inbox could not be read (%s)", type(exc).__name__)
        counts["errors"] += 1
        return counts
    tokens: dict[str, str | None] = {}
    for item in items:
        if out_of_time():
            break
        try:
            if item.tenant_id not in tokens:
                tenant = ctx.store.get_tenant(item.tenant_id)
                tokens[item.tenant_id] = ctx.forge_tokens.token_for(tenant) if tenant is not None else None
            token = tokens[item.tenant_id]
            if not token:
                continue
            counts["threads"] += 1
            for key, value in poll_thread(ctx, item, token).items():
                counts[key] += value
        except Exception as exc:  # noqa: BLE001 -- one thread's failure does not stop the rest
            counts["errors"] += 1
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            if item.tenant_id not in tokens:
                tokens[item.tenant_id] = None  # no credential: not asked again this poll
            log.warning("github comment approvals: %s tenant=%s not polled (%s)",
                        item.approval_id, item.tenant_id, code)
    tokens.clear()
    # The agents' blocking questions ride the same tick (§3.9): their own
    # counts, their own failures, never this poll's.
    counts.update(poll_answers(ctx, out_of_time=out_of_time))
    return counts


# --------------------------------------------------------------------------
# `/answer <n> <option>` on an agent's issue (§3.9, owner decision Q5)
# --------------------------------------------------------------------------

#: The reply command, as the questions comment shows it.
ANSWER_COMMAND = "/answer"
#: The first non-blank line of a reply: the command, a question number and
#: the option's label (quotes around it are allowed and dropped).
_ANSWER_LINE = re.compile(r"^/answer[ \t]+(\d{1,3})[ \t]+(.+?)[ \t]*$", re.IGNORECASE)
#: What an `/answer` that answered nothing is recorded as.
ANSWER_IGNORED = "answer_comment_ignored"
#: The answer's `via`: a label for where it was given, never a permission.
ANSWER_VIA = "github-comment"
#: Question waits read per tick, and how many ignored replies one keeps.
QUESTION_THREADS_PER_TICK = 20
MAX_IGNORED = 20
#: A parked task's issue run (`validation.ISSUE_RUN_METADATA_KEY`).
_ISSUE_RUN_KEY = "issue_run"


def question_marker(task_id: str, attempt_id: str) -> str:
    return f"<!-- swarmcloud-question:{task_id}:{attempt_id} -->"


def parse_answer(body: Any) -> tuple[int, str] | None:
    """(the 1-based question number, the option) a reply's first non-blank line names, or None."""
    if not isinstance(body, str) or body.startswith(_OUR_MARKER):
        return None
    for line in body.splitlines():
        if line.strip():
            found = _ANSWER_LINE.match(line.strip())
            if not found:
                return None
            option = found.group(2).strip()
            if len(option) >= 2 and option[0] == option[-1] and option[0] in "\"'`":
                option = option[1:-1].strip()
            return int(found.group(1)), option
    return None


def render_questions(task_id: str, wait: Mapping[str, Any], owner_login: str | None) -> str:
    """The comment on the agent's issue: who it waits for, each question, the reply syntax.

    Every text is the agent's, so it is redacted and its @-mentions
    neutralised before it is public; the owner is named by GitHub login only.
    """
    from .agent_questions import _redacted_question

    blocking = set(wait.get("blocking") or [])
    who = f"@{owner_login}" if owner_login else "the person who launched it"
    lines = [
        question_marker(task_id, str(wait.get("attempt_id") or "")),
        f"**SwarmCloud: agent `{task_id}` is parked until {who} answers its blocking "
        f"question{'s' if len(blocking) != 1 else ''}.** It holds no capacity while it waits, "
        "and is ended after 72 hours unanswered.",
        "",
    ]
    for n, entry in enumerate(wait.get("questions") or [], start=1):
        question, _ = _redacted_question(entry)
        mark = " **(blocking)**" if n - 1 in blocking else ""
        lines.append(f"{n}. {neutralise_mentions(question['question'] or '')}{mark}")
        options = sorted(question["options"], key=lambda o: o["label"] != question["recommended"])
        for option in options:
            rec = " _(recommended)_" if option["label"] == question["recommended"] else ""
            desc = f": {neutralise_mentions(option['description'])}" if option["description"] else ""
            lines.append(f"   - `{neutralise_mentions(option['label'] or '')}`{rec}{desc}")
        if question["context"]:
            lines.append(f"   > {neutralise_mentions(question['context']).replace(chr(10), ' ')}")
    lines += [
        "",
        "Answer in the console on the agent's Questions card, or reply with one comment per "
        "answer whose first line is exactly:",
        "",
        f"    {ANSWER_COMMAND} <question number> <option>",
        "",
        "for example `/answer 1 " + str(((wait.get("questions") or [{}])[0].get("recommended")) or "<option>")
        + "`. A reply counts only from a member of the workspace who connected this GitHub "
        "account in onboarding and may answer this agent (who launched it, or an admin); "
        "any other reply is ignored and recorded.",
        "",
        f"_Written by SwarmCloud for agent `{task_id}`._",
    ]
    return "\n".join(lines) + "\n"


@dataclasses.dataclass
class QuestionItem:
    """One agent parked on a blocking question, with the issue it is asked on."""

    task_id: str
    tenant_id: str
    doc: Mapping[str, Any]
    wait: Mapping[str, Any]
    run: IssueRun
    thread: IssueRef


def question_candidates(ctx: Any) -> list[QuestionItem]:
    """Every agent parked on a blocking question whose task is an issue run's, across tenants."""
    from .agent_questions import parked_human_snapshots, parked_wait

    db = ctx.db
    items: list[QuestionItem] = []
    for snap in parked_human_snapshots(db):
        doc = snap.to_dict() or {}
        wait = parked_wait(doc)
        run_id = (doc.get("metadata") or {}).get(_ISSUE_RUN_KEY)
        if wait is None or not isinstance(run_id, str) or not run_id:
            continue
        tenant_id = str(doc.get("tenant_id") or "")
        try:
            run = IssueRuns(db, now=ctx.now).get(tenant_id, run_id)
        except (NotFound, ApiError):
            continue
        # The run is read in the task's own tenant: another tenant's run is
        # the not-found above, so a task can never point the poll elsewhere.
        if run.tenant_id != tenant_id:
            continue
        items.append(QuestionItem(task_id=str(doc.get("id") or snap.id), tenant_id=tenant_id,
                                  doc=doc, wait=wait, run=run, thread=run.issue))
    items.sort(key=lambda i: i.task_id)
    return items


def _owner_login(db: Any, tenant_id: str, email: str | None) -> str | None:
    """The GitHub login of the submitter's ACTIVE onboarding connection here, else None."""
    if not email:
        return None
    snap = db.collection(CONNECTIONS).document(connection_id_for(tenant_id, email)).get()
    doc = snap.to_dict() if snap.exists else None
    if not doc or doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE:
        return None
    login = str(doc.get("forge_login") or "").strip()
    return login if re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) else None


def _patch_wait(db: Any, item: QuestionItem, patch: Callable[[dict[str, Any]], None]) -> bool:
    """Adopt the wait into `metadata.question_wait` and apply `patch` to it, in
    one transaction guarded on the task still parked on the SAME wait."""
    from google.cloud import firestore

    from swarm_common.admission import _snapshot

    from .agent_questions import QUESTION_WAIT_KEY, adopt_wait, parked_wait

    ref = db.collection("tasks").document(item.task_id)

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != item.tenant_id:
            return False
        wait = parked_wait(data)
        if wait is None or wait.get("attempt_id") != item.wait.get("attempt_id"):
            return False
        metadata = dict(data.get("metadata") or {})
        adopt_wait(metadata, wait)
        record = dict(metadata[QUESTION_WAIT_KEY])
        patch(record)
        metadata[QUESTION_WAIT_KEY] = record
        txn.update(ref, {"metadata": metadata})
        return True

    return _apply(db.transaction())


def _recorded(item: QuestionItem) -> Mapping[str, Any]:
    """The adopted record of THIS wait, or {} before it was adopted."""
    from .agent_questions import QUESTION_WAIT_KEY

    record = (item.doc.get("metadata") or {}).get(QUESTION_WAIT_KEY)
    if isinstance(record, Mapping) and record.get("attempt_id") == item.wait.get("attempt_id"):
        return record
    return {}


def _post_questions(ctx: Any, item: QuestionItem, token: str) -> bool:
    """Post the questions comment once per wait; True when it is there now."""
    if isinstance(_recorded(item).get("comment"), Mapping):
        return True
    writer: GitHubWriter = ctx.forge_writer
    marker = question_marker(item.task_id, str(item.wait.get("attempt_id") or ""))
    found = writer.find_comment(item.thread, marker, token, login=item.run.forge_login)
    posted = found or writer.create_comment(
        item.thread, render_questions(item.task_id, item.wait,
                                      _owner_login(ctx.db, item.tenant_id, item.doc.get("submitted_by"))),
        token)
    comment = {"id": posted.id, "url": posted.url, "login": posted.login,
               "thread": item.thread.short, "posted_at": ctx.now()}
    return _patch_wait(ctx.db, item, lambda record: record.update(comment=comment))


def _verify_answerer(ctx: Any, item: QuestionItem, comment: Comment, own_logins: set[str]) -> Any:
    """The member's AuthContext when the §4.9 checks and the answer rule hold; Refused otherwise."""
    from .agent_questions import answer_refusal

    if comment.user_type != "User" or comment.login.lower().endswith("[bot]"):
        raise Refused("a bot's comment answers nothing")
    if comment.via_app:
        raise Refused("a comment written through an app answers nothing")
    if comment.login.strip().lower() in own_logins:
        raise Refused("this account writes SwarmCloud's own comments, so it is not a member's answer")
    if not comment.created_at or comment.updated_at != comment.created_at:
        raise Refused("an edited comment answers nothing")
    email = member_for(ctx.db, item.tenant_id, comment.login, comment.user_id)
    if email is None:
        raise Refused("no member of this workspace has connected this GitHub account in onboarding")
    try:
        auth = _member_auth(ctx, item.tenant_id, email, comment.id)
    except _NotMember:
        raise Refused("the member this GitHub account belongs to is no longer a member of the workspace") from None
    except UpstreamUnavailable:
        raise Refused("membership could not be asked of the directory", retry=True) from None
    _require_member(ctx, auth)
    refusal = answer_refusal(item.doc.get("submitted_by"), auth.email, is_admin=auth.is_admin)
    if refusal is not None:
        raise Refused(refusal)
    return auth


def answer_from_comment(ctx: Any, item: QuestionItem, comment: Comment, own_logins: set[str]) -> bool:
    """One reply: True when it answered; False when it is no `/answer`; Refused otherwise."""
    from .agent_questions import AnswerBody, _redacted_question, answer_question
    from .agent_output import AgentOutputService

    parsed = parse_answer(comment.body)
    if parsed is None:
        return False
    number, option = parsed
    auth = _verify_answerer(ctx, item, comment, own_logins)
    questions = item.wait.get("questions") or []
    if not 1 <= number <= len(questions):
        raise Refused(f"question {number} is not one of this agent's {len(questions)} question(s)")
    # The labels as served and posted (redacted), which `answer_question`
    # checks the choice against too: a label redaction masks is refused here,
    # for this reason, rather than by the store with a different one.
    entry = questions[number - 1]
    served = _redacted_question(dict(entry))[0]["options"] if isinstance(entry, Mapping) else []
    labels = [o["label"] for o in served]
    if option not in labels:
        raise Refused(f"the option is not one of question {number}'s labels")
    try:
        answer_question(
            ctx.store, AgentOutputService(ctx.inspection), ctx.db, item.tenant_id, item.task_id,
            AnswerBody(question_index=number - 1, choice=option, via=ANSWER_VIA),
            submitted_by=None, email=auth.email, is_admin=auth.is_admin, now=ctx.now(),
        )
    except UpstreamUnavailable:
        raise Refused("the answer could not be stored now", retry=True) from None
    except ApiError as exc:
        # Our own refusal sentences (the label, the history cap): constant text.
        raise Refused(exc.message) from None
    log.info("agent question answered by github comment task=%s tenant=%s comment=%s",
             item.task_id, item.tenant_id, comment.id)
    return True


def _ignore(ctx: Any, item: QuestionItem, comment: Comment, reason: str) -> None:
    """`answer_comment_ignored`: a log line and an entry on the wait; never acted on."""
    entry = {"comment_id": comment.id, "login": comment.login[:100], "reason": reason[:300],
             "at": ctx.now()}
    log.info("%s task=%s tenant=%s comment=%s login=%s reason=%s", ANSWER_IGNORED, item.task_id,
             item.tenant_id, comment.id, comment.login[:100], reason[:300])

    def patch(record: dict[str, Any]) -> None:
        ignored = [e for e in record.get("ignored") or [] if isinstance(e, Mapping)]
        record["ignored"] = (ignored + [entry])[-MAX_IGNORED:]

    _patch_wait(ctx.db, item, patch)


def poll_question_thread(ctx: Any, item: QuestionItem, token: str) -> dict[str, int]:
    """Post the questions once, then read the issue's new replies from the wait's cursor."""
    counts = {"posted": 0, "answered": 0, "ignored": 0}
    had = isinstance(_recorded(item).get("comment"), Mapping)
    if not _post_questions(ctx, item, token):
        return counts  # the wait moved on meanwhile
    counts["posted"] += 0 if had else 1
    record = _recorded(item)
    cursor = record.get("cursor") if isinstance(record.get("cursor"), Mapping) else {}
    last_id = int(cursor.get("last_comment_id") or 0)
    since = _aware(cursor.get("since")) or _aware(item.wait.get("asked_at"))
    posted_id = (record.get("comment") or {}).get("id") if isinstance(record.get("comment"), Mapping) else None
    own_logins = {str(x).strip().lower() for x in (
        item.run.forge_login, (record.get("comment") or {}).get("login")
        if isinstance(record.get("comment"), Mapping) else None) if x}
    writer: GitHubWriter = ctx.forge_writer
    for comment in _list_comments(writer, item.thread, token, since):
        if comment.id <= last_id or comment.id == posted_id:
            continue
        try:
            answered = answer_from_comment(ctx, item, comment, own_logins)
        except Refused as refused:
            if refused.retry:
                break
            last_id, since = comment.id, _aware(comment.created_at) or since
            _ignore(ctx, item, comment, refused.message)
            counts["ignored"] += 1
            continue
        last_id, since = comment.id, _aware(comment.created_at) or since
        if answered:
            counts["answered"] += 1
    moved = {"last_comment_id": last_id, "since": since}
    _patch_wait(ctx.db, item, lambda r: r.update(cursor=moved))
    return counts


def poll_answers(ctx: Any, *, out_of_time: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """The tick's question poll: expire 72-hour waits, then post and read. Never raises."""
    from .agent_questions import expire_question_waits

    counts = {"question_threads": 0, "questions_posted": 0, "answered": 0, "answers_ignored": 0,
              "question_waits_expired": 0, "question_errors": 0}
    try:
        counts["question_waits_expired"] = expire_question_waits(ctx.db, ctx.now())
        items = question_candidates(ctx)
    except Exception as exc:  # noqa: BLE001
        log.warning("agent questions: the waits could not be read (%s)", type(exc).__name__)
        counts["question_errors"] += 1
        return counts
    tokens: dict[str, str | None] = {}
    for item in items[:QUESTION_THREADS_PER_TICK]:
        if out_of_time():
            break
        try:
            if item.tenant_id not in tokens:
                tenant = ctx.store.get_tenant(item.tenant_id)
                tokens[item.tenant_id] = ctx.forge_tokens.token_for(tenant) if tenant is not None else None
            token = tokens[item.tenant_id]
            if not token:
                continue
            counts["question_threads"] += 1
            got = poll_question_thread(ctx, item, token)
            counts["questions_posted"] += got["posted"]
            counts["answered"] += got["answered"]
            counts["answers_ignored"] += got["ignored"]
        except Exception as exc:  # noqa: BLE001 -- one wait's failure does not stop the rest
            counts["question_errors"] += 1
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            tokens.setdefault(item.tenant_id, None)
            log.warning("agent questions: task %s tenant=%s not polled (%s)", item.task_id,
                        item.tenant_id, code)
    tokens.clear()
    return counts


# --------------------------------------------------------------------------
# The merge request (issueci.merge_gate calls this)
# --------------------------------------------------------------------------


def announce_merge(ctx: Any, run: IssueRun, record: Mapping[str, Any], token: str) -> None:
    """Post the merge request on the pull request once per `merge` record (§4.9). Never raises."""
    approval_id = str(record.get("approval_id") or "")
    if not approval_id or record.get("request_comment_id") or record.get("state") != approvals.PENDING:
        return
    digest = record.get("digest") or {}
    thread = _pr_ref(run, (record.get("subject") or {}).get("pr") or (run.pull_request or {}).get("number"))
    if thread is None or not digest.get("head_sha"):
        return
    writer: GitHubWriter = ctx.forge_writer
    try:
        found = writer.find_comment(thread, approval_marker(approval_id), token, login=run.forge_login)
        posted = found or writer.create_comment(
            thread, render_merge_request(approval_id, run, str(digest["head_sha"]), digest.get("verdict")), token)
        ctx.db.collection(approvals.COLLECTION).document(approval_id).update(
            {"request_comment_id": posted.id, "request_comment_login": posted.login})
    except (ApiError, ForgeWriteNotFound) as exc:
        log.warning("approval %s: merge request not posted on %s (%s)", approval_id, thread.short,
                    getattr(exc, "code", type(exc).__name__))
    except Exception as exc:  # noqa: BLE001 -- the comment is not the merge gate
        log.warning("approval %s: merge request not posted (%s)", approval_id, type(exc).__name__)


__all__ = [
    "ANSWER_COMMAND",
    "ANSWER_IGNORED",
    "COMMAND",
    "CURSORS",
    "VIA",
    "announce_merge",
    "approval_marker",
    "candidates",
    "command_text",
    "handle",
    "member_for",
    "parse_answer",
    "parse_command",
    "poll_answers",
    "pick",
    "poll",
    "poll_thread",
    "render_merge_request",
    "render_questions",
]
