"""A person's move of a Voice it project's state (docs/design/voice-it.md
§3.13.2; swarm_api/voice/projectstate.py).

    POST /v1/voice/projects/{project_id}/state
        {"action": "complete" | "cancel" | "archive" | "unarchive" | "reopen",
         "why": optional text}
        -> {project, cancelled, skipped, still_running}

Mark completed, Cancel, Archive, Unarchive and Reopen, from Projects (and
Talk's project menu). A move its state does not allow is 409; an unknown
action is 422.

CANCEL moves the project to `cancelled`, then cancels each of its queued or
running starts through the platform's own cancel route for its kind, called
here as the person, with the person's own permission on each: an agent by
`cancel_task` (`POST /v1/tasks/{id}/cancel`), a workflow by `cancel_workflow`
(`POST /v1/workflows/{id}/cancel`), an issue run through its running agent.
Their background work (stopping the execution, the finish wake) runs after
this response as it does after theirs. So a cancelled attempt's lease, its
fencing generation and its capacity are released as for any other cancel;
nothing here reserves or frees capacity. A start whose cancel fails is
answered in `still_running` and kept on the project, with its failure in the
move's `voice_audit` row.

WHO. The person alone: the caller on the Voice it list (403), the project the
caller's own in the console's selected workspace (`tenant_scope`); another
person's project, another tenant's and a missing one are the same 404. A
session grant is refused (403): the planner never cancels, archives or
reopens a project, and its own moves are `ProjectStates.planner_move`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import Conflict
from ..voice import grant as vgrant
from ..voice import projectstate as vstate
from ..voice import store as vstore
from . import tasks as task_routes
from . import workflows as workflow_routes
from .voice import allowed_person

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def platform_cancel(ctx: AppContext, auth: AuthContext, tenant_id: str,
                    background: BackgroundTasks) -> vstate.Cancel:
    """The platform's own cancel routes, called as the person in their
    workspace; raises when a cancel did not happen."""

    def cancel(kind: str, target_id: str) -> None:
        if kind == "agent":
            task_routes.cancel_task(target_id, background, tenant_id=tenant_id, auth=auth, ctx=ctx)
            return
        if kind == "workflow":
            answer = workflow_routes.cancel_workflow(
                target_id, background, tenant_id=tenant_id, auth=auth, ctx=ctx)
            if isinstance(answer, JSONResponse):
                raise Conflict(f"workflow {target_id!r}: the cancel was contended "
                               f"({answer.status_code}); cancel it again")
            return
        raise Conflict(f"no platform cancel for a {kind!r}")

    return cancel


@router.post("/projects/{project_id}/state")
async def move_project_state(
    project_id: str,
    background: BackgroundTasks,
    body: dict[str, Any] = Body(...),
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if auth.via == vgrant.VIA:
        raise vgrant.RouteNotGranted("a project's state is moved here by the person, never a grant")
    vstore.check_id(project_id, "vp_", "project_id")
    store = vstore.VoiceStore(ctx.db, now=ctx.now)
    states = vstate.ProjectStates(ctx.db, store, now=ctx.now)
    cancel = platform_cancel(ctx, auth, tenant_id, background)
    return await run_in_threadpool(lambda: states.person_move(
        tenant_id=tenant_id, person=person, project_id=project_id,
        action=body.get("action"), why=body.get("why"), cancel=cancel))
