"""Voice it session routes (docs/design/voice-it.md §3.6, §3.7, §10 phase 1).

    POST /v1/voice/sessions                   {offer_sdp?, project_id?}
        -> {project_id, session_id, state, new_project, mode, answer_sdp (live only),
            fallback: {from, reason} (a speech session that became text)}
    POST /v1/voice/sessions/{session_id}/turns  {text}
        -> {turn_id, reply, cards, delegation_id (speech, when delegated)}
    POST /v1/voice/sessions/{session_id}/end
        -> {project_id, session_id, state, changed, voice_told}

    GET    /v1/voice/projects                 -> {projects: [...]}, newest first
    GET    /v1/voice/projects/{project_id}    -> the project
    PATCH  /v1/voice/projects/{project_id}    {title} -> the project
    DELETE /v1/voice/projects/{project_id}    {confirm} -> {project_id, deleted, documents, objects}
    GET    /v1/voice/preferences              -> {preferences, revision, can_undo, previous}
    PATCH  /v1/voice/preferences              {changes, revision?} -> the same, plus changed
    POST   /v1/voice/preferences/undo         {revision?} -> the same
    GET    /v1/voice/pool                     -> {pool, account, reason}
    GET    /v1/voice/sessions/{session_id}/ledger
        -> {session_id, date, time_zone, session, day, not_counted}

THE VOICE IT POOL (owner, 2026-11-02). Voice it talks on the ChatGPT plan
account the quota broker reserves in its "voice-it" pool; swarm-voice leases
it per session. `GET /v1/voice/pool` (Voice it › Preferences) asks the
broker, as this service's platform identity, which account the CALLER's next
session would lease, without leasing it: `{"pool": "voice-it", "account":
{label, display_name, provider} | null, "reason": "" | "not_in_voice_pool" |
"no_voice_account_registered" | "no_voice_account_available"}`. The person
is the verified email, never a query parameter, so nobody can ask about
someone else. No credential is read or served.

THE LEDGER (§3.8; swarm_api/voice/ledger.py) is computed on every read from
the session's seconds, its turns' usage and what Voice it started, against
$100 for the session and $300 for the person's day in their saved time zone.
The same who-guard and workspace: another tenant's session, or another
person's, is the same 404 as a missing one. It needs no swarm-voice.

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
missing one are the same 404. Each connect to a live session writes its
`heartbeat_at`, so an open tab never reads as abandoned. It ends itself before 600 seconds, under the
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

A REFUSED WRITE IS LOGGED, never its body (`RefusalLogged`). The card write
and every Voice it visual and note write run under a route class that logs a
4xx at WARNING as its route, status, code, the field and the reason: the
`ApiError`'s own message, or, for a body the request model refused, pydantic's
location and message, never the input. Each line goes through `redact`, so
nothing credential-shaped reaches the log either way. Before this, a refused
mock-up left only the request log's status and the reason could not be read.

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

WHO. Version one is the owner's only (§1): the caller's verified email must be in
`voice_people` (VOICE_IT_PEOPLE, else the platform owner), or the answer is
403 `forbidden` before anything is read. The workspace is the one the
console has selected, through `tenant_scope`, the same guard every
tenant-scoped route declares; the person is the verified email, never a
field of the body.

ORDER. Who first (403), then whether swarm-voice is deployed (503
`voice_unavailable`, nothing written), then the transaction that refuses a
second live session (409 `conflict`), then swarm-voice. No credential is
read or sent: swarm-voice leases the ChatGPT plan account from the quota
broker's Voice it pool itself, and its pool refusals (403, or 409
`voice_pool_busy`/`voice_pool_refused`) reach the browser with its sentence
(voice/sessions.py, `pool_refusal`). The
browser's offer goes to swarm-voice unchanged and its answer comes back
unchanged; neither is logged or stored.

THE ENGINE AND THE TURNS (docs/design/voice-it-own-speech.md §4.1, §10.2;
voice/sessions.py, `pick_engine`). The start takes no engine and no mode: the
body is `{offer_sdp?, project_id?}` and anything else is 422. It reads Voice
it › Settings once: own speech (the default) is a `speech` session, the fast
Claude front at swarm-voice and the speech machine brought to RUNNING, with
no SDP and no OpenAI pool asked; GPT-Live is `live`, today's path
(`offer_sdp` is required and `answer_sdp` comes back), and with no OpenAI API
key in the Voice it pool it is `text`, with `fallback: {from: "gpt-live",
reason: "no_openai_key"}`. An own-speech session that cannot start becomes
`text`, with `fallback: {from: "own", reason}` (`speech_not_deployed`,
`front_refused`, `no_l4`, `not_ready`). Any offer a
text or speech start is sent is dropped unread. The browser posts each turn
of a text or speech session to `/turns` as `{"text": 1..4000 characters}`,
answered `{turn_id, reply, cards}` (and `delegation_id` when the front
delegated). A turn passes the same guards as every Voice it action -- the
person on the Voice it list (403), the console's selected workspace through
`tenant_scope`, the session the caller's own there (else the same 404) --
and is 409 `conflict` (`voice_session_not_live`) on a session that is not a
live text or speech one; the plan's usage limit is 429 `rate_limited` with
`detail.resets_at`. It is not on the grant's lists (voice/grant.py): the
session grant is what swarm-voice's planner acts with during the turn, and
never what posts one. The person's text is never logged: it rides in the
body alone, and nothing here writes it anywhere.

THE SPEECH ROUTES (voice_speech.py: the audio socket and the machine's
sweep) are mounted on this module's `router`, beside the Voice it routes,
with their own paths and their own admission. Stop on a speech session also
stops the machine. The machine's lifecycle is `app.state.speech_machine`
when one is set (a test's), else voice_speech's `machine_for`, and none
while SWARM_SPEECH_* leave the engine undeployed.

swarm-voice's client is `app.state.voice_client` when one is set (a test's
fake), else the deployment's from SWARM_VOICE_URL, else none.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from swarm_redaction import redact

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ApiError, Conflict, ValidationFailed
from ..brokerclient import AccountPool
from .accounts import VOICE_POOL, account_pool
from .profile import preferences_store
from ..preferences import Preferences
from ..voice import cards as vcards
from ..voice import grant as vgrant
from ..voice import ledger as vledger
from ..voice import preferences as vprefs
from ..voice import sessions as vs
from ..voice import settings as vsettings
from ..voice import store as vstore
from ..voice import stream as vstream

router = APIRouter(prefix="/v1/voice", tags=["voice"])
log = logging.getLogger("swarm_api.voice.refused")

#: A refusal's field and reason are cut to this many characters in the log.
REFUSAL_LOG_MAX = 300


def _for_log(value: Any) -> str:
    return redact(str(value)).text.replace("\n", " ")[:REFUSAL_LOG_MAX]


def log_refusal(route: str, status: int, code: str, field: Any, reason: Any) -> None:
    """One WARNING line for a refused Voice it write: its route, status, code,
    field and reason, each redacted. Never the body: callers pass the error's
    own message or pydantic's location and message, not its input."""
    log.warning("voice write refused route=%s status=%s code=%s field=%s reason=%s",
                route, status, code, _for_log(field or "-"), _for_log(reason))


class RefusalLogged(APIRoute):
    """A write route whose refusals (4xx) are logged by `log_refusal`; the
    answer itself is the app's exception handlers', unchanged."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        route = self.path

        async def logged(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = list(exc.errors())
                first = errors[0] if errors else {}
                field = ".".join(str(part) for part in (first.get("loc") or ()))
                log_refusal(route, 422, ValidationFailed.code, field, first.get("msg") or "invalid")
                raise
            except ApiError as exc:
                if 400 <= exc.status_code < 500 and request.method != "GET":
                    log_refusal(route, exc.status_code, exc.code, exc.detail.get("field"),
                                exc.message)
                raise

        return logged


#: A project or session id this service minted: `vp_`/`vs_` and 32 hex.
_ID_MAX = 64


class StartSession(BaseModel):
    #: Not stripped: the SDP goes to swarm-voice byte for byte.
    model_config = ConfigDict(extra="forbid")

    #: Required for a Live session (Settings name GPT-Live); a speech or text
    #: session has no WebRTC and drops it unread. No engine is taken here.
    offer_sdp: str | None = Field(default=None, min_length=1, max_length=vs.MAX_SDP_BYTES)
    project_id: str | None = Field(default=None, min_length=1, max_length=_ID_MAX)


class TextTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=vs.MAX_TURN_CHARS)


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


def speech_machine(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """The speech machine's lifecycle, or None when the engine is not
    deployed here (SWARM_SPEECH_* empty)."""
    if not rvs.speech_config(request.app).enabled:
        return None
    machine = getattr(request.app.state, "speech_machine", None)
    return machine if machine is not None else rvs.machine_for(request.app, ctx)


def _service(ctx: AppContext, client: Any, machine: Any = None) -> vs.VoiceSessions:
    if client is None:
        raise vs.unavailable()
    return vs.VoiceSessions(ctx.db, client,
                            marker_key=str(getattr(ctx.settings, "child_key", "") or ""),
                            now=ctx.now,
                            class_models=ctx.settings.worker_class_models,
                            machine=machine)


#: The broker's reasons for "no account" (`GET /v1/voice/pool`); anything
#: else it sends reads as unavailable rather than passing an unknown word on.
POOL_REASONS = frozenset({"", "not_in_voice_pool", "no_voice_account_registered",
                          "no_voice_account_available"})


def _pool_answer(raw: Any) -> dict[str, Any]:
    """The broker's answer by the allow-list: the account's label, Name and
    provider (never its id, owner or anything else), and the reason."""
    raw = raw if isinstance(raw, dict) else {}
    account = raw.get("account")
    shown = None
    if isinstance(account, dict) and isinstance(account.get("label"), str):
        display = account.get("display_name")
        provider = account.get("provider")
        shown = {"label": account["label"],
                 "display_name": display if isinstance(display, str) else None,
                 "provider": provider if isinstance(provider, str) else None}
    reason = raw.get("reason")
    reason = reason if isinstance(reason, str) and reason in POOL_REASONS else (
        "" if shown is not None else "no_voice_account_available")
    return {"pool": VOICE_POOL, "account": shown, "reason": reason}


@router.get("/pool")
async def voice_pool(
    person: str = Depends(allowed_person),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Which Voice it pool account the caller's next session would lease."""
    return _pool_answer(await run_in_threadpool(pool.voice_pool, person))


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
    pool: AccountPool = Depends(account_pool),
    machine: Any = Depends(speech_machine),
) -> dict:
    service = _service(ctx, client, machine)
    if body.project_id is not None:
        _check_id(body.project_id, "vp_", "project_id")
    # Settings' engine, read once: no engine is ever taken from the caller.
    chosen = (await run_in_threadpool(vsettings.VoiceSettings(ctx.db, now=ctx.now).read))["settings"]
    pick = await run_in_threadpool(
        lambda: vs.pick_engine(chosen, pool, person, speech_deployed=machine is not None))
    if pick.mode == vs.MODE_LIVE and (body.offer_sdp is None or not body.offer_sdp.startswith("v=0")):
        raise ValidationFailed("offer_sdp is not an SDP offer", detail={"field": "offer_sdp"})

    def _start() -> dict:
        return service.start(tenant_id=tenant_id, person=person, offer=body.offer_sdp or "",
                             project_id=body.project_id, mode=pick.mode, fallback=pick.fallback,
                             chosen=chosen)

    return await run_in_threadpool(_start)


@router.post("/sessions/{session_id}/turns")
async def text_turn(
    session_id: str,
    body: TextTurn,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
) -> dict:
    service = _service(ctx, client)
    _check_id(session_id, "vs_", "session_id")
    if not body.text.strip():
        raise ValidationFailed("a turn needs some text", detail={"field": "text"})
    return await run_in_threadpool(
        service.turn, tenant_id=tenant_id, person=person, session_id=session_id, text=body.text)


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    client: Any = Depends(voice_client),
    machine: Any = Depends(speech_machine),
) -> dict:
    service = _service(ctx, client, machine)
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


router.add_api_route("/sessions/{session_id}/cards", write_card, methods=["POST"],
                     route_class_override=RefusalLogged)


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
    if found.state == vs.LIVE:
        # Each connect (about every nine minutes) keeps an open tab's session
        # from reading as abandoned (sessions.LIVE_STALE_SECONDS).
        await run_in_threadpool(vs.record_heartbeat, ctx.db, project_id=found.project_id,
                                session_id=found.session_id, now=ctx.now())
    if found.state == vs.ENDED and not await run_in_threadpool(stream.pending):
        # Ended and fully sent: 204 tells EventSource not to reconnect.
        return Response(status_code=204)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# The ledger (§3.8, §10 phase 3)
# --------------------------------------------------------------------------


def _ledger(ctx: AppContext, prefs: Preferences, request: Request) -> vledger.VoiceLedger:
    return vledger.VoiceLedger(
        ctx.db, now=ctx.now,
        prices=lambda: ctx.store.get_platform_settings()["model_prices"],
        time_zone=lambda email: prefs.read(email).time_zone,
        work=getattr(request.app.state, "voice_work", None))


@router.get("/sessions/{session_id}/ledger")
async def read_ledger(
    session_id: str,
    request: Request,
    person: str = Depends(allowed_person),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    prefs: Preferences = Depends(preferences_store),
) -> dict:
    _check_id(session_id, "vs_", "session_id")
    return await run_in_threadpool(
        _ledger(ctx, prefs, request).compute, tenant_id=tenant_id, person=person,
        session_id=session_id)


# --------------------------------------------------------------------------
# The speech routes (docs/design/voice-it-own-speech.md §14 row L6)
# --------------------------------------------------------------------------

# voice_speech's routes carry their own full paths (the audio socket under
# /v1/voice, the sweep under /v1/internal), already resolved when they were
# declared, so they join this router's list as they are and keep them; main.py
# includes `router` alone. Not `include_router`: FastAPI nests an included
# router lazily, and the route inventories read this router's routes flat.
# Imported last: voice_speech imports voice_readback, which imports
# `allowed_person` from this module.
from . import voice_speech as rvs  # noqa: E402

router.routes.extend(rvs.router.routes)
