"""Subscription credentials are refreshed by exactly one writer, safely.

The hazard this guards: refresh tokens ROTATE. If the access token were
published before the rotated refresh token were persisted, a crash between the
two would leave the tenant with credentials that work now and no way to obtain
any more -- locked out until a human re-authenticates. And if workers refreshed
concurrently, each would invalidate the others.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.credentials import CredentialRefresher, REFRESH_SUFFIX
from quota_broker.oauth import (
    Credential,
    CredentialError,
    ReauthRequired,
    parse_credential,
    refresh,
    serialise,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
BASE = "swarm-tenant-eng-anthropic"
REFRESH_SECRET = BASE + REFRESH_SUFFIX


class _Log:
    def __init__(self): self.records = []
    def info(self, m, **k): self.records.append(("info", m, k))
    def warning(self, m, **k): self.records.append(("warning", m, k))
    def error(self, m, **k): self.records.append(("error", m, k))


class _Store:
    def __init__(self, initial=None, fail_on=None):
        self.data = dict(initial or {})
        self.writes = []
        self.fail_on = fail_on

    def access(self, name):
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def add_version(self, name, payload):
        if self.fail_on and name == self.fail_on:
            raise RuntimeError("simulated crash")
        self.writes.append(name)
        self.data[name] = payload


class _Endpoint:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def exchange(self, refresh_token):
        self.calls.append(refresh_token)
        if self.raises:
            raise self.raises
        return self.result


def _stored(expires_in_hours: float, refresh_token="refresh-A") -> str:
    return json.dumps({
        "accessToken": "access-old",
        "refreshToken": refresh_token,
        "expiresAt": int((NOW + timedelta(hours=expires_in_hours)).timestamp() * 1000),
    })


def _refresher(store, endpoint, log=None):
    return CredentialRefresher(store, endpoint, logger=log or _Log(), now=lambda: NOW)


# -- parsing ---------------------------------------------------------------

def test_reads_the_shape_claude_code_keeps_in_the_keychain():
    c = parse_credential(_stored(5), now=NOW)
    assert c.refresh_token == "refresh-A"
    assert c.expires_at == NOW + timedelta(hours=5)


def test_reads_the_snake_case_shape_the_endpoint_returns():
    c = parse_credential(json.dumps({
        "access_token": "a", "refresh_token": "r", "expires_in": 3600}), now=NOW)
    assert c.expires_at == NOW + timedelta(hours=1)


def test_epoch_milliseconds_are_not_read_as_seconds():
    """Claude Code stores milliseconds; reading them as seconds lands in 1970
    and makes every credential look permanently expired."""
    c = parse_credential(_stored(5), now=NOW)
    assert c.expires_at.year == 2026


def test_a_credential_with_no_refresh_token_is_refused():
    with pytest.raises(CredentialError):
        parse_credential(json.dumps({"accessToken": "a"}), now=NOW)


# -- the ordering invariant ------------------------------------------------

def test_the_rotated_refresh_token_is_persisted_before_the_access_token():
    """The whole point. Reverse this order and a crash locks the tenant out."""
    store = _Store({REFRESH_SECRET: _stored(1)})
    ep = _Endpoint({"access_token": "access-new", "refresh_token": "refresh-B",
                    "expires_in": 28800})
    _refresher(store, ep).refresh_tenant("eng", "anthropic")
    assert store.writes.index(REFRESH_SECRET) < store.writes.index(BASE)


def test_a_crash_after_persisting_refresh_leaves_the_tenant_recoverable():
    store = _Store({REFRESH_SECRET: _stored(1)}, fail_on=BASE)
    ep = _Endpoint({"access_token": "access-new", "refresh_token": "refresh-B",
                    "expires_in": 28800})
    log = _Log()
    with pytest.raises(RuntimeError):
        _refresher(store, ep, log).refresh_tenant("eng", "anthropic")
    # The new refresh token survived, so the next sweep can still refresh.
    assert parse_credential(store.data[REFRESH_SECRET], now=NOW).refresh_token == "refresh-B"


# -- rotation --------------------------------------------------------------

def test_an_endpoint_that_does_not_rotate_keeps_the_existing_refresh_token():
    """Dropping it because the response omitted it would strand the tenant."""
    c = Credential("old", "refresh-A", NOW)
    out = refresh(c, _Endpoint({"access_token": "new", "expires_in": 3600}), now=NOW)
    assert out.refresh_token == "refresh-A"


def test_a_rotated_refresh_token_replaces_the_old_one():
    c = Credential("old", "refresh-A", NOW)
    out = refresh(c, _Endpoint({"access_token": "new", "refresh_token": "refresh-B",
                                "expires_in": 3600}), now=NOW)
    assert out.refresh_token == "refresh-B"


# -- when not to act -------------------------------------------------------

def test_a_credential_far_from_expiry_is_left_alone():
    store = _Store({REFRESH_SECRET: _stored(10)})
    ep = _Endpoint({"access_token": "x"})
    out = _refresher(store, ep).refresh_tenant("eng", "anthropic")
    assert out.refreshed is False and out.reason == "still_valid"
    assert ep.calls == [] and store.writes == []


def test_a_tenant_with_no_refresh_secret_is_not_an_error():
    """The normal case for an API-key or setup-token tenant."""
    out = _refresher(_Store({}), _Endpoint({})).refresh_tenant("eng", "anthropic")
    assert out.refreshed is False and out.reason == "no_refresh_credential"


def test_invalid_grant_stops_rather_than_retrying():
    store = _Store({REFRESH_SECRET: _stored(1)})
    ep = _Endpoint(raises=ReauthRequired("rejected"))
    log = _Log()
    out = _refresher(store, ep, log).refresh_tenant("eng", "anthropic")
    assert out.reason == "reauth_required"
    assert store.writes == []
    assert any(lvl == "error" for lvl, _, _ in log.records)


def test_a_transient_failure_is_retried_next_sweep_not_escalated():
    store = _Store({REFRESH_SECRET: _stored(1)})
    out = _refresher(store, _Endpoint(raises=CredentialError("503"))).refresh_tenant(
        "eng", "anthropic")
    assert out.reason == "refresh_failed" and store.writes == []


def test_one_bad_tenant_does_not_stop_the_sweep():
    store = _Store({REFRESH_SECRET: _stored(1),
                    "swarm-tenant-ops-anthropic" + REFRESH_SUFFIX: _stored(1)})
    ep = _Endpoint({"access_token": "new", "expires_in": 3600})
    outs = _refresher(store, ep).sweep([("eng", "anthropic"), ("ops", "anthropic")])
    assert len(outs) == 2 and all(o.refreshed for o in outs)


# -- never leak ------------------------------------------------------------

def test_logs_and_outcomes_never_carry_token_material():
    store = _Store({REFRESH_SECRET: _stored(1)})
    ep = _Endpoint({"access_token": "SUPERSECRET", "refresh_token": "ALSOSECRET",
                    "expires_in": 3600})
    log = _Log()
    out = _refresher(store, ep, log).refresh_tenant("eng", "anthropic")
    blob = json.dumps([out.as_dict(), [(l, m, {k: str(v) for k, v in kw.items()})
                                       for l, m, kw in log.records]])
    assert "SUPERSECRET" not in blob and "ALSOSECRET" not in blob


def test_serialise_round_trips():
    c = Credential("a", "r", NOW + timedelta(hours=4))
    assert parse_credential(serialise(c), now=NOW).refresh_token == "r"
