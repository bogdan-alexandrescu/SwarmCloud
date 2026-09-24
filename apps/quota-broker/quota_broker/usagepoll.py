"""Polling account quota windows, within a budget that cannot be measured.

This is the caller `AccountStore.record_reading` never had. Without it,
`Account.windows` stayed empty for the life of the platform, so `headroom`
returned full for every account, `_binding_remaining` had nothing to bind on,
and `may_serve`'s assign floor -- the check that stops an agent being started on
an account with 2% left -- could never fire.

WHY THIS IS A POLLER AND NOT A SWEEP
------------------------------------
`quota_broker.usage` documents the constraint: roughly five calls per five
minutes, 429 with `retry-after: 299`, and no rate-limit headers on a 200, so the
remaining budget can only be discovered by running out of it. The budget is also
SHARED -- a human running `/usage`, or a rotation daemon on a laptop, spends
from the same allowance against the same accounts.

A sweep that polled every account every tick would therefore work with three
accounts and silently stop working with eight, at which point every reading
would be stale and nothing would say so. Instead:

  * at most `max_polls` accounts per round, which is a configured number rather
    than "all of them";
  * OLDEST READING FIRST, so a large pool rotates through rather than starving
    its tail;
  * a 429 ENDS THE ROUND. The budget is spent; the remaining accounts keep their
    previous readings and are first in line next time. Retrying inside the round
    would spend the next window's budget too.

A FAILED POLL NEVER BECOMES A READING
-------------------------------------
Every failure path here leaves the account's previous windows untouched. An
account whose poll failed is an account with an OLD reading, which
`Account.observed_at` already expresses and `DEFAULT_STALE_AFTER` already acts
on. Recording an empty or zero reading instead would say "this account is
completely free", which is the single most expensive thing this system could get
wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import usage
from .accounts import Account


@dataclass(frozen=True)
class PollOutcome:
    account_id: str
    polled: bool
    reason: str


class UsagePoller:
    def __init__(
        self,
        store: Any,
        account_store: Any,
        *,
        logger: Any,
        max_polls: int = usage.DEFAULT_MAX_POLLS_PER_SWEEP,
        now: Any = None,
        fetch: Any = None,
    ) -> None:
        self._store = store
        self._accounts = account_store
        self._log = logger
        self._max_polls = max_polls
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Injectable so the tests never reach the network, and so the adapter
        # stays the only place that knows the endpoint's shape.
        self._fetch = fetch or usage.fetch

    def poll_round(
        self,
        accounts: list[Account],
        *,
        keep_going: Callable[[], bool] | None = None,
    ) -> list[PollOutcome]:
        """One round, oldest reading first, stopping on a 429.

        And stopping when `keep_going` says so: the sweep lease's fence, asked
        before each poll, because two concurrent rounds spend one budget twice.
        """
        outcomes: list[PollOutcome] = []
        if self._max_polls <= 0 or not accounts:
            return outcomes

        # `observed_at is None` sorts first: an account never read is the one
        # whose headroom is most wrongly assumed to be full.
        ordered = sorted(
            accounts,
            key=lambda a: (
                a.observed_at is not None,
                a.observed_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )

        for account in ordered[: self._max_polls]:
            if keep_going is not None and not keep_going():
                break
            try:
                token = self._store.access(account.secret)
            except Exception as exc:
                # Never log the secret's NAME alongside a failure reason that
                # might carry payload detail; the account id is enough to act on.
                self._log.warning(
                    "could not read an account's access token; leaving its reading untouched",
                    extra={"account_id": account.account_id, "error": type(exc).__name__},
                )
                outcomes.append(PollOutcome(account.account_id, False, "token_unreadable"))
                continue

            try:
                windows = self._fetch(token)
            except usage.RateLimited as exc:
                # The budget is spent. Ending the round is the whole point: the
                # accounts not reached keep their previous readings and sort
                # first next time.
                self._log.info(
                    "usage budget spent; ending this round",
                    extra={
                        "account_id": account.account_id,
                        "retry_after_seconds": exc.retry_after_seconds,
                        "polled": len(outcomes),
                    },
                )
                outcomes.append(PollOutcome(account.account_id, False, "rate_limited"))
                break
            except usage.UsageUnavailable as exc:
                self._log.warning(
                    "usage reading unavailable; leaving the previous one in place",
                    extra={"account_id": account.account_id, "error": str(exc)},
                )
                outcomes.append(PollOutcome(account.account_id, False, "unavailable"))
                continue

            self._accounts.record_reading(account.account_id, windows, self._now())
            outcomes.append(PollOutcome(account.account_id, True, "ok"))

        return outcomes
