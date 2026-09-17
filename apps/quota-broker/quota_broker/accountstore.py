"""Where accounts live: Firestore for the facts, Secret Manager for the secret.

The split is not incidental. Firestore holds what everything needs to read
constantly -- who owns an account, how much headroom it has, whether it is
draining -- and Secret Manager holds the one thing almost nothing should read at
all. Putting the credential in Firestore would make every component that lists
accounts a component that can read them.

CONCURRENCY
-----------
`assigned` is incremented by the scheduler as agents start and decremented as
they finish, from more than one process. Those are transactions, not
read-modify-writes, because the count is what `choose()` uses to spread load: a
lost decrement makes an account look permanently busier than it is and quietly
pushes work onto the others until someone notices the imbalance.

The credential itself has exactly ONE writer -- the refresher -- for the reason
given at length in quota_broker.credentials: refresh tokens rotate, and two
writers invalidate each other's tokens.
"""

from __future__ import annotations

import logging
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


class AccountStore:
    def __init__(self, db: Any, *, now: Any = None) -> None:
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- reading ----------------------------------------------------------

    def list(self) -> list[Account]:
        """Every account. Deliberately unfiltered.

        The refresher needs ALL of them, including paused, draining and idle
        ones, because an account nobody is using is an account whose refresh
        token is quietly expiring. A filter here would be the kind of
        optimisation that breaks the property the pool exists for.
        """
        out: list[Account] = []
        for doc in self._db.collection(COLLECTION).stream():
            data = doc.to_dict() or {}
            try:
                out.append(Account.from_firestore(data))
            except (KeyError, ValueError) as exc:
                # One malformed document must not hide the rest of the fleet.
                log.warning(
                    "skipping an unreadable account document",
                    extra={"account_id": doc.id, "error": str(exc)},
                )
        return out

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
                assigned=current.assigned,
                reason=current.reason,
            )
            ref.set(updated.to_firestore())
            return updated

        account = Account(
            account_id=account_id,
            owner_tenant=owner_tenant,
            label=label,
            provider=provider,
            lend_to=lend,
        )
        ref.set(account.to_firestore())
        return account

    def set_state(self, account_id: str, state: AccountState, reason: str = "") -> None:
        self._db.collection(COLLECTION).document(account_id).update(
            {"state": state.value, "reason": reason}
        )

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
        return secret_name(account.owner_tenant, account.label)


__all__ = ["AccountStore", "COLLECTION"]
