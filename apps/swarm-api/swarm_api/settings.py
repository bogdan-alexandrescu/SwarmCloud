"""API-process settings.

`swarm_common.config.Settings` is frozen and holds everything the platform as a
whole needs. This module adds only what the API process alone needs and nothing
that belongs in the frozen contract: which Google groups map to tenants (in
admin-defined priority order), which groups grant admin rights, how long a
Cloud Identity membership answer may be cached, and which Pub/Sub topic wakes
the scheduler.

Concurrency limits deliberately do NOT appear here -- they live in Firestore so
an admin can change them through the admin API without a redeploy.

Two settings here are SAFETY settings rather than tuning knobs, and both fail
the process at start rather than degrading quietly:

  * `hardened` (derived from ENVIRONMENT) turns the optional-by-default token
    audience check into a required one. An unpinned audience means any Google
    ID token from an allowed domain authenticates, including one a third-party
    SaaS obtained when an employee signed in with Google.
  * `REQUIRE_AUTH=false` used to be parsed and then never read by anything, so
    an operator could set it and believe auth was off (or on). Nothing in this
    build can disable authentication, so the value is refused outright instead
    of being silently ignored.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from swarm_common.config import Settings
from swarm_common.identity import SERVICE_ACCOUNT_EMAIL, TenantMember
from swarm_common.profiles import MODEL_CLASSES

from .refusals import validate_environment as validate_refusal_switches


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _percent(name: str, default: int) -> int:
    value = _int(name, default)
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be a percent from 0 to 100, got {value}")
    return value


def _positive(name: str, default: int) -> int:
    """A limit that must admit at least one: 0 or below would refuse every
    request it gates, which is an outage written as a setting."""
    value = _int(name, default)
    if value < 1:
        raise ValueError(f"{name} must be 1 or more, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


_MEMBER_KEYS = ("email", "kind", "principal", "uid")


def _tenant_members(
    project_id: str, *, admin_lists: dict[str, tuple[str, ...]]
) -> tuple[TenantMember, ...]:
    """TENANT_SERVICE_ACCOUNTS -> the listed accounts (contract request 30).

    Terraform renders a JSON LIST of `{email, kind, principal, uid}`
    (terraform/infra/locals.tf). Everything wrong with it is refused HERE, at
    startup, with a ValueError -- never at request time, where the same
    mistake would be a 401 for one caller and a working deployment for
    everyone else, and nobody would look.

    Refused: a value that is not a list; an entry missing a key; a `kind`
    other than group/user; an empty principal or uid; an email that is not a
    user-managed service account (`identity.SERVICE_ACCOUNT_EMAIL`) IN THIS
    PROJECT -- the frozen regex cannot pin the project, it has no project id,
    so the `endswith` below is that pin; the same email listed twice, under the
    same tenant or another (checked explicitly, never left to a dict or set to
    collapse); an email also named by ADMIN_USERS, ADMIN_POOL_USERS,
    ROLLUP_SWEEPER_USERS or SECRET_ADMIN_PRINCIPALS, since a listing grants one
    tenant's rights and an admin entry would add every tenant's.
    """
    raw = os.environ.get("TENANT_SERVICE_ACCOUNTS", "").strip()
    if not raw:
        return ()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"TENANT_SERVICE_ACCOUNTS is not valid JSON: {exc.msg}") from None
    if not isinstance(entries, list):
        raise ValueError(
            "TENANT_SERVICE_ACCOUNTS must be a JSON list of "
            "{email, kind, principal, uid} objects"
        )

    suffix = f"@{project_id.strip().lower()}.iam.gserviceaccount.com"
    admins = {
        name: {value.strip().lower() for value in values}
        for name, values in admin_lists.items()
    }
    result: list[TenantMember] = []
    for index, entry in enumerate(entries):
        where = f"TENANT_SERVICE_ACCOUNTS[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        missing = [key for key in _MEMBER_KEYS if key not in entry]
        if missing:
            raise ValueError(f"{where} is missing {', '.join(missing)}")
        if not all(isinstance(entry[key], str) for key in _MEMBER_KEYS):
            raise ValueError(f"{where}: every field must be a string")
        email = entry["email"].strip().lower()
        kind = entry["kind"].strip().lower()
        principal = entry["principal"].strip().lower()
        uid = entry["uid"].strip()
        if kind not in {"group", "user"}:
            raise ValueError(f"{where}: kind must be group or user, got {kind!r}")
        if not principal:
            raise ValueError(f"{where}: principal is empty")
        if not uid:
            raise ValueError(
                f"{where}: uid is empty; the account's unique id is pinned beside its "
                "email so a recreated account does not inherit the tenant"
            )
        if not SERVICE_ACCOUNT_EMAIL.fullmatch(email) or not email.endswith(suffix):
            raise ValueError(
                f"{where}: {email!r} is not a user-managed service account in project "
                f"{project_id!r}; only <id>{suffix} may be listed"
            )
        if any(member.email == email for member in result):
            raise ValueError(
                f"{where}: {email!r} is listed more than once; a service account may be "
                "listed under one tenant, once"
            )
        for name, values in admins.items():
            if email in values:
                raise ValueError(
                    f"{where}: {email!r} is also in {name}; a listing grants exactly one "
                    "tenant's rights and may not be combined with an admin entry"
                )
        result.append(TenantMember(email=email, kind=kind, principal=principal, uid=uid))
    return tuple(result)


def parse_worker_class_models(raw: str) -> dict[str, str]:
    """WORKER_CLASS_MODELS -> {model class: model ID}. Empty means none pinned.

    #1435, docs/design/model-per-step.md §4.2: terraform's
    `jsonencode(local.runner_models)` (`model_class_env`), the ONE place a
    class's ID is stated, and the same map the scheduler dispatches with. This
    service fills `Task.model` from it and serves it on /v1/runtimes. Refused
    at start, as the scheduler refuses its copy
    (`scheduler.settings.check_worker_class_models`), against the frozen
    `ModelClass.admits` rather than a restatement of it: a map whose keys are
    not exactly the MODEL_CLASSES names, or whose ID is not of its class, would
    record and serve the wrong model for every step of that class.

    EMPTY IS ALLOWED: the local emulator loop, and a settings object a test
    builds. Then no class but a profile's default can be named
    (`validation.check_model_class_available`), and `TaskCreate.model` is
    recorded as sent, as before this map existed.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        models = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WORKER_CLASS_MODELS is not valid JSON: {exc}") from exc
    if not isinstance(models, dict):
        raise ValueError(
            "WORKER_CLASS_MODELS must be a JSON object of model class -> model ID, "
            f"got {type(models).__name__}"
        )
    if not models:
        return {}
    missing = sorted(set(MODEL_CLASSES) - set(models))
    extra = sorted(str(k) for k in set(models) - set(MODEL_CLASSES))
    if missing or extra:
        raise ValueError(
            f"WORKER_CLASS_MODELS must name exactly the model classes "
            f"{tuple(MODEL_CLASSES)}; missing {missing}, not a class {extra}"
        )
    for name, model_id in models.items():
        if not MODEL_CLASSES[name].admits(model_id):
            raise ValueError(
                f"WORKER_CLASS_MODELS[{name!r}] = {model_id!r} is not a {name} model: "
                f"a pin of this class is claude-{MODEL_CLASSES[name].family}-<major>"
                f"[-<minor>][-<yyyymmdd>] with major at least "
                f"{MODEL_CLASSES[name].min_major}"
            )
    return dict(models)


@dataclass(frozen=True)
class ApiSettings:
    """Everything the API needs at process start."""

    core: Settings

    #: Admin-registered tenant groups, in priority order. `resolve_tenant` walks
    #: this list and the FIRST match wins, so the order is what makes a user in
    #: several groups land in the same tenant on every request.
    iap_audiences: tuple[str, ...] = ()
    tenant_groups: tuple[str, ...] = ()

    #: Membership in any of these grants the admin surface. Kept separate from
    #: tenant groups so an admin still belongs to a normal tenant for their own
    #: tasks.
    admin_groups: tuple[str, ...] = ()
    #: Admin by EMAIL rather than by group membership.
    #:
    #: An escape hatch, and it exists because admin is otherwise unreachable
    #: here: `is_admin` is computed from Cloud Identity group membership, and
    #: this platform's service account cannot read groups at all -- the Groups
    #: API does not authorize through GCP IAM, and a *.gserviceaccount.com
    #: identity is not a Workspace principal. With no resolvable groups,
    #: `admin_set` is empty and NOBODY is an admin, which 403s every operator
    #: screen.
    #:
    #: It is strictly worse operationally than a group: changing it needs a
    #: deploy. It is NOT weaker authentication -- the email still comes from
    #: the verified IAP assertion, the same source the group lookup would have
    #: started from. Delete it in the same change that grants swarm-api a
    #: Workspace Group Reader role.
    admin_users: tuple[str, ...] = ()
    #: The platform owner (docs/workspaces.md §6.5), one email. Always an
    #: admin, from configuration, so the owner keeps admin if every
    #: `admin_roles/` document is lost, and no admin can make themselves the
    #: owner through the UI. Seeded as the one `role: owner` document, which
    #: nobody else may change. Empty means no owner: admins still work, and
    #: nothing is protected from demotion but the last admin.
    #:
    #: ADMIN_USERS above is migrated ONCE into `admin_roles/` documents
    #: (`swarm_api.admins`) and keeps granting admin as the fallback until the
    #: operator trims it to this one address.
    platform_owner: str = ""
    #: Whether a platform admin (`AuthContext.is_admin`) may select a
    #: registered GROUP workspace they are not a member of, as that
    #: workspace's admin (docs/design/workspace-roles.md §4.2, owner answer
    #: Q5, 2026-10-18). Never anyone else's personal (`u-`) workspace, on or
    #: off. OFF by default (PLATFORM_ADMIN_REACH): it widens who may enter a
    #: tenant (invariant 9), so it goes on in dev first, is exercised by the
    #: acceptance probe, and only then goes on in prod -- the same order as
    #: the refusal switches (§4.7). Off, `AuthContext.platform_reach` is
    #: empty and `Authenticator.may_act_in` never answers "platform_admin",
    #: which is the behaviour before roles exactly.
    platform_admin_reach: bool = False
    #: Whether a platform-side workspace membership -- a signed
    #: `workspace_memberships` grant an admin's approval of a join request
    #: writes (`swarm_api.memberships`; docs/design/join-requests.md §3.4,
    #: §4.5, owner answer Q1 (A), 2026-10-25) -- admits its person to that
    #: group workspace beside its Google group. OFF by default
    #: (PLATFORM_MEMBERSHIPS): it widens who may enter a tenant (invariant 9),
    #: so it goes on in dev first, is exercised by the acceptance probe, and
    #: only then goes on in prod, the same order as PLATFORM_ADMIN_REACH. Off,
    #: no membership is ever read: `AuthContext.platform_memberships` is
    #: empty, `Authenticator.is_tenant_member` asks only the group, and an
    #: approval admits nobody (§3.4, design C's behaviour). Turning it off
    #: again is the rollback: anyone admitted only by a membership loses the
    #: workspace on their next request, and nothing has to be deleted.
    platform_memberships: bool = False
    #: The join-request rate limits (docs/design/join-requests.md §4.1), read
    #: by `joinrequests.JoinStore`; each defaults to the constant of the same
    #: meaning in `joinrequests.py`, which says why that value.
    #: Open join requests per person across workspaces
    #: (JOIN_OPEN_PER_PERSON). 3: a person joining a team needs one or two at
    #: a time; more is someone trawling for codes that work.
    join_open_per_person: int = 3
    #: Join requests per person per 24 hours, withdrawn ones included
    #: (JOIN_DAILY_PER_PERSON). 10: asking and withdrawing in a loop must not
    #: ring an admin's bell all day.
    join_daily_per_person: int = 10
    #: Days after a denial before the same person may ask the same workspace
    #: again (JOIN_DENIED_HOLD_DAYS). 7: a denial is not answered by asking
    #: again a minute later, and a week is long enough for the admin's reason
    #: to be acted on.
    join_denied_hold_days: int = 7
    #: Open join requests per workspace (JOIN_OPEN_PER_WORKSPACE). 50: more
    #: than an admin will read; the refusal is shown only to someone holding
    #: a working code, so it reveals nothing to anyone else.
    join_open_per_workspace: int = 50
    #: How long one instance trusts a workspace role it read
    #: (ROLE_CACHE_SECONDS; `swarm_api.roles`, docs/design/workspace-roles.md
    #: §4.12). 15: the same bound `admins.ROLE_CACHE_SECONDS` puts on a
    #: removed platform admin, so a person narrowed by a workspace admin
    #: keeps their old role on another instance for no longer than a demoted
    #: platform admin keeps theirs. A decision that must see a change made a
    #: moment ago (an approval, an answer) reads past it, fresh.
    role_cache_seconds: float = 15.0
    #: A NARROWER capability than admin, by email: these callers may call the
    #: admin routes in `auth.POOL_ADMIN_ROUTES` -- today only
    #: `PUT /v1/admin/limits/runner/{runner_profile}` -- and no other.
    #:
    #: WHY IT EXISTS. The verification gate (swarm-verify) has to narrow
    #: `runner:mock` for race-test, and the owner decided on 2026-09-24 that it
    #: does so through the admin route rather than a Firestore write role. The
    #: first form of that decision put the gate in `admin_users`; review showed
    #: admin is one boolean that also lets it disable any tenant
    #: (`PUT /v1/admin/tenants/{id}/limits`) and rewrite any tenant's workflow
    #: state, and the owner reversed it the same day. This list is the
    #: replacement: the route the gate needs, and nothing else.
    #:
    #: It does NOT set `is_admin`, so nothing that reads that flag -- the
    #: operator screens, the cross-tenant fields on /v1/stats and /v1/capacity
    #: -- sees a pool admin as an admin. And the route set is an ALLOW-LIST:
    #: an admin route added later is admin-only until someone decides
    #: otherwise and adds it there.
    #:
    #: Same comparison as `admin_users`: the bare email from the verified
    #: token, case-insensitive. A `serviceAccount:` prefix matches nobody.
    admin_pool_users: tuple[str, ...] = ()
    #: The identity the per-tenant Cloud Scheduler rollup jobs present
    #: (terraform/modules/scheduler/jobs.tf, `workflow_rollup` and
    #: `issue_run_advance`): may call `POST /v1/admin/workflows/rollup` and
    #: `POST /v1/admin/runs/advance` (`auth.ROLLUP_SWEEPER_ROUTES`) and no
    #: other route, admin or not. NOT an admin and NOT a tenant member -- see
    #: `AuthContext.is_rollup_sweeper`. Admitted before the domain check, as a
    #: service account must be, and only with an explicit `email_verified`.
    #:
    #: Same comparison as `admin_pool_users`: the bare email from the verified
    #: token, case-insensitive. An address here may not also be on ADMIN_USERS,
    #: ADMIN_POOL_USERS, ALLOWED_USERS or TENANT_SERVICE_ACCOUNTS: two roles on
    #: one address make which applies depend on the order auth.py asks.
    rollup_sweeper_users: tuple[str, ...] = ()
    #: The identity the schedule tick presents (terraform/modules/scheduler,
    #: `swarm-schedule-tick`, docs/schedules.md SD10). Terraform renders it
    #: (lane S13) before any route admits it: lane S2 admits it to
    #: `POST /v1/admin/schedules/tick` alone (`auth.SCHEDULE_TICK_ROUTES`).
    #: Until then nothing consults it, so reading it grants nothing.
    schedule_tick_users: tuple[str, ...] = ()
    #: Authorise these exact addresses, regardless of their domain.
    #:
    #: WHY THIS EXISTS, and it is not the same idea as `admin_users` above.
    #: That one decides who is an ADMIN among callers already authorised;
    #: this decides who is a CALLER at all, and it is the difference between
    #: this platform being deployable outside a Google Workspace or not.
    #:
    #: Authorisation is otherwise by hosted domain, which is exactly right for
    #: an organisation: `allowed_domains = ["example.com"]` means the people in
    #: that organisation. It collapses for anyone else. A single developer on a
    #: personal project has no domain of their own, so the only value that
    #: would let them in is `["gmail.com"]` -- which authorises every Google
    #: account on earth to run agents on their billing account. The safe
    #: configuration has to be expressible, or the unsafe one gets used.
    #:
    #: It is ADDITIVE and never subtractive: an address here is admitted even
    #: when its domain is not listed, and a domain that IS listed still works.
    #: Setting only this and leaving `allowed_domains` empty is the solo
    #: deployment; setting only domains is the organisation; setting both
    #: admits a named contractor alongside the company.
    #:
    #: It is not weaker authentication. The address still comes from a verified
    #: Google ID token or IAP assertion -- the same source the domain check
    #: reads -- so this changes WHICH verified identities are admitted, never
    #: whether the identity was verified.
    allowed_users: tuple[str, ...] = ()
    #: Principals that hold secretVersionAdder on EVERY tenant's provider-key
    #: secrets, and must therefore never be a tenant's own principal.
    #:
    #: terraform/infra/variables.tf already refuses that shape -- it validates
    #: secret_admin_members against var.tenants and caught it once, when
    #: bogdan@saga.xyz was both the secret admin and the principal of the
    #: u-bogdan fallback tenant. But that validation can only see DECLARED
    #: tenants, and `Store.ensure_tenant` creates a self-service tenant for any
    #: allowed-domain caller on first sight. On 2026-09-21 admin@saga.xyz --
    #: the platform account the tfvars comment describes as one that "owns no
    #: tenant" -- signed in to the web UI and became the principal of tenant
    #: u-admin, bypassing the validation entirely.
    #:
    #: A tenant whose principal can add a secret version to every other
    #: tenant's provider key can replace one with a key pointing at
    #: infrastructure they control, after which that tenant's prompts, source
    #: and output all flow through it. So the runtime enforces what terraform
    #: enforces, on the path terraform cannot see.
    secret_admin_principals: tuple[str, ...] = ()
    #: Service accounts a tenant lists besides its principal (contract
    #: request 30), from TENANT_SERVICE_ACCOUNTS. Refused at startup, not at
    #: request time, when an entry is not a user-managed service account in
    #: THIS project, is listed more than once, sits under two tenants, is
    #: missing its unique id, or is also in admin_users, admin_pool_users or
    #: secret_admin_principals. See `_tenant_members`.
    tenant_service_accounts: tuple[TenantMember, ...] = ()
    #: The Workspace user this service account acts AS when reading groups.
    #:
    #: Cloud Identity's Groups API does not authorize through GCP IAM -- a
    #: service account is not a Workspace principal and every lookup returns
    #: Error(2028). Domain-wide delegation, authorised in the Admin console
    #: against this service account's OAuth client id and scoped to
    #: cloud-identity.groups.readonly, lets it act as a real user that the
    #: API will answer.
    #:
    #: Empty disables delegation, which is correct for local development where
    #: google.auth.default() returns user credentials that cannot delegate
    #: anyway. Always empty under `groups_auth = "admin_role"`.
    groups_impersonate_user: str = ""

    #: `GROUPS_AUTH`: how swarm-api is authorised to read groups
    #: (`swarm_api.groups.GROUPS_AUTH_MODES`; docs/design/day-one-install.md
    #: section 8). `admin_role`: the Workspace "Groups Reader" admin role on
    #: this service account, which asks as itself, so `groups_impersonate_user`
    #: is cleared and no request delegates. `delegation`: acts as
    #: `groups_impersonate_user`. Default `delegation`, because that is the mode
    #: the live deployment is set up for; installs set `admin_role` (Q2). An
    #: unknown value is refused at start.
    groups_auth: str = "delegation"

    #: Cloud Identity membership checks are a network round trip on the request
    #: path, so answers are cached briefly per (caller, group).
    group_cache_ttl_seconds: int = 120

    #: The registry of job-made group and service workspaces
    #: (`swarm_api.registry`, docs/design/workspace-kinds.md §4.4). 60: a
    #: workspace that becomes `ready`, or a service record set
    #: `deprovisioned`, takes effect on every instance within a minute, for
    #: two Firestore queries per instance per minute. Shorter costs reads on
    #: the request path for no decision that needs it sooner.
    workspace_registry_cache_seconds: float = 60.0
    #: How long a failing registry read keeps serving the last good one. 600:
    #: a Firestore blip does not 503 every caller, and past ten minutes a
    #: group made or a service disabled since is too likely to be missed, so
    #: the answer is a 503 rather than a guess (the reasoning of Q5 (a)).
    workspace_registry_stale_seconds: float = 600.0

    #: Pub/Sub topic that wakes the scheduler after a submission. Empty disables
    #: the nudge; the Cloud Scheduler safety tick still drains the queue.
    dispatch_topic: str = ""

    #: Base URL of the quota broker, which owns the account pool.
    #:
    #: The account routes in this API are a PROXY: refreshing an OAuth
    #: credential revokes the one it replaces, so the broker is the platform's
    #: single writer for subscription credentials and this service never writes
    #: one itself. Empty therefore means the account routes REFUSE with a 503 --
    #: there is no local path to fall back to, and inventing one would be the
    #: second writer the design exists to prevent.
    quota_broker_url: str = ""
    #: Audience for the ID token this service mints to call that broker.
    #:
    #: NOT THE SAME VALUE AS THE URL, and assuming it was fails only once
    #: deployed. terraform gives the broker service a CUSTOM AUDIENCE
    #: (`https://swarm-quota-broker.<env>.swarm.internal`, infra/locals.tf
    #: `push_audiences`) and the broker verifies `aud` against exactly that
    #: string, which is not its Cloud Run URL. Empty falls back to the URL,
    #: which is right for a plain service and for a local broker.
    quota_broker_audience: str = ""

    #: Maximum page size a caller may request on any list endpoint.
    max_page_size: int = 200
    default_page_size: int = 50

    #: How many object keys one checkpoint listing may examine.
    #:
    #: A bound is required because a task's prefix is unbounded in principle --
    #: a long-running attempt checkpoints every few minutes for hours. It is
    #: NEVER silent: `truncated` comes back true when the limit bit, because a
    #: screen showing the first N of M and a screen showing all of them are
    #: otherwise identical.
    object_scan_limit: int = 5000

    #: Log window sizes, in bytes of the RAW object.
    #:
    #: The ceiling exists because the worker's cap on a captured stream is
    #: `max_stdout_bytes`, 32 MiB, and a route that served one in a single
    #: response would put 32 MiB through JSON encoding into a browser. Paging
    #: is what the offset/next_offset pair is for.
    max_log_bytes: int = 1024 * 1024
    default_log_bytes: int = 64 * 1024
    #: The FLOOR, and it is a safety setting rather than a tuning knob. A log
    #: window is cut back to a token boundary so paging can never split a
    #: credential in half, and a window smaller than a line has no boundary to
    #: cut at -- it can only be withheld. `limit_bytes` is clamped UP to this.
    min_log_bytes: int = 4 * 1024

    #: Rate-limit burst. The sustained rate comes from the frozen Settings.
    rate_limit_burst: int = 40

    #: How a tenant's Google service account is spelled. This is a REFERENCE to
    #: an identity created out of band (terraform's tenancy module, or
    #: scripts/register-tenant.sh); the API never creates one. Terraform's
    #: spelling is the default because terraform is what seeds the tenant
    #: document in a deployed environment.
    tenant_service_account_prefix: str = "swarm-agent-worker"

    #: Kubernetes namespace prefix, same reasoning: terraform's
    #: `namespace_prefix` default.
    tenant_namespace_prefix: str = "swarm-tenant-"

    #: The Cloud KMS key VERSION that signs every step spec, named in full
    #: (projects/.../cryptoKeys/step-spec/cryptoKeyVersions/<n>), contract
    #: request 34. A version, not a key: an asymmetric key has no primary.
    #: Empty only in local development; `build_context` refuses every
    #: deployed environment without it, dev included
    #: (`specsigning.signer_from_settings`).
    spec_signing_key_version: str = ""

    #: {full version name: PEM} over the step-spec key's ENABLED versions, as
    #: JSON: the SPEC_VERIFY_KEYS every worker carries (terraform/infra/
    #: spec_signing.tf). Read by `verdictpublish.spec_refusal` only, which
    #: verifies a gated step's signed spec before it reads the step's forge
    #: credential, as the worker does (#748). Empty, or not a JSON object of
    #: strings, verifies nothing: every control-plane publish is declined and
    #: the worker publishes, so a missing value costs the saving, never a
    #: push from an unverified document.
    spec_verify_keys: str = ""

    #: Whether the deployment actually SAID which environment this is.
    #:
    #: The frozen `Settings.from_env` defaults ENVIRONMENT to "dev" when the
    #: variable is unset, so `core.environment == "dev"` is two different
    #: facts: a deployment that declared dev, and one that declared nothing.
    #: `GET /v1/tenants/me` serves both the name and this flag, because the web
    #: UI's environment badge (Brand.tsx) is only allowed to draw what was
    #: measured -- and a defaulted "dev" on a production console is the exact
    #: badge it exists not to draw.
    #:
    #: FALSE BY DEFAULT, on purpose: a caller that builds ApiSettings by hand
    #: and forgets it gets "not declared", which the UI renders as its loud
    #: unknown-environment case, never as a quiet dev. `from_env` sets it from
    #: the variable; terraform sets ENVIRONMENT on every service through
    #: `common_env` (terraform/infra/locals.tf).
    environment_declared: bool = False

    #: The web console's public origin, `https://<frontend_hostname>`, from
    #: SWARM_CONSOLE_URL (terraform renders it from `var.frontend_hostname`).
    #:
    #: THE ONE SOURCE OF A CONSOLE LINK (owner decision 2026-10-01): every task
    #: and workflow the API serves carries `links.console` built from this
    #: (`codec.agent_console_url`, `codec.workflow_console_url`), and the
    #: plugin, the sc CLI and the MCP answers print that rather than spelling
    #: the host themselves. EMPTY BY DEFAULT, and empty means NO link -- null,
    #: never a run.app guess: swarm-ui is behind IAP, so its run.app address is
    #: a link that does not open.
    console_url: str = ""

    #: ADMIN › SETUP (docs/design/day-one-plan.md §5.3, lane DAYONE-SETUP-API-1).
    #: The GitHub organisation the console's App manifest form posts to, from
    #: SETUP_GITHUB_ORG. EMPTY BY DEFAULT, and empty means the manifest route
    #: answers 409 `setup_unconfigured`: a guessed org would register the App
    #: under someone else's account.
    setup_github_org: str = ""
    #: The deployment's hostname `<H>`, from SWARM_HOSTNAME; empty falls back
    #: to `console_url`'s host (`install_hostname`), never a run.app guess.
    setup_hostname: str = ""
    #: The `swarm-github-hooks` receiver's public URL, from GITHUB_HOOKS_URL:
    #: the App manifest's `hook_attributes.url`. Empty means the manifest has
    #: no hook URL and the hook route answers 409 `setup_unconfigured`.
    setup_github_hooks_url: str = ""
    #: The `swarm-doctor` job's account, the one caller of
    #: `PUT /v1/admin/setup/health`, from SWARM_DOCTOR_ACCOUNT. Empty means
    #: `swarm-doctor@<project>.iam.gserviceaccount.com` (`doctor_identity`), the
    #: name the install's Terraform gives it, so no deployment has to set it.
    doctor_account: str = ""

    #: THE QA IDENTITY (docs/design/qa-identity.md §4.1, lane QI-API, part of
    #: #1649): the address of the QA service account whose IAP JWT swarm-api
    #: signs for a live attempt of a granted tenant (`swarm_api.qa_identity`).
    #: From QA_IDENTITY_CONSOLE_ACCOUNT (terraform/infra/locals.tf). EMPTY BY
    #: DEFAULT, and empty means this deployment has no QA identity: every
    #: submission naming one is 422 and every signing request 503
    #: `qa_identity_unconfigured`. The console host it is signed for is
    #: `console_url`'s, never a second value that could drift from it.
    qa_identity_console_account: str = ""
    #: A QA credential's life. 900 (owner Q5 (a): 15 minutes, renewed at 10),
    #: clamped to 300-3600 by `qa_identity.lifetime_seconds`: under 300 a
    #: renewal becomes a hot loop, and IAP refuses a JWT claiming over an hour.
    qa_identity_lifetime_seconds: int = 900

    #: The internal decision API through the cluster's Tailscale egress Service
    #: (INTERNAL_DECISION_API_URL; JUDGE-NET's switch, terraform
    #: `internal_decision_api_enabled`). Non-empty means the tailnet egress is
    #: on, so the internal Jev backends are offered as available
    #: (`swarm_api.judgment`); this service never calls it. EMPTY BY DEFAULT:
    #: the egress needs the owner's Tailscale OAuth client first.
    internal_decision_api_url: str = ""

    # --- child tasks (docs/design/child-tasks.md) --------------------------
    #: `swarm-child-key`, the HMAC key that verifies a registration nonce,
    #: derives a registration id and attests a registration (§3.2). Held by
    #: swarm-scheduler and swarm-api only, from Secret Manager; never a
    #: Terraform secret version. EMPTY MEANS NO CHILD PATH: both worker routes
    #: answer 503 `child_submit_unavailable` and nothing is created. Excluded
    #: from the repr so a logged settings object cannot carry it.
    child_key: str = field(default="", repr=False)
    #: The previous version, accepted beside the current one during a rotation
    #: (§5 F13). Empty outside one.
    child_key_previous: str = field(default="", repr=False)
    #: The audience a WORKER mints its ID token for when it calls the child
    #: routes (SWARM_API_AUDIENCE): the constant custom audience terraform
    #: gives this service, the same value the scheduler hands each worker. The
    #: child routes pin `aud` to it (routes/children.py). Empty in local
    #: development, where the app's own verifier is used.
    child_audience: str = ""
    #: Children one task may have across all its attempts (§7): sixteen plus
    #: the parent fit inside a new tenant's `default_tenant_max_active` of 20,
    #: so a fresh tenant can run one full fan-out without an admin.
    max_children_per_task: int = 16
    #: How far an attempt proof's timestamp may be from this clock (§7). Wide
    #: enough for ordinary skew; a replay inside it is answered by the request
    #: id dedupe with the child already made.
    child_proof_skew_seconds: int = 120

    # --- the live browser (docs/design/live-browser.md, lane LB-L9) ---------
    #: `swarm-live-key`, the HMAC key that signs a live ticket (§3.3). Held by
    #: swarm-api and swarm-live only, from Secret Manager; created by
    #: scripts/create-secrets.sh, never a Terraform secret version. EMPTY MEANS
    #: NO LIVE PATH: every live route answers 503 `live_unavailable` and no
    #: session is opened. Excluded from the repr, as `child_key` is.
    live_key: str = field(default="", repr=False)
    #: The previous version, accepted when VERIFYING beside the current one
    #: during a rotation; never used to mint. Empty outside one.
    live_key_previous: str = field(default="", repr=False)
    #: Where swarm-api dials the relay for a viewer's socket (§3.3 step 3, R2):
    #: `swarm-live` in swarm-system, as plain `ws://<internal LB address>:8080`
    #: (owner decision 2026-10-11 (b)), from SWARM_LIVE_URL, which
    #: terraform/infra/locals.tf sets from live.tf's reserved address. The
    #: relay's `/internal/close` is the same address over http. Empty means no
    #: relay, and a socket closes with "live view unavailable".
    live_relay_url: str = ""
    #: A tunnel ticket's life (§3.3 step 1): long enough for the worker to hand
    #: it to the gate and the gate to dial, short enough that a copy is useless.
    live_tunnel_ticket_seconds: int = 60
    #: A viewer ticket's life (§5.2 step 3): the console uses it at once.
    live_viewer_ticket_seconds: int = 30
    #: How long a controller's console has to renew before control lapses
    #: (§4.2): a closed tab stops renewing, and its control ends within this.
    live_control_renew_seconds: int = 60
    #: How long a controller may send no input before control ends (§4.2), and
    #: how far each input moves the hold's deadline (§4.3). 300 is the design's
    #: default: long enough to find a password, short enough that a person who
    #: walked away does not hold an 8-vCPU pod.
    control_idle_seconds: int = 300
    #: How often an open socket re-asks whether its person is still a member
    #: of the task's tenant (§5.2). With the group cache's 120 s this ends a
    #: removed person's view, and their control, within about 7 minutes.
    live_membership_recheck_seconds: float = 300.0
    #: How often an open socket reads its session: whether it closed, and
    #: whether its person took or lost control (which the relay is told). One
    #: read per socket per tick; 2 s is the delay a person sees on Take control.
    live_session_poll_seconds: float = 2.0
    #: How often a controller's input counts are written to the session, which
    #: also moves the hold's deadline (§4.3). Throttled: one write per flush,
    #: never one per key.
    live_input_flush_seconds: float = 15.0
    #: §5.10: viewer tickets per person per minute, and take-control attempts
    #: per person per task per minute. In-process, as the API's limiter is.
    live_viewer_tickets_per_minute: int = 30
    live_takes_per_minute: int = 6
    #: §4.3, HANDOFF-POLICY (owner decision 2026-10-13, replacing Q3's flat 10
    #: minutes, `human_hold_seconds`): how long a hand-off holds its pod with
    #: NOBODY watching -- from the request, and past the last moment a viewer
    #: was seen. 60: 13 of 42 measured hand-offs expired with nobody there,
    #: each holding a 2-unit lease for 10 minutes. `swarm_api.handoff`.
    human_unwatched_seconds: int = 60
    #: The ceiling, from the request, on a hold that viewers and a controller
    #: keep extending. 1800: 7 of the 42 expired after the person said they
    #: were ready, so a watched hold outlives 10 minutes -- but not an
    #: afternoon (invariant 4).
    human_hold_max_seconds: int = 1800
    #: How often an open viewer socket tells swarm-api "someone is watching",
    #: which moves the hold's deadline to now + `human_unwatched_seconds`.
    #: Must stay well under that, or a watched hold lapses between writes; one
    #: write per socket per flush, and only while a hand-off is open.
    live_watch_flush_seconds: float = 15.0
    #: §5.10: hand-off requests per attempt, read by `swarm_api.handoff`.
    max_handoffs_per_attempt: int = 3
    #: BROWSER EGRESS (docs/browser-egress.md §2.2, §6.1; lane EGRESS-SVC).
    #: `swarm-egress-key`, from SWARM_EGRESS_KEY by Secret Manager reference
    #: (terraform/infra/egress.tf): what the attempt egress ticket is MAC'd
    #: with. Empty means no proxied browsing: the route answers 503 and the
    #: worker runs the attempt direct. Excluded from the repr, as `live_key` is.
    egress_key: str = field(default="", repr=False)
    #: An egress ticket's life. The worker renews it before it expires, so
    #: this is how long a ticket copied out of a compromised renderer keeps
    #: working; clamped to swarm-egress's MAX_TICKET_SECONDS (900).
    egress_ticket_seconds: int = 600
    #: THE SWITCH (§6.1), off by default: a ticket is minted only when this is
    #: on AND the task's tenant is in `browser_egress_tenants` AND its profile
    #: is in `browser_egress_profiles` and is a live-browser profile. Rendered
    #: by egress.tf from var.browser_egress, the same setting swarm-egress
    #: reads, so the two cannot disagree about who is switched on.
    browser_egress_enabled: bool = False
    browser_egress_tenants: tuple[str, ...] = ()
    browser_egress_profiles: tuple[str, ...] = ("claude-code-browser",)
    #: The exit country (owner decision Q6: US), returned with the ticket so
    #: the take-control dialog can say where sites see the browser from.
    browser_egress_country: str = "us"
    #: THE TYPESAFE FORWARDER (docs/design/workspace-secrets.md §3.5; lane
    #: SEC-INJECT-A). `swarm-typesafe-fwd-key`, from SWARM_TYPESAFE_FWD_KEY by
    #: Secret Manager reference (terraform/infra/typesafe_fwd.tf): what the
    #: attempt TypeSafe ticket is MAC'd with. Empty means no forwarder: the
    #: route answers 503. Excluded from the repr, as `egress_key` is.
    typesafe_fwd_key: str = field(default="", repr=False)
    #: A TypeSafe ticket's life: how long one copied out of a compromised pod
    #: can spend its key. Five minutes, shorter than the egress ticket's ten,
    #: because a ticket here spends a person's or a workspace's key rather than
    #: platform bandwidth. Clamped to the forwarder's 60-900 s.
    typesafe_ticket_seconds: int = 300
    #: THE SHARED GRAPH SERVICE (docs/design/graph-service.md §3.2; lane
    #: GS-TICKET, part of #1384). `swarm-graph-ticket-key`, from
    #: SWARM_GRAPH_TICKET_KEY by Secret Manager reference: what the attempt
    #: graph ticket is MAC'd with, its own key so a graph-service compromise
    #: cannot mint an egress or TypeSafe ticket. Empty means no graph service:
    #: the route answers 503 and the worker stages as it does today. Excluded
    #: from the repr, as `typesafe_fwd_key` is.
    graph_ticket_key: str = field(default="", repr=False)
    #: A graph ticket's life. Ten minutes, the egress ticket's: a ticket here
    #: reads only the graph its attempt is already entitled to read (§3.2), so
    #: it need not be as short as the TypeSafe ticket's. Clamped to the
    #: service's 60-900 s.
    graph_ticket_seconds: int = 600
    #: Where swarm-api reads the graph service's cache stats
    #: (docs/design/graph-answer-cache.md §5.3): SWARM_GRAPH_STATS_URL,
    #: `http://<the internal load balancer's address>:8081`, which
    #: terraform/infra/graph_service.tf sets while the stats are served. Empty
    #: by default, and empty (or anything not http(s)) means no stats: the
    #: repository's Cache section answers `stats: not_read` (not_configured)
    #: and calls nothing (routes/graph_cache.py). An address, not a secret, so
    #: it stays in the repr.
    graph_stats_url: str = ""
    #: SETTINGS INTEGRITY (docs/design/tenant-settings-integrity.md §4.5; #1457,
    #: lanes ISO-SETTINGS-MAC and ISO-API-SETTINGS). `swarm-settings-key`, from
    #: SWARM_SETTINGS_KEY by Secret Manager reference: what swarm-api MACs every
    #: `workspace_settings` block it writes with (`swarm_api.judgment`). Empty
    #: means nothing is signed, which is every write before ISO-SETTINGS-MAC.
    #: Excluded from the repr, as `graph_ticket_key` is.
    settings_key: str = field(default="", repr=False)
    #: The previous version, accepted when VERIFYING beside the current one
    #: while `scripts/resign-workspace-settings.sh` re-signs under the new one
    #: (SWARM_SETTINGS_KEY_PREVIOUS). Empty outside a rotation.
    settings_key_previous: str = field(default="", repr=False)
    #: The tenants whose settings are acted on only when their MAC verifies
    #: (SETTINGS_MAC_TENANTS, comma-separated, or `*` for every tenant): the
    #: per-tenant cutover, owner decision Q7. EMPTY by default, so every reader
    #: behaves as before until the stored documents are re-signed and an
    #: operator lists a tenant; a tenant listed before that reads its unsigned
    #: settings as unset (fail closed).
    settings_mac_tenants: tuple[str, ...] = ()
    #: The tenants whose job-made workspace worker gets NO Firestore role
    #: (WORKER_FIRESTORE_OFF_TENANTS, comma-separated, or `*` for every
    #: tenant): #1457 design B's per-tenant cutover (owner decision Q7: dev
    #: first, u-bogdan, then eng, then all) on the in-cloud workspace path,
    #: the twin of terraform/modules/tenancy `worker_firestore_access` for a
    #: Terraform tenant. People writes `worker_firestore_access` on the record
    #: from it at every approval, retry and ceiling change, and the workspace
    #: job grants and verifies by the record (`workspaces.worker_firestore_access`).
    #: EMPTY by default, so every workspace keeps today's grant: a tenant is
    #: listed only once its worker reaches its state through swarm-api's routes,
    #: since a listed worker still on the direct path gets PermissionDenied.
    worker_firestore_off_tenants: tuple[str, ...] = ()
    #: The NAT addresses every worker's MCP call leaves through, which a
    #: registered MCP server's operator allowlists (docs/design/registered-
    #: mcp-servers.md §4.9). MCP_EGRESS_ADDRESSES, comma-separated, rendered
    #: by terraform/infra/locals.tf from the NAT's reserved addresses (the
    #: resource the `mcp_egress_addresses` output reads). Empty serves `[]`
    #: on GET /v1/mcp-servers, and the console says so in words.
    mcp_egress_addresses: tuple[str, ...] = ()
    #: GRANT ATTESTATION (docs/design/workspace-secrets.md, "What
    #: SEC-GRANT-ATTEST built"; owner decision 2026-10-15). `swarm-workspace-
    #: grant-key`, from SWARM_WORKSPACE_GRANT_KEY by Secret Manager reference
    #: (terraform/infra/workspace_grant_key.tf): what swarm-api MACs every
    #: workspace-secret grant with (`swarm_api.grant_attest`). Empty means
    #: grants are written unattested. Excluded from the repr, as `live_key` is.
    workspace_grant_key: str = field(default="", repr=False)
    #: The previous version, accepted when VERIFYING beside the current one
    #: during a rotation; never used to sign. Empty outside one.
    workspace_grant_key_previous: str = field(default="", repr=False)
    #: OFF by default, because grants written before SEC-GRANT-ATTEST carry no
    #: attestation: switched on before `POST /v1/admin/workspace-secrets/
    #: attest-grants` has re-signed them, every such grant would stop being
    #: released and a required slot would park CREDENTIAL_MISSING across
    #: every workspace at once. While off, an UNATTESTED grant is released
    #: with a `grant_attestation_missing` warning; a grant whose attestation is
    #: present but wrong is dropped either way. docs/runbooks/
    #: workspace-grant-attestation.md is the order to turn it on in.
    grant_attestation_required: bool = False
    #: Whether agent-runtime-browser carries jev-browser (JEV-IMAGE, #1296).
    #: ON by default because the image does; a deployment rolled back to an
    #: image without it sets BROWSER_JEV_INSTALLED=false, and a `browser_tool:
    #: jev` submission is then refused (`browser_tool_unavailable`) rather than
    #: run without the tool it named (BSTATUS-API).
    browser_jev_installed: bool = True

    # --- stopping a cancelled task's execution (#627) ----------------------
    #: Whether the cancel routes ask for the execution to be stopped
    #: (`swarm_api.executioncancel`). On by default in a deployment
    #: (EXECUTION_CANCEL_ENABLED), because waiting for the worker or the
    #: reconciler left executions running 7-13 h after a cancel; off in this
    #: dataclass's own default so a test or a local run never reaches Google.
    execution_cancel_enabled: bool = False

    # --- refusing a person's task on an ungranted repository (#780 D4) -----
    #: Whether a PERSON's task on a GitHub repository they hold no grant for is
    #: refused (403 REPOSITORY_NOT_GRANTED). OFF by default (owner decision
    #: 2026-10-08): the check shipped (#845) before anyone could connect GitHub
    #: and choose repositories, so it refused every person's task. While off, a
    #: person with no grant runs with the tenant token, `git`, with write, as
    #: before #780; a person WITH a grant still runs with their own slot. The
    #: deployed value is terraform's, var.repository_grants_enforced, rendered
    #: by terraform/infra/locals.tf (OB10): on in dev, where the GitHub App is
    #: registered and people can connect; off in prod, which has no App, until
    #: the owner says otherwise. A service submission is never refused either
    #: way. This default stays off so a test or a local run refuses nothing.
    repository_grants_enforced: bool = False

    # --- the forge-credential broker (#1041) -------------------------------
    #: Whether `POST /v1/attempts/forge-credential` releases a person's
    #: `git-u-` slot at all (FORGE_BROKER, terraform `forge_broker`;
    #: docs/design/user-scoped-secrets.md §5). OFF by default and in every
    #: environment until the owner turns it on: off, the route answers 503
    #: forge_credential_unavailable before reading anything, and workers read
    #: the slot with their own account as before the broker (the scheduler
    #: hands them the same switch). The bootstrap root's `forge_slot_reader`
    #: binding is removed only after this has been on in dev and a live run
    #: passed. Not a refusal switch: it turns a new route on, and the route's
    #: refusals are never switchable.
    forge_broker: bool = False

    # --- a step's additional repositories (#1434, MR-API) ------------------
    #: Whether a submission may name `additional_repositories`. OFF by
    #: default (docs/design/multi-repository-steps.md §6, "Feature flag"):
    #: while off, a submission that names any is refused
    #: `additional_repositories_disabled` and nothing is stored, so a
    #: swarm-api deployed ahead of the workers that clone them writes no task
    #: those workers would fail. The operator turns it on
    #: (ADDITIONAL_REPOSITORIES_ENABLED) after workers with lane MR-WORKER
    #: are deployed. Its limits are not here: they are platform settings an
    #: admin edits at run time (`store.PLATFORM_SETTINGS_DEFAULTS`).
    additional_repositories_enabled: bool = False

    #: The topic the reconciler stops executions from (EXECUTION_CANCEL_TOPIC,
    #: terraform's `<prefix>-execution-cancel`). The reconciler, not this
    #: service, holds the stop permissions. Empty means a cancel is left to the
    #: worker's poll and the reconciler's pass, as before.
    execution_cancel_topic: str = ""
    # --- the personal-workspace job's trigger (docs/workspaces.md §2.1, W7) -
    #: The Pub/Sub topic an approval, a retry, a ceiling change and the
    #: dispatch sweep publish a workspace id to (WORKSPACE_APPLY_TOPIC). An
    #: Eventarc trigger on it starts a Workflows execution that runs the Cloud
    #: Run job swarm-workspace-apply (WD2, re-decided 2026-10-10); all three are
    #: the owner's bootstrap apply (lane W4), so the name is the one that apply
    #: creates.
    workspace_apply_topic: str = "swarm-workspace-apply"
    #: Whether swarm-api publishes to it at all (WORKSPACE_APPLY_PUBLISH).
    #: OFF by default, because the topic and what it starts do not exist until the
    #: owner's W4 apply: publishing into a missing topic would fail every
    #: approval's dispatch and fill the sweep's attempts with noise. Off, an
    #: approval still records the decision and stays `approved`, and the sweep
    #: publishes it once this is turned on (§2.2). Tests inject a fake
    #: publisher instead of turning it on.
    workspace_apply_publish: bool = False

    # --- the search tool (docs/search-tool.md, lane SEARCH-TOOL, #1030) ----
    #: Whether `POST /v1/attempts/{id}/search` exists, and whether a worker is
    #: told to name the `search` tool to its agent (the child-key answer's
    #: `search_tool`). OFF by default (owner decision 2026-10-13): the
    #: provider key is a platform secret the operator stores first, and every
    #: query is billed to the platform. Off, the route is a 404 and no agent
    #: is offered the tool. A feature flag, not a refusal switch: its off
    #: position is "no such tool", never "let the request through".
    search_tool_enabled: bool = False
    #: Whether swarm-api's BACKGROUND GitHub reads (the merge wake's CI reads,
    #: the issue run's CI read, the stranded-PR sweep, the repository index's
    #: head poll) go out with the App's INSTALLATION token for the
    #: repository's owner (swarm_api.forge_installation, #1483) instead of the
    #: tenant's `-git` token. ON by default: on 2026-10-17 those reads, made
    #: as one person's App user token, spent that person's 5,000 calls an hour
    #: and every push test and publish acting as them failed. An owner with no
    #: installation (reached through a person's token) and a failed mint keep
    #: the tenant's token either way, so on is never less able to read than
    #: off. FORGE_INSTALLATION_READS=false turns it off.
    forge_installation_reads: bool = True
    #: Background GitHub reads (the merge wake, the after-PR gates, the
    #: stranded-PR sweep, the index poll, forgeperms' cached read) skip a
    #: tick while their credential has less than this percent of its limit
    #: left (swarm_api.forge_limits, QUOTA-THROTTLE, #1483). 20: on 2026-10-17
    #: background reads spent a person's 5,000 calls an hour and their own
    #: push tests and lane publishes were refused; a fifth of the window is
    #: what those actions spent in the busiest measured hour, so it is kept
    #: for them. Actions a person takes are never deferred. 0 turns it off.
    forge_quota_defer_percent: int = 20
    #: Which `swarm_api.search.PROVIDERS` entry answers (SEARCH_PROVIDER).
    #: Brave: the first implementation, pending the owner's choice
    #: (questions.json of lane SEARCH-TOOL).
    search_provider: str = "brave"
    #: The Secret Manager secret holding the provider's key: a PLATFORM
    #: secret, stored with `scripts/create-secrets.sh --search-api-key
    #: --stdin`, read per call, never an environment variable.
    search_api_key_secret: str = "swarm-search-api-key"
    #: Queries a tenant may make per UTC day, all its attempts together.
    #: 200: at Brave's $5 per 1,000 (read 2026-10-14) a tenant can cost at
    #: most $1 a day, and the 26 analysed browser tasks made at most a handful
    #: of searches each, so 200 is room for dozens of tasks.
    search_daily_queries_per_tenant: int = 200

    # --- model classes (docs/design/model-per-step.md §4.4, #1435) ---------
    #: Model class -> the ID a step of that class runs (WORKER_CLASS_MODELS,
    #: `parse_worker_class_models`). Empty: no class but a profile's default
    #: may be named, and `TaskCreate.model` is recorded as sent. There is no
    #: separate enablement flag (owner decision R4, 2026-10-17): this map is
    #: set by the terraform that already pins the scheduler's, and lane order
    #: kept the classes unreachable until the scheduler and worker had merged.
    #: `hash=False`: a dict is unhashable, and the settings are compared, never
    #: hashed, by this field.
    worker_class_models: dict[str, str] = field(default_factory=dict, hash=False)

    @property
    def project_id(self) -> str:
        return self.core.project_id

    @property
    def install_hostname(self) -> str:
        """`<H>`: SWARM_HOSTNAME, else `console_url`'s host, else empty."""
        if self.setup_hostname:
            return self.setup_hostname
        return (urlsplit(self.console_url).hostname or "") if self.console_url else ""

    @property
    def deployer_identity(self) -> str:
        """The engine's caller (`swarm-tf-deployer`), derived from the project id
        (docs/design/day-one-plan.md §5.3, review-fix finding 2)."""
        return f"swarm-tf-deployer@{self.project_id}.iam.gserviceaccount.com".lower()

    @property
    def doctor_identity(self) -> str:
        return (self.doctor_account
                or f"swarm-doctor@{self.project_id}.iam.gserviceaccount.com").lower()

    @property
    def hardened(self) -> bool:
        """True outside local development, where safety checks are mandatory."""
        return self.core.environment.strip().lower() not in {"dev", "test", "local"}

    @classmethod
    def from_env(cls) -> "ApiSettings":
        core = Settings.from_env()
        if not _bool("REQUIRE_AUTH", True):
            # Historically parsed and then read by nothing, which made it look
            # like a working switch in .env.example and docker-compose.yml.
            raise ValueError(
                "REQUIRE_AUTH=false is refused: this service cannot serve an "
                "unauthenticated request in any environment. Every route resolves a "
                "tenant from a verified Google ID token, and without one there is no "
                "tenant to attribute work to. Remove the variable."
            )
        # REFUSAL_<CODE> switches are read where each refusal is made
        # (swarm_api.refusals); a misspelt one is refused here, at start.
        validate_refusal_switches()
        admin_users = _csv("ADMIN_USERS")
        admin_pool_users = _csv("ADMIN_POOL_USERS")
        secret_admin_principals = _csv("SECRET_ADMIN_PRINCIPALS")
        allowed_users = _csv("ALLOWED_USERS")
        rollup_sweeper_users = _csv("ROLLUP_SWEEPER_USERS")
        platform_owner = os.environ.get("PLATFORM_OWNER", "").strip().lower()
        if platform_owner and (
            "@" not in platform_owner
            or "," in platform_owner
            or platform_owner.endswith("gserviceaccount.com")
        ):
            # One person's address. A list would make "the owner" ambiguous,
            # and a service account cannot sign in to the console to use it.
            raise ValueError("PLATFORM_OWNER must be one person's email address")
        if platform_owner and platform_owner in {u.lower() for u in admin_pool_users}:
            raise ValueError(
                "PLATFORM_OWNER and ADMIN_POOL_USERS both name the same address; "
                "a pool admin is a narrower role than the owner, not a second one"
            )
        # An unknown provider is refused here, at start, not at the first query.
        from .search import PROVIDERS as SEARCH_PROVIDERS

        search_provider = os.environ.get("SEARCH_PROVIDER", "brave").strip().lower() or "brave"
        if search_provider not in SEARCH_PROVIDERS:
            raise ValueError(
                f"SEARCH_PROVIDER must be one of {', '.join(sorted(SEARCH_PROVIDERS))}, "
                f"got {search_provider!r}"
            )
        from .groups import GROUPS_AUTH_ADMIN_ROLE, parse_groups_auth

        groups_auth = parse_groups_auth(os.environ.get("GROUPS_AUTH"))
        groups_impersonate_user = os.environ.get("GROUPS_IMPERSONATE_USER", "").strip()
        if groups_auth == GROUPS_AUTH_ADMIN_ROLE:
            # The admin role needs no subject; one still rendered beside it
            # must not quietly turn delegation back on.
            groups_impersonate_user = ""
        sweepers = {u.lower() for u in rollup_sweeper_users}
        for name, values in (
            ("PLATFORM_OWNER", (platform_owner,) if platform_owner else ()),
            ("ADMIN_USERS", admin_users),
            ("ADMIN_POOL_USERS", admin_pool_users),
            ("ALLOWED_USERS", allowed_users),
        ):
            overlap = sorted(sweepers & {v.lower() for v in values})
            if overlap:
                raise ValueError(
                    f"ROLLUP_SWEEPER_USERS and {name} both name {', '.join(overlap)}; "
                    "the rollup sweeper may call one route and is no other kind of caller"
                )
        return cls(
            core=core,
            tenant_service_accounts=_tenant_members(
                core.project_id,
                admin_lists={
                    "ADMIN_USERS": admin_users,
                    "PLATFORM_OWNER": (platform_owner,) if platform_owner else (),
                    "ADMIN_POOL_USERS": admin_pool_users,
                    "ROLLUP_SWEEPER_USERS": rollup_sweeper_users,
                    "SECRET_ADMIN_PRINCIPALS": secret_admin_principals,
                },
            ),
            # One per backend service fronting this platform. Terraform sets
            # it; without it the IAP path is OFF rather than unpinned, because
            # an unpinned audience accepts an assertion minted by IAP for any
            # backend in any project.
            iap_audiences=_csv("IAP_AUDIENCES"),
            tenant_groups=_csv("TENANT_GROUPS"),
            admin_groups=_csv("ADMIN_GROUPS"),
            admin_users=admin_users,
            platform_owner=platform_owner,
            platform_admin_reach=_bool("PLATFORM_ADMIN_REACH", False),
            platform_memberships=_bool("PLATFORM_MEMBERSHIPS", False),
            join_open_per_person=_positive("JOIN_OPEN_PER_PERSON", 3),
            join_daily_per_person=_positive("JOIN_DAILY_PER_PERSON", 10),
            join_denied_hold_days=_positive("JOIN_DENIED_HOLD_DAYS", 7),
            join_open_per_workspace=_positive("JOIN_OPEN_PER_WORKSPACE", 50),
            role_cache_seconds=float(_int("ROLE_CACHE_SECONDS", 15)),
            admin_pool_users=admin_pool_users,
            rollup_sweeper_users=rollup_sweeper_users,
            schedule_tick_users=_csv("SCHEDULE_TICK_USERS"),
            allowed_users=allowed_users,
            secret_admin_principals=secret_admin_principals,
            groups_impersonate_user=groups_impersonate_user,
            groups_auth=groups_auth,
            group_cache_ttl_seconds=_int("GROUP_CACHE_TTL_SECONDS", 120),
            workspace_registry_cache_seconds=float(
                _int("WORKSPACE_REGISTRY_CACHE_SECONDS", 60)),
            workspace_registry_stale_seconds=float(
                _int("WORKSPACE_REGISTRY_STALE_SECONDS", 600)),
            dispatch_topic=os.environ.get("DISPATCH_TOPIC", "").strip(),
            execution_cancel_enabled=_bool("EXECUTION_CANCEL_ENABLED", True),
            repository_grants_enforced=_bool("REPOSITORY_GRANTS_ENFORCED", False),
            forge_broker=_bool("FORGE_BROKER", False),
            additional_repositories_enabled=_bool("ADDITIONAL_REPOSITORIES_ENABLED", False),
            execution_cancel_topic=os.environ.get("EXECUTION_CANCEL_TOPIC", "").strip(),
            workspace_apply_topic=os.environ.get(
                "WORKSPACE_APPLY_TOPIC", "swarm-workspace-apply"
            ).strip()
            or "swarm-workspace-apply",
            workspace_apply_publish=_bool("WORKSPACE_APPLY_PUBLISH", False),
            quota_broker_url=os.environ.get("QUOTA_BROKER_URL", "").strip(),
            quota_broker_audience=os.environ.get("QUOTA_BROKER_AUDIENCE", "").strip(),
            max_page_size=_int("MAX_PAGE_SIZE", 200),
            default_page_size=_int("DEFAULT_PAGE_SIZE", 50),
            object_scan_limit=_int("OBJECT_SCAN_LIMIT", 5000),
            max_log_bytes=_int("MAX_LOG_BYTES", 1024 * 1024),
            default_log_bytes=_int("DEFAULT_LOG_BYTES", 64 * 1024),
            min_log_bytes=_int("MIN_LOG_BYTES", 4 * 1024),
            rate_limit_burst=_int("RATE_LIMIT_BURST", max(40, core.requests_per_second * 2)),
            tenant_service_account_prefix=os.environ.get(
                "TENANT_SERVICE_ACCOUNT_PREFIX", "swarm-agent-worker"
            ).strip()
            or "swarm-agent-worker",
            tenant_namespace_prefix=os.environ.get(
                "TENANT_NAMESPACE_PREFIX", "swarm-tenant-"
            ).strip()
            or "swarm-tenant-",
            spec_signing_key_version=os.environ.get("SPEC_SIGNING_KEY_VERSION", "").strip(),
            spec_verify_keys=os.environ.get("SPEC_VERIFY_KEYS", "").strip(),
            # Read beside `Settings.from_env`, which reads the same variable and
            # substitutes "dev" when it is absent. Blank counts as absent: a
            # variable that exists and says nothing has declared nothing.
            environment_declared=bool(os.environ.get("ENVIRONMENT", "").strip()),
            console_url=os.environ.get("SWARM_CONSOLE_URL", "").strip(),
            setup_github_org=os.environ.get("SETUP_GITHUB_ORG", "").strip(),
            setup_hostname=os.environ.get("SWARM_HOSTNAME", "").strip().lower(),
            setup_github_hooks_url=os.environ.get("GITHUB_HOOKS_URL", "").strip(),
            doctor_account=os.environ.get("SWARM_DOCTOR_ACCOUNT", "").strip().lower(),
            qa_identity_console_account=os.environ.get(
                "QA_IDENTITY_CONSOLE_ACCOUNT", "").strip().lower(),
            qa_identity_lifetime_seconds=_int("QA_IDENTITY_LIFETIME_SECONDS", 900),
            internal_decision_api_url=os.environ.get("INTERNAL_DECISION_API_URL", "").strip(),
            child_key=os.environ.get("SWARM_CHILD_KEY", "").strip(),
            child_key_previous=os.environ.get("SWARM_CHILD_KEY_PREVIOUS", "").strip(),
            child_audience=os.environ.get("SWARM_API_AUDIENCE", "").strip(),
            max_children_per_task=_int("MAX_CHILDREN_PER_TASK", 16),
            child_proof_skew_seconds=_int("CHILD_PROOF_SKEW_SECONDS", 120),
            live_key=os.environ.get("SWARM_LIVE_KEY", "").strip(),
            live_key_previous=os.environ.get("SWARM_LIVE_KEY_PREVIOUS", "").strip(),
            live_relay_url=os.environ.get("SWARM_LIVE_URL", "").strip(),
            live_tunnel_ticket_seconds=_int("LIVE_TUNNEL_TICKET_SECONDS", 60),
            live_viewer_ticket_seconds=_int("LIVE_VIEWER_TICKET_SECONDS", 30),
            live_control_renew_seconds=_int("LIVE_CONTROL_RENEW_SECONDS", 60),
            control_idle_seconds=_int("CONTROL_IDLE_SECONDS", 300),
            live_membership_recheck_seconds=float(
                _int("LIVE_MEMBERSHIP_RECHECK_SECONDS", 300)),
            live_session_poll_seconds=float(_int("LIVE_SESSION_POLL_SECONDS", 2)),
            live_input_flush_seconds=float(_int("LIVE_INPUT_FLUSH_SECONDS", 15)),
            live_viewer_tickets_per_minute=_int("LIVE_VIEWER_TICKETS_PER_MINUTE", 30),
            live_takes_per_minute=_int("LIVE_TAKES_PER_MINUTE", 6),
            human_unwatched_seconds=_int("HUMAN_UNWATCHED_SECONDS", 60),
            human_hold_max_seconds=_int("HUMAN_HOLD_MAX_SECONDS", 1800),
            live_watch_flush_seconds=float(_int("LIVE_WATCH_FLUSH_SECONDS", 15)),
            max_handoffs_per_attempt=_int("MAX_HANDOFFS_PER_ATTEMPT", 3),
            egress_key=os.environ.get("SWARM_EGRESS_KEY", "").strip(),
            egress_ticket_seconds=_int("EGRESS_TICKET_SECONDS", 600),
            browser_egress_enabled=_bool("BROWSER_EGRESS_ENABLED", False),
            browser_egress_tenants=_csv("BROWSER_EGRESS_TENANTS"),
            browser_egress_profiles=_csv("BROWSER_EGRESS_PROFILES", "claude-code-browser"),
            browser_egress_country=os.environ.get("BROWSER_EGRESS_COUNTRY", "us").strip().lower()
            or "us",
            typesafe_fwd_key=os.environ.get("SWARM_TYPESAFE_FWD_KEY", "").strip(),
            typesafe_ticket_seconds=_int("TYPESAFE_TICKET_SECONDS", 300),
            graph_ticket_key=os.environ.get("SWARM_GRAPH_TICKET_KEY", "").strip(),
            graph_ticket_seconds=_int("GRAPH_TICKET_SECONDS", 600),
            graph_stats_url=os.environ.get("SWARM_GRAPH_STATS_URL", "").strip(),
            settings_key=os.environ.get("SWARM_SETTINGS_KEY", "").strip(),
            settings_key_previous=os.environ.get("SWARM_SETTINGS_KEY_PREVIOUS", "").strip(),
            settings_mac_tenants=_csv("SETTINGS_MAC_TENANTS"),
            worker_firestore_off_tenants=_csv("WORKER_FIRESTORE_OFF_TENANTS"),
            mcp_egress_addresses=_csv("MCP_EGRESS_ADDRESSES"),
            workspace_grant_key=os.environ.get("SWARM_WORKSPACE_GRANT_KEY", "").strip(),
            workspace_grant_key_previous=os.environ.get(
                "SWARM_WORKSPACE_GRANT_KEY_PREVIOUS", "").strip(),
            grant_attestation_required=_bool("GRANT_ATTESTATION_REQUIRED", False),
            browser_jev_installed=_bool("BROWSER_JEV_INSTALLED", True),
            search_tool_enabled=_bool("SEARCH_TOOL_ENABLED", False),
            forge_installation_reads=_bool("FORGE_INSTALLATION_READS", True),
            forge_quota_defer_percent=_percent("FORGE_QUOTA_DEFER_PERCENT", 20),
            search_provider=search_provider,
            search_api_key_secret=os.environ.get(
                "SEARCH_API_KEY_SECRET", "swarm-search-api-key").strip()
            or "swarm-search-api-key",
            search_daily_queries_per_tenant=_int("SEARCH_DAILY_QUERIES_PER_TENANT", 200),
            worker_class_models=parse_worker_class_models(
                os.environ.get("WORKER_CLASS_MODELS", "")
            ),
        )
