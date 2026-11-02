"""Voice it › Preferences: the caller's "Sign in with ChatGPT" (swarm_api.voice.signin).

    GET  /v1/voice/chatgpt             -> {stored, stored_at}
    POST /v1/voice/chatgpt/authorize   -> {authorize_url, state, redirect_uri,
                                           expires_in_seconds, instructions}
    POST /v1/voice/chatgpt/exchange    {state, callback} -> {stored, stored_at}

WHO. Voice it's allowed people only, the check the session routes make
(`routes.voice.allowed_person`: VOICE_IT_PEOPLE, else the platform owner), so
anyone else is 403 before anything is read or written. The person is the
verified email and nothing else: no body names a person, a tenant or a secret,
and the bodies forbid every field they do not name.

WHAT IS WRITTEN. One new version of the caller's own personal secret
`swarm-tenant-<personal tenant>-openai`, in the shape swarm-voice's
credential.py parses, through Config › Secrets' writer (`routes.workspace_secrets.
secret_writer`: `app.state.workspace_secret_writer` when a test sets one). No
answer carries the refresh token, the code or the PKCE verifier.

NOT ON THE SESSION GRANT'S ALLOWLIST. A secret value is never reachable with a
Voice it session grant (voice-it.md §3.6); these routes take a person's own
sign-in and nothing else.

OpenAI's token endpoint is `app.state.voice_chatgpt_tokens` when one is set (a
test's fake), else the real one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from ..deps import AppContext, get_context
from ..schemas import StrictModel
from ..voice import signin as vsi
from ..workspace_secrets import SecretWriter
from .voice import allowed_person
from .workspace_secrets import secret_writer

router = APIRouter(prefix="/v1/voice/chatgpt", tags=["voice"])

#: Its body carries an OAuth authorization code: in `main.UNCAPTURED_ROUTES`.
EXCHANGE_ROUTE = "/v1/voice/chatgpt/exchange"


class FinishSignIn(StrictModel):
    #: The state `/authorize` returned: which pending sign-in this finishes.
    state: str = Field(min_length=8, max_length=256)
    #: What the person pasted: the callback address, `<code>#<state>`, or the code.
    #: Write-only: never echoed, and the validation handler never echoes a body.
    callback: str = Field(min_length=4, max_length=vsi.MAX_PASTE)


def token_endpoint(request: Request) -> Any:
    tokens = getattr(request.app.state, "voice_chatgpt_tokens", None)
    if tokens is None:
        tokens = vsi.HttpTokenEndpoint()
        request.app.state.voice_chatgpt_tokens = tokens
    return tokens


def _service(ctx: AppContext, writer: SecretWriter, tokens: Any) -> vsi.ChatGPTSignIn:
    return vsi.ChatGPTSignIn(ctx.db, writer, tokens, now=ctx.now)


@router.get("")
async def sign_in_status(
    person: str = Depends(allowed_person),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
    tokens: Any = Depends(token_endpoint),
) -> dict:
    return await run_in_threadpool(_service(ctx, writer, tokens).status, person)


@router.post("/authorize")
async def begin_sign_in(
    person: str = Depends(allowed_person),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
    tokens: Any = Depends(token_endpoint),
) -> dict:
    return await run_in_threadpool(_service(ctx, writer, tokens).begin, person)


@router.post(EXCHANGE_ROUTE.removeprefix(router.prefix))
async def finish_sign_in(
    body: FinishSignIn,
    person: str = Depends(allowed_person),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
    tokens: Any = Depends(token_endpoint),
) -> dict:
    service = _service(ctx, writer, tokens)
    return await run_in_threadpool(
        lambda: service.finish(person, state=body.state.strip(), pasted=body.callback))
