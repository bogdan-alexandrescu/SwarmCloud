"""Voice it session routes (docs/design/voice-it.md §3.6, §3.7, §10 phase 1).

    POST /v1/voice/sessions                   {offer_sdp, project_id?}
        -> {project_id, session_id, state, new_project, answer_sdp}
    POST /v1/voice/sessions/{session_id}/end
        -> {project_id, session_id, state, changed, voice_told}

    GET    /v1/voice/projects                 -> {projects: [...]}, newest first
    GET    /v1/voice/projects/{project_id}    -> the project
    PATCH  /v1/voice/projects/{project_id}    {title} -> the project
    DELETE /v1/voice/projects/{project_id}    {confirm} -> {project_id, deleted, documents, objects}
    GET    /v1/voice/preferences              -> {preferences, revision, can_undo, previous}
    PATCH  /v1/voice/preferences              {changes, revision?} -> the same, plus changed
    POST   /v1/voice/preferences/undo         {revision?} -> the same

    GET    /v1/voice/cards/schema             -> the card catalogue and the planner's tools
    POST   /v1/voice/sessions/{session_id}/cards  {card} -> {card}
    GET    /v1/voice/sessions/{session_id}/events[?after=N]
                                              -> text/event-stream, resumes by sequence

CARDS AND THE STREAM (§3.5, §4; swarm_api/voice/cards.py and voice/stream.py).
The schema route serves the one catalogue every card write is validated
against, with each kind as a strict `draw_<kind>` tool for the planner. The
stream takes NO tenant guard: `EventSource` sends no header, so the tenant is
the one the session document names, answered only when it is one the caller
belongs to on this request; another tenant's session, another person's, and a
missing one are the same 404. It ends itself before 600 seconds, under the
660-second request timeout, and the browser resumes from `Last-Event-ID`; once
the session has ended and everything is sent, a reconnect is 204.

THE CARD WRITE (§3.5, §3.6, §4). `POST /v1/voice/sessions/{session_id}/cards`
takes `{card}` and writes it through `cards.write_card` as the caller's
person, with the tenant, the project and the session from the session
document, never from the body or a header the writer chose; another tenant's
or another person's session is the same 404 as a missing one, and an ended
session is 409. It is the one write a session grant reaches
(`voice.grant.WRITE_ROUTES`): swarm-voice's planner draws its cards here, as
the session's person, with the `voice_audit` row every grant request writes
first. A grant writes only to its own session: any other session id is 403.
`VoiceStore.put_card` validates against the catalogue again, so no write path
bypasses it.

PROJECTS AND PREFERENCES (§3.7, §3.11, §10 phase 2; swarm_api/voice/store.py
and voice/preferences.py). The same who-guard and the same workspace: a
project is read only when its tenant is the selected workspace and its person
is the caller, so another tenant's project, or another person's, is the same
404 as a missing one. Deleting a project removes its objects under
`tenants/<tenant>/voice/<project>/` and then its documents; it is 409 while one
of its sessions is live, and 422 unless the body's `confirm` is the
project's title, typed. Preferences are the person's own, keyed by an HMAC of
the verified email, never by a field of the body. None of these routes needs
swarm-voice, so none answers 503 for its absence.

THE SHAPE is the Talk pane's (apps/swarm-ui/src/voice/rtc.ts,
startVoiceSession and endVoiceSession): the pane sends `offer_sdp`, reads
`answer_sdp`, and ends a session by its id alone. Ending finds the session's
project among the caller's own projects in the selected workspace, so
another tenant's session id, or another person's, is the same 404 as a
missing one.

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

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import Conflict, ValidationFailed
from ..voice import cards as vcards
from ..voice import grant as vgrant
from ..voice import preferences as vprefs
from ..voice import sessions as vs
from ..voice import store as vstore
from ..voice import stream as vstream

router = APIRouter(prefix="/v1/voice", tags=["voice"])

#: A project or session id this service minted: `vp_`/`vs_` and 32 hex.
_ID_MAX = 64


class StartSession(BaseModel):
    #: Not stripped: the SDP goes to swarm-voice byte for byte.
    model_config = ConfigDict(extra="forbid")

    offer_sdp: str = Field(min_length=1, max_length=vs.MAX_SDP_BYTES)
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
    if not body.offer_sdp.startswith("v=0"):
        raise ValidationFailed("offer_sdp is not an SDP offer", detail={"field": "offer_sdp"})
    if body.project_id is not None:
        _check_id(body.project_id, "vp_", "project_id")
    return await run_in_threadpool(
        service.start, tenant_id=tenant_id, person=person, offer=body.offer_sdp,
        project_id=body.project_id)


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    service = _service(ctx, client)
    _check_id(session_id, "vs_", "session_id")
    return await run_in_threadpool(
        service.end, tenant_id=tenant_id, person=person, session_id=session_id)


# --------------------------------------------------------------------------
# Projects and preferences (§3.7, §3.11, §10 phase 2)
# --------------------------------------------------------------------------


class RenameProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=vstore.TITLE_MAX)


class DeleteProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The project's title, typed: the delete's confirmation.
    confirm: str | None = Field(default=None, max_length=vstore.TITLE_MAX)


class EditPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any]
    revision: int | None = Field(default=None, ge=0)


class UndoPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int | None = Field(default=None, ge=0)


def voice_blobs(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """`app.state.voice_blobs` when one is set (a test's), else the artifact
    bucket's, else none (no bucket: nothing was ever written there)."""
    if not hasattr(request.app.state, "voice_blobs"):
        core = getattr(ctx.settings, "core", None)
        request.app.state.voice_blobs = vstore.build_voice_blobs(
            bucket=str(getattr(core, "artifact_bucket", "") or ""),
            project_id=getattr(ctx.settings, "project_id", None))
    return request.app.state.voice_blobs


def _store(ctx: AppContext, blobs: Any) -> vstore.VoiceStore:
    return vstore.VoiceStore(ctx.db, now=ctx.now, blobs=blobs)


def _preferences(ctx: AppContext) -> vprefs.VoicePreferences:
    return vprefs.VoicePreferences(
        ctx.db, key=str(getattr(ctx.settings, "child_key", "") or ""), now=ctx.now)


@router.get("/projects")
async def list_projects(
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    rows = await run_in_threadpool(
        _store(ctx, blobs).list_projects, tenant_id=tenant_id, person=person)
    return {"projects": rows}


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _check_id(project_id, "vp_", "project_id")
    return await run_in_threadpool(
        _store(ctx, blobs).get_project, tenant_id=tenant_id, person=person,
        project_id=project_id)


@router.patch("/projects/{project_id}")
async def rename_project(
    project_id: str,
    body: RenameProject,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _check_id(project_id, "vp_", "project_id")
    return await run_in_threadpool(
        _store(ctx, blobs).rename_project, tenant_id=tenant_id, person=person,
        project_id=project_id, title=body.title)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    body: DeleteProject | None = None,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _check_id(project_id, "vp_", "project_id")
    return await run_in_threadpool(
        _store(ctx, blobs).delete_project, tenant_id=tenant_id, person=person,
        project_id=project_id, confirm=body.confirm if body else None)


@router.get("/preferences")
async def read_preferences(
    person: str = Depends(allowed_person),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return await run_in_threadpool(_preferences(ctx).read, person)


@router.patch("/preferences")
async def edit_preferences(
    body: EditPreferences,
    person: str = Depends(allowed_person),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return await run_in_threadpool(
        _preferences(ctx).edit, person, body.changes, revision=body.revision)


@router.post("/preferences/undo")
async def undo_preferences(
    body: UndoPreferences,
    person: str = Depends(allowed_person),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return await run_in_threadpool(_preferences(ctx).undo, person, revision=body.revision)



# --------------------------------------------------------------------------
# The card catalogue and the card stream (§3.5, §4, §10 phase 2)
# --------------------------------------------------------------------------


@router.get("/cards/schema")
async def card_schema(person: str = Depends(allowed_person)) -> dict:
    return vcards.catalogue()


class WriteCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The writer's fields of one card; `cards.validate` judges them.
    card: dict[str, Any]


@router.post("/sessions/{session_id}/cards")
async def write_card(
    session_id: str,
    body: WriteCard,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _check_id(session_id, "vs_", "session_id")
    tenants = vstream.tenants_of(auth)
    if auth.via == vgrant.VIA:
        # A grant writes only to the session it was minted for, in its
        # session's workspace (the authenticator resolved it to that one).
        if auth.voice_session != session_id:
            raise vgrant.RouteNotGranted("a Voice it grant writes only to its own session")
        tenants = frozenset({auth.tenant_id})
    found = await run_in_threadpool(
        vstream.find_session, ctx.db, person=person, session_id=session_id, tenants=tenants)
    if found.state != vs.LIVE:
        raise Conflict(f"voice session {session_id!r} has ended; no card is drawn on it")
    card = await run_in_threadpool(
        vcards.write_card, vstore.VoiceStore(ctx.db, now=ctx.now), tenant_id=found.tenant_id,
        person=person, project_id=found.project_id, session_id=found.session_id, card=body.card)
    return {"card": card}


def stream_timing(request: Request) -> dict[str, Any]:
    """`app.state.voice_stream_timing` when one is set (a test's fake clock
    and sleep), else the real ones."""
    return dict(getattr(request.app.state, "voice_stream_timing", None) or {})


@router.get("/sessions/{session_id}/events", response_model=None)
async def session_events(
    session_id: str,
    request: Request,
    after: int | None = Query(default=None, ge=0),
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    timing: dict[str, Any] = Depends(stream_timing),
) -> Response:
    _check_id(session_id, "vs_", "session_id")
    start_after = vstream.resume_after(request.headers.get("last-event-id"), after)
    found = await run_in_threadpool(
        vstream.find_session, ctx.db, person=person, session_id=session_id,
        tenants=vstream.tenants_of(auth))
    stream = vstream.CardStream(vstore.VoiceStore(ctx.db, now=ctx.now), ctx.db,
                                found=found, person=person, after=start_after, **timing)
    if found.state == vs.ENDED and not await run_in_threadpool(stream.pending):
        # Ended and fully sent: 204 tells EventSource not to reconnect.
        return Response(status_code=204)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
