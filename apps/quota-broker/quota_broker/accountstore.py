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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .accounts import (
    Account,
    AccountError,
    AccountState,
    WindowReading,
    account_id_for,
    secret_name,
    validate_label,
)

log = logging.getLogger(__name__)

COLLECTION = "accounts"


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
        """
        out: list[Account] = []
        unreadable: list[str] = []
        for doc in self._db.collection(COLLECTION).stream():
            data = doc.to_dict() or {}
            try:
                out.append(Account.from_firestore(data))
            except (KeyError, ValueError) as exc:
                # One malformed document must not hide the rest of the fleet --
                # and must not hide itself either. See `AccountListing`.
                unreadable.append(str(doc.id))
                log.warning(
                    "skipping an unreadable account document",
                    extra={"account_id": doc.id, "error": str(exc)},
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
        return Account.from_firestore(snap.to_dict() or {})

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
            current = Account.from_firestore(existing.to_dict() or {})
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
        self._db.collection(COLLECTION).document(account_id).update(
            {"state": state.value, "reason": reason, "state_before_reauth": ""}
        )

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
        current = Account.from_firestore(snap.to_dict() or {})
        if current.state is AccountState.REAUTH_REQUIRED:
            return current
        ref.update(
            {
                "state": AccountState.REAUTH_REQUIRED.value,
                "reason": reason,
                "state_before_reauth": current.state.value,
            }
        )
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
        current = Account.from_firestore(snap.to_dict() or {})
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

    def remove(self, account_id: str) -> None:
        """Forget the account. Does NOT delete the secret.

        Deliberate: a credential destroyed by a mistyped label cannot be
        recovered, and Secret Manager's own delayed destruction is what makes
        that reversible. Removing the record stops the account being used;
        deleting the secret is a separate, explicit act.
        """
        self._db.collection(COLLECTION).document(account_id).delete()

    def record_reading(
        self,
        account_id: str,
        windows: dict[str, WindowReading],
        observed_at: datetime,
    ) -> Account | None:
        """Fold a worker's rate-limit reading into an account.

        Out-of-order reports are expected: several pods report at once, and the
        network does not preserve order. `Account.with_reading` keeps the newer
        one, so a late arrival cannot make an exhausted account look available.
        """
        ref = self._db.collection(COLLECTION).document(account_id)
        snap = ref.get()
        if not getattr(snap, "exists", False):
            log.warning(
                "a reading arrived for an account that is not registered",
                extra={"account_id": account_id},
            )
            return None
        current = Account.from_firestore(snap.to_dict() or {})
        updated = current.with_reading(windows, observed_at)
        if updated is current:
            return current
        ref.update(
            {
                "windows": {
                    name: {"utilization": w.utilization, "resets_at": w.resets_at}
                    for name, w in updated.windows.items()
                },
                "observed_at": updated.observed_at,
            }
        )
        return updated

    def secret_for(self, account: Account) -> str:
        """Where this account's credential actually lives.

        The RECORDED name wins over a derived one. Deriving was safe until the
        naming rule changed, at which point every account already registered
        began pointing at a secret that had never existed -- silently, because
        a missing refresh secret and a tenant that simply uses an API key
        report the same thing.
        """
        return account.secret or secret_name(account.owner_tenant, account.label)


__all__ = ["AccountStore", "COLLECTION"]
