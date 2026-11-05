"""Voice it › Settings (docs/design/voice-it-own-speech.md §11.3; voice/settings.py).

    GET   /v1/voice/settings            -> {settings, version, updated_by, updated_at,
                                            applied, pending, openai_key, zones}
    GET   /v1/voice/settings/catalogue  -> the catalogue: every name a setting may take
    PATCH /v1/voice/settings            {version, <field>: <name>, ...} -> the same as GET

WHO. Reading is for a Voice it person (VOICE_IT_PEOPLE, else the platform
owner) or a platform admin: the read-only view for one, the editing view for
the other. Writing is for platform admins only, `require_admin`'s full-admin
rule, the role every Admin route checks; a pool admin is not one. The writer
is the verified email, never a field of the body.

NAMES ONLY. The body is `version` plus the fields to change, each a name from
the catalogue. A field the catalogue does not define, a value that is not one
of its names, or a zone with no speech machine is 422 with `detail.field`
naming it; a stale `version` is 409 with the current one in `detail.version`.
Each write lands with its `voice_audit` row in one transaction.

THE KEY. `openai_key` is `{set, by, at, label, link}`: whether the Voice it
pool holds an OpenAI developer API key (the broker's `GET /v1/voice/pool`,
asked as this service for the caller, the same question Voice it ›
Preferences asks; for an admin who is not a Voice it person, for the first
one), who added it and when (the broker's record on the key's account,
or, for a key stored before the broker kept one, who opened the one-time
link that set it), and whether a link is open now. The key itself is never read here: the broker
answers no route with it but the Voice it lease. It is set only through the
existing `voice-api-key` link, minted by `POST /v1/admin/setup/links`.

This router is mounted beside the session routes in `main.create_app`; it
imports nothing from routes/voice.py, so mounting it there makes no import
cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext, require_admin
from ..brokerclient import AccountPool
from ..deps import AppContext, current_auth, get_context
from ..voice import sessions as vs
from ..voice import settings as vsettings
from ..voice import speech_catalogue as cat
from .accounts import account_pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/voice/settings", tags=["voice"])

PATCH_ROUTE = ("PATCH", "/v1/voice/settings")


def reader(auth: AuthContext = Depends(current_auth),
           ctx: AppContext = Depends(get_context)) -> str:
    """The caller's email when they may read Settings; 403 otherwise."""
    people = tuple(getattr(ctx.settings, "voice_people", ()) or ())
    if not (auth.is_admin or vs.is_allowed(people, auth.email)):
        raise vs.not_allowed()
    return auth.email.strip().lower()


def writer(auth: AuthContext = Depends(current_auth)) -> str:
    """The caller's email when they are a platform admin; 403 (or 503 when
    the directory could not say) otherwise."""
    require_admin(auth, PATCH_ROUTE)
    return auth.email.strip().lower()


def settings_service(request: Request, ctx: AppContext = Depends(get_context)) -> \
        vsettings.VoiceSettings:
    """`app.state.speech_zones` when a test sets it, else SWARM_SPEECH_ZONES."""
    zones = getattr(request.app.state, "speech_zones", None)
    return vsettings.VoiceSettings(ctx.db, now=ctx.now,
                                   zones=tuple(zones) if zones else cat.configured_zones())


def _pool_person(person: str, ctx: AppContext) -> str:
    """Whom the broker is asked about: the caller when Voice it is theirs,
    else the first Voice it person (an admin reading Settings is answered for
    the people the key serves; the broker answers only for them)."""
    people = tuple(p.strip().lower() for p in getattr(ctx.settings, "voice_people", ()) or ()
                   if p.strip())
    return person if person in people or not people else people[0]


def _key_status(pool: AccountPool, person: str, ctx: AppContext) -> dict[str, Any]:
    try:
        answer = pool.voice_pool(_pool_person(person, ctx))
    except Exception as exc:  # noqa: BLE001 -- the class name only; set reads unknown
        log.warning("the Voice it pool could not be read for the key's status (%s)",
                    type(exc).__name__)
        answer = None
    return vsettings.key_status(answer, vsettings.key_links(ctx.db), now=ctx.now())


def _answer(service: vsettings.VoiceSettings, shown: dict[str, Any], pool: AccountPool,
            person: str, ctx: AppContext) -> dict[str, Any]:
    return {**shown, "openai_key": _key_status(pool, person, ctx),
            "zones": list(service.zones)}


@router.get("")
async def read_settings(
    person: str = Depends(reader),
    service: vsettings.VoiceSettings = Depends(settings_service),
    pool: AccountPool = Depends(account_pool),
    ctx: AppContext = Depends(get_context),
) -> dict:
    def _read() -> dict:
        return _answer(service, service.read(), pool, person, ctx)

    return await run_in_threadpool(_read)


@router.get("/catalogue")
async def read_catalogue(
    person: str = Depends(reader),
    service: vsettings.VoiceSettings = Depends(settings_service),
) -> dict:
    return cat.view(service.zones)


@router.patch("")
async def change_settings(
    body: dict[str, Any] = Body(...),
    person: str = Depends(writer),
    service: vsettings.VoiceSettings = Depends(settings_service),
    pool: AccountPool = Depends(account_pool),
    ctx: AppContext = Depends(get_context),
) -> dict:
    changes = {k: v for k, v in body.items() if k != "version"}

    def _write() -> dict:
        shown = service.update(changes=changes, version=body.get("version"), by=person)
        log.info("audit: Voice it settings changed to version %s fields=%s",
                 shown["version"], ",".join(sorted(changes)))
        return _answer(service, shown, pool, person, ctx)

    return await run_in_threadpool(_write)


__all__ = ["PATCH_ROUTE", "reader", "router", "settings_service", "writer"]
