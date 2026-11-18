"""One cluster, two clients: the BEARER TOKEN the scheduler and the reconciler send.

A GKE client authenticates with a Google OAuth access token, and that token
expires. Both services build their kubernetes client exactly once -- the
reconciler keeps its `Reconciler` in `app.state`, the scheduler keeps its
`Scheduler` there -- and Cloud Run keeps an instance warm for as long as its
Cloud Scheduler tick keeps arriving. So a token obtained while the client is
being BUILT is the token every later request carries, for the whole life of the
process.

That is what happened. swarm-reconciler instance 00a41e8c started at
2026-09-22T00:18:03Z and minted its token on its first pass at 00:20:24Z; from
00:55:08Z every GKE list returned 401 Unauthorized, for 3h50m and 40 consecutive
passes, on that same process. The reconciler then -- correctly -- skipped every
GKE finding, so no GKE workload was reconciled at all: a stuck browser-profile
task would have held its lease and its capacity indefinitely.

These tests pin the construction, which is the part a unit test can reach: that
the token is obtained when the REQUEST is built and not when the client is, that
a token past its expiry is never sent, and that the two copies of the fix agree.
The complementary property -- that a 401 is reported as "backend unavailable"
and never as an empty cluster -- is asserted in
tests/unit/worker/test_reconciler_safety.py, because a failed read rendered as
an empty result is the failure mode this platform keeps producing.

This file deliberately imports BOTH services, for the same reason
test_gke_client_host.py does: they ship as separate images sharing only the
frozen `swarm_common` package, so `install_google_bearer_token` exists twice on
purpose and the equality asserted here is the only thing holding the copies
together.
"""

from __future__ import annotations

import base64
from datetime import timedelta

import google.auth._helpers as google_auth_helpers
import google.auth.credentials
import pytest

from reconciler.backends import GkeBackend
from reconciler.backends import google_bearer_token as reconciler_bearer_token
from reconciler.backends import (
    install_google_bearer_token as reconciler_install_bearer_token,
)
from scheduler.dispatch import GkeJobDispatcher, GkeTarget
from scheduler.dispatch import google_bearer_token as scheduler_bearer_token
from scheduler.dispatch import (
    install_google_bearer_token as scheduler_install_bearer_token,
)

from .conftest import scheduler_settings

BARE_ENDPOINT = "10.0.0.1"

#: Enough of a PEM to be base64-decoded and written to disk. Nothing here parses
#: it; no client is ever asked to open a connection.
CA_PEM = b"-----BEGIN CERTIFICATE-----\nnot-a-real-ca\n-----END CERTIFICATE-----\n"
CA_PEM_B64 = base64.b64encode(CA_PEM).decode()

#: What Cloud Run's metadata server actually hands back. It caches the instance's
#: token and returns whatever is left of it, so a first mint routinely arrives
#: with well under an hour of life -- the reconciler's died 35 minutes in.
METADATA_TOKEN_LIFETIME = timedelta(minutes=35)


class ScriptedCredentials(google.auth.credentials.Credentials):
    """google.auth credentials with a scripted token and no network.

    Deliberately a SUBCLASS of the real `Credentials` rather than a stand-in, so
    `valid`, `expired` and google.auth's own refresh threshold are the library's
    real implementations. A hand-rolled `valid` could agree with the fix while
    disagreeing with google.auth, and the test would pass while production 401s.

    Each refresh mints the next token in sequence, so a test can say which mint a
    request carried. Nothing here ever reaches a metadata server.
    """

    def __init__(self, *, lifetime: timedelta = METADATA_TOKEN_LIFETIME) -> None:
        super().__init__()
        self._lifetime = lifetime
        self.refreshes = 0

    def refresh(self, request: object) -> None:  # noqa: D102 - google.auth's interface
        self.refreshes += 1
        self.token = f"access-token-{self.refreshes}"
        self.expiry = google_auth_helpers.utcnow() + self._lifetime


class NeverMintsCredentials(google.auth.credentials.Credentials):
    """Credentials whose refresh succeeds without producing a token.

    Not hypothetical: `google.auth.default()` resolves happily in environments
    where the token endpoint then returns nothing useful, and a Configuration
    seeded with `Bearer None` is a 401 whose cause is invisible.
    """

    def refresh(self, request: object) -> None:  # noqa: D102 - google.auth's interface
        self.token = None


def expire(credentials: google.auth.credentials.Credentials) -> None:
    """Move the credential's expiry into the past.

    Cheaper and less fragile than moving the clock: `Credentials.expired`
    compares `expiry` against `google.auth._helpers.utcnow()`, so a past expiry
    is exactly the state a token acquires by sitting in a warm instance.
    """
    credentials.expiry = google_auth_helpers.utcnow() - timedelta(seconds=1)


def authorization_header(api_client: object) -> str:
    """The Authorization header this client would put on its NEXT request.

    Driven through the kubernetes client's real auth path --
    `update_params_for_auth`, which is what `ApiClient.__call_api` calls on every
    request -- rather than by reading `configuration.api_key`. The question under
    test is precisely whether the token is obtained when a request is built, so
    the test has to go through the code that builds one.
    """
    headers: dict[str, str] = {}
    api_client.update_params_for_auth(headers, {}, ["BearerToken"])
    return headers["authorization"]


@pytest.fixture
def offline_google_auth(monkeypatch):
    """`google.auth.default()` returning scripted credentials, and no network.

    The construction under test is the real one: it really calls
    `google.auth.default()`, really builds a kubernetes `Configuration` and
    really installs the refresh hook. Only the credential source is replaced, so
    this runs with no cloud credentials and no metadata server.
    """
    import google.auth
    import google.auth.transport.requests

    credentials = ScriptedCredentials()
    monkeypatch.setattr(
        google.auth, "default", lambda **kwargs: (credentials, "test-project")
    )
    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )
    return credentials


@pytest.fixture
def terraform_environment(monkeypatch):
    """Exactly the variables terraform/infra/locals.tf sets for both services."""
    monkeypatch.setenv("GKE_ENDPOINT", BARE_ENDPOINT)
    monkeypatch.setenv("GKE_CA_CERT_B64", CA_PEM_B64)
    monkeypatch.delenv("GKE_CA_CERT_PATH", raising=False)


def reconciler_client(credentials, terraform_env_applied) -> object:
    backend = GkeBackend()
    backend._configure()
    return backend._batch.api_client


def scheduler_client(credentials, tmp_path) -> object:
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(CA_PEM)
    dispatcher = GkeJobDispatcher(
        scheduler_settings(), target=GkeTarget(BARE_ENDPOINT, str(ca_file))
    )
    return dispatcher._api().api_client


# -- the token is obtained per request, not per client ----------------------

def test_the_reconciler_mints_a_new_token_once_the_old_one_expires(
    offline_google_auth, terraform_environment
):
    """The bug, stated as a test: one client, two requests, two tokens.

    Before the fix `_configure` minted once and returned early on every later
    pass, so the second header below was byte-identical to the first and the
    cluster answered 401 for as long as the instance lived.
    """
    credentials = offline_google_auth
    api_client = reconciler_client(credentials, terraform_environment)

    first = authorization_header(api_client)
    assert first == "Bearer access-token-1"

    expire(credentials)
    second = authorization_header(api_client)

    assert second == "Bearer access-token-2"
    assert credentials.refreshes == 2


def test_the_scheduler_mints_a_new_token_once_the_old_one_expires(
    offline_google_auth, tmp_path
):
    """The identical construction lived on the dispatch side.

    Here the cost is a browser-profile task that can never be dispatched from a
    warm instance. The lease does come back -- `GkeJobDispatcher.dispatch` wraps
    the ApiException into a DispatchError -- so this leaks no capacity, but the
    task never starts.
    """
    credentials = offline_google_auth
    api_client = scheduler_client(credentials, tmp_path)

    assert authorization_header(api_client) == "Bearer access-token-1"

    expire(credentials)

    assert authorization_header(api_client) == "Bearer access-token-2"
    assert credentials.refreshes == 2


@pytest.mark.parametrize(
    "build_client", [reconciler_client, scheduler_client], ids=["reconciler", "scheduler"]
)
def test_an_expired_token_is_never_the_one_sent(
    build_client, offline_google_auth, terraform_environment, tmp_path
):
    """Not "eventually refreshed" -- never sent.

    A 401 from the Kubernetes API is indistinguishable from an absent or
    malformed bearer, so a single request carrying a dead token costs a whole
    reconciliation pass and reads in the log as an IAM problem.
    """
    credentials = offline_google_auth
    argument = terraform_environment if build_client is reconciler_client else tmp_path
    api_client = build_client(credentials, argument)

    dead_tokens = []
    for _ in range(4):
        dead_tokens.append(credentials.token)
        expire(credentials)
        assert authorization_header(api_client) not in {
            f"Bearer {token}" for token in dead_tokens
        }


@pytest.mark.parametrize(
    "build_client", [reconciler_client, scheduler_client], ids=["reconciler", "scheduler"]
)
def test_a_token_with_life_left_is_reused(
    build_client, offline_google_auth, terraform_environment, tmp_path
):
    """The other half of correct: do not mint one per request.

    `Credentials.refresh` on Cloud Run is a metadata-server round trip. Minting
    per request would put one in front of every Kubernetes call the reconciler
    makes -- and it lists jobs, namespaces and pods on every pass -- so the fix
    has to respect google.auth's own answer to "is this still good".
    """
    credentials = offline_google_auth
    argument = terraform_environment if build_client is reconciler_client else tmp_path
    api_client = build_client(credentials, argument)

    headers = {authorization_header(api_client) for _ in range(5)}

    assert headers == {"Bearer access-token-1"}
    assert credentials.refreshes == 1


# -- the trap the seeding exists to avoid -----------------------------------

@pytest.mark.parametrize(
    "build_client", [reconciler_client, scheduler_client], ids=["reconciler", "scheduler"]
)
def test_the_configuration_actually_offers_a_bearer_token(
    build_client, offline_google_auth, terraform_environment, tmp_path
):
    """`auth_settings()` is gated on `api_key` already holding "authorization".

    A Configuration carrying only the refresh hook advertises no bearer token at
    all, so the client sends NO Authorization header -- and an unauthenticated
    request to the Kubernetes API gets the same 401 this whole change is about.
    Hooking without seeding would look like a fix and change nothing.
    """
    credentials = offline_google_auth
    argument = terraform_environment if build_client is reconciler_client else tmp_path
    api_client = build_client(credentials, argument)

    assert "BearerToken" in api_client.configuration.auth_settings()
    assert api_client.configuration.refresh_api_key_hook is not None


# -- the two copies agree ---------------------------------------------------

@pytest.mark.parametrize(
    "install",
    [reconciler_install_bearer_token, scheduler_install_bearer_token],
    ids=["reconciler", "scheduler"],
)
def test_both_copies_produce_the_same_sequence_of_headers(install, monkeypatch):
    """`install_google_bearer_token` exists twice; it must behave once.

    Same reasoning as `gke_api_host`: two images, one shared frozen package,
    nowhere in-image for a single copy. This is what stops one side being fixed
    while the other keeps a stale token -- which is exactly how the missing
    https:// scheme survived on the scheduler after the reconciler gained it.
    """
    import google.auth.transport.requests
    from kubernetes import client as k8s

    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )
    credentials = ScriptedCredentials()
    configuration = k8s.Configuration()
    install(configuration, credentials)

    sequence = []
    for _ in range(3):
        sequence.append(configuration.auth_settings()["BearerToken"]["value"])
        expire(credentials)
    sequence.append(configuration.auth_settings()["BearerToken"]["value"])

    assert sequence == [
        "Bearer access-token-1",
        "Bearer access-token-2",
        "Bearer access-token-3",
        "Bearer access-token-4",
    ]


@pytest.mark.parametrize(
    "install",
    [reconciler_install_bearer_token, scheduler_install_bearer_token],
    ids=["reconciler", "scheduler"],
)
def test_a_client_that_cannot_refresh_per_request_is_refused_outright(install, monkeypatch):
    """Setting an attribute nothing reads is the quietest way to unfix this.

    `refresh_api_key_hook` is the whole mechanism, and a Configuration without
    it would accept the assignment, never call it, and serve the first token for
    the life of the process -- back to the 401 with nothing to show for it. Both
    images resolve `kubernetes>=30` at build time rather than from a lock, and
    this repository has already lost a working check to a client version that
    silently ignored what it was handed (kubectl 1.22 and manifest fields,
    CLAUDE.md).
    """
    import google.auth.transport.requests

    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )

    class ConfigurationWithoutTheHook:
        """A Configuration from a client version that predates the hook."""

        __slots__ = ("api_key",)

        def __init__(self) -> None:
            self.api_key = {}

    with pytest.raises(RuntimeError) as failure:
        install(ConfigurationWithoutTheHook(), ScriptedCredentials())

    assert "refresh_api_key_hook" in str(failure.value)


@pytest.mark.parametrize(
    "bearer_token",
    [reconciler_bearer_token, scheduler_bearer_token],
    ids=["reconciler", "scheduler"],
)
def test_credentials_that_cannot_state_an_expiry_are_refreshed_every_call(
    bearer_token, monkeypatch
):
    """`Credentials.expired` is False when `expiry` is None -- "never expires".

    That is the right default for a static token and the wrong one for anything
    minted, so an unknown expiry is treated as "mint again". One extra metadata
    round trip is cheaper than one reconciliation pass lost to a dead token.
    """
    import google.auth.transport.requests

    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )

    class NoExpiryCredentials(ScriptedCredentials):
        def refresh(self, request: object) -> None:
            super().refresh(request)
            self.expiry = None

    credentials = NoExpiryCredentials()

    assert bearer_token(credentials) == "access-token-1"
    assert bearer_token(credentials) == "access-token-2"
    assert credentials.refreshes == 2


@pytest.mark.parametrize(
    "bearer_token",
    [reconciler_bearer_token, scheduler_bearer_token],
    ids=["reconciler", "scheduler"],
)
def test_a_missing_token_fails_loudly_and_prints_nothing(bearer_token, monkeypatch):
    """`Bearer None` is a 401 with no cause attached; say so instead.

    And say it without the credential: this message reaches the reconciliation
    report, which the API serves to operators, and `task.last_error`, which the
    API returns to the tenant verbatim.
    """
    import google.auth.transport.requests

    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )

    with pytest.raises(RuntimeError) as failure:
        bearer_token(NeverMintsCredentials())

    message = str(failure.value)
    assert "no access token" in message
    assert "Bearer" not in message
