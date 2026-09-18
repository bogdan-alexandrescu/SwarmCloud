"""Subscription credentials are refreshed by exactly one writer, safely.

The hazard this guards: refresh tokens ROTATE. If the access token were
published before the rotated refresh token were persisted, a crash between the
two would leave the tenant with credentials that work now and no way to obtain
any more -- locked out until a human re-authenticates. And if workers refreshed
concurrently, each would invalidate the others.
"""

from __future__ import annotations

import itertools
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.logging_setup import CloudLoggingFormatter

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


class _Capture(logging.Handler):
    """A REAL logging handler behind a REAL logging.Logger.

    Deliberately not a duck-typed fake. A fake that accepts `**kwargs` makes
    structlog-style calls -- `log.info("x", tenant_id=t)` -- pass here and raise
    TypeError in production, where the logger is a plain logging.Logger. The
    same fake also hides a reserved `extra=` key, which stdlib rejects with
    "Attempt to overwrite 'x' in LogRecord" only when a real record is built.
    Both are exactly the seam this module cannot afford: it runs on a timer,
    and a crash inside it is a tenant quietly losing its credential.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []
        self._formatter = CloudLoggingFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self._formatter.format(record))


_LOGGER_SEQ = itertools.count()


class _Log:
    """Adapter so a test can say `log.records` and get what was really emitted."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(f"test.credentials.{next(_LOGGER_SEQ)}")
        self._logger.handlers = []
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)
        self._capture = _Capture()
        self._logger.addHandler(self._capture)

    @property
    def records(self) -> list[str]:
        return self._capture.records

    def __getattr__(self, name: str):
        return getattr(self._logger, name)


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

def test_a_credential_far_from_expiry_is_not_exchanged():
    """The property is about the ENDPOINT, not about writing nothing.

    This test used to seed only the refresh secret and assert `writes == []`,
    which quietly encoded the onboarding bug: with the worker-facing secret
    absent, writing nothing is exactly the failure. Seeded as already published,
    so what it asserts is what it means -- a valid token is not spent.
    """
    store = _Store({REFRESH_SECRET: _stored(10), "swarm-tenant-eng-anthropic": "access-old"})
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
    assert any(json.loads(r)["severity"] == "ERROR" for r in log.records)


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
    blob = json.dumps([out.as_dict(), log.records])
    assert "SUPERSECRET" not in blob and "ALSOSECRET" not in blob
    assert log.records, "the refresh path must say something, or nothing is observable"


def test_serialise_round_trips():
    c = Credential("a", "r", NOW + timedelta(hours=4))
    assert parse_credential(serialise(c), now=NOW).refresh_token == "r"


def test_the_keychain_item_can_be_pasted_verbatim():
    """`security find-generic-password -s 'Claude Code-credentials' -w` returns
    a `claudeAiOauth` wrapper. Requiring the operator to unwrap it by hand puts
    a jq expression between them and a working tenant, and gets it wrong once."""
    payload = json.dumps({
        "claudeAiOauth": {
            "accessToken": "access-x",
            "refreshToken": "refresh-x",
            "expiresAt": int((NOW + timedelta(hours=5)).timestamp() * 1000),
            "scopes": ["user:inference"],
        }
    })
    credential = parse_credential(payload, now=NOW)
    assert credential.refresh_token == "refresh-x"
    assert credential.expires_at == NOW + timedelta(hours=5)


def test_an_access_token_pasted_on_its_own_is_refused_with_a_usable_message():
    """The likeliest wrong paste. It must not be stored as if it were valid."""
    with pytest.raises(CredentialError) as exc:
        parse_credential("sk-ant-oat01-notjson", now=NOW)
    assert "refreshToken" in str(exc.value)


# -- onboarding: valid is not the same as published -------------------------

def test_a_freshly_onboarded_credential_is_published_immediately():
    """The bug an adversarial review found and reality was about to confirm.

    An operator onboards a credential minted minutes ago. Its access token has
    hours left, so `needs_refresh` is false and the refresher used to return
    without writing anything -- leaving the secret the worker actually mounts
    with NO version, for as long as the credential stayed fresh. Every task for
    that tenant then failed on a missing secret, caused by a credential that was
    working perfectly.
    """
    base = "swarm-tenant-eng-anthropic"
    store = _Store({f"{base}{REFRESH_SUFFIX}": _stored(8)})   # 8h of life left
    ep = _Endpoint({"access_token": "unused"})

    out = _refresher(store, ep).refresh_tenant("eng", "anthropic")

    assert out.reason == "published"
    assert ep.calls == [], "the token was valid; exchanging it would waste it"
    assert store.writes == [base], "only the worker-facing half needed writing"
    assert store.data[base] == "access-old"


def test_a_token_already_published_is_not_written_again():
    """Writing every sweep would add a secret version every five minutes --
    288 a day of identical values."""
    base = "swarm-tenant-eng-anthropic"
    store = _Store({
        f"{base}{REFRESH_SUFFIX}": _stored(8),
        base: "access-old",
    })

    out = _refresher(store, _Endpoint()).refresh_tenant("eng", "anthropic")

    assert out.reason == "still_valid"
    assert store.writes == []


def test_an_unreadable_base_secret_is_treated_as_needing_the_token():
    """Publishing one the secret may already have costs a redundant version.
    NOT publishing one it lacks costs every task for that tenant. The uncertain
    case takes the cheap mistake."""
    base = "swarm-tenant-eng-anthropic"

    class _Unreadable(_Store):
        def access(self, name):
            if name == base:
                raise RuntimeError("permission denied")
            return super().access(name)

    store = _Unreadable({f"{base}{REFRESH_SUFFIX}": _stored(8)})
    out = _refresher(store, _Endpoint()).refresh_tenant("eng", "anthropic")

    assert out.reason == "published"
    assert store.writes == [base]


# -- the wire format --------------------------------------------------------

def test_the_token_request_is_form_encoded_to_the_right_host():
    """Both of these were wrong and both produced a 403 in production.

    JSON to an OAuth token endpoint is answered with 403 -- a status that reads
    like an authorization failure and is really a content-type one. And
    console.anthropic.com refuses a refresh that platform.claude.com accepts.
    Verified against claudeswitch, which does this successfully in production.
    """
    import urllib.request
    from quota_broker.oauth import TOKEN_ENDPOINT, HttpTokenEndpoint

    assert TOKEN_ENDPOINT == "https://platform.claude.com/v1/oauth/token"

    captured = {}

    class _Response:
        def read(self):
            return b'{"access_token":"a","expires_in":3600}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        captured["content_type"] = request.headers.get("Content-type")
        captured["user_agent"] = request.headers.get("User-agent", "")
        return _Response()

    original = urllib.request.urlopen
    urllib.request.urlopen = _urlopen
    try:
        HttpTokenEndpoint().exchange("my-refresh-token")
    finally:
        urllib.request.urlopen = original

    assert captured["url"] == "https://platform.claude.com/v1/oauth/token"
    assert captured["content_type"] == "application/x-www-form-urlencoded"
    # The WAF refuses unrecognised clients with 403. urllib's default
    # User-Agent is Python-urllib, which is refused; measured from one machine,
    # same second: no UA -> 429, Python-urllib -> 403, claude-cli -> 400.
    assert captured["user_agent"].startswith("claude-cli/")
    assert "grant_type=refresh_token" in captured["body"]
    assert "refresh_token=my-refresh-token" in captured["body"]
    assert "client_id=" in captured["body"]
    # Not JSON, which is the mistake this guards.
    assert not captured["body"].startswith("{")
