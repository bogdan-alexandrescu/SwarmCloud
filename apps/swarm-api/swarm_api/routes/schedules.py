"""Schedule routes: the tenant's own, and the admin list and actions (docs/schedules.md §7.1, lane S3).

    GET    /v1/schedule-types                      the catalogue, as the caller may create it
    GET    /v1/schedules                           the tenant's schedules
    POST   /v1/schedules                           create (`client_request_id` makes a repeat idempotent)
    POST   /v1/schedules:preview                   cron in words and the next five (§6.3)
    GET    /v1/schedules/{id}                      one schedule, with its last 50 firings
    PATCH  /v1/schedules/{id}                      edit, carrying `revision`
    DELETE /v1/schedules/{id}?confirm=<name>       delete, with the typed name
    POST   /v1/schedules/{id}:pause | :resume | :run | :take-ownership
    POST   /v1/schedules/{id}:pause {cancel_live: true, confirm}   and cancel the live runs (§2.11)
    POST   /v1/schedules:pause-all {confirm}       every enabled schedule of the caller's tenant (§2.11)
    GET    /v1/schedules/{id}/firings[/{firing_id}]
    GET    /v1/schedules/{id}/spans?firing_id=…    one page of firings' work and attempt spans
    GET    /v1/schedules/{id}/audit                newest first
    GET    /v1/admin/schedules                     every tenant's, with the tick's health (§5.3)
    POST   /v1/admin/schedules/{id}:pause | :disable | :enable
    POST   /v1/admin/tenants/{tenant_id}/schedules:pause {confirm}   §5.3's pause-all

NOT HERE: approvals and the SD3 merge switch (`:merge-mode`) are lane S5's;
the tick (`POST /v1/admin/schedules/tick`) is `routes/schedule_tick.py`. A
`PATCH` asking for `gate.merge: auto` is refused 422 `use_merge_switch` by
`schedules.resolve_gate`, so the switch's audit cannot be bypassed here.

WHO MAY WRITE (docs/design/workspace-roles.md §4.1, §4.8; lane
ROLES-GATES-AUTOMATE). Every write here -- create, edit, delete, pause,
pause-all, resume, take-ownership, run -- needs the role `member` in the
selected workspace (`_member`), checked before anything is read or written. A
`viewer` is `role_member_required`, a NEW refusal on existing routes, so it
goes through its switch: off, the write goes through and the log names who
would have been refused. The preview and every read stay open to a viewer.
The firing itself checks its OWNER's role (`schedulefire.owner_acts_as`).

A PLATFORM-SCOPE SCHEDULE IS A PLATFORM ADMIN'S (BGD, 2026-10-24, "Platform
admins only"; PLATFORM-SCHED-ADMIN, part of #2157). One whose stored
`scope.mode` is `platform` reads every workspace's aggregates, so only a
platform admin may create one (`schedules.check_creator`) -- and, by that
decision, change one: edit, delete, pause, resume, run, take-ownership and
lane S5's merge switch are 403 `platform_admin_required` to anyone else,
workspace admin included (`_platform_only`), and pause-all leaves such a
schedule as it is for them, listed under `admin_required`. The check reads
the STORED schedule, before the write and again in its transaction, so a
body naming another scope cannot dodge it and a scope moved to platform
between the two reads is still caught. It is raised, not a report-only
switch: report-only would be the very write the decision forbids, and only
platform admins create these schedules, so no ordinary flow depends on the
write going through. Reads stay open; a workspace-scope schedule is managed
exactly as before.

TENANT-SCOPED (§5.1, invariant 9). The tenant on every route comes from
`tenant_scope` (a read) or `tenant_for` (a create), never from the body:
`ScheduleCreate` refuses `tenant_id` and `owner` by name. Every read compares
the stored `tenant_id` with the caller's and answers a mismatch with the same
404 as a missing id, so a schedule id is never an oracle for another tenant.
Firings and audit entries are filtered by tenant again in the application,
as `IssueRuns.list` does, so an index mistake cannot move the boundary.

BY NAME (invariant 10). A caller names a `type` from the catalogue and the
request models refuse every other key by name -- `image`, `command`,
`profile`, `resources`, `backend` included. The profile a type's work runs is
named in code (`scheduletypes`), and the work is created by the tick through
the ordinary `SubmissionService` paths.

NOTHING HERE CREATES DEMAND (invariants 1-3). A schedule, a firing, a queued
or awaiting-approval firing and an audit entry are Firestore documents.
`:run` hands the tick's own `Ticker.fire_now` a `run_now` firing, which
submits through `SubmissionService`; that work is QUEUED/READY until
admission leases it in the one all-pools transaction. No route here reads,
reserves or waits for capacity.

ONE WRITER PER TENANT AT A TIME. Creates, renames and deletes also write
`schedule_tenants/{tenant_id}`, read in the same transaction, so two creates
racing cannot both pass the 25-per-tenant limit or both take one name: the
second one's transaction retries and sees the first. The same document holds
the `client_request_id`s of the last 24 hours (§7.1) as hashes, and the
per-tenant limit an admin raised, when one did.

THE AUDIT (§4.8) is written in the transaction that makes the change, as
`{schedule_id, tenant_id, action, by, at, detail}`, `by` an email or
`admin:<email>`. Lane S5's `schedaudit` module will own the collection; the
entries written here are already in its documented shape. An entry's id
sorts newest first (`{schedule_id}:{inverted ms}:{random}`), so the audit
route reads it with an equality filter ordered by `__name__` -- the shape
the automatic single-field index serves, needing no composite index.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from itertools import chain
from typing import Any, Iterable, Mapping

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES

from .. import approvals, cronexpr, roles, schedulefire, schedules, scheduletypes
from ..attempt_totals import totals_for
from ..auth import AuthContext
from ..deps import AppContext, admin_auth, current_auth, get_context, paged_limit, tenant_scope
from ..errors import Conflict, NoClaudeAccount, NotFound, ValidationFailed, WorkspaceNotReady
from ..issueruns import RUNS_COLLECTION, TERMINAL_RUN_STATES, IssueRuns, RunState
from ..repositories import COLLECTION as REPOSITORIES
from ..repositories import Repositories, platform_of
from ..schedules import ScheduleConflict, ScheduleForbidden, ScheduleInvalid
from ..store import CancelContended
from . import tasks as task_routes
from . import workflows as workflow_routes

log = logging.getLogger(__name__)

router = APIRouter(tags=["schedules"])

# --------------------------------------------------------------------------
# Constants. Each is a decision, with its reason.
# --------------------------------------------------------------------------

TENANTS = "schedule_tenants"
AUDIT = "schedule_audit"

#: §7.1: a repeated `client_request_id` within 24 h returns the first schedule.
REQUEST_WINDOW = timedelta(hours=24)

#: §7.1: `GET /v1/schedules/{id}` carries its last 50 firings.
HISTORY = 50

#: `run_now` firings have no slot, so the slot-ordered history query (S4's
#: index, `schedule_id, slot desc`) puts them last. They are read by their own
#: equality query, at most this many, and merged by `fired_at`.
RUN_NOW_SCAN = 200

#: THE HISTORY TIMELINE'S PAGE (#1109, SCHED-HISTORY-TIMELINE). schedules.html
#: detail A's risk note: the timeline needs every firing's work and its
#: attempts' spans, one read per firing page, and that cost decides the design.
#: So `/spans` takes the firing ids of ONE page the History tab draws, at most
#: this many, and never reads a firing it was not asked for.
SPAN_FIRINGS = 10

#: The tasks one `/spans` call reads, across its firings: each costs an
#: attempts query. A page whose work is bigger serves the rest as unread, with
#: that reason, rather than reading without bound.
SPAN_TASKS = 100

#: Attempts read per task, newest first. A task that retried more than this
#: says so (`attempts_truncated`); max_attempts is 3 by default.
SPAN_ATTEMPTS = 20

#: Awaiting-approval firings read for the pending counts. They are documents
#: a person clears; more than this many pending is itself the news.
PENDING_SCAN = 1000

#: The tenant's schedules read for a list or a uniqueness check: above any
#: limit an admin would raise to, so the read is never the thing that bounds.
TENANT_SCAN = 1000

#: §5.3's tick health: the last hour of `schedule_ticks/{unix_minute}`, read
#: by id in one batched get -- no query and no index.
TICK_HEALTH_MINUTES = 60

#: Audit ids sort newest first: this minus the entry's Unix milliseconds,
#: zero-padded. 10**13 ms is the year 2286.
_AUDIT_EPOCH_MS = 10**13

_ID = re.compile(r"^sch_[0-9a-f]{12}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: What a firing serves. The finisher's lease token and its private record of
#: half-made work are the tick's own bookkeeping.
_FIRING_PRIVATE = ("finisher", "finishing_until", "recorded", "dry_run_requested")


# --------------------------------------------------------------------------
# Request bodies of the verbs. Extra keys are refused by name.
# --------------------------------------------------------------------------


class _Verb(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PreviewIn(_Verb):
    cron: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    type: str | None = Field(default=None, min_length=1, max_length=64)


class ReasonIn(_Verb):
    reason: str | None = Field(default=None, max_length=500)


class PauseIn(ReasonIn):
    #: §2.11 "pause and cancel live runs": `confirm` is then the schedule's name.
    cancel_live: StrictBool = False
    confirm: str | None = Field(default=None, max_length=200)


class PauseAllIn(ReasonIn):
    #: The tenant id, typed (§7.1).
    confirm: str = Field(min_length=1, max_length=200)


class RunIn(_Verb):
    dry_run: StrictBool = False


def _verb(model: type[BaseModel], body: Mapping[str, Any] | None) -> Any:
    try:
        return model.model_validate(body or {})
    except ValidationError as exc:
        raise ScheduleInvalid("invalid_request", "the request is not valid", errors=schedules._errors(exc)) from None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _now(ctx: AppContext) -> datetime:
    now = ctx.now()
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _aware(moment: Any) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _json(value: Any) -> Any:
    """Timestamps as ISO 8601 in UTC, everywhere in a document."""
    if isinstance(value, datetime):
        return _aware(value).astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _not_found(schedule_id: str) -> NotFound:
    # One sentence for "no such schedule" and "another tenant's" (§5.1). The
    # id is the caller's text: quoted by repr, bounded.
    return NotFound(f"schedule {(schedule_id or '')[:64]!r} not found")


def _schedule_ref(ctx: AppContext, schedule_id: str) -> Any:
    return ctx.db.collection(schedules.COLLECTION).document(schedule_id)


def _tenant_ref(ctx: AppContext, tenant_id: str) -> Any:
    return ctx.db.collection(TENANTS).document(tenant_id)


def _owned(snap: Any, tenant_id: str | None, schedule_id: str) -> dict[str, Any]:
    """The document, or the 404 a missing one gets. `tenant_id=None`: an admin's read."""
    doc = snap.to_dict() if snap.exists else None
    if doc is None or (tenant_id is not None and doc.get("tenant_id") != tenant_id):
        raise _not_found(schedule_id)
    return doc


def _read(ctx: AppContext, tenant_id: str | None, schedule_id: str) -> dict[str, Any]:
    if not _ID.match(schedule_id or ""):
        raise _not_found(schedule_id)
    return _owned(_schedule_ref(ctx, schedule_id).get(), tenant_id, schedule_id)


def _member(ctx: AppContext, auth: AuthContext) -> None:
    """The role gate on every write here (workspace-roles.md §4.8):
    `role_member_required` for a `viewer`, through its switch."""
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.MEMBER)


def _platform_scope(doc: Mapping[str, Any]) -> bool:
    return (doc.get("scope") or {}).get("mode") == "platform"


def _platform_only(auth: AuthContext, doc: Mapping[str, Any], action: str) -> None:
    """403 `platform_admin_required` when the STORED `doc` is platform-scope and
    the caller is not a platform admin (BGD, 2026-10-24). Logged with who asked,
    as `RoleStore.require` logs its refusals; nothing has been written yet."""
    if not _platform_scope(doc) or auth.is_admin or auth.is_owner:
        return
    log.warning("refusal code=platform_admin_required tenant=%s email=%s schedule=%s action=%s",
                doc.get("tenant_id"), auth.email, doc.get("schedule_id"), action)
    raise ScheduleForbidden(
        "platform_admin_required",
        "only a platform admin may change a platform-scope schedule: it reads every workspace's aggregates",
    )


def _audit(txn: Any, ctx: AppContext, doc: Mapping[str, Any], action: str, by: str, at: datetime,
           detail: Mapping[str, Any] | None = None) -> None:
    """One `schedule_audit` entry, in `txn` (§4.8). Append-only: never updated."""
    ms = int(at.timestamp() * 1000)
    entry_id = f"{doc['schedule_id']}:{_AUDIT_EPOCH_MS - ms:013d}:{secrets.token_hex(4)}"
    txn.set(ctx.db.collection(AUDIT).document(entry_id), {
        "schedule_id": doc["schedule_id"],
        "tenant_id": doc["tenant_id"],
        "action": action,
        "by": by,
        "at": at,
        "detail": dict(detail or {}),
    })


def _bump_tenant(txn: Any, ref: Any, tenant_doc: Mapping[str, Any], tenant_id: str, now: datetime,
                 request: tuple[str, str] | None = None) -> None:
    """Write the tenant's serialisation document; prune requests older than 24 h."""
    requests = {
        key: value for key, value in dict(tenant_doc.get("requests") or {}).items()
        if (_aware(value.get("at")) or _EPOCH) > now - REQUEST_WINDOW
    }
    if request is not None:
        requests[request[0]] = {"schedule_id": request[1], "at": now}
    txn.set(ref, {
        **dict(tenant_doc),
        "tenant_id": tenant_id,
        "writes": int(tenant_doc.get("writes") or 0) + 1,
        "requests": requests,
    })


def _request_key(client_request_id: str) -> str:
    # Hashed: the caller's text is any string, and a map key must be safe.
    return hashlib.sha256(client_request_id.encode()).hexdigest()[:32]


def _tenant_schedules(ctx: AppContext, tenant_id: str, txn: Any = None) -> list[dict[str, Any]]:
    query = (
        ctx.db.collection(schedules.COLLECTION)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .limit(TENANT_SCAN)
    )
    snaps = txn.get(query) if txn is not None else query.stream()
    rows = [snap.to_dict() or {} for snap in snaps]
    return [row for row in rows if row.get("tenant_id") == tenant_id]


def _registrations(ctx: AppContext, tenant_id: str, scope: schedules.ScopeIn | None) -> tuple[list[str], bool]:
    """(the tenant's registrations the scope may name, whether any is `platform: true`).

    `repos`: the named ids that are THIS tenant's (another tenant's id reads
    as unregistered, §5.4). `all`: every registration now -- only the platform
    flag matters, `all` resolves at firing time. `platform`: none.
    """
    if scope is None or scope.mode == "platform":
        return [], False
    if scope.mode == "repos":
        ids = list(dict.fromkeys(scope.repo_ids))[: schedules.MAX_SCOPE_REPOS + 1]
        if not ids:
            return [], False
        refs = [ctx.db.collection(REPOSITORIES).document(repo_id) for repo_id in ids]
        found = [
            snap.to_dict() or {} for snap in ctx.db.get_all(refs)
            if snap.exists and (snap.to_dict() or {}).get("tenant_id") == tenant_id
        ]
        return [str(row.get("repo_id")) for row in found], any(platform_of(row) for row in found)
    repos = Repositories(ctx.db, now=ctx.now)
    platform = False
    token: str | None = None
    while True:
        page, token = repos.list(tenant_id, limit=100, page_token=token)
        platform = platform or any(platform_of(row) for row in page)
        if not token:
            return [], platform


def _check_approvers(ctx: AppContext, tenant: Any, gate: Mapping[str, Any],
                     before: Mapping[str, Any] | None = None) -> None:
    """§4.6: each named approver is a member NOW, asked of the directory."""
    names = gate.get("approvers")
    if not isinstance(names, list):
        return
    if before is not None and before.get("approvers") == names:
        return
    outside = [name for name in names if not ctx.authenticator.is_tenant_member(name, tenant)]
    if outside:
        raise ScheduleInvalid("approver_not_member", "each named approver must be a member of this tenant",
                              approvers=outside)


def _check_grants(ctx: AppContext, doc: Mapping[str, Any], owner: str) -> None:
    """§5.4: a type that pushes needs the owner's `write` grant on each repository.

    The same grant read a submission makes (`SubmissionService._grant_mode`),
    made early so the form can say so; with REPOSITORY_GRANTS_ENFORCED off a
    submission runs without one, and so does this. `all` resolves at firing
    time, where a repository without the grant is skipped.
    """
    if not getattr(ctx.settings, "repository_grants_enforced", False):
        return
    entry = scheduletypes.get(doc["type"])
    if entry is None or doc["scope"]["mode"] != "repos" or not schedules.pushes(entry, doc["params"]):
        return
    missing = [
        repo_id for repo_id in doc["scope"]["repo_ids"]
        if ctx.submissions._grant_mode(doc["tenant_id"], owner, repo_id) != "write"
    ]
    if missing:
        raise ScheduleForbidden(
            "repository_not_granted",
            f"{doc['type']} pushes, and {owner} holds no write grant on these repositories",
            repo_ids=missing,
        )


def _check_file_issues(ctx: AppContext, doc: Mapping[str, Any], owner: str) -> None:
    """§3.4: an observer that files issues names a registered repository its owner can
    write (owner decision 2026-10-11). The firing checks it again before it writes."""
    if doc.get("type") != "observer":
        return
    # Imported here: schedtypes/__init__.py keeps an executor out of every
    # import but its own type's.
    from ..schedtypes import observer

    observer.check_file_issues(ctx, {**doc, "owner": owner})


def _check_qa_target(ctx: AppContext, doc: Mapping[str, Any], auth: AuthContext) -> None:
    """§3.17: a visual-qa schedule names a QA target of its own tenant, and its
    creator or editor administers that tenant. The firing reads the target again."""
    if doc.get("type") != "visual-qa":
        return
    # Imported here, as `_check_file_issues` imports its type's executor.
    from ..schedtypes import visual_qa

    visual_qa.check_target(ctx, doc, auth)


def _words(cron: str | None) -> str | None:
    try:
        return cronexpr.words(cronexpr.parse(cron or ""))
    except cronexpr.CronError:
        return None


def _tier(doc: Mapping[str, Any]) -> str | None:
    entry = scheduletypes.get(str(doc.get("type")))
    if entry is None:
        return None
    try:
        return schedules.risk_tier(entry, doc["gate"], doc["params"])
    except (KeyError, ValidationError):
        return None


def _spend_today(doc: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Today's reported spend, in the schedule's zone, with what it does not cover (§4.3)."""
    try:
        day = schedules.spend_day(now, doc.get("timezone") or "UTC")
    except ScheduleInvalid:
        day = now.date().isoformat()
    spend = schedules.spend_for(doc.get("spend"), day)
    unreported = int(spend.get("unreported_attempts") or 0)
    return {
        "day": day,
        "reported_usd": float(spend.get("reported_usd") or 0.0),
        "unreported_attempts": unreported,
        # An attempt whose cost was never reported is not zero: "partial"
        # says the figure is a floor (the console's `Mark kind="partial"`).
        "coverage": "complete" if unreported == 0 else "partial",
    }


def _budget_use(doc: Mapping[str, Any], now: datetime, live: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Today's burn as the tick's budget gate counts it (§4.3), for the console's bar.

    Four parts of `per_day_usd`: what was reported; each attempt that never
    reported a cost, counted at `per_run_usd` (an unknown cost is the cap,
    never zero); each live work item, reserved at `per_run_usd`; and what is
    left. `live` is the schedule's `created` firings, and every work item they
    made counts -- `Ticker._finish` sums exactly that, so this is the
    reservation the next firing's gate would see, short of whatever the tick's
    next advance ends. None when the budget lacks a figure to count against.
    """
    budget = doc.get("budget") or {}
    if budget.get("per_run_usd") is None or budget.get("per_day_usd") is None:
        return None
    spend = _spend_today(doc, now)
    live_items = sum(len(f.get("work") or []) for f in live)
    unreported = int(spend["unreported_attempts"])
    unreported_usd = schedules.reserved_usd(budget, live_items=0, unreported_attempts=unreported)
    live_usd = schedules.reserved_usd(budget, live_items=live_items, unreported_attempts=0)
    cents = round(float(budget["per_day_usd"]) * 100) - round(spend["reported_usd"] * 100) \
        - round(unreported_usd * 100) - round(live_usd * 100)
    return {
        "day": spend["day"],
        "per_day_usd": float(budget["per_day_usd"]),
        "per_run_usd": float(budget["per_run_usd"]),
        "reported_usd": spend["reported_usd"],
        "unreported_attempts": unreported,
        "unreported_usd": unreported_usd,
        "live_firings": len(live),
        "live_items": live_items,
        "live_usd": live_usd,
        # Never negative: a day can end above `per_day_usd` (§4.3), and then
        # nothing is free; the console draws the parts past the day's figure.
        "free_usd": max(0, cents) / 100,
    }


def schedule_to_api(doc: Mapping[str, Any], now: datetime, *, pending: int = 0,
                    live: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The stored fields and what is computed on read. `live` (the schedule's
    `created` firings) is read only by the one-schedule route, which then
    serves `budget_use`; the list does not scan each schedule's firings.
    `pending` is how many waiting approvals name the schedule (`_waiting`),
    and `state_word` is `schedules.state_word`'s, from that same count."""
    out = _json(dict(doc))
    out["words"] = _words(doc.get("cron"))
    out["tier"] = _tier(doc)
    out["spend_today"] = _spend_today(doc, now)
    out["pending_approvals"] = pending
    out["state_word"] = schedules.state_word(doc, waiting=pending)
    if live is not None:
        out["budget_use"] = _budget_use(doc, now, live)
    return out


def firing_to_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    return _json({k: v for k, v in doc.items() if k not in _FIRING_PRIVATE})


def _inbox(ctx: AppContext, tenant_id: str) -> list[dict[str, Any]] | None:
    """The Approvals tab's own inbox (`approvals.inbox`, oldest first), so a
    count here is the count there. None when it cannot be read: the schedules
    are still listed, and the summary says what it could not count."""
    try:
        return approvals.inbox(ctx, tenant_id)
    except Exception as exc:  # one read's failure does not blank the list
        log.warning("schedules: the approvals inbox of %s not read: %s", tenant_id, type(exc).__name__)
        return None


def _waiting(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Waiting approvals per schedule: every inbox item whose subject names
    one -- a firing waiting to run, a scheduled run's plan or hold, and its
    pull request's merge (`approvals.request`, `approvals.project_run`)."""
    counts: dict[str, int] = {}
    for item in items:
        sid = (item.get("subject") or {}).get("schedule_id")
        if sid:
            counts[str(sid)] = counts.get(str(sid), 0) + 1
    return counts


def _pending(ctx: AppContext, tenant_id: str | None = None) -> dict[str, int]:
    """Awaiting-approval firings per schedule: a count, never their content."""
    query = ctx.db.collection(schedulefire.FIRINGS).where(
        filter=FieldFilter("state", "==", schedulefire.AWAITING_APPROVAL)
    )
    if tenant_id is not None:
        query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
    counts: dict[str, int] = {}
    for snap in query.limit(PENDING_SCAN).stream():
        row = snap.to_dict() or {}
        if tenant_id is not None and row.get("tenant_id") != tenant_id:
            continue
        sid = str(row.get("schedule_id"))
        counts[sid] = counts.get(sid, 0) + 1
    return counts


def _history(ctx: AppContext, tenant_id: str, schedule_id: str, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` firings: by slot (S4's index), merged with the run-now ones."""
    col = ctx.db.collection(schedulefire.FIRINGS)
    by_slot = (
        col.where(filter=FieldFilter("schedule_id", "==", schedule_id))
        .order_by("slot", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    run_now = (
        col.where(filter=FieldFilter("schedule_id", "==", schedule_id))
        .where(filter=FieldFilter("trigger", "==", "run_now"))
        .limit(RUN_NOW_SCAN)
    )
    rows: dict[str, dict[str, Any]] = {}
    for snap in chain(by_slot.stream(), run_now.stream()):
        row = snap.to_dict() or {}
        if row.get("tenant_id") == tenant_id and row.get("schedule_id") == schedule_id:
            rows[str(row.get("firing_id"))] = row

    def newest(row: Mapping[str, Any]) -> tuple[datetime, datetime]:
        fired = _aware(row.get("fired_at")) or _EPOCH
        return fired, _aware(row.get("slot")) or fired

    return sorted(rows.values(), key=newest, reverse=True)[:limit]


def _may(role: str, auth: AuthContext) -> bool:
    if role == "owner":
        return auth.is_owner
    if role == "admin":
        return auth.is_admin or auth.is_owner
    return True


def _workspace_hold(ctx: AppContext, auth: AuthContext, tenant: Any) -> dict[str, str] | None:
    """§5.5: before a personal workspace is ready, a schedule is created PAUSED, with why."""
    try:
        ctx.submissions.workspace_gate(auth, tenant)
    except (WorkspaceNotReady, NoClaudeAccount) as exc:
        return {"code": exc.code, "reason": exc.message}
    return None


# --------------------------------------------------------------------------
# The catalogue and the preview
# --------------------------------------------------------------------------


@router.get("/v1/schedule-types")
def list_schedule_types(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
) -> dict:
    """§7.1: each type the caller may create, with the scopes they may create it in."""
    rows = []
    for entry in scheduletypes.TYPES:
        scopes = sorted(mode for mode, role in entry.creators.items() if _may(role, auth))
        if not scopes:
            continue
        rows.append({**scheduletypes.describe(entry, scheduletypes.EXECUTOR_DIR), "creatable_scopes": scopes})
    return {"types": rows, "tenant_id": tenant_id}


@router.post("/v1/schedules:preview")
def preview_schedule(
    body: dict[str, Any] | None = Body(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§6.3: from the tick's own parser, so the form and the tick cannot disagree."""
    request = _verb(PreviewIn, body)
    return {"preview": _json(schedules.preview(request.cron, request.timezone, _now(ctx), request.type))}


# --------------------------------------------------------------------------
# The tenant's schedules
# --------------------------------------------------------------------------


@router.get("/v1/schedules")
def list_schedules(
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The tenant's schedules, each with its `state_word`, and the `summary`
    Automate's landing leads with (`schedules.summarize`). Both count the
    waiting approvals from the Approvals tab's own inbox; when it cannot be
    read, each schedule falls back to its firings waiting to run."""
    now = _now(ctx)
    inbox = _inbox(ctx, tenant_id)
    pending = _waiting(inbox) if inbox is not None else _pending(ctx, tenant_id)
    rows = sorted(_tenant_schedules(ctx, tenant_id), key=lambda d: str(d.get("name", "")).casefold())
    listed = [schedule_to_api(row, now, pending=pending.get(row["schedule_id"], 0)) for row in rows]
    return {
        "schedules": listed,
        "summary": _json(schedules.summarize((row["state_word"] for row in listed), inbox)),
        "tenant_id": tenant_id,
    }


@router.post("/v1/schedules", status_code=status.HTTP_201_CREATED)
def create_schedule(
    response: Response,
    body: dict[str, Any] = Body(...),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§7.1 create. The tenant, owner and actor fields come from the verified token."""
    _member(ctx, auth)
    request = schedules.parse_create(body)
    # The create path's tenant resolution, as `POST /v1/runs` uses: it refuses
    # a disabled tenant and a secret admin's, and checks the principal.
    tenant = ctx.submissions.tenant_for(auth)
    tenant_id = tenant.tenant_id
    now = _now(ctx)
    hold = _workspace_hold(ctx, auth, tenant)
    if hold is not None and request.state == "enabled":
        request = request.model_copy(update={"state": "paused"})
    registered, platform_repository = _registrations(ctx, tenant_id, request.scope)
    schedule_id = schedules.new_schedule_id()

    def build(existing: list[dict[str, Any]], limit: int) -> dict[str, Any]:
        doc = schedules.build_schedule(
            request, tenant_id=tenant_id, actor=auth.email, now=now, registered_repo_ids=registered,
            existing_names=[str(row.get("name", "")) for row in existing], existing_count=len(existing),
            limit=limit, is_admin=auth.is_admin, is_owner=auth.is_owner, schedule_id=schedule_id,
            root=scheduletypes.EXECUTOR_DIR,
        )
        # `build_schedule` holds the creator rule without the repository's
        # `platform` flag; the registrations are read here, so it is held here.
        entry = scheduletypes.get(doc["type"])
        schedules.check_creator(entry, doc["scope"]["mode"], is_admin=auth.is_admin, is_owner=auth.is_owner,
                                platform_repository=platform_repository)
        if hold is not None:
            doc["pause"] = {"by": auth.email, "at": now, "reason": hold["reason"], "code": hold["code"]}
        return doc

    # Everything that reads outside Firestore's transaction first, against a
    # document built with no neighbours: a refusal costs no transaction.
    draft = build([], schedules.SCHEDULES_PER_TENANT)
    _check_approvers(ctx, tenant, draft["gate"])
    _check_grants(ctx, draft, auth.email)
    _check_file_issues(ctx, draft, auth.email)
    _check_qa_target(ctx, draft, auth)

    tenant_ref = _tenant_ref(ctx, tenant_id)
    key = _request_key(request.client_request_id) if request.client_request_id else None

    @firestore.transactional
    def _apply(txn: Any) -> tuple[dict[str, Any], bool]:
        tenant_doc = _snapshot(txn.get(tenant_ref)).to_dict() or {}
        if key is not None:
            seen = (tenant_doc.get("requests") or {}).get(key)
            if seen and (_aware(seen.get("at")) or _EPOCH) > now - REQUEST_WINDOW:
                first = _snapshot(txn.get(_schedule_ref(ctx, str(seen.get("schedule_id"))))).to_dict()
                if first and first.get("tenant_id") == tenant_id:
                    return first, False
        existing = _tenant_schedules(ctx, tenant_id, txn)
        limit = int(tenant_doc.get("limit") or schedules.SCHEDULES_PER_TENANT)
        doc = build(existing, limit)
        txn.set(_schedule_ref(ctx, schedule_id), doc)
        _bump_tenant(txn, tenant_ref, tenant_doc, tenant_id, now, (key, schedule_id) if key else None)
        _audit(txn, ctx, doc, "create", auth.email, now, {
            "type": doc["type"], "scope": doc["scope"], "cron": doc["cron"], "timezone": doc["timezone"],
            "gate": doc["gate"], "budget": doc["budget"], "params": doc["params"], "state": doc["state"],
        })
        return doc, True

    doc, created = _apply(ctx.db.transaction())
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/v1/schedules/{doc['schedule_id']}"
    log.info("schedule %s tenant=%s type=%s %s", doc["schedule_id"], tenant_id, doc["type"],
             "created" if created else "returned for a repeated client_request_id")
    return {"schedule": schedule_to_api(doc, now), "created": created}


@router.get("/v1/schedules/{schedule_id}")
def get_schedule(
    schedule_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    doc = _read(ctx, tenant_id, schedule_id)
    firings = _history(ctx, tenant_id, schedule_id, HISTORY)
    # The list's count, so the sheet's word is the list's word.
    inbox = _inbox(ctx, tenant_id)
    if inbox is not None:
        pending = _waiting(inbox).get(schedule_id, 0)
    else:
        pending = sum(1 for f in firings if f.get("state") == schedulefire.AWAITING_APPROVAL)
    # Every `created` firing, not only those among the newest 50: a live one
    # older than the history still holds its reservation.
    live = [f for f in schedulefire.Ticker(ctx)._of_schedule(schedule_id, [schedulefire.CREATED])
            if f.get("tenant_id") == tenant_id]
    return {
        "schedule": schedule_to_api(doc, _now(ctx), pending=pending, live=live),
        "firings": _firings_api(ctx, tenant_id, firings),
    }


#: The fields an edit's audit entry carries before and after (§4.8).
_EDIT_AUDITED = ("name", "gate", "budget", "scope", "cron", "timezone", "params", "policy", "state")


@router.patch("/v1/schedules/{schedule_id}")
def patch_schedule(
    schedule_id: str,
    body: dict[str, Any] = Body(...),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§7.1 edit, carrying `revision`; a stale one is 409 `schedule_changed`."""
    _member(ctx, auth)
    stored = _read(ctx, tenant_id, schedule_id)
    _platform_only(auth, stored, "edit")
    patch = schedules.parse_patch(body)
    registered, platform_repository = _registrations(ctx, tenant_id, patch.scope)
    now = _now(ctx)

    def edit(doc: Mapping[str, Any], existing: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        return schedules.apply_edit(
            doc, patch, actor=auth.email, now=now, registered_repo_ids=registered,
            existing_names=[str(row.get("name", "")) for row in existing if row.get("schedule_id") != schedule_id],
            is_admin=auth.is_admin, is_owner=auth.is_owner, platform_repository=platform_repository,
            root=scheduletypes.EXECUTOR_DIR,
        )

    draft = edit(stored, [])
    if draft["gate"].get("approvers") != stored["gate"].get("approvers"):
        tenant = ctx.store.get_tenant(tenant_id) or ctx.submissions.tenant_for(auth)
        _check_approvers(ctx, tenant, draft["gate"], stored["gate"])
    if draft["scope"] != stored["scope"] or draft["params"] != stored["params"]:
        _check_grants(ctx, draft, str(stored.get("owner") or ""))
        _check_file_issues(ctx, draft, str(stored.get("owner") or ""))
        _check_qa_target(ctx, draft, auth)

    ref = _schedule_ref(ctx, schedule_id)
    tenant_ref = _tenant_ref(ctx, tenant_id)

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        tenant_doc = _snapshot(txn.get(tenant_ref)).to_dict() or {}
        before = _owned(_snapshot(txn.get(ref)), tenant_id, schedule_id)
        _platform_only(auth, before, "edit")
        existing = _tenant_schedules(ctx, tenant_id, txn)
        doc = edit(before, existing)
        txn.set(ref, doc)
        _bump_tenant(txn, tenant_ref, tenant_doc, tenant_id, now)
        changed = {
            field: {"from": before.get(field), "to": doc.get(field)}
            for field in _EDIT_AUDITED if before.get(field) != doc.get(field)
        }
        _audit(txn, ctx, doc, "edit", auth.email, now, {"revision": doc["revision"], "changed": changed})
        return doc

    doc = _apply(ctx.db.transaction())
    return {"schedule": schedule_to_api(doc, now)}


@router.delete("/v1/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: str,
    confirm: str | None = Query(default=None, max_length=200),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§1.3: a hard delete, with the schedule's name typed. Firings and audit stay;
    work the schedule already made is ordinary work and is not cancelled."""
    _member(ctx, auth)
    stored = _read(ctx, tenant_id, schedule_id)
    _platform_only(auth, stored, "delete")
    if confirm is None or confirm.strip() != stored["name"]:
        raise ScheduleInvalid("confirmation_required", "type the schedule's name in `confirm` to delete it")
    now = _now(ctx)
    ref = _schedule_ref(ctx, schedule_id)
    tenant_ref = _tenant_ref(ctx, tenant_id)

    @firestore.transactional
    def _apply(txn: Any) -> None:
        tenant_doc = _snapshot(txn.get(tenant_ref)).to_dict() or {}
        doc = _owned(_snapshot(txn.get(ref)), tenant_id, schedule_id)
        _platform_only(auth, doc, "delete")
        txn.delete(ref)
        _bump_tenant(txn, tenant_ref, tenant_doc, tenant_id, now)
        _audit(txn, ctx, doc, "delete", auth.email, now, {"name": doc["name"], "type": doc["type"],
                                                         "revision": doc["revision"]})

    _apply(ctx.db.transaction())
    return {"deleted": schedule_id}


# --------------------------------------------------------------------------
# State moves: one transaction each, re-reading the state it moves from
# --------------------------------------------------------------------------


def _move(ctx: AppContext, tenant_id: str | None, schedule_id: str, *, by: str, action: str,
          change: Any) -> dict[str, Any]:
    """Re-read, `change(doc, now)` -> the update or None (nothing to do), write it
    with its audit entry. `tenant_id=None` is an admin's move."""
    if not _ID.match(schedule_id or ""):
        raise _not_found(schedule_id)
    now = _now(ctx)
    ref = _schedule_ref(ctx, schedule_id)

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        doc = _owned(_snapshot(txn.get(ref)), tenant_id, schedule_id)
        update = change(doc, now)
        if update is None:
            return doc
        update = {**update, "updated_by": by, "updated_at": now, "revision": int(doc.get("revision") or 0) + 1}
        after = {**doc, **update}
        txn.update(ref, update)
        _audit(txn, ctx, after, action, by, now, {
            "from": doc.get("state"), "to": after.get("state"), "revision": after["revision"],
            **({"reason": after["pause"].get("reason")} if after.get("pause") else {}),
            **({"owner": {"from": doc.get("owner"), "to": after.get("owner")}}
               if doc.get("owner") != after.get("owner") else {}),
        })
        return after

    return _apply(ctx.db.transaction())


def _paused(by: str, now: datetime, reason: str | None) -> dict[str, Any]:
    return {"state": "paused", "next_run_at": None,
            "pause": {"by": by, "at": now, "reason": reason or "paused", "code": None}}


def _enabled(doc: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Back to `enabled` from now: the next slot AFTER now, so a resume never
    replays the slots it was paused through (they were never claimed)."""
    next_slot, next_run = schedules.next_times(doc, now)
    return {"state": "enabled", "pause": None, "next_slot": next_slot, "next_run_at": next_run,
            # A resume after the consecutive-failure stop starts the count
            # again; otherwise the next failure would pause it at once.
            "consecutive_failures": 0}


@router.post("/v1/schedules/{schedule_id}:pause")
def pause_schedule(
    schedule_id: str,
    background: BackgroundTasks,
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§2.11: no new firings. Live work continues -- unless `cancel_live`, typed
    with the schedule's name, which also cancels the work of its live firings."""
    _member(ctx, auth)
    request = _verb(PauseIn, body)
    stored = _read(ctx, tenant_id, schedule_id)
    _platform_only(auth, stored, "pause")
    if request.cancel_live:
        if request.confirm is None or request.confirm != stored["name"]:
            raise ScheduleInvalid("confirmation_required",
                                  "type the schedule's name in `confirm` to cancel its live runs")

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        _platform_only(auth, doc, "pause")
        if doc["state"] == "disabled":
            if request.cancel_live:
                # Disabled already makes no firings; what is asked is the cancel.
                return None
            raise ScheduleConflict("schedule_disabled", "an admin disabled this schedule")
        if doc["state"] == "paused":
            return None
        return _paused(auth.email, now, request.reason)

    doc = _move(ctx, tenant_id, schedule_id, by=auth.email, action="pause", change=change)
    if not request.cancel_live:
        return {"schedule": schedule_to_api(doc, _now(ctx))}
    cancelled = _cancel_live(ctx, auth, tenant_id, doc, background)
    return {"schedule": schedule_to_api(doc, _now(ctx)), "cancelled": cancelled}


def _cancel_one(ctx: AppContext, auth: AuthContext, tenant_id: str, kind: str, item_id: str,
                background: BackgroundTasks) -> str:
    """One task or workflow through the platform's own cancel route body, so
    its execution stop, child cascade and wake happen exactly as there.

    `contended`: the cancel's transaction stayed contended through its
    retries (#1822), so the item is still live and pausing again with
    `cancel_live` finishes it. A task's route raises that refusal; a
    workflow's RETURNS it as the 503 response, after cancelling every step
    it could, so it is read off the response rather than caught."""
    try:
        if kind == "task":
            task_routes.cancel_task(item_id, background, tenant_id=tenant_id, auth=auth, ctx=ctx)
        else:
            answer = workflow_routes.cancel_workflow(item_id, background, tenant_id=tenant_id, auth=auth, ctx=ctx)
            if isinstance(answer, JSONResponse) and answer.status_code == CancelContended.status_code:
                return "contended"
    except CancelContended:
        return "contended"
    except Conflict:
        return "already_ended"
    except NotFound:
        return "not_found"
    return "cancel_requested"


def _run_targets(ctx: AppContext, tenant_id: str, run_id: str) -> list[tuple[str, str]]:
    """What an issue run has live: its planner while PLANNING, its workflow
    while RUNNING, its last CI-fix round while FIXING. The run itself ends
    `CANCELLED` on its ordinary advance when that work does."""
    try:
        run = IssueRuns(ctx.db, now=ctx.now).get(tenant_id, run_id)
    except NotFound:
        return []
    if run.state in TERMINAL_RUN_STATES:
        return []
    if run.state == RunState.PLANNING and run.planner_task_id:
        return [("task", run.planner_task_id)]
    if run.state == RunState.FIXING and run.ci_fix_workflows:
        return [("workflow", run.ci_fix_workflows[-1])]
    if run.workflow_id and run.state in (RunState.APPROVED, RunState.RUNNING, RunState.FIXING):
        return [("workflow", run.workflow_id)]
    return []


def _cancel_live(ctx: AppContext, auth: AuthContext, tenant_id: str, doc: Mapping[str, Any],
                 background: BackgroundTasks) -> list[dict[str, Any]]:
    """§2.11: cancel each work item of the schedule's `created` firings, in this
    tenant only, then audit what was asked and what each cancel did. The
    firings end `cancelled` on the tick's advance (§2.9), not here."""
    live = schedulefire.Ticker(ctx)._of_schedule(doc["schedule_id"], [schedulefire.CREATED])
    live = [f for f in live if f.get("tenant_id") == tenant_id]
    results: list[dict[str, Any]] = []
    for firing in live:
        for item in firing.get("work") or []:
            kind, item_id = str(item.get("kind")), str(item.get("id"))
            if kind in ("task", "workflow"):
                targets = [(kind, item_id)]
            elif kind == "issue_run":
                targets = _run_targets(ctx, tenant_id, item_id)
            else:
                targets = []  # an api_action ended when it was recorded
            outcome = [_cancel_one(ctx, auth, tenant_id, k, i, background) for k, i in targets]
            results.append({"kind": kind, "id": item_id, "firing_id": firing["firing_id"],
                            "result": outcome[0] if outcome else "already_ended"})
    now = _now(ctx)

    @firestore.transactional
    def _apply(txn: Any) -> None:
        _audit(txn, ctx, doc, "cancel_live", auth.email, now, {
            "firings": [f["firing_id"] for f in live], "items": results,
        })

    _apply(ctx.db.transaction())
    return results


def _pause_all(ctx: AppContext, tenant_id: str, *, by: str, action: str,
               reason: str | None, platform_admin: bool) -> dict[str, Any]:
    """§2.11: every `enabled` schedule of the tenant to `paused` with one reason,
    each through `_move` -- its own transaction re-reading its state, its own
    audit entry. Any other state is left as it is.

    A platform-scope schedule is a platform admin's (`_platform_only`): for
    anyone else it is left as it is and listed under `admin_required`, read
    from the stored schedule in its own transaction. The rest of the tenant's
    are still paused: refusing the whole brake over one schedule the caller
    may not touch would leave every other one running."""
    reason = reason or "all of the tenant's schedules were paused"
    paused: list[str] = []
    unchanged: list[str] = []
    refused: list[str] = []

    #: Set on every attempt, so a retried transaction reports what committed.
    moved: dict[str, str] = {}

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        if _platform_scope(doc) and not platform_admin:
            moved[doc["schedule_id"]] = "admin_required"
            return None
        moved[doc["schedule_id"]] = "paused" if doc["state"] == "enabled" else "unchanged"
        return _paused(by, now, reason) if moved[doc["schedule_id"]] == "paused" else None

    for row in _tenant_schedules(ctx, tenant_id):
        try:
            _move(ctx, tenant_id, row["schedule_id"], by=by, action=action, change=change)
        except NotFound:
            continue  # deleted since the list was read: nothing left to pause
        outcome = moved.get(row["schedule_id"])
        {"paused": paused, "admin_required": refused}.get(outcome or "", unchanged).append(row["schedule_id"])
    if refused:
        log.warning("refusal code=platform_admin_required tenant=%s by=%s schedules=%s action=%s",
                    tenant_id, by, ",".join(sorted(refused)), action)
    return {"tenant_id": tenant_id, "paused": sorted(paused), "unchanged": sorted(unchanged),
            "admin_required": sorted(refused)}


@router.post("/v1/schedules:pause-all")
def pause_all_schedules(
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§2.11: a member pauses every enabled schedule of their own tenant, typed
    with the tenant id. Live work continues."""
    _member(ctx, auth)
    request = _verb(PauseAllIn, body)
    if request.confirm != tenant_id:
        raise ScheduleInvalid("confirmation_required",
                              "type your tenant's id in `confirm` to pause all of its schedules")
    return _pause_all(ctx, tenant_id, by=auth.email, action="pause_all", reason=request.reason,
                      platform_admin=auth.is_admin or auth.is_owner)


@router.post("/v1/schedules/{schedule_id}:resume")
def resume_schedule(
    schedule_id: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§1.3: from `paused` or `auto_paused`. A `disabled` one only an admin re-enables."""
    _member(ctx, auth)

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        _platform_only(auth, doc, "resume")
        if doc["state"] == "disabled":
            raise ScheduleForbidden("admin_required", "an admin disabled this schedule; only an admin may re-enable it")
        if doc["state"] == "enabled":
            return None
        schedules.resolve_type(doc["type"], scheduletypes.EXECUTOR_DIR)
        return _enabled(doc, now)

    doc = _move(ctx, tenant_id, schedule_id, by=auth.email, action="resume", change=change)
    return {"schedule": schedule_to_api(doc, _now(ctx))}


@router.post("/v1/schedules/{schedule_id}:take-ownership")
def take_ownership(
    schedule_id: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§1.1: the caller becomes the member firings submit as. Audited."""
    _member(ctx, auth)
    stored = _read(ctx, tenant_id, schedule_id)
    _platform_only(auth, stored, "take_ownership")
    _check_grants(ctx, stored, auth.email)
    _check_file_issues(ctx, stored, auth.email)

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        _platform_only(auth, doc, "take_ownership")
        if doc.get("owner") == auth.email:
            return None
        return {"owner": auth.email}

    doc = _move(ctx, tenant_id, schedule_id, by=auth.email, action="take_ownership", change=change)
    return {"schedule": schedule_to_api(doc, _now(ctx))}


@router.post("/v1/schedules/{schedule_id}:run")
def run_schedule(
    schedule_id: str,
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§7.1: a `run_now` firing, under the gate's `run` point, the overlap and the
    budget -- the tick's own finish (`Ticker.fire_now`), as the schedule's
    stored owner, never as the caller."""
    _member(ctx, auth)
    request = _verb(RunIn, body)
    doc = _read(ctx, tenant_id, schedule_id)
    _platform_only(auth, doc, "run")
    if doc["state"] == "disabled":
        raise ScheduleConflict("schedule_disabled", "an admin disabled this schedule")
    if not schedulefire.schedules_enabled(ctx.settings):
        raise ScheduleConflict("schedules_disabled", "schedules are switched off platform-wide (SCHEDULES_ENABLED)")
    firing = schedulefire.Ticker(ctx).fire_now(doc, actor=auth.email, dry_run=request.dry_run)
    now = _now(ctx)
    ref = _schedule_ref(ctx, schedule_id)

    # The firing is written by the tick's own transactions; its audit entry
    # follows at once, naming it.
    @firestore.transactional
    def _apply(txn: Any) -> None:
        current = _snapshot(txn.get(ref)).to_dict() or doc
        _audit(txn, ctx, current, "run_now", auth.email, now, {
            "firing_id": firing.get("firing_id"), "dry_run": request.dry_run, "state": firing.get("state"),
        })

    _apply(ctx.db.transaction())
    return {"firing": firing_to_api(firing)}


# --------------------------------------------------------------------------
# History and audit
# --------------------------------------------------------------------------


@router.get("/v1/schedules/{schedule_id}/firings")
def list_firings(
    schedule_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _read(ctx, tenant_id, schedule_id)
    rows = _history(ctx, tenant_id, schedule_id, paged_limit(ctx, limit))
    return {"firings": _firings_api(ctx, tenant_id, rows)}


@router.get("/v1/schedules/{schedule_id}/firings/{firing_id}")
def get_firing(
    schedule_id: str,
    firing_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _read(ctx, tenant_id, schedule_id)
    # A firing id is `{schedule_id}:...`; any other is not this schedule's.
    if not firing_id.startswith(f"{schedule_id}:") or "/" in firing_id:
        raise NotFound(f"firing {firing_id[:96]!r} not found")
    snap = ctx.db.collection(schedulefire.FIRINGS).document(firing_id).get()
    row = snap.to_dict() if snap.exists else None
    if not row or row.get("tenant_id") != tenant_id or row.get("schedule_id") != schedule_id:
        raise NotFound(f"firing {firing_id[:96]!r} not found")
    return {"firing": _firings_api(ctx, tenant_id, [row])[0]}


#: Why an item draws a dash instead of bars, in words the UI shows as served.
_UNREAD_API_ACTION = "an API action runs no attempt: it ended when it was recorded"
_UNREAD_CAP = f"more work than one page reads ({SPAN_TASKS} tasks): open the item to see it"

#: Why an item's cost is a dash, never $0 (SCHED-HISTORY-FOLLOWUP, part of #1109).
_COST_API_ACTION = "an API action runs no attempt, so it records no cost"
_COST_NO_ATTEMPT = "no attempt has run yet"
_COST_STILL_RUNNING = "its attempts are still running: a cost is written when an attempt ends"
_COST_NONE_RECORDED = "no attempt recorded a cost"
_COST_UNREAD = "its attempts could not be read"


def _val(v: Any) -> Any:
    """An enum's value; a stored string or None as it is."""
    return getattr(v, "value", v)


def _span_task(task: Any, step_label: str | None, attempts: list[Any] | None,
               unread: str | None) -> dict[str, Any]:
    """One task as the timeline draws it: the fields `lanes.buildLane` reads,
    and its attempts' spans. Not the input, the spec or the result: the
    timeline draws when capacity was held, and nothing of what was asked.
    The one exception is `metadata.title`, the name the History gives a
    schedule-made task (VQ-028, part of #2391); null when unset, and no other
    metadata key is served."""
    shown = None if attempts is None else attempts[:SPAN_ATTEMPTS]
    title = (task.metadata or {}).get("title")
    return _json({
        "task_id": task.id,
        "title": title if isinstance(title, str) and title.strip() else None,
        "step_id": task.step_id,
        "label": step_label or task.step_id or task.runner_profile,
        "state": _val(task.state),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "current_generation": task.current_generation,
        "park_reason": _val(task.park_reason),
        "next_eligible_at": task.next_eligible_at,
        "attempts": None if shown is None else [
            {"attempt_id": a.attempt_id, "task_id": a.task_id, "generation": a.generation,
             "created_at": a.created_at, "completed_at": a.completed_at, "exit_code": a.exit_code}
            for a in reversed(shown)  # oldest first, as the lanes draw them
        ],
        "attempts_truncated": attempts is not None and len(attempts) > SPAN_ATTEMPTS,
        "attempts_unread": unread,
    })


def _item_tasks(ctx: AppContext, tenant_id: str, item: Mapping[str, Any]) -> tuple[
        list[tuple[str, str | None]], dict[str, Any]]:
    """The tasks one work item ran, as (task_id, label), and the item's own
    facts: its state and when it last moved, or why it cannot be drawn."""
    kind, item_id = str(item.get("kind")), str(item.get("id"))
    if kind == "task":
        return [(item_id, None)], {}
    if kind == "workflow":
        found = ctx.store.workflows_by_id(tenant_id, [item_id], submitted_by=None)
        if not found:
            return [], {"unread": "its workflow was not found"}
        wf = found[0]
        steps = [(s.task_id, s.step_id) for s in wf.steps if s.task_id]
        return steps, _json({"state": _val(wf.state), "updated_at": wf.updated_at})
    if kind == "issue_run":
        try:
            run = IssueRuns(ctx.db, now=ctx.now).get(tenant_id, item_id)
        except NotFound:
            return [], {"unread": "its issue run was not found"}
        out: list[tuple[str, str | None]] = []
        if run.planner_task_id:
            out.append((run.planner_task_id, "plan"))
        wf_ids = ([run.workflow_id] if run.workflow_id else []) + list(run.ci_fix_workflows)
        workflows = {w.workflow_id: w for w in ctx.store.workflows_by_id(tenant_id, wf_ids, submitted_by=None)}
        for n, wf_id in enumerate(wf_ids):
            wf = workflows.get(wf_id)
            prefix = "" if n == 0 and run.workflow_id else f"fix {n if run.workflow_id else n + 1} · "
            for step in (wf.steps if wf is not None else []):
                if step.task_id:
                    out.append((step.task_id, f"{prefix}{step.step_id}"))
        # What the list under the lanes names the run by: its issue, and the
        # title read when the run was created (None when that read failed).
        issue = {"number": run.issue.number, "ref": run.issue.short, "url": run.issue.url,
                 "title": (run.issue_read or {}).get("title")}
        return out, _json({"state": _val(run.state), "updated_at": run.updated_at, "issue": issue})
    return [], {"unread": _UNREAD_API_ACTION if kind == "api_action" else f"a {kind} has no attempts to read"}


def _item_cost(kind: str, task_ids: list[str], totals: Mapping[str, Mapping[str, Any]],
               live: set[str], unread: str | None) -> dict[str, Any]:
    """One item's cost as the tick sums a firing's (`Ticker._cost`, §4.3):
    every recorded `cost_usd` of its tasks' attempts, each attempt that
    recorded none counted in `unreported_attempts`, and a task whose attempts
    could not be read counted as one. NULL IS NOT ZERO: with no recorded
    figure `reported_usd` is null and `unread` says why. `unread` is also set
    beside a figure that covers only part of the item (the page's task cap)."""
    if kind == "api_action":
        return {"reported_usd": None, "unreported_attempts": 0, "unread": _COST_API_ACTION}
    reported, known, unreported, failed, attempts = 0.0, False, 0, 0, 0
    for task_id in task_ids:
        row = totals.get(task_id) or {}
        if row.get("attempts_read") != "ok":
            unreported += 1
            failed += 1
            continue
        if row.get("cost_usd_total") is not None:
            reported += float(row["cost_usd_total"])
            known = True
        attempts += int(row.get("attempts") or 0)
        unreported += int(row.get("attempts") or 0) - int(row.get("attempts_with_cost") or 0)
    if known:
        return {"reported_usd": round(reported, 6), "unreported_attempts": unreported, "unread": unread}
    if unread is None:
        if failed:
            unread = _COST_UNREAD
        elif attempts == 0:
            unread = _COST_NO_ATTEMPT
        elif live.intersection(task_ids):
            unread = _COST_STILL_RUNNING
        else:
            unread = _COST_NONE_RECORDED
    return {"reported_usd": None, "unreported_attempts": unreported, "unread": unread}


# --------------------------------------------------------------------------
# What a firing serves beyond its document (AUTO-API-SCHEDULES, part of #2417)
# --------------------------------------------------------------------------

#: The words `issueci` stores on a run whose pull request was closed unmerged
#: (`RunState.FAILED`, its `error`). Nothing else on the run records a close:
#: `pull_request` keeps the last reading, and a run failed for another reason
#: (its fix rounds spent) leaves its pull request open. A test holds these
#: words to `issueci`'s.
_PR_CLOSED_WORDS = "was closed without being merged"

#: Why a firing's `largest_run_usd` is null, never $0.
_LARGEST_NO_RUN = "it started no run"
_LARGEST_PARTIAL = "a run has attempts that reported no cost, so the largest is not known"
_LARGEST_OLDER = (f"only the newest {SPAN_FIRINGS} firings of a read are costed: "
                  "open the firing to read its largest run")


def _pr_state(run: Mapping[str, Any], merge_waiting: bool) -> str:
    """`merged` once the CI loop read it merged; `waiting_on_approval` while a
    merge approval waits at the head it was asked at; `closed` when the run
    failed because it was closed unmerged; `open` otherwise, as last read."""
    if (run.get("pull_request") or {}).get("merged") is True:
        return "merged"
    if merge_waiting:
        return "waiting_on_approval"
    if run.get("state") == RunState.FAILED.value and _PR_CLOSED_WORDS in str(run.get("error") or ""):
        return "closed"
    return "open"


def _pull_requests(ctx: AppContext, tenant_id: str,
                   rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """`{number, state}` per issue run of `rows`' work that opened a pull
    request: the runs in one batched read, the waiting merges in one more,
    only when a run is at the merge point. A run of another tenant, missing,
    or with no pull request is left out, and its item carries no field."""
    run_ids = list(dict.fromkeys(
        str(item.get("id")) for row in rows for item in row.get("work") or []
        if item.get("kind") == "issue_run" and item.get("id")
    ))
    if not run_ids:
        return {}
    col = ctx.db.collection(RUNS_COLLECTION)
    runs: dict[str, dict[str, Any]] = {}
    for snap in ctx.db.get_all([col.document(i) for i in run_ids]):
        data = snap.to_dict() if snap.exists else None
        if data and data.get("tenant_id") == tenant_id and (data.get("pull_request") or {}).get("number"):
            runs[str(data.get("id"))] = data
    # `approvals._live_merge`'s rule: a merge record waits only while its run
    # is CHECKING at the head it was asked at.
    at_merge = {rid: str(r["pull_request"].get("head_sha")) for rid, r in runs.items()
                if r.get("state") == RunState.CHECKING.value and r["pull_request"].get("merged") is not True}
    waiting: set[str] = set()
    if at_merge:
        for doc in approvals.pending(ctx.db, tenant_id):
            rid = str((doc.get("subject") or {}).get("run_id"))
            if doc.get("kind") == approvals.MERGE and rid in at_merge \
                    and (doc.get("digest") or {}).get("head_sha") == at_merge[rid]:
                waiting.add(rid)
    return {rid: {"number": int(r["pull_request"]["number"]), "state": _pr_state(r, rid in waiting)}
            for rid, r in runs.items()}


def _largest(costs: list[tuple[str, Mapping[str, Any]]]) -> tuple[float | None, str | None]:
    """The costliest run among a firing's items, each `(kind, _item_cost)`. An
    API action is not a run. Null with the reason when any run's cost is not
    wholly reported: the unknown one could be the largest."""
    runs = [cost for kind, cost in costs if kind != "api_action"]
    if not runs:
        return None, _LARGEST_NO_RUN
    for cost in runs:
        if cost.get("reported_usd") is None:
            return None, cost.get("unread") or _COST_NONE_RECORDED
        if cost.get("unread") or cost.get("unreported_attempts"):
            return None, cost.get("unread") or _LARGEST_PARTIAL
    return max(float(cost["reported_usd"]) for cost in runs), None


def _stored_largest(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """What the firing's own document says, for a firing past the costed ones:
    the tick's `cost` IS its one run's cost when it ran one task or workflow
    and every attempt reported. (Not an issue run's: the tick sums no task of
    an issue run into `cost`.)"""
    work = [item for item in row.get("work") or [] if item.get("kind") != "api_action"]
    if not work:
        return None, _LARGEST_NO_RUN
    cost = row.get("cost") or {}
    if len(work) == 1 and work[0].get("kind") in ("task", "workflow") and row.get("state") == schedulefire.DONE \
            and cost.get("reported_usd") is not None and not cost.get("unreported_attempts"):
        return float(cost["reported_usd"]), None
    return None, _LARGEST_OLDER


def _largest_runs(ctx: AppContext, tenant_id: str,
                  rows: list[Mapping[str, Any]]) -> dict[str, tuple[float | None, str | None]]:
    """`largest_run_usd` for the newest `SPAN_FIRINGS` of `rows`, read as
    `/spans` reads an item's cost (`_item_tasks`, `_item_cost`), within its
    `SPAN_TASKS` cap, so the firing and its spans cannot disagree."""
    budget = SPAN_TASKS
    plan: dict[str, list[tuple[str, str | None, list[str]]] | None] = {}
    every: list[str] = []
    for row in rows[:SPAN_FIRINGS]:
        items: list[tuple[str, str | None, list[str]]] | None = []
        for item in row.get("work") or []:
            kind = str(item.get("kind"))
            if kind == "api_action":
                items.append((kind, None, []))
                continue
            pairs, facts = _item_tasks(ctx, tenant_id, item) if budget > 0 else ([], {})
            if budget <= 0 or len(pairs) > budget:
                items = None
                break
            budget -= len(pairs)
            ids = [t for t, _ in pairs]
            every.extend(ids)
            items.append((kind, facts.get("unread") or (None if ids else "it ran no task yet"), ids))
        plan[str(row.get("firing_id"))] = items
    tasks = ctx.store.tasks_by_id(tenant_id, every) if every else {}
    live = {t for t, task in tasks.items() if task.state not in TERMINAL_STATES}
    found = [t for t in every if t in tasks]
    totals = totals_for(ctx.db, tenant_id, found) if found else {}
    out: dict[str, tuple[float | None, str | None]] = {}
    for fid, items in plan.items():
        if items is None:
            out[fid] = (None, _UNREAD_CAP)
            continue
        out[fid] = _largest([(kind, _item_cost(kind, ids, totals, live, None if ids else unread))
                             for kind, unread, ids in items])
    return out


def _firings_api(ctx: AppContext, tenant_id: str, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Firings as served: `firing_to_api`, each issue-run work item that
    opened a pull request with its `pull_request`, and `largest_run_usd`
    with `largest_run_unread` saying why when it is null."""
    prs = _pull_requests(ctx, tenant_id, rows)
    largest = _largest_runs(ctx, tenant_id, rows)
    out = []
    for row in rows:
        shown = firing_to_api(row)
        for item in shown.get("work") or []:
            if item.get("kind") == "issue_run" and str(item.get("id")) in prs:
                item["pull_request"] = prs[str(item.get("id"))]
        usd, unread = largest.get(str(row.get("firing_id"))) or _stored_largest(row)
        shown["largest_run_usd"] = usd
        shown["largest_run_unread"] = unread
        out.append(shown)
    return out


@router.get("/v1/schedules/{schedule_id}/spans")
def list_firing_spans(
    schedule_id: str,
    firing_id: list[str] = Query(default=[]),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The work of ONE PAGE of firings and its attempts' spans (#1109).

    The History tab's timeline draws a lane per firing and a bar per task each
    work item ran; `GET /v1/schedules/{id}` serves the firings and their
    `work[]`, and this serves what the bars need for the firing ids asked --
    at most `SPAN_FIRINGS`, never every firing ever. No document is written
    and no new shape is stored: the firings, issue runs, workflows, tasks and
    attempts are read as they are.

    TENANT-SCOPED like every route here: the schedule is read under the
    caller's tenant first (another tenant's is the same 404 as a missing
    one), a firing is served only if its id is this schedule's and its stored
    `tenant_id` and `schedule_id` match, and every task, workflow and run is
    read through the tenant-checked store. A firing id that is not this
    schedule's is listed in `not_found` and nothing of it is read.

    HONEST GAPS (redesign-v2.md): an item whose tasks or attempts could not be
    read carries `unread` (or a task its `attempts_unread`) with the reason,
    so the UI draws a dash and says why, never an empty lane.

    WHAT THE LIST UNDER THE LANES NAMES (SCHED-HISTORY-FOLLOWUP): each item's
    `cost` (`_item_cost`), from ONE batched attempts read for the page's
    tasks (`attempt_totals.totals_for`, a query per 30 tasks, inside the same
    `SPAN_TASKS` cap), and an issue run's `issue`: its number, reference, url
    and title from the run record, read under the caller's tenant.
    """
    _read(ctx, tenant_id, schedule_id)
    wanted = list(dict.fromkeys(i for i in firing_id if i))
    if len(wanted) > SPAN_FIRINGS:
        raise ValidationFailed(
            f"at most {SPAN_FIRINGS} firings are read at once",
            detail={"asked": len(wanted), "limit": SPAN_FIRINGS},
        )
    ours = [i for i in wanted if i.startswith(f"{schedule_id}:") and "/" not in i]
    col = ctx.db.collection(schedulefire.FIRINGS)
    rows: dict[str, dict[str, Any]] = {}
    if ours:
        for snap in ctx.db.get_all([col.document(i) for i in ours]):
            row = snap.to_dict() if snap.exists else None
            if row and row.get("tenant_id") == tenant_id and row.get("schedule_id") == schedule_id:
                rows[str(row.get("firing_id"))] = row

    budget = SPAN_TASKS
    out: list[dict[str, Any]] = []
    # Each item's read tasks, for the one cost read after the page is walked.
    costed: list[tuple[dict[str, Any], list[str]]] = []
    live: set[str] = set()
    for fid in wanted:
        row = rows.get(fid)
        if row is None:
            continue
        items: list[dict[str, Any]] = []
        for item in row.get("work") or []:
            kind, item_id = str(item.get("kind")), str(item.get("id"))
            shown: dict[str, Any] = {"kind": kind, "id": item_id, "state": None, "updated_at": None,
                                     "issue": None, "tasks": [], "unread": None, "cost": None}
            if budget <= 0 and kind != "api_action":
                shown["unread"] = _UNREAD_CAP
                costed.append((shown, []))
                items.append(shown)
                continue
            pairs, facts = _item_tasks(ctx, tenant_id, item)
            shown.update(facts)
            if len(pairs) > budget:
                shown["unread"] = _UNREAD_CAP
                pairs = pairs[:budget]
            budget -= len(pairs)
            tasks = ctx.store.tasks_by_id(tenant_id, (t for t, _ in pairs))
            costed.append((shown, [t for t, _ in pairs if t in tasks]))
            live.update(t for t, task in tasks.items() if task.state not in TERMINAL_STATES)
            for task_id, label in pairs:
                task = tasks.get(task_id)
                if task is None:
                    shown["tasks"].append({"task_id": task_id, "label": label, "attempts": None,
                                           "attempts_unread": "its task was not found"})
                    continue
                try:
                    attempts = ctx.store.list_attempts(tenant_id, task_id, limit=SPAN_ATTEMPTS + 1)
                except Exception as exc:  # one task's read does not blank the page
                    log.warning("schedule spans: attempts of %s not read: %s", task_id, type(exc).__name__)
                    shown["tasks"].append(_span_task(task, label, None, "its attempts could not be read"))
                    continue
                shown["tasks"].append(_span_task(task, label, attempts, None))
            if not pairs and shown["unread"] is None and facts.get("unread") is None:
                shown["unread"] = "it ran no task yet"
            items.append(shown)
        out.append({"firing_id": fid, "items": items})
    every = [t for _, ids in costed for t in ids]
    totals = totals_for(ctx.db, tenant_id, every) if every else {}
    for shown, ids in costed:
        # A task named in the work but not found under this tenant is
        # counted unread, as `Ticker._cost` counts a task it cannot read.
        missing = [t["task_id"] for t in shown["tasks"] if t["task_id"] not in ids]
        # The item's own reason stands for its cost when it ran nothing that
        # could be summed, or when the page's cap cut its tasks short.
        reason = shown["unread"] if shown["unread"] == _UNREAD_CAP or not (ids or missing) else None
        shown["cost"] = _item_cost(shown["kind"], ids + missing, totals, live, reason)
    return {"firings": out, "not_found": [i for i in wanted if i not in rows]}


@router.get("/v1/schedules/{schedule_id}/audit")
def list_audit(
    schedule_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§4.8, newest first: the ids sort that way (see the module docstring)."""
    _read(ctx, tenant_id, schedule_id)
    query = (
        ctx.db.collection(AUDIT)
        .where(filter=FieldFilter("schedule_id", "==", schedule_id))
        .order_by("__name__")
        .limit(paged_limit(ctx, limit))
    )
    rows = [snap.to_dict() or {} for snap in query.stream()]
    rows = [row for row in rows if row.get("tenant_id") == tenant_id]
    return {"audit": _json(rows)}


# --------------------------------------------------------------------------
# The admin view (§5.3)
# --------------------------------------------------------------------------


def _admin_row(doc: Mapping[str, Any], now: datetime, pending: int) -> dict[str, Any]:
    """What §5.3 lists. Not the parameters, gate or spec: those are the
    tenant's decisions, and an admin's view is not that tenant's identity."""
    last = doc.get("last_firing") or {}
    return _json({
        "schedule_id": doc.get("schedule_id"),
        "tenant_id": doc.get("tenant_id"),
        "name": doc.get("name"),
        "type": doc.get("type"),
        "tier": _tier(doc),
        "cron": doc.get("cron"),
        "timezone": doc.get("timezone"),
        "words": _words(doc.get("cron")),
        "next_run_at": doc.get("next_run_at"),
        "last_outcome": last.get("outcome"),
        "last_firing_at": last.get("ended_at") or last.get("slot"),
        "spend_today": _spend_today(doc, now),
        "pending_approvals": pending,
        "state": doc.get("state"),
        "pause": doc.get("pause"),
        "owner": doc.get("owner"),
    })


def _tick_health(ctx: AppContext, now: datetime) -> dict[str, Any]:
    """The last hour of tick reports (§2.10), read by id."""
    minute = schedulefire.unix_minute(now)
    refs = [ctx.db.collection(schedulefire.TICKS).document(str(m))
            for m in range(minute - TICK_HEALTH_MINUTES + 1, minute + 1)]
    reports = [snap.to_dict() or {} for snap in ctx.db.get_all(refs) if snap.exists]
    reports.sort(key=lambda r: _aware(r.get("started_at")) or _EPOCH)
    lateness = [
        float(item.get("seconds") or 0.0)
        for report in reports for item in (report.get("lateness") or [])
    ]
    last = reports[-1] if reports else None
    return _json({
        "window_minutes": TICK_HEALTH_MINUTES,
        "ticks": len(reports),
        "disabled": sum(1 for r in reports if r.get("disabled")),
        "errors": sum(int(r.get("errors") or 0) for r in reports),
        "truncated": sum(1 for r in reports if r.get("truncated")),
        "fired": sum(int(r.get("fired") or 0) for r in reports),
        "max_lateness_seconds": max(lateness) if lateness else None,
        "last": {k: v for k, v in last.items() if k not in ("lateness", "expire_at")} if last else None,
    })


@router.get("/v1/admin/schedules")
def admin_list_schedules(
    tenant: str | None = Query(default=None, max_length=128),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None, max_length=64),
    _admin: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§5.3: every tenant's schedules (or one tenant's), by id, with the tick's health."""
    now = _now(ctx)
    size = paged_limit(ctx, limit)
    query = ctx.db.collection(schedules.COLLECTION)
    if tenant:
        query = query.where(filter=FieldFilter("tenant_id", "==", tenant))
    query = query.order_by("__name__")
    if page_token:
        if not _ID.match(page_token):
            raise ScheduleInvalid("invalid_page_token", "page_token is not one this route issued")
        query = query.start_after({"__name__": page_token})
    rows = [snap.to_dict() or {} for snap in query.limit(size + 1).stream()]
    if tenant:
        rows = [row for row in rows if row.get("tenant_id") == tenant]
    next_token = None
    if len(rows) > size:
        rows = rows[:size]
        next_token = rows[-1]["schedule_id"]
    pending = _pending(ctx, tenant or None)
    return {
        "schedules": [_admin_row(row, now, pending.get(row["schedule_id"], 0)) for row in rows],
        "next_page_token": next_token,
        "tick": _tick_health(ctx, now),
    }


def _admin_by(auth: AuthContext) -> str:
    return f"admin:{auth.email}"


@router.post("/v1/admin/schedules/{schedule_id}:pause")
def admin_pause_schedule(
    schedule_id: str,
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§5.3: an admin pauses any tenant's schedule; the tenant may resume it."""
    request = _verb(ReasonIn, body)
    by = _admin_by(auth)

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        if doc["state"] == "disabled":
            raise ScheduleConflict("schedule_disabled", "this schedule is disabled; enable it first")
        if doc["state"] == "paused":
            return None
        return _paused(by, now, request.reason)

    doc = _move(ctx, None, schedule_id, by=by, action="admin_pause", change=change)
    return {"schedule": _admin_row(doc, _now(ctx), 0)}


@router.post("/v1/admin/schedules/{schedule_id}:disable")
def admin_disable_schedule(
    schedule_id: str,
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§1.3, §5.3: disabled until an admin re-enables it; the tenant cannot resume it."""
    request = _verb(ReasonIn, body)
    by = _admin_by(auth)

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        if doc["state"] == "disabled":
            return None
        return {"state": "disabled", "next_run_at": None,
                "pause": {"by": by, "at": now, "reason": request.reason or "disabled by an admin", "code": None}}

    doc = _move(ctx, None, schedule_id, by=by, action="admin_disable", change=change)
    return {"schedule": _admin_row(doc, _now(ctx), 0)}


@router.post("/v1/admin/schedules/{schedule_id}:enable")
def admin_enable_schedule(
    schedule_id: str,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§5.3 re-enable: only a `disabled` schedule. A tenant's own pause is the
    tenant's to lift, so an admin's enable does not override it."""
    by = _admin_by(auth)

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        if doc["state"] != "disabled":
            raise ScheduleConflict("schedule_not_disabled", f"this schedule is {doc['state']}, not disabled")
        # A type withdrawn from the catalogue is why the tick disables; it
        # stays disabled until the type is back.
        schedules.resolve_type(doc["type"], scheduletypes.EXECUTOR_DIR)
        return _enabled(doc, now)

    doc = _move(ctx, None, schedule_id, by=by, action="admin_enable", change=change)
    return {"schedule": _admin_row(doc, _now(ctx), 0)}


@router.post("/v1/admin/tenants/{tenant_id}/schedules:pause")
def admin_pause_tenant_schedules(
    tenant_id: str,
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§5.3: an admin pauses all of any tenant's enabled schedules, typed with
    that tenant id. The tenant may resume each; live work is not cancelled."""
    request = _verb(PauseAllIn, body)
    if ctx.store.get_tenant(tenant_id) is None:
        raise NotFound(f"tenant {tenant_id[:64]!r} not found")
    if request.confirm != tenant_id:
        raise ScheduleInvalid("confirmation_required",
                              "type the tenant's id in `confirm` to pause all of its schedules")
    return _pause_all(ctx, tenant_id, by=_admin_by(auth), action="admin_pause_all", reason=request.reason,
                      platform_admin=True)
