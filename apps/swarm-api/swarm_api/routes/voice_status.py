"""A Voice it project's status view and the planner's brief
(swarm_api/voice/projectbrief.py).

    GET  /v1/voice/projects/{project_id}/status
        -> {project_id, title, state, open_cards, waiting_on_owner,
            cards: [{card_id, kind, title, state, summary, updated_at}],
            cards_total, roster: [{kind, id, title, state, done, total}],
            brief: {text, at, session_id} | null}
    POST /v1/voice/sessions/{session_id}/brief  {"text": "..."}
        -> {session_id, brief, budget}

WHO READS THE STATUS. The roster route's guards, unchanged: the caller on
the Voice it list (403), the project the caller's own in the console's
selected workspace (`tenant_scope`); another person's project, another
tenant's and a missing one are the same 404. A session grant reaches it (it
is on `voice.grant.ALLOWED_ROUTES`) as the session's person in the
session's workspace, and only for its own session's project: any other
project id is 403 before anything is read. So the fast front reads no more
than the person's own project page shows them.

WHO WRITES THE BRIEF. swarm-voice alone, through its session's grant, for
that session's project only, once after each planner turn. It asks the caps
first (`BudgetGuard.enforce`, as the work route does): a session at a cap is
409 `conflict` with `detail.reason` `voice_budget_capped` and nothing is
written. A person's call is 403: the brief is the planner's note.

It reads, or writes one field of the caller's own project; nothing here
admits, reserves or runs work.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..voice import grant as vgrant
from ..voice import ledger as vledger
from ..voice import projectbrief as vbrief
from ..voice import store as vstore
from .voice import _ender, _guard, allowed_person, speech_machine, voice_client

router = APIRouter(prefix="/v1/voice", tags=["voice"])

#: The two routes, as (method, template), for the grant's lists.
STATUS_ROUTE = ("GET", "/v1/voice/projects/{project_id}/status")
BRIEF_ROUTE = ("POST", "/v1/voice/sessions/{session_id}/brief")


class BriefWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Checked in bytes by `projectbrief.check_text`; this bound only keeps a
    #: body far over it from being read whole.
    text: str = Field(min_length=1, max_length=vbrief.BRIEF_TEXT_MAX * 4)


def _granted_session(request: Request, auth: AuthContext) -> vgrant.GrantSession:
    return vgrant.session_of(request.headers.get(vgrant.SESSION_HEADER),
                             tenant_id=auth.tenant_id, person=auth.email)


@router.get("/projects/{project_id}/status")
async def project_status(
    project_id: str,
    request: Request,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    vstore.check_id(project_id, "vp_", "project_id")
    if auth.via == vgrant.VIA and _granted_session(request, auth).project_id != project_id:
        raise vgrant.RouteNotGranted("a Voice it grant reads only its own session's project")
    store = vstore.VoiceStore(ctx.db, now=ctx.now)
    return await run_in_threadpool(lambda: vbrief.status(
        ctx.db, store, tenant_id=tenant_id, person=person, project_id=project_id))


@router.post("/sessions/{session_id}/brief")
async def write_brief(
    session_id: str,
    body: BriefWrite,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
    machine: Any = Depends(speech_machine),
) -> dict:
    if auth.via != vgrant.VIA:
        raise vgrant.RouteNotGranted("only swarm-voice writes a Voice it project's planner brief")
    vstore.check_id(session_id, "vs_", "session_id")
    session = _granted_session(request, auth)
    if session_id != session.session_id:
        raise vgrant.RouteNotGranted("a Voice it grant writes only to its own session")
    where = {"tenant_id": session.tenant_id, "person": session.person,
             "project_id": session.project_id, "session_id": session.session_id}
    guard = _guard(ctx, request, end=_ender(ctx, client, machine))
    state = await run_in_threadpool(lambda: guard.enforce(**where))
    if state.get("capped") is not None:
        raise vledger.VoiceCapReached(
            "Refused: this session reached its budget cap; no brief is written.",
            detail={"reason": vledger.SESSION_CAPPED, "line": state["capped"],
                    "ends_at": state.get("ends_at")})
    store = vstore.VoiceStore(ctx.db, now=ctx.now)
    brief = await run_in_threadpool(lambda: vbrief.write_brief(
        store, tenant_id=session.tenant_id, person=session.person,
        project_id=session.project_id, session_id=session.session_id, text=body.text,
        now=ctx.now))
    return {"session_id": session.session_id, "brief": brief, "budget": state}
