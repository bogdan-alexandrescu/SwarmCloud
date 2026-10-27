"""A person's preferences: `user_preferences/up_<32 hex>`.

docs/design/user-profile.md §4.3 (Q1). One document per person, holding their
display name (§4.4), default workspace (§4.5), bell settings (§4.6) and time
zone, and nothing the scheduler, reconciler or worker reads. It holds no email
and no credential.

THE TIME ZONE (lane TIMEZONE-SERVER, part of #2391). BGD decided on 2026-10-26
that a person's zone is kept here, so it follows them to every browser and
survives sign-out; before, the console kept it in one browser's local storage.
It is an IANA name `zoneinfo` knows, or absent: absent reads as UTC, and is
told apart from a saved UTC (`time_zone is None`) so the console can write a
zone a browser kept before this field existed, once. A save that does not name
a zone keeps the stored one: the name, default and bell saves predate the
field, and a missing zone must never reset it.

THE ID IS AN HMAC, NOT `user_hash`. It is `up_` + the first 32 hex digits of
HMAC-SHA256(swarm-child-key, "user-prefs:" + lower(email)): the key that keys
`childkey.py`'s registration ids, under a message prefix no other derivation
uses. `notification_reads` can use a computable id because a planted read mark
only hides an item from a bell; a planted default workspace would misfile work
across tenants and a planted name would let an agent rename a person
(invariant 9). A document a worker creates under an id of its own invention is
one no person's request ever reads.

KEY ROTATION. The id is derived under every configured version of the key,
newest first; one `get_all` reads them all, the newest that exists is the
person's, and a save writes under the newest and deletes the older copy in
the same transaction. If every version a document was written under has been
rotated out, the person gets no name, the computed default and an unmuted bell
until they save again: today's behaviour, so a lost document fails safe.

AN UNREADABLE DOCUMENT IS AN ERROR, NEVER A DEFAULT. `read` raises
`PreferencesUnavailable` (503) when Firestore fails or no key is configured,
so authentication can refuse a headerless request (Q9) rather than guess. An
ABSENT document is not an error: it is the defaults, at revision 0.

ONLY THE VERIFIED CALLER WRITES THEIR OWN (Q8). `save` takes the caller's
email from the authenticated context; there is no route that names another
person. Whether `default_workspace` is one of the caller's `tenant_choices` is
the route's check (PROFILE-API), because only the request knows them.
"""

from __future__ import annotations

import logging
import threading
import time
import zoneinfo
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Iterable

from google.cloud import firestore

from swarm_common.admission import _snapshot
from swarm_common.models import utcnow

from . import display_names
from .display_names import DisplayNames, hmac_id
from .errors import (
    ApiError,
    PreferencesChanged,
    PreferencesUnavailable,
    UpstreamUnavailable,
    ValidationFailed,
)
from .forgeapp import user_hash

log = logging.getLogger(__name__)

COLLECTION = "user_preferences"
#: The id's message prefix (Q1). No other derivation's message begins with it.
PURPOSE = "user-prefs:"
ID_PREFIX = "up_"
#: The document's shape version.
SCHEMA_VERSION = 1

#: What a bell item may be about (§4.6). Every one may be muted (Q3).
TOPICS = ("approval", "waiting_step", "question", "failed", "finished", "account", "admin")
#: How often a read needs-you item rings again (§4.6). `as_set` is today's.
REMINDERS = ("as_set", "every_4h", "daily", "never")
DEFAULT_REMINDER = "as_set"

#: The zone a person who never saved one sees every time in.
DEFAULT_TIME_ZONE = "UTC"
#: `detail.reason` of a save naming a zone that is not one.
UNKNOWN_TIME_ZONE = "unknown_time_zone"

#: How long authentication serves a read before reading again, per instance
#: (§4.5): a save is seen by other instances within this.
CACHE_SECONDS = 15.0

#: What a failed save says. Nothing was written: the transaction is all or nothing.
SAVE_UNAVAILABLE = "Your preferences could not be saved, and nothing changed. Retry."


def preference_id(key: str, email: str) -> str:
    """`up_` + 32 hex of HMAC-SHA256(key, "user-prefs:" + lower(email))."""
    return ID_PREFIX + hmac_id(key, PURPOSE + email.strip().lower())


@lru_cache(maxsize=1)
def known_zones() -> frozenset[str]:
    """Every IANA zone name this interpreter's database holds (the system's
    on the bookworm image, else the `tzdata` package). Exact names only: a
    path, an abbreviation such as `PDT` or a differently cased name is not
    one, though `ZoneInfo` would open some of them."""
    return frozenset(zoneinfo.available_timezones())


def is_time_zone(value: Any) -> bool:
    return isinstance(value, str) and value in known_zones()


def check_time_zone(value: Any) -> str:
    """`value` when it is an IANA zone, else a 422 in words."""
    if not is_time_zone(value):
        raise ValidationFailed(
            "time_zone is not a time zone this server knows. Use an IANA name "
            "such as America/Los_Angeles, Europe/London or UTC.",
            detail={"reason": UNKNOWN_TIME_ZONE},
        )
    return value


def key_versions(settings: Any) -> tuple[str, ...]:
    """The configured `swarm-child-key` versions, newest first, as
    `childkey.ChildKeys` holds them. getattr: hand-built test settings
    predate the fields."""
    return tuple(
        v
        for v in (
            str(getattr(settings, "child_key", "") or ""),
            str(getattr(settings, "child_key_previous", "") or ""),
        )
        if v
    )


@dataclass(frozen=True)
class StoredPreferences:
    """One person's preferences as read. Absent is the defaults at revision 0."""

    display_name: str | None = None
    display_name_folded: str | None = None
    default_workspace: str | None = None
    muted: tuple[str, ...] = ()
    reminder: str = DEFAULT_REMINDER
    #: None when none is saved: the person sees DEFAULT_TIME_ZONE.
    time_zone: str | None = None
    revision: int = 0
    exists: bool = False

    @classmethod
    def from_doc(cls, doc: dict[str, Any] | None) -> "StoredPreferences":
        """Defensive: a field of the wrong type reads as its default, an
        unknown topic or reminder is dropped, so a bad document never breaks
        authentication."""
        if doc is None:
            return cls()
        name = doc.get("display_name")
        folded = doc.get("display_name_folded")
        workspace = doc.get("default_workspace")
        notifications = doc.get("notifications")
        notifications = notifications if isinstance(notifications, dict) else {}
        muted = notifications.get("muted")
        muted = muted if isinstance(muted, list) else []
        reminder = notifications.get("reminder")
        zone = doc.get("time_zone")
        revision = doc.get("revision")
        return cls(
            display_name=name if isinstance(name, str) and name else None,
            display_name_folded=folded if isinstance(folded, str) and folded else None,
            default_workspace=workspace if isinstance(workspace, str) and workspace else None,
            muted=tuple(t for t in TOPICS if t in muted),
            reminder=reminder if reminder in REMINDERS else DEFAULT_REMINDER,
            time_zone=zone if is_time_zone(zone) else None,
            revision=revision if isinstance(revision, int) and not isinstance(revision, bool)
            and revision >= 0 else 0,
            exists=True,
        )

    def as_api(self) -> dict[str, Any]:
        """The saved sections as `GET /v1/profile` and the save serve them."""
        return {
            "display_name": self.display_name,
            "default_workspace": self.default_workspace,
            "notifications": {"muted": list(self.muted), "reminder": self.reminder},
            "time_zone": self.time_zone,
            "revision": self.revision,
        }


def _validate_notifications(muted: Any, reminder: Any) -> tuple[list[str], str]:
    if not isinstance(muted, (list, tuple)) or not all(isinstance(t, str) for t in muted):
        raise ValidationFailed("muted is a list of topics.")
    unknown = [t for t in muted if t not in TOPICS]
    if unknown:
        raise ValidationFailed("muted names a topic that does not exist: " + ", ".join(TOPICS)
                               + " are the topics.")
    if len(set(muted)) != len(muted):
        raise ValidationFailed("muted names a topic twice.")
    if reminder not in REMINDERS:
        raise ValidationFailed("reminder is one of " + ", ".join(REMINDERS) + ".")
    return [t for t in TOPICS if t in muted], reminder


class Preferences:
    """`user_preferences/`: the id, the read across key versions, the cached
    read for authentication, and the transactional save."""

    def __init__(
        self,
        db: Any,
        keys: Iterable[str],
        *,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_SECONDS,
        names_ttl: float = display_names.NAME_CACHE_SECONDS,
    ) -> None:
        self._db = db
        self.keys = tuple(k for k in keys if k)
        self._now = now
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, StoredPreferences]] = {}
        self.names = DisplayNames(db, self, clock=clock, ttl=names_ttl)

    @property
    def configured(self) -> bool:
        return bool(self.keys)

    def ids(self, email: str) -> list[str]:
        """The person's id under each configured key version, newest first."""
        return [preference_id(key, email) for key in self.keys]

    def ref(self, doc_id: str) -> Any:
        return self._db.collection(COLLECTION).document(doc_id)

    # -- reads -----------------------------------------------------------------

    def read_many(self, emails: Iterable[str]) -> dict[str, StoredPreferences]:
        """Each email's preferences, from one `get_all` across every key
        version. Raises `PreferencesUnavailable` when they cannot be read."""
        wanted = list(dict.fromkeys(emails))
        if not self.configured:
            # No key, no id: nothing may be read under a computable one instead.
            raise PreferencesUnavailable()
        ids = {email: self.ids(email) for email in wanted}
        refs = [self.ref(i) for email in wanted for i in ids[email]]
        try:
            found = {
                snap.id: snap.to_dict()
                for snap in self._db.get_all(refs)
                if snap.exists
            }
        except Exception as exc:  # noqa: BLE001 - any Firestore failure is a 503
            raise PreferencesUnavailable() from exc
        out: dict[str, StoredPreferences] = {}
        for email in wanted:
            doc = next((found[i] for i in ids[email] if i in found), None)
            out[email] = StoredPreferences.from_doc(doc)
        return out

    def read(self, email: str) -> StoredPreferences:
        """The caller's preferences, uncached. `PreferencesUnavailable` when
        they cannot be read; the defaults when there are none."""
        return self.read_many([email])[email]

    def cached(self, email: str) -> StoredPreferences:
        """`read` through a 15-second per-instance cache keyed by email, for
        authentication (§4.5). A failure is never cached."""
        key = email.strip().lower()
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        stored = self.read(email)
        with self._lock:
            self._cache[key] = (now, stored)
        return stored

    def forget(self, email: str) -> None:
        """Drop the caller's cached preferences and name, after a save."""
        with self._lock:
            self._cache.pop(email.strip().lower(), None)
        self.names.forget(email)

    # -- the save --------------------------------------------------------------

    def save(
        self,
        email: str,
        *,
        display_name: str | None,
        default_workspace: str | None,
        muted: Any,
        reminder: Any,
        revision: Any,
        time_zone: Any = None,
    ) -> StoredPreferences:
        """Replace the caller's whole preference set, in one transaction.

        `revision` must equal the stored one (0 when there is none), or this is
        a 409 `preferences_changed` and nothing is stored. A display name
        passes `display_names.check` and rule 6; its claim, the deletion of its
        old claim and the `display_name_changed` audit entry are written in the
        same transaction as the document. `time_zone` None keeps the stored
        zone; any other value must be an IANA zone."""
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValidationFailed("revision is the number the last read returned.")
        if default_workspace is not None and (
            not isinstance(default_workspace, str) or not default_workspace
        ):
            raise ValidationFailed("default_workspace is a workspace id, or null.")
        muted_list, reminder_value = _validate_notifications(muted, reminder)
        zone = check_time_zone(time_zone) if time_zone is not None else None
        name = display_names.check(display_name) if display_name is not None else None
        new_folded = display_names.fold(name) if name is not None else None
        if not self.configured:
            raise UpstreamUnavailable(SAVE_UNAVAILABLE)
        caller_ids = self.ids(email)

        try:
            before = self.read(email)
            if new_folded is not None and new_folded != before.display_name_folded:
                self.names.ensure_free(new_folded, caller_ids)
            transaction = self._db.transaction()

            @firestore.transactional
            def _apply(txn: Any) -> tuple[StoredPreferences, StoredPreferences]:
                found: dict[str, dict[str, Any]] = {}
                for doc_id in caller_ids:
                    snap = _snapshot(txn.get(self.ref(doc_id)))
                    if snap.exists:
                        found[doc_id] = snap.to_dict() or {}
                current_id = next((i for i in caller_ids if i in found), None)
                current = StoredPreferences.from_doc(
                    found[current_id] if current_id is not None else None
                )
                if revision != current.revision:
                    raise PreferencesChanged(
                        "Your preferences were changed elsewhere since this page "
                        "read them. Reload and save again."
                    )
                claims = self.names.read_in(
                    txn,
                    caller_ids=caller_ids,
                    old_folded=current.display_name_folded,
                    new_folded=new_folded,
                )
                at = self._now()
                body = {
                    "v": SCHEMA_VERSION,
                    "display_name": name,
                    "display_name_folded": new_folded,
                    "default_workspace": default_workspace,
                    "notifications": {"muted": muted_list, "reminder": reminder_value},
                    "time_zone": zone if zone is not None else current.time_zone,
                    "revision": current.revision + 1,
                    "updated_at": at,
                }
                txn.set(self.ref(caller_ids[0]), body)
                for stale in found:
                    if stale != caller_ids[0]:
                        txn.delete(self.ref(stale))
                self.names.write_in(
                    txn,
                    claims,
                    owner=caller_ids[0],
                    new_folded=new_folded,
                    by=email,
                    old_name=current.display_name,
                    new_name=name,
                    at=at,
                )
                return current, StoredPreferences.from_doc(body)

            current, saved = _apply(transaction)
        except PreferencesUnavailable as exc:
            # The read before the save; its message is about a request's
            # workspace, and this is a save.
            raise UpstreamUnavailable(SAVE_UNAVAILABLE) from exc
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - any Firestore failure: nothing was written
            raise UpstreamUnavailable(SAVE_UNAVAILABLE) from exc
        finally:
            self.forget(email)

        changed = [
            label
            for label, was, is_now in (
                ("display_name", current.display_name, saved.display_name),
                ("default_workspace", current.default_workspace, saved.default_workspace),
                ("muted", current.muted, saved.muted),
                ("reminder", current.reminder, saved.reminder),
                ("time_zone", current.time_zone, saved.time_zone),
            )
            if was != is_now
        ]
        # Which fields, never their values (§4.2).
        log.info(
            "preferences saved user_hash=%s revision=%d changed=%s",
            user_hash(email),
            saved.revision,
            ",".join(changed) or "-",
        )
        return saved


def build_preferences(db: Any, settings: Any) -> Preferences:
    """From ApiSettings: the key versions `childkey.ChildKeys` holds."""
    return Preferences(db, key_versions(settings))
