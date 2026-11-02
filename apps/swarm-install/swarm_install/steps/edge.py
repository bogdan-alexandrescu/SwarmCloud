"""Stage 2, the edge and identity prerequisites: the Desktop client secret's
container, the IAP service agent, the global address, the DNS record and the
certificate's precondition; and, after the hand-off, the waits for the OAuth
brand and the Desktop client the console's Sign-in client step makes (design
§4; plan §9.1; lesson 17: none of these is in Terraform; issue #2964).

**Why the client waits for the console.** The console is reachable before any
brand exists: the frontend's backends turn IAP on with no OAuth client
(terraform/modules/frontend/main.tf), so IAP uses its Google-managed OAuth
client, which needs no brand and admits the organisation's own people
(https://docs.cloud.google.com/iap/docs/custom-oauth-configuration). That
page also says an app on the Google-managed client is reached programmatically
through a separate OAuth client in the `programmatic_clients` setting, which
the IAP settings API writes (https://docs.cloud.google.com/iap/docs/sharing-oauth-clients;
terraform/bootstrap/iap_programmatic_clients.tf). So the person makes the
brand and the Desktop client from the console's Admin › Setup, and the CLI
waits for it as it waits for the GitHub App.

| Step | Manifest entries | Kind |
|---|---|---|
| `edge.client-secret-container` | `secret.swarm-iap-desktop-client-secret.container` | engine: creates `swarm-iap-desktop-client-secret` empty, so infra pass 2 can grant swarm-api secretVersionAdder on it alone; its versions come from the console's Admin › Setup step Sign-in client, never from this terminal |
| `edge.iap-agent` | `other.iap-service-agent` | engine: `generateServiceIdentity` for iap.googleapis.com is both the apply and the proof: it is idempotent and answers the agent's email, which is recorded; the agent's account lives in a Google-owned project, so it is never read |
| `edge.global-ip` | `network.global-ip` | engine: reserves the address the frontend module names (`swarm-ui-ip`) |
| `edge.dns` | `dns.frontend-hostname` | wait, 60 min: when one of the project's public Cloud DNS managed zones covers the hostname, `apply` creates the A record there after one typed approval (the hostname); otherwise the record is set at the person's registrar, so `apply` prints it and opens a page showing what the hostname resolves to now. Either way the engine polls Google Public DNS (`dns.google/resolve`, the resolver that page shows), never the local resolver, which can cache a negative answer |
| `edge.certificate` | `certificate.managed-ssl` | wait, 60 min (`waits.CERTIFICATE`); it never starts before DNS resolves to the reserved address (design R6) |
| `edge.console-answers` (stage 8) | `other.console-through-iap` | wait, 60 min (`handoff.CONSOLE_ANSWERS`): first in stage 8, after infra pass 2 and bootstrap pass 2 (stage 7), it polls until `swarm-ui-cert` is `ACTIVE` and an anonymous GET of `https://<H>/` answers IAP's sign-in (302, 401 or 403, `handoff.IAP_ANSWERS`), so stage 8's first call through the hostname does not meet a TLS error or a load balancer still routing |
| `edge.brand` (stage 13, after `handoff.console`) | `oauth-brand.iap` | wait, 6 h (`SIGN_IN_CLIENT`): no API reads or creates a brand (Google shut the IAP OAuth Admin API down on 2026-03-19), and the console refuses to create a client before Branding and Audience are set, so the Desktop client is the brand's proof. The console's Admin › Setup step Sign-in client (issue #2964) links Branding, Audience (Internal) and Create client and takes the client's id and secret in a form; this asks nothing and is met once a Desktop client id is recorded or the console saved one |
| `edge.desktop-client` (stage 13) | `oauth-client.desktop-sc`, `declared-value.frontend_iap_programmatic_clients` | wait, 6 h: met once the client the console saved last is recorded, its secret has an enabled version and the new deployment's context on this machine holds it (`verify_context`). `apply` reads the secret's latest version with the person's own credentials (`take_client`) into that credential store; nothing is typed. The hand-off wait has normally done all of it already, with the bootstrap and infra passes that put the id in IAP's programmatic-clients list and the discovery document (`handoff.sign_in_triggered`) |

The managed certificate itself is created by infra pass 1 (stage 5), which
attaches it to the load balancer; Google provisions a managed certificate only
once it is attached. So while it is not yet attached, the certificate step is
met as soon as its precondition holds (DNS resolves to the reserved address),
and once it is attached it waits, up to the 60 min bound, for `ACTIVE`.

**Why the console wait is in stage 8, not stage 7.** Within a stage the engine
runs steps in registration order (modules by name), and infra pass 2 and
bootstrap pass 2 are registered by `infra.py`, after this module: a stage-7
step here would run before both passes. As the first step of stage 8 it runs
after every stage-7 step and before stage 8's first call through
`https://<H>` (`identity.group-read`).

**What the console wait assumes** (audit N17, plausible): an anonymous GET
answering 302, 401 or 403 means the certificate is served and IAP stands in
front of the backends. It cannot see whether the installer's own IAP grant
has spread yet; a 403 for the installer after this wait is the identity
steps' to retry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .. import swarm_context
from ..exitcodes import MET, UNMET, WAITING
from ..waits import CERTIFICATE, WaitSpec
from . import Observation, Plan, open_page
from .preflight import ASK, COMPUTE, GCP, USAGE, ApiError, Ask, Gcp, project_number, remember

#: The frontend module's names (terraform/modules/frontend/main.tf:101, :297,
#: `${var.name_prefix}-ui` with the default prefix `swarm`).
FRONTEND = "swarm-ui"
ADDRESS = f"{FRONTEND}-ip"
CERT = f"{FRONTEND}-cert"
DESKTOP_SECRET = "swarm-iap-desktop-client-secret"
CLIENT_ID = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")

#: The console answering behind IAP right after infra pass 2 (certificate, IAP,
#: routing). Defined here and re-exported by `handoff`, which imports this
#: module for the client steps it registers after the hand-off.
CONSOLE_ANSWERS = WaitSpec(
    reason="the console answering behind IAP",
    expected="up to 10 min",
    expected_s=10 * 60,
    bound_s=60 * 60,
    look_at=("the project's SSL certificates page in the Google Cloud console (opened when this wait gives up): "
             "the certificate's status and its domain's status say whether DNS or provisioning is behind; "
             "the countdown above says what the console answered last"),
    interval_s=15,
)
#: What an unauthenticated GET of an IAP-protected console answers: a redirect
#: to sign in (or a 401/403 for a client IAP will not redirect).
IAP_ANSWERS = (302, 401, 403)

DNS_API = "https://dns.googleapis.com/dns/v1"
#: Google Public DNS over HTTPS, JSON form: the resolver the page `edge.dns`
#: opens shows, so what the person sees and what the engine polls agree.
PUBLIC_RESOLVER = "https://dns.google/resolve"
#: The A record's TTL, as the printed record has always said.
RECORD_TTL = 300
#: Typed at the DNS approval instead of the hostname: the person sets the record
#: at their DNS provider themselves, as when no zone covers the hostname.
MANUAL = "manual"
#: `generateServiceIdentity` answers a long-running operation; it is polled
#: this often, this many times (it is normally done on the first answer).
IDENTITY_POLL_S = 2
IDENTITY_POLLS = 30
#: `addresses.insert` answers a Compute operation; it is polled this often,
#: this many times, before the address is re-checked (audit N19).
ADDRESS_POLL_S = 2
ADDRESS_POLLS = 60

def _dns_look_at(ctx: Any) -> str:
    """Google Public DNS's answer for the real hostname, and the address it must hold."""
    outputs = getattr(ctx, "outputs", None) or {}
    hostname = getattr(getattr(ctx, "state", None), "hostname", None) or outputs.get("hostname")
    address = outputs.get("global_ip") or "the reserved address (edge.global-ip)"
    if not hostname:
        return f"the hostname's A record must answer {address} and nothing else"
    return (f"{PUBLIC_RESOLVER}?name={hostname}&type=A must answer {address} and nothing else; the record "
            "is set where the step above said (the project's Cloud DNS zone, or your DNS provider)")


#: plan §9.1: DNS, 60 min.
DNS_WAIT = WaitSpec(
    reason="the hostname's A record resolving to the reserved address",
    expected="a few minutes after the record is saved",
    expected_s=10 * 60,
    bound_s=60 * 60,
    look_at=_dns_look_at,
    interval_s=30,
)


class EdgeApi:
    """The Cloud DNS, Service Usage and Google Public DNS calls stage 2 makes
    that `preflight.Gcp` has no method for. The authenticated ones go through
    its one REST session, so errors are the same `ApiError`; the public
    resolver is asked anonymously."""

    def __init__(self, gcp: Callable[[], Gcp], http_get: Callable[..., Any] | None = None) -> None:
        self._gcp = gcp
        self._http_get = http_get

    def managed_zones(self, project: str) -> list[dict[str, Any]]:
        """Cloud DNS `managedZones.list` for the project."""
        return self._gcp()._pages(f"{DNS_API}/projects/{project}/managedZones", "managedZones")

    def a_records(self, project: str, zone: str, name: str) -> list[dict[str, Any]]:
        """Cloud DNS `resourceRecordSets.list`, the A records named `name` (a fully qualified name)."""
        return self._gcp()._pages(f"{DNS_API}/projects/{project}/managedZones/{zone}/rrsets", "rrsets",
                                  {"name": name, "type": "A"})

    def change_records(self, project: str, zone: str, change: Mapping[str, Any]) -> dict[str, Any]:
        """Cloud DNS `changes.create`: the deletions and additions apply together."""
        return self._gcp()._call("POST", f"{DNS_API}/projects/{project}/managedZones/{zone}/changes",
                                 body=dict(change))

    def generate_service_identity(self, project: str, service: str) -> dict[str, Any]:
        """Service Usage `services.generateServiceIdentity`: idempotent; answers an operation."""
        return self._gcp().generate_service_identity(project, service) or {}

    def operation(self, name: str) -> dict[str, Any]:
        return self._gcp()._call("GET", f"{USAGE}/v1beta1/{name}") or {}

    def global_operation(self, project: str, name: str) -> dict[str, Any]:
        """Compute `globalOperations.get`."""
        return self._gcp()._call("GET", f"{COMPUTE}/projects/{project}/global/operations/{name}") or {}

    def _get(self) -> Callable[..., Any]:
        if self._http_get is not None:
            return self._http_get
        import requests

        return requests.get

    def http_status(self, url: str) -> int | None:
        """The status an anonymous GET of `url` answers, redirects not
        followed; `None` when it could not be asked (a TLS error while the
        certificate is not served, a refused connection, a timeout)."""
        try:
            return int(self._get()(url, timeout=30, allow_redirects=False).status_code)
        except Exception:  # noqa: BLE001 - an answer that cannot be had yet is polled again, not fatal
            return None

    def resolve(self, hostname: str) -> set[str] | None:
        """The IPv4 addresses Google Public DNS answers for `hostname` now
        (empty when it answers none); `None` when it could not be asked."""
        get = self._get()
        try:
            response = get(PUBLIC_RESOLVER, params={"name": hostname, "type": "A"}, timeout=30)
            if response.status_code >= 400:
                return None
            answer = response.json()
        except Exception:  # noqa: BLE001 - a resolver that cannot be asked is polled again, not fatal
            return None
        if answer.get("Status") not in (0, 3):  # NOERROR, NXDOMAIN; SERVFAIL and the rest: ask again
            return None
        return {str(a.get("data")) for a in answer.get("Answer") or () if a.get("type") == 1}


class _Base:
    stage = 2
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, edge: Callable[[], EdgeApi] | None = None) -> None:
        self._gcp = gcp
        self._ask = ask
        self._edge = edge

    @property
    def gcp(self) -> Gcp:
        return self._gcp()

    @property
    def edge(self) -> EdgeApi:
        return self._edge() if self._edge is not None else EdgeApi(self._gcp)

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


#: A person working through Admin › Setup's Sign-in client step: three
#: Google Cloud console pages and a form (issue #2964).
SIGN_IN_CLIENT = WaitSpec(
    reason="you saving the Desktop sign-in client in the console's Admin › Setup",
    expected="5 to 10 min",
    expected_s=10 * 60,
    bound_s=6 * 60 * 60,
    look_at="the console's Admin › Setup page, step Sign-in client; run swarm install again to resume",
    interval_s=10,
)
#: The console document Admin › Setup's Sign-in client step writes
#: (`swarm-install/<env>/console/sign_in_client`: `client_id`, `saved_at`, `saved_by`).
SIGN_IN_DOCUMENT = "sign_in_client"


def console_client(ctx: Any) -> dict[str, Any]:
    """The client the console saved, `{}` when none (or not a client id)."""
    found = dict(ctx.read_console(SIGN_IN_DOCUMENT) or {})
    return found if CLIENT_ID.match(str(found.get("client_id") or "")) else {}


def client_pending(ctx: Any) -> str:
    """Why the recorded client is not the one the console saved last, or ""."""
    saved = console_client(ctx)
    if not saved:
        return ""
    if saved["client_id"] != ctx.outputs.get("desktop_client_id"):
        return f"the console saved the Desktop client {saved['client_id']}; it is not taken in yet"
    if str(saved.get("saved_at") or "") != str(ctx.outputs.get("desktop_client_saved_at") or ""):
        return "the console saved the Desktop client's secret again; it is not taken in yet"
    return ""


def take_client(ctx: Any, gcp: Gcp, contexts: swarm_context.Run) -> None:
    """The console's client into the state, and its secret's latest version,
    read with the person's own credentials, into the swarm credential store
    of the new deployment's context (`verify_context`), so verify never asks.
    The secret is never printed, logged or written to the state."""
    saved = console_client(ctx)
    if not saved:
        raise LookupError("the console has not saved a Desktop client (Admin › Setup, Sign-in client)")
    version = gcp.enabled_version(ctx.project, DESKTOP_SECRET)
    if version is None:
        raise LookupError(f"secret {DESKTOP_SECRET} has no enabled version yet")
    remember(ctx, outputs={"desktop_client_id": saved["client_id"]})
    value = bytearray(gcp.access_secret_version(ctx.project, DESKTOP_SECRET, version))
    try:
        name = swarm_context.store_desktop_secret(ctx, bytes(value), run=contexts)
    finally:
        value[:] = b"\0" * len(value)
    remember(ctx, outputs={"verify_context": name, "desktop_client_secret_version": version,
                           "desktop_client_saved_at": str(saved.get("saved_at") or "")})


class ClientSecretContainerStep(_Base):
    id = "edge.client-secret-container"
    satisfies = ("secret.swarm-iap-desktop-client-secret.container",)
    _explain = (
        "The Desktop client's secret is typed in the console's Admin › Setup (Sign-in client) and swarm-api "
        "adds it as a version of this secret, the one secret infra pass 2 lets swarm-api add versions to. "
        "The container is made here, empty, so the grant has a secret to name."
    )

    def _check(self, ctx: Any) -> Observation:
        if self.gcp.secret(ctx.project, DESKTOP_SECRET) is None:
            return Observation(UNMET, f"secret {DESKTOP_SECRET} does not exist")
        return Observation(MET, f"secret {DESKTOP_SECRET} exists")

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"create the empty secret {DESKTOP_SECRET}",
                    ("its version comes from the console's Sign-in client step, never from this terminal",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.gcp.create_secret(ctx.project, DESKTOP_SECRET, {"managed-by": "swarm-install"})


class BrandStep(_Base):
    id = "edge.brand"
    stage = 13
    kind = "wait"
    wait = SIGN_IN_CLIENT
    needs_project = False
    satisfies = ("oauth-brand.iap",)
    _explain = (
        "sc's sign-in needs an OAuth brand and a Desktop client. No API creates or reads either: Google shut "
        "the IAP OAuth Admin API down on 2026-03-19. The console's Admin › Setup (Sign-in client, its first "
        "step) links Branding, Audience (Internal) and Create client, and takes the client's id and secret "
        "in a form; the console refuses to create a client before Branding and Audience are set, so the "
        "client is the brand's proof. The browser does not need the brand: IAP signs the organisation's own "
        "people in with its Google-managed OAuth client. This waits for the console, and asks nothing here."
    )

    def _check(self, ctx: Any) -> Observation:
        client = str(ctx.outputs.get("desktop_client_id") or "")
        saved = console_client(ctx)
        if CLIENT_ID.match(client):
            return Observation(MET, f"the Desktop client {client} exists, so the OAuth brand does")
        if saved:
            return Observation(MET, f"the console saved the Desktop client {saved['client_id']}, so the OAuth "
                                    "brand exists")
        return Observation(WAITING, "waiting for the console's Admin › Setup step Sign-in client (Branding, "
                                    "Audience: Internal, then the Desktop client)")

    def deep_link(self, ctx: Any) -> str:
        return f"https://console.cloud.google.com/auth/branding?project={ctx.project}"

    def audience_link(self, ctx: Any) -> str:
        return f"https://console.cloud.google.com/auth/audience?project={ctx.project}"

    def plan(self, ctx: Any) -> Plan:
        return Plan(
            "wait for the console's Sign-in client step (the OAuth brand and the Desktop client)",
            (
                "the console's Admin › Setup, step Sign-in client, links "
                f"{self.deep_link(ctx)} (App name SwarmCloud; support email your own address), "
                f"{self.audience_link(ctx)} (Internal) and the Create client page (Desktop app, SwarmCloud sc)",
                "nothing is asked in this terminal",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        """Nothing to make or ask here: the hand-off opened the console, whose
        Sign-in client step sends the person to each page; the engine polls
        the install record."""
        ctx.events.emit("step_apply", step=self.id,
                        message="waiting for the console's Admin › Setup step Sign-in client")


class IapAgentStep(_Base):
    id = "edge.iap-agent"
    satisfies = ("other.iap-service-agent",)
    _explain = (
        "IAP invokes Cloud Run as its own service agent; without it every request through the load "
        "balancer answers 'The IAP service account is not provisioned' (lesson 17). The agent's account "
        "lives in a Google-owned project no customer can read, so generateServiceIdentity, which is "
        "idempotent and answers the agent's email, is both the apply and the proof."
    )

    def _check(self, ctx: Any) -> Observation:
        email = str(ctx.outputs.get("iap_service_agent") or "")
        if not email:
            return Observation(UNMET, "generateServiceIdentity for iap.googleapis.com has not answered yet")
        return Observation(MET, "", {"iap_service_agent": email})

    def plan(self, ctx: Any) -> Plan:
        return Plan("create IAP's service agent (generateServiceIdentity; idempotent) and record its email",
                    ("generateServiceIdentity iap.googleapis.com",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        edge = self.edge
        answer = edge.generate_service_identity(ctx.project, "iap.googleapis.com")
        polls = 0
        while answer.get("name") and answer.get("done") is False:
            if polls >= IDENTITY_POLLS:
                raise TimeoutError(f"generateServiceIdentity for iap.googleapis.com did not finish "
                                   f"({answer.get('name')}); run swarm install again to carry on")
            ctx.clock.sleep(IDENTITY_POLL_S)
            polls += 1
            answer = edge.operation(str(answer["name"]))
        if answer.get("error"):
            error = answer["error"]
            raise RuntimeError(f"generateServiceIdentity for iap.googleapis.com failed: "
                               f"{error.get('message') or error}")
        # v1beta1 answers an operation whose `response` is the ServiceIdentity;
        # an answer that is the ServiceIdentity itself is read the same way.
        email = str((answer.get("response") or {}).get("email") or answer.get("email") or "")
        if not email:
            # Done with no email in the answer: the agent's documented address.
            email = f"service-{self._number(ctx)}@gcp-sa-iap.iam.gserviceaccount.com"
        remember(ctx, outputs={"iap_service_agent": email})


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
        """`addresses.insert` answers an operation before the address exists;
        it is polled until done, so the engine's re-check sees the address
        (audit N19). An answer with no operation is re-checked at once."""
        answer = self.gcp.create_global_address(ctx.project, {
            "name": ADDRESS,
            "addressType": "EXTERNAL",
            "ipVersion": "IPV4",
            "labels": {"managed-by": "swarm-terraform"},
        }) or {}
        polls = 0
        while answer.get("name") and answer.get("status", "DONE") != "DONE":
            if polls >= ADDRESS_POLLS:
                raise TimeoutError(f"reserving {ADDRESS} did not finish ({answer.get('name')} is "
                                   f"{answer.get('status')}); run swarm install again to carry on")
            ctx.clock.sleep(ADDRESS_POLL_S)
            polls += 1
            answer = self.edge.global_operation(ctx.project, str(answer["name"]))
        errors = (answer.get("error") or {}).get("errors") or ()
        if errors:
            raise RuntimeError(f"reserving {ADDRESS} failed: "
                               + "; ".join(str(e.get("message") or e.get("code") or e) for e in errors))


def _reserved_ip(step: _Base, ctx: Any) -> str:
    ip = ctx.outputs.get("global_ip")
    if not ip:
        found = step.gcp.global_address(ctx.project, ADDRESS)
        ip = (found or {}).get("address")
    if not ip:
        raise LookupError(f"global address {ADDRESS} is not reserved (edge.global-ip)")
    return str(ip)


def dns_problem(step: _Base, ctx: Any) -> str | None:
    """Why the hostname does not (only) resolve to the reserved address, as
    Google Public DNS answers now, or None."""
    hostname, ip = step._hostname(ctx), _reserved_ip(step, ctx)
    found = step.edge.resolve(hostname)
    if found is None:
        return f"Google Public DNS ({PUBLIC_RESOLVER}) did not answer for {hostname}; asking again"
    if found == {ip}:
        return None
    return f"{hostname} resolves to {sorted(found) or 'nothing'}, not {ip}"


def _fqdn(name: str) -> str:
    return name.rstrip(".").lower() + "."


def covering_zone(zones: list[dict[str, Any]], hostname: str) -> dict[str, Any] | None:
    """The public managed zone whose DNS name is the longest suffix of `hostname`, or None."""
    host = _fqdn(hostname)
    covering = [z for z in zones
                if str(z.get("visibility") or "public") == "public" and z.get("dnsName")
                and (host == _fqdn(str(z["dnsName"])) or host.endswith("." + _fqdn(str(z["dnsName"]))))]
    return max(covering, key=lambda z: len(str(z["dnsName"])), default=None)


class DnsStep(_Base):
    id = "edge.dns"
    kind = "wait"
    wait = DNS_WAIT
    satisfies = ("dns.frontend-hostname",)
    _explain = (
        "Google issues the managed certificate only once the hostname resolves to the load balancer's "
        "address; until then it stays FAILED_NOT_VISIBLE (lesson 17). When the project's Cloud DNS holds "
        "the hostname's zone the engine creates the record there, after a typed approval; otherwise it is "
        "set at the person's DNS provider. Either way it is polled through Google Public DNS."
    )

    def _check(self, ctx: Any) -> Observation:
        problem = dns_problem(self, ctx)
        return Observation(WAITING, problem) if problem else Observation(MET, f"{self._hostname(ctx)} resolves")

    def _record(self, ctx: Any) -> str:
        return f"{self._hostname(ctx)}.  {RECORD_TTL}  IN  A  {_reserved_ip(self, ctx)}"

    def zone(self, ctx: Any) -> dict[str, Any] | None:
        """The project's public Cloud DNS zone covering the hostname, or None:
        none covers it, or the zones cannot be listed (Cloud DNS off, or no
        right to list them), and the record is then set by hand."""
        try:
            zones = self.edge.managed_zones(ctx.project)
        except ApiError:
            return None
        return covering_zone(zones, self._hostname(ctx))

    def plan(self, ctx: Any) -> Plan:
        record = self._record(ctx)
        zone = self.zone(ctx)
        if zone is not None:
            return Plan(
                f"create the hostname's A record in Cloud DNS zone {zone.get('name')} after your typed approval "
                "(the hostname); the install continues once it resolves",
                (record, f"Cloud DNS zone {zone.get('name')} ({zone.get('dnsName')}) in {ctx.project}"),
            )
        return Plan(
            "set the hostname's A record at your DNS provider; the install continues once it resolves",
            (record,),
        )

    def deep_link(self, ctx: Any) -> str:
        """What the hostname resolves to now, as Google's public resolvers see it.

        The record itself is set at the person's registrar, which the engine
        cannot know, so the page it opens is the one that shows the change
        landing; the engine polls the same resolver (`PUBLIC_RESOLVER`)."""
        return f"https://toolbox.googleapps.com/apps/dig/#A/{self._hostname(ctx)}"

    def apply(self, ctx: Any, plan: Plan) -> None:
        """In the project's Cloud DNS zone, the record is created after one
        typed approval; else it is set at the person's DNS provider: print it,
        open the page that shows it landing, and let the engine poll."""
        zone = self.zone(ctx)
        if zone is None or not self._create(ctx, zone):
            self._by_hand(ctx)

    def _create(self, ctx: Any, zone: dict[str, Any]) -> bool:
        """Create (or correct) the A record in `zone`; False when the person
        chose to set it themselves."""
        hostname, ip = self._hostname(ctx), _reserved_ip(self, ctx)
        name, zone_name = _fqdn(hostname), str(zone["name"])
        edge = self.edge
        existing = edge.a_records(ctx.project, zone_name, name)
        if [r.get("rrdatas") for r in existing] == [[ip]]:
            return True  # already there: the engine polls until it resolves
        replacing = "".join(f"\n  replacing {r.get('name')} {r.get('ttl')} IN A {' '.join(r.get('rrdatas') or ())}"
                            for r in existing)
        typed = self._ask(ctx, self.id, (
            f"The installer will {'replace' if existing else 'create'} this record in Cloud DNS zone {zone_name} "
            f"({zone.get('dnsName')}) of {ctx.project}:\n  {self._record(ctx)}{replacing}\n"
            f"Type the hostname ({hostname}) to approve, or {MANUAL} to set it at your DNS provider yourself"
        )).strip()
        if typed == MANUAL:
            return False
        if typed != hostname:
            raise PermissionError(f"not confirmed (typed {typed!r}, not {hostname!r}); no DNS record was changed")
        edge.change_records(ctx.project, zone_name, {
            "deletions": existing,
            "additions": [{"name": name, "type": "A", "ttl": RECORD_TTL, "rrdatas": [ip]}],
        })
        remember(ctx, outputs={"dns_zone": zone_name})
        return True

    def _by_hand(self, ctx: Any) -> None:
        open_page(self.deep_link(ctx), why=(
            f"Set this A record at your DNS provider: {self._record(ctx)}\n"
            f"This page shows what {self._hostname(ctx)} resolves to now; the install continues by itself "
            f"once it resolves to {_reserved_ip(self, ctx)} (nothing to press)."
        ))


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
                                    "pass 1 attaches it (stage 5), and edge.console-answers waits for ACTIVE "
                                    "before stage 8's first call through the hostname",
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
    stage = 13
    kind = "wait"
    wait = SIGN_IN_CLIENT
    needs_project = False
    satisfies = (
        "oauth-client.desktop-sc",
        "declared-value.frontend_iap_programmatic_clients",
    )
    _explain = (
        "sc signs a person in to IAP with this Desktop OAuth client (a gcloud token is refused with 401 "
        "code 900). No API creates one, so the console's Admin › Setup (Sign-in client) takes its id and "
        "secret in a form; the secret goes to Secret Manager only. This waits for that, then reads the "
        "secret with your own credentials into the swarm credential store of the new deployment's context, "
        "which verify signs in to, and asks nothing here."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask,
                 contexts: swarm_context.Run = swarm_context.run_command,
                 edge: Callable[[], EdgeApi] | None = None) -> None:
        super().__init__(gcp, ask, edge)
        self._contexts = contexts

    def _stored_context(self, ctx: Any) -> tuple[str, str]:
        """`(context, "")` when the new deployment's context, with the secret,
        is on this machine; else `("", why not)`. Reads the context list only."""
        try:
            name = swarm_context.stored_context(ctx, self._contexts)
        except (RuntimeError, ValueError) as exc:
            return "", str(exc)
        if not name:
            return "", ("the Desktop client's secret is not in the swarm credential store of a context for "
                        f"{swarm_context.deployment_url(ctx) or 'the new deployment'} on this machine")
        return name, ""

    def _check(self, ctx: Any) -> Observation:
        pending = client_pending(ctx)
        if pending:
            return Observation(UNMET, pending)
        client = str(ctx.outputs.get("desktop_client_id") or "")
        if not CLIENT_ID.match(client):
            return Observation(WAITING, "waiting for the console's Admin › Setup step Sign-in client")
        if self.gcp.secret(ctx.project, DESKTOP_SECRET) is None:
            return Observation(UNMET, f"secret {DESKTOP_SECRET} does not exist")
        version = self.gcp.enabled_version(ctx.project, DESKTOP_SECRET)
        if version is None:
            return Observation(UNMET, f"secret {DESKTOP_SECRET} has no enabled version")
        context, missing = self._stored_context(ctx)
        if not context:
            return Observation(UNMET, missing)
        return Observation(MET, "", {"desktop_client_id": client, "desktop_client_secret_version": version,
                                     "verify_context": context})

    def plan(self, ctx: Any) -> Plan:
        return Plan(
            "take the Desktop client the console's Sign-in client step saved",
            (
                f"read the latest version of {DESKTOP_SECRET} with your own credentials",
                "write it to the swarm credential store of the new deployment's context (swarm context add "
                "--client-secret-stdin); never to a file, a log or the install state",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        """The console's client, or a recorded one whose secret this machine's
        credential store lacks (another laptop), is taken from Secret Manager;
        with neither, the engine polls until the console saves one."""
        if not console_client(ctx) and not CLIENT_ID.match(str(ctx.outputs.get("desktop_client_id") or "")):
            return
        if console_client(ctx):
            take_client(ctx, self.gcp, self._contexts)
            return
        version = self.gcp.enabled_version(ctx.project, DESKTOP_SECRET)
        if version is None:
            # The check says so; the engine polls until the console saves
            # the client again (Admin › Setup, step Sign-in client).
            return
        value = bytearray(self.gcp.access_secret_version(ctx.project, DESKTOP_SECRET, version))
        try:
            name = swarm_context.store_desktop_secret(ctx, bytes(value), run=self._contexts)
        finally:
            value[:] = b"\0" * len(value)
        remember(ctx, outputs={"verify_context": name, "desktop_client_secret_version": version})


class SignInRecordStep(_Base):
    """Triggered by the hand-off wait once the console's Sign-in client step
    saved a client this state has not taken in (`handoff.sign_in_triggered`)."""

    id = "handoff.sign-in-client"
    stage = 13
    needs_project = False
    _explain = ("Once the console saved the Desktop client, its id goes to the install state and its secret, "
                "read with your own credentials, to the new deployment's swarm credential store.")

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask,
                 contexts: swarm_context.Run = swarm_context.run_command) -> None:
        super().__init__(gcp, ask)
        self._contexts = contexts

    def trigger(self, ctx: Any) -> bool:
        return bool(client_pending(ctx)) and self.gcp.enabled_version(ctx.project, DESKTOP_SECRET) is not None

    def _check(self, ctx: Any) -> Observation:
        pending = client_pending(ctx)
        if pending:
            return Observation(UNMET, pending)
        if not console_client(ctx):
            return Observation(UNMET, "the console has not saved a Desktop client yet")
        return Observation(MET, f"the Desktop client {ctx.outputs.get('desktop_client_id')} is taken in")

    def plan(self, ctx: Any) -> Plan:
        return Plan("take the Desktop client the console saved",
                    (f"read the latest version of {DESKTOP_SECRET} with your own credentials into the new "
                     "deployment's swarm credential store",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        take_client(ctx, self.gcp, self._contexts)


class ConsoleAnswersStep(_Base):
    id = "edge.console-answers"
    stage = 8
    kind = "wait"
    wait = CONSOLE_ANSWERS
    satisfies = ("other.console-through-iap",)
    _explain = (
        "Stage 8's first step calls swarm-api through https://<hostname>. Infra pass 1 attached the managed "
        "certificate, but Google provisions it for up to an hour, and the load balancer and IAP take minutes "
        "more after pass 2; a call before then fails on TLS or a 404. This waits, first in stage 8, until the "
        "certificate is ACTIVE and the hostname answers with IAP's sign-in (audit N17)."
    )

    def _check(self, ctx: Any) -> Observation:
        found = self.gcp.ssl_certificate(ctx.project, CERT)
        if found is None:
            return Observation(UNMET, f"{CERT} does not exist (infra pass 1 creates it)")
        status = (found.get("managed") or {}).get("status")
        if status != "ACTIVE":
            return Observation(WAITING, f"{CERT} is {status or 'not provisioned yet'}")
        url = f"https://{self._hostname(ctx)}/"
        answered = self.edge.http_status(url)
        if answered is None:
            return Observation(WAITING, f"{CERT} is ACTIVE; {url} does not answer over TLS yet")
        if answered not in IAP_ANSWERS:
            return Observation(WAITING, f"{CERT} is ACTIVE; {url} answers {answered}, not IAP's sign-in "
                                        f"({', '.join(map(str, IAP_ANSWERS))})")
        return Observation(MET, f"{CERT} is ACTIVE and {url} answers through IAP ({answered})")

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"wait (up to 60 min) for {CERT} to be ACTIVE and https://<hostname>/ to answer through IAP",
                    (f"poll {CERT}'s managed.status and an anonymous GET of https://<hostname>/",))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """Nothing to make: Google finishes the certificate and the load
        balancer by itself, and the engine polls the check."""


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK,
                edge: Callable[[], EdgeApi] | None = None,
                contexts: swarm_context.Run = swarm_context.run_command) -> list[Any]:
    """Stage 2 in order: the Desktop client secret's empty container, the IAP
    agent, the address before DNS before the certificate; then the console
    wait, first in stage 8. The brand and the Desktop client are waits after
    the hand-off (`handoff.build_steps` registers `client_steps`)."""
    return [
        ClientSecretContainerStep(gcp, ask, edge),
        IapAgentStep(gcp, ask, edge),
        GlobalIpStep(gcp, ask, edge),
        DnsStep(gcp, ask, edge),
        CertificateStep(gcp, ask, edge),
        ConsoleAnswersStep(gcp, ask, edge),
    ]


def client_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK,
                 edge: Callable[[], EdgeApi] | None = None,
                 contexts: swarm_context.Run = swarm_context.run_command) -> list[Any]:
    """The brand and the Desktop client, waits met by the console's record."""
    return [BrandStep(gcp, ask, edge), DesktopClientStep(gcp, ask, contexts, edge=edge)]


STEPS = build_steps()
