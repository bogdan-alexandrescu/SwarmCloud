"""Voice it's Settings: one document, names only (docs/design/voice-it-own-speech.md §11).

    voice_settings/platform
        {engine, machine, stt_model, tts_model, tts_voice, conversation_model,
         idle, zone, version, updated_by, updated_at,
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
`tts_voice` and `conversation_model` take effect at the next session or
sentence without touching the machine, so they are never pending.

THE OPENAI KEY'S STATUS (§10.3) is `{set, by, at}` and never the key: `set`
from the quota broker's Voice it pool (an `openai-api` account), `by` and `at`
from the `voice-api-key` one-time link that was redeemed last, whose owner is
the platform admin who pasted it (`setup_links`). This module reads neither
the key nor its secret; no answer of the broker's carries it.
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

#: The broker's provider for an OpenAI developer API key
#: (`quota_broker.openai_api_key.PROVIDER`).
KEY_PROVIDER = "openai-api"
#: The broker's `GET /v1/voice/pool` reasons that settle whether a key exists.
NO_KEY = vs.NO_LIVE_ACCOUNT
KEY_BUSY = "no_voice_account_available"


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


def view(data: Mapping[str, Any], zones: tuple[str, ...]) -> dict[str, Any]:
    chosen = effective(data, zones)
    applied = _applied(data)
    version = data.get("version")
    return {
        "settings": chosen,
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
            before = effective(data, zones)
            checked = check_changes(changes, before, zones)
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
    only for a Voice it person). `by` and `at` name the last redeemed link's
    owner and when it was opened, only while a key is set. `link` is the open
    link's expiry while one is unused and unexpired, so the page can show it."""
    rows = list(links)
    is_set: bool | None = None
    label: str | None = None
    if isinstance(pool_answer, Mapping):
        account = pool_answer.get("account")
        reason = pool_answer.get("reason")
        if isinstance(account, Mapping) and account.get("provider") == KEY_PROVIDER:
            is_set = True
            label = account.get("label") if isinstance(account.get("label"), str) else None
        elif reason == NO_KEY:
            is_set = False
        elif reason == KEY_BUSY:
            is_set = True
    by = at = None
    if is_set:
        used = [r for r in rows if isinstance(r.get("used_at"), datetime)]
        if label is not None and any(r.get("label") == label for r in used):
            used = [r for r in used if r.get("label") == label]
        last = _latest(used, "used_at")
        if last is not None:
            by = last.get("owner") if isinstance(last.get("owner"), str) else None
            at = _iso(last.get("used_at"))
    open_links = [r for r in rows if r.get("used_at") is None
                  and isinstance(r.get("expires_at"), datetime) and r["expires_at"] > now]
    newest = _latest(open_links, "expires_at")
    return {"set": is_set, "by": by, "at": at, "label": label,
            "link": {"open": True, "expires_at": _iso(newest["expires_at"])}
            if newest is not None else {"open": False, "expires_at": None}}


__all__ = [
    "AUDIT_CHANGED",
    "COLLECTION",
    "DOCUMENT",
    "KEY_PROVIDER",
    "MACHINE_FIELDS",
    "VoiceSettings",
    "check_changes",
    "effective",
    "key_links",
    "key_status",
    "pending",
    "view",
]
