"""The refresh sweep leaves an account alone while an agent holds it (#626).

Refreshing a pool account's OAuth credential REVOKES the access token issued
before it. The sweep refreshes every account three hours before its token
expires, whether or not an agent is on it -- so a running agent had its token
pulled out from under it, failed with a refused credential and restarted in
place: 18 claude-code and 8 codex attempts, about 169 agent-minutes, in the
2026-10-05 history analysis.

Pinned here:

  * a HELD account whose token is inside the refresh window but outside the
    held margin is not exchanged: no endpoint call, no secret written, and the
    outcome says why (`held_deferred`) with the token's expiry;
  * a held account whose token expires within the margin IS refreshed -- an
    expired token stops the agent just as surely as a revoked one;
  * an account nobody holds is refreshed exactly as before (the "no in-use
    filter" property of `due_for_refresh` survives: an idle account is never
    skipped, so it cannot rot);
  * `_sweep_account_pool` tells the refresher which accounts hold a LIVE hold
    (an expired hold is no agent);
  * the margin is a setting, and a value that is not a duration is refused.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import REFRESH_REASONS, Account, Hold
from quota_broker.credentials import (
    DEFAULT_HELD_REFRESH_MARGIN,
    REFRESH_SUFFIX,
    CredentialRefresher,
)
from quota_broker.main import _sweep_account_pool
from quota_broker.settings import held_refresh_margin

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.writes: list[str] = []

    def access(self, name):
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def add_version(self, name, payload):
        self.writes.append(name)
        self.data[name] = payload


class _Endpoint:
    def __init__(self):
        self.calls: list[str] = []

    def exchange(self, refresh_token):
        self.calls.append(refresh_token)
        return {"access_token": "fresh-" + "a" * 8, "refresh_token": "r2", "expires_in": 28800}


def _stored(expires_in: timedelta) -> str:
    return json.dumps({
        "accessToken": "current-" + "b" * 8,
        "refreshToken": "r1",
        "expiresAt": int((NOW + expires_in).timestamp() * 1000),
    })


def _refresher(store, endpoint, **kw):
    return CredentialRefresher(
        store, endpoint, logger=logging.getLogger("test.held"), now=lambda: NOW, **kw
    )


BASE = "swarm-account-eng--busy"


def test_a_held_account_inside_the_window_but_outside_the_margin_is_not_refreshed():
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": _stored(timedelta(hours=2)), BASE: "x"})
    ep = _Endpoint()

    out = _refresher(store, ep).sweep_accounts([(BASE, "eng:busy")], held={"eng:busy"})

    assert ep.calls == [], "exchanging the refresh token revokes the agent's access token"
    assert store.writes == []
    assert out[0].refreshed is False
    assert out[0].reason == "held_deferred"
    assert out[0].expires_at == NOW + timedelta(hours=2)


def test_a_held_account_near_expiry_is_refreshed():
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": _stored(timedelta(minutes=10))})
    ep = _Endpoint()

    out = _refresher(store, ep).sweep_accounts([(BASE, "eng:busy")], held={"eng:busy"})

    assert ep.calls == ["r1"]
    assert out[0].reason == "refreshed"


def test_an_unheld_account_is_refreshed_as_before():
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": _stored(timedelta(hours=2))})
    ep = _Endpoint()

    out = _refresher(store, ep).sweep_accounts([(BASE, "eng:busy")])

    assert ep.calls == ["r1"]
    assert out[0].reason == "refreshed"


def test_the_margin_is_the_refreshers_setting():
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": _stored(timedelta(hours=2))})
    ep = _Endpoint()

    out = _refresher(store, ep, held_margin=timedelta(hours=2, minutes=30)).sweep_accounts(
        [(BASE, "eng:busy")], held={"eng:busy"}
    )

    assert out[0].reason == "refreshed"


def test_held_deferred_is_a_recorded_reason():
    assert "held_deferred" in REFRESH_REASONS


class _AccountStore:
    """What `_sweep_account_pool` reads and writes, and nothing else."""

    def __init__(self, accounts):
        self.accounts = {a.account_id: a for a in accounts}
        self.recorded: dict[str, str] = {}

    def list(self):
        return list(self.accounts.values())

    def get(self, account_id):
        return self.accounts.get(account_id)

    def secret_for(self, account):
        return f"swarm-account-{account.owner_tenant}--{account.label}"

    def record_refresh(self, account_id, *, at, reason, expires_at=None, **_):
        self.recorded[account_id] = reason
        return True


def _account(label, holds=()):
    return Account(account_id=f"eng:{label}", owner_tenant="eng", label=label,
                   holds=tuple(holds), assigned=len(holds))


def test_the_pool_sweep_defers_only_accounts_with_a_live_hold():
    live = Hold(assignment_id="asg-1", tenant_id="eng", expires_at=NOW + timedelta(minutes=20))
    expired = Hold(assignment_id="asg-2", tenant_id="eng", expires_at=NOW - timedelta(minutes=1))
    accounts = _AccountStore([
        _account("busy", [live]),
        _account("stale", [expired]),
        _account("idle"),
    ])
    secrets = _Store({
        f"swarm-account-eng--{label}{REFRESH_SUFFIX}": _stored(timedelta(hours=2))
        for label in ("busy", "stale", "idle")
    })
    ep = _Endpoint()

    result = _sweep_account_pool(_refresher(secrets, ep), accounts, NOW)

    assert accounts.recorded == {
        "eng:busy": "held_deferred",
        "eng:stale": "refreshed",
        "eng:idle": "refreshed",
    }
    assert len(ep.calls) == 2
    assert result["held_deferred"] == ["eng:busy"]


def test_the_margin_setting_defaults_and_parses(monkeypatch):
    monkeypatch.delenv("ACCOUNT_HELD_REFRESH_MARGIN_SECONDS", raising=False)
    assert held_refresh_margin() == DEFAULT_HELD_REFRESH_MARGIN
    monkeypatch.setenv("ACCOUNT_HELD_REFRESH_MARGIN_SECONDS", "900")
    assert held_refresh_margin() == timedelta(seconds=900)
    monkeypatch.setenv("ACCOUNT_HELD_REFRESH_MARGIN_SECONDS", "-5")
    with pytest.raises(ValueError):
        held_refresh_margin()
