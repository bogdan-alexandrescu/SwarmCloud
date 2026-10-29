"""Stage 2, the edge and identity prerequisites: the OAuth brand, the IAP
service agent, the global address, the DNS record, the certificate's
precondition and the Desktop client with its secret (design §4; plan §9.1;
lesson 17: none of these is in Terraform).

| Step | Manifest entries | Kind |
|---|---|---|
| `edge.brand` | `oauth-brand.iap` | human: no API creates a brand (B02), so the step prints the console steps and polls its own check every 10 s for 30 min |
| `edge.iap-agent` | `other.iap-service-agent` | engine: `generateServiceIdentity` for iap.googleapis.com |
| `edge.global-ip` | `network.global-ip` | engine: reserves the address the frontend module names (`swarm-ui-ip`) |
| `edge.dns` | `dns.frontend-hostname` | wait, 60 min: the hostname must resolve to that address |
| `edge.certificate` | `certificate.managed-ssl` | wait, 60 min (`waits.CERTIFICATE`); it never starts before DNS resolves to the reserved address (design R6) |
| `edge.desktop-client` | `oauth-client.desktop-sc`, `declared-value.frontend_iap_programmatic_clients`, `secret.swarm-iap-desktop-client-secret.container` | engine: guided console step; the id is asked, the secret goes through `secret_entry` straight to Secret Manager |

The managed certificate itself is created by infra pass 1 (stage 5), which
attaches it to the load balancer; Google provisions a managed certificate only
once it is attached. So while it is not yet attached, the certificate step is
met as soon as its precondition holds (DNS resolves to the reserved address),
and once it is attached it waits, up to the 60 min bound, for `ACTIVE`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .. import secret_entry
from ..exitcodes import MET, UNMET, WAITING
from ..secret_entry import DevTty, Terminal
from ..waits import CERTIFICATE, WaitSpec
from . import Observation, Plan
from .preflight import ASK, GCP, Ask, Gcp, project_number, remember

#: The frontend module's names (terraform/modules/frontend/main.tf:101, :297,
#: `${var.name_prefix}-ui` with the default prefix `swarm`).
FRONTEND = "swarm-ui"
ADDRESS = f"{FRONTEND}-ip"
CERT = f"{FRONTEND}-cert"
DESKTOP_SECRET = "swarm-iap-desktop-client-secret"
CLIENT_ID = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")

#: B02 / plan §9.1: the brand is re-checked every 10 s for 30 min.
BRAND_WAIT = WaitSpec(
    reason="the OAuth brand (consent screen) being created in the console",
    expected="2 to 5 min",
    expected_s=5 * 60,
    bound_s=30 * 60,
    look_at="Google Auth Platform > Branding, then Audience: Internal",
    interval_s=10,
)

#: plan §9.1: DNS, 60 min.
DNS_WAIT = WaitSpec(
    reason="the hostname's A record resolving to the reserved address",
    expected="a few minutes after the record is saved",
    expected_s=10 * 60,
    bound_s=60 * 60,
    look_at="dig +short <hostname> must print the reserved address and nothing else",
    interval_s=30,
)


class _Base:
    stage = 2
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask) -> None:
        self._gcp = gcp
        self._ask = ask

    @property
    def gcp(self) -> Gcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    #: Whether the check needs the project to exist. On an empty project (a dry
    #: run, or a first run before foundations.project) the check is `unmet`,
    #: never `unknown`: nothing failed to run, there is simply nothing yet.
    needs_project = True

    def check(self, ctx: Any) -> Observation:
        if self.needs_project and self.gcp.project(ctx.project) is None:
            return Observation(UNMET, f"project {ctx.project} does not exist yet (foundations.project)")
        return self._check(ctx)

    def _check(self, ctx: Any) -> Observation:  # pragma: no cover - every step overrides it
        raise TypeError(f"{type(self).__name__} has no check")

    def _number(self, ctx: Any) -> str:
        number = ctx.outputs.get("project_number") or project_number(self.gcp, ctx.project)
        if not number:
            raise LookupError(f"project {ctx.project} has no number yet (stage 1 creates it)")
        return str(number)

    def _hostname(self, ctx: Any) -> str:
        hostname = ctx.state.hostname or ctx.outputs.get("hostname")
        if not hostname:
            raise LookupError("no hostname is recorded (preflight.inputs asks it)")
        return str(hostname)


class BrandStep(_Base):
    id = "edge.brand"
    kind = "human"
    wait = BRAND_WAIT
    satisfies = ("oauth-brand.iap",)
    _explain = (
        "IAP needs an OAuth brand (consent screen) before any backend can turn it on. It has no API "
        "(Google deprecated the one that created brands), is Internal, and can never be deleted, so a "
        "person creates it once in the console; the engine polls until it exists (lesson 17)."
    )

    def _check(self, ctx: Any) -> Observation:
        brands = self.gcp.iap_brands(self._number(ctx))
        if not brands:
            return Observation(UNMET, "no OAuth brand exists yet")
        internal = [b for b in brands if b.get("orgInternalOnly")]
        if not internal:
            return Observation(UNMET, "the OAuth brand is External; IAP here needs Audience: Internal")
        return Observation(MET, "", {"oauth_brand": str(internal[0].get("name", ""))})

    def plan(self, ctx: Any) -> Plan:
        return Plan(
            "create the OAuth brand in the console (once; it can never be deleted); the install continues "
            "by itself when it exists",
            (
                f"open https://console.cloud.google.com/auth/branding?project={ctx.project}",
                "App name: SwarmCloud; support email: a group you own; save",
                f"open https://console.cloud.google.com/auth/audience?project={ctx.project} and choose Internal",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        """No API creates a brand: the plan printed the console steps, and the engine polls the check."""


class IapAgentStep(_Base):
    id = "edge.iap-agent"
    satisfies = ("other.iap-service-agent",)
    _explain = (
        "IAP invokes Cloud Run as its own service agent; without it every request through the load "
        "balancer answers 'The IAP service account is not provisioned' (lesson 17)."
    )

    def _email(self, ctx: Any) -> str:
        return f"service-{self._number(ctx)}@gcp-sa-iap.iam.gserviceaccount.com"

    def _check(self, ctx: Any) -> Observation:
        email = self._email(ctx)
        if self.gcp.service_account(email) is None:
            return Observation(UNMET, f"{email} does not exist")
        return Observation(MET, "", {"iap_service_agent": email})

    def plan(self, ctx: Any) -> Plan:
        return Plan("create IAP's service agent", (f"generateServiceIdentity iap.googleapis.com -> {self._email(ctx)}",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.gcp.generate_service_identity(ctx.project, "iap.googleapis.com")


class GlobalIpStep(_Base):
    id = "edge.global-ip"
    satisfies = ("network.global-ip",)
    _explain = (
        "The one stable address the A record and the managed certificate point at. It is reserved now, "
        "under the frontend module's name, so the DNS record can be set while the rest is built."
    )

    def _check(self, ctx: Any) -> Observation:
        found = self.gcp.global_address(ctx.project, ADDRESS)
        if found is None:
            return Observation(UNMET, f"global address {ADDRESS} is not reserved")
        if found.get("status") not in ("RESERVED", "IN_USE") or not found.get("address"):
            return Observation(WAITING, f"global address {ADDRESS} is {found.get('status')}")
        return Observation(MET, f"{ADDRESS} is {found['address']}", {"global_ip": found["address"]})

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"reserve the global address {ADDRESS}", (f"{ADDRESS}: external, IPv4, global",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.gcp.create_global_address(ctx.project, {
            "name": ADDRESS,
            "addressType": "EXTERNAL",
            "ipVersion": "IPV4",
            "labels": {"managed-by": "swarm-terraform"},
        })


def _reserved_ip(step: _Base, ctx: Any) -> str:
    ip = ctx.outputs.get("global_ip")
    if not ip:
        found = step.gcp.global_address(ctx.project, ADDRESS)
        ip = (found or {}).get("address")
    if not ip:
        raise LookupError(f"global address {ADDRESS} is not reserved (edge.global-ip)")
    return str(ip)


def dns_problem(step: _Base, ctx: Any) -> str | None:
    """Why the hostname does not (only) resolve to the reserved address, or None."""
    hostname, ip = step._hostname(ctx), _reserved_ip(step, ctx)
    found = step.gcp.resolve(hostname)
    if found == {ip}:
        return None
    return f"{hostname} resolves to {sorted(found) or 'nothing'}, not {ip}"


class DnsStep(_Base):
    id = "edge.dns"
    kind = "wait"
    wait = DNS_WAIT
    satisfies = ("dns.frontend-hostname",)
    _explain = (
        "Google issues the managed certificate only once the hostname resolves to the load balancer's "
        "address; until then it stays FAILED_NOT_VISIBLE (lesson 17)."
    )

    def _check(self, ctx: Any) -> Observation:
        problem = dns_problem(self, ctx)
        return Observation(WAITING, problem) if problem else Observation(MET, f"{self._hostname(ctx)} resolves")

    def plan(self, ctx: Any) -> Plan:
        return Plan(
            "set the hostname's A record at your DNS provider; the install continues once it resolves",
            (f"{self._hostname(ctx)}.  300  IN  A  {_reserved_ip(self, ctx)}",),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        """The record is set at the person's DNS provider (the plan prints it); the engine polls."""


class CertificateStep(_Base):
    id = "edge.certificate"
    kind = "wait"
    wait = CERTIFICATE
    satisfies = ("certificate.managed-ssl",)
    _explain = (
        "The managed certificate can stay PROVISIONING for up to an hour, and never leaves it while DNS "
        "points elsewhere; the wait starts only once the hostname resolves to the reserved address "
        "(design R6), and is bounded at 60 min."
    )

    def _check(self, ctx: Any) -> Observation:
        problem = dns_problem(self, ctx)
        if problem:
            return Observation(UNMET, f"the certificate wait has not started: {problem}")
        found = self.gcp.ssl_certificate(ctx.project, CERT)
        status = ((found or {}).get("managed") or {}).get("status")
        if status == "ACTIVE":
            return Observation(MET, f"{CERT} is ACTIVE")
        if found is None or not found.get("inUseBy"):
            return Observation(MET, f"DNS resolves to the reserved address; {CERT} is provisioned once infra "
                                    "pass 1 attaches it (stage 5), and stage 7 waits for ACTIVE",
                               {"certificate_precondition": True})
        return Observation(WAITING, f"{CERT} is {status}")

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"wait for {CERT} (up to 60 min), only once DNS resolves to the reserved address")

    def apply(self, ctx: Any, plan: Plan) -> None:
        problem = dns_problem(self, ctx)
        if problem:
            raise RuntimeError(f"the certificate wait does not start before DNS is right: {problem}")


class DesktopClientStep(_Base):
    id = "edge.desktop-client"
    satisfies = (
        "oauth-client.desktop-sc",
        "declared-value.frontend_iap_programmatic_clients",
        "secret.swarm-iap-desktop-client-secret.container",
    )
    _explain = (
        "sc signs a person in to IAP with this Desktop OAuth client (a gcloud token is refused with 401 "
        "code 900). No API creates one, so it is made in the console; its id is not secret, its secret is "
        "typed at a hidden prompt and goes straight to Secret Manager, never through a chat."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask,
                 open_terminal: Callable[[], Terminal] = DevTty.open) -> None:
        super().__init__(gcp, ask)
        self._open_terminal = open_terminal

    def _check(self, ctx: Any) -> Observation:
        client = str(ctx.outputs.get("desktop_client_id") or "")
        if not CLIENT_ID.match(client):
            return Observation(UNMET, "no Desktop client id is recorded")
        if self.gcp.secret(ctx.project, DESKTOP_SECRET) is None:
            return Observation(UNMET, f"secret {DESKTOP_SECRET} does not exist")
        version = self.gcp.enabled_version(ctx.project, DESKTOP_SECRET)
        if version is None:
            return Observation(UNMET, f"secret {DESKTOP_SECRET} has no enabled version")
        return Observation(MET, "", {"desktop_client_id": client, "desktop_client_secret_version": version})

    def plan(self, ctx: Any) -> Plan:
        return Plan(
            "create the Desktop OAuth client in the console, then give its id, and its secret at a hidden prompt",
            (
                f"open https://console.cloud.google.com/auth/clients/create?project={ctx.project}",
                "Application type: Desktop app; name: SwarmCloud sc; create",
                f"the secret goes to Secret Manager ({DESKTOP_SECRET}), never to a chat or a file",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        client = str(ctx.outputs.get("desktop_client_id") or "")
        if not CLIENT_ID.match(client):
            client = self._ask(ctx, self.id, "The Desktop client's id (ends .apps.googleusercontent.com)").strip()
            if not CLIENT_ID.match(client):
                raise ValueError(f"{client!r} is not a Desktop client id")
            remember(ctx, outputs={"desktop_client_id": client})
        gcp = self.gcp
        if gcp.secret(ctx.project, DESKTOP_SECRET) is None:
            gcp.create_secret(ctx.project, DESKTOP_SECRET, {"managed-by": "swarm-install"})
        if gcp.enabled_version(ctx.project, DESKTOP_SECRET) is None:
            version = secret_entry.enter_secret(
                ctx, step_id=self.id, project=ctx.project, secret_id=DESKTOP_SECRET,
                prompt="The Desktop client's secret", store=gcp, open_terminal=self._open_terminal,
            )
            remember(ctx, outputs={"desktop_client_secret_version": version})


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK,
                open_terminal: Callable[[], Terminal] = DevTty.open) -> list[Any]:
    """Stage 2 in order: the brand before the client, the address before DNS before the certificate."""
    return [
        BrandStep(gcp, ask),
        IapAgentStep(gcp, ask),
        GlobalIpStep(gcp, ask),
        DnsStep(gcp, ask),
        CertificateStep(gcp, ask),
        DesktopClientStep(gcp, ask, open_terminal),
    ]


STEPS = build_steps()
