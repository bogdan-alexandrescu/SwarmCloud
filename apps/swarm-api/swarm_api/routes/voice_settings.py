"""Voice it › Settings (docs/design/voice-it-own-speech.md §11.3; voice/settings.py).

    GET   /v1/voice/settings            -> {settings, version, updated_by, updated_at,
                                            applied, pending, openai_key, dedicated_key,
                                            chain, zones}
    GET   /v1/voice/settings/catalogue  -> the catalogue: every name a setting may take
    PATCH /v1/voice/settings            {version, <field>: <name>, ...} -> the same as GET
                                        (`debug` takes true or false, not a name)
    DELETE /v1/voice/settings/dedicated-key  {label} -> the same as GET

WHO. Reading is for a Voice it person (VOICE_IT_PEOPLE, else the platform
owner) or a platform admin: the read-only view for one, the editing view for
the other. Writing is for platform admins only, `require_admin`'s full-admin
rule, the role every Admin route checks; a pool admin is not one. The writer
is the verified email, never a field of the body.

NAMES ONLY. The body is `version` plus the fields to change, each a name from
the catalogue. A field the catalogue does not define, a value that is not one
of its names, or a zone with no speech machine is 422 with `detail.field`
naming it; a stale `version` is 409 with the current one in `detail.version`.
Each write lands with its `voice_audit` row in one transaction. Debug mode
(`debug`, voice/settings.py) is written by the same people the same way: a
platform admin, versioned and audited; anything but true or false is 422.

THE KEY. `openai_key` is `{set, by, at, label, link}`: whether the Voice it
pool holds an OpenAI developer API key (the broker's `GET /v1/voice/pool`,
asked as this service for the caller, the same question Voice it ›
Preferences asks; for an admin who is not a Voice it person, for the first
one), who added it and when (the broker's record on the key's account,
or, for a key stored before the broker kept one, who opened the one-time
link that set it), and whether a link is open now. The key itself is never read here: the broker
answers no route with it but the Voice it lease. It is set only through the
existing `voice-api-key` link, minted by `POST /v1/admin/setup/links`.

THE DEDICATED KEY AND THE CHAIN (docs/design/llm-providers.md §6.4).
`dedicated_key` is `{set, label, display_name, by, at, state, link, replace}`
for the broker's `voice_dedicated` account, and `chain` is `[{tier, state}]`
for plans, shared keys and that key, both from the broker's platform-wide
listing (`voice/settings.py`). `replace` (`{kind: "voice-api-key", label}`)
is for a platform admin; anyone else reads it as None, the read-only view.
A broker read that fails is logged by its class name and reads as None.

DELETING THE DEDICATED KEY (llm-providers.md §4.4) is a platform admin's, the
writer's rule; `dedicated_key.can_delete` says whether the reader may. The
body is the label typed back, which must be the dedicated key's (422,
`detail.field` "label", otherwise). The account deleted is the one the
broker's listing names as the dedicated key, never an id from the caller, so
the route reaches no other key; the broker's `DELETE /v1/accounts/{id}`
destroys its secret at once. No dedicated key is 404; a listing that could not
be read is 503 and deletes nothing. A refusal of the broker's is answered as
the broker said it. Each delete the broker was asked for writes one
`voice_audit` row (who, when, the label, the outcome) and an `audit:` log
line, as the settings writes are.

This router is mounted beside the session routes in `main.create_app`; it
imports nothing from routes/voice.py, so mounting it there makes no import
cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext, require_admin
from ..brokerclient import AccountPool
from ..deps import AppContext, current_auth, get_context
from ..errors import ApiError, NotFound, UpstreamUnavailable, ValidationFailed
from ..voice import sessions as vs
from ..voice import settings as vsettings
from ..voice import speech_catalogue as cat
from .accounts import account_pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/voice/settings", tags=["voice"])

PATCH_ROUTE = ("PATCH", "/v1/voice/settings")
DELETE_KEY_ROUTE = ("DELETE", "/v1/voice/settings/dedicated-key")


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


def key_deleter(auth: AuthContext = Depends(current_auth)) -> str:
    """The caller's email when they are a platform admin, as `writer`."""
    require_admin(auth, DELETE_KEY_ROUTE)
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


def _broker_read(read: Callable[[], dict[str, Any]], what: str) -> dict[str, Any] | None:
    """One broker read, or None (unknown, never "not set") when it fails."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 -- the class name only; the status reads unknown
        log.warning("the broker could not be read for %s (%s)", what, type(exc).__name__)
        return None


def _answer(service: vsettings.VoiceSettings, shown: dict[str, Any], pool: AccountPool,
            person: str, ctx: AppContext, *, admin: bool) -> dict[str, Any]:
    asked = _pool_person(person, ctx)
    answer = _broker_read(lambda: pool.voice_pool(asked), "the Voice it key's status")
    listing = _broker_read(lambda: pool.voice_it_accounts(), "the dedicated key and the chain")
    links = vsettings.key_links(ctx.db)
    now = ctx.now()
    return {**shown, "openai_key": vsettings.key_status(answer, links, now=now),
            "dedicated_key": vsettings.dedicated_key_status(listing, answer, links, now=now,
                                                            admin=admin),
            "chain": vsettings.chain_status(listing, asked),
            "zones": list(service.zones)}


@router.get("")
async def read_settings(
    person: str = Depends(reader),
    service: vsettings.VoiceSettings = Depends(settings_service),
    pool: AccountPool = Depends(account_pool),
    ctx: AppContext = Depends(get_context),
    auth: AuthContext = Depends(current_auth),
) -> dict:
    def _read() -> dict:
        return _answer(service, service.read(), pool, person, ctx, admin=auth.is_admin)

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
        return _answer(service, shown, pool, person, ctx, admin=True)

    return await run_in_threadpool(_write)


@router.delete("/dedicated-key")
async def delete_dedicated_key(
    body: dict[str, Any] = Body(...),
    person: str = Depends(key_deleter),
    service: vsettings.VoiceSettings = Depends(settings_service),
    pool: AccountPool = Depends(account_pool),
    ctx: AppContext = Depends(get_context),
) -> dict:
    typed = body.get("label")

    def _delete() -> dict:
        try:
            listing = pool.voice_it_accounts()
        except Exception as exc:  # noqa: BLE001 -- the class name only
            log.warning("the broker could not be read for the dedicated key's delete (%s)",
                        type(exc).__name__)
            raise UpstreamUnavailable("the account pool could not say which key is Voice it's "
                                      "dedicated key; nothing was deleted") from None
        dedicated = vsettings.dedicated_account(listing)
        if dedicated is None or not isinstance(dedicated.get("account_id"), str):
            raise NotFound("Voice it has no dedicated key to delete")
        label = dedicated.get("label")
        if not isinstance(typed, str) or not isinstance(label, str) or typed != label:
            raise ValidationFailed("type the dedicated key's label to delete it",
                                   detail={"field": "label"})
        try:
            pool.remove(str(dedicated["account_id"]))
        except ApiError as exc:
            vsettings.audit_dedicated_key_delete(ctx.db, by=person, label=label,
                                                 outcome=exc.code, now=ctx.now())
            log.info("audit: Voice it's dedicated key delete refused label=%s code=%s",
                     label, exc.code)
            raise
        vsettings.audit_dedicated_key_delete(ctx.db, by=person, label=label,
                                             outcome=vsettings.DELETED, now=ctx.now())
        log.info("audit: Voice it's dedicated key deleted label=%s", label)
        return _answer(service, service.read(), pool, person, ctx, admin=True)

    return await run_in_threadpool(_delete)


__all__ = ["DELETE_KEY_ROUTE", "PATCH_ROUTE", "key_deleter", "reader", "router",
           "settings_service", "writer"]
