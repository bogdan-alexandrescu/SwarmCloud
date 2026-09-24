"""WHERE THE API IS, and which credential that address takes.

THE DEFECT, MEASURED FROM A LAPTOP ON 2026-09-24, in this order:

    uv run swarm doctor
      tier      user-credentials
      api       UNREACHABLE
                GET /v1/tenants/me -> 404: an HTML 404 from Google's edge, not
                from the API

    scripts/api.sh GET /runtimes            -> HTTP 401 (IAP error code 900)
    SWARM_IMPERSONATE_SA=swarm-verify@...   -> HTTP 403 "Access denied. For
      scripts/api.sh GET /runtimes             user swarm-verify@..."

    and, from a browser signed in as bogdan@saga.xyz, the SAME API over the
    SAME front door:
      GET https://swarm.saga.xyz/v1/runtimes -> 200, five runner profiles

So the control plane was healthy and the bridge called it unreachable. Two
independent causes, one per half of this file:

  1. `resolve_api_url` only ever asked Cloud Run, and Cloud Run's answer for a
     `team` deployment is the *.run.app address, whose ingress is
     `internal-and-cloud-load-balancing`. That address answers a byte-identical
     272-byte HTML 404 to every path, healthy or not -- and 404 is the one
     status a reader takes for "wrong route on a broken deployment" rather than
     "wrong host". `scripts/lib/common.sh` has documented this since 2026-09-22
     and reads `frontend_hostname` out of Track C's tfvars; the Python bridge,
     which is the half a Claude Code session uses, did not.
  2. Even pointed at the load balancer it presented a Google ID token, which
     IAP refuses with `Invalid IAP credentials: Invalid JWT audience` -- a
     sentence that sends a reader to their IAM policy when the problem is the
     KIND of token. The front door takes an OAuth ACCESS token.

Everything here is offline: a tfvars file in `tmp_path`, a fake `_run`, and a
fake opener. Nothing shells out and nothing resolves a hostname.
"""

from __future__ import annotations

import io
import re
import urllib.error
from pathlib import Path

import pytest

from swarm_mcp import auth, client
from swarm_mcp.auth import Tier
from swarm_mcp.client import SwarmClient, SwarmError

_REPO = Path(__file__).resolve().parents[3]

#: Every variable that can decide the address or the credential. Cleared for
#: every test, because a CI shell that exports one would silently answer these
#: questions for us -- and the answer would look like a code change.
_VARS = (
    "SWARM_API_URL",
    "API_URL",
    "SWARM_API_HOST",
    "API_HOST",
    "SWARM_REPO_ROOT",
    "ENVIRONMENT",
    "PROJECT_ID",
    "REGION",
    "API_SERVICE",
    "SWARM_ID_TOKEN",
    "SWARM_IAP_CLIENT_ID",
    "SWARM_IMPERSONATE_SA",
    "SWARM_ACCESS_TOKEN",
    "API_AUDIENCE",
    "K_SERVICE",
    "CLOUD_RUN_JOB",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: False)


def _tfvars(tmp_path: Path, body: str, *, environment: str = "dev") -> None:
    """A tfvars file where `front_door_host` looks for one."""
    directory = tmp_path / "terraform" / "environments" / environment
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{environment}.tfvars").write_text(body)


def _explode(*_args, **_kwargs):
    """Stands in for `_run`. Called at all, the test has failed."""
    raise AssertionError(
        "gcloud was consulted; the front door should have answered first"
    )


# -- half one: the address -------------------------------------------------


def test_the_front_door_comes_from_track_cs_tfvars_and_not_from_a_copy():
    """READ, not restated. `frontend_hostname` is Track C's input and the only
    place the hostname is decided, so this asserts the bridge agrees with that
    file rather than asserting a literal -- a literal here would be the third
    copy of the name and the one nobody updates.

    Parsed independently of the implementation (a different expression, over
    the same file) so that the two cannot both be wrong in the same way.
    """
    tfvars = (_REPO / "terraform" / "environments" / "dev" / "dev.tfvars").read_text()
    expected = re.findall(r'frontend_hostname\s*=\s*"(.+?)"', tfvars)
    assert expected, "dev.tfvars no longer sets frontend_hostname"
    assert client.front_door_host() == expected[0]


def test_the_front_door_wins_over_cloud_run_entirely(monkeypatch, tmp_path):
    """THE MUTATION THIS CATCHES: delete the `front_door_host()` branch from
    `resolve_api_url`. `_run` then gets called, and an assertion fires instead
    of the *.run.app address being handed back as if it worked.
    """
    _tfvars(tmp_path, 'enable_frontend   = true\nfrontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_ID", "a-project")
    monkeypatch.setattr(client, "_run", _explode)

    assert client.resolve_api_url() == "https://swarm.example.com"


def test_an_explicit_url_still_wins_over_the_front_door(monkeypatch, tmp_path):
    """`terraform/infra/verify.tf` sets API_URL to the *.run.app address for the
    in-VPC job, where that address DOES work and there is no gcloud in the image
    to look anything up with. The front door must not override it."""
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("API_URL", "https://swarm-api-xyz-uc.a.run.app")
    monkeypatch.setattr(client, "_run", _explode)

    assert client.resolve_api_url() == "https://swarm-api-xyz-uc.a.run.app"


@pytest.mark.parametrize("variable", ["API_HOST", "SWARM_API_HOST"])
@pytest.mark.parametrize(
    "supplied", ["swarm.example.com", "https://swarm.example.com", "https://swarm.example.com/"]
)
def test_the_operator_can_name_the_host_with_or_without_a_scheme(
    monkeypatch, tmp_path, variable, supplied
):
    """`common.sh::front_door_host` strips both schemes and a trailing slash,
    so this does too: an operator who pastes the URL out of a browser must not
    end up with `https://https://swarm...`."""
    _tfvars(tmp_path, 'frontend_hostname = "from-tfvars.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv(variable, supplied)
    assert client.front_door_host() == "swarm.example.com"


def test_enable_frontend_false_means_there_is_no_front_door(monkeypatch, tmp_path):
    """The hostname is usually still written down next to the switch that turns
    the load balancer off. Using it then sends every call at a name that
    resolves to nothing, which is why `common.sh` checks the flag first."""
    _tfvars(
        tmp_path,
        'enable_frontend   = false\nfrontend_hostname = "swarm.example.com"\n',
    )
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    assert client.front_door_host() == ""


def test_a_solo_deployment_has_no_front_door_and_falls_back_to_cloud_run(
    monkeypatch, tmp_path
):
    """The shape this whole change must not break: one person, one GCP project,
    no organisation, no load balancer. Cloud Run is then the address, and its
    default ingress serves it."""
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))  # no terraform/ at all
    monkeypatch.setenv("PROJECT_ID", "a-project")
    monkeypatch.setattr(
        client, "_run", lambda argv, **kw: "https://swarm-api-xyz-uc.a.run.app|all"
    )
    assert client.front_door_host() == ""
    assert client.resolve_api_url() == "https://swarm-api-xyz-uc.a.run.app"


def test_an_empty_ingress_annotation_is_the_default_and_is_allowed(monkeypatch, tmp_path):
    """`gcloud` prints nothing for an unset annotation, and unset means `all`.
    Treating empty as "not `all`" would refuse every solo deployment."""
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_ID", "a-project")
    monkeypatch.setattr(client, "_run", lambda argv, **kw: "https://svc-xyz-uc.a.run.app|")
    assert client.resolve_api_url() == "https://svc-xyz-uc.a.run.app"


def test_an_address_whose_ingress_refuses_the_caller_is_refused_not_handed_back(
    monkeypatch, tmp_path
):
    """THE MUTATION THIS CATCHES: go back to `--format=value(status.url)` and
    drop the ingress check. `resolve_api_url` then returns an address that
    answers HTTP 404 to every path -- including `/readyz` -- and the caller
    reports a healthy control plane as a broken one. That is not hypothetical:
    `scripts/e2e-test.sh` did exactly this and told its operator to rule out
    IAM and then redeploy.
    """
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_ID", "a-project")
    monkeypatch.setattr(
        client,
        "_run",
        lambda argv, **kw: "https://swarm-api-xyz-uc.a.run.app|internal-and-cloud-load-balancing",
    )
    with pytest.raises(SwarmError) as caught:
        client.resolve_api_url()
    message = str(caught.value)
    assert "internal-and-cloud-load-balancing" in message
    assert "API_HOST" in message, "the refusal must name the way out of it"
    assert "404" in message, (
        "the reader has to be told which status this produces, or they will read "
        "the 404 they get as a missing route"
    )


def test_the_url_is_read_out_of_the_two_field_format(monkeypatch, tmp_path):
    """The format string gained a second field, so the parse is asserted: a
    client that kept the whole `url|ingress` string as the URL would send every
    request to a host with a pipe in it."""
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_ID", "a-project")
    seen: list[list[str]] = []

    def _describe(argv, **_kw):
        seen.append(argv)
        return "https://svc-xyz-uc.a.run.app/|all"

    monkeypatch.setattr(client, "_run", _describe)
    assert client.resolve_api_url() == "https://svc-xyz-uc.a.run.app"
    assert any("ingress" in arg for arg in seen[0]), (
        "the ingress must be read in the SAME call as the url; a second call is "
        "a second chance for them to disagree"
    )


# -- half two: the credential ---------------------------------------------


def test_the_front_door_takes_an_access_token_and_cloud_run_takes_an_id_token(
    monkeypatch, tmp_path
):
    """THE MUTATION THIS CATCHES: revert `request` to `self._id_token()`. IAP
    answers that with `Invalid IAP credentials: Invalid JWT audience`, and the
    remedy a reader draws from those words is the wrong one."""
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(SwarmClient, "access_token", lambda self: "ACCESS")
    monkeypatch.setattr(SwarmClient, "_id_token", lambda self: "IDTOKEN")

    front = SwarmClient(base_url="https://swarm.example.com")
    assert front.front_door is True
    assert front.credential() == "ACCESS"

    direct = SwarmClient(base_url="https://swarm-api-xyz-uc.a.run.app")
    assert direct.front_door is False
    assert direct.credential() == "IDTOKEN"


def test_a_deployment_with_its_own_oauth_client_still_gets_an_id_token(
    monkeypatch, tmp_path
):
    """THE MUTATION THIS CATCHES: make `credential()` return `access_token()`
    for every front door. `Tier.IAP` is then detected, printed by `swarm
    doctor`, and never used -- a tier that exists, is announced, and does
    nothing, which is the shape this lane was sent to find rather than to add.

    That tier is for the OTHER shape of IAP deployment: one whose backend
    service sets `oauth2_client_id`, where an ID token minted for that client id
    is the documented programmatic path and `auth.id_token_for` already mints
    it. This deployment is not that shape; somebody else's will be.
    """
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("SWARM_IAP_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    monkeypatch.setattr(
        SwarmClient,
        "access_token",
        lambda self: (_ for _ in ()).throw(AssertionError("an access token was minted")),
    )
    monkeypatch.setattr(SwarmClient, "_id_token", lambda self: "IDTOKEN")

    c = SwarmClient()
    assert c.tier is Tier.IAP
    assert c.front_door is True
    # The audience is the client id, never the service url -- getting that
    # backwards is the `Invalid JWT audience` this whole module is about.
    assert c._audience == "123.apps.googleusercontent.com"
    assert c.credential() == "IDTOKEN"


def test_the_header_that_actually_goes_out_at_the_front_door_is_the_access_token(
    monkeypatch, tmp_path
):
    """Asserted at the transport, not at the chooser. `credential()` could be
    right and `request` could still send something else -- which is precisely
    the class of bug `test_plugin_commands` found in `follow_live_with`: the
    decision was correct and the field a reader consumes was not.
    """
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(SwarmClient, "access_token", lambda self: "ACCESS")
    monkeypatch.setattr(
        SwarmClient,
        "_id_token",
        lambda self: (_ for _ in ()).throw(AssertionError("an ID token was minted")),
    )

    headers: list[str] = []

    class _Response:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _opener(request, timeout=None):  # noqa: ARG001
        headers.append(request.get_header("Authorization"))
        return _Response()

    monkeypatch.setattr(client, "_open", _opener)
    SwarmClient(base_url="https://swarm.example.com").request("GET", "/v1/runtimes")
    assert headers == ["Bearer ACCESS"]


def test_the_front_door_is_decided_by_the_address_not_by_configuration(
    monkeypatch, tmp_path
):
    """An operator inside the VPC who points SWARM_API_URL at the *.run.app
    address must keep getting an ID token, even though a front door exists --
    `api_is_front_door` in the shell asks the same question the same way."""
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("SWARM_API_URL", "https://swarm-api-xyz-uc.a.run.app")
    monkeypatch.setattr(SwarmClient, "access_token", lambda self: "ACCESS")
    monkeypatch.setattr(SwarmClient, "_id_token", lambda self: "IDTOKEN")

    c = SwarmClient()
    assert c.base_url == "https://swarm-api-xyz-uc.a.run.app"
    assert c.front_door is False
    assert c.credential() == "IDTOKEN"


def test_a_path_under_the_front_door_host_is_still_the_front_door(monkeypatch, tmp_path):
    """A deployment can serve the API under a prefix. Matching the bare origin
    only would send an ID token to IAP for every such deployment."""
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    assert client.is_front_door("https://swarm.example.com") is True
    assert client.is_front_door("https://swarm.example.com/api") is True
    # A host that merely STARTS with the front door's name is a different host.
    assert client.is_front_door("https://swarm.example.com.evil.test") is False
    assert client.is_front_door("") is False


def test_impersonation_reaches_the_access_token(monkeypatch, tmp_path):
    """SWARM_IMPERSONATE_SA was honoured by the ID-token path and ignored by
    this one, which made `swarm doctor`'s own advice a lie: it tells an operator
    to set that variable to reach a team deployment, and setting it left the
    front door presenting the operator's own token -- refused by IAP with a
    DIFFERENT error than the one they were told to expect.

    THE MUTATION THIS CATCHES: drop the impersonation branch from
    `access_token`. The argv then carries no `--impersonate-service-account`
    and this goes red.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(client, "_run", lambda argv, **kw: calls.append(argv) or "TOKEN")
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")

    c = SwarmClient(base_url="https://example.invalid")
    assert c.access_token() == "TOKEN"
    assert calls, "no token was minted at all"
    assert any(
        arg == "--impersonate-service-account=sa@example.iam.gserviceaccount.com"
        for arg in calls[-1]
    ), calls[-1]


def test_an_explicit_access_token_beats_impersonation(monkeypatch, tmp_path):
    """Same precedence as `common.sh`: a token the operator supplied is the one
    they meant, and no subprocess runs."""
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    monkeypatch.setenv("SWARM_ACCESS_TOKEN", "SUPPLIED")
    monkeypatch.setattr(client, "_run", _explode)
    assert SwarmClient(base_url="https://example.invalid").access_token() == "SUPPLIED"


def test_the_front_door_beats_the_local_proxy(monkeypatch, tmp_path):
    """THE MUTATION THIS CATCHES: put the `Tier.PROXY` branch back first in
    `connect`. `gcloud run services proxy` calls the *.run.app address directly,
    which a team deployment's ingress refuses -- so the old order spent up to 25
    seconds starting a subprocess in order to reach a host that cannot answer,
    while the load balancer that can was in Track C's tfvars, unread.
    """
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        auth,
        "Proxy",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a proxy was started")),
    )

    c = SwarmClient()
    assert c.tier is Tier.PROXY, "the fixture leaves this machine on user credentials"
    assert c._proxy is None
    assert c.base_url == "https://swarm.example.com"
    # No proxy, so nothing else supplies the header.
    assert c.sends_own_token is True
    assert c.front_door is True


def test_an_iap_refusal_names_the_access_token_and_not_only_a_client_id():
    """The old remedy was unreachable advice on this deployment.

    `terraform/modules/frontend/main.tf` sets `iap { enabled = true }` with NO
    `oauth2_client_id` -- deliberately, because supplying one means a client
    secret in state -- so IAP uses a Google-managed OAuth client and there is no
    audience for anyone to mint an ID token against. Telling a reader to set
    SWARM_IAP_CLIENT_ID here sends them to configure a variable that cannot
    help, they get the identical 401, and they conclude the platform is broken.
    """
    message = client._explain(
        401, "Invalid IAP credentials: Invalid JWT audience."
    )
    # Case-insensitive: this sentence is shouted on purpose and the casing is
    # presentation, not the claim.
    assert "access token" in message.lower()
    assert "roles/iap.httpsResourceAccessor" in message
    assert "Invalid JWT audience." in message, "Google's own clause must survive"


def test_an_html_sign_in_page_separates_the_401_from_the_403():
    """They need different answers and used to get one. A 403 that NAMES the
    caller is IAP saying "authenticated, not on the list", which is one IAM
    grant from working; a 401 is the token itself being refused, which no grant
    fixes. Measured both against the live front door on 2026-09-24."""
    message = client._explain(403, "<!doctype html><html>sign in</html>")
    assert "403" in message and "401" in message
    assert "frontend_iap_members" in message, (
        "the 403 remedy lives in Track C's tfvars and the reader has to be sent "
        "to the right file"
    )


def test_the_edge_flag_survives_all_of_this(monkeypatch, tmp_path):
    """`fetch_accounts` grades on `SwarmError.edge` to tell "this deployment has
    no /v1/accounts route" from "you never reached the API", and `sc` renders an
    em dash rather than a zero on the second. A change to the credential path
    must not disturb it."""
    _tfvars(tmp_path, 'frontend_hostname = "swarm.example.com"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(SwarmClient, "access_token", lambda self: "ACCESS")

    def _opener(request, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {},
            io.BytesIO(b"Invalid IAP credentials: Invalid JWT audience."),
        )

    monkeypatch.setattr(client, "_open", _opener)
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="https://swarm.example.com").request("GET", "/v1/accounts")
    assert caught.value.edge is True
    assert caught.value.status == 401
