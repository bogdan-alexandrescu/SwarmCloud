"""Voice it preferences: `voice_preferences/vp_<32 hex>` (docs/design/voice-it.md
§3.7, §3.11).

One document per person, the standing preferences Voice it › Preferences and
"remember I prefer Postgres" edit: the voice, the default mic mode, how brief
to be, preferred stacks, hosting and repositories, how many options to
propose, the grace window and the per-size reservations. It holds no email
and no credential.

THE ID IS AN HMAC, as swarm_api/preferences.py keys `user_preferences`:
`vp_` + the first 32 hex digits of HMAC-SHA256(swarm-child-key,
"voice-prefs:" + lower(email)), under a message prefix no other derivation
uses, so no worker can compute it and a document planted under an id of its
own invention is one no person's request ever reads. With no key configured
the store refuses (503) rather than fall back to an id anyone can compute.

UNDO. Every edit keeps the values it replaced as `previous`, with the
revision they had, in the same transaction that writes the new ones. Undo
swaps them back, so Undo twice is a redo, and a card's Undo after a later
edit restores the values just before that later edit: the person always
undoes the change they last saw. `revision` counts every write; an edit or
undo naming a `revision` that is not the stored one is 409, so a stale
console tab cannot undo a change it never showed.

A voice change takes effect at the next GPT-Live session (§3.11: the voice
is fixed per session); this module only stores it.
"""

from __future__ import annotations

from typing import Any, Callable

from google.cloud import firestore

from swarm_common.admission import _snapshot

from ..display_names import hmac_id
from ..errors import Conflict, UpstreamUnavailable, ValidationFailed

COLLECTION = "voice_preferences"
PURPOSE = "voice-prefs:"
ID_PREFIX = "vp_"

#: GPT-Live's voices (§3.11; OpenAI's realtime voice list).
VOICES = ("alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse")
MIC_MODES = ("push_to_talk", "open_mic")
BREVITY = ("terse", "normal", "detailed")
SIZES = ("fix", "small", "medium", "large", "project")

LIST_MAX_ITEMS = 20
ITEM_MAX_CHARS = 80
#: §4: the planner proposes between one and five options.
OPTIONS_MIN, OPTIONS_MAX = 1, 5
#: §3.8: the grace window, in seconds, before a started thing is spent.
GRACE_MIN, GRACE_MAX = 0, 600
#: §3.8: a per-size reservation, in US dollars.
RESERVATION_MAX_USD = 500

DEFAULTS: dict[str, Any] = {
    "voice": "marin",
    "mic_mode": "push_to_talk",
    "brevity": "normal",
    "stacks": [],
    "hosting": [],
    "repositories": [],
    "options": 3,
    "grace_seconds": 30,
    "reservations": {},
}
FIELDS = tuple(DEFAULTS)


class VoicePreferencesUnavailable(UpstreamUnavailable):
    code = "voice_preferences_unavailable"


class VoicePreferencesStale(Conflict):
    pass


def preference_id(key: str, email: str) -> str:
    """`vp_` + 32 hex of HMAC-SHA256(key, "voice-prefs:" + lower(email))."""
    return ID_PREFIX + hmac_id(key, PURPOSE + email.strip().lower())


def _refuse(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(f"{field} {message}", detail={"field": field})


def _one_of(field: str, value: Any, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise _refuse(field, f"is one of {', '.join(allowed)}")
    return value


def _int_in(field: str, value: Any, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise _refuse(field, f"is a whole number from {low} to {high}")
    return value


def _strings(field: str, value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > LIST_MAX_ITEMS:
        raise _refuse(field, f"is a list of at most {LIST_MAX_ITEMS} names")
    out: list[str] = []
    for item in value:
        text = item.strip() if isinstance(item, str) else ""
        if not text or len(text) > ITEM_MAX_CHARS or any(ord(c) < 32 for c in text):
            raise _refuse(field, f"holds names of 1 to {ITEM_MAX_CHARS} printable characters")
        if text not in out:
            out.append(text)
    return out


def _reservations(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise _refuse("reservations", "is an object of size to US dollars")
    out: dict[str, int] = {}
    for size, usd in value.items():
        _one_of("reservations", size, SIZES)
        out[size] = _int_in("reservations", usd, 0, RESERVATION_MAX_USD)
    return out


def validate(changes: Any) -> dict[str, Any]:
    """The edit, checked field by field: an unknown field or a bad value is a
    422 and nothing is written."""
    if not isinstance(changes, dict) or not changes:
        raise ValidationFailed("an edit names at least one preference", detail={"field": "changes"})
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ValidationFailed(f"unknown preference: {', '.join(unknown)}",
                               detail={"field": unknown[0]})
    checked: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "voice":
            checked[field] = _one_of(field, value, VOICES)
        elif field == "mic_mode":
            checked[field] = _one_of(field, value, MIC_MODES)
        elif field == "brevity":
            checked[field] = _one_of(field, value, BREVITY)
        elif field in ("stacks", "hosting", "repositories"):
            checked[field] = _strings(field, value)
        elif field == "options":
            checked[field] = _int_in(field, value, OPTIONS_MIN, OPTIONS_MAX)
        elif field == "grace_seconds":
            checked[field] = _int_in(field, value, GRACE_MIN, GRACE_MAX)
        else:
            checked[field] = _reservations(value)
    return checked


def _values(doc: dict[str, Any] | None) -> dict[str, Any]:
    """The stored values over the defaults; a field of the wrong shape reads
    as its default, so a bad document never breaks a session's start."""
    stored = (doc or {}).get("values") or {}
    out = dict(DEFAULTS)
    for field in FIELDS:
        if field in stored:
            try:
                out[field] = validate({field: stored[field]})[field]
            except ValidationFailed:
                pass
    return out


def view(doc: dict[str, Any] | None) -> dict[str, Any]:
    doc = doc or {}
    previous = doc.get("previous")
    return {
        "preferences": _values(doc),
        "revision": int(doc.get("revision") or 0),
        "can_undo": isinstance(previous, dict),
        "previous": _values({"values": previous}) if isinstance(previous, dict) else None,
    }


class VoicePreferences:
    """One person's Voice it preferences."""

    def __init__(self, db: Any, *, key: str, now: Callable[[], Any]) -> None:
        self._db = db
        self._key = key
        self._now = now

    def _ref(self, person: str) -> Any:
        if not self._key:
            raise VoicePreferencesUnavailable(
                "Voice it preferences are unavailable: this deployment has no settings key "
                "(SWARM_CHILD_KEY). Nothing was read or saved.")
        return self._db.collection(COLLECTION).document(preference_id(self._key, person))

    def read(self, person: str) -> dict[str, Any]:
        snap = self._ref(person).get()
        return view(snap.to_dict() if snap.exists else None)

    def edit(self, person: str, changes: Any, *, revision: int | None = None) -> dict[str, Any]:
        """Apply `changes`, keeping the values they replace for Undo."""
        checked = validate(changes)
        ref = self._ref(person)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            stored = int(doc.get("revision") or 0)
            if revision is not None and revision != stored:
                raise VoicePreferencesStale(
                    "Your preferences changed since this page read them; reload and try again.",
                    detail={"revision": stored})
            before = _values(doc)
            after = {**before, **checked}
            new = {"values": after, "previous": before, "previous_revision": stored,
                   "revision": stored + 1, "updated_at": self._now(),
                   "created_at": doc.get("created_at") or self._now()}
            txn.set(ref, new)
            return new

        return {**view(_apply(self._db.transaction())), "changed": sorted(checked)}

    def undo(self, person: str, *, revision: int | None = None) -> dict[str, Any]:
        """Swap back to the values the last edit replaced; 409 when there is
        nothing to undo or `revision` is stale."""
        ref = self._ref(person)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            stored = int(doc.get("revision") or 0)
            if revision is not None and revision != stored:
                raise VoicePreferencesStale(
                    "Your preferences changed since this page read them; reload and try again.",
                    detail={"revision": stored})
            previous = doc.get("previous")
            if not isinstance(previous, dict):
                raise VoicePreferencesStale("There is no preference change to undo.",
                                            detail={"revision": stored})
            new = {**doc, "values": _values({"values": previous}), "previous": _values(doc),
                   "previous_revision": stored, "revision": stored + 1,
                   "updated_at": self._now()}
            txn.set(ref, new)
            return new

        return view(_apply(self._db.transaction()))
