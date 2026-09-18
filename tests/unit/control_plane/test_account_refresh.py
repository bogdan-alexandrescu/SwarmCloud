"""Refreshing a pool of accounts, without a human ever stepping in.

The property: an operator onboards an account once and never touches it again.
That holds only if every account is refreshed on a timer whether or not anything
is using it, because an unexchanged refresh token expires on its own.
"""

from __future__ import annotations

import itertools
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.credentials import REFRESH_SUFFIX, CredentialRefresher
from quota_broker.oauth import CredentialError, ReauthRequired
from swarm_common.logging_setup import CloudLoggingFormatter

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
_SEQ = itertools.count()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []
        self._fmt = CloudLoggingFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self._fmt.format(record))


class _Log:
    """A real logging.Logger, so a structlog-style call fails here not in prod."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(f"test.accounts.{next(_SEQ)}")
        self._logger.handlers = []
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)
        self._cap = _Capture()
        self._logger.addHandler(self._cap)

    @property
    def records(self) -> list[str]:
        return self._cap.records

    def __getattr__(self, name):
        return getattr(self._logger, name)


class _Store:
    def __init__(self, initial=None, fail_on=None):
        self.data = dict(initial or {})
        self.writes: list[str] = []
        self.fail_on = fail_on

    def access(self, name):
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def add_version(self, name, payload):
        if self.fail_on and name == self.fail_on:
            raise RuntimeError("simulated failure")
        self.writes.append(name)
        self.data[name] = payload


class _Endpoint:
    def __init__(self, result=None, raises=None, per_token=None):
        self.result, self.raises = result, raises
        self.per_token = per_token or {}
        self.calls: list[str] = []

    def exchange(self, refresh_token):
        self.calls.append(refresh_token)
        if refresh_token in self.per_token:
            outcome = self.per_token[refresh_token]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if self.raises:
            raise self.raises
        return self.result


def _stored(hours, refresh_token="r"):
    return json.dumps({
        "accessToken": "old",
        "refreshToken": refresh_token,
        "expiresAt": int((NOW + timedelta(hours=hours)).timestamp() * 1000),
    })


def _refresher(store, endpoint, log=None):
    return CredentialRefresher(store, endpoint, logger=log or _Log(), now=lambda: NOW)


def _acct_secret(label):
    return f"swarm-account-u-bogdan-{label}"


# -- the property the pool exists for --------------------------------------

def test_an_idle_account_is_refreshed_like_a_busy_one():
    """The whole reason "no human steps in" is true. Nothing here knows or cares
    whether an agent is using this account."""
    base = _acct_secret("idle")
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(1)})
    ep = _Endpoint({"access_token": "new", "refresh_token": "r2", "expires_in": 28800})

    out = _refresher(store, ep).sweep_accounts([(base, "idle")])

    assert [o.refreshed for o in out] == [True]
    assert ep.calls == ["r"]
    # Both halves written, refresh first.
    assert store.writes == [f"{base}{REFRESH_SUFFIX}", base]


def test_one_bad_account_does_not_stop_the_rest_of_the_pool():
    """The others are exactly what the fleet falls back on when one goes bad, so
    a sweep that aborts on the first failure fails at the worst moment."""
    good1, bad, good2 = _acct_secret("a"), _acct_secret("b"), _acct_secret("c")
    store = _Store({
        f"{good1}{REFRESH_SUFFIX}": _stored(1, "r1"),
        f"{bad}{REFRESH_SUFFIX}": _stored(1, "rbad"),
        f"{good2}{REFRESH_SUFFIX}": _stored(1, "r3"),
    })
    ep = _Endpoint(per_token={
        "r1": {"access_token": "n1", "expires_in": 28800},
        "rbad": ReauthRequired("revoked"),
        "r3": {"access_token": "n3", "expires_in": 28800},
    })

    out = _refresher(store, ep).sweep_accounts([
        (good1, "a"), (bad, "b"), (good2, "c"),
    ])

    assert [o.refreshed for o in out] == [True, False, True]
    assert out[1].reason == "reauth_required"


def test_a_dead_account_is_named_so_a_human_can_be_told_which_one():
    """"Some account needs re-auth" is not actionable when there are five."""
    base = _acct_secret("dead")
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(1)})
    log = _Log()

    out = _refresher(store, _Endpoint(raises=ReauthRequired("gone")), log).sweep_accounts(
        [(base, "dead")]
    )

    assert out[0].reason == "reauth_required"
    assert any("dead" in r for r in log.records)


def test_an_account_far_from_expiry_costs_no_endpoint_call():
    """Refreshing every account every five minutes regardless would burn the
    token endpoint's rate limit for nothing, and the limit is shared with the
    agents actually working."""
    base = _acct_secret("fresh")
    # Seeded as already published: an account whose worker-facing secret is
    # EMPTY is a different case, and one where writing nothing is the bug.
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(10), base: "old"})
    ep = _Endpoint({"access_token": "new"})

    out = _refresher(store, ep).sweep_accounts([(base, "fresh")])

    assert out[0].reason == "still_valid"
    assert ep.calls == []
    assert store.writes == []


def test_an_account_with_no_stored_credential_is_not_an_error():
    """Registered but not yet populated is the normal state between
    `account add` recording it and the credential being uploaded."""
    out = _refresher(_Store({}), _Endpoint()).sweep_accounts([(_acct_secret("new"), "new")])
    assert out[0].refreshed is False
    assert out[0].reason == "no_refresh_credential"


def test_the_rotated_refresh_token_is_persisted_before_the_access_token():
    """Order is the safety property. A crash between the two must leave the
    account recoverable, not holding credentials it cannot renew."""
    base = _acct_secret("rot")
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(1, "old-r")})
    ep = _Endpoint({"access_token": "new", "refresh_token": "new-r", "expires_in": 28800})

    _refresher(store, ep).sweep_accounts([(base, "rot")])

    assert store.writes.index(f"{base}{REFRESH_SUFFIX}") < store.writes.index(base)


def test_a_crash_after_the_refresh_write_leaves_the_account_recoverable():
    base = _acct_secret("crash")
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(1, "old-r")}, fail_on=base)
    ep = _Endpoint({"access_token": "new", "refresh_token": "new-r", "expires_in": 28800})

    out = _refresher(store, ep).sweep_accounts([(base, "crash")])

    assert out[0].refreshed is False
    # The rotated refresh token survived, so the next sweep can try again.
    assert json.loads(store.data[f"{base}{REFRESH_SUFFIX}"])["refreshToken"] == "new-r"


def test_no_token_material_reaches_the_logs_or_the_outcome():
    base = _acct_secret("leak")
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(1, "SECRET-REFRESH")})
    ep = _Endpoint({"access_token": "SECRET-ACCESS", "refresh_token": "SECRET-ROTATED",
                    "expires_in": 28800})
    log = _Log()

    out = _refresher(store, ep, log).sweep_accounts([(base, "leak")])

    blob = json.dumps([[o.as_dict() for o in out], log.records])
    for secret in ("SECRET-REFRESH", "SECRET-ACCESS", "SECRET-ROTATED"):
        assert secret not in blob


def test_an_empty_pool_sweeps_cleanly():
    """A deployment with no accounts yet must not be a special case."""
    assert _refresher(_Store({}), _Endpoint()).sweep_accounts([]) == []
