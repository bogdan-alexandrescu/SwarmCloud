"""How a caller proves who it is, without assuming a Google Workspace org.

THE CONSTRAINT EVERYTHING HERE IS BUILT AROUND, measured on 2026-09-20 against
a throwaway Cloud Run service with `ingress=all`, IAM enforced, and no IAP
anywhere in the path:

    unauthenticated          -> 403                     (IAM does gate)
    authenticated via proxy  -> authorization_present: false
                                goog_headers: []

The container receives NOTHING about the caller. Not a re-minted token, not an
`x-goog-authenticated-user-email` -- nothing. So Cloud Run's own IAM and
application-level identity are mutually exclusive, permanently. A deployment
can have an edge gate OR per-user tenant resolution, never both.

That is not a quirk of one project; it is how Cloud Run works, and it is the
fact an open-source deployment has to be designed around. It produces two
honest shapes rather than one compromised one:

    SOLO   Cloud Run IAM is the gate. The app is told its tenant at deploy
           time, because it cannot learn one from the request. Needs a GCP
           project and nothing else -- no organisation, no Workspace, no
           domain, no certificate, no IAP brand. One person, or a small team
           sharing one workspace, IS one tenant, so this is not a downgrade.

    TEAM   An external load balancer with IAP is the gate, `allUsers` holds
           run.invoker so the caller's token reaches the app unmodified, and
           the app resolves a tenant per user. Needs an organisation.

The tiers below are how a client authenticates into one of those. They are
DETECTED, not configured, because the most common failure for a new user is
not picking the wrong tier -- it is not knowing that tiers exist.

THE SIGNED-IN TIER (2026-09-25) is the one a developer is meant to be on. A
TEAM deployment's IAP uses a Google-managed OAuth client, which no user
credential can mint for (measured: a gcloud user token is 401, IAP error code
900), so until then the only way through from a laptop was impersonating a
service account -- every developer acting as the same robot. Google's
documented answer is one Desktop OAuth client per deployment, allowlisted on
the IAP resource as a programmatic client; each developer signs in with it once
(`sc login`, signin.py) and presents the ID token it mints. The tier is chosen
when the resolved deployment (config.py) has a client id; impersonation stays
ahead of it, because SWARM_IMPERSONATE_SA is set on purpose and is what CI uses.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .client import SwarmError, _run
from .follow import RUN_PREFIX

if TYPE_CHECKING:  # pragma: no cover
    from .config import Deployment

_METADATA = "http://metadata.google.internal/computeMetadata/v1"


class Tier(str, Enum):
    #: SWARM_ID_TOKEN was supplied. Nothing is detected; the operator is right.
    EXPLICIT = "explicit-token"
    #: Running inside GCP. The metadata server mints for any audience, free.
    METADATA = "metadata-server"
    #: A service account we may impersonate. The CI answer for a SOLO
    #: deployment. Only a service account may set an audience on an ID token,
    #: which is what IAP needs -- but the audience this tier sets is the API's
    #: own URL, so satisfying IAP takes the client id as well, and that is the
    #: tier below rather than this one.
    IMPERSONATE = "service-account"
    #: An IAP-protected front door plus the client id to address it.
    IAP = "iap"
    #: A developer signed in with the deployment's Desktop OAuth client
    #: (`sc login`). The ID token's audience is that client id, and IAP admits
    #: it because the client is allowlisted as a programmatic client. The
    #: developer is THEMSELVES on the other side, not a service account.
    SIGNED_IN = "signed-in"
    #: Ordinary user credentials. `gcloud run services proxy` mints the token,
    #: because `gcloud auth print-identity-token` CANNOT set an audience for a
    #: user -- the single fact that makes a laptop different from CI.
    PROXY = "user-credentials"


#: Which tiers can actually reach which deployment. Kept as data rather than as
#: branches because `swarm doctor` has to explain it, and an explanation
#: computed from the same table it acts on cannot drift from the behaviour.
REACHES: dict[Tier, tuple[str, ...]] = {
    Tier.EXPLICIT: ("solo", "team"),
    # Inside GCP, so inside the VPC: a team deployment's
    # `internal-and-cloud-load-balancing` ingress accepts the call, and the
    # audience is the service URL. The deployed `swarm-verify` job is exactly
    # this -- see terraform/infra/verify.tf, which sets API_AUDIENCE to the
    # service url and runs on the connector.
    Tier.METADATA: ("solo", "team"),
    # NOT "team". See WHY_NOT below: this tier is only ever reached when the
    # metadata server is absent, so the caller is outside the VPC, where a
    # team deployment's ingress refuses it before reading the token.
    Tier.IMPERSONATE: ("solo",),
    Tier.IAP: ("team",),
    # Only a team deployment has IAP, and IAP is the only thing that takes an
    # ID token minted for an allowlisted Desktop client.
    Tier.SIGNED_IN: ("team",),
    Tier.PROXY: ("solo",),
}

WHY_NOT: dict[tuple[Tier, str], str] = {
    (Tier.PROXY, "team"): (
        "a team deployment keeps Cloud Run open to `allUsers` and gates at the "
        "load balancer, so its ingress refuses the direct call the proxy makes -- "
        "and the load balancer will not take a gcloud USER credential either: "
        "measured 2026-09-24 against the live front door, a gcloud user access "
        "token is answered 401 with IAP error code 900, because a Google-managed "
        "IAP client admits only OAuth clients allowlisted as programmatic "
        "clients. Sign in with the deployment's Desktop OAuth client instead: "
        f"`{RUN_PREFIX}sc context add <name> --url <deployment> --client-id <id>`, "
        f"then `{RUN_PREFIX}sc login` (the plugin asks for the client id at "
        "install). CI sets "
        "SWARM_IMPERSONATE_SA to a service account holding "
        "roles/iap.httpsResourceAccessor."
    ),
    (Tier.SIGNED_IN, "solo"): (
        "a solo deployment has no load balancer and no IAP, so an ID token minted "
        "for a Desktop OAuth client has nothing that will accept it. Remove the "
        f"client id from this context (`{RUN_PREFIX}sc context add <name> --url "
        "<run.app address>` with no --client-id)."
    ),
    (Tier.IMPERSONATE, "team"): (
        "this tier reaches the front door and IAP ACCEPTS its credential -- "
        "measured 2026-09-24, IAP answered 403 'Access denied. For user "
        "swarm-verify@...', and a 403 that NAMES the caller is authentication "
        "having succeeded. What is missing is the authorisation: "
        "roles/iap.httpsResourceAccessor, granted through frontend_iap_members in "
        "terraform/bootstrap/terraform.tfvars, which the owner applies and CI does "
        "not (moved out of terraform/infra on 2026-09-24). That list now holds "
        "domain:saga.xyz and swarm-verify; a service account not on it gets the "
        "same named 403 until it is added there. SWARM_IAP_CLIENT_ID is NOT the "
        "answer here: terraform/modules/frontend sets no oauth2_client_id on "
        "purpose, so IAP uses a Google-managed client and there is no audience to "
        "mint an ID token for at all."
    ),
    (Tier.IAP, "solo"): (
        "a solo deployment has no load balancer and no IAP, so there is nothing "
        "for an IAP audience to address. Unset SWARM_IAP_CLIENT_ID."
    ),
}


@dataclass
class Detection:
    tier: Tier
    detail: str
    #: Everything checked and rejected, in order, so a wrong answer is
    #: debuggable without reading this file.
    considered: list[tuple[str, str]] = field(default_factory=list)


def _metadata_available(timeout: float = 0.3) -> bool:
    """Is there a GCP metadata server?

    The environment variables come first because they are free and certain; the
    probe exists for GCE and GKE, which set neither. The timeout is short on
    purpose: off-GCP this hostname usually fails to resolve instantly, but on a
    hostile network it can hang, and a CLI that pauses for thirty seconds
    before printing anything reads as broken.
    """
    if os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"):
        return True
    request = urllib.request.Request(f"{_METADATA}/", headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def detect(deployment: "Deployment | None" = None) -> Detection:
    """Which tier, given the environment and -- for sign-in -- the deployment.

    The deployment is PASSED, never resolved here: resolving reads the user's
    config file, and a tier check that quietly did file I/O would make every
    caller's answer depend on a machine it did not ask about.
    """
    considered: list[tuple[str, str]] = []

    if os.environ.get("SWARM_ID_TOKEN", "").strip():
        return Detection(Tier.EXPLICIT, "SWARM_ID_TOKEN is set", considered)
    considered.append(("SWARM_ID_TOKEN", "not set"))

    if _metadata_available():
        return Detection(Tier.METADATA, "running inside GCP", considered)
    considered.append(("metadata server", "not reachable; not running inside GCP"))

    impersonate = os.environ.get("SWARM_IMPERSONATE_SA", "").strip()
    client_id = os.environ.get("SWARM_IAP_CLIENT_ID", "").strip()
    if client_id and impersonate:
        return Detection(
            Tier.IAP, f"IAP client id, impersonating {impersonate}", considered
        )
    if client_id and not impersonate:
        considered.append((
            "SWARM_IAP_CLIENT_ID",
            "set, but SWARM_IMPERSONATE_SA is not -- IAP needs a token minted "
            "for that audience, and only a service account can set one",
        ))
    else:
        considered.append(("SWARM_IAP_CLIENT_ID", "not set"))

    if impersonate:
        return Detection(Tier.IMPERSONATE, f"impersonating {impersonate}", considered)
    considered.append(("SWARM_IMPERSONATE_SA", "not set"))

    if deployment is not None and deployment.client_id:
        return Detection(
            Tier.SIGNED_IN,
            f"context {deployment.context} has a Desktop OAuth client; you sign in as yourself",
            considered,
        )
    considered.append((
        "sign-in client",
        "no deployment configured" if deployment is None
        else f"context {deployment.context} has no OAuth client id",
    ))

    return Detection(
        Tier.PROXY,
        "ordinary user credentials; an authenticated local proxy will be used",
        considered,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Proxy:
    """`gcloud run services proxy`, supervised.

    WHY A SUBPROCESS AND NOT A TOKEN. `gcloud auth print-identity-token` refuses
    `--audiences` for user credentials -- only service accounts may set one --
    and Cloud Run rejects a token whose audience is not its own URL. The proxy
    is Google's answer to exactly that gap: it mints the right token per request
    and forwards on localhost. Reimplementing it would mean reimplementing the
    user OAuth flow, which is how a five-minute setup becomes an afternoon.
    """

    def __init__(self, service: str, region: str, project: str) -> None:
        self.service = service
        self.region = region
        self.project = project
        self.port = _free_port()
        self._process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, *, timeout: float = 25.0) -> str:
        argv = [
            "gcloud", "run", "services", "proxy", self.service,
            "--region", self.region, "--project", self.project,
            "--port", str(self.port),
        ]
        try:
            self._process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except FileNotFoundError as exc:
            raise SwarmError(
                "gcloud is not installed. It is how a laptop authenticates to "
                "Cloud Run; there is no supported way to mint the token without it."
            ) from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                output = (self._process.stdout.read() if self._process.stdout else "") or ""
                raise SwarmError(
                    f"`gcloud run services proxy {self.service}` exited: "
                    f"{output.strip()[:400]}"
                )
            with socket.socket() as sock:
                sock.settimeout(0.25)
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self.url
            time.sleep(0.25)
        self.stop()
        raise SwarmError(
            f"the local proxy to {self.service} did not start within {timeout:.0f}s"
        )

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._process.kill()
        self._process = None

    def __enter__(self) -> "Proxy":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def id_token_for(audience: str, *, tier: Tier) -> str:
    """An ID token for `audience`, by whichever route this tier allows."""
    if tier is Tier.EXPLICIT:
        return os.environ["SWARM_ID_TOKEN"].strip()

    if tier is Tier.METADATA:
        request = urllib.request.Request(
            f"{_METADATA}/instance/service_accounts/default/identity"
            f"?audience={urllib.parse.quote(audience, safe='')}&format=full",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode().strip()

    if tier in (Tier.IMPERSONATE, Tier.IAP):
        return _run([
            "gcloud", "auth", "print-identity-token",
            f"--impersonate-service-account={os.environ['SWARM_IMPERSONATE_SA'].strip()}",
            f"--audiences={audience}",
            "--include-email",
        ])

    if tier is Tier.SIGNED_IN:
        raise SwarmError(
            "the signed-in tier's token comes from the stored sign-in (signin.py), "
            "not from gcloud"
        )
    raise SwarmError(
        "user credentials cannot mint a token for a specific audience; this "
        "tier reaches the API through a local proxy instead"
    )
