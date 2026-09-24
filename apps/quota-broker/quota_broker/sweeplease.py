"""A lease on the credential phase of the quota sweep: one sweep exchanges at a time.

WHY (docs/audits/2026-09-18/06-quota-broker-accounts.md section 3). Nothing
serialised `/v1/quota/sweep`. Cloud Scheduler retries a tick it believes timed
out WITHOUT cancelling the first request, one broker instance serves many
requests at once, and there can be several instances. Two sweeps then read the
same refresh token; one exchanges it and the provider rotates it; the other
presents the token that was just rotated away and is told `invalid_grant`
about a perfectly healthy account. `_sweep_account_pool` confirms before it
marks anything REAUTH_REQUIRED, which made the race harmless THERE -- and did
nothing about the exchanges themselves, which the losing sweep spent against
the token endpoint's shared rate limit for every account it touched, nor about
the subscription-tenant sweep, which has no confirmation step at all.

WHY FIRESTORE. The race is between instances as much as between requests, so a
process lock covers one of the two. The broker already owns this database.

WHY NOT THE FROZEN `swarm_common.models.Lease`. That type is a TASK's claim on
capacity -- task, attempt, pools, units, fencing generation -- and nothing in
it describes a sweep. Writing a sweep lock into the `leases` collection would
hand the reconciler a document it reads as a task lease. So this is its own
document, in its own collection, like `accounts` and `credential_publications`,
and the frozen contract is untouched.

HOW IT HOLDS.

  * `acquire` takes the lease if nobody holds it, the holder released it, or
    the holder's deadline has passed. A sweep killed mid-flight never
    releases; its lease simply expires.
  * `renew` is called before EVERY exchange (see `LeaseFence`). It succeeds
    only while the document still names this holder, so a sweep whose lease
    was taken over stops before its next exchange rather than finishing a
    pass it no longer owns.
  * `release` gives it back only if it is still ours, so a sweep that lost its
    lease cannot free the one somebody else now holds.

It FAILS CLOSED: a sweep that cannot read or write the lease does not exchange.
Skipping one tick costs five minutes on a token that is refreshed hours before
it expires; an exchange with no exclusion can cost the token.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Broker-owned, like `accounts` and `credential_publications`. NOT `leases`:
#: that collection is task leases, and the reconciler reads every document in it.
COLLECTION = "sweep_leases"

#: The one lease that exists today. A name rather than a singleton so a second
#: serialised job does not need a second module.
QUOTA_SWEEP = "quota-sweep"

#: How long a lease lives without a renewal.
#:
#: It only has to outlive ONE step, because it is renewed before every
#: exchange: the longest single wait in a step is the token endpoint's 30s
#: timeout (`oauth.HttpTokenEndpoint`), plus a Secret Manager read and write.
#: 120s is four of those. And it bounds the cost of a sweep that dies holding
#: it: the next tick is five minutes away (`quota_refresh_schedule`), so a dead
#: holder's lease has always expired by then.
DEFAULT_TTL = timedelta(seconds=120)


def txn_snapshot(result: Any) -> Any:
    """One DocumentSnapshot out of a Firestore transactional get.

    `Transaction.get()` returns a GENERATOR in google-cloud-firestore, because
    the same method takes a Query as well as a DocumentReference. Treating the
    result as a snapshot raises `AttributeError: 'generator' object has no
    attribute 'exists'` against the real client while every in-memory double
    hands back a snapshot directly -- so no unit test can catch it. The frozen
    `swarm_common.admission` carries the same adapter for the same reason.

    Lives here, and `main` imports it, so the broker has one copy of it.
    """
    if hasattr(result, "exists"):
        return result
    try:
        return next(iter(result))
    except StopIteration:  # pragma: no cover - a get always yields one result
        raise RuntimeError("transactional get returned no snapshot") from None


def new_holder() -> str:
    """An id for one sweep: which instance, which process, which request.

    The first two are for the person reading the document; the third is what
    makes two concurrent requests in one process different holders.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class LeaseState:
    #: Whether THIS caller now holds the lease.
    acquired: bool
    #: This caller when acquired; whoever holds it when not.
    holder: str
    expires_at: datetime | None
    generation: int | None


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_live(data: dict[str, Any], now: datetime) -> bool:
    expires = _aware(data.get("expires_at"))
    return (
        bool(data.get("holder"))
        and data.get("released_at") is None
        and expires is not None
        and expires > now
    )


class FirestoreSweepLease:
    """The lease, as one document, changed only inside transactions."""

    def __init__(
        self,
        db: Any,
        *,
        name: str = QUOTA_SWEEP,
        ttl: timedelta = DEFAULT_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._name = name
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _ref(self) -> Any:
        return self._db.collection(COLLECTION).document(self._name)

    def acquire(self, holder: str) -> LeaseState:
        from google.cloud import firestore

        ref = self._ref()
        now = self._now()
        ttl = self._ttl

        @firestore.transactional
        def _apply(txn: Any) -> LeaseState:
            snap = txn_snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
            current = str(data.get("holder") or "")
            if _is_live(data, now) and current != holder:
                return LeaseState(
                    acquired=False,
                    holder=current,
                    expires_at=_aware(data.get("expires_at")),
                    generation=_int(data.get("generation")),
                )
            generation = (_int(data.get("generation")) or 0) + 1
            expires_at = now + ttl
            txn.set(
                ref,
                {
                    "holder": holder,
                    "acquired_at": now,
                    "renewed_at": now,
                    "expires_at": expires_at,
                    "released_at": None,
                    # Counts takeovers. A generation that climbs by more than
                    # one per tick is sweeps dying mid-flight, or overlapping.
                    "generation": generation,
                },
            )
            return LeaseState(
                acquired=True, holder=holder, expires_at=expires_at, generation=generation
            )

        return _apply(self._db.transaction())

    def renew(self, holder: str) -> bool:
        """Extend the lease if it is still ours. False means stop now.

        "Still ours" is the holder field, not the deadline: a lease past its
        deadline that nobody took over is still ours to extend, because taking
        it over is a write that would have replaced the holder.
        """
        from google.cloud import firestore

        ref = self._ref()
        now = self._now()
        ttl = self._ttl

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = txn_snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
            if data.get("holder") != holder or data.get("released_at") is not None:
                return False
            txn.update(ref, {"renewed_at": now, "expires_at": now + ttl})
            return True

        return _apply(self._db.transaction())

    def release(self, holder: str) -> bool:
        """Give it back, if it is still ours. Never frees somebody else's."""
        from google.cloud import firestore

        ref = self._ref()
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = txn_snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
            if data.get("holder") != holder or data.get("released_at") is not None:
                return False
            txn.update(ref, {"released_at": now, "expires_at": now})
            return True

        return _apply(self._db.transaction())


class InProcessSweepLease:
    """The same lease, held in this process only.

    For a broker built with no Firestore handle, exactly as
    `InMemoryPublishLedger` is: correct across the requests of one process and
    blind across instances. Production always has the handle; this exists so a
    deployment or a test without one still gets the within-process exclusion
    rather than none.
    """

    def __init__(
        self,
        *,
        ttl: timedelta = DEFAULT_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def acquire(self, holder: str) -> LeaseState:
        with self._lock:
            now = self._now()
            current = str(self._data.get("holder") or "")
            if _is_live(self._data, now) and current != holder:
                return LeaseState(
                    acquired=False,
                    holder=current,
                    expires_at=_aware(self._data.get("expires_at")),
                    generation=_int(self._data.get("generation")),
                )
            generation = (_int(self._data.get("generation")) or 0) + 1
            self._data = {
                "holder": holder,
                "expires_at": now + self._ttl,
                "released_at": None,
                "generation": generation,
            }
            return LeaseState(True, holder, self._data["expires_at"], generation)

    def renew(self, holder: str) -> bool:
        with self._lock:
            if self._data.get("holder") != holder or self._data.get("released_at") is not None:
                return False
            self._data["expires_at"] = self._now() + self._ttl
            return True

    def release(self, holder: str) -> bool:
        with self._lock:
            if self._data.get("holder") != holder or self._data.get("released_at") is not None:
                return False
            now = self._now()
            self._data["released_at"] = now
            self._data["expires_at"] = now
            return True


def build_sweep_lease(db: Any | None) -> FirestoreSweepLease | InProcessSweepLease:
    return FirestoreSweepLease(db) if db is not None else InProcessSweepLease()


class LeaseFence:
    """`keep_going` for one sweep: renews the lease, and says stop once it is lost.

    Passed to every loop that exchanges or polls, and called before each
    step. Once a renewal fails it answers False for the rest of the sweep
    without asking again -- a lease that was taken over does not come back,
    and asking would be one more write against a document another sweep owns.

    A renewal that RAISES counts as lost, for the reason the module docstring
    gives for failing closed.
    """

    def __init__(self, lease: Any, holder: str) -> None:
        self._lease = lease
        self._holder = holder
        self.lost = False
        self.renewals = 0

    def __call__(self) -> bool:
        if self.lost:
            return False
        try:
            held = bool(self._lease.renew(self._holder))
        except Exception as exc:
            log.error(
                "could not renew the sweep lease; stopping the credential sweep here",
                extra={"error": type(exc).__name__, "detail": str(exc)[:200]},
            )
            held = False
        if not held:
            self.lost = True
            log.warning(
                "the sweep lease was taken over mid-sweep; stopping before the next "
                "exchange so two sweeps never present the same refresh token",
                extra={"holder": self._holder, "renewals": self.renewals},
            )
            return False
        self.renewals += 1
        return True

    def release(self) -> bool:
        """Release at the end of the sweep. A lost lease is not ours to release."""
        if self.lost:
            return False
        try:
            return bool(self._lease.release(self._holder))
        except Exception as exc:
            # Not fatal: an unreleased lease expires on its own within DEFAULT_TTL.
            log.warning(
                "could not release the sweep lease; it expires on its own",
                extra={"error": type(exc).__name__},
            )
            return False


__all__ = [
    "COLLECTION",
    "DEFAULT_TTL",
    "QUOTA_SWEEP",
    "FirestoreSweepLease",
    "InProcessSweepLease",
    "LeaseFence",
    "LeaseState",
    "build_sweep_lease",
    "new_holder",
    "txn_snapshot",
]
