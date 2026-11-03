"""swarm-voice: GPT-Live sessions for Voice it (docs/design/voice-it.md §3.2, §3.4;
section 10, phase one).

A one-replica StatefulSet in `swarm-system` on the GKE cluster (§3.4), so a
voice session's sideband is held by exactly one process for the length of the
conversation, which no request-shaped service can do. It holds no data
authority: no Firestore client, no storage client. The browser never reaches it;
swarm-api calls it inside the VPC.

Routes:

* `POST /internal/sessions` -- swarm-api starts a voice session. Body:
  `voice_session_id` (swarm-api's session id), `person` (the signed-in person's
  email) and `offer` (the browser's SDP offer). swarm-voice leases a ChatGPT
  plan account from the quota broker's Voice it pool for `person`
  (accountlease.py; owner, 2026-11-02: "pool only, wait for it"), creates the
  GPT-Live session on that lease, attaches its sideband and answers
  `201 {voice_session_id, live_session_id, answer}`. When the broker leases
  nothing, nothing starts: 403 `{"code": "voice_pool_refused", "message"}` for
  a person outside the pool, 409 `{"code": "voice_pool_busy" |
  "voice_pool_refused", "message"}` when its account is in use or none is
  registered, `message` being one sentence the voice can say. 409 also when
  the voice session already has a live one; 502 when OpenAI or the broker
  failed (the status only). The lease is released when the session ends, when
  its start fails and when OpenAI closes it; if swarm-voice dies, the broker's
  hold expires on its own (ten-minute TTL, renewed every four minutes).
  No person's own ChatGPT sign-in is read: there is no Secret Manager path for
  the OpenAI credential any more.
  Optionally, together, `project_id` and `grant`: the session grant
  (grant.py, docs/design/voice-it.md §3.6), held in memory on the live session
  until it ends and presented only to swarm-api. A grant without a project, a
  grant for another session or one swarm-api did not mint is 422, and the
  refusal never carries it.
* `POST /internal/sessions/{voice_session_id}/end` -- closes the GPT-Live
  session and its sideband, which stops OpenAI's per-second billing.
  `200 {voice_session_id, ended, usage_seconds, reason}`, 404 for a session
  this process does not hold.
* `GET /healthz` -- counts only.

THE CALLER. Each `/internal/` call carries a Google ID token minted for
`SWARM_VOICE_AUDIENCE`; only the service accounts in `SWARM_VOICE_CALLERS`
(swarm-api's) are admitted. The audience is required: google-auth skips the
`aud` check when it is None (swarm_api/auth.py, GoogleTokenVerifier).

NOTHING SECRET LEAVES. A response carries the SDP answer and ids, never a
credential; an error carries a status, never a body; the leased token is held
in memory only (accountlease.PoolCredential) and never logged.

Configuration, read once at start (never at import):

* `PROJECT_ID` -- this deployment's project.
* `QUOTA_BROKER_URL` (and `QUOTA_BROKER_AUDIENCE`) -- the quota broker, whose
  Voice it pool leases each session's account and whose workspace pool leases
  each planner turn's Claude account (plannerlease.py). Required: without it
  no session could start.
* `SWARM_VOICE_AUDIENCE` -- the audience swarm-api mints its ID token for.
* `SWARM_VOICE_CALLERS` -- comma-separated service account emails admitted.
* `SWARM_API_URL` (and optionally `SWARM_API_AUDIENCE`) -- swarm-api's
  in-cluster address, where the planner (planner.build_planner) reads and
  draws cards as the session's person through the session grant. Unset, no
  session offers delegation. Each turn runs Claude Code (`CLAUDE_CODE_BIN`,
  `claude` on PATH) on a pool lease; a full pool is a spoken refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from .accountlease import NONE_AVAILABLE, NOT_IN_POOL, VoicePoolRefused, build_broker
from .credential import CredentialError
from .grant import GrantInvalid, SessionGrant
from .live import MAX_OFFER_BYTES, LiveApi, LiveError, SessionExists, Sessions
from .planner import build_planner

log = logging.getLogger("swarm_voice")

VOICE_ID = r"^[A-Za-z0-9_-]{8,128}$"

CallerCheck = Callable[[str], Awaitable[str]]


class StartRequest(BaseModel):
    voice_session_id: str = Field(pattern=VOICE_ID)
    person: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    offer: str = Field(min_length=4, max_length=MAX_OFFER_BYTES, pattern=r"^v=0\r?\n")
    #: The session grant and its project (grant.py). A SecretStr with no
    #: constraint, so neither its repr nor a validation error carries it: its
    #: shape is checked by `SessionGrant`, whose refusal names no value.
    project_id: str | None = Field(default=None, max_length=64)
    grant: SecretStr | None = None


def session_grant(body: StartRequest) -> SessionGrant | None:
    """The start call's grant, or None when swarm-api sent none; 422 when it is
    incomplete, for another session, or not one swarm-api mints."""
    if body.grant is None and body.project_id is None:
        return None
    if body.grant is None or body.project_id is None:
        raise HTTPException(422, "a session grant needs both project_id and grant")
    try:
        grant = SessionGrant(project_id=body.project_id, session_id=body.voice_session_id,
                             value=body.grant.get_secret_value())
    except GrantInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    return grant


#: What swarm-api is told when the Voice it pool leases no account: (status, code,
#: one sentence the voice or the page can say). Nothing starts on any of them.
POOL_REFUSALS: dict[str, tuple[int, str, str]] = {
    NOT_IN_POOL: (403, "voice_pool_refused",
                  "Voice it's ChatGPT plan isn't available to you, so this conversation can't start."),
    NONE_AVAILABLE: (409, "voice_pool_busy",
                     "Voice it's ChatGPT plan is in use by another session; try again when it ends."),
}
POOL_REFUSED_DEFAULT = (409, "voice_pool_refused",
                        "Voice it has no ChatGPT plan account set up yet, so this conversation can't start.")


def pool_refusal(reason: str) -> JSONResponse:
    status, code, message = POOL_REFUSALS.get(reason, POOL_REFUSED_DEFAULT)
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def google_caller_check(audience: str, callers: frozenset[str]) -> CallerCheck:
    """An ID-token check pinned to `audience` that admits only `callers`."""
    if not audience:
        raise RuntimeError("SWARM_VOICE_AUDIENCE is required: without it the token's audience is not checked")
    if not callers:
        raise RuntimeError("SWARM_VOICE_CALLERS is empty: nothing could call swarm-voice")
    request_holder: list[Any] = []

    def verify(token: str) -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        if not request_holder:
            request_holder.append(google_requests.Request())
        try:
            claims = id_token.verify_oauth2_token(token, request_holder[0], audience=audience)
        except ValueError:
            # google-auth's message may echo token content: never propagated.
            raise PermissionError("id token verification failed") from None
        email = claims.get("email")
        if not email or claims.get("email_verified") is False or email not in callers:
            raise PermissionError("the caller is not admitted")
        return str(email)

    async def check(token: str) -> str:
        return await asyncio.to_thread(verify, token)

    return check


def build_sessions(http: Any) -> Sessions:
    # Every session's account is leased from the quota broker's Voice it pool;
    # without QUOTA_BROKER_URL this raises and the process does not start.
    leases = build_broker(http)
    # The planner (§3.3) reaches swarm-api at SWARM_API_URL through the session
    # grant; without that address it is None and no session offers delegation.
    # Each planner turn leases its Claude account through the same broker client.
    return Sessions(LiveApi(http), leases=leases, planner=build_planner(http, broker=leases))


def build_caller_check() -> CallerCheck:
    callers = frozenset(c.strip() for c in os.environ.get("SWARM_VOICE_CALLERS", "").split(",") if c.strip())
    return google_caller_check(os.environ.get("SWARM_VOICE_AUDIENCE", "").strip(), callers)


def create_app(*, sessions: Sessions | None = None, verify_caller: CallerCheck | None = None) -> FastAPI:
    """The app. With no arguments (uvicorn's `--factory`), everything is built from
    the environment when the app starts, never at import."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = None
        if app.state.sessions is None:
            import httpx

            http = httpx.AsyncClient()
            app.state.sessions = build_sessions(http)
        if app.state.verify_caller is None:
            app.state.verify_caller = build_caller_check()
        try:
            yield
        finally:
            # A shutdown closes every session: one left open would bill with no sideband.
            await app.state.sessions.end_all()
            if http is not None:
                await http.aclose()

    app = FastAPI(title="swarm-voice", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    app.state.verify_caller = verify_caller

    async def caller(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "an ID token is required")
        try:
            return await app.state.verify_caller(token.strip())
        except PermissionError:
            raise HTTPException(401, "the caller is not admitted") from None

    @app.post("/internal/sessions", status_code=201)
    async def start(body: StartRequest, _caller: str = Depends(caller)) -> Any:
        grant = session_grant(body)
        try:
            session = await app.state.sessions.start(
                body.voice_session_id, person=body.person, offer=body.offer, grant=grant)
        except SessionExists:
            raise HTTPException(409, "this voice session already has a live session") from None
        except VoicePoolRefused as exc:
            log.info("voice session %s: the Voice it pool leased no account (%s)",
                     body.voice_session_id, exc.reason)
            return pool_refusal(exc.reason)
        except (CredentialError, LiveError) as exc:
            log.warning("voice session %s: start failed: %s", body.voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        return {"voice_session_id": session.voice_id, "live_session_id": session.live_id, "answer": session.answer}

    @app.post("/internal/sessions/{voice_session_id}/end")
    async def end(voice_session_id: str, _caller: str = Depends(caller)) -> dict:
        try:
            session = await app.state.sessions.end(voice_session_id)
        except (CredentialError, LiveError) as exc:
            log.warning("voice session %s: end failed: %s", voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        if session is None:
            raise HTTPException(404, "no such live session")
        return {"voice_session_id": voice_session_id, "ended": session.ended,
                "usage_seconds": session.usage_seconds, "reason": session.reason}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        sessions_ = app.state.sessions
        return JSONResponse(sessions_.counts() if sessions_ is not None else {"sessions": 0, "attached": 0})

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run("swarm_voice.main:create_app", factory=True, host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
