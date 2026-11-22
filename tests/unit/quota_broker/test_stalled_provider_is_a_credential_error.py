"""A provider that accepts and then stalls is the module's own error (issue 53).

urllib wraps only the SEND in `URLError`. A timeout while the answer is read is
a bare `TimeoutError`, an `OSError` that is not a `URLError`, so it went past
every `except URLError` here: `finish_account_authorization` catches only
`CredentialError` and answered a bare 500, and the refresh sweep logged the
generic `error` outcome where a transient failure is `refresh_failed`.

Every server is a socket on 127.0.0.1 that is listening and never accepted
from: the kernel completes the handshake, the request is sent, the answer
never comes. The control is a port nobody listens on, which is refused and
was always a `URLError`. `urlopen` is wrapped only to shorten its timeout.
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import socket
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker import oauth, openai_plan
from quota_broker.credentials import REFRESH_SUFFIX, CredentialRefresher
from quota_broker.oauth import CredentialError, HttpTokenEndpoint, ReauthRequired
from quota_broker.openai_plan import HttpPlanTokenEndpoint
from quota_broker.usage import UsageUnavailable

#: Seconds; long enough for a loopback answer, short enough to keep the file quick.
SHORT = 0.5


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    real = urllib.request.urlopen

    def urlopen(request, timeout=None):  # noqa: ARG001
        return real(request, timeout=SHORT)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


@contextlib.contextmanager
def _silent() -> Iterator[str]:
    """The base URL of a port that accepts connections and never answers."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


def _refused() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def _redeem(endpoint) -> None:
    endpoint.redeem(
        code=secrets.token_hex(8),
        verifier=secrets.token_hex(16),
        redirect_uri="https://example.test/callback",
        state=secrets.token_hex(16),
    )


# -- oauth.py ---------------------------------------------------------------------
@pytest.mark.parametrize("base", [_silent, None], ids=["stalled", "refused"])
def test_redeem_is_a_credential_error_naming_the_token_endpoint(base):
    with (base() if base else contextlib.nullcontext(_refused())) as url:
        with pytest.raises(CredentialError, match="could not reach the token endpoint") as caught:
            _redeem(HttpTokenEndpoint(url=f"{url}/v1/oauth/token"))
    # MUTATION: no OSError branch -- a bare TimeoutError escapes pytest.raises.
    assert not isinstance(caught.value, ReauthRequired)


@pytest.mark.parametrize("base", [_silent, None], ids=["stalled", "refused"])
def test_exchange_is_a_transient_credential_error_naming_the_token_endpoint(base):
    with (base() if base else contextlib.nullcontext(_refused())) as url:
        with pytest.raises(CredentialError, match="could not reach the token endpoint") as caught:
            HttpTokenEndpoint(url=f"{url}/v1/oauth/token").exchange(secrets.token_hex(16))
    # Transient, not terminal: a stall must never ask a person to sign in again.
    assert not isinstance(caught.value, ReauthRequired)


@pytest.mark.parametrize("base", [_silent, None], ids=["stalled", "refused"])
def test_profile_is_a_credential_error_naming_the_profile_endpoint(base, monkeypatch):
    with (base() if base else contextlib.nullcontext(_refused())) as url:
        monkeypatch.setattr(oauth, "PROFILE_ENDPOINT", f"{url}/api/oauth/profile")
        with pytest.raises(CredentialError, match="could not reach the profile endpoint"):
            HttpTokenEndpoint().profile(secrets.token_hex(16))


# -- openai_plan.py ------------------------------------------------------------------
@pytest.mark.parametrize("base", [_silent, None], ids=["stalled", "refused"])
def test_plan_exchange_is_a_credential_error_naming_the_chatgpt_token_endpoint(base):
    with (base() if base else contextlib.nullcontext(_refused())) as url:
        with pytest.raises(CredentialError, match="could not reach the ChatGPT token endpoint") as caught:
            HttpPlanTokenEndpoint(url=f"{url}/oauth/token").exchange(secrets.token_hex(16))
    assert not isinstance(caught.value, ReauthRequired)


@pytest.mark.parametrize("base", [_silent, None], ids=["stalled", "refused"])
def test_device_sign_in_is_a_credential_error_naming_it(base, monkeypatch):
    with (base() if base else contextlib.nullcontext(_refused())) as url:
        monkeypatch.setattr(openai_plan, "DEVICE_USERCODE_ENDPOINT", f"{url}/deviceauth/usercode")
        with pytest.raises(CredentialError, match="could not reach the ChatGPT device sign-in"):
            HttpPlanTokenEndpoint().start_device()


@pytest.mark.parametrize("base", [_silent, None], ids=["stalled", "refused"])
def test_plan_usage_is_usage_unavailable_naming_the_route(base, monkeypatch):
    published = json.dumps({"access_token": secrets.token_hex(16), "account_id": "acct-1"})
    with (base() if base else contextlib.nullcontext(_refused())) as url:
        monkeypatch.setattr(openai_plan, "USAGE_ENDPOINT", f"{url}/backend-api/wham/usage")
        with pytest.raises(UsageUnavailable, match="could not reach the plan's usage route"):
            openai_plan.fetch_usage(published, timeout_seconds=SHORT)


# -- the refresh sweep -------------------------------------------------------------
class _Store:
    def __init__(self, data: dict[str, str]) -> None:
        self.data = dict(data)

    def access(self, name: str) -> str:
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def add_version(self, name: str, payload: str) -> None:
        self.data[name] = payload


def test_a_stalled_token_endpoint_in_the_sweep_is_refresh_failed_not_error():
    now = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
    stored = json.dumps({
        "accessToken": secrets.token_hex(16),
        "refreshToken": secrets.token_hex(16),
        "expiresAt": int((now + timedelta(minutes=30)).timestamp() * 1000),
    })
    store = _Store({"swarm-tenant-eng-anthropic" + REFRESH_SUFFIX: stored})
    logger = logging.getLogger("test.stalled_provider.sweep")
    with _silent() as url:
        refresher = CredentialRefresher(
            store, HttpTokenEndpoint(url=f"{url}/v1/oauth/token"), logger=logger, now=lambda: now
        )
        outcomes = refresher.sweep([("eng", "anthropic")])
    # MUTATION: no OSError branch in exchange -- the sweep's catch-all says "error".
    assert [o.reason for o in outcomes] == ["refresh_failed"]
