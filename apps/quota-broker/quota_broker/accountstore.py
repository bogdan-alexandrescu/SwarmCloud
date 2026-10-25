"""Where accounts live: Firestore for the facts, Secret Manager for the secret.

The split is not incidental. Firestore holds what everything needs to read
constantly -- who owns an account, how much headroom it has, whether it is
draining -- and Secret Manager holds the one thing almost nothing should read at
all. Putting the credential in Firestore would make every component that lists
accounts a component that can read them.

CONCURRENCY
-----------
`assigned` moves as agents start and finish, from more than one process. Those
are transactions, not read-modify-writes, because the count is what `choose()`
uses to spread load: a lost decrement makes an account look permanently busier
than it is and quietly pushes work onto the others until someone notices the
imbalance.

It is a projection of `holds` rather than a free-standing counter, and the
transactions in `quota_broker.main` are the only things that write either. See
`accounts.Hold`: a worker that is killed outright never releases, so a bare
counter could only ever drift upward.

The credential itself has exactly ONE writer -- the refresher -- for the reason
given at length in quota_broker.credentials: refresh tokens rotate, and two
writers invalidate each other's tokens.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .accounts import (
    ASSIGNABLE_STATES,
    LIMIT_RESET_SKEW,
    LIMIT_WINDOW_HORIZON,
    Account,
    AccountError,
    AccountState,
    WindowReading,
    _withheld,
    account_id_for,
    accounts_serving,
    secret_name,
    validate_display_name,
    validate_label,
    validate_max_agents,
    validate_model_classes,
    windows_to_firestore,
)

log = logging.getLogger(__name__)

COLLECTION = "accounts"


#: What `Account.from_firestore` raises on a document Firestore stored and the
#: decoder cannot read, and NOTHING ELSE. Named once so the listing and every
#: single-account read catch the same set.
#:
#:   KeyError        a required field is missing (`label`, `owner_tenant`)
#:   ValueError      a field holds a value its type refuses (`state: "bogus"`)
#:   TypeError       a field holds the wrong type (`assigned: null` reaches
#:                   `int(None)`)
#:   AttributeError  a map is not a map (`windows: "x"` has no `.items()`)
#:
#: NOT a bare `except Exception`, deliberately. Those four are what a bad
#: DOCUMENT produces; anything else -- a Firestore outage, a permission error
#: -- is not one account being unreadable, and skipping it would turn "the
#: store is down" into an empty pool that reports every document as malformed.
#: That failure propagates to the caller.
#:
#: A BUG IN THE DECODER is the case this set cannot tell apart: one that
#: raises for every document raises a TypeError or an AttributeError, exactly
#: what a bad document does. The set is kept at four regardless (the owner's
#: decision on #180), and the loudness comes from the other side: when EVERY
#: document of a non-empty collection lands here, `list_reporting` and the
#: hold sweep (`quota_broker.main._prune_all_holds`) log at ERROR, because a
#: whole pool going malformed at once is a decoder or a writer, not an account.
#:
#: Public because the broker's hold paths catch the same set and must not
#: drift from it.
MALFORMED_ERRORS = (KeyError, ValueError, TypeError, AttributeError)


class MalformedAccountError(AccountError):
    """An account document exists and cannot be decoded into an `Account`.

    One typed error for the four exceptions in `MALFORMED_ERRORS`, so that a caller
    handling one account can say "that document is broken" without knowing
    how the decoder happens to fail on it -- and so that the listing and the
    single-account reads cannot drift into catching different sets, which is
    how #180 happened.

    Carries the DOCUMENT ID and the exception CLASS, and never the exception's
    message: `str(exc)` on a ValueError quotes the value it refused, and an
    account document holds credential metadata (the secret's name, lending,
    the operator's reason). The id is enough to find the document; the class
    is enough to know what shape to look for.
    """

    def __init__(self, account_id: str, error: str) -> None:
        self.account_id = account_id
        self.error = error
        super().__init__(
            f"account document {account_id!r} is malformed ({error}); "
            "fix or remove it in Firestore"
        )


def _decode(account_id: str, data: dict[str, Any]) -> Account:
    """`Account.from_firestore`, with a malformed document made one typed error.

    Logs here rather than at each caller, so every path that meets a bad
    document says so once, loudly, in the same words.
    """
    try:
        return Account.from_firestore(data)
    except MALFORMED_ERRORS as exc:
        # See `MALFORMED_ERRORS` for why these four and not `Exception`.
        error = type(exc).__name__
        log.warning(
            "an account document is malformed and cannot be read",
            extra={"account_id": account_id, "error": error},
        )
        # `from None`: the chained original would put `str(exc)` -- which can
        # quote a field value -- back into any traceback that logs this.
        raise MalformedAccountError(account_id, error) from None


# ---------------------------------------------------------------------------
# Each account's health record (#2302)
# ---------------------------------------------------------------------------
#
# WHY IT EXISTS. An account's 429 used to be reported against its TENANT
# alone, so the only trace of "which account was refused, by which window, how
# often" was the tenant's halved adaptive target -- and the halving punished
# every other account for one account's window. Now the 429 is the account's
# (`quota_broker.main`, the report routes), and this record is where it, and
# everything else that goes wrong with one subscription, is kept for a person
# to read (`GET /v1/accounts/{id}/health`).
#
# WHERE: `accounts/{account_id}/health/record`, ONE document in a
# subcollection, not a field on the account document. The account document is
# rewritten by every hold transaction, and a health write racing a hold would
# make each retry the other for nothing. A subcollection is also invisible to
# `list_reporting`'s stream, so a health record can never be read as a
# malformed account.
#
# BOUNDED, by construction: per-hour counts for `HEALTH_RETENTION` (7 days,
# 168 buckets at most) and the last `HEALTH_EVENTS_MAX` events, oldest
# dropped, on every write. An event carries a kind, a time, the tenant and
# task that met it, the refusing window and its reset, and a SHORT message
# (`health_message`): never a page's or a prompt's content, never a token.

HEALTH_SUBCOLLECTION = "health"
HEALTH_DOCUMENT = "record"
#: How long per-hour counts are kept: the longest window the read serves.
HEALTH_RETENTION = timedelta(days=7)
#: Events kept per account; the 51st drops the oldest.
HEALTH_EVENTS_MAX = 50
#: Characters of an event's message kept. A message is a platform-written
#: sentence ("claude-code reported a provider rate limit"), never output.
HEALTH_MESSAGE_MAX = 200

#: The kinds an event may be, and nothing else (a report naming another kind is
#: refused at the route, not stored as "other").
HEALTH_RATE_LIMIT = "rate_limit"
HEALTH_EXHAUSTED = "exhausted"
HEALTH_AUTH = "auth"
HEALTH_OVERLOADED = "overloaded"
HEALTH_SERVER_ERROR = "server_error"
HEALTH_NETWORK = "network"
HEALTH_ASSIGN_REFUSED = "assign_refused"
HEALTH_REAUTH = "reauth"
HEALTH_REFRESH_FAILED = "refresh_failed"
HEALTH_KINDS = (
    HEALTH_RATE_LIMIT,
    HEALTH_EXHAUSTED,
    HEALTH_AUTH,
    HEALTH_OVERLOADED,
    HEALTH_SERVER_ERROR,
    HEALTH_NETWORK,
    HEALTH_ASSIGN_REFUSED,
    HEALTH_REAUTH,
    HEALTH_REFRESH_FAILED,
)
#: The provider errors a holding worker may report (`/account-error`): auth,
#: an overloaded 529, a 5xx, a network failure.
PROVIDER_ERROR_KINDS = (HEALTH_AUTH, HEALTH_OVERLOADED, HEALTH_SERVER_ERROR, HEALTH_NETWORK)
#: The kinds that make an account `erroring` (`health_status`): everything that
#: is not a limit and not a re-auth mark, which have statuses of their own.
HEALTH_ERROR_KINDS = (*PROVIDER_ERROR_KINDS, HEALTH_ASSIGN_REFUSED, HEALTH_REFRESH_FAILED)
#: Errors within the last hour that make an account `erroring`. Three, not one:
#: a single network blip is not a sick subscription.
HEALTH_ERRORING_MIN = 3

#: The windows a rate-limit event names. Anything else the provider calls a
#: window (a per-model weekly cap) is `other`.
HEALTH_WINDOWS = ("five_hour", "seven_day")
HEALTH_WINDOW_OTHER = "other"
#: How close a report's reset instant must be to a window's own `resets_at`
#: to say that window refused it. The provider reports both from one clock.
#: One value with the rule that says a reading shows that window reset
#: (`accounts.LIMIT_RESET_SKEW`), so the two cannot disagree on "same window".
_WINDOW_MATCH_SKEW = LIMIT_RESET_SKEW
#: How long an account reads as limited after a 429 that named no reset and no
#: Retry-After: the AIMD cooldown ceiling (`AimdConfig.max_cooldown_seconds`).
HEALTH_DEFAULT_LIMIT = timedelta(seconds=900)

#: Refresh outcomes (`accounts.REFRESH_REASONS`) that are a FAILURE of the
#: credential's refresh, recorded as `refresh_failed`. Not `still_valid`,
#: `held_deferred` or `no_refresh_credential`: nothing failed in those.
REFRESH_FAILURES = (
    "refresh_failed",
    "reauth_required",
    "publish_failed",
    "unreadable",
    "store_unavailable",
    "error",
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def health_message(raw: Any) -> str:
    """An event's message: redacted, one line, at most `HEALTH_MESSAGE_MAX`
    characters.

    REDACTED HERE, ON WRITE, AND AGAIN ON READ (`_event_to_api`; BGD,
    2026-10-25, "Yes, add it"). A worker's report is redacted before it is
    sent (`agent_worker.control`) and swarm-api redacts on the way out
    (`routes/accounts.py`), but a record that relied on every sender being
    current stored whatever an older worker, or a caller holding a platform
    token, sent -- and a document that holds a credential is one every reader
    of the collection holds. So `swarm_redaction.redact`, the patterns the
    worker and swarm-api use, runs on the WHOLE text first: capping first
    could cut a token below the length a pattern recognises and store its
    prefix. Then control characters out, whitespace collapsed, capped.

    IMPORTED HERE, NOT AT THE TOP. `kubernetes/render.py` (the workspace-apply
    image) imports `scheduler.dispatch`, which reaches this module, from a
    bare source tree with no swarm-redaction in it; a module-level import
    would fail that render. Every image that writes or serves a record -- the
    broker's, the scheduler's -- installs swarm-redaction, and `uv pip check`
    refuses an image that lacks it, so this import cannot first fail in
    production. If it ever did, it raises: a message is never stored
    unredacted.
    """
    from swarm_redaction import redact

    text = redact(str(raw or "")).text
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return text[:HEALTH_MESSAGE_MAX]


def _hour_key(moment: datetime) -> str:
    """`2026-10-25T04`: the UTC hour a moment falls in, as a map key."""
    return _aware(moment).astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _hour_start(key: str) -> datetime | None:
    try:
        return datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _instant(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return _aware(raw)
    if isinstance(raw, str) and raw:
        try:
            return _aware(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return None


def refusing_window(
    account: Account | None, now: datetime, *, reset_at: datetime | None
) -> str:
    """Which window refused a 429: `five_hour`, `seven_day` or `other`.

    The worker's report says WHEN the window reopens, not which one it was;
    the account's own readings say both. So: the window whose `resets_at` is
    the report's reset instant (within `_WINDOW_MATCH_SKEW`); failing that, the
    window that blocks the account now with the least left; failing that,
    `other` -- a 429 the readings do not explain.
    """
    if account is None or not account.windows:
        return HEALTH_WINDOW_OTHER
    name: str | None = None
    if reset_at is not None:
        for window, reading in account.windows.items():
            if abs(_aware(reading.resets_at) - _aware(reset_at)) <= _WINDOW_MATCH_SKEW:
                name = window
                break
    if name is None:
        blocking = account.blocking_windows(now)
        if blocking:
            name = min(blocking, key=lambda w: blocking[w].remaining())
    return name if name in HEALTH_WINDOWS else HEALTH_WINDOW_OTHER


def limited_until_for(
    now: datetime,
    *,
    reset_at: datetime | None,
    retry_after_seconds: int | None,
    account: Account | None = None,
    window: str = HEALTH_WINDOW_OTHER,
) -> datetime:
    """Until when one 429 keeps the account limited, at most `HEALTH_RETENTION`
    -- and at most the named window's own length (`LIMIT_WINDOW_HORIZON`).

    The provider's own reset instant first, then its Retry-After, then the
    refusing window's `resets_at`, then `HEALTH_DEFAULT_LIMIT`.

    BOUNDED TWICE because `choose()` honours the result (`Account.is_limited`):
    a bad report naming a reset a month out would otherwise take the account
    out of the pool for the week the record keeps, and a five-hour window
    cannot refuse for longer than five hours.
    """
    until: datetime | None = None
    if reset_at is not None and _aware(reset_at) > now:
        until = _aware(reset_at)
    elif retry_after_seconds is not None:
        until = now + timedelta(seconds=max(0, int(retry_after_seconds)))
    elif account is not None and window in account.windows:
        resets = _aware(account.windows[window].resets_at)
        until = resets if resets > now else None
    if until is None:
        until = now + HEALTH_DEFAULT_LIMIT
    return min(until, now + LIMIT_WINDOW_HORIZON.get(window, HEALTH_RETENTION))


def health_event(
    kind: str,
    now: datetime,
    *,
    tenant_id: str | None = None,
    task_id: str | None = None,
    window: str | None = None,
    reset_at: datetime | None = None,
    message: str = "",
) -> dict[str, Any]:
    """One event, as stored. Refuses a kind outside `HEALTH_KINDS`."""
    if kind not in HEALTH_KINDS:
        raise ValueError(f"unknown health event kind {kind!r}")
    return {
        "at": now,
        "kind": kind,
        "tenant_id": tenant_id or None,
        "task_id": task_id or None,
        "window": window or None,
        "reset_at": _aware(reset_at) if reset_at is not None else None,
        "message": health_message(message),
    }


def fold_health(
    raw: dict[str, Any] | None,
    event: dict[str, Any],
    now: datetime,
    *,
    limited_until: datetime | None = None,
) -> dict[str, Any]:
    """`raw` with `event` counted and appended, pruned to its bounds. Pure.

    Hourly buckets older than `HEALTH_RETENTION` are dropped and the event list
    is cut to its newest `HEALTH_EVENTS_MAX`. `limited_until` only ever moves
    LATER: a short 429 must not shorten a weekly window another one recorded.
    """
    data = dict(raw or {})
    hourly_raw = data.get("hourly")
    hourly: dict[str, dict[str, int]] = {}
    oldest = now - HEALTH_RETENTION
    if isinstance(hourly_raw, dict):
        for key, counts in hourly_raw.items():
            start = _hour_start(key)
            if start is None or start + timedelta(hours=1) <= oldest or not isinstance(counts, dict):
                continue
            hourly[key] = {
                k: int(v) for k, v in counts.items() if k in HEALTH_KINDS and isinstance(v, int)
            }
    bucket = hourly.setdefault(_hour_key(event["at"]), {})
    bucket[event["kind"]] = bucket.get(event["kind"], 0) + 1
    events_raw = data.get("events")
    events = [e for e in events_raw if isinstance(e, dict)] if isinstance(events_raw, list) else []
    events.append(event)
    events = events[-HEALTH_EVENTS_MAX:]
    stored_until = _instant(data.get("limited_until"))
    if limited_until is not None and (stored_until is None or limited_until > stored_until):
        stored_until = limited_until
    return {
        "hourly": hourly,
        "events": events,
        "limited_until": stored_until,
        "updated_at": now,
    }


def _counts_since(
    raw: dict[str, Any], since: datetime
) -> dict[str, int]:
    """Events per kind at or after `since`.

    EXACT while the stored events reach back that far (fewer than the cap, or
    the oldest is older than `since`); otherwise from the hourly buckets, whole
    UTC hours from the one `since` falls in -- which may count up to one hour
    more than asked, and never fewer.
    """
    events = [e for e in raw.get("events") or [] if isinstance(e, dict)]
    stamps = [_instant(e.get("at")) for e in events]
    covered = len(events) < HEALTH_EVENTS_MAX or (
        bool(stamps) and stamps[0] is not None and stamps[0] <= since
    )
    out = {kind: 0 for kind in HEALTH_KINDS}
    if covered:
        for event, at in zip(events, stamps):
            if at is not None and at >= since and event.get("kind") in out:
                out[event["kind"]] += 1
        return out
    floor = since.replace(minute=0, second=0, microsecond=0)
    for key, counts in (raw.get("hourly") or {}).items():
        start = _hour_start(key)
        if start is None or start < floor or not isinstance(counts, dict):
            continue
        for kind, n in counts.items():
            if kind in out and isinstance(n, int):
                out[kind] += n
    return out


def account_limited(account: Account, raw: dict[str, Any] | None, now: datetime) -> bool:
    """Whether this account is LIMITED now: a recorded 429 has not run out, or
    its window readings keep it from a new agent (`Account.assignable`, the
    S15 rule `choose()` applies)."""
    until = _instant((raw or {}).get("limited_until"))
    if until is not None and until > now:
        return True
    return not account.assignable(now)


def health_status(account: Account | None, raw: dict[str, Any] | None, now: datetime) -> str:
    """One word for an account's health. THE RULE, in order -- the first that
    holds wins:

      1. `reauth_needed` -- the account is REAUTH_REQUIRED: its credential is
         dead and only a person signing in again brings it back. First,
         because nothing else about the account matters until then.
      2. `limited` -- a 429 recorded against it has not reached its reset
         (`limited_until`), or its window readings block a new agent
         (`account_limited`). Waiting is the cure, not a person.
      3. `erroring` -- `HEALTH_ERRORING_MIN` or more errors (`HEALTH_ERROR_KINDS`:
         auth, overloaded, 5xx, network, refused at assignment, refresh
         failed) in the last hour.
      4. `healthy` -- none of the above.

    `account` None (a record whose account could not be read) skips 1 and the
    readings half of 2.
    """
    data = raw or {}
    if account is not None and account.state is AccountState.REAUTH_REQUIRED:
        return "reauth_needed"
    if account is not None and account_limited(account, data, now):
        return "limited"
    until = _instant(data.get("limited_until"))
    if account is None and until is not None and until > now:
        return "limited"
    recent = _counts_since(data, now - timedelta(hours=1))
    if sum(recent[k] for k in HEALTH_ERROR_KINDS) >= HEALTH_ERRORING_MIN:
        return "erroring"
    return "healthy"


def _event_to_api(event: dict[str, Any]) -> dict[str, Any]:
    at = _instant(event.get("at"))
    reset = _instant(event.get("reset_at"))
    return {
        "at": at.isoformat() if at else None,
        "kind": event.get("kind") if event.get("kind") in HEALTH_KINDS else None,
        "tenant_id": event.get("tenant_id") if isinstance(event.get("tenant_id"), str) else None,
        "task_id": event.get("task_id") if isinstance(event.get("task_id"), str) else None,
        "window": event.get("window") if isinstance(event.get("window"), str) else None,
        "reset_at": reset.isoformat() if reset else None,
        "message": health_message(event.get("message")),
    }


def health_counts(raw: dict[str, Any] | None, now: datetime) -> dict[str, dict[str, int]]:
    """Counts by kind for the last 1 h, 24 h and 7 d (`_counts_since`)."""
    data = raw or {}
    return {
        "last_1h": _counts_since(data, now - timedelta(hours=1)),
        "last_24h": _counts_since(data, now - timedelta(hours=24)),
        "last_7d": _counts_since(data, now - HEALTH_RETENTION),
    }


def health_to_api(
    account_id: str, account: Account | None, raw: dict[str, Any] | None, now: datetime
) -> dict[str, Any]:
    """The whole record, as `GET /v1/accounts/{id}/health` serves it.

    An hourly series of 168 entries, oldest first and dense (an hour with
    nothing in it is there with zeros, so a chart needs no gap logic), the
    counts, the last events newest first, and the status. By an allow-list of
    fields: whatever else a document holds is not served.
    """
    data = raw or {}
    hourly = data.get("hourly") if isinstance(data.get("hourly"), dict) else {}
    top = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    series = []
    for back in range(int(HEALTH_RETENTION / timedelta(hours=1)) - 1, -1, -1):
        start = top - timedelta(hours=back)
        counts = hourly.get(_hour_key(start))
        kinds = {
            k: int(v)
            for k, v in (counts.items() if isinstance(counts, dict) else ())
            if k in HEALTH_KINDS and isinstance(v, int) and v
        }
        series.append({"hour": start.isoformat(), "counts": kinds, "total": sum(kinds.values())})
    events = [e for e in data.get("events") or [] if isinstance(e, dict)]
    until = _instant(data.get("limited_until"))
    return {
        "account_id": account_id,
        "status": health_status(account, data, now),
        "limited_until": until.isoformat() if until and until > now else None,
        "counts": health_counts(data, now),
        "hourly": series,
        "events": [_event_to_api(e) for e in reversed(events)],
        "observed_at": now.isoformat(),
    }


@dataclass(frozen=True)
class AccountListing:
    """Every account that could be read, AND the documents that could not.

    THE SECOND FIELD IS THE POINT. `list()` skips a malformed document and logs
    it, which is right -- one bad document must not hide the fleet -- but the
    skip left the caller holding a shorter list with nothing to say it was
    short. A pool of five then renders as four accounts, "4 accounts
    registered", and a headroom figure taken over four, and every one of those
    figures is wrong in a way no reader can see. Returning the count alongside
    the rows is what makes the shortfall sayable.

    `unreadable` holds Firestore DOCUMENT IDS, which is what an operator needs
    to go and look at the offending document -- a bare count says something is
    wrong without saying where.
    """

    accounts: list[Account] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


class AccountStore:
    def __init__(self, db: Any, *, now: Any = None) -> None:
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- reading ----------------------------------------------------------

    def list_reporting(self) -> AccountListing:
        """Every account, AND every document that could not be turned into one.

        Deliberately unfiltered: the refresher needs ALL of them, including
        paused, draining and idle ones, because an account nobody is using is
        an account whose refresh token is quietly expiring. A filter here would
        be the kind of optimisation that breaks the property the pool exists
        for.

        Skipping a malformed document is right -- one of them must not hide the
        rest of the fleet -- but a skip that only reaches a log line is this
        platform's defining bug happening inside the store: the caller gets a
        shorter list and no way to know it is short, and every figure derived
        from it (the row count, "N accounts registered", the pool's headroom)
        is then confidently wrong. So the ids come back with the rows.

        EVERY DOCUMENT UNREADABLE is logged at ERROR, once, with the ids. One
        bad document is a warning about that document; all of them at once is
        what a decoder bug or a bad writer looks like (see `MALFORMED_ERRORS`),
        and `list()` would otherwise hand its callers an empty pool with
        nothing above WARNING to say why.
        """
        out: list[Account] = []
        unreadable: list[str] = []
        for doc in self._db.collection(COLLECTION).stream():
            try:
                out.append(_decode(str(doc.id), doc.to_dict() or {}))
            except MalformedAccountError:
                # One malformed document must not hide the rest of the fleet --
                # and must not hide itself either. See `AccountListing`.
                # `_decode` has logged it; only the four exceptions a bad
                # document produces arrive here (see `MALFORMED_ERRORS`).
                unreadable.append(str(doc.id))
        if unreadable and not out:
            log.error(
                "every account document is unreadable; the pool reads as empty. "
                "One malformed document is that document; all of them is a "
                "decoder or writer bug",
                extra={"unreadable": sorted(unreadable), "documents": len(unreadable)},
            )
        return AccountListing(accounts=out, unreadable=sorted(unreadable))

    def list(self) -> list[Account]:
        """The readable accounts alone.

        Kept because the refresher, the poller and the hold sweep all want
        exactly this and have nothing to say about a document they cannot
        parse. Anything that RENDERS the pool calls `list_reporting` instead,
        because for a reader the shortfall is part of the answer.
        """
        return self.list_reporting().accounts

    def get(self, account_id: str) -> Account | None:
        snap = self._db.collection(COLLECTION).document(account_id).get()
        if not getattr(snap, "exists", False):
            return None
        return _decode(account_id, snap.to_dict() or {})

    def for_tenant(self, tenant_id: str) -> list[Account]:
        """Accounts this tenant may use: its own, plus anything lent to it."""
        return [a for a in self.list() if a.may_serve(tenant_id)]

    # -- writing ----------------------------------------------------------

    def register(
        self,
        owner_tenant: str,
        label: str,
        *,
        provider: str = "anthropic",
        lend_to: Iterable[str] = (),
    ) -> Account:
        """Record an account. Does NOT write the credential.

        The credential goes to Secret Manager out of band, by the same rule that
        keeps provider keys out of terraform state: the value must not pass
        through a component whose job is bookkeeping.
        """
        validate_label(label)
        if not owner_tenant:
            raise AccountError("an account must have an owning tenant")

        account_id = account_id_for(owner_tenant, label)
        lend = tuple(t for t in lend_to if t and t != owner_tenant)

        ref = self._db.collection(COLLECTION).document(account_id)
        existing = ref.get()
        if getattr(existing, "exists", False):
            # Re-registering is the normal path for changing who an account is
            # lent to. The readings and state are preserved: re-registering must
            # not silently un-pause an account an operator paused.
            # Raises MalformedAccountError BEFORE the `set` below, which is the
            # point: a `set` built from a document nobody could read would
            # replace its holds and state with defaults.
            current = _decode(account_id, existing.to_dict() or {})
            updated = Account(
                account_id=account_id,
                owner_tenant=owner_tenant,
                label=label,
                provider=provider,
                state=current.state,
                lend_to=lend,
                windows=current.windows,
                observed_at=current.observed_at,
                # CARRIED OVER, because this is a `set` and not an update: a
                # re-registration is how lending is changed, it happens while
                # agents are running, and dropping the holds here would reset
                # every live count to zero and stack the next several agents
                # onto an account that already has four.
                holds=current.holds,
                unreadable_by=dict(current.unreadable_by),
                last_assigned_at=current.last_assigned_at,
                assigned=current.assigned,
                reason=current.reason,
                # CARRIED OVER with the state, for the same reason the state
                # is: this is a `set`, and changing who an account is lent to
                # must not throw away the record of where a recovered account
                # belongs. Dropping it here would silently turn a paused
                # account's recovery into AVAILABLE.
                state_before_reauth=current.state_before_reauth,
                # The refresh sweep's record of this credential, carried for
                # the reason the holds are: this is a `set`, and changing who
                # an account is lent to must not blank the token's expiry the
                # Accounts screen shows.
                last_refresh_at=current.last_refresh_at,
                token_expires_at=current.token_expires_at,
                last_refresh_reason=current.last_refresh_reason,
                # CARRIED OVER, for the reason the state is: changing who an
                # account is lent to must not widen an opus-only account back
                # to every class and hand it the next Fable step (R5).
                model_classes=current.model_classes,
                # CARRIED OVER, for the reason the holds are: this is a `set`,
                # and changing who an account is lent to must not forget the
                # Name its owner gave it (#1913).
                display_name=current.display_name,
                # CARRIED OVER, for the reason the classes are: re-registering
                # must not lift a cap its owner lowered (#2222).
                max_agents=current.max_agents,
                # CARRIED OVER, for the reason the holds are (#2222): changing
                # who an account is lent to must not forget which organisation
                # it runs on, or a duplicate would go unseen until the next
                # refresh. A new sign-in records its own right after this.
                organisation_id=current.organisation_id,
                organisation_name=current.organisation_name,
                organisation_plan=current.organisation_plan,
                seat_id=current.seat_id,
                registered_at=current.registered_at,
                duplicate_of=current.duplicate_of,
                duplicate_withheld=current.duplicate_withheld,
                # The identical-readings count too: a re-registration is not
                # a new reading, so it neither restarts nor clears it.
                possible_duplicate_of=current.possible_duplicate_of,
                identical_readings=current.identical_readings,
                # PRESERVED, never re-derived. An account registered before the
                # naming rule changed keeps pointing at the secret that holds
                # its credential; re-deriving here would orphan it on the next
                # re-registration.
                secret_ref=current.secret or secret_name(owner_tenant, label),
            )
            ref.set(updated.to_firestore())
            return updated

        account = Account(
            account_id=account_id,
            owner_tenant=owner_tenant,
            label=label,
            provider=provider,
            lend_to=lend,
            secret_ref=secret_name(owner_tenant, label),
            # Which of two registrations of one organisation is the older
            # (`accounts.duplicates`, #2222).
            registered_at=self._now(),
        )
        ref.set(account.to_firestore())
        return account

    def set_state(self, account_id: str, state: AccountState, reason: str = "") -> None:
        """An operator's state change. Supersedes anything the sweep remembered.

        `state_before_reauth` is cleared here because this call is a PERSON
        saying where the account belongs. Leaving an older remembered state
        behind would let a later recovery restore a decision that had since
        been overruled -- the account would come back not where the last person
        to touch it put it, but where it was two episodes ago.
        """
        payload: dict[str, Any] = {
            "state": state.value, "reason": reason, "state_before_reauth": "",
        }
        if state is AccountState.AVAILABLE:
            # AN OPERATOR'S RESUME ENDS A RECORDED 429's HOLD
            # (`Account.is_limited`), on the account and its health record.
            # The hold is bounded (`limited_until_for`), but a bad report can
            # still hold an account for days, and a person putting the account
            # back in service is the one who can say it is wrong. A wrong
            # resume costs one more 429, which holds it again.
            payload["limited_until"] = None
            payload["limited_window"] = None
            self._health_ref(account_id).set({"limited_until": None}, merge=True)
        self._db.collection(COLLECTION).document(account_id).update(payload)

    def mark_reauth_required(self, account_id: str, reason: str) -> Account | None:
        """Record that this account's credential is dead. Returns it, or None.

        THE WRITER THE SWEEP NEVER HAD. The refresher detected a revoked
        refresh token, logged it and counted it, and the document stayed at
        AVAILABLE with an empty reason -- so `choose()` kept handing the
        account to new agents that could not authenticate, `due_for_refresh()`
        kept retrying a token that cannot come back, and a listing showed
        nothing at all pointing at the account that was actually broken.

        Separate from `set_state` on purpose. This one REMEMBERS the state it
        is replacing, because it is a background sweep overwriting a state a
        person may have chosen; `set_state` is that person, and forgets.

        An account already marked is left exactly as it is: `due_for_refresh`
        excludes it, so this only ever fires on the transition, and rewriting
        it would replace the remembered state with REAUTH_REQUIRED itself and
        lose the way back.
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            # A refresh outcome for an account that has since been removed.
            # Not an error: `remove` is allowed to race a sweep in flight.
            log.warning(
                "a refresh outcome arrived for an account that is not registered",
                extra={"account_id": account_id},
            )
            return None
        current = _decode(account_id, snap.to_dict() or {})
        if current.state is AccountState.REAUTH_REQUIRED:
            return current
        ref.update(
            {
                "state": AccountState.REAUTH_REQUIRED.value,
                "reason": reason,
                "state_before_reauth": current.state.value,
            }
        )
        # On the transition only, like the mark itself: one re-auth, one event.
        # No tenant: a dead credential is met by the refresh sweep, not a task.
        self.record_health(account_id, HEALTH_REAUTH,
                           message=f"marked for re-authentication: {reason}")
        return self.get(account_id)

    def clear_reauth_required(self, account_id: str) -> Account | None:
        """Give a recovered account back the state it had. Returns it, or None.

        THE EXIT, and it has to ship with the mark rather than after it.
        `due_for_refresh` excludes REAUTH_REQUIRED, so once the sweep writes
        that state the sweep will never look at the account again: without a
        way back, marking a dead credential would turn a silent failure into a
        permanent one.

        Called only where a credential has just been proven to work -- a
        completed exchange, or a freshly written credential pair. Not on
        `still_valid`, which says the stored ACCESS token has hours left and
        says nothing about the refresh token that is the thing that died.

        An account that is not marked is returned untouched, so this is safe to
        call on every success without first asking what state the account is in.
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            return None
        current = _decode(account_id, snap.to_dict() or {})
        if current.state is not AccountState.REAUTH_REQUIRED:
            return current
        restored = current.state_before_reauth or AccountState.AVAILABLE
        ref.update(
            {"state": restored.value, "reason": "", "state_before_reauth": ""}
        )
        log.info(
            "an account's credential works again; restoring the state it had",
            extra={"account_id": account_id, "state": restored.value},
        )
        return self.get(account_id)

    def set_model_classes(self, account_id: str, classes: Iterable[str]) -> Account | None:
        """The owner's list of the model classes this account serves (R5).

        Validated against the frozen vocabulary and stored in its order; an
        empty list means every class. An UPDATE of the one field, not a `set`
        of the document, so the holds and readings moving under it in other
        transactions are untouched. None when no such account exists.

        Takes effect at the next assignment: a step already on the account
        keeps it, because its class did not change -- only who may be handed
        the next one.
        """
        stored = validate_model_classes(classes)
        ref = self._db.collection(COLLECTION).document(account_id)
        if not getattr(ref.get(), "exists", False):
            return None
        ref.update({"model_classes": list(stored)})
        return self.get(account_id)

    def set_display_name(self, account_id: str, name: str | None) -> Account | None:
        """The owner's Name for this account, or None to clear it (#1913).

        Validated by `validate_display_name` and stored trimmed. An UPDATE of
        the one field, as `set_model_classes` is, so the holds and readings
        moving under it in other transactions are untouched and a rename never
        interrupts work on the account. The id, the label and the secret do
        not change: the Name is display text. None when no such account exists.
        """
        stored = None if name is None else validate_display_name(name)
        ref = self._db.collection(COLLECTION).document(account_id)
        if not getattr(ref.get(), "exists", False):
            return None
        ref.update({"display_name": stored})
        return self.get(account_id)

    def set_max_agents(self, account_id: str, max_agents: int) -> Account | None:
        """The owner's cap on how many agents this account holds at once (#2222).

        Validated by `validate_max_agents`. An UPDATE of the one field, as
        `set_model_classes` is, so the holds and readings moving under it in
        other transactions are untouched. Lowering it moves no agent: the ones
        already on the account keep it, and no new one is handed it until
        enough of them finish. None when no such account exists.
        """
        stored = validate_max_agents(max_agents)
        ref = self._db.collection(COLLECTION).document(account_id)
        if not getattr(ref.get(), "exists", False):
            return None
        ref.update({"max_agents": stored})
        return self.get(account_id)

    def remove(self, account_id: str) -> None:
        """Forget the account. Does NOT delete the secret.

        Deliberate: a credential destroyed by a mistyped label cannot be
        recovered, and Secret Manager's own delayed destruction is what makes
        that reversible. Removing the record stops the account being used;
        deleting the secret is a separate, explicit act.
        """
        self._db.collection(COLLECTION).document(account_id).delete()
        # The health record goes with it: Firestore keeps a subcollection when
        # its parent is deleted, and an account registered again under the same
        # id must not inherit a removed one's 429s.
        self._health_ref(account_id).delete()

    def record_reading(
        self,
        account_id: str,
        windows: dict[str, WindowReading],
        observed_at: datetime,
    ) -> Account | None:
        """Fold a rate-limit reading into an account, window by window.

        Two writers now: the broker's usage poller and every worker holding an
        account (`POST /v1/accounts/{id}/readings`). A MERGE per window --
        `Account.with_reading` -- so a reading carrying only `seven_day` keeps
        the stored `five_hour` (docs/web-ui/06-accounts.md P4). Out-of-order
        reports are expected, and the newer reading of each window wins.

        IN A TRANSACTION, because a merge is a read-modify-write and two pods
        reporting different windows of the same account at once would
        otherwise each write back the other's window as it was before.
        """
        from google.cloud import firestore

        from .sweeplease import txn_snapshot

        ref = self._db.collection(COLLECTION).document(account_id)

        @firestore.transactional
        def _apply(txn: Any) -> Account | None:
            snap = txn_snapshot(txn.get(ref))
            if not getattr(snap, "exists", False):
                return None
            current = _decode(account_id, snap.to_dict() or {})
            updated = current.with_reading(windows, observed_at)
            if updated is current:
                return current
            payload: dict[str, Any] = {
                "windows": windows_to_firestore(updated.windows),
                "observed_at": updated.observed_at,
            }
            if current.limited_until is not None and updated.limited_until is None:
                # The reading shows the refusing window reset
                # (`Account.with_reading`). The health record's hold goes with
                # it, in this transaction: `record_health` mirrors the
                # record's value, so a record left holding the old one would
                # put it back on the account at the next report.
                payload["limited_until"] = None
                payload["limited_window"] = None
                txn.set(self._health_ref(account_id), {"limited_until": None}, merge=True)
            txn.update(ref, payload)
            return updated

        result = _apply(self._db.transaction())
        if result is None:
            log.warning(
                "a reading arrived for an account that is not registered",
                extra={"account_id": account_id},
            )
        return result

    def record_refresh(
        self,
        account_id: str,
        *,
        at: datetime,
        reason: str,
        expires_at: datetime | None,
        organisation: Any = None,
    ) -> bool:
        """Write what the refresh sweep did with this account. False if it is gone.

        KEYED BY ACCOUNT ID, which is `tenant:label` and the document id: a
        label is unique only within a tenant (docs/web-ui/06-accounts.md P2).

        `token_expires_at` is written only when the outcome carries one. A
        failed exchange says nothing about when the stored token expires, and
        blanking a known expiry on every failure would hide exactly the figure
        an operator needs while it is failing. `organisation` likewise (#2222):
        written only when the exchange named one, so a refresh that could not
        say never erases what an earlier one recorded -- and its seat (#2282)
        only when the exchange named that too: a refresh never changes the
        login a credential was issued to, so an answer that leaves it unsaid
        must not erase the seat that tells two Team seats apart.
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            # Removed while the sweep was in flight; `remove` may race it.
            return False
        payload: dict[str, Any] = {"last_refresh_at": at, "last_refresh_reason": reason}
        if expires_at is not None:
            payload["token_expires_at"] = expires_at
        if organisation is not None:
            payload["organisation_id"] = organisation.identity
            payload["organisation_name"] = organisation.name
            payload["organisation_plan"] = organisation.plan
            seat = getattr(organisation, "seat", None)
            if seat is not None:
                payload["seat_id"] = seat
        ref.update(payload)
        if reason in REFRESH_FAILURES:
            self.record_health(account_id, HEALTH_REFRESH_FAILED,
                               message=f"credential refresh: {reason}")
        return True

    def record_organisation(self, account_id: str, organisation: Any) -> Account | None:
        """Write which organisation this account's credential runs on (#2222).

        `organisation` is an `oauth.Organisation`: an opaque identity, never
        the provider's id, its display name and plan, and the seat (#2282).
        None for an account that is gone.

        A NEW SIGN-IN WRITES ITS SEAT EVEN WHEN IT IS None: the credential was
        replaced, possibly by another login, so the old seat no longer says
        anything about it. A seatless account pairs only by identical
        readings (`accounts.one_subscription`).
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        if not getattr(ref.get(), "exists", False):
            return None
        ref.update(
            {
                "organisation_id": organisation.identity,
                "organisation_name": organisation.name,
                "organisation_plan": organisation.plan,
                "seat_id": getattr(organisation, "seat", None),
            }
        )
        return self.get(account_id)

    def set_duplicate(self, account_id: str, duplicate_of: str | None, withheld: bool) -> bool:
        """Mark (or clear) this account as one of a pair on one subscription
        (one organisation and seat, #2282).

        False when the account is gone. Written by the broker from
        `accounts.duplicates`, never by a caller.
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        if not getattr(ref.get(), "exists", False):
            return False
        ref.update({"duplicate_of": duplicate_of, "duplicate_withheld": bool(withheld)})
        return True

    def set_identical_readings(
        self,
        account_id: str,
        count: Any,
        possible_duplicate_of: str | None,
    ) -> bool:
        """Write (or clear) this account's identical-readings count and the
        possible duplicate it implies (#2222). `count` is an
        `accounts.IdenticalReadings` or None. False when the account is gone.
        Written by the broker from `accounts.identical_reading_pairs`, never
        by a caller.
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        if not getattr(ref.get(), "exists", False):
            return False
        ref.update({
            "identical_readings": count.to_firestore() if count is not None else None,
            "possible_duplicate_of": possible_duplicate_of,
        })
        return True

    # -- the health record (#2302) ------------------------------------------

    def _health_ref(self, account_id: str) -> Any:
        return (
            self._db.collection(COLLECTION)
            .document(account_id)
            .collection(HEALTH_SUBCOLLECTION)
            .document(HEALTH_DOCUMENT)
        )

    def health(self, account_id: str) -> dict[str, Any]:
        """The raw health record, or {} when nothing was ever recorded."""
        snap = self._health_ref(account_id).get()
        if not getattr(snap, "exists", False):
            return {}
        return snap.to_dict() or {}

    def health_many(self, account_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """`health` for each id: what a listing draws its rows from."""
        return {account_id: self.health(account_id) for account_id in account_ids}

    def record_health(
        self,
        account_id: str,
        kind: str,
        *,
        tenant_id: str | None = None,
        task_id: str | None = None,
        window: str | None = None,
        reset_at: datetime | None = None,
        message: str = "",
        limited_until: datetime | None = None,
    ) -> bool:
        """Count one event against an account. NEVER RAISES; False if not written.

        IN A TRANSACTION, because the record is a read-modify-write and two
        workers on one account report together. Never raising, because every
        caller is on its way somewhere more important -- a park, a release, a
        refresh sweep -- and a health record that could not be written must not
        cost that. An unknown kind is a bug in the caller and is logged at
        ERROR, not silently counted as something else.

        `limited_until` (a rate-limit or exhausted report) is MIRRORED onto
        the account document in the same transaction: the record's folded
        value, which only ever moves later, and the window of the report that
        set it. `choose()` reads it from there (`Account.is_limited`), so the
        assign path never reads a health record per candidate, and the record
        and the hold cannot disagree. An account that is gone gets no mirror;
        the record is still written, as before.
        """
        from google.cloud import firestore

        from .sweeplease import txn_snapshot

        now = self._now()
        try:
            event = health_event(
                kind,
                now,
                tenant_id=tenant_id,
                task_id=task_id,
                window=window,
                reset_at=reset_at,
                message=message,
            )
        except ValueError:
            log.error("a health event of an unknown kind was not recorded",
                      extra={"account_id": account_id, "kind": kind})
            return False
        ref = self._health_ref(account_id)
        account_ref = self._db.collection(COLLECTION).document(account_id)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            # Every read before any write: Firestore refuses a read after a
            # write inside one transaction.
            snap = txn_snapshot(txn.get(ref))
            account_snap = (
                txn_snapshot(txn.get(account_ref)) if limited_until is not None else None
            )
            raw = snap.to_dict() if getattr(snap, "exists", False) else None
            folded = fold_health(raw, event, now, limited_until=limited_until)
            txn.set(ref, folded)
            if account_snap is None or not getattr(account_snap, "exists", False):
                return
            stored = folded["limited_until"]
            current = account_snap.to_dict() or {}
            window = (
                event["window"]
                if stored == limited_until
                else current.get("limited_window")
            )
            txn.update(account_ref, {"limited_until": stored, "limited_window": window})

        try:
            _apply(self._db.transaction())
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning(
                "could not record an account health event",
                extra={"account_id": account_id, "kind": kind, "error": type(exc).__name__},
            )
            return False
        return True

    def every_account_limited(self, tenant_id: str, provider: str, now: datetime) -> bool:
        """Whether every account `tenant_id` may draw `provider` work from is limited.

        THE SET `choose()` DRAWS FROM, by its own predicate less the filters
        that pass with time: owned or lent (`accounts_serving`, invariant 9),
        of this provider, in a state that takes new agents, not a withheld
        duplicate (`_withheld`), and not one THIS tenant found unreadable
        (`is_unreadable_for`). An account `choose()` would never hand this
        tenant is no room, so it must not keep the tenant from being halved:
        a borrower that cannot read a lent account would otherwise never be.
        A FULL account and one awaiting its first reading ARE room -- a hold
        ends, a reading arrives -- and the step's model class is not asked
        about: a 429 is about the provider, not one step. Each account in the
        set is limited by `account_limited`: a recorded 429 not yet reset, or
        window readings that block it.

        True on an EMPTY set too: no account can take the tenant's work, which
        is the pool exhausted as surely as every account spent. That is the one
        case an account's 429 still halves the tenant (`quota_broker.main`).
        """
        accounts = self.list()
        withheld = _withheld(accounts)
        serving = [
            a
            for a in accounts_serving(accounts, tenant_id, provider)
            if a.state in ASSIGNABLE_STATES
            and a.account_id not in withheld
            and not a.is_unreadable_for(tenant_id, now)
        ]
        records = self.health_many(a.account_id for a in serving)
        return all(account_limited(a, records.get(a.account_id), now) for a in serving)

    def secret_for(self, account: Account) -> str:
        """Where this account's credential actually lives.

        The RECORDED name wins over a derived one. Deriving was safe until the
        naming rule changed, at which point every account already registered
        began pointing at a secret that had never existed -- silently, because
        a missing refresh secret and a tenant that simply uses an API key
        report the same thing.
        """
        return account.secret or secret_name(account.owner_tenant, account.label)


__all__ = [
    "AccountListing",
    "AccountStore",
    "COLLECTION",
    "HEALTH_KINDS",
    "MalformedAccountError",
    "PROVIDER_ERROR_KINDS",
    "health_status",
    "health_to_api",
]
