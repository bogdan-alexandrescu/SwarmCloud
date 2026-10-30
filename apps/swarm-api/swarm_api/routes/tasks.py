"""Task routes.

Read the tenant argument on every store call below. It comes from
`tenant_scope`, never from `auth.tenant_id` and never from a path, a query
string or a header.

WHY THE DEPENDENCY RATHER THAN `auth.tenant_id`. This docstring used to claim
that deriving the id from the verified token was the whole boundary -- "there is
no request a caller can construct that reads another tenant's task". That was
wrong, and the counterexample is two VERIFIED identities holding the SAME id:
the frozen `tenant_id_for_group` slugs the local part only, so `eng@example.com`
and `eng@partner.com` both derive `eng`, and a registered group `u-eng@example.com`
derives what the personal tenant of `eng@example.com` derives. Submitting was
already refused by `ensure_tenant`; listing, reading and CANCELLING were not.
`tenant_scope` applies that same principal check before any store call here.

NAMES BESIDE EMAILS (docs/design/user-profile.md §4.4). The two task reads
serve `submitted_by_name` beside `submitted_by`, from one lookup per page
(`people.add_names`): ten tasks by three people read three names once. A name
is a label; the submitter filter and every check here keep using the email.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from swarm_common.states import TaskState

from .. import agent_questions, roles
from ..agent_output import AgentOutputService
from ..attempt_totals import totals_for, with_totals
from ..children import (
    PARENT_CANCELLED,
    PROOF_HEADER,
    TIMESTAMP_HEADER,
    ChildService,
    cascade_children,
)
from ..auth import AuthContext
from .. import sessiontag
from ..codec import attempt_to_api, claude_session_of, task_to_api
from ..deps import (
    AppContext,
    current_auth,
    get_context,
    paged_limit,
    submission_scope,
    tenant_scope,
)
from ..errors import Forbidden, ValidationFailed
from ..handoff import HandoffService
from .children import child_service
from ..heartbeats import heartbeats_for_page
from ..people import add_names, name_lookup
from ..schemas import TaskBatchCreate, TaskCreate
from ..store import Page, task_listing_token
from ..startup import startup_of
from ..task_accounts import accounts_for, accounts_for_attempts
from ..task_input import TaskMasking, input_copy, masking_for
from ..utilisation import MAX_ATTEMPTS as UTILISATION_ATTEMPTS
from ..utilisation import utilisation_of
from ..waiting import waiting_for_page
from ..waiting_detail import build_waiting, read_inputs, waiting_row
from ..waker import TASK_FINISHED, ring

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def require_member(ctx: AppContext, auth: AuthContext, *, fresh: bool = False) -> None:
    """The role floor on every write here that is a member's
    (docs/design/workspace-roles.md §4.1, §4.8): submitting, cancelling
    (anyone's, Q8 -- a `viewer` cancels nothing, their own task included),
    answering the agent and saying "ready". 403 `role_member_required` for a
    `viewer`, through its switch: off, the request goes through and the log
    names the refusal it would have made. Asked BEFORE anything is read or
    written, so a refused request changes nothing. Each route's narrower rule
    (the submitter's, the owner's) is still checked after it, on top: the
    role is the floor, never the permission. `fresh` for the answers, which
    must see a narrowing made a moment ago, as a decision does."""
    store = getattr(ctx, "roles", None)
    if store is not None:
        store.require(auth, roles.MEMBER, fresh=fresh)


def agent_output_service(ctx: AppContext = Depends(get_context)) -> AgentOutputService:
    """The Artifacts tab's reads, built around the one `InspectionService` the app holds.

    A dependency, as `routes.checkpoints.checkpoint_content` is, so a test can
    swap the window size through `app.dependency_overrides` -- FastAPI's own
    injection point -- instead of patching an import.
    """
    return AgentOutputService(ctx.inspection)


def _event_to_api(event, masking: TaskMasking) -> dict:
    """One event, its `detail` masked by the task's masker (the PR #229 review).

    A FAILED event's `detail.error` is the stderr tail `last_error` is, and a
    detail can quote the agent anywhere else; every string in it is masked by
    the rules and the literals the task's input named, string by string --
    never by key, because the keys are the platform's (`TaskMasking.leaves`).
    A lookup key's value (`task_input.LOOKUP_KEYS`) is masked by
    `TaskMasking.name`, so a clean filename stays findable (#296).
    """
    detail, count = masking.leaves(event.detail)
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "type": event.type.value,
        "at": event.at,
        "attempt_id": event.attempt_id,
        "lease_id": event.lease_id,
        "generation": event.generation,
        "detail": detail,
        "detail_redaction_count": count,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(
    body: TaskCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    require_member(ctx, auth)
    result = ctx.submissions.submit_tasks(auth, [body])
    task = result.tasks[0]
    response.headers["Location"] = f"/v1/tasks/{task.id}"
    return {
        "task": task_to_api(task, console_url=ctx.settings.console_url),
        "scheduler_woken": result.woke_scheduler,
    }


@router.post("/batch", status_code=status.HTTP_201_CREATED)
def create_task_batch(
    body: TaskBatchCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    require_member(ctx, auth)
    result = ctx.submissions.submit_tasks(auth, body.tasks)
    return {
        "tasks": [
            task_to_api(task, console_url=ctx.settings.console_url) for task in result.tasks
        ],
        "count": len(result.tasks),
        "scheduler_woken": result.woke_scheduler,
    }


#: `GET /v1/tasks?view=` (#168). `full`, or no `view` at all, is the row every
#: other route serves; `summary` drops what a list never shows.
TASK_VIEWS: tuple[str, ...] = ("full", "summary")

#: `submitted_by=me` is the caller's own verified email (U5).
OWNER_ME = "me"

#: `GET /v1/tasks?claude_session=` (CHECK-FILTER-TASKS, #1749): the store page
#: the session filter reads, and how many of them one request reads before it
#: answers with what matched and a token. The session is the frozen
#: `Task.metadata`'s `claude_session`, and no index pairs it with the state
#: and submitter filters the bridge sends beside it (adding one per
#: combination is the index sprawl `Store.list_tasks` already declines), so
#: the route filters after reading. A task row costs one document read -- no
#: rollup, unlike a workflow -- and the enrichment below runs only on the rows
#: served, so 10 pages of 200 is 2,000 rows a request: eight times the 250
#: live tasks tenant eng held on 2026-10-20, and still a bound.
FILTER_PAGE_SIZE = 200
MAX_FILTER_PAGES = 10

#: Why a row the session filter read was not served, counted in
#: `filter.left_out` -- the two kinds the bridge's SC-SESSION-SCOPE report
#: counts (`swarm_mcp.session.ownership`).
LEFT_OUT_REASONS: tuple[str, ...] = ("other_session", "untagged")


def _owner_filter(requested: str | None, auth: AuthContext, forced: str | None) -> str | None:
    """The submitter `GET /v1/tasks` narrows to, or None for every submitter (U5).

    `forced` is `deps.submission_scope`: the caller's own email for a
    continuation-scoped account, which always reads only its own tasks and is
    refused (403) for naming anyone else. Otherwise `me` is the caller's
    verified email, and any other value must BE the caller's email unless the
    caller `is_admin`. An admin naming another member still reads only the
    tenant `tenant_scope` checked: the owner filter narrows a tenant, it
    never replaces one (`Store.list_tasks` applies both).
    """
    if requested is None:
        return forced
    value = requested.strip()
    own = auth.email
    if value.lower() == OWNER_ME or value.lower() == own.lower():
        return own
    if forced is not None:
        raise Forbidden(
            "a continuation-scoped account lists only the tasks it submitted; "
            "submitted_by may name only itself"
        )
    if not auth.is_admin:
        raise Forbidden(
            "submitted_by may name only yourself (or `me`); "
            "filtering by another member needs an admin"
        )
    return value


@router.get("")
def list_tasks(
    state: str | None = Query(default=None),
    workflow_id: str | None = Query(default=None),
    runner_profile: str | None = Query(default=None),
    # A parent's children (docs/design/child-tasks.md §6.3), in this tenant.
    parent_task_id: str | None = Query(default=None, min_length=1, max_length=128),
    submitted_by: str | None = Query(
        default=None,
        description="`me`, or a submitter's email (another member's needs an admin)",
    ),
    view: str | None = Query(default=None, description="`full` (default) or `summary`"),
    claude_session: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    scope_submitter: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One page of the caller's tenant's tasks, newest first.

    `submitted_by` (U5) narrows the page to one submitter, always inside the
    checked tenant. Alone, it is applied IN THE QUERY, so pages are full and
    the cursor is exact. Combined with `state`, `workflow_id` or
    `runner_profile`, no index covers the combination, so it is applied after
    the cursor is taken and a page may be short (`Store.list_tasks`).

    `view=summary` (#168) serves each row without `input`, `metadata` and
    `result_summary` and their masking counts -- the keys absent, not null --
    and says `"view": "summary"`; the masking for them is never run. No
    `view`, or `view=full`, is the response this route has always served.

    Each lease-holding row carries its worker's `heartbeat_at` and the
    reconciler's `heartbeat_grace_seconds` (#179), from batched reads of the
    page's current leases (`swarm_api.heartbeats`).

    Every row, in both views, carries its task's attempt totals
    (`swarm_api.attempt_totals`), from one batched attempts read per 30 rows.

    `claude_session` (CHECK-FILTER-TASKS, #1749, as CHECK-FILTER did for
    `GET /v1/workflows`) keeps only the tasks tagged with that Claude Code
    session (`metadata.claude_session`, read through the task's masker, so
    exactly the rows a client comparing the served `claude_session` would
    keep), combined with every other filter. The sc plugin's check reads its
    own session's single tasks this way instead of every page of the
    caller's. No index serves it, so the route reads store pages and pages on
    INSIDE the request until `limit` rows matched or `MAX_FILTER_PAGES` store
    pages were read, and hands back a token that continues past its last
    row. `filter` echoes the session, so a client tells this route from one
    that ignored it; `filter.left_out` counts the rows read and not served by
    why, and `filter.left_out_single` the part of them in no workflow and
    with no parent -- the rows a single-task reader would have counted.
    """
    parsed_state: TaskState | None = None
    if state is not None:
        try:
            parsed_state = TaskState(state)
        except ValueError:
            raise ValidationFailed(
                f"unknown state {state!r}",
                detail={"known_states": [s.value for s in TaskState]},
            ) from None
    if view is not None and view not in TASK_VIEWS:
        raise ValidationFailed(
            f"unknown view {view!r}",
            detail={"known_views": list(TASK_VIEWS)},
        )
    summary = view == "summary"
    session = None
    if claude_session is not None:
        session = sessiontag.valid(claude_session)
        if session is None:
            raise ValidationFailed(
                f"claude_session {claude_session!r} is not a Claude Code session id",
                detail={"pattern": sessiontag.PATTERN},
            )
    owner = _owner_filter(submitted_by, auth, scope_submitter)
    query = {
        "state": parsed_state,
        "workflow_id": workflow_id,
        "runner_profile": runner_profile,
        "submitted_by": owner,
        "parent_task_id": parent_task_id,
    }
    scope: dict | None = None
    if session is None:
        page = ctx.store.list_tasks(
            tenant_id, limit=paged_limit(ctx, limit), page_token=page_token, **query
        )
    else:
        page, left_out, left_out_single = _session_page(
            ctx, tenant_id, session, limit=paged_limit(ctx, limit), page_token=page_token,
            query=query,
        )
        scope = {
            "claude_session": session,
            "left_out": left_out,
            "left_out_single": left_out_single,
        }
    # One read of the page's READY tasks' pools, de-duplicated (#362).
    waiting = waiting_for_page(ctx.db, page.items, as_of=ctx.now())
    # #379: each row's account, from ONE bounded collection-group query per 30
    # rows of the page (Firestore's `in` cap) -- two for the 50-row phone page,
    # seven for a full 200 -- capped at 64 event documents per task, and none
    # for a row whose profile calls no model, never asks the pool, or was never
    # admitted. A finished task's answer is cached per process. See
    # `swarm_api.task_accounts`.
    accounts = accounts_for(ctx.db, tenant_id, page.items)
    # #179: the lease-holding rows' current leases, one `get_all` per 100.
    beats = heartbeats_for_page(ctx.db, tenant_id, page.items, core=ctx.settings.core)
    # EVERY ATTEMPT'S SPEND AND TIME on each row, both views (owner decision
    # 2026-10-05, P1 follow-up): one batched attempts query per 30 rows of the
    # page, never one per task (`swarm_api.attempt_totals.totals_for`).
    totals = totals_for(ctx.db, tenant_id, [task.id for task in page.items])
    # #1782: each row's one-line reason, from the row's own document and the
    # pool reading above -- no event, pool or related read per row.
    now = ctx.now()
    payload: dict = {
        "tasks": [
            with_totals(
                task_to_api(
                    task,
                    waiting.get(task.id),
                    account=accounts.get(task.id),
                    console_url=ctx.settings.console_url,
                    heartbeat=beats.get(task.id),
                    summary=summary,
                    waiting=waiting_row(
                        build_waiting(task, waiting_for=waiting.get(task.id), as_of=now)
                    ),
                ),
                totals,
            )
            for task in page.items
        ],
        "next_page_token": page.next_page_token,
        "tenant_id": tenant_id,
    }
    if scope is not None:
        payload["filter"] = scope
    add_names(name_lookup(ctx), payload["tasks"], ("submitted_by",))
    if summary:
        payload["view"] = "summary"
    return payload


def _session_page(
    ctx: AppContext,
    tenant_id: str,
    session: str,
    *,
    limit: int,
    page_token: str | None,
    query: dict,
) -> tuple[Page, dict[str, int], dict[str, int]]:
    """`(page, left_out, left_out_single)`: one page of the tasks tagged with
    `session`, read store page by store page under the route's other filters.

    THE TOKEN IS THE LAST ROW SERVED'S, as in the workflow route's
    `_scoped_page`. When `limit` matches are reached before the end of a
    store page, the page's own token would skip the rows after the last one
    served, so the token is minted on that row (`task_listing_token`) and
    the rest of the page is read again by the next request -- and counted in
    `left_out` only then, so a walk counts each row once. When the store page
    ends exactly there, or the scan stops at `MAX_FILTER_PAGES`, the store
    page's token is the position."""
    matched: list = []
    left_out = {reason: 0 for reason in LEFT_OUT_REASONS}
    left_out_single = {reason: 0 for reason in LEFT_OUT_REASONS}
    token = page_token
    for _ in range(MAX_FILTER_PAGES):
        page = ctx.store.list_tasks(
            tenant_id, limit=FILTER_PAGE_SIZE, page_token=token, **query
        )
        token = page.next_page_token
        for index, task in enumerate(page.items):
            tagged = claude_session_of(task, masking_for(task, warm=False))
            if tagged != session:
                why = "other_session" if tagged else "untagged"
                left_out[why] += 1
                if task.workflow_id is None and task.parent_task_id is None:
                    left_out_single[why] += 1
                continue
            matched.append(task)
            if len(matched) == limit:
                if index < len(page.items) - 1:
                    token = task_listing_token(task)
                return Page(items=matched, next_page_token=token), left_out, left_out_single
        if token is None:
            break
    return Page(items=matched, next_page_token=token), left_out, left_out_single


@router.get("/open-questions")
def list_open_questions(
    claude_session: str = Query(...),
    since: datetime | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The caller's finished tasks of one Claude Code session that ended at or
    after `since` (default 7 days ago, at most 31) and have at least one
    owner question without an answer, newest end first, in ONE call (#2835).

    The sc plugin's check (`owner_questions`) paged the session's SUCCEEDED
    tasks for these and gave up after 10 pages, so a busy session's older
    questions were never listed. This route asks the store for the tasks
    whose agent asked questions (`agent_questions.open_question_tasks`) and
    filters the session and the answers itself. Always the caller's OWN
    tasks: the questions are the submitter's to answer. Each row is the row
    `GET /v1/tasks` serves; `filter` echoes the session and the window, and
    `complete` is false only when the read hit its bound.
    """
    session = sessiontag.valid(claude_session)
    if session is None:
        raise ValidationFailed(
            f"claude_session {claude_session!r} is not a Claude Code session id",
            detail={"pattern": sessiontag.PATTERN},
        )
    now = ctx.now()
    earliest = now - timedelta(days=agent_questions.OPEN_QUESTIONS_MAX_DAYS)
    if since is None:
        since = now - timedelta(days=agent_questions.OPEN_QUESTIONS_DEFAULT_DAYS)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if since < earliest:
        raise ValidationFailed(
            f"since may reach back at most {agent_questions.OPEN_QUESTIONS_MAX_DAYS} days",
            detail={"earliest": earliest.isoformat()},
        )
    tasks, complete = agent_questions.open_question_tasks(
        ctx.db, tenant_id, auth.email, session, since
    )
    payload: dict = {
        "tasks": [task_to_api(task, console_url=ctx.settings.console_url) for task in tasks],
        "complete": complete,
        "filter": {"claude_session": session, "since": since.isoformat(), "open_questions": True},
        "tenant_id": tenant_id,
    }
    add_names(name_lookup(ctx), payload["tasks"], ("submitted_by",))
    return payload


@router.get("/{task_id}")
def get_task(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    task = ctx.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    waiting = waiting_for_page(ctx.db, [task], as_of=ctx.now())
    accounts = accounts_for(ctx.db, tenant_id, [task])
    beats = heartbeats_for_page(ctx.db, tenant_id, [task], core=ctx.settings.core)
    # EVERY ATTEMPT'S SPEND AND TIME beside the last attempt's (owner decision
    # 2026-10-05, lane review P1): the task document describes only the attempt
    # that ended last. One attempt query; see `swarm_api.attempt_totals`.
    totals = totals_for(ctx.db, tenant_id, [task.id])
    # #1782: why it waits, in full -- its newest events and, for a dependency
    # or children park, ONE read of the tenant's related tasks; for a READY
    # task ONE count of its place in line (WAIT-LINE, never on a listing).
    reads = read_inputs(ctx.store, task, waiting_for=waiting.get(task.id))
    detail = build_waiting(
        task,
        events=reads.events,
        waiting_for=waiting.get(task.id),
        related=reads.related,
        account=accounts.get(task.id),
        line=reads.line,
        as_of=ctx.now(),
    )
    served = with_totals(
        task_to_api(
            task,
            waiting.get(task.id),
            account=accounts.get(task.id),
            console_url=ctx.settings.console_url,
            heartbeat=beats.get(task.id),
            waiting=detail,
        ),
        totals,
    )
    add_names(name_lookup(ctx), [served], ("submitted_by",))
    return {"task": served}


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: str,
    # Both: the scope decides WHOSE task may be cancelled, `auth` records WHO
    # cancelled it. Cancelling is a write, and it was reachable across a tenant
    # id collision until this dependency existed.
    background: BackgroundTasks,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    require_member(ctx, auth)
    task, target = ctx.store.request_cancel_with_target(
        tenant_id, task_id, by=auth.email, tenant_member=auth.tenant_member
    )
    # STOP THE EXECUTION NOW (#627), rather than waiting for the worker's next
    # poll or the reconciler: some executions ran 7-13 h past a cancel. Only
    # the first cancel of a task names one, so a second press asks nothing.
    # After the response, so a slow backend does not hold the caller; and it
    # releases nothing -- the worker, SIGTERMed, ends the task and its lease.
    if target is not None:
        background.add_task(ctx.executions.cancel, target)
    # OD-B15-4: cancelling a parent cancels its children, at once; the
    # scheduler's sweep makes it certain (docs/design/child-tasks.md §3.4).
    children: list = []
    ended_children: list[str] = []
    cascaded = cascade_children(
        ctx, task, why=PARENT_CANCELLED, by=auth.email, targets=children,
        ended=ended_children,
    )
    # A child cancelled with its parent is stopped the same way (#627).
    for child_target in children:
        background.add_task(ctx.executions.cancel, child_target)
    # THE FINISH WAKE (#636). A task this call ENDED -- it held no capacity,
    # or no live worker was behind it -- has no worker to ring for it, so its
    # dependants, and any capacity the cancel released, waited for the
    # scheduler's safety tick. Ring the same `task_finished` the worker rings,
    # after the response: the cancel is already durable, and a lost wake costs
    # one tick. A cancel that only FLAGGED a live worker rings nothing here;
    # that worker ends the task and rings itself.
    if task.state is TaskState.CANCELLED:
        background.add_task(
            ring, ctx.waker, ctx.metrics, TASK_FINISHED,
            task_id=task.id, tenant_id=task.tenant_id, state=task.state.value,
        )
    # The same for each child the cascade ENDED, whatever the parent did: a
    # child with no live worker has nobody else to ring for it, and its own
    # dependants waited a tick (#636, left by #741). A child only flagged is
    # its worker's to ring. Same tenant as the parent: `cascade` refuses any
    # other (invariant 9).
    for child_id in ended_children:
        background.add_task(
            ring, ctx.waker, ctx.metrics, TASK_FINISHED,
            task_id=child_id, tenant_id=task.tenant_id, state=TaskState.CANCELLED.value,
        )
    accounts = accounts_for(ctx.db, tenant_id, [task])
    # A task cancelled between attempts has already spent: the response
    # carries every attempt's totals, as `get_task` does (P1 follow-up).
    totals = totals_for(ctx.db, tenant_id, [task.id])
    served = with_totals(
        task_to_api(task, account=accounts.get(task.id), console_url=ctx.settings.console_url),
        totals,
    )
    # `submitted_by_name` beside `submitted_by`, as `get_task` serves it.
    add_names(name_lookup(ctx), [served], ("submitted_by",))
    return {
        "task": served,
        # A task holding capacity stays in its state until the worker or the
        # reconciler releases the lease; decrementing the pool from here would
        # free a slot that a live container still occupies.
        "released_immediately": task.state.value == "CANCELLED",
        "children_cancelled": cascaded,
        # Whether this call asked the backend to stop the task's execution.
        "execution_cancel_requested": target is not None,
    }


# --------------------------------------------------------------------------
# The live-browser hand-off (docs/design/live-browser.md §4, lane LB-L10)
# --------------------------------------------------------------------------


def handoff_service(
    ctx: AppContext = Depends(get_context), children: ChildService = Depends(child_service)
) -> HandoffService:
    return HandoffService(
        settings=ctx.settings, db=ctx.db, store=ctx.store, children=children, now=ctx.now
    )


@router.post("/{task_id}/handoff", status_code=status.HTTP_201_CREATED)
async def request_handoff(
    task_id: str,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: HandoffService = Depends(handoff_service),
) -> dict:
    """WORKER ONLY (§4.1 step 3): no `current_auth`, no `tenant_scope`. The
    tenant is the worker's ID token checked against the body's, and the worker
    is the attempt key's signature over the RAW body; `swarm_api.handoff`
    holds every check."""
    raw = await request.body()
    wait, created = await run_in_threadpool(
        lambda: service.request(
            serverless_authorization or authorization,
            task_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return {"human_wait": wait, "created": created}


@router.post("/{task_id}/handoff/resolve")
async def resolve_handoff(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: HandoffService = Depends(handoff_service),
) -> dict:
    """WORKER ONLY, as `request_handoff`: the wait ended (expired, cancelled,
    live path lost, or returned)."""
    raw = await request.body()
    resolved = await run_in_threadpool(
        lambda: service.resolve(
            serverless_authorization or authorization,
            task_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    return {"resolved": resolved}


@router.post("/{task_id}/human-ready")
def human_ready(
    task_id: str,
    background: BackgroundTasks,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: HandoffService = Depends(handoff_service),
) -> dict:
    """§4.3: "I'm ready to sign in". The ONLY promotion of a
    PARKED(HUMAN_REQUIRED) task; it writes READY and nothing else, so the
    re-admission is an ordinary all-or-nothing one (invariants 1 and 2)."""
    require_member(ctx, auth, fresh=True)
    task = service.human_ready(tenant_id, task_id, email=auth.email, submitted_by=submitted_by)
    # Admission sooner than the safety tick: a person is waiting.
    background.add_task(
        ring, ctx.waker, ctx.metrics, "human_ready", tenant_id=task.tenant_id, task_id=task.id,
    )
    return {"task": task_to_api(task, console_url=ctx.settings.console_url)}


@router.get("/{task_id}/events")
def list_events(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    order: Literal["asc", "desc"] = Query(default="asc"),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One page of a task's events, and the token for the next.

    Without a token this is what it always was -- the OLDEST `limit` events,
    ascending -- so the web UI and `swarm_mcp.follow`, which pass neither
    parameter, get byte-for-byte the rows they got before. What is new is that
    the rest of the history is reachable: `next_page_token` is non-null exactly
    when more events exist in the requested order, and `order=desc` puts the
    END of a run on the first page.

    A token is bound to this task and to the order it was minted for; sending
    it anywhere else is a 422 rather than a plausible wrong page. See
    `Store._keyset_page` for why events that share a timestamp are neither
    dropped nor repeated at a page boundary.

    Each `detail` is masked by the task's masker, so the task is read first --
    which `Store.list_events` does anyway for its tenant check.
    """
    masking = masking_for(ctx.store.get_task(tenant_id, task_id, submitted_by=submitted_by))
    page = ctx.store.list_events(
        tenant_id,
        task_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
        descending=order == "desc",
    )
    return {
        "task_id": task_id,
        "events": [_event_to_api(e, masking) for e in page.items],
        "next_page_token": page.next_page_token,
    }


@router.get("/{task_id}/startup")
def read_startup(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The current attempt's start-up steps and the profile's usual times (STARTUP-API).

    The console's start-up progress bar (owner request 2026-10-13). The same
    scope as `GET /v1/tasks/{id}`: another tenant's task, or another
    submitter's for a continuation-scoped caller, is the same 404 as a
    missing one. See `swarm_api.startup` for the shape and the reads.
    """
    task = ctx.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    return startup_of(ctx.db, task, ctx.startup_estimates)


@router.get("/{task_id}/utilisation")
def read_utilisation(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The task's CPU, memory and throttling over time, newest attempt first (LIVE-UTIL-API).

    The owner, 2026-10-20 (#1689): use over time, not only a limit and a
    peak, and throttling measured. Points every 5 s, from the worker's
    HEARTBEAT events, while the task runs and after; the console polls it.
    The same scope as `GET /v1/tasks/{id}`: another tenant's task, or another
    submitter's for a continuation-scoped caller, is the same 404 as a
    missing one. See `swarm_api.utilisation` for the shape and the reads.
    """
    task = ctx.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    attempts = ctx.store.list_attempts(tenant_id, task_id, limit=UTILISATION_ATTEMPTS)
    return utilisation_of(ctx.db, task, attempts, read_at=ctx.now())


@router.get("/{task_id}/attempts")
def list_attempts(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every attempt for one task, newest first.

    P4, and the thing that makes a retried task legible. `result_summary` is
    written once, by `finish()`, at terminal state -- so a task that failed
    twice and succeeded on the third attempt carries ONLY attempt three's
    numbers, and the first two attempts' exit codes, errors and peak RSS were
    unreachable through any API.

    Tenant-scoped like events and artifacts, not admin-gated: these are the
    caller's own attempts.

    Each row carries the attempt's CPU -- `cpu_seconds`, `peak_cpu_cores`,
    `mean_cpu_cores`, `cpu_limit_cores` -- as typed fields (contract request
    #15, accepted on #184, 2026-09-25). They replaced #188's opt-in
    `include=usage`, which read the task's events once per request to find the
    worker's newest HEARTBEAT reading. That parameter is no longer read: an
    older client that still sends it gets the same rows, with the figures in
    them.

    And when they were written (contract request #26, accepted on #184,
    2026-09-26): `cpu_measured_at`, the worker's clock at the reading;
    `cpu_limit_source`, `cgroup` or `resource_class`; and
    `cpu_reading_age_seconds`, the reading's age against this response's
    `read_at`. All three are null on an attempt from before the change.
    """
    # Resolve the task first so a wrong id is a 404 about the TASK rather than
    # an empty attempt list, which would read as "this task never ran". Its
    # masker masks each attempt's `error` (the PR #229 review).
    task = ctx.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    masking = masking_for(task)
    attempts = ctx.store.list_attempts(
        tenant_id, task_id, limit=paged_limit(ctx, limit)
    )
    read_at = ctx.now()
    return {
        "task_id": task_id,
        # The clock each row's `cpu_reading_age_seconds` is taken against.
        "read_at": read_at,
        "attempts": [attempt_to_api(a, masking=masking, read_at=read_at) for a in attempts],
        # #379: each attempt's own account and the accounts it swapped away
        # from, keyed by `attempt_id`, from one bounded read of this task's
        # account events. BESIDE the rows rather than in them, so a row stays
        # the one shape `GET /v1/attempts` serves too (`attempt_to_api`).
        "accounts_by_attempt": accounts_for_attempts(ctx.db, task, attempts),
    }


@router.get("/{task_id}/artifacts")
def list_artifacts(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """What this attempt left in GCS, by reference.

    `complete` is the field that stops an empty list being read as an answer: it
    is false until the task reaches a terminal state and the worker writes its
    result summary, so "no artifacts yet" and "this task produced none" are
    distinguishable. `artifacts_skipped` lists the files the worker wrote and
    did not upload, for the same reason, as `{name, cause}` entries: the cause
    the worker recorded (`cap`, `upload_error`, `refused`; #165), or null for
    a name from an older summary. `artifacts_over_cap` counts the files
    past the 500-file cap (#227): null until `complete`, 0 when none were.
    `complete` alone does not mean nothing was left out.

    No download URL is minted here. The `uri` is a `gs://` reference, and the
    bytes are read through `/artifacts/content` and `/artifacts/raw`, which
    apply the tenant check and read-time redaction a signed URL would skip.

    Per entry (#184): `attempt_id` (the attempt its stored uri lies under),
    `kind` and `content_type` from the one name table, and `role` --
    `agent_stdout`, `agent_stderr` or `agent_transcript` for the agent's own
    streams, from `result_summary.agent_streams` (or the runner's naming
    convention for an attempt made before it existed). Top level: the
    manifest's `attempt_id`. No object is read.
    """
    return service.list_artifacts(
        tenant_id, task_id, limit=paged_limit(ctx, limit), submitted_by=submitted_by
    )


@router.get("/{task_id}/artifacts/content")
def read_artifact(
    task_id: str,
    name: str = Query(..., min_length=1, max_length=512),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """ONE artifact's content, resolved by the server from the task's manifest.

    THE PARAMETER IS A NAME AND NOT A PATH, and that is deliberate to the point
    of being the reason this route is shaped the way it is. A route that
    accepted a GCS path, or a key, or even a "relative" one, would be a
    path-traversal hole into another tenant's prefix the moment any segment
    check was missed -- and the only thing standing between it and invariant 9
    would be the completeness of a sanitiser. Here `name` is matched for exact
    equality against the entries `AgentLifecycle._upload_outputs` wrote into
    THIS task's `result_summary`, and the object key is rebuilt from the task
    document's own tenant and id. A caller who sends a path gets a 404 about an
    artifact this task does not list, because that is what it is.

    The tenant boundary is the SAME one the listing route uses and is not
    re-derived: `tenant_scope` decides the scope, `Store.get_task` 404s a task
    belonging to anyone else with the message a missing task gets, and only
    then does a key exist.

    BOUNDED AND REDACTED. `limit_bytes` is clamped into the service's range and
    every response carries `truncated` and `next_offset`; a cut window also
    carries a `detail` saying so in words, because a reader who takes a
    truncated transcript for the whole output has been misled by the response
    rather than by the data. Content is run through `swarm_api.redaction` on
    the way out whatever happened at write time -- see `read_artifact`'s
    docstring for why the worker's pass is not a guarantee this route may lean
    on. An artifact that is not text is reported as `binary` with no bytes, not
    base64-encoded: bytes nothing can scan are bytes this route does not serve
    -- `/artifacts/raw` is the route for bytes, and says what it did to them.

    `kind` and `content_type` (#184) are the listing's, from the same name
    table; the NUL sniff above stays the authority on whether it is text.
    """
    return service.read_artifact(
        tenant_id,
        task_id,
        submitted_by=submitted_by,
        name=name,
        offset=offset,
        limit_bytes=limit_bytes,
    )


@router.get("/{task_id}/artifacts/raw", response_class=StreamingResponse)
def read_artifact_raw(
    task_id: str,
    name: str = Query(..., min_length=1, max_length=512),
    disposition: str | None = Query(default=None),
    redaction: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    service: AgentOutputService = Depends(agent_output_service),
) -> StreamingResponse:
    """ONE artifact's BYTES: for an `<img>`, an "open full", and a download (#184).

    THROUGH THE API, NEVER A SIGNED URL -- the owner's decision, so the tenant
    check and read-time redaction apply to every byte. Resolved exactly as
    `/artifacts/content` resolves a name (a NAME from this task's manifest,
    never a path), and classified from the object's first 4096 bytes:

      * an image -- an image extension AND matching magic bytes -- is served as
        stored, `image/png|jpeg|gif|webp`, `X-Swarm-Redaction: not-applied`;
      * text -- no NUL -- is ALWAYS `text/plain; charset=utf-8`, never HTML or
        JSON, so nothing an agent wrote renders here; redacted window by
        window, `X-Swarm-Redaction: applied`;
      * anything else is `application/octet-stream`, an attachment whatever
        `disposition` asked, not redacted, and says so.

    `disposition` is `inline` or `attachment` (the default). Every error --
    404 not listed, 410 listed but reclaimed, 503 unreadable, 422 a bad
    disposition -- is the JSON envelope, sent before any byte. Chunked, with
    `X-Artifact-Bytes` carrying the stored size instead of a Content-Length.
    Staged inputs are read the same way, from the upstream task.

    `?tenant=` is accepted here (`auth.TENANT_QUERY_ROUTES`) because a browser
    fetching an `<img src>` or a download link sends no `X-Swarm-Tenant`. It
    is not a parameter of this function: `current_auth` reads it and puts it
    through the header's own membership check, so `tenant_scope` below is
    still the only tenant this body sees.

    `redaction` is `applied` (the default; the answer above, unchanged) or
    `none`: the OWNER-ONLY RAW PATCH (BGD's decision on #1857, 2026-10-27).
    A served `.patch` is redacted like any text, so its credential-shaped
    test fixtures come back masked and it no longer applies; the masking
    rules stay as they are and the person who SUBMITTED the task may take the
    bytes as stored. Honoured only for a top-level `.patch` name and a caller
    whose email is `Task.submitted_by` (compared as `submission_scope`
    compares it); anyone else -- another member, a tenant admin, a platform
    admin -- gets the 403 envelope naming the rule (`detail.rule`
    `patch_only` or `submitter_only`), and another tenant's id the same 404
    as before. Served `text/x-diff; charset=utf-8`, ALWAYS an attachment
    whatever `disposition` asked, `Cache-Control: no-store`,
    `X-Swarm-Redaction: not-applied`; every such read writes one
    `artifact_audit` record (who, which artifact, when -- never the content)
    before the first byte, and is refused 503 when it cannot.
    """
    raw = service.raw_artifact(
        tenant_id,
        task_id,
        name=name,
        disposition=disposition,
        submitted_by=submitted_by,
        redaction=redaction,
        caller=auth.email,
    )
    return StreamingResponse(raw.chunks, media_type=raw.media_type, headers=raw.headers)


@router.get("/{task_id}/checkpoints")
def list_checkpoints(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every checkpoint this task has written, across every attempt.

    READ-ONLY, and it has to be: a checkpoint is the entire value of an attempt
    that was parked or killed, and the only component allowed to remove one is
    `reconciler.checkpoints`, which decides by reference rather than by age.
    Nothing on this path writes, deletes or mutates an object or a document.

    TENANT SCOPE. `tenant_scope` -- not `auth.tenant_id` -- then
    `Store.get_task`, which 404s a task belonging to anyone else with the same
    message a missing task gets. Only after both does a prefix exist, and it is
    built from the task document's own tenant and id rather than from anything
    in the request. `attempt_id` can only NARROW that prefix and is validated as
    a single path segment first, so there is no value of it that reaches
    another tenant's objects.

    WHY ACROSS ATTEMPTS. A resume is by definition a new attempt, and it
    restores the checkpoint `task.latest_checkpoint` names -- or nothing, with
    no fallback to the newest one under the prefix (#347) -- so the checkpoint
    that matters after a crash was written by the attempt that died. Listing
    only the current attempt would hide it.

    THE THREE ANSWERS. A failed LISTING is a 503, never `{"checkpoints": []}`
    with a 200. A checkpoint whose manifest is missing is reported `absent` and
    `resumable: false` -- the manifest is the commit marker, so there is nothing
    to resume from. A checkpoint whose manifest could not be READ is reported
    `unreadable` with `resumable: null`, because "cannot resume" and "cannot
    tell" are different facts.
    """
    return ctx.inspection.list_checkpoints(
        tenant_id,
        task_id,
        submitted_by=submitted_by,
        attempt_id=attempt_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
    )


@router.get("/{task_id}/logs")
def read_logs(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    # Repeatable (#184): `?stream=agent_stderr&stream=stdout`. None serves the
    # runner's two streams, exactly as before.
    stream: list[str] | None = Query(default=None),
    source: str = Query(default="auto"),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """An attempt's captured stdout and stderr, redacted at read time.

    THE REDACTION IS NOT BELT-AND-BRACES, IT IS THE BRACES. The worker's own
    scrubbing pass replaces REGISTERED LITERAL VALUES -- the provider key this
    platform resolved out of Secret Manager -- and matches no patterns at all;
    both `_redact_before_upload` and `_publish_live_logs` skip the pass
    entirely when no secret was registered, which is every `mock`-profile run.
    So a token the agent minted, an `Authorization:` header a tool echoed, or
    an `.env` printed out of a cloned repository reaches the bucket in the
    clear. Everything served here is therefore run through
    `swarm_api.redaction` on the way out, unconditionally, and the response
    says so in `redaction.applied_at_read_time`.

    The same filter is applied to any upstream error string this route reports,
    because a storage client's exception can quote the request that failed and
    a signed URL is a credential with an expiry.

    Nothing in this route reads, logs or echoes a request header. The
    `Authorization` header is consumed by `deps.current_auth` and by nothing
    else in the process.

    TWO OBJECTS PER STREAM. `logs/<stream>.log` is the complete record, written
    once when the attempt finishes; `logs/live/<stream>.tail.log` is a bounded
    window republished every few seconds while it runs. `source=auto` (the
    default) prefers the record and falls back to the tail when the record is
    ABSENT -- never when it is unreadable, because serving a different object
    in place of a failed read reports success over the wrong window.

    Each stream reports `ok`, `absent` or `unreadable` independently, and
    `content` is null rather than `""` in the latter two, so a client that
    renders content without reading status shows nothing instead of an empty
    log that looks like a silent agent.

    WHOSE STREAMS (#184). `stdout` and `stderr` are the RUNNER process's --
    its own JSON log lines -- which is what the drawer's "Output, as the agent
    wrote it" panel showed under the wrong name. `agent_stdout` and
    `agent_stderr` are the agent CLI's own; `stream` repeats to ask for
    several. For a runner with no agent CLI they are `not_applicable`. The
    response carries `read_at`, and every stream its object's
    `object_updated_at` and `age_seconds`, so a live read says how old it is.
    """
    return ctx.inspection.read_logs(
        tenant_id,
        task_id,
        submitted_by=submitted_by,
        attempt_id=attempt_id,
        stream=stream,
        source=source,
        offset=offset,
        limit_bytes=limit_bytes,
    )


@router.get("/{task_id}/transcript")
def read_transcript(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    source: str = Query(default="auto"),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    include_raw: bool = Query(default=False),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """The agent's own stdout, parsed into steps: text, thinking, tool calls, results (#184).

    The same object `/logs?stream=agent_stdout` would serve -- the final copy,
    the live tail while it runs, or the runner's artifact for an attempt made
    before the worker published agent streams -- parsed ON THE SERVER, every
    string redacted after JSON decoding and capped at 16 KiB. Windows cut on
    newlines only. `format` is sniffed (`claude-stream-json`, `claude-json`,
    `ndjson`, `text`); for `text`, `steps` is null and the raw `/logs` window
    is the view. `include_raw=true` adds each event's own record, redacted.
    Absent and unreadable are 200s with `stream.status`.
    """
    return service.read_transcript(
        tenant_id,
        task_id,
        submitted_by=submitted_by,
        attempt_id=attempt_id,
        source=source,
        offset=offset,
        limit_bytes=limit_bytes,
        include_raw=include_raw,
    )


@router.get("/{task_id}/answer")
def read_answer(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """The agent's final answer, as Markdown, redacted (#184).

    The last `result` event's `result` in the agent's stdout -- never
    `result_summary.runner.summary`, which the runner cuts at 2,000 characters,
    unless there is no result event, and then marked as the cut summary it may
    be. `status` is `ok`, `not_yet` (still running), `absent` (ended with
    neither) or `unreadable` (a read failed; nothing further down is tried).
    """
    return service.read_answer(
        tenant_id, task_id, attempt_id=attempt_id, submitted_by=submitted_by
    )


@router.get("/{task_id}/questions")
def read_questions(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """What the task's agent asked its owner, from its `questions.json`, redacted.

    Only a file the worker validated and counted (`result_summary.questions`)
    is served, read through the artifact read's tenant check and redaction.
    `status` is `ok`, `none` (it asked none; `questions: []`, never a 404) or
    `unavailable` (counted, not readable, with `unavailable_because`). Each
    question carries the owner's `answer` (or null) -- its `choice`, `text`,
    `by`, `at`, `via` and `session`, and its `history` -- and `may_answer` /
    `may_answer_because` say whether the caller may answer. See
    `swarm_api.agent_questions`.
    """
    return agent_questions.read_questions(
        ctx.store, service, tenant_id, task_id, submitted_by=submitted_by,
        viewer=auth.email, viewer_is_admin=auth.is_admin,
        viewer_role_refusal=agent_questions.role_refusal(getattr(ctx, "roles", None), auth),
    )


@router.post("/{task_id}/answers")
def answer_question(
    task_id: str,
    body: agent_questions.AnswerBody,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """The owner answers one of the agent's questions: an option label, free text, or both.

    Only the task's submitter or an admin (403 for another member; another
    tenant's task is a 404). `question_index` must name a question the worker
    counted and `choice` one of THAT question's option labels; `text` is at
    most 2,000 characters and is redacted before it is stored. One answer per
    question: re-answering replaces it and keeps the replaced one in its
    `history`. Stored as `metadata.owner_answers`; answers with the questions
    as `GET /questions` now serves them. See `swarm_api.agent_questions`.

    `via` (`console` | `claude-code`) and `session` (a Claude Code session id:
    a UUID or 8-64 of `[A-Za-z0-9_-]`, else 422) say where it was answered;
    the sc plugin's `swarm_task_answer` sends `claude-code` and its session.
    Both are stored, kept in `history` and served. They are labels: neither
    widens nor narrows who may answer, which is decided before they are read.

    A `viewer` answers nothing (`role_member_required`, through its switch),
    their own task's agent included: the submitter's rule is on top of the
    role, never instead of it (workspace-roles.md §4.8).
    """
    require_member(ctx, auth, fresh=True)
    return agent_questions.answer_question(
        ctx.store, service, ctx.db, tenant_id, task_id, body,
        submitted_by=submitted_by, email=auth.email, is_admin=auth.is_admin, now=ctx.now(),
    )


@router.get("/{task_id}/input")
def read_input(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The task's input as a screen may draw it: masked at read time, counted (#184).

    The prompt, the rest of the input and the whole input, each as
    `redaction.redact` returns it with its own `redaction_count` -- the same
    redactor, and the same count, every other output this API serves carries
    -- and the task's metadata as `metadata`, from the same masker, with its
    own count. `GET /v1/tasks/{id}` serves the same masking of the input and
    the metadata as objects (owner decision, 2026-09-26: masked everywhere).
    See `swarm_api.task_input`.
    """
    task = ctx.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    return input_copy(task, read_at=ctx.now())
