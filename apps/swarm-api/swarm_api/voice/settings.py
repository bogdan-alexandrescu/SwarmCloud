"""Voice it's Settings: one document, names only (docs/design/voice-it-own-speech.md §11).

    voice_settings/platform
        {engine, machine, stt_model, tts_model, tts_voice, conversation_model,
         planner_model, idle, zone, debug, version, updated_by, updated_at,
         applied: {machine, stt_model, tts_model, idle, zone, image, at}}

ONE DOCUMENT for the deployment: one speech machine serves it. A missing
document, or a missing field, reads as the catalogue's default
(`speech_catalogue.defaults`); a stored name the catalogue no longer lists
reads as the default too, so a removed entry can never reach the machine.

NAMES ONLY. Every write is checked against `speech_catalogue`: a field it does
not define, or a value that is not one of its names, is refused 422 with the
field named, so a machine type, image, command, URL or model path can never
arrive as free text (CONTRACT.md invariant 10's rule, applied here).

OPTIMISTIC WRITES. `version` counts writes; a write names the version it was
made against and is refused 409 (the current version in `detail`) when another
write landed first, inside the same Firestore transaction that makes the write
and its `voice_audit` row.

PENDING VERSUS APPLIED. `applied` is written by the speech machine's lifecycle
(`mark_applied`, called after a start has set the machine type and metadata and
the boot has the models, §11.4). `pending` is every machine field whose chosen
value differs from `applied`: what the next start changes. A change made during
a session is pending and never touches the running machine. `engine`,
`tts_voice`, `conversation_model` and `planner_model` take effect at the next
session or sentence without touching the machine, so they are never pending.

THE PLANNER MODEL is read once, at a session's start
(`VoiceSettings.session_planner_model`): the chosen class's pin, which the
start body hands swarm-voice and swarm-voice keeps for that session. A change
reaches the next session, never one already running. A document that never
set it gives None, and swarm-voice keeps VOICE_PLANNER_MODEL (voice.tf's
`local.runner_models.fable`), which is the same class as the default.

DEBUG MODE (the owner, 10 November 2026: "Trace on Telemetry"). `debug` is
the one setting that is not a catalogue name: a boolean, default False, so a
fresh deployment traces nothing. It is written by the same people, through
the same versioned PATCH and with the same `voice_audit` row as every other
field, and refused 422 unless it is true or false. Like the planner model it
is read once, at a session's start (`VoiceSettings.session_debug`), and the
start body carries `debug: true` only when it is on; swarm-voice then sends
each planner turn's trace to swarm-api (voice/trace.py). A change reaches the
next session, never one already running.

THE OPENAI KEY'S STATUS (§10.3) is `{set, by, at}` and never the key: `set`
from the quota broker's Voice it pool (an `openai-api` account), `by` and `at`
from the key account itself, where the broker records the platform admin who
added it and when it stored it (`key_added_by`, `key_added_at`;
`quota_broker.openai_api_key.added_fields`, the owner, 5 November 2026). An
account stored before the broker kept them falls back to the `voice-api-key`
one-time link that was redeemed last, whose owner is the platform admin who
pasted it (`setup_links`). This module reads neither the key nor its secret;
no answer of the broker's carries it.

THE DEDICATED KEY AND THE CHAIN LINE (docs/design/llm-providers.md §5.3,
§6.4) are read from the broker's platform-wide account listing
(`GET /v1/accounts`, asked as this service), never measured here.
`dedicated_key_status` is the `voice_dedicated` account's
`{set, label, display_name, by, at, state, link, replace}`: one per
deployment, in the Voice it pool alone, never lent; its state is the broker's
(`key_state`). `chain_status` is the three tiers a Voice it lease walks
(plans in the Voice it pool, keys shared with Voice it, the dedicated key),
each `available`, `no_quota_left` or `not_set_up` for the person, from the
broker's `can_take_new_agent`. Only those words leave this module: no account
id, owner workspace, key or secret name. A listing that could not be read is
None everywhere, never "not set". Deleting the key waits on the broker's
`DELETE /v1/accounts/{id}` for a key (LP-B1 stage (c)); nothing here offers it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from .. import setup_links as sl
from ..errors import Conflict, ValidationFailed
from . import sessions as vs
from . import speech_catalogue as cat

COLLECTION = "voice_settings"
DOCUMENT = "platform"
AUDIT = vs.AUDIT
AUDIT_CHANGED = "voice_settings_changed"

#: What a start applies to the stopped machine (§4.2 step 1, §11.4).
MACHINE_FIELDS: tuple[str, ...] = ("machine", "stt_model", "tts_model", "idle", "zone")

#: Debug mode's field: a boolean beside the catalogue's names, default off.
DEBUG = "debug"

#: The broker's provider for an OpenAI developer API key
#: (`quota_broker.openai_api_key.PROVIDER`).
KEY_PROVIDER = "openai-api"
#: The broker's `GET /v1/voice/pool` reasons that settle whether a key exists.
NO_KEY = vs.NO_LIVE_ACCOUNT
KEY_BUSY = "no_voice_account_available"

#: The broker's Voice it pool (`quota_broker.accounts.POOL_VOICE_IT`) and the
#: plan provider whose plans in it are the chain's first tier (§5.3).
POOL_VOICE_IT = "voice-it"
PLAN_PROVIDER = "openai"
#: The chain line's tiers, in the order a Voice it lease walks them (§5.3):
#: the broker's tier names (`quota_broker.accounts.TIERS`).
CHAIN_TIERS: tuple[str, ...] = ("plan", "_".join(("api", "key")), "voice_dedicated")
#: The dedicated key's states (`key_state`).
KEY_STATES: tuple[str, ...] = ("available", "limited", "paused", "draining", "credit_gone")


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def effective(data: Mapping[str, Any], zones: tuple[str, ...]) -> dict[str, str]:
    """Every field's chosen name: the stored one while the catalogue still
    lists it, else the default."""
    chosen = cat.defaults(zones)
    for field in cat.FIELDS:
        if field == "tts_voice":
            continue
        value = data.get(field)
        if isinstance(value, str) and value in cat.choices(field, zones):
            chosen[field] = value
    model = cat.tts(chosen["tts_model"])
    voice = data.get("tts_voice")
    if model is not None:
        chosen["tts_voice"] = voice if isinstance(voice, str) and voice in model.voices else (
            model.default_voice)
    return chosen


def _applied(data: Mapping[str, Any]) -> dict[str, Any] | None:
    applied = data.get("applied")
    if not isinstance(applied, Mapping):
        return None
    shown: dict[str, Any] = {f: applied.get(f) for f in MACHINE_FIELDS}
    shown["image"] = applied.get("image")
    shown["at"] = _iso(applied.get("at"))
    return shown


def pending(chosen: Mapping[str, str], applied: Mapping[str, Any] | None) -> list[str]:
    """The machine fields the next start changes, in `MACHINE_FIELDS` order.
    Nothing applied yet means every one of them is applied at the next start."""
    if applied is None:
        return list(MACHINE_FIELDS)
    return [f for f in MACHINE_FIELDS if applied.get(f) != chosen.get(f)]


def debug_on(data: Mapping[str, Any]) -> bool:
    """Debug mode as stored; anything but a stored True is off."""
    return data.get(DEBUG) is True


def view(data: Mapping[str, Any], zones: tuple[str, ...]) -> dict[str, Any]:
    chosen = effective(data, zones)
    applied = _applied(data)
    version = data.get("version")
    return {
        "settings": {**chosen, DEBUG: debug_on(data)},
        "version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
        "updated_by": data.get("updated_by") if isinstance(data.get("updated_by"), str) else None,
        "updated_at": _iso(data.get("updated_at")),
        "applied": applied,
        "pending": pending(chosen, applied),
    }


def _refuse(field: str, message: str, **detail: Any) -> ValidationFailed:
    return ValidationFailed(message, detail={"field": field, **detail})


def check_changes(changes: Mapping[str, Any], current: Mapping[str, str],
                  zones: tuple[str, ...]) -> dict[str, str]:
    """`changes` as names the catalogue lists, or 422 naming the field."""
    if not changes:
        raise _refuse("settings", "name at least one setting to change")
    for field in changes:
        if field not in cat.FIELDS:
            raise _refuse(str(field)[:64], f"{str(field)[:64]!r} is not a Voice it setting; "
                          f"the settings are {', '.join(cat.FIELDS)}")
    checked: dict[str, str] = {}
    for field in cat.FIELDS:
        if field not in changes or field == "tts_voice":
            continue
        value = changes[field]
        names = cat.choices(field, zones)
        if not isinstance(value, str) or value not in names:
            if field == "zone":
                raise _refuse(field, "that zone has no speech machine; pick one of the zones "
                              "the catalogue lists", choices=list(names))
            raise _refuse(field, f"{field} takes a name from the catalogue, never free text",
                          choices=list(names))
        checked[field] = value
    model = checked.get("tts_model", current["tts_model"])
    voices = cat.choices("tts_voice", zones, tts_model=model)
    if "tts_voice" in changes:
        voice = changes["tts_voice"]
        if not isinstance(voice, str) or voice not in voices:
            raise _refuse("tts_voice", f"tts_voice takes one of {model}'s voices",
                          choices=list(voices))
        checked["tts_voice"] = voice
    elif current["tts_voice"] not in voices:
        raise _refuse("tts_voice", f"{model} has no voice {current['tts_voice']!r}; "
                      "name one of its voices in the same change", choices=list(voices))
    return checked


class VoiceSettings:
    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 zones: tuple[str, ...] | None = None) -> None:
        self._db = db
        self._now = now
        self.zones = zones or cat.configured_zones()

    def _ref(self) -> Any:
        return self._db.collection(COLLECTION).document(DOCUMENT)

    def read(self) -> dict[str, Any]:
        snap = self._ref().get()
        return view((snap.to_dict() or {}) if snap.exists else {}, self.zones)

    def session_planner_model(self, class_models: Mapping[str, str]) -> str | None:
        """The model id a session starting now plans on, for its start body:
        the stored class's pin in `class_models` (swarm-api's
        `worker_class_models`). None when the setting is unset, names a class
        the catalogue no longer lists, or the class has no pin of its own
        class: swarm-voice then keeps VOICE_PLANNER_MODEL."""
        snap = self._ref().get()
        stored = (snap.to_dict() or {}).get("planner_model") if snap.exists else None
        if not isinstance(stored, str):
            return None
        return cat.planner_pin(stored, class_models)

    def session_debug(self) -> bool:
        """Whether a session starting now is traced: read once, at its start."""
        snap = self._ref().get()
        return debug_on((snap.to_dict() or {}) if snap.exists else {})

    def update(self, *, changes: Mapping[str, Any], version: Any, by: str) -> dict[str, Any]:
        """Write `changes` made against `version`, with its audit row, in one
        transaction; 422 for a name the catalogue lacks, 409 for a stale version."""
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise _refuse("version", "version is the settings' version this change was made "
                          "against, a whole number from GET /v1/voice/settings")
        ref = self._ref()
        zones = self.zones

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            data = dict(snap.to_dict() or {}) if snap.exists else {}
            current_version = view(data, zones)["version"]
            if version != current_version:
                raise Conflict("the settings changed since they were read; read them again",
                               detail={"field": "version", "version": current_version})
            before: dict[str, Any] = {**effective(data, zones), DEBUG: debug_on(data)}
            names = {f: v for f, v in changes.items() if f != DEBUG}
            checked: dict[str, Any] = check_changes(names, before, zones) \
                if names or DEBUG not in changes else {}
            if DEBUG in changes:
                if not isinstance(changes[DEBUG], bool):
                    raise _refuse(DEBUG, "debug is true or false")
                checked[DEBUG] = changes[DEBUG]
            now = self._now()
            written = {**data, **checked, "version": current_version + 1,
                       "updated_by": by, "updated_at": now}
            txn.set(ref, written)
            txn.set(self._db.collection(AUDIT).document(), {
                "action": AUDIT_CHANGED, "tenant_id": None, "by": by, "at": now,
                "project_id": None, "session_id": None,
                "detail": {"version": current_version + 1,
                           "changed": {f: {"from": before.get(f), "to": v}
                                       for f, v in checked.items() if before.get(f) != v}},
            })
            return written

        return view(_apply(self._db.transaction()), zones)

    def mark_applied(self, *, values: Mapping[str, str], image: str) -> dict[str, Any]:
        """Record what a start applied to the machine (§11.4). `values` are
        the machine fields' names as the start set them; `image` the speech
        server's digest the instance's metadata now names."""
        applied: dict[str, Any] = {}
        for field in MACHINE_FIELDS:
            value = values.get(field)
            if not isinstance(value, str) or value not in cat.choices(field, self.zones):
                raise _refuse(field, f"applied {field} is not a catalogue name")
            applied[field] = value
        applied["image"] = image
        applied["at"] = self._now()
        self._ref().set({"applied": applied}, merge=True)
        return self.read()


def _latest(rows: Iterable[Mapping[str, Any]], key: str) -> Mapping[str, Any] | None:
    dated = [r for r in rows if isinstance(r.get(key), datetime)]
    return max(dated, key=lambda r: r[key]) if dated else None


def key_links(db: Any) -> list[dict[str, Any]]:
    """Every `voice-api-key` link's record (a hash-keyed document carrying no
    token and no value)."""
    rows = (db.collection(sl.COLLECTION)
            .where(filter=FieldFilter("kind", "==", sl.VOICE_API_KEY))
            .stream())
    return [dict(snap.to_dict() or {}) for snap in rows]


def key_status(pool_answer: Mapping[str, Any] | None, links: Iterable[Mapping[str, Any]],
               *, now: datetime) -> dict[str, Any]:
    """`{set, by, at, label, link}` for Settings' key row, never the key.

    `set` is None when the broker could not say (unreachable, or it answers
    only for a Voice it person). `by` and `at` are the broker's record on the
    key account; for an account stored before the broker kept it, the last
    redeemed link's owner and when it was opened. Both only while a key is
    set. `link` is the open link's expiry while one is unused and unexpired,
    so the page can show it."""
    rows = list(links)
    is_set: bool | None = None
    label: str | None = None
    by = at = None
    if isinstance(pool_answer, Mapping):
        account = pool_answer.get("account")
        reason = pool_answer.get("reason")
        if isinstance(account, Mapping) and account.get("provider") == KEY_PROVIDER:
            is_set = True
            label = account.get("label") if isinstance(account.get("label"), str) else None
            added_by, added_at = account.get("key_added_by"), account.get("key_added_at")
            if isinstance(added_by, str) and added_by and isinstance(added_at, str) and added_at:
                by, at = added_by, added_at
        elif reason == NO_KEY:
            is_set = False
        elif reason == KEY_BUSY:
            is_set = True
    if is_set and by is None:
        used = [r for r in rows if isinstance(r.get("used_at"), datetime)]
        if label is not None and any(r.get("label") == label for r in used):
            used = [r for r in used if r.get("label") == label]
        last = _latest(used, "used_at")
        if last is not None:
            by = last.get("owner") if isinstance(last.get("owner"), str) else None
            at = _iso(last.get("used_at"))
    return {"set": is_set, "by": by, "at": at, "label": label, "link": _open_link(rows, now)}


def _open_link(links: Iterable[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """The newest unused, unexpired `voice-api-key` link's expiry, if any."""
    open_links = [r for r in links if r.get("used_at") is None
                  and isinstance(r.get("expires_at"), datetime) and r["expires_at"] > now]
    newest = _latest(open_links, "expires_at")
    return ({"open": True, "expires_at": _iso(newest["expires_at"])}
            if newest is not None else {"open": False, "expires_at": None})


def _rows(listing: Mapping[str, Any] | None) -> list[Mapping[str, Any]] | None:
    """The broker listing's account rows, or None when it could not be read."""
    if not isinstance(listing, Mapping) or not isinstance(listing.get("accounts"), list):
        return None
    return [a for a in listing["accounts"] if isinstance(a, Mapping)]


def _in_voice_pool(row: Mapping[str, Any]) -> bool:
    return POOL_VOICE_IT in (row.get("pools") or ())


def _is_dedicated(row: Mapping[str, Any]) -> bool:
    return (row.get("provider") == KEY_PROVIDER and row.get("voice_dedicated") is True
            and _in_voice_pool(row))


def _over_spend_cap(row: Mapping[str, Any]) -> bool:
    cap, spent = row.get("monthly_spend_cap_usd"), row.get("month_spend_usd")
    return (isinstance(cap, (int, float)) and isinstance(spent, (int, float))
            and spent >= cap)


def _can_serve(row: Mapping[str, Any]) -> bool:
    """The broker's verdict (`can_take_new_agent`), with the two §5.1 marks
    the listing's verdict does not fold in yet: credit gone (`exhausted_at`)
    and a month's spend at its cap, both the broker's own figures."""
    return (row.get("can_take_new_agent") is True and row.get("exhausted_at") is None
            and not _over_spend_cap(row))


def key_state(row: Mapping[str, Any]) -> str:
    """One of KEY_STATES for the broker's row of a key: an operator's pause or
    drain first, then credit gone, then a recorded 429 that has not reset."""
    state = str(row.get("state") or "").upper()
    if state == "DRAINING":
        return "draining"
    if state == "PAUSED":
        return "paused"
    if row.get("exhausted_at") is not None:
        return "credit_gone"
    if any(isinstance(b, Mapping) and b.get("limited_until") for b in row.get("blocked_by") or ()):
        return "limited"
    return "available" if state == "AVAILABLE" else state.lower()


def dedicated_key_status(listing: Mapping[str, Any] | None,
                         pool_answer: Mapping[str, Any] | None,
                         links: Iterable[Mapping[str, Any]], *, now: datetime,
                         admin: bool) -> dict[str, Any]:
    """`{set, label, display_name, by, at, state, link, replace}` for the
    dedicated Voice it key (the broker's `voice_dedicated` account, one per
    deployment, never lent), never the key or its secret's name.

    `set` (and with it everything the broker says) is None when the listing
    could not be read, never False. `by` and `at` are the broker's record when
    its pool answer names this very account, else the last redeemed
    `voice-api-key` link for its label. `replace` is the one-time link that
    replaces it, `{kind, label}`, for a platform admin while a key is set; a
    reader who is not one gets None, the read-only view."""
    rows = list(links)
    status: dict[str, Any] = {"set": None, "label": None, "display_name": None, "by": None,
                              "at": None, "state": None, "link": _open_link(rows, now),
                              "replace": None}
    accounts = _rows(listing)
    if accounts is None:
        return status
    dedicated = next((a for a in accounts if _is_dedicated(a)), None)
    if dedicated is None:
        return {**status, "set": False}
    label = dedicated.get("label") if isinstance(dedicated.get("label"), str) else None
    name = dedicated.get("display_name")
    by = at = None
    chosen = pool_answer.get("account") if isinstance(pool_answer, Mapping) else None
    if isinstance(chosen, Mapping) and chosen.get("account_id") == dedicated.get("account_id"):
        added_by, added_at = chosen.get("key_added_by"), chosen.get("key_added_at")
        if isinstance(added_by, str) and added_by and isinstance(added_at, str) and added_at:
            by, at = added_by, added_at
    if by is None and label is not None:
        last = _latest([r for r in rows if r.get("label") == label
                        and isinstance(r.get("used_at"), datetime)], "used_at")
        if last is not None:
            by = last.get("owner") if isinstance(last.get("owner"), str) else None
            at = _iso(last.get("used_at"))
    return {**status, "set": True, "label": label,
            "display_name": name if isinstance(name, str) else None, "by": by, "at": at,
            "state": key_state(dedicated),
            "replace": {"kind": sl.VOICE_API_KEY, "label": label}
            if admin and label is not None else None}


def chain_status(listing: Mapping[str, Any] | None, person: str) -> list[dict[str, Any]]:
    """The Voice it chain line of §6.4, "Plans → shared keys → this key":
    `[{tier, state}]` in CHAIN_TIERS' order, each tier's accounts those of the
    Voice it pool that list `person` (the broker's `may_serve_voice`).

    `state` is `available` when the broker says one of them can take a
    session now, `no_quota_left` when some are registered and none can (spent,
    limited, out of credit, full or paused: the broker's
    `no_voice_account_available`), `not_set_up` when none is, and None when
    the listing could not be read."""
    accounts = _rows(listing)
    if accounts is None:
        return [{"tier": tier, "state": None} for tier in CHAIN_TIERS]
    who = person.strip().lower()
    mine = [a for a in accounts if _in_voice_pool(a)
            and who in [str(p).strip().lower() for p in a.get("voice_it_people") or ()]]
    plans = [a for a in mine if a.get("provider") == PLAN_PROVIDER]
    keys = [a for a in mine if a.get("provider") == KEY_PROVIDER]
    tiers = (plans, [a for a in keys if a.get("voice_dedicated") is not True],
             [a for a in keys if a.get("voice_dedicated") is True])

    def _state(tier: list[Mapping[str, Any]]) -> str:
        if not tier:
            return "not_set_up"
        return "available" if any(_can_serve(a) for a in tier) else "no_quota_left"

    return [{"tier": name, "state": _state(tier)} for name, tier in zip(CHAIN_TIERS, tiers)]


__all__ = [
    "AUDIT_CHANGED",
    "CHAIN_TIERS",
    "COLLECTION",
    "DEBUG",
    "DOCUMENT",
    "KEY_PROVIDER",
    "KEY_STATES",
    "MACHINE_FIELDS",
    "VoiceSettings",
    "chain_status",
    "check_changes",
    "debug_on",
    "dedicated_key_status",
    "effective",
    "key_links",
    "key_state",
    "key_status",
    "pending",
    "view",
]
