"""Stage 8, identity wiring: swarm-api can read Google groups, and a group is
registered only once swarm-api has read it (design §8; plan §5.3; lesson 20).

| Step | Manifest entries | Kind |
|---|---|---|
| `identity.group-read` | `workspace-group.cloud-identity-presence`, `workspace-group.tenant-groups`, `workspace-delegation.groups-reader-admin-role`, `workspace-delegation.groups-dwd` | human: the Admin console grant, the admin-role path offered first (Q2), delegation the fallback; polls the probe |
| `identity.admin-group` | `workspace-group.admin-surface-swarm-admins` | engine: the platform admins' group, registered only after its probe passes |

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
from typing import Any

from .. import api_client
from ..api_client import ADMIN_ROLE, DELEGATION, ApiClient, GroupResult
from ..exitcodes import MET, UNMET
from ..waits import WaitSpec
from . import Observation, Plan
from .preflight import ASK, EMAIL, GCP, Ask, Gcp, remember

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


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK,
                api: ApiFactory = api_client.for_install) -> list[Any]:
    """Stage 8 in order: group reading first, then the groups that depend on it."""
    return [GroupReadStep(gcp, ask, api), AdminGroupStep(gcp, ask, api)]


STEPS = build_steps()
