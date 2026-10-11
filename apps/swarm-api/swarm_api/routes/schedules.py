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
    GET    /v1/schedules/{id}/audit                newest first
    GET    /v1/admin/schedules                     every tenant's, with the tick's health (§5.3)
    POST   /v1/admin/schedules/{id}:pause | :disable | :enable
    POST   /v1/admin/tenants/{tenant_id}/schedules:pause {confirm}   §5.3's pause-all

NOT HERE: approvals and the SD3 merge switch (`:merge-mode`) are lane S5's;
the tick (`POST /v1/admin/schedules/tick`) is `routes/schedule_tick.py`. A
`PATCH` asking for `gate.merge: auto` is refused 422 `use_merge_switch` by
`schedules.resolve_gate`, so the switch's audit cannot be bypassed here.

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
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from swarm_common.admission import _snapshot

from .. import cronexpr, schedulefire, schedules, scheduletypes
from ..auth import AuthContext
from ..deps import AppContext, admin_auth, current_auth, get_context, paged_limit, tenant_scope
from ..errors import Conflict, NoClaudeAccount, NotFound, WorkspaceNotReady
from ..issueruns import TERMINAL_RUN_STATES, IssueRuns, RunState
from ..repositories import COLLECTION as REPOSITORIES
from ..repositories import Repositories, platform_of
from ..schedules import ScheduleConflict, ScheduleForbidden, ScheduleInvalid
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


def schedule_to_api(doc: Mapping[str, Any], now: datetime, *, pending: int = 0) -> dict[str, Any]:
    out = _json(dict(doc))
    out["words"] = _words(doc.get("cron"))
    out["tier"] = _tier(doc)
    out["spend_today"] = _spend_today(doc, now)
    out["pending_approvals"] = pending
    return out


def firing_to_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    return _json({k: v for k, v in doc.items() if k not in _FIRING_PRIVATE})


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
    now = _now(ctx)
    pending = _pending(ctx, tenant_id)
    rows = sorted(_tenant_schedules(ctx, tenant_id), key=lambda d: str(d.get("name", "")).casefold())
    return {
        "schedules": [schedule_to_api(row, now, pending=pending.get(row["schedule_id"], 0)) for row in rows],
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
    pending = sum(1 for f in firings if f.get("state") == schedulefire.AWAITING_APPROVAL)
    return {
        "schedule": schedule_to_api(doc, _now(ctx), pending=pending),
        "firings": [firing_to_api(f) for f in firings],
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
    stored = _read(ctx, tenant_id, schedule_id)
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

    ref = _schedule_ref(ctx, schedule_id)
    tenant_ref = _tenant_ref(ctx, tenant_id)

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        tenant_doc = _snapshot(txn.get(tenant_ref)).to_dict() or {}
        before = _owned(_snapshot(txn.get(ref)), tenant_id, schedule_id)
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
    stored = _read(ctx, tenant_id, schedule_id)
    if confirm is None or confirm.strip() != stored["name"]:
        raise ScheduleInvalid("confirmation_required", "type the schedule's name in `confirm` to delete it")
    now = _now(ctx)
    ref = _schedule_ref(ctx, schedule_id)
    tenant_ref = _tenant_ref(ctx, tenant_id)

    @firestore.transactional
    def _apply(txn: Any) -> None:
        tenant_doc = _snapshot(txn.get(tenant_ref)).to_dict() or {}
        doc = _owned(_snapshot(txn.get(ref)), tenant_id, schedule_id)
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
    request = _verb(PauseIn, body)
    if request.cancel_live:
        stored = _read(ctx, tenant_id, schedule_id)
        if request.confirm is None or request.confirm != stored["name"]:
            raise ScheduleInvalid("confirmation_required",
                                  "type the schedule's name in `confirm` to cancel its live runs")

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
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
    its execution stop, child cascade and wake happen exactly as there."""
    try:
        if kind == "task":
            task_routes.cancel_task(item_id, background, tenant_id=tenant_id, auth=auth, ctx=ctx)
        else:
            workflow_routes.cancel_workflow(item_id, background, tenant_id=tenant_id, auth=auth, ctx=ctx)
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
               reason: str | None) -> dict[str, Any]:
    """§2.11: every `enabled` schedule of the tenant to `paused` with one reason,
    each through `_move` -- its own transaction re-reading its state, its own
    audit entry. Any other state is left as it is."""
    reason = reason or "all of the tenant's schedules were paused"
    paused: list[str] = []
    unchanged: list[str] = []

    #: Set on every attempt, so a retried transaction reports what committed.
    moved: dict[str, bool] = {}

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        moved[doc["schedule_id"]] = doc["state"] == "enabled"
        return _paused(by, now, reason) if moved[doc["schedule_id"]] else None

    for row in _tenant_schedules(ctx, tenant_id):
        try:
            _move(ctx, tenant_id, row["schedule_id"], by=by, action=action, change=change)
        except NotFound:
            continue  # deleted since the list was read: nothing left to pause
        (paused if moved.get(row["schedule_id"]) else unchanged).append(row["schedule_id"])
    return {"tenant_id": tenant_id, "paused": sorted(paused), "unchanged": sorted(unchanged)}


@router.post("/v1/schedules:pause-all")
def pause_all_schedules(
    body: dict[str, Any] | None = Body(default=None),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§2.11: a member pauses every enabled schedule of their own tenant, typed
    with the tenant id. Live work continues."""
    request = _verb(PauseAllIn, body)
    if request.confirm != tenant_id:
        raise ScheduleInvalid("confirmation_required",
                              "type your tenant's id in `confirm` to pause all of its schedules")
    return _pause_all(ctx, tenant_id, by=auth.email, action="pause_all", reason=request.reason)


@router.post("/v1/schedules/{schedule_id}:resume")
def resume_schedule(
    schedule_id: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """§1.3: from `paused` or `auto_paused`. A `disabled` one only an admin re-enables."""

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
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
    stored = _read(ctx, tenant_id, schedule_id)
    _check_grants(ctx, stored, auth.email)
    _check_file_issues(ctx, stored, auth.email)

    def change(doc: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
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
    request = _verb(RunIn, body)
    doc = _read(ctx, tenant_id, schedule_id)
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
    return {"firings": [firing_to_api(f) for f in rows]}


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
    return {"firing": firing_to_api(row)}


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
    return _pause_all(ctx, tenant_id, by=_admin_by(auth), action="admin_pause_all", reason=request.reason)
