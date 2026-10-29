"""Admin › Setup and Health (docs/design/day-one-plan.md §5; D26, D27; lane
DAYONE-SETUP-API-1, part of #2688).

    GET  /v1/admin/setup                          the page's whole state
    POST /v1/admin/setup/github-app/manifest      the App manifest and form target
    POST /v1/admin/setup/github-app/conversion    state check, code exchange, 3 secrets
    GET  /v1/admin/setup/github-app/installation  is the App on the org yet
    POST /v1/admin/setup/github-app/self-check    the `/admin/forge/app` check, recorded
    POST /v1/admin/setup/github-app/hook          turn the webhook on (engine or admin)
    POST /v1/admin/setup/links                    a one-time link, three kinds
    POST /v1/admin/setup/teams                    a team: its workspace record and `teams/<id>`
    POST /v1/admin/setup/finish                   `console/finished`, once every step is done
    PUT  /v1/admin/setup/health                   `health/latest` (swarm-doctor only)
    GET  /v1/admin/setup/health                   `health/latest`
    POST /v1/admin/setup/health/groups-probe      registered groups readable, met/unmet
    POST /v1/setup/links/redeem                   a link's named person opens it

WHO MAY CALL (§5.2, §5.3). Every `/v1/admin/setup/*` route is a PLATFORM
admin's: a workspace admin, a pool admin or a member is 403. Two service
identities are admitted by their verified email and only on named routes: the
engine's deployer `swarm-tf-deployer@<project>.iam.gserviceaccount.com` on the
hook and the groups probe (review-fix finding 2), and `swarm-doctor` on the
health write and the groups probe. The redeem route is the narrow exception
the links are: any signed-in person, for a link minted for their own email.

WHAT IS WRITTEN (§5.4). The console's documents under `swarm-install/<env>/`
and `health/latest`, through `install_record.InstallRecord`, and the link
digests under `setup_links/`. The engine's document `swarm-install/<env>` is
read and never written. No answer, log record or Firestore field carries a
secret: the App's three secrets go from GitHub's answer straight into Secret
Manager, and a link's value straight into its one container.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .. import install_record as ir
from .. import roles
from .. import setup_links as sl
from .. import workspaces as ws
from ..auth import TENANT_HEADER, AuthContext, bearer_tokens, require_admin
from ..codec import console_origin
from ..deps import AppContext, admin_auth, current_auth, get_context
from ..errors import Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed
from ..groups import GroupLookupError, MembershipResolver, check_group_readable
from ..people import People
from ..schemas import StrictModel
from ..setup_github import (
    GitHubSetupApi,
    ManifestFlow,
    SecretManagerSetupSecrets,
    SetupSecrets,
    SetupUnconfigured,
)
from .accounts import account_pool
from .people import name_first_admin, people_service, workspace_directory

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/setup", tags=["setup"])
links_router = APIRouter(prefix="/v1/setup", tags=["setup"])

PREFIX = "/v1/admin/setup"
HOOK = ("POST", f"{PREFIX}/github-app/hook")
GROUPS_PROBE = ("POST", f"{PREFIX}/health/groups-probe")
HEALTH_WRITE = ("PUT", f"{PREFIX}/health")

ENGINE = "engine"
DOCTOR = "doctor"
ADMIN = "admin"

#: The routes each service identity may call, and no other (review-fix
#: finding 2, review finding 12).
SERVICE_ROUTES: dict[str, frozenset[tuple[str, str]]] = {
    ENGINE: frozenset({HOOK, GROUPS_PROBE}),
    DOCTOR: frozenset({HEALTH_WRITE, GROUPS_PROBE}),
}
#: Routes no platform admin may call: only the doctor writes its findings.
SERVICE_ONLY: frozenset[tuple[str, str]] = frozenset({HEALTH_WRITE})

#: The CLI holds the install lease while it runs; a heartbeat older than this
#: reads as "swarm install is not running" (§5.1).
LEASE_STALE = timedelta(minutes=2)

#: Design §12's add-ons. Enabling one is CLI-only (it needs a Terraform
#: switch); its secret goes in through an `addon-secret` link.
ADDONS: tuple[str, ...] = ("bright-data-egress", "tailscale", "typesafe", "brave-search", "ci")


@dataclass(frozen=True)
class SetupCaller:
    kind: str
    email: str
    auth: AuthContext | None = None


def _service_identity(ctx: AppContext, authorization: str | None,
                      serverless_authorization: str | None,
                      iap_assertion: str | None) -> tuple[str, str] | None:
    """(kind, email) when the verified caller is the deployer or the doctor.

    Verified by the authenticator's own verifiers, never by trusting a claim:
    the IAP assertion (the engine reaches swarm-api through the load balancer)
    or a Google ID token (the doctor job). `_verifier` is read because
    `auth.py` is not this lane's to change, as `routes.people.workspace_directory`
    reads `_groups`. Anything that does not verify is not a service identity,
    and the request then goes the ordinary way and is answered by it."""
    claims: dict[str, Any] | None = None
    authenticator = ctx.authenticator
    if iap_assertion:
        iap = getattr(authenticator, "iap", None)
        if iap is not None and getattr(iap, "configured", False):
            try:
                claims = iap.verify(iap_assertion)
            except Exception:  # noqa: BLE001 - not verified is not a service identity
                claims = None
    else:
        verifier = getattr(authenticator, "_verifier", None)
        for token in bearer_tokens(serverless_authorization or authorization) if verifier else ():
            try:
                claims = verifier.verify(token)
                break
            except Exception:  # noqa: BLE001
                continue
    if not claims or claims.get("email_verified") is not True:
        return None
    email = str(claims.get("email", "")).strip().lower()
    if email and email == ctx.settings.deployer_identity:
        return ENGINE, email
    if email and email == ctx.settings.doctor_identity:
        return DOCTOR, email
    return None


def setup_caller(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"),
    iap_assertion: str | None = Header(default=None, alias="X-Goog-IAP-JWT-Assertion"),
    requested_tenant: str | None = Header(default=None, alias=TENANT_HEADER),
    ctx: AppContext = Depends(get_context),
) -> SetupCaller:
    """The one gate of every `/v1/admin/setup/*` route (§5.2, §5.3)."""
    route = (request.method.upper(), request.url.path.rstrip("/"))
    service = _service_identity(ctx, authorization, serverless_authorization, iap_assertion)
    if service is not None:
        kind, email = service
        if route not in SERVICE_ROUTES[kind]:
            log.info("setup route refused to a service identity kind=%s route=%s %s",
                     kind, route[0], route[1])
            raise Forbidden(f"this identity may not call {route[0]} {route[1]}")
        return SetupCaller(kind=kind, email=email)
    auth = current_auth(request, authorization, serverless_authorization, iap_assertion,
                        requested_tenant, ctx)
    if route in SERVICE_ONLY:
        raise Forbidden(f"only swarm-doctor may call {route[0]} {route[1]}")
    # No route is handed over: the full platform-admin rule, so a pool admin
    # or a workspace admin is refused here exactly as a member is.
    require_admin(auth, None)
    return SetupCaller(kind=ADMIN, email=auth.email, auth=auth)


#: The gate of every Admin › Setup route no service identity may call, on
#: the route itself: `admin_auth`, the dependency every `/v1/admin` route
#: declares, so the admin surface's sweeps (tests/unit/control_plane/
#: test_pool_admin_is_narrow.py) see these routes as admin-gated. A platform
#: admin passes; a pool admin, a workspace admin, a member, the deployer and
#: the doctor are refused (none of these routes is in POOL_ADMIN_ROUTES).
#: Only the hook, the groups probe and the health write go through
#: `setup_caller`, because a service identity must reach them.


def admin_setup_caller(auth: AuthContext = Depends(current_auth)) -> SetupCaller:
    """The caller of a route `admin_auth` already let through: the same
    `current_auth` answer, which FastAPI resolves once per request."""
    return SetupCaller(kind=ADMIN, email=auth.email, auth=auth)


# --------------------------------------------------------------------------
# collaborators, injectable on app.state
# --------------------------------------------------------------------------


def install_record(ctx: AppContext = Depends(get_context)) -> ir.InstallRecord:
    return ir.InstallRecord(ctx.db, ctx.settings.core.environment)


def setup_secrets(request: Request, ctx: AppContext = Depends(get_context)) -> SetupSecrets:
    """Secret Manager, built once per process; a test sets
    `app.state.setup_secrets` to a fake."""
    found = getattr(request.app.state, "setup_secrets", None)
    if found is None:
        found = SecretManagerSetupSecrets(ctx.settings.project_id)
        request.app.state.setup_secrets = found
    return found


def setup_github_api(request: Request) -> GitHubSetupApi:
    found = getattr(request.app.state, "setup_github_api", None)
    if found is None:
        found = GitHubSetupApi()
        request.app.state.setup_github_api = found
    return found


def manifest_flow(
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    secrets_: SetupSecrets = Depends(setup_secrets),
    github: GitHubSetupApi = Depends(setup_github_api),
) -> ManifestFlow:
    return ManifestFlow(record=record, secrets_=secrets_, github=github, now=ctx.now)


def links_service(ctx: AppContext = Depends(get_context)) -> sl.SetupLinks:
    return sl.SetupLinks(ctx.db, now=ctx.now)


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _cli_running(engine: dict[str, Any] | None, now: datetime) -> bool:
    lease = (engine or {}).get("lease") or {}
    beat = _as_datetime(lease.get("heartbeat"))
    return beat is not None and now - beat <= LEASE_STALE


def _github_view(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    view = {k: v for k, v in doc.items() if k != "state_hash"}
    view["started"] = bool(doc.get("state_hash")) or bool(doc.get("id"))
    return view


def _accounts_view(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Per label: owner, link expiry, signed-in time. Never the link hash."""
    return {label: {k: v for k, v in (value or {}).items() if k != "link_hash"}
            for label, value in (doc or {}).items()}


@router.get("", dependencies=[Depends(admin_auth)])
def setup_state(
    caller: SetupCaller = Depends(admin_setup_caller),  # noqa: ARG001 - the gate
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
) -> dict:
    engine = record.read_engine()
    return {
        "env": record.env,
        "engine": engine,
        "cli_running": _cli_running(engine, ctx.now()),
        "github_app": _github_view(record.read(record.console_path(ir.GITHUB_APP))),
        "teams": [{"id": team_id, **doc} for team_id, doc in record.teams()],
        "accounts": _accounts_view(record.read(record.console_path(ir.ACCOUNTS))),
        "finished": record.read(record.console_path(ir.FINISHED)),
        "addons": [{"name": name, "enable": f"swarm addon enable {name}",
                    "secret_link": name in sl.ADDON_SECRETS} for name in ADDONS],
        "hostname": ctx.settings.install_hostname or None,
        "github_org": ctx.settings.setup_github_org or None,
    }


# --------------------------------------------------------------------------
# the GitHub App
# --------------------------------------------------------------------------


class ManifestStart(StrictModel):
    #: The App's display name; GitHub caps App names at 34 characters.
    name: str | None = Field(default=None, min_length=1, max_length=34)


class Conversion(StrictModel):
    code: str = Field(min_length=1, max_length=256)
    state: str = Field(min_length=1, max_length=256)


@router.post("/github-app/manifest", dependencies=[Depends(admin_auth)])
def github_app_manifest(
    body: ManifestStart | None = None,
    caller: SetupCaller = Depends(admin_setup_caller),  # noqa: ARG001 - the gate
    ctx: AppContext = Depends(get_context),
    flow: ManifestFlow = Depends(manifest_flow),
) -> dict:
    settings = ctx.settings
    name = (body.name if body and body.name else f"SwarmCloud {settings.core.environment}")
    return flow.start(org=settings.setup_github_org, hostname=settings.install_hostname,
                      hooks_url=settings.setup_github_hooks_url, name=name)


@router.post("/github-app/conversion", dependencies=[Depends(admin_auth)])
def github_app_conversion(
    body: Conversion,
    caller: SetupCaller = Depends(admin_setup_caller),
    flow: ManifestFlow = Depends(manifest_flow),
) -> dict:
    result = flow.exchange(code=body.code, state=body.state)
    log.info("github app created by the console app_id=%s by=%s", result["id"], caller.email)
    return result


@router.get("/github-app/installation", dependencies=[Depends(admin_auth)])
def github_app_installation(
    caller: SetupCaller = Depends(admin_setup_caller),  # noqa: ARG001 - the gate
    ctx: AppContext = Depends(get_context),
    flow: ManifestFlow = Depends(manifest_flow),
) -> dict:
    if not ctx.settings.setup_github_org:
        raise SetupUnconfigured("SETUP_GITHUB_ORG is not set")
    return flow.installation(ctx.settings.setup_github_org)


@router.post("/github-app/self-check", dependencies=[Depends(admin_auth)])
def github_app_self_check(
    request: Request,
    caller: SetupCaller = Depends(admin_setup_caller),  # noqa: ARG001 - the gate
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
) -> dict:
    """`ForgeApp.check_app_key` (the `/v1/admin/forge/app` check), recorded as
    `github_app.self_check`. It answers `{ok, app_id, slug, reason}` only."""
    checked = request.app.state.forge_app.check_app_key()
    result = {"ok": bool(checked.get("ok")), "reason": checked.get("reason"), "at": ctx.now()}
    record.write(record.console_path(ir.GITHUB_APP), {"self_check": result})
    return result


@router.post("/github-app/hook")
def github_app_hook(
    caller: SetupCaller = Depends(setup_caller),
    ctx: AppContext = Depends(get_context),
    flow: ManifestFlow = Depends(manifest_flow),
) -> dict:
    """Turn the App's webhook on. The App JWT is signed HERE, with the key only
    swarm-api reads; the request carries nothing and the answer is `{active}`
    (review finding 10)."""
    result = flow.activate_hook(ctx.settings.setup_github_hooks_url)
    log.info("github app webhook turned on by=%s", caller.kind)
    return result


# --------------------------------------------------------------------------
# teams
# --------------------------------------------------------------------------


class TeamCreate(StrictModel):
    group: str = Field(min_length=3, max_length=254)
    display_name: str | None = Field(default=None, max_length=ws.MAX_DISPLAY_NAME)
    first_admin: str | None = Field(default=None, max_length=320)
    invites: list[str] = Field(default_factory=list, max_length=200)


@router.post("/teams", dependencies=[Depends(admin_auth)])
def create_team(
    body: TeamCreate,
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
    record: ir.InstallRecord = Depends(install_record),
) -> JSONResponse:
    """A team: the group workspace through the existing workspace path
    (`People.create`, as `POST /v1/admin/workspaces`; `swarm-workspace-apply`
    then does the namespace, quota, verify-keys and identity, the one writer
    of design §5.7), its first admin, and `teams/<id>`."""
    first_admin = roles.subject_for_write(body.first_admin) if body.first_admin else None
    auth = caller.auth
    status, workspace = people.create(
        ctx.submissions.workspaces, kind=ws.GROUP, principal=body.group,
        display_name=body.display_name, limits=None, by=caller.email,
        is_owner=bool(auth.is_owner) if auth is not None else False)
    content = people.admin_view(workspace)
    granted = None
    if first_admin is not None:
        granted = name_first_admin(ctx, workspace, first_admin, by=caller.email)
    team_id = str(workspace.get("tenant_id") or content.get("workspace_id"))
    existing = record.read(record.team_path(team_id)) or {}
    progress = dict(existing.get("progress") or {})
    progress.setdefault("created_at", ctx.now())
    progress["workspace_state"] = workspace.get("state")
    record.write(record.team_path(team_id), {
        "group": str(workspace.get("principal") or body.group).lower(),
        "workspace": team_id,
        "first_admin": first_admin,
        "invites": sorted({i.strip().lower() for i in body.invites if i.strip()}),
        "progress": progress,
    })
    return JSONResponse(status_code=status, content={
        "team": team_id, "workspace": content, "first_admin": granted})


def _first_team(record: ir.InstallRecord) -> tuple[str, dict[str, Any]] | None:
    teams = record.teams()
    return teams[0] if teams else None


# --------------------------------------------------------------------------
# one-time links
# --------------------------------------------------------------------------


class LinkCreate(StrictModel):
    kind: Literal["pool-account", "team-git", "addon-secret"]
    #: The pool account's label, the add-on's name; for `team-git`, the team.
    label: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    #: The person the link is for: their verified email opens it.
    owner: str = Field(min_length=3, max_length=320)


def _is_member(directory: MembershipResolver | None, email: str, group: str) -> bool:
    if directory is None:
        raise UpstreamUnavailable("this deployment has no group directory to ask")
    try:
        return bool(directory.groups_for(email, (group,), fresh=True))
    except GroupLookupError:
        raise UpstreamUnavailable("the group directory did not answer") from None


@router.post("/links", status_code=201, dependencies=[Depends(admin_auth)])
def mint_link(
    body: LinkCreate,
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    links: sl.SetupLinks = Depends(links_service),
    directory: MembershipResolver | None = Depends(workspace_directory),
) -> dict:
    """One route, three kinds (review finding 21). The token is in this answer
    and nowhere else; Firestore keeps its digest."""
    owner = roles.subject_for_write(body.owner)
    tenant_id: str | None = None
    team_id: str | None = None
    if body.kind == sl.POOL_ACCOUNT:
        first = _first_team(record)
        if first is None:
            raise Conflict("create the first team first: a pool account is owned by it")
        team_id, team = first
        tenant_id = str(team.get("workspace") or team_id)
        if not _is_member(directory, owner, str(team.get("group") or "")):
            raise Forbidden(f"{owner} is not a member of the first team's group; "
                            "invite them to it first")
    elif body.kind == sl.TEAM_GIT:
        team = record.read(record.team_path(body.label))
        if team is None:
            raise NotFound(f"no team {body.label!r}")
        team_id = body.label
        tenant_id = str(team.get("workspace") or team_id)
    elif body.label not in sl.ADDON_SECRETS:
        raise ValidationFailed(f"no add-on secret for {body.label!r}",
                               detail={"addons": sorted(sl.ADDON_SECRETS)})
    token, link = links.mint(kind=body.kind, label=body.label, owner=owner, by=caller.email,
                             tenant_id=tenant_id, team_id=team_id)
    if body.kind == sl.POOL_ACCOUNT:
        record.write(record.console_path(ir.ACCOUNTS), {body.label: {
            "owner": owner, "link_hash": sl.digest(token), "link_expires": link["expires_at"],
        }})
    elif body.kind == sl.TEAM_GIT and team_id is not None:
        team = record.read(record.team_path(team_id)) or {}
        progress = dict(team.get("progress") or {})
        progress["git_link"] = {"owner": owner, "expires_at": link["expires_at"]}
        record.write(record.team_path(team_id), {"progress": progress})
    path = sl.FORMS[body.kind].format(token=token)
    base = console_origin(ctx.settings.console_url)
    log.info("minted a setup link kind=%s label=%s", body.kind, body.label)
    return {"kind": body.kind, "label": body.label, "owner": owner,
            "url": f"{base}{path}" if base else None, "path": path,
            "expires_at": link["expires_at"].isoformat()}


class LinkRedeem(StrictModel):
    token: str = Field(min_length=16, max_length=128)
    #: The team git slot's or the add-on's value, for those kinds only.
    value: str | None = Field(default=None, min_length=1, max_length=16384)


@links_router.post("/links/redeem")
def redeem_link(
    body: LinkRedeem,
    auth: AuthContext = Depends(current_auth),
    links: sl.SetupLinks = Depends(links_service),
    secrets_: SetupSecrets = Depends(setup_secrets),
) -> dict:
    """Open a link as the person it was minted for, once.

    `pool-account` answers the first team's workspace id and the label; the
    console then runs the existing sign-in (`POST /v1/accounts/authorize`,
    then `/exchange`) with that id as `X-Swarm-Tenant`, so the account lands
    in the first team. `team-git` and `addon-secret` take the value in the same
    request and write it straight into their one container; it is never
    returned or logged."""
    link = links.redeem(body.token, email=auth.email)
    kind = link.get("kind")
    if kind == sl.POOL_ACCOUNT:
        return {"kind": kind, "label": link.get("label"), "tenant": link.get("tenant_id"),
                "sign_in": {"authorize": "/v1/accounts/authorize",
                            "exchange": "/v1/accounts/exchange",
                            "header": TENANT_HEADER}}
    if body.value is None:
        raise ValidationFailed(f"a {kind} link is opened with its value; the link is now used, "
                               "ask the admin for a new one")
    if kind == sl.TEAM_GIT:
        secret_id = sl.team_git_secret(str(link.get("tenant_id")))
    else:
        secret_id = sl.ADDON_SECRETS[str(link.get("label"))]
    secrets_.add_version(secret_id, body.value or "")
    log.info("a setup link wrote its secret kind=%s label=%s", kind, link.get("label"))
    return {"kind": kind, "label": link.get("label"), "secret": secret_id, "written": True}


# --------------------------------------------------------------------------
# finished
# --------------------------------------------------------------------------


@router.post("/finish", dependencies=[Depends(admin_auth)])
def finish(
    request: Request,
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
) -> dict:
    """Write `console/finished` once every required console step is done: the
    App installed and its self-check passed, a first team, and a pool account
    signed in to it. A labelled account seen in the first team's pool gets its
    `signed_in_at` here. The CLI starts verify when this document exists."""
    app = record.read(record.console_path(ir.GITHUB_APP)) or {}
    first = _first_team(record)
    accounts = record.read(record.console_path(ir.ACCOUNTS)) or {}
    missing: list[str] = []
    if not app.get("installed_on"):
        missing.append("github_app_installed")
    if not (app.get("self_check") or {}).get("ok"):
        missing.append("github_app_self_check")
    if first is None:
        missing.append("first_team")
    signed = {label for label, value in accounts.items() if (value or {}).get("signed_in_at")}
    if first is not None and accounts:
        tenant = str(first[1].get("workspace") or first[0])
        listing = account_pool(request, ctx).list_accounts(tenant)
        present = {str(a.get("label") or "") for a in listing.get("accounts") or []
                   if isinstance(a, dict) and a.get("owner_tenant") == tenant}
        now = ctx.now()
        newly = {label: {**(accounts[label] or {}), "signed_in_at": now}
                 for label in accounts if label in present and label not in signed}
        if newly:
            record.write(record.console_path(ir.ACCOUNTS), newly)
            signed |= set(newly)
    if not signed:
        missing.append("pool_account_signed_in")
    if missing:
        raise Conflict("setup is not finished yet", detail={"missing": missing})
    done = record.read(record.console_path(ir.FINISHED))
    if done is None:
        done = record.write(record.console_path(ir.FINISHED), {"at": ctx.now(), "by": caller.email})
    return {"finished": done}


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


class HealthReport(StrictModel):
    run_at: datetime
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)


@router.put("/health")
def put_health(
    body: HealthReport,
    caller: SetupCaller = Depends(setup_caller),  # noqa: ARG001 - the gate: doctor only
    record: ir.InstallRecord = Depends(install_record),
) -> dict:
    written = record.write(record.health_path(), {
        "run_at": body.run_at, "checks": body.checks, "findings": body.findings})
    return {"run_at": written["run_at"], "checks": len(body.checks),
            "findings": len(body.findings)}


@router.get("/health", dependencies=[Depends(admin_auth)])
def get_health(
    caller: SetupCaller = Depends(admin_setup_caller),  # noqa: ARG001 - the gate
    record: ir.InstallRecord = Depends(install_record),
) -> dict:
    return {"latest": record.read(record.health_path())}


@router.post("/health/groups-probe")
def groups_probe(
    caller: SetupCaller = Depends(setup_caller),
    ctx: AppContext = Depends(get_context),
    directory: MembershipResolver | None = Depends(workspace_directory),
) -> dict:
    """Stage 8's and the doctor's re-probe, asked with swarm-api's OWN group
    identity (review finding 12): one per-group membership question per
    registered group (`searchTransitiveGroups` 403s here), past the cache. Any
    answer proves the group readable (`met`); a failed lookup is `unmet`. The
    answer is met/unmet per group, never a member list or a membership."""
    authenticator = ctx.authenticator
    registered = getattr(authenticator, "registered_group_workspaces", None)
    groups: list[str] = list(ctx.settings.tenant_groups) + list(ctx.settings.admin_groups)
    if callable(registered):
        try:
            groups += list(registered().values())
        except Exception:  # noqa: BLE001 - the configured groups still answer
            log.warning("groups probe: the registry could not be read")
    seen: list[str] = []
    for group in groups:
        key = group.strip().lower()
        if key and key not in seen:
            seen.append(key)
    results = []
    for group in seen:
        if directory is None:
            results.append({"group": group, "state": "unmet", "reason": "no_directory"})
            continue
        try:
            check_group_readable(directory, caller.email, group)
            results.append({"group": group, "state": "met"})
        except GroupLookupError:
            results.append({"group": group, "state": "unmet", "reason": "unreadable"})
    log.info("groups probe by=%s groups=%d unmet=%d", caller.kind, len(results),
             sum(1 for r in results if r["state"] != "met"))
    return {"groups": results, "met": all(r["state"] == "met" for r in results)}


__all__ = ["admin_setup_caller", "links_router", "router", "setup_caller"]
