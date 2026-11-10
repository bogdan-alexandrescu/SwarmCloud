"""Voice it's visual and note routes (docs/design/voice-it-own-speech.md §8.3;
swarm_api/voice/visuals.py).

    GET   /v1/voice/projects/{project_id}/visuals              -> {visuals}
    GET   /v1/voice/projects/{project_id}/visuals/{visual_id}  -> {visual, body, notes}
    POST  /v1/voice/sessions/{session_id}/visuals
          {visual} | {focus} | {approve} | {approved: true}
        -> {visual} | {focus} | {visual} | {approved}
    POST  /v1/voice/sessions/{session_id}/pointer    {visual_id, anchor} -> {pointer}
    POST  /v1/voice/projects/{project_id}/visuals/{visual_id}/notes
          {note, session_id?} | {to_work, session_id?}    -> {note} | {work, notes}
    PATCH /v1/voice/projects/{project_id}/visuals/{visual_id}/notes/{note_id}
          {text?, state?, session_id?}                     -> {note}
    POST  /v1/voice/projects/{project_id}/visuals/{visual_id}/notes/work
          {note_ids, kind, title?, session_id?}            -> {work, notes}
    POST  /v1/voice/projects/{project_id}/visuals/{visual_id}/approval
          {frame, session_id?}                             -> {visual}

WHO. The same guards as every Voice it route: the caller on the Voice it list
(403), and the session or project the caller's own in the console's selected
workspace (`tenant_scope`), else the same 404 as a missing one.

THE SESSION GRANT reaches two of these, and only the two `voice.grant.
WRITE_ROUTES` names, both classed as heard (`voice.readback.AS_HEARD_ROUTES`):
the visual write, and the note write. Under a grant the tenant is the session
document's and the person the session's, never the body's; the visual write
takes only the grant's own session (else 403), and the note write only the
grant's own session's project (else 403). So the planner's draws and reads
(`visual`), its `speak` `point_at` (`focus`, §8.5), its spoken notes (`note`)
and its `notes_to_work` (`to_work`, §8.4) all ride on those two routes, and no
other write is open to a grant. The pointer, the note edit and the notes-to-
work route are the page's: a grant reaching them is 403 before anything runs.

A MOCK-UP'S APPROVAL (`visuals.Visuals.approve`). The person's spoken pick
("go with the second one") rides on the visual write the grant already
reaches, as `{approve: {visual_id, frame}}`, and the planner reads what was
approved there too, as `{approved: true}`: no route joins the grant's lists.
On the Stage the person approves through the approval route, the page's
alone like the note edit. Either way the visual is looked up in the caller's
own project and tenant, so another workspace's visual is the same 404 as a
missing one. An approval records a choice; it deploys and starts nothing.

A `code` OR `log` VISUAL IS READ HERE, never sent: the planner names the source
(`{repo, ref, path, from, to}`, `{repo, pull}` or `{task_id, step}`) and the
read runs as the session's person through their repository grant
(`visuals.repository_scope`) with the GitHub App installation token
(`visuals.GitHubRepoReader`), or through the task store in the session's
tenant (`visuals.log_tail`). What comes back is redacted before it is stored.
A body is answered as a JSON string, never as HTML.

swarm-voice's repository reader is `app.state.voice_repo_reader` when one is
set (a test's fake), else the GitHub App's over `forge_installations`.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from swarm_redaction import redact

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, submission_scope, tenant_scope
from ..errors import Conflict, ValidationFailed
from ..voice import grant as vgrant
from ..voice import sessions as vs
from ..voice import store as vstore
from ..voice import stream as vstream
from ..voice import visuals as vvis
from .voice import allowed_person, voice_blobs

router = APIRouter(prefix="/v1/voice", tags=["voice"])


class VisualBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_id: str | None = None
    kind: Literal["mockup", "diagram", "spec", "code", "log"]
    title: str = Field(min_length=1, max_length=120)
    #: The drawn kinds' text; null for `code` and `log`, which are read here.
    body: str | None = Field(default=None, max_length=vvis.BODY_MAX_BYTES)
    parts: list[dict[str, Any]] | None = None
    source: dict[str, Any] | None = None


class FocusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_id: str
    anchor: dict[str, Any] | None = None
    caption: str | None = Field(default=None, max_length=500)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_id: str
    #: The frame, by its id or its label.
    frame: str = Field(min_length=1, max_length=120)


class WriteVisual(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual: VisualBody | None = None
    focus: FocusBody | None = None
    approve: ApproveBody | None = None
    #: True: answer the project's approved mock-ups, for the planner.
    approved: Literal[True] | None = None


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame: str = Field(min_length=1, max_length=120)
    session_id: str | None = Field(default=None, max_length=64)


class Pointer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_id: str
    anchor: dict[str, Any] | None = None


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=vvis.NOTE_MAX)
    via: Literal["voice", "typed", "tap"]
    anchor: dict[str, Any] | None = None
    #: A part named by its id or label ("the header"), for a spoken note.
    part: str | None = Field(default=None, max_length=120)


class ToWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_ids: list[str] = Field(min_length=1, max_length=vvis.NOTES_TO_WORK_MAX)
    kind: Literal["follow_up", "issue", "lane_brief"]
    title: str | None = Field(default=None, min_length=1, max_length=120)


class WriteNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: NoteBody | None = None
    to_work: ToWork | None = None
    #: The page's live session, so the note reaches its stream; a grant's is its own.
    session_id: str | None = Field(default=None, max_length=64)


class EditNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=vvis.NOTE_MAX)
    state: Literal["open", "resolved"] | None = None
    session_id: str | None = Field(default=None, max_length=64)


class NotesWork(ToWork):
    session_id: str | None = Field(default=None, max_length=64)


def repo_reader(request: Request, ctx: AppContext = Depends(get_context)) -> vvis.RepoReader:
    reader = getattr(request.app.state, "voice_repo_reader", None)
    if reader is None:
        reader = vvis.GitHubRepoReader(getattr(ctx, "forge_installations", None))
        request.app.state.voice_repo_reader = reader
    return reader


def _visuals(ctx: AppContext, blobs: Any) -> vvis.Visuals:
    return vvis.Visuals(ctx.db, now=ctx.now, blobs=blobs)


def _person_only(auth: AuthContext) -> None:
    if auth.via == vgrant.VIA:
        raise vgrant.RouteNotGranted("this route is the page's; a Voice it grant does not reach it")


def _live(ctx: AppContext, auth: AuthContext, person: str, session_id: str) -> vvis.Where:
    """The caller's live session; under a grant only the grant's own."""
    vstore.check_id(session_id, "vs_", "session_id")
    tenants = vstream.tenants_of(auth)
    if auth.via == vgrant.VIA:
        if auth.voice_session != session_id:
            raise vgrant.RouteNotGranted("a Voice it grant writes only to its own session")
        tenants = frozenset({auth.tenant_id})
    found = vstream.find_session(ctx.db, person=person, session_id=session_id, tenants=tenants)
    if found.state != vs.LIVE:
        raise Conflict(f"voice session {session_id!r} has ended; nothing is drawn on it")
    return vvis.Where(tenant_id=found.tenant_id, person=person, project_id=found.project_id,
                      session_id=found.session_id)


def _project(ctx: AppContext, auth: AuthContext, person: str, project_id: str,
             session_id: str | None) -> vvis.Where:
    """The project a note write belongs to. Under a grant: the grant's own
    session's project, in its tenant; otherwise the selected workspace's,
    with the page's live session when it names one."""
    vstore.check_id(project_id, "vp_", "project_id")
    if auth.via == vgrant.VIA:
        where = _live(ctx, auth, person, auth.voice_session)
        if where.project_id != project_id or (session_id not in (None, auth.voice_session)):
            raise vgrant.RouteNotGranted("a Voice it grant writes only to its own session's project")
        return where
    tenant_id = tenant_scope(auth, ctx)
    if session_id is not None:
        where = _live(ctx, auth, person, session_id)
        if where.project_id != project_id or where.tenant_id != tenant_id:
            raise ValidationFailed("session_id is a live session of this project",
                                   detail={"field": "session_id"})
        return where
    return vvis.Where(tenant_id=tenant_id, person=person, project_id=project_id, session_id=None)


def _read(ctx: AppContext, reader: vvis.RepoReader, where: vvis.Where, body: VisualBody,
          submitted_by: str | None) -> tuple[str, list[dict[str, str]], dict | None, dict | None]:
    """(body, parts, source view, lines) of the visual `body` asks for."""
    source = vvis.check_source(body.kind, body.source)
    if source is None:
        if body.body is None:
            raise ValidationFailed(f"body is required for a {body.kind}", detail={"field": "body"})
        if body.kind == "spec":
            parts = vvis.spec_parts(body.body)
        elif body.kind == "mockup":
            text, parts = vvis.check_frames(body.body)
            return text, parts, None, None
        else:
            parts = vvis.check_parts(body.parts)
        lines = vvis._count_lines(body.body) if body.kind == "spec" else None
        return body.body, parts, None, lines
    if body.body is not None or body.parts:
        raise ValidationFailed(f"a {body.kind} is read by swarm-api; send its source only",
                               detail={"field": "body"})
    if isinstance(source, vvis.LogSource):
        text = vvis.log_tail(ctx.store, tenant_id=where.tenant_id, submitted_by=submitted_by,
                             source=source)
        text = redact(text).text
        return text, [], source.view(), vvis._count_lines(text)
    vvis.repository_scope(ctx.db, tenant_id=where.tenant_id, person=where.person,
                          repo=source.repo, at=ctx.now())
    owner, name = source.repo.split("/", 1)
    if isinstance(source, vvis.DiffSource):
        text = redact(reader.read_diff(where.tenant_id, owner, name, source.pull)).text
        return text, [], source.view(), vvis._count_lines(text)
    whole = reader.read_file(where.tenant_id, owner, name, source.ref, source.path)
    text, lines = vvis.file_slice(redact(whole).text, source.start, source.end)
    return text, [], source.view(), lines


@router.get("/projects/{project_id}/visuals")
async def list_visuals(
    project_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    vstore.check_id(project_id, "vp_", "project_id")
    where = vvis.Where(tenant_id=tenant_id, person=person, project_id=project_id, session_id=None)
    return {"visuals": await run_in_threadpool(_visuals(ctx, blobs).list, where)}


@router.get("/projects/{project_id}/visuals/{visual_id}")
async def get_visual(
    project_id: str,
    visual_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    vstore.check_id(project_id, "vp_", "project_id")
    where = vvis.Where(tenant_id=tenant_id, person=person, project_id=project_id, session_id=None)
    return await run_in_threadpool(_visuals(ctx, blobs).get, where, visual_id)


@router.post("/sessions/{session_id}/visuals")
async def write_visual(
    session_id: str,
    body: WriteVisual,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
    reader: vvis.RepoReader = Depends(repo_reader),
    submitted_by: str | None = Depends(submission_scope),
) -> dict:
    given = [body.visual, body.focus, body.approve, body.approved]
    if sum(v is not None for v in given) != 1:
        raise ValidationFailed("the body is {visual}, {focus}, {approve} or {approved}",
                               detail={"field": "visual"})
    where = await run_in_threadpool(_live, ctx, auth, person, session_id)
    visuals = _visuals(ctx, blobs)
    if body.approve is not None:
        view = await run_in_threadpool(
            visuals.approve, where, visual_id=body.approve.visual_id, frame=body.approve.frame,
            via="voice" if auth.via == vgrant.VIA else "stage")
        return {"visual": view}
    if body.approved is not None:
        return {"approved": await run_in_threadpool(visuals.approved, where)}
    if body.focus is not None:
        focus = await run_in_threadpool(
            visuals.focus, where, visual_id=body.focus.visual_id, anchor=body.focus.anchor,
            caption=body.focus.caption)
        return {"focus": focus}
    visual = body.visual
    assert visual is not None
    text, parts, source, lines = await run_in_threadpool(
        _read, ctx, reader, where, visual, submitted_by)
    view = await run_in_threadpool(
        visuals.put, where, kind=visual.kind, title=visual.title, body=text, parts=parts,
        source=source, lines=lines,
        created_by="planner" if auth.via == vgrant.VIA else "person",
        visual_id=None if visual.visual_id is None else vvis.check_visual_id(visual.visual_id))
    return {"visual": view}


@router.post("/sessions/{session_id}/pointer")
async def write_pointer(
    session_id: str,
    body: Pointer,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _person_only(auth)
    where = await run_in_threadpool(_live, ctx, auth, person, session_id)
    pointer = await run_in_threadpool(
        _visuals(ctx, blobs).point, where, visual_id=body.visual_id, anchor=body.anchor)
    return {"pointer": pointer}


@router.post("/projects/{project_id}/visuals/{visual_id}/notes")
async def write_note(
    project_id: str,
    visual_id: str,
    body: WriteNote,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    if (body.note is None) == (body.to_work is None):
        raise ValidationFailed("the body is {note} or {to_work}", detail={"field": "note"})
    where = await run_in_threadpool(_project, ctx, auth, person, project_id, body.session_id)
    visuals = _visuals(ctx, blobs)
    if body.to_work is not None:
        return await run_in_threadpool(
            visuals.notes_to_work, where, visual_id=visual_id, note_ids=body.to_work.note_ids,
            kind=body.to_work.kind, title=body.to_work.title)
    note = body.note
    assert note is not None
    if auth.via == vgrant.VIA and note.via != "voice":
        raise ValidationFailed("a note the planner pins is a spoken one (via voice)",
                               detail={"field": "via"})
    view = await run_in_threadpool(
        visuals.add_note, where, visual_id=visual_id, text=note.text, via=note.via,
        anchor=note.anchor, part=note.part)
    return {"note": view}


@router.patch("/projects/{project_id}/visuals/{visual_id}/notes/{note_id}")
async def edit_note(
    project_id: str,
    visual_id: str,
    note_id: str,
    body: EditNote,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _person_only(auth)
    where = await run_in_threadpool(_project, ctx, auth, person, project_id, body.session_id)
    note = await run_in_threadpool(
        _visuals(ctx, blobs).edit_note, where, visual_id=visual_id, note_id=note_id,
        text=body.text, state=body.state)
    return {"note": note}


@router.post("/projects/{project_id}/visuals/{visual_id}/notes/work")
async def notes_to_work(
    project_id: str,
    visual_id: str,
    body: NotesWork,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _person_only(auth)
    where = await run_in_threadpool(_project, ctx, auth, person, project_id, body.session_id)
    return await run_in_threadpool(
        _visuals(ctx, blobs).notes_to_work, where, visual_id=visual_id, note_ids=body.note_ids,
        kind=body.kind, title=body.title)


@router.post("/projects/{project_id}/visuals/{visual_id}/approval")
async def approve_visual(
    project_id: str,
    visual_id: str,
    body: Approval,
    person: str = Depends(allowed_person),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    blobs: Any = Depends(voice_blobs),
) -> dict:
    _person_only(auth)
    where = await run_in_threadpool(_project, ctx, auth, person, project_id, body.session_id)
    view = await run_in_threadpool(
        _visuals(ctx, blobs).approve, where, visual_id=visual_id, frame=body.frame, via="stage")
    return {"visual": view}
