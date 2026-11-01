"""Voice it session routes (docs/design/voice-it.md §3.6, §3.7, §10 phase 1).

    POST /v1/voice/sessions                                   {offer, project_id?}
        -> {project_id, session_id, state, new_project, answer}
    POST /v1/voice/projects/{project_id}/sessions/{session_id}/end
        -> {project_id, session_id, state, changed, voice_told}

WHO. Version one is BGD's only (§1): the caller's verified email must be in
`voice_people` (VOICE_IT_PEOPLE, else the platform owner), or the answer is
403 `forbidden` before anything is read. The workspace is the one the
console has selected, through `tenant_scope`, the same guard every
tenant-scoped route declares; the person is the verified email, never a
field of the body.

ORDER. Who first (403), then whether swarm-voice is deployed (503
`voice_unavailable`, nothing written), then the transaction that refuses a
second live session (409 `conflict`), then swarm-voice. The
browser's offer goes to swarm-voice unchanged and its answer comes back
unchanged; neither is logged or stored.

swarm-voice's client is `app.state.voice_client` when one is set (a test's
fake), else the deployment's from SWARM_VOICE_URL, else none.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ValidationFailed
from ..voice import sessions as vs

router = APIRouter(prefix="/v1/voice", tags=["voice"])

#: A project or session id this service minted: `vp_`/`vs_` and 32 hex.
_ID_MAX = 64


class StartSession(BaseModel):
    #: Not stripped: the SDP goes to swarm-voice byte for byte.
    model_config = ConfigDict(extra="forbid")

    offer: str = Field(min_length=1, max_length=vs.MAX_SDP_BYTES)
    project_id: str | None = Field(default=None, min_length=1, max_length=_ID_MAX)


def voice_client(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    client = getattr(request.app.state, "voice_client", None)
    if client is None:
        client = vs.client_for(ctx.settings)
        request.app.state.voice_client = client
    return client


def allowed_person(auth: AuthContext = Depends(current_auth),
                   ctx: AppContext = Depends(get_context)) -> str:
    """The caller's email when Voice it is open to them; 403 otherwise."""
    people = tuple(getattr(ctx.settings, "voice_people", ()) or ())
    if not vs.is_allowed(people, auth.email):
        raise vs.not_allowed()
    return auth.email.strip().lower()


def _service(ctx: AppContext, client: Any) -> vs.VoiceSessions:
    if client is None:
        raise vs.unavailable()
    return vs.VoiceSessions(ctx.db, client,
                            marker_key=str(getattr(ctx.settings, "child_key", "") or ""),
                            now=ctx.now)


def _check_id(value: str, prefix: str, what: str) -> None:
    if not value.startswith(prefix) or len(value) > _ID_MAX or not value[len(prefix):].isalnum():
        raise ValidationFailed(f"{what} is not a Voice it id", detail={"field": what})


@router.post("/sessions")
async def start_session(
    body: StartSession,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    service = _service(ctx, client)
    if not body.offer.startswith("v=0"):
        raise ValidationFailed("offer is not an SDP offer", detail={"field": "offer"})
    if body.project_id is not None:
        _check_id(body.project_id, "vp_", "project_id")
    return await run_in_threadpool(
        service.start, tenant_id=tenant_id, person=person, offer=body.offer,
        project_id=body.project_id)


@router.post("/projects/{project_id}/sessions/{session_id}/end")
async def end_session(
    project_id: str,
    session_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    service = _service(ctx, client)
    _check_id(project_id, "vp_", "project_id")
    _check_id(session_id, "vs_", "session_id")
    return await run_in_threadpool(
        service.end, tenant_id=tenant_id, person=person, project_id=project_id,
        session_id=session_id)
