"""The poller that finally gives `record_reading` a caller.

The properties under test are all about what happens when a poll FAILS, because
that is where the expensive mistake lives: an account whose reading could not be
taken must keep its old one, never gain an empty or zero one. An account
reporting "completely free" when it is exhausted is the single most costly thing
this system can get wrong -- the scheduler would send every agent to it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quota_broker.accounts import Account, AccountState, WindowReading
from quota_broker.usage import RateLimited, UsageUnavailable
from quota_broker.usagepoll import UsagePoller

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class Log:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def error(self, *a, **k): pass


class Secrets:
    def __init__(self, tokens=None, fail=()):
        self._tokens = tokens or {}
        self._fail = set(fail)

    def access(self, name):
        if name in self._fail:
            raise KeyError(name)
        return self._tokens.get(name, "tok")


class Accounts:
    def __init__(self):
        self.recorded = []

    def record_reading(self, account_id, windows, observed_at):
        self.recorded.append((account_id, windows, observed_at))


def account(label, observed_at=None):
    return Account(
        account_id=f"u-bogdan:{label}",
        owner_tenant="u-bogdan",
        label=label,
        provider="anthropic",
        state=AccountState.AVAILABLE,
        observed_at=observed_at,
    )


def reading(pct=0.4):
    return {"five_hour": WindowReading(utilization=pct, resets_at=NOW + timedelta(hours=1))}


def poller(**kw):
    kw.setdefault("logger", Log())
    kw.setdefault("now", lambda: NOW)
    return UsagePoller(kw.pop("secrets", Secrets()), kw.pop("accounts", Accounts()), **kw)


def test_a_rate_limit_ends_the_round_instead_of_retrying():
    """The budget is spent; the remaining accounts must NOT be attempted.

    Retrying inside the round would spend the next window's allowance too, and
    the endpoint gives no way to know how much is left.
    """
    accounts = Accounts()
    calls = []

    def fetch(token):
        calls.append(token)
        raise RateLimited(299)

    p = UsagePoller(Secrets(), accounts, logger=Log(), now=lambda: NOW, fetch=fetch, max_polls=3)
    out = p.poll_round([account("a"), account("b"), account("c")])

    assert len(calls) == 1, "the round continued after a 429"
    assert [o.reason for o in out] == ["rate_limited"]
    assert accounts.recorded == []


def test_a_failed_poll_records_nothing_at_all():
    """The account keeps its previous reading. It does NOT gain an empty one."""
    accounts = Accounts()

    def fetch(token):
        raise UsageUnavailable("endpoint returned HTTP 503")

    p = UsagePoller(Secrets(), accounts, logger=Log(), now=lambda: NOW, fetch=fetch, max_polls=3)
    out = p.poll_round([account("a")])

    assert accounts.recorded == [], "a failed read was recorded as a reading"
    assert out[0].polled is False


def test_an_unreadable_token_skips_that_account_and_continues():
    """One bad secret must not stop the pool being read."""
    accounts = Accounts()
    p = UsagePoller(
        Secrets(fail={"swarm-account-u-bogdan--a"}),
        accounts,
        logger=Log(),
        now=lambda: NOW,
        fetch=lambda t: reading(),
        max_polls=3,
    )
    out = p.poll_round([account("a"), account("b")])

    assert [o.reason for o in out] == ["token_unreadable", "ok"]
    assert [r[0] for r in accounts.recorded] == ["u-bogdan:b"]


def test_the_never_read_account_is_polled_first():
    """`observed_at is None` is the account whose headroom is most wrongly assumed."""
    accounts = Accounts()
    p = UsagePoller(
        Secrets(), accounts, logger=Log(), now=lambda: NOW,
        fetch=lambda t: reading(), max_polls=1,
    )
    fresh = account("fresh", observed_at=NOW - timedelta(minutes=1))
    never = account("never", observed_at=None)
    p.poll_round([fresh, never])

    assert [r[0] for r in accounts.recorded] == ["u-bogdan:never"]


def test_the_oldest_reading_is_polled_before_a_fresher_one():
    accounts = Accounts()
    p = UsagePoller(
        Secrets(), accounts, logger=Log(), now=lambda: NOW,
        fetch=lambda t: reading(), max_polls=1,
    )
    old = account("old", observed_at=NOW - timedelta(hours=2))
    new = account("new", observed_at=NOW - timedelta(minutes=2))
    p.poll_round([new, old])

    assert [r[0] for r in accounts.recorded] == ["u-bogdan:old"]


def test_the_budget_is_a_hard_cap():
    """More accounts than budget must not mean more calls than budget."""
    accounts = Accounts()
    calls = []
    p = UsagePoller(
        Secrets(), accounts, logger=Log(), now=lambda: NOW, max_polls=2,
        fetch=lambda t: (calls.append(t), reading())[1],
    )
    p.poll_round([account(x) for x in "abcde"])

    assert len(calls) == 2
    assert len(accounts.recorded) == 2


def test_nothing_happens_with_no_accounts_or_no_budget():
    accounts = Accounts()
    p = UsagePoller(Secrets(), accounts, logger=Log(), now=lambda: NOW, fetch=lambda t: reading())
    assert p.poll_round([]) == []
    p2 = UsagePoller(
        Secrets(), accounts, logger=Log(), now=lambda: NOW,
        fetch=lambda t: reading(), max_polls=0,
    )
    assert p2.poll_round([account("a")]) == []
    assert accounts.recorded == []
