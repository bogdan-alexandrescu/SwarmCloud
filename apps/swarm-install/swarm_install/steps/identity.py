"""Stage 8, identity wiring: the organisation's groups exist, swarm-api can
read Google groups, and a group is registered only once swarm-api has read it
(design §8; plan §5.3; lesson 20).

| Step | Manifest entries | Kind |
|---|---|---|
| `identity.groups` | `workspace-group.install-groups` | human only where no API answers: the platform admins' group and, for a team (not one person, decision 7), the first team's group, created through the Cloud Identity Groups API on one optional choice; without the right, the Admin console's groups page opens and the check is polled |
| `identity.groups-reader-role` | `workspace-delegation.groups-reader-assignment` | human only where no API answers: the predefined Groups Reader admin role assigned to swarm-api's account through the Directory API after a typed approval; refused, the Admin console's roles page opens and `roleAssignments.list` is polled |
| `identity.group-read` | `workspace-group.cloud-identity-presence`, `workspace-group.tenant-groups`, `workspace-delegation.groups-reader-admin-role`, `workspace-delegation.groups-dwd` | human: the admin-role path first (Q2), delegation the fallback (its Admin console page opens with the client id and scope, and the stage-8 re-apply runs before the poll); polls the probe through swarm-api |
| `identity.admin-group` | `workspace-group.admin-surface-swarm-admins` | engine: the platform admins' group, registered only after its probe passes |
| `identity.qa-console` | `switch.enable_qa_identity`, `sa.qa-console`, `role.custom.swarmQaJwtSigner`, `iam-binding.qa-console-authoritative-policy` | engine, read-only: the live-browser QA identity terraform/bootstrap creates (`qa_identity.tf`) is exactly as designed |
| `identity.allowed-domains` | `declared-value.allowed-domains-solo` | engine, read-only: swarm-api's `ALLOWED_DOMAINS` is this organisation's domain, never a consumer domain |

**Through the APIs first, a page only where none answers.** The groups are
created with `groups.create` under `customers/<id>` (the Directory API's
`customers/my_customer`), the caller as each group's owner, so the installer is
the member the probes name. The Groups Reader role is a grant, so it is
assigned only after a typed approval, with `roleAssignments.insert` on
swarm-api's numeric unique id. When Google refuses either (the person is not a
Workspace admin, or their sign-in does not carry the Admin SDK's scopes), the
step opens the Admin console's page with `open_page`, says what to do there and
polls its own check; the person never presses Enter.

**What stage 8 reads but does not write.** qa-console's account, role and
policy are terraform/bootstrap resources and `ALLOWED_DOMAINS` is terraform/
infra's `allowed_domains`: the engine's Terraform passes (DAYONE-ENGINE-3)
write them. Stage 8 checks them where the design places them, and never
writes them a second way.

**The stage-8 re-apply** (DAYONE-ENGINE-INPUTS) is the Terraform pass,
registered at stage 8, that applies `allowed_domains`, the admin groups and
`enable_qa_identity` once the groups above exist. Until it has run, both
read-only checks are `waiting` for it, never failing; their apply runs that
pass through the engine (it is a stage-8 step with a `pass_name`, whatever its
id), and they then check and pass. With no such pass registered they check
directly.

**The probe runs through swarm-api, in the mode swarm-api runs** (review
finding 8). The engine never calls the Groups API itself: what matters is
whether swarm-api, with its own identity and its `GROUPS_AUTH`, can answer
`checkTransitiveMembership`. So every probe is `POST
/v1/admin/setup/health/groups-probe`, called as `swarm-tf-deployer`
(`api_client`), and its answer names the mode it ran in.

**The 503 trap** (`docs/multi-tenancy.md`): a registered group swarm-api cannot
read fails its members' requests. The engine records a group in
`admin_groups` (which infra writes into `admin_pool_users` and
`frontend_iap_members`) only after the probe for that group is `met`; a failed
probe registers nothing.

**The Directory scopes when stage 0 has none** (audit N14). Google may refuse
the Directory scopes to gcloud's own client at stage 0, which then records
`directory_scopes` false and goes on. The first step here that calls the
Directory API then signs the person in through the deployment's own Desktop
client (stage 2 made it; its id is `desktop_client_id` and its secret is in the
new context's swarm credential store, `verify_context`): `open_page` opens the
consent page for the two Directory scopes, a listener on 127.0.0.1 takes the
redirect, and the access token goes into an in-memory session used for the
Directory calls only (`IdentityGcp.use_directory`). No refresh token is asked
for, and the token is never written to the state, a file, an event or a log.
A check never signs in: until an apply has, it reads swarm-api's own probe.
A refused sign-in is a refusal like any other: the Admin console's page opens.

**B04, lesson 20:** an `unauthorized_client` answer is the Admin console (the
delegation or the admin role was never granted there), never IAM; only a
`signJwt` refusal is a Google Cloud grant.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import secrets
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .. import api_client, secret_entry
from ..api_client import ADMIN_ROLE, DELEGATION, ApiClient, GroupResult
from ..exitcodes import MET, UNMET, WAITING
from ..waits import WaitSpec
from . import Observation, Plan, open_page
from .preflight import ASK, EMAIL, GCP, IAM, ApiError, Ask, Gcp, directory_declined, remember

GROUPS_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"
SWARM_API_ACCOUNT = "swarm-api"

ADMIN_ROLE_PATH = "admin.google.com > Account > Admin roles > Groups Reader > Admins > Assign service accounts"
DELEGATION_PATH = "admin.google.com > Security > Access and data control > API controls > Domain-wide delegation > Add new"

#: The person's answer when asked which path they took, by mode.
PATHS = {"admin-role": ADMIN_ROLE, "delegation": DELEGATION}

#: An Admin console grant takes effect in minutes, occasionally longer.
GRANT_WAIT = WaitSpec(
    reason="the Admin console grant reaching the Groups API",
    expected="1 to 10 min after it is saved",
    expected_s=10 * 60,
    bound_s=45 * 60,
    look_at="the Admin console path above, for swarm-api's account: the role must be saved there; a saved "
            "grant takes up to 10 min to reach the Groups API",
    interval_s=15,
)

#: Creating a group in the Admin console, or its creation reaching the Groups API.
GROUPS_WAIT = WaitSpec(
    reason="the groups being created in the Admin console",
    expected="1 to 5 min after they are saved",
    expected_s=5 * 60,
    bound_s=45 * 60,
    look_at="the Admin console's groups page (opened above): each group the step named must be listed there",
    interval_s=15,
)

ApiFactory = Callable[[Any], ApiClient]

ADMIN_DIRECTORY = "https://admin.googleapis.com/admin/directory/v1"
CLOUD_IDENTITY = "https://cloudidentity.googleapis.com/v1"
#: The Admin SDK does not accept cloud-platform alone: the customer read and
#: the role assignment carry their own scopes; groups.create takes cloud-platform.
WORKSPACE_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/admin.directory.customer.readonly",
    "https://www.googleapis.com/auth/admin.directory.rolemanagement",
)

#: The Desktop client's sign-in for the Directory scopes (stage 0 had none).
DIRECTORY_SCOPES = WORKSPACE_SCOPES[1:]
CONSENT_PAGE = "https://accounts.google.com/o/oauth2/v2/auth"
CODE_EXCHANGE = "https://oauth2.googleapis.com/token"
#: Long enough to pick the account and read the consent screen.
DIRECTORY_SIGN_IN_TIMEOUT_S = 10 * 60

#: The Admin console pages a person works on where no API answered.
GROUPS_PAGE = "https://admin.google.com/ac/groups"
ADMIN_ROLES_PAGE = "https://admin.google.com/ac/roles"
DELEGATION_PAGE = "https://admin.google.com/ac/owl/domainwidedelegation"

#: The groups an install creates, under the organisation's domain (`org_domain`).
ADMIN_GROUP = "swarm-admins"
TEAM_GROUP = "swarm-team"
#: The predefined Groups Reader role, by its system name and its display name.
GROUPS_READER_ROLE = "_GROUPS_READER_ROLE"
GROUPS_READER = "Groups Reader"
#: The typed approval for the role assignment, and the fallback's answer.
APPROVE = "ASSIGN"
YES = ("y", "yes")
NO = ("n", "no")

QA_ACCOUNT = "qa-console"
QA_ROLE = "swarmQaJwtSigner"
QA_PERMISSIONS = ["iam.serviceAccounts.signJwt"]
RUN_API = "https://run.googleapis.com/v2"

#: Domains that are every Google account on earth (the manifest's
#: `declared-value.allowed-domains-solo`, docs/deploying-without-an-organisation.md).
CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


class DirectorySignInFailed(LookupError):
    """The Desktop client's sign-in for the Directory scopes did not give a
    token that carries them; the step takes its Admin console page instead."""


class IdentityGcp(Gcp):
    """`preflight.Gcp` plus the reads stage 8's read-only checks make. The
    Directory calls go through the Desktop client's session once one is
    handed in (`use_directory`), else through the application-default one."""

    _directory: Gcp | None = None

    def use_directory(self, session: Any) -> None:
        """The session the Desktop client's sign-in made, for the Directory
        calls only; held in this process's memory, never written anywhere."""
        self._directory = Gcp(session)

    @property
    def directory_ready(self) -> bool:
        """A Directory session is held and its token has not expired."""
        if self._directory is None:
            return False
        return not getattr(getattr(self._directory.session, "credentials", None), "expired", False)

    @property
    def _admin(self) -> Gcp:
        return self._directory or self

    def custom_role(self, project: str, role: str) -> dict[str, Any] | None:
        return self._call("GET", f"{IAM}/projects/{project}/roles/{role}", missing_ok=True)

    def service_account_policy(self, email: str) -> dict[str, Any]:
        return self._call("POST", f"{IAM}/projects/-/serviceAccounts/{email}:getIamPolicy", body={}) or {}

    def run_service(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{RUN_API}/projects/{project}/locations/{region}/services/{name}", missing_ok=True)

    @property
    def session(self) -> Any:
        if self._session is None:
            import google.auth  # lazy: no credentials needed to import the engine
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=list(WORKSPACE_SCOPES))
            self._session = AuthorizedSession(credentials)
        return self._session

    def _billed(self, project: str, method: str, url: str, *, body: Any = None, params: Any = None,
                missing_ok: bool = False) -> Any:
        """A Cloud Identity call, billed to the install's project: user
        credentials need `x-goog-user-project` there (docs/multi-tenancy.md)."""
        response = self.session.request(method, url, json=body, params=params, timeout=60,
                                        headers={"x-goog-user-project": project})
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            raise ApiError(response.status_code, str(error.get("status") or ""),
                           str(error.get("message") or response.reason))
        return response.json() if response.content else {}

    # The Directory API (Admin SDK) ---------------------------------------------

    def customer_id(self) -> str:
        """The organisation's Workspace or Cloud Identity customer id (`C0...`)."""
        found = self._admin._call("GET", f"{ADMIN_DIRECTORY}/customers/my_customer") or {}
        customer = str(found.get("id") or "")
        if not customer:
            raise LookupError("the Directory API named no customer for the signed-in account")
        return customer

    def admin_roles(self, customer: str) -> list[dict[str, Any]]:
        return self._admin._pages(f"{ADMIN_DIRECTORY}/customer/{customer}/roles", "items")

    def role_assignments(self, customer: str, role_id: str) -> list[dict[str, Any]]:
        return self._admin._pages(f"{ADMIN_DIRECTORY}/customer/{customer}/roleassignments", "items",
                                  {"roleId": role_id})

    def assign_role(self, customer: str, role_id: str, assigned_to: str) -> dict[str, Any]:
        """`roleAssignments.insert`, organisation-wide, to a numeric unique id
        (a service account's `uniqueId`)."""
        return self._admin._call("POST", f"{ADMIN_DIRECTORY}/customer/{customer}/roleassignments",
                                 body={"roleId": role_id, "assignedTo": assigned_to, "scopeType": "CUSTOMER"}) or {}

    # The Cloud Identity Groups API ---------------------------------------------

    def group_name(self, project: str, email: str) -> str | None:
        """`groups/<id>` for a group's email, or None when there is no such group."""
        found = self._billed(project, "GET", f"{CLOUD_IDENTITY}/groups:lookup",
                             params={"groupKey.id": email}, missing_ok=True)
        return str((found or {}).get("name") or "") or None

    def create_group(self, project: str, parent: str, email: str, display_name: str) -> dict[str, Any]:
        """`groups.create` under `customers/<id>`, the caller its initial owner."""
        return self._billed(project, "POST", f"{CLOUD_IDENTITY}/groups",
                            params={"initialGroupConfig": "WITH_INITIAL_OWNER"},
                            body={"parent": parent, "groupKey": {"id": email}, "displayName": display_name,
                                  "labels": {"cloudidentity.googleapis.com/groups.discussion_forum": ""}}) or {}


@dataclass
class _LazyIdentity:
    """One `IdentityGcp`, built on first use (never at import)."""

    made: list[IdentityGcp] = field(default_factory=list)

    def __call__(self) -> IdentityGcp:
        if not self.made:
            self.made.append(IdentityGcp())
        return self.made[0]


IDENTITY_GCP = _LazyIdentity()


# -- the Directory scopes through the deployment's Desktop client (audit N14) -----


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _post_token(form: Mapping[str, str]) -> dict[str, Any]:
    """The code exchange at Google's token endpoint; an error names only Google's error code."""
    import requests

    try:
        response = requests.post(CODE_EXCHANGE, data=dict(form), timeout=30)
        answer = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise DirectorySignInFailed(f"Google's token endpoint did not answer ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise DirectorySignInFailed(f"Google refused the sign-in code ({answer.get('error') or response.status_code})")
    return answer


def _loopback(answer: dict[str, str], done: threading.Event) -> HTTPServer:
    """A listener on 127.0.0.1 that keeps the query of the first redirect."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # nothing on the terminal mid-flow
            pass

        def do_GET(self) -> None:  # noqa: N802
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if "code" not in params and "error" not in params:
                self.send_response(404)  # a favicon or a pre-fetch, not the redirect
                self.end_headers()
                return
            if not done.is_set():
                answer.update(params)
                done.set()
            said = ("Signed in. You can close this tab; swarm install goes on." if "code" in params
                    else f"The sign-in did not complete: {html.escape(params.get('error', ''))}")
            page = f"<!doctype html><title>SwarmCloud</title><p>{said}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    return HTTPServer(("127.0.0.1", 0), Handler)


def desktop_directory_session(ctx: Any, *, client_id: str, client_secret: str,
                              exchange: Callable[[Mapping[str, str]], dict[str, Any]] = _post_token,
                              timeout: float = DIRECTORY_SIGN_IN_TIMEOUT_S) -> Any:
    """The installed-app flow (PKCE and the client's secret) for the two
    Directory scopes, through the deployment's Desktop client: the consent
    page opens with `open_page`, the redirect comes back to a listener on
    127.0.0.1, and the code is exchanged for an access token. Returns an
    authorized session over that token; no refresh token is asked for and
    nothing is written anywhere."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials

    verifier = _b64url(secrets.token_bytes(48))
    state = _b64url(secrets.token_bytes(24))
    answer: dict[str, str] = {}
    done = threading.Event()
    server = _loopback(answer, done)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(DIRECTORY_SCOPES),
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "state": state,
        "prompt": "consent",
        # Online: Google issues no refresh token, so nothing outlives this run.
        "access_type": "online",
    }
    if installer(ctx):
        query["login_hint"] = installer(ctx)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        open_page(f"{CONSENT_PAGE}?{urllib.parse.urlencode(query)}", why=(
            "Stage 0's Google sign-in went without the Directory scopes, and this step reads your "
            "organisation's Directory (its customer id and admin roles). Sign in on this page and allow "
            "the two read and role-management Directory scopes for this deployment's own client; the "
            "install goes on by itself once you have."))
        if not done.wait(timeout):
            raise DirectorySignInFailed(f"no answer from the browser within {int(timeout)} s")
    finally:
        server.shutdown()
        server.server_close()
    if answer.get("state") != state:
        raise DirectorySignInFailed("the redirect carried another sign-in's state; nothing was used")
    if "error" in answer:
        raise DirectorySignInFailed(f"Google did not grant the Directory scopes: {answer['error']}")
    tokens = exchange({"client_id": client_id, "client_secret": client_secret, "code": answer.get("code", ""),
                       "code_verifier": verifier, "redirect_uri": redirect_uri,
                       "grant_type": "authorization_code"})
    granted = set(str(tokens.get("scope") or "").split())
    token = str(tokens.get("access_token") or "")
    if not token or not set(DIRECTORY_SCOPES) <= granted:
        raise DirectorySignInFailed("Google signed you in without the Directory scopes")
    expires_in = int(tokens.get("expires_in") or 3600)
    # google-auth compares a naive UTC expiry.
    expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=max(expires_in - 60, 0))
    return AuthorizedSession(Credentials(token=token, expiry=expiry, scopes=list(DIRECTORY_SCOPES)))


def _stored_desktop_secret(ctx: Any) -> str:
    """The Desktop client's secret from the new deployment's context in the
    swarm credential store (stage 2 put it there, `verify_context`), else ""."""
    context = str(ctx.outputs.get("verify_context") or "")
    if not context:
        return ""
    try:
        from swarm_mcp import credentials  # the swarm_mcp wheel install.sh installs beside this one
    except ImportError:
        return ""
    try:
        return credentials.store().get(credentials.key(context, credentials.CLIENT_SECRET)) or ""
    except Exception:  # noqa: BLE001 - a store that cannot be read is the hidden prompt's case
        return ""


def directory_sign_in(ctx: Any, *, step_id: str = "identity.directory-sign-in") -> Any:
    """A Directory session through the deployment's Desktop client: its id
    from the state, its secret from the credential store (typed at a hidden
    prompt only when the store has none)."""
    client_id = str(ctx.outputs.get("desktop_client_id") or "")
    if not client_id:
        raise DirectorySignInFailed("no Desktop client id is recorded (edge.desktop-client records it)")
    client_secret = _stored_desktop_secret(ctx) or secret_entry.type_secret(
        ctx, step_id=step_id, what="the Desktop client's secret",
        prompt="The Desktop client's secret, for the Directory sign-in", use=lambda value: value.decode())
    return desktop_directory_session(ctx, client_id=client_id, client_secret=client_secret)


def swarm_api_email(project: str) -> str:
    return f"{SWARM_API_ACCOUNT}@{project}.iam.gserviceaccount.com"


def why_unmet(found: GroupResult, mode: str, project: str, client_id: str | None = None) -> str:
    """One line a person can act on: where to fix it, by the probe's reason."""
    account = swarm_api_email(project)
    if found.reason == "sign_jwt_denied":
        return (f"{found.group}: swarm-api could not sign its delegation JWT (a Google Cloud grant: "
                f"{account} needs iam.serviceAccounts.signJwt on itself)")
    if found.reason in ("unauthorized_client", "permission_denied", "forbidden"):
        if mode == DELEGATION:
            return (f"{found.group}: the Admin console has not authorised swarm-api's client "
                    f"{client_id or '(its numeric unique id)'} for {GROUPS_SCOPE}. This is not IAM: "
                    f"{DELEGATION_PATH} ({DELEGATION_PAGE})")
        return (f"{found.group}: swarm-api's account {account} does not hold the Groups Reader admin role. "
                f"This is not IAM: {ADMIN_ROLE_PATH}")
    if found.reason in ("not_found", "group_not_found"):
        return f"{found.group}: no such group in the organisation's Workspace or Cloud Identity"
    if found.reason == "not_a_member":
        return f"{found.group}: the member named is not in the group; name a member of it"
    return f"{found.group}: {found.state}" + (f" ({found.reason})" if found.reason else "") + (
        f": {found.detail}" if found.detail else "")


class _Base:
    stage = 8
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, api: ApiFactory = api_client.for_install) -> None:
        self._gcp = gcp
        self._ask = ask
        self._api = api

    @property
    def gcp(self) -> Gcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    def _client_id(self, ctx: Any) -> str | None:
        found = self.gcp.service_account(swarm_api_email(ctx.project)) or {}
        return str(found.get("uniqueId") or "") or None

    def _probe(self, ctx: Any, group: str, member: str) -> tuple[GroupResult, str]:
        answer = self._api(ctx).groups_probe([{"group": group, "member": member}])
        return answer.of(group), answer.mode


def installer(ctx: Any) -> str:
    """The person installing: the owner of the groups the install creates."""
    return str(ctx.outputs.get("installer") or getattr(ctx, "holder", "") or "")


def one_person(ctx: Any) -> bool:
    """Decision 7: the deployment is for one person (`sizing.mode`, stage 0).
    A state from before `sizing` existed is a team install."""
    return str((ctx.outputs.get("sizing") or {}).get("mode") or "") == "individual"


def install_groups(ctx: Any) -> dict[str, str]:
    """The platform admins' group and, for a team, the first team's group, by role.

    Names recorded once are kept (a resume, or an admin group registered
    before this step existed); otherwise they are `swarm-admins@` and
    `swarm-team@` the organisation's domain (`org_domain`, preflight). A
    one-person install has no team group: its first workspace is the
    installer's own, and the admin group holds only the installer."""
    recorded = dict(ctx.outputs.get("workspace_groups") or {})
    groups = {"admin": str(recorded.get("admin") or next(iter(ctx.outputs.get("admin_groups") or ()), ""))}
    if not one_person(ctx):
        groups["team"] = str(recorded.get("team") or "")
    if not all(groups.values()):
        domain = str(ctx.outputs.get("org_domain") or "").strip().lower()
        if not domain:
            raise LookupError("no org_domain is recorded (preflight.org-domain reads it)")
        names = {"admin": ADMIN_GROUP, "team": TEAM_GROUP}
        groups = {role: email or f"{names[role]}@{domain}" for role, email in groups.items()}
    return groups


def probe_group(ctx: Any) -> str:
    """The group swarm-api's read is proved on: the first team's, or, with no
    team group (one person), the platform admins'."""
    groups = ctx.outputs.get("workspace_groups") or {}
    return str(groups.get("team") or groups.get("admin") or "")


def described(groups: dict[str, str]) -> str:
    """The groups in words, for a plan or a check."""
    admins = f"the platform admins' group {groups['admin']}"
    return f"{admins} and the first team's group {groups['team']}" if "team" in groups else admins


class _Workspace(_Base):
    """A stage 8 step that writes through the Groups or Directory API."""

    def __init__(self, gcp: Callable[[], IdentityGcp], ask: Ask, api: ApiFactory = api_client.for_install, *,
                 directory: Callable[[Any], Any] | None = None) -> None:
        super().__init__(gcp, ask, api)
        self._directory = directory

    @property
    def gcp(self) -> IdentityGcp:
        return self._gcp()  # type: ignore[return-value]

    def _directory_pending(self, ctx: Any) -> bool:
        """Stage 0 went without the Directory scopes and no Desktop-client
        session is held yet: a Directory call would be refused."""
        return directory_declined(ctx) and not getattr(self.gcp, "directory_ready", False)

    def _directory_access(self, ctx: Any) -> None:
        """Before an apply's Directory calls: sign in through the Desktop
        client when `_directory_pending`. Raises DirectorySignInFailed."""
        if self._directory_pending(ctx):
            sign_in = self._directory or (lambda c: directory_sign_in(c, step_id=self.id))
            self.gcp.use_directory(sign_in(ctx))

    def deep_link(self, ctx: Any) -> None:
        """No page by default: the step opens one itself, only when an API refused."""
        return None


class GroupsStep(_Workspace):
    id = "identity.groups"
    kind = "human"
    wait = GROUPS_WAIT
    satisfies = ("workspace-group.install-groups",)
    _explain = (
        "The admin surface is governed by a group and the first team is a group, never named people; a "
        "one-person install has no team group. The engine creates them through the Cloud Identity Groups "
        "API, the installer as each one's owner; without the right to create groups, the Admin console's "
        "groups page opens and the engine waits until they exist."
    )

    def _missing(self, ctx: Any, groups: dict[str, str]) -> list[str]:
        return [email for email in groups.values() if not self.gcp.group_name(ctx.project, email)]

    def check(self, ctx: Any) -> Observation:
        groups = install_groups(ctx)
        missing = self._missing(ctx, groups)
        if missing:
            return Observation(UNMET, f"{', '.join(missing)} does not exist yet")
        return Observation(MET, f"{' and '.join(groups.values())} exist", {"workspace_groups": groups})

    def plan(self, ctx: Any) -> Plan:
        groups = install_groups(ctx)
        return Plan(
            f"create {described(groups)}",
            (
                "offered once, optional: create them through the Cloud Identity Groups API under your "
                "organisation's customer, you as each one's owner and its only member",
                "without the right to create groups, the Admin console's groups page opens and the install "
                "continues by itself once they exist",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        groups = install_groups(ctx)
        remember(ctx, outputs={"workspace_groups": groups})
        missing = self._missing(ctx, groups)
        if not missing:
            return
        answer = self._ask(ctx, self.id, f"Create {' and '.join(missing)} through the Groups API, you as "
                                         "owner? (yes/no)", default="yes").strip().lower()
        if answer not in YES + NO:
            raise ValueError(f"{answer!r} is neither yes nor no")
        if answer in NO:
            why = "You chose to create the groups yourself"
        else:
            try:
                self._directory_access(ctx)
                parent = f"customers/{self.gcp.customer_id()}"
                for role, email in groups.items():
                    if email in missing:
                        label = "SwarmCloud platform admins" if role == "admin" else "SwarmCloud first team"
                        self.gcp.create_group(ctx.project, parent, email, label)
                        ctx.events.emit("step_apply", step=self.id, message=f"created {email}")
                return
            except (ApiError, LookupError) as exc:
                why = f"Google refused to create the groups ({exc})"
        still = self._missing(ctx, groups)
        open_page(GROUPS_PAGE, why=(
            f"{why}. In the Admin console's groups page, Create group: {', '.join(still)}, with you as owner. "
            "The install continues by itself once they exist."))


class GroupsReaderRoleStep(_Workspace):
    id = "identity.groups-reader-role"
    kind = "human"
    wait = GRANT_WAIT
    satisfies = ("workspace-delegation.groups-reader-assignment",)
    _explain = (
        "swarm-api reads group membership as itself with the predefined Groups Reader admin role (Q2): the "
        "Groups API authorises through Workspace admin roles, not IAM. The engine assigns it through the "
        "Directory API after your typed approval, because it is a grant; refused, the Admin console's roles "
        "page opens and the engine polls until the assignment shows."
    )

    def _role_id(self, customer: str) -> str:
        for role in self.gcp.admin_roles(customer):
            name = str(role.get("roleName") or "")
            if name == GROUPS_READER_ROLE or (role.get("isSystemRole") and name.lower() == GROUPS_READER.lower()):
                return str(role["roleId"])
        raise LookupError(f"the predefined {GROUPS_READER} role is not among this organisation's admin roles")

    def _assigned(self, ctx: Any, unique_id: str) -> bool:
        customer = self.gcp.customer_id()
        role_id = self._role_id(customer)
        return any(str(a.get("assignedTo") or "") == unique_id
                   and str(a.get("scopeType") or "CUSTOMER") == "CUSTOMER"
                   for a in self.gcp.role_assignments(customer, role_id))

    def _probed(self, ctx: Any) -> GroupResult | None:
        """swarm-api's own read of the probe group, in admin-role mode, or
        None when there is nothing to probe with yet."""
        group = probe_group(ctx)
        member = installer(ctx)
        if not group or not member:
            return None
        found, mode = self._probe(ctx, group, member)
        return found if mode == ADMIN_ROLE else None

    def check(self, ctx: Any) -> Observation:
        if ctx.outputs.get("groups_auth") == DELEGATION:
            return Observation(MET, "the delegation fallback was chosen; identity.group-read checks it")
        account = swarm_api_email(ctx.project)
        unique_id = self._client_id(ctx)
        if not unique_id:
            return Observation(UNMET, f"{account} does not exist yet (bootstrap pass 1 creates it)")
        assigned: bool | None = None
        if self._directory_pending(ctx):
            # A check never signs in: the apply asks for the Directory scopes.
            unlisted = "are not listed until stage 8 signs in with the Directory scopes"
        else:
            try:
                assigned = self._assigned(ctx, unique_id)
            except ApiError as exc:
                unlisted = f"could not be listed ({exc})"
        if assigned is None:
            # The assignment list is closed to this sign-in: swarm-api's own read
            # in admin-role mode is then the proof the role is there.
            found = self._probed(ctx)
            if found is not None and found.state == MET:
                return Observation(MET, f"swarm-api reads {found.group} with the {GROUPS_READER} role",
                                   {"groups_auth": ADMIN_ROLE})
            return Observation(UNMET, f"the {GROUPS_READER} assignments {unlisted} and swarm-api "
                                      f"does not read groups as {account} yet")
        if not assigned:
            return Observation(UNMET, f"{account} does not hold the {GROUPS_READER} admin role")
        return Observation(MET, f"{account} holds the {GROUPS_READER} admin role", {"groups_auth": ADMIN_ROLE})

    def plan(self, ctx: Any) -> Plan:
        account = swarm_api_email(ctx.project)
        return Plan(
            f"assign the predefined {GROUPS_READER} admin role to {account}, after your typed approval",
            (
                f"roleAssignments.insert: {GROUPS_READER}, organisation-wide, to {account}'s numeric unique id",
                "refused, the Admin console's admin roles page opens and the install continues by itself once "
                "the assignment shows",
                "or answer delegation for the domain-wide delegation fallback",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        account = swarm_api_email(ctx.project)
        typed = self._ask(ctx, self.id, (
            f"Assign the {GROUPS_READER} admin role to {account}? It lets swarm-api read the membership of "
            f"every group in your organisation, and nothing else. Type {APPROVE} to approve, or delegation "
            "for the fallback")).strip()
        if typed.lower() == "delegation":
            remember(ctx, outputs={"groups_auth": DELEGATION})
            return
        if typed != APPROVE:
            raise ValueError(f"{typed!r} is not {APPROVE}; nothing was assigned")
        try:
            unique_id = self._client_id(ctx)
            if not unique_id:
                raise LookupError(f"{account} does not exist yet")
            self._directory_access(ctx)
            customer = self.gcp.customer_id()
            self.gcp.assign_role(customer, self._role_id(customer), unique_id)
            ctx.events.emit("step_apply", step=self.id, message=f"assigned {GROUPS_READER} to {account}")
            remember(ctx, outputs={"groups_auth": ADMIN_ROLE})
            return
        except (ApiError, LookupError) as exc:
            why = f"Google refused the assignment ({exc})"
        open_page(ADMIN_ROLES_PAGE, why=(
            f"{why}. In the Admin console's admin roles page: {GROUPS_READER} > Admins > Assign service "
            f"accounts > {account}. The install continues by itself once the assignment shows."))


class GroupReadStep(_Base):
    id = "identity.group-read"
    kind = "human"
    wait = GRANT_WAIT
    satisfies = (
        "workspace-group.cloud-identity-presence",
        "workspace-group.tenant-groups",
        "workspace-delegation.groups-reader-admin-role",
        "workspace-delegation.groups-dwd",
    )
    _explain = (
        "swarm-api reads group membership to decide every request's tenant. The Groups API authorises "
        "through Workspace admin roles, not IAM, so a person grants it once in the Admin console: the "
        "Groups Reader admin role on swarm-api's account (preferred, Q2), or domain-wide delegation as "
        "the fallback. The engine then probes through swarm-api itself, in the mode it runs (lesson 20)."
    )

    def check(self, ctx: Any) -> Observation:
        group = str(ctx.outputs.get("probe_group") or "")
        member = str(ctx.outputs.get("probe_member") or "")
        if not group or not member:
            return Observation(UNMET, "no probe group is named yet")
        chosen = str(ctx.outputs.get("groups_auth") or ADMIN_ROLE)
        found, mode = self._probe(ctx, group, member)
        if mode and mode != chosen:
            return Observation(UNMET, f"swarm-api reads groups in {mode} mode and this install chose {chosen}; "
                                      "the stage-8 re-apply renders GROUPS_AUTH, and this step runs it")
        if found.state != MET:
            client = self._client_id(ctx) if chosen == DELEGATION else None
            return Observation(UNMET, why_unmet(found, chosen, ctx.project, client))
        return Observation(MET, f"swarm-api read {group} in {mode or chosen} mode",
                           {"groups_auth": mode or chosen, "probed_groups": [group]})

    def plan(self, ctx: Any) -> Plan:
        account = swarm_api_email(ctx.project)
        return Plan(
            "let swarm-api read Google groups: the admin-role path first, delegation only as the fallback",
            (
                f"1 (recommended) admin role: {ADMIN_ROLE_PATH}: {account}",
                f"2 (fallback) delegation: {DELEGATION_PATH}: swarm-api's client id (its numeric unique id) "
                f"with scope {GROUPS_SCOPE}, and a mailbox for it to act as",
                "name a group to probe with (the first team's group, or one made for it) and one of its members",
                "the install continues by itself once swarm-api reads the group",
            ),
        )

    def apply(self, ctx: Any, plan: Plan) -> None:
        recorded = str(ctx.outputs.get("groups_auth") or "")
        if recorded in PATHS.values():
            # identity.groups-reader-role recorded the path (the role assigned,
            # or the delegation fallback chosen): it is not asked again.
            outputs: dict[str, Any] = {"groups_auth": recorded}
        else:
            answer = self._ask(ctx, self.id, "Which path did you take: admin-role (recommended) or delegation?",
                               default="admin-role").strip().lower()
            if answer not in PATHS:
                raise ValueError(f"{answer!r} is neither admin-role nor delegation")
            outputs = {"groups_auth": PATHS[answer]}
        # A group identity.groups created (the first team's, or for one person
        # the admins'), and its owner.
        group = probe_group(ctx)
        if group and installer(ctx):
            for key, value in (("probe_group", group), ("probe_member", installer(ctx))):
                if not ctx.outputs.get(key):
                    outputs[key] = value
        if outputs["groups_auth"] == DELEGATION and not ctx.outputs.get("groups_impersonate_user"):
            mailbox = self._ask(ctx, self.id, "The mailbox swarm-api acts as (a Workspace user, ideally not a person)")
            if not EMAIL.match(mailbox.strip()):
                raise ValueError(f"{mailbox!r} is not an email address")
            outputs["groups_impersonate_user"] = mailbox.strip()
        for key, question in (("probe_group", "The group to probe with (its email)"),
                              ("probe_member", "One member of that group (an email)")):
            if not ctx.outputs.get(key) and not outputs.get(key):
                value = self._ask(ctx, self.id, question).strip()
                if not EMAIL.match(value):
                    raise ValueError(f"{value!r} is not an email address")
                outputs[key] = value
        remember(ctx, outputs=outputs)
        if outputs["groups_auth"] == DELEGATION and not ctx.dry_run:
            self._delegate(ctx)

    def _delegate(self, ctx: Any) -> None:
        """The delegation fallback finishes in this run (audit #21): the Admin
        console's domain-wide delegation page opens with what to paste there,
        and the stage-8 re-apply runs now, so swarm-api's GROUPS_AUTH says
        delegation before the poll. The engine re-checks a re-apply already
        done and runs it again only if its check is no longer met."""
        client = self._client_id(ctx) or f"(the numeric unique id of {swarm_api_email(ctx.project)})"
        open_page(DELEGATION_PAGE, why=(
            f"Domain-wide delegation for swarm-api: Add new, Client ID {client}, OAuth scopes "
            f"{GROUPS_SCOPE}, then Authorise. The install continues by itself once swarm-api reads the group."))
        for step in reapply_steps(ctx):
            ctx.engine.run_step(step)


class AdminGroupStep(_Base):
    id = "identity.admin-group"
    satisfies = ("workspace-group.admin-surface-swarm-admins",)
    _explain = (
        "The admin surface is governed by a group, never one named person. It is registered only after "
        "swarm-api has read it: a registered group swarm-api cannot read fails every one of its members' "
        "requests with 503 (lesson 20)."
    )

    def check(self, ctx: Any) -> Observation:
        groups = list(ctx.outputs.get("admin_groups") or [])
        member = str(ctx.outputs.get("admin_group_member") or "")
        if not groups or not member:
            return Observation(UNMET, "no admin group is registered yet")
        for group in groups:
            found, mode = self._probe(ctx, group, member)
            if found.state != MET:
                return Observation(UNMET, why_unmet(found, mode or str(ctx.outputs.get("groups_auth") or ADMIN_ROLE),
                                                    ctx.project))
        return Observation(MET, f"swarm-api reads {', '.join(groups)}", {"admin_groups": groups})

    def plan(self, ctx: Any) -> Plan:
        return Plan("name the platform admins' group; it is registered once swarm-api reads it",
                    ("the group's email, and one member of it", "probe through swarm-api; register only if met"))

    def apply(self, ctx: Any, plan: Plan) -> None:
        group = str((ctx.outputs.get("workspace_groups") or {}).get("admin") or "")
        member = installer(ctx) if group else ""
        if not group or not member:
            # No group was created by identity.groups: the person names one.
            group = self._ask(ctx, self.id, "The platform admins' group (its email)").strip()
            member = self._ask(ctx, self.id, "One member of that group (an email)").strip()
        for value in (group, member):
            if not EMAIL.match(value):
                raise ValueError(f"{value!r} is not an email address")
        found, mode = self._probe(ctx, group, member)
        if found.state != MET:
            raise RuntimeError("not registered: " + why_unmet(
                found, mode or str(ctx.outputs.get("groups_auth") or ADMIN_ROLE), ctx.project))
        remember(ctx, outputs={"admin_groups": [group], "admin_group_member": member})


#: The stage the re-apply runs in, beside this module's steps.
REAPPLY_STAGE = 8


def reapply_steps(ctx: Any) -> list[Any]:
    """The stage-8 re-apply (DAYONE-ENGINE-INPUTS): every Terraform pass step
    (one with a `pass_name`) the engine runs at stage 8, found by what it is
    rather than by its id. Empty outside an engine, or before it is registered."""
    engine = getattr(ctx, "engine", None)
    return [step for step in getattr(engine, "steps", None) or ()
            if getattr(step, "stage", None) == REAPPLY_STAGE and getattr(step, "pass_name", "")]


class _ReadOnly:
    """A stage 8 check of what an engine Terraform pass writes (module docstring)."""

    stage = 8
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    _explain = ""
    written_by = ""

    def __init__(self, gcp: Callable[[], IdentityGcp]) -> None:
        self._gcp = gcp

    @property
    def gcp(self) -> IdentityGcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    def observe(self, ctx: Any) -> Observation:  # pragma: no cover - every step overrides it
        raise TypeError(f"{type(self).__name__} has no check")

    def check(self, ctx: Any) -> Observation:
        pending = [step.id for step in reapply_steps(ctx) if not ctx.state.done(step.id)]
        if pending:
            return Observation(WAITING, f"waiting for the stage-8 re-apply ({', '.join(pending)}), which "
                                        f"writes this: {self.written_by}")
        return self.observe(ctx)

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"check what {self.written_by} wrote",
                    ("run the stage-8 re-apply first if it has not run", self.written_by))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """Stage 8 writes nothing here: it runs the stage-8 re-apply through the
        engine when that has not run yet (the engine skips it later as done),
        then checks."""
        for step in reapply_steps(ctx):
            if not ctx.state.done(step.id):
                ctx.engine.run_step(step)
        found = self.check(ctx)
        if found.status != MET:
            raise RuntimeError(f"{found.detail}. Stage 8 does not write this a second way: {self.written_by} "
                               "has applied and this is still not so")


class QaConsoleStep(_ReadOnly):
    id = "identity.qa-console"
    satisfies = (
        "switch.enable_qa_identity",
        "sa.qa-console",
        "role.custom.swarmQaJwtSigner",
        "iam-binding.qa-console-authoritative-policy",
    )
    written_by = ("terraform/bootstrap/qa_identity.tf, with enable_qa_identity generated by the engine's "
                  "stage-8 re-apply (DAYONE-ENGINE-INPUTS)")
    _explain = (
        "A live-browser QA agent signs in to the console as qa-console through IAP, with a 15-minute JWT "
        "swarm-api signs. No key exists; the custom role carries signJwt alone, never Token Creator; and "
        "the account's policy binds it to swarm-api only, authoritatively (QI-IAM, #1649)."
    )

    def observe(self, ctx: Any) -> Observation:
        email = f"{QA_ACCOUNT}@{ctx.project}.iam.gserviceaccount.com"
        if not self.gcp.service_account(email):
            return Observation(UNMET, f"{email} does not exist (enable_qa_identity is off, or bootstrap has not run)")
        role = self.gcp.custom_role(ctx.project, QA_ROLE)
        if not role or role.get("deleted"):
            return Observation(UNMET, f"the custom role {QA_ROLE} does not exist")
        permissions = sorted(role.get("includedPermissions") or [])
        if permissions != QA_PERMISSIONS:
            return Observation(UNMET, f"{QA_ROLE} carries {permissions}, not exactly {QA_PERMISSIONS}")
        expected = [{"role": f"projects/{ctx.project}/roles/{QA_ROLE}",
                     "members": [f"serviceAccount:{swarm_api_email(ctx.project)}"]}]
        bindings = [{"role": b.get("role"), "members": sorted(b.get("members") or [])}
                    for b in self.gcp.service_account_policy(email).get("bindings") or []]
        if bindings != expected:
            return Observation(UNMET, f"{email}'s policy is {bindings}, not {QA_ROLE} for swarm-api alone")
        return Observation(MET, f"{email}: {QA_ROLE} (signJwt only), bound to swarm-api alone")


class AllowedDomainsStep(_ReadOnly):
    id = "identity.allowed-domains"
    satisfies = ("declared-value.allowed-domains-solo",)
    written_by = "terraform/infra's allowed_domains, applied by the engine's stage-8 re-apply (DAYONE-ENGINE-INPUTS)"
    _explain = (
        "There is no shared bearer token: the hosted-domain allow-list is the whole authentication boundary "
        "beyond a valid Google ID token. gmail.com would admit every Google account on earth, and "
        "terraform/infra's default names another organisation's domain."
    )

    def observe(self, ctx: Any) -> Observation:
        region = ctx.state.region
        if not region:
            raise LookupError("no region is recorded (preflight.region confirms it)")
        service = self.gcp.run_service(ctx.project, str(region), "swarm-api") or {}
        env = {str(e.get("name")): str(e.get("value") or "")
               for c in (service.get("template") or {}).get("containers") or [] for e in c.get("env") or []}
        domains = sorted({d.strip().lower() for d in env.get("ALLOWED_DOMAINS", "").split(",") if d.strip()})
        if not domains:
            return Observation(UNMET, "swarm-api has no ALLOWED_DOMAINS")
        consumer = sorted(set(domains) & CONSUMER_DOMAINS)
        if consumer:
            return Observation(UNMET, f"ALLOWED_DOMAINS holds {', '.join(consumer)}, which admits every Google account")
        # Decision 1: ALLOWED_DOMAINS is [org_domain]; before preflight recorded
        # it, the admin groups' domain is the organisation's.
        own = sorted({str(ctx.outputs.get("org_domain") or "").strip().lower()} - {""}) or sorted(
            {str(g).rsplit("@", 1)[-1].lower() for g in ctx.outputs.get("admin_groups") or []})
        if not own:
            return Observation(UNMET, "neither org_domain nor an admin group is recorded, so this organisation's "
                                      "domain is not known")
        missing = [d for d in own if d not in domains]
        if missing:
            return Observation(UNMET, f"ALLOWED_DOMAINS is {', '.join(domains)}; it does not hold this "
                                      f"organisation's {', '.join(missing)}")
        return Observation(MET, f"ALLOWED_DOMAINS is {', '.join(domains)}", {"allowed_domains": domains})


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK,
                api: ApiFactory = api_client.for_install,
                identity_gcp: Callable[[], IdentityGcp] = IDENTITY_GCP) -> list[Any]:
    """Stage 8 in order: the groups and the Groups Reader role first, then
    group reading, then the admin group registered once it is read, then the
    read-only checks of what the stage-8 re-apply writes."""
    return [GroupsStep(identity_gcp, ask, api), GroupsReaderRoleStep(identity_gcp, ask, api),
            GroupReadStep(gcp, ask, api), AdminGroupStep(gcp, ask, api),
            QaConsoleStep(identity_gcp), AllowedDomainsStep(identity_gcp)]


STEPS = build_steps()
