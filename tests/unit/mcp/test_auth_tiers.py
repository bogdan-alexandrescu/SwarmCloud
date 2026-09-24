"""Which tier a machine is on, and what that tier can actually reach.

WHY THIS MATTERS MORE THAN IT LOOKS. SwarmCloud is meant to be deployable by
anyone with a GCP project, not only by an organisation with Google Workspace.
The thing that decides whether that is true is not the runtime -- it is whether
a newcomer can authenticate at all. Their commonest failure is not choosing the
wrong tier; it is not knowing tiers exist, and reading `Invalid JWT audience`
as a bug in the platform rather than as "your laptop cannot mint that kind of
token, and here is the one it can".

Everything here is offline: detection is a function of the environment, and the
reachability table is data.
"""

from __future__ import annotations

import pytest

from swarm_mcp import auth, client
from swarm_mcp.auth import REACHES, WHY_NOT, Tier, detect

ALL_VARS = (
    "SWARM_ID_TOKEN",
    "SWARM_IAP_CLIENT_ID",
    "SWARM_IMPERSONATE_SA",
    "K_SERVICE",
    "CLOUD_RUN_JOB",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    # Detection probes the metadata server. A laptop running these tests would
    # get a fast failure; a machine ON GCP would not, and the suite must give
    # the same answer in both places.
    monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: False)


def test_a_bare_laptop_lands_on_the_proxy_tier(monkeypatch):
    """The default for anyone who has only run `gcloud auth login` -- which is
    the whole audience for an open-source deployment."""
    assert detect().tier is Tier.PROXY


def test_running_inside_gcp_wins_over_everything_configurable(monkeypatch):
    """The metadata server is free, certain and needs no configuration, so a
    worker that ALSO has SWARM_IMPERSONATE_SA set must not pay for gcloud."""
    monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: True)
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    assert detect().tier is Tier.METADATA


def test_an_explicit_token_beats_detection_entirely(monkeypatch):
    monkeypatch.setenv("SWARM_ID_TOKEN", "supplied.by.the.operator")
    assert detect().tier is Tier.EXPLICIT


def test_iap_needs_both_a_client_id_and_a_service_account(monkeypatch):
    """Only a service account may set an audience on an ID token. An IAP client
    id without one is a half-configuration that would otherwise fail later with
    an error about audiences rather than about what is missing."""
    monkeypatch.setenv("SWARM_IAP_CLIENT_ID", "123.apps.googleusercontent.com")
    detection = detect()
    assert detection.tier is Tier.PROXY
    assert any("only a service account can set one" in v for _, v in detection.considered)

    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    assert detect().tier is Tier.IAP


def test_a_service_account_without_an_iap_client_id_is_the_direct_tier(monkeypatch):
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    assert detect().tier is Tier.IMPERSONATE


def test_detection_records_everything_it_rejected():
    """So a wrong answer is debuggable without reading the source."""
    considered = dict(detect().considered)
    assert set(considered) >= {"SWARM_ID_TOKEN", "metadata server", "SWARM_IMPERSONATE_SA"}


# -- the reachability table ------------------------------------------------


def test_every_tier_declares_what_it_reaches():
    assert set(REACHES) == set(Tier)
    for tier, profiles in REACHES.items():
        assert profiles, f"{tier} reaches nothing"
        assert set(profiles) <= {"solo", "team"}


def test_the_proxy_tier_cannot_reach_a_team_deployment():
    """Measured, not assumed: a team deployment keeps ingress at
    internal-and-cloud-load-balancing, and Cloud Run answers a proxied call
    from outside the VPC with an HTML 404."""
    assert REACHES[Tier.PROXY] == ("solo",)
    assert "ingress refuses" in WHY_NOT[(Tier.PROXY, "team")]


def test_the_iap_tier_cannot_reach_a_solo_deployment():
    assert REACHES[Tier.IAP] == ("team",)
    assert "no load balancer" in WHY_NOT[(Tier.IAP, "solo")]


def test_every_unreachable_pairing_has_an_explanation():
    """A tier that silently cannot reach a profile is the failure this whole
    module exists to prevent."""
    for tier, reaches in REACHES.items():
        for profile in ("solo", "team"):
            if profile not in reaches:
                assert (tier, profile) in WHY_NOT, f"{tier}->{profile} unexplained"


# -- the proxy tier does not send its own token ----------------------------


def test_the_proxy_tier_must_not_add_an_authorization_header(monkeypatch):
    """gcloud supplies it. A second header would replace the only credential
    Cloud Run accepts with one it does not."""
    c = client.SwarmClient(base_url="https://example.invalid", connect=True)
    assert c.tier is Tier.PROXY
    assert c.sends_own_token is False


def test_every_other_tier_does_send_its_own_token(monkeypatch):
    monkeypatch.setenv("SWARM_ID_TOKEN", "a.b.c")
    c = client.SwarmClient(base_url="https://example.invalid", connect=True)
    assert c.sends_own_token is True


def test_user_credentials_are_refused_a_targeted_token_with_a_readable_reason():
    """The fact that makes a laptop different from CI, stated where someone
    hits it rather than in a doc they have not found."""
    with pytest.raises(client.SwarmError, match="cannot mint a token for a specific audience"):
        auth.id_token_for("https://example.invalid", tier=Tier.PROXY)


def test_the_iap_tier_addresses_the_client_id_not_the_service_url(monkeypatch):
    """Getting this backwards produces `Invalid JWT audience`, which sends
    people to look at their IAM policy, where the problem is not."""
    monkeypatch.setenv("SWARM_IAP_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    c = client.SwarmClient(base_url="https://swarm.example.com", connect=True)
    assert c._audience == "123.apps.googleusercontent.com"


# -- edge errors -----------------------------------------------------------


def test_an_html_404_is_explained_as_an_ingress_refusal():
    """A 404 here almost never means "no such route". It is what Cloud Run
    returns when ingress refuses the caller, and reporting the status alone
    sends people hunting for a routing bug that does not exist."""
    message = client._explain(404, "<html><head><title>404 Page not found</title></head></html>")
    assert "INGRESS" in message
    assert "internal-and-cloud-load-balancing" in message


def test_an_html_401_is_explained_as_iap_answering_first():
    message = client._explain(401, "<!doctype html><html>sign in</html>")
    assert "IAP rejected this before the API saw it" in message
    assert "SWARM_IAP_CLIENT_ID" in message


def test_a_json_error_from_the_application_is_passed_through_unchanged():
    """The API's own errors are already written for a human. Wrapping them in a
    guess about edges would bury the real message."""
    assert client._explain(409, '{"detail": "task is already cancelled"}') == (
        "task is already cancelled"
    )


def test_an_empty_body_still_says_something():
    assert client._explain(500, "") == "HTTP 500"
