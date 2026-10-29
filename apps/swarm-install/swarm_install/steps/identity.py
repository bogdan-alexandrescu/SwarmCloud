"""Stage 8, identity wiring: swarm-api can read Google groups, and a group is
registered only once swarm-api has read it (design §8; plan §5.3; lesson 20).

| Step | Manifest entries | Kind |
|---|---|---|
| `identity.group-read` | `workspace-group.cloud-identity-presence`, `workspace-group.tenant-groups`, `workspace-delegation.groups-reader-admin-role`, `workspace-delegation.groups-dwd` | human: the Admin console grant, the admin-role path offered first (Q2), delegation the fallback; polls the probe |
| `identity.admin-group` | `workspace-group.admin-surface-swarm-admins` | engine: the platform admins' group, registered only after its probe passes |
| `identity.qa-console` | `switch.enable_qa_identity`, `sa.qa-console`, `role.custom.swarmQaJwtSigner`, `iam-binding.qa-console-authoritative-policy` | engine, read-only: the live-browser QA identity terraform/bootstrap creates (`qa_identity.tf`) is exactly as designed |
| `identity.allowed-domains` | `declared-value.allowed-domains-solo` | engine, read-only: swarm-api's `ALLOWED_DOMAINS` is this organisation's domain, never a consumer domain |

**What stage 8 reads but does not write.** qa-console's account, role and
policy are terraform/bootstrap resources and `ALLOWED_DOMAINS` is terraform/
infra's `allowed_domains`: the engine's Terraform passes (DAYONE-ENGINE-3)
write them. Stage 8 checks them where the design places them, and its apply
names the pass that writes them rather than writing them a second way.

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

**B04, lesson 20:** an `unauthorized_client` answer is the Admin console (the
delegation or the admin role was never granted there), never IAM; only a
`signJwt` refusal is a Google Cloud grant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import api_client
from ..api_client import ADMIN_ROLE, DELEGATION, ApiClient, GroupResult
from ..exitcodes import MET, UNMET
from ..waits import WaitSpec
from . import Observation, Plan
from .preflight import ASK, EMAIL, GCP, IAM, Ask, Gcp, remember

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
    look_at="the Admin console path above, for swarm-api's account; swarm doctor --explain "
            "workspace-delegation.groups-reader-admin-role",
    interval_s=15,
)

ApiFactory = Callable[[Any], ApiClient]

QA_ACCOUNT = "qa-console"
QA_ROLE = "swarmQaJwtSigner"
QA_PERMISSIONS = ["iam.serviceAccounts.signJwt"]
RUN_API = "https://run.googleapis.com/v2"

#: Domains that are every Google account on earth (the manifest's
#: `declared-value.allowed-domains-solo`, docs/deploying-without-an-organisation.md).
CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


class IdentityGcp(Gcp):
    """`preflight.Gcp` plus the reads stage 8's read-only checks make."""

    def custom_role(self, project: str, role: str) -> dict[str, Any] | None:
        return self._call("GET", f"{IAM}/projects/{project}/roles/{role}", missing_ok=True)

    def service_account_policy(self, email: str) -> dict[str, Any]:
        return self._call("POST", f"{IAM}/projects/-/serviceAccounts/{email}:getIamPolicy", body={}) or {}

    def run_service(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{RUN_API}/projects/{project}/locations/{region}/services/{name}", missing_ok=True)


@dataclass
class _LazyIdentity:
    """One `IdentityGcp`, built on first use (never at import)."""

    made: list[IdentityGcp] = field(default_factory=list)

    def __call__(self) -> IdentityGcp:
        if not self.made:
            self.made.append(IdentityGcp())
        return self.made[0]


IDENTITY_GCP = _LazyIdentity()


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
                    f"{DELEGATION_PATH}")
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
                                      "run swarm install again so infra pass 2 renders GROUPS_AUTH")
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
        answer = self._ask(ctx, self.id, "Which path did you take: admin-role (recommended) or delegation?",
                           default="admin-role").strip().lower()
        if answer not in PATHS:
            raise ValueError(f"{answer!r} is neither admin-role nor delegation")
        outputs: dict[str, Any] = {"groups_auth": PATHS[answer]}
        if PATHS[answer] == DELEGATION and not ctx.outputs.get("groups_impersonate_user"):
            mailbox = self._ask(ctx, self.id, "The mailbox swarm-api acts as (a Workspace user, ideally not a person)")
            if not EMAIL.match(mailbox.strip()):
                raise ValueError(f"{mailbox!r} is not an email address")
            outputs["groups_impersonate_user"] = mailbox.strip()
        for key, question in (("probe_group", "The group to probe with (its email)"),
                              ("probe_member", "One member of that group (an email)")):
            if not ctx.outputs.get(key):
                value = self._ask(ctx, self.id, question).strip()
                if not EMAIL.match(value):
                    raise ValueError(f"{value!r} is not an email address")
                outputs[key] = value
        remember(ctx, outputs=outputs)


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

    def check(self, ctx: Any) -> Observation:  # pragma: no cover - every step overrides it
        raise TypeError(f"{type(self).__name__} has no check")

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"check what {self.written_by} wrote", (self.written_by,))

    def apply(self, ctx: Any, plan: Plan) -> None:
        found = self.check(ctx)
        if found.status != MET:
            raise RuntimeError(f"{found.detail}. Stage 8 does not write this: {self.written_by} does; "
                               "run swarm install again once it has applied")


class QaConsoleStep(_ReadOnly):
    id = "identity.qa-console"
    satisfies = (
        "switch.enable_qa_identity",
        "sa.qa-console",
        "role.custom.swarmQaJwtSigner",
        "iam-binding.qa-console-authoritative-policy",
    )
    written_by = "terraform/bootstrap/qa_identity.tf, applied by the engine's bootstrap pass (DAYONE-ENGINE-3)"
    _explain = (
        "A live-browser QA agent signs in to the console as qa-console through IAP, with a 15-minute JWT "
        "swarm-api signs. No key exists; the custom role carries signJwt alone, never Token Creator; and "
        "the account's policy binds it to swarm-api only, authoritatively (QI-IAM, #1649)."
    )

    def check(self, ctx: Any) -> Observation:
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
    written_by = "terraform/infra's allowed_domains, generated by the engine's tfvars (DAYONE-ENGINE-3)"
    _explain = (
        "There is no shared bearer token: the hosted-domain allow-list is the whole authentication boundary "
        "beyond a valid Google ID token. gmail.com would admit every Google account on earth, and "
        "terraform/infra's default names another organisation's domain."
    )

    def check(self, ctx: Any) -> Observation:
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
        own = sorted({str(g).rsplit("@", 1)[-1].lower() for g in ctx.outputs.get("admin_groups") or []})
        if not own:
            return Observation(UNMET, "no admin group is registered yet, so this organisation's domain is not known")
        missing = [d for d in own if d not in domains]
        if missing:
            return Observation(UNMET, f"ALLOWED_DOMAINS is {', '.join(domains)}; it does not hold this "
                                      f"organisation's {', '.join(missing)}")
        return Observation(MET, f"ALLOWED_DOMAINS is {', '.join(domains)}", {"allowed_domains": domains})


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK,
                api: ApiFactory = api_client.for_install,
                identity_gcp: Callable[[], IdentityGcp] = IDENTITY_GCP) -> list[Any]:
    """Stage 8 in order: group reading first, then the groups that depend on
    it, then the read-only checks (the allowed domains need the admin group)."""
    return [GroupReadStep(gcp, ask, api), AdminGroupStep(gcp, ask, api),
            QaConsoleStep(identity_gcp), AllowedDomainsStep(identity_gcp)]


STEPS = build_steps()
