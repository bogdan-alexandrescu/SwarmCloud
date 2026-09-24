"""One cluster, two clients: the `host` the scheduler and the reconciler build.

GKE_ENDPOINT is `google_container_cluster.endpoint` handed over verbatim by
terraform/infra/locals.tf, and that attribute is a BARE address -- `10.0.0.1`,
no scheme. The kubernetes client concatenates `configuration.host` with the
resource path, so a host without a scheme is a URL urllib3 cannot route: the
scheduler's dispatch fails after the lease is already taken, and the reconciler
goes blind on the GKE backend.

This file deliberately imports BOTH services. They ship as separate images that
share only the frozen `swarm_common` package, so neither can import the other
and `gke_api_host` exists twice on purpose (see the docstring on either copy).
The equality asserted here is the only thing holding those two copies together
-- the check that was missing while they disagreed.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from swarm_common.profiles import Backend

from reconciler.backends import GkeBackend
from reconciler.backends import gke_api_host as reconciler_gke_api_host
from scheduler.dispatch import DispatchError, GkeJobDispatcher, GkeTarget, build_router
from scheduler.dispatch import gke_api_host as scheduler_gke_api_host

from .conftest import scheduler_settings

#: The shape terraform actually produces: an address, nothing else.
BARE_ENDPOINT = "10.0.0.1"
EXPECTED_HOST = "https://10.0.0.1"

#: Enough of a PEM to be base64-decoded and written to disk. Nothing in this
#: test parses it; the client is never asked to open a connection.
CA_PEM = b"-----BEGIN CERTIFICATE-----\nnot-a-real-ca\n-----END CERTIFICATE-----\n"
CA_PEM_B64 = base64.b64encode(CA_PEM).decode()


class _FakeCredentials:
    token = "fake-token-that-never-leaves-this-process"

    def refresh(self, request: object) -> None:
        """google.auth's interface: the caller refreshes before reading .token."""


@pytest.fixture
def offline_google_auth(monkeypatch):
    """google.auth with no credentials and no network.

    The client construction under test is the real one -- it really calls
    google.auth.default() and really builds a kubernetes Configuration. Only the
    credential source is replaced, so this runs with no cloud credentials.
    """
    import google.auth
    import google.auth.transport.requests

    monkeypatch.setattr(
        google.auth, "default", lambda **kwargs: (_FakeCredentials(), "test-project")
    )
    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda *args, **kwargs: object()
    )


@pytest.fixture
def terraform_environment(monkeypatch):
    """Exactly the variables terraform/infra/locals.tf sets for both services."""
    monkeypatch.setenv("GKE_ENDPOINT", BARE_ENDPOINT)
    monkeypatch.setenv("GKE_CA_CERT_B64", CA_PEM_B64)
    monkeypatch.delenv("GKE_CA_CERT_PATH", raising=False)


# -- the construction itself ------------------------------------------------

def test_a_bare_endpoint_gains_the_scheme_the_kubernetes_client_needs():
    assert scheduler_gke_api_host(BARE_ENDPOINT) == EXPECTED_HOST
    assert reconciler_gke_api_host(BARE_ENDPOINT) == EXPECTED_HOST


@pytest.mark.parametrize(
    "endpoint",
    [
        BARE_ENDPOINT,
        "  10.0.0.1  ",
        "34.118.229.12",
        "https://10.0.0.1",
        "https://10.0.0.1/",
        "gke-endpoint.example",
        "",
    ],
)
def test_the_two_copies_agree_on_every_input(endpoint):
    assert scheduler_gke_api_host(endpoint) == reconciler_gke_api_host(endpoint)


@pytest.mark.parametrize("endpoint", ["http://10.0.0.1", "ftp://10.0.0.1", "https://"])
def test_the_two_copies_refuse_the_same_endpoints_for_the_same_reason(endpoint):
    with pytest.raises(ValueError) as from_scheduler:
        scheduler_gke_api_host(endpoint)
    with pytest.raises(ValueError) as from_reconciler:
        reconciler_gke_api_host(endpoint)
    assert str(from_scheduler.value) == str(from_reconciler.value)


def test_an_unset_endpoint_is_not_turned_into_a_url():
    """Both callers read "" as "not configured" and must keep reading it that way."""
    assert scheduler_gke_api_host("") == ""
    assert reconciler_gke_api_host("   ") == ""


# -- the shipped paths ------------------------------------------------------

def test_the_scheduler_builds_its_client_against_the_https_url(
    offline_google_auth, tmp_path
):
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(CA_PEM)
    dispatcher = GkeJobDispatcher(
        scheduler_settings(), target=GkeTarget(BARE_ENDPOINT, str(ca_file))
    )

    api = dispatcher._api()

    assert api.api_client.configuration.host == EXPECTED_HOST


def test_terraforms_variables_reach_a_usable_scheduler_client(
    terraform_environment, offline_google_auth
):
    """GKE_CA_CERT_B64, not GKE_CA_CERT_PATH: nothing writes a CA file here."""
    router = build_router(scheduler_settings())

    dispatcher = router.for_backend(Backend.GKE_AUTOPILOT)
    api = dispatcher._api()

    assert api.api_client.configuration.host == EXPECTED_HOST
    assert Path(api.api_client.configuration.ssl_ca_cert).read_bytes() == CA_PEM


def test_scheduler_and_reconciler_address_the_same_cluster_identically(
    terraform_environment, offline_google_auth
):
    """The whole point: one GKE_ENDPOINT, two services, one host string."""
    scheduler_api = build_router(scheduler_settings()).for_backend(
        Backend.GKE_AUTOPILOT
    )._api()

    reconciler_backend = GkeBackend()
    reconciler_backend._configure()

    scheduler_host = scheduler_api.api_client.configuration.host
    reconciler_host = reconciler_backend._batch.api_client.configuration.host
    assert scheduler_host == reconciler_host == EXPECTED_HOST


def test_a_misconfigured_endpoint_fails_as_a_dispatch_error(tmp_path):
    """Invariant 1: the lease must come back.

    `Scheduler._dispatch` catches DispatchError and nothing else, and its
    handler is what returns the capacity. A ValueError out of the client
    construction would escape holding the slot until the dispatch deadline, for
    a container that was never created.
    """
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(CA_PEM)
    dispatcher = GkeJobDispatcher(
        scheduler_settings(), target=GkeTarget("http://10.0.0.1", str(ca_file))
    )

    with pytest.raises(DispatchError) as failure:
        dispatcher._api()

    assert failure.value.code == "gke_misconfigured"
