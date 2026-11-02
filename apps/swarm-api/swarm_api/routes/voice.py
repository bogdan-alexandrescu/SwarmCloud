"""Voice it session routes (docs/design/voice-it.md §3.6, §3.7, §10 phase 1).

    POST /v1/voice/sessions                   {offer_sdp, project_id?}
        -> {project_id, session_id, state, new_project, answer_sdp}
    POST /v1/voice/sessions/{session_id}/end
        -> {project_id, session_id, state, changed, voice_told}

    GET    /v1/voice/projects                 -> {projects: [...]}, newest first
    GET    /v1/voice/projects/{project_id}    -> the project
    PATCH  /v1/voice/projects/{project_id}    {title} -> the project
    DELETE /v1/voice/projects/{project_id}    -> {project_id, deleted, documents, objects}
    GET    /v1/voice/preferences              -> {preferences, revision, can_undo, previous}
    PATCH  /v1/voice/preferences              {changes, revision?} -> the same, plus changed
    POST   /v1/voice/preferences/undo         {revision?} -> the same

PROJECTS AND PREFERENCES (§3.7, §3.11, §10 phase 2; swarm_api/voice/store.py
and voice/preferences.py). The same who-guard and the same workspace: a
project is read only when its tenant is the selected workspace and its person
is the caller, so another tenant's project, or another person's, is the same
404 as a missing one. Deleting a project removes its objects under
`tenants/<tenant>/voice/<project>/` and then its documents; it is 409 while one
of its sessions is live. Preferences are the person's own, keyed by an HMAC of
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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ValidationFailed
from ..voice import preferences as vprefs
from ..voice import sessions as vs
from ..voice import store as vstore

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
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _check_id(project_id, "vp_", "project_id")
    return await run_in_threadpool(
        _store(ctx, blobs).delete_project, tenant_id=tenant_id, person=person,
        project_id=project_id)


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
