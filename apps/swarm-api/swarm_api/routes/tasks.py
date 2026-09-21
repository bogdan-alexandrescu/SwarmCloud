"""Task routes.

Read the tenant argument on every store call below. It comes from
`tenant_scope`, never from `auth.tenant_id` and never from a path, a query
string or a header.

WHY THE DEPENDENCY RATHER THAN `auth.tenant_id`. This docstring used to claim
that deriving the id from the verified token was the whole boundary -- "there is
no request a caller can construct that reads another tenant's task". That was
wrong, and the counterexample is two VERIFIED identities holding the SAME id:
the frozen `tenant_id_for_group` slugs the local part only, so `eng@saga.xyz`
and `eng@partner.com` both derive `eng`, and a registered group `u-eng@saga.xyz`
derives what the personal tenant of `eng@saga.xyz` derives. Submitting was
already refused by `ensure_tenant`; listing, reading and CANCELLING were not.
`tenant_scope` applies that same principal check before any store call here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from swarm_common.states import TaskState

from ..auth import AuthContext
from ..codec import attempt_to_api, task_to_api
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..errors import ValidationFailed
from ..schemas import TaskBatchCreate, TaskCreate

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def _event_to_api(event) -> dict:
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "type": event.type.value,
        "at": event.at,
        "attempt_id": event.attempt_id,
        "lease_id": event.lease_id,
        "generation": event.generation,
        "detail": event.detail,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(
    body: TaskCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    result = ctx.submissions.submit_tasks(auth, [body])
    task = result.tasks[0]
    response.headers["Location"] = f"/v1/tasks/{task.id}"
    return {"task": task_to_api(task), "scheduler_woken": result.woke_scheduler}


@router.post("/batch", status_code=status.HTTP_201_CREATED)
def create_task_batch(
    body: TaskBatchCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    result = ctx.submissions.submit_tasks(auth, body.tasks)
    return {
        "tasks": [task_to_api(task) for task in result.tasks],
        "count": len(result.tasks),
        "scheduler_woken": result.woke_scheduler,
    }


@router.get("")
def list_tasks(
    state: str | None = Query(default=None),
    workflow_id: str | None = Query(default=None),
    runner_profile: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    parsed_state: TaskState | None = None
    if state is not None:
        try:
            parsed_state = TaskState(state)
        except ValueError:
            raise ValidationFailed(
                f"unknown state {state!r}",
                detail={"known_states": [s.value for s in TaskState]},
            ) from None
    page = ctx.store.list_tasks(
        tenant_id,
        state=parsed_state,
        workflow_id=workflow_id,
        runner_profile=runner_profile,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
    )
    return {
        "tasks": [task_to_api(task) for task in page.items],
        "next_page_token": page.next_page_token,
        "tenant_id": tenant_id,
    }


@router.get("/{task_id}")
def get_task(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"task": task_to_api(ctx.store.get_task(tenant_id, task_id))}


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: str,
    # Both: the scope decides WHOSE task may be cancelled, `auth` records WHO
    # cancelled it. Cancelling is a write, and it was reachable across a tenant
    # id collision until this dependency existed.
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    task = ctx.store.request_cancel(tenant_id, task_id, by=auth.email)
    return {
        "task": task_to_api(task),
        # A task holding capacity stays in its state until the worker or the
        # reconciler releases the lease; decrementing the pool from here would
        # free a slot that a live container still occupies.
        "released_immediately": task.state.value == "CANCELLED",
    }


@router.get("/{task_id}/events")
def list_events(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    events = ctx.store.list_events(tenant_id, task_id, limit=paged_limit(ctx, limit))
    return {"task_id": task_id, "events": [_event_to_api(e) for e in events]}


@router.get("/{task_id}/attempts")
def list_attempts(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
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
    """
    # Resolve the task first so a wrong id is a 404 about the TASK rather than
    # an empty attempt list, which would read as "this task never ran".
    ctx.store.get_task(tenant_id, task_id)
    attempts = ctx.store.list_attempts(
        tenant_id, task_id, limit=paged_limit(ctx, limit)
    )
    return {
        "task_id": task_id,
        "attempts": [attempt_to_api(a) for a in attempts],
    }


@router.get("/{task_id}/artifacts")
def list_artifacts(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """What this attempt left in GCS, by reference.

    `complete` is the field that stops an empty list being read as an answer: it
    is false until the task reaches a terminal state and the worker writes its
    result summary, so "no artifacts yet" and "this task produced none" are
    distinguishable. `artifacts_skipped` names the files the worker dropped at
    the size cap, for the same reason.

    No download URL is minted here. The `uri` is a `gs://` reference the caller
    reads with their own credentials, which keeps the tenant boundary in the one
    place IAM already enforces it.
    """
    result = ctx.store.list_artifacts(tenant_id, task_id, limit=paged_limit(ctx, limit))
    return {"task_id": task_id, **result}
