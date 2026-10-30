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
    POST /v1/admin/setup/teams/{id}/invites       an invite and its Google Admin members page
    POST /v1/admin/setup/smoke                    the first team's smoke, again after a failure
    POST /v1/admin/setup/verify-runs              the console's two verify runs, as the first team
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
read and never written.

THE FIRST TEAM (lane DAYONE-SETUP-API-2). Its row is read live on every page
read: the workspace's state, each invitee's membership per the Groups API,
its pool accounts. Creating it records a team-level grant over the App
installation's repositories (`teams/<id>.grant`, decision 3). Once its first
pool account lands, swarm-api runs its smoke, one small task on a granted
repository, recorded as `teams/<id>.smoke`; `finish` waits for it to pass.
The smoke and the console's verify runs are submitted as swarm-api's own
service account in the team's tenant, so they run on the team's own slot and
never on a person's GitHub connection. No answer, log record or Firestore field carries a
secret: the App's three secrets go from GitHub's answer straight into Secret
Manager, and a link's value straight into its one container.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field

from .. import install_record as ir
from .. import roles
from .. import setup_links as sl
from .. import workspaces as ws
from ..auth import TENANT_HEADER, AuthContext, bearer_tokens, require_admin
from ..codec import console_origin
from ..deps import AppContext, admin_auth, current_auth, get_context
from ..errors import ApiError, Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed
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
# the first team: its repository grant, its live row, its smoke, the console's
# verify runs (lane DAYONE-SETUP-API-2, decisions 3 and 4)
# --------------------------------------------------------------------------

#: The Google Admin page that adds members to a group, by the group's id (the
#: Cloud Identity `groups/<id>` name's last segment, which is the Directory
#: id the Admin console uses). Google has no API this deployment may call to
#: add a member (decision 4: no Groups admin grant), so the person adds them
#: there and the row polls membership through the Groups API.
ADMIN_GROUP_MEMBERS = "https://admin.google.com/ac/groups/{group_id}/members"
#: When the group's id cannot be looked up: the Admin console's group list.
ADMIN_GROUPS = "https://admin.google.com/ac/groups"

#: Pages of `GET /installation/repositories` read at most (100 a page).
MAX_REPOSITORY_PAGES = 10

SMOKE_PROMPT = ("Add a file SMOKE.md saying this deployment's first team ran its smoke task, "
                "and commit it. Do not open a pull request.")
VERIFY_TASK_PROMPT = ("Add a file VERIFY.md saying this deployment ran a single task from the "
                      "console, and open a pull request titled 'swarm install verify'.")
RUN_PROFILE = "claude-code"
SMOKE_LABEL = "swarm-install-smoke"
VERIFY_LABEL = "swarm-install-verify"

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
_DONE_STATES = {"SUCCEEDED": SUCCEEDED, "FAILED": FAILED, "CANCELLED": FAILED,
                "DEAD_LETTERED": FAILED}


def run_state(raw: Any) -> str:
    """A task's state as the setup row says it: running, succeeded, failed."""
    value = getattr(raw, "value", raw)
    return _DONE_STATES.get(str(value or "").upper(), RUNNING)


def runner_identity(project_id: str) -> str:
    """The identity the first team's smoke and the console's verify runs are
    submitted as: swarm-api's own service account. A service submitter is
    resolved by the service rule (`forge.service_slot`): the team's own slot,
    never a person's GitHub connection (decision 3, lesson 43), and the
    workspace gate does not judge it (WD7)."""
    return f"swarm-api@{project_id}.iam.gserviceaccount.com"


class TeamRuns:
    """Submits and reads the first team's runs through the existing
    submission path (`SubmissionService`), as `runner_identity` in the team's
    tenant. A test sets `app.state.setup_runs` to a fake."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def _auth(self, tenant_id: str, group: str) -> AuthContext:
        from swarm_common.identity import Principal

        email = runner_identity(self._ctx.settings.project_id)
        return AuthContext(
            principal=Principal(email=email, subject=f"setup:{tenant_id}",
                                domain=email.rsplit("@", 1)[-1], groups=()),
            tenant_id=tenant_id, is_admin=False, tenant_principal=group)

    def submit_task(self, *, tenant_id: str, group: str, repository: str, prompt: str,
                    label: str) -> str:
        from ..schemas import TaskCreate

        spec = TaskCreate(runner_profile=RUN_PROFILE, input={"prompt": prompt},
                          repository_url=f"https://github.com/{repository}",
                          metadata={"unit": label})
        result = self._ctx.submissions.submit_tasks(self._auth(tenant_id, group), [spec])
        return str(result.tasks[0].id)

    def submit_workflow(self, *, tenant_id: str, group: str, repository: str,
                        label: str) -> str:
        from ..schemas import WorkflowCreate

        spec = WorkflowCreate.model_validate({
            "repository_url": f"https://github.com/{repository}",
            "metadata": {"unit": label},
            "steps": [
                {"step_id": "write", "runner_profile": RUN_PROFILE,
                 "input": {"prompt": "Add a file VERIFY.md saying this deployment ran a "
                                     "workflow from the console; commit it."}},
                {"step_id": "open", "runner_profile": RUN_PROFILE, "depends_on": ["write"],
                 "builds_on": "write",
                 "input": {"prompt": "Open a pull request with the previous step's commit, "
                                     "titled 'swarm install verify'."}},
            ],
        })
        submission = self._ctx.submissions.submit_workflow(self._auth(tenant_id, group), spec)
        return str(submission.workflow.workflow_id)

    def task_state(self, *, tenant_id: str, task_id: str) -> str:
        task = self._ctx.store.get_task(tenant_id, task_id, submitted_by=None)
        return run_state(task.state)


def setup_runs(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    found = getattr(request.app.state, "setup_runs", None)
    return found if found is not None else TeamRuns(ctx)


def installation_repositories(flow: ManifestFlow, github: GitHubSetupApi,
                              org: str) -> dict[str, Any] | None:
    """The App installation on `org` and the repositories it covers, or None
    while the App is not created or not installed there.

    Signed with the App's key (`ManifestFlow._signed`), as the installation
    check is; the installation token is minted for this one read, held in
    memory, and never stored, returned or logged."""
    if not org or not (flow._doc().get("id")):  # noqa: SLF001 - the flow's own record
        return None
    jwt = flow._signed()  # noqa: SLF001 - the one App JWT signer
    installs = github.installations(jwt)
    found = next((i for i in installs
                  if str((i.get("account") or {}).get("login") or "").lower() == org.lower()),
                 None)
    if found is None or not isinstance(found.get("id"), int):
        return None
    minted = github._call("POST", f"/app/installations/{found['id']}/access_tokens",  # noqa: SLF001
                          jwt=jwt, body={})
    token = minted.get("token") if isinstance(minted, dict) else None
    if not isinstance(token, str) or not token:
        raise UpstreamUnavailable("GitHub minted no installation token")
    names: list[str] = []
    for page in range(1, MAX_REPOSITORY_PAGES + 1):
        answer = github._call(  # noqa: SLF001
            "GET", f"/installation/repositories?per_page=100&page={page}", jwt=token)
        rows = (answer or {}).get("repositories") if isinstance(answer, dict) else None
        rows = [r for r in rows or [] if isinstance(r, dict) and r.get("full_name")]
        names += [str(r["full_name"]) for r in rows]
        if len(rows) < 100:
            break
    return {"installation_id": found["id"], "account": org,
            "selection": found.get("repository_selection"), "repositories": sorted(set(names))}


def ensure_grant(record: ir.InstallRecord, team_id: str, team: dict[str, Any], *,
                 flow: ManifestFlow, github: GitHubSetupApi, org: str, by: str,
                 now: datetime) -> dict[str, Any]:
    """Decision 3: the first team's team-level grant over the repositories the
    App installation covers, recorded on `teams/<id>.grant` and shown on the
    page. Written once the App is installed on at least one repository; until
    then `state: waiting_for_app` (not installed) or `waiting_for_repositories`
    (installed on none, as a new organisation is), and the next read of the
    page tries again. A grant already recorded is kept as it is."""
    grant = dict(team.get("grant") or {})
    if grant.get("state") == "granted" and grant.get("repositories"):
        return grant
    try:
        found = installation_repositories(flow, github, org)
    except ApiError as exc:
        log.info("first team grant: the installation could not be read code=%s", exc.code)
        found = None
    if found is None or not found["repositories"]:
        waiting = "waiting_for_app" if found is None else "waiting_for_repositories"
        if grant.get("state") == waiting:
            return grant  # unchanged: no write on every poll of the page
        grant = {"state": waiting, "checked_at": now}
        if found is not None:
            grant.update({"installation_id": found["installation_id"],
                          "account": found["account"], "selection": found["selection"]})
    else:
        grant = {"state": "granted", "mode": "write", "at": now, "by": by, **found}
        log.info("first team granted the App's repositories team=%s repositories=%d",
                 team_id, len(found["repositories"]))
    record.write(record.team_path(team_id), {"grant": grant})
    return grant


def admin_members_url(directory: Any, group: str) -> str:
    """The Google Admin page that adds members to `group` (decision 4)."""
    lookup = getattr(directory, "group_resource_name", None)
    if callable(lookup):
        try:
            name = str(lookup(group) or "")
        except Exception:  # noqa: BLE001 - the group list still gets them there
            name = ""
        group_id = name.rsplit("/", 1)[-1]
        if group_id:
            return ADMIN_GROUP_MEMBERS.format(group_id=group_id)
    return ADMIN_GROUPS


def _membership(directory: MembershipResolver | None, email: str, group: str) -> bool | None:
    """Whether `email` is in `group` now, per the Groups API; None when it did
    not answer (never read as "not a member")."""
    if directory is None or not group:
        return None
    try:
        return bool(directory.groups_for(email, (group,), fresh=True))
    except GroupLookupError:
        return None


def _pool_labels(request: Request, ctx: AppContext, tenant: str) -> list[str]:
    listing = account_pool(request, ctx).list_accounts(tenant)
    return sorted({str(a.get("label") or "") for a in listing.get("accounts") or []
                   if isinstance(a, dict) and a.get("owner_tenant") == tenant} - {""})


def advance_smoke(record: ir.InstallRecord, team_id: str, team: dict[str, Any], *,
                  runs: Any, pool_accounts: list[str], now: datetime) -> dict[str, Any] | None:
    """The first team's smoke (plan §5, stage 12): once its first pool account
    has landed and its grant names a repository, ONE small task on that
    repository, as the team; then its state, read live until it ends. The
    result is `teams/<id>.smoke`, which `finish` waits for. A failed smoke is
    started again only by `POST /v1/admin/setup/smoke`."""
    smoke = dict(team.get("smoke") or {})
    tenant = str(team.get("workspace") or team_id)
    if smoke.get("task_id"):
        if smoke.get("state") in (SUCCEEDED, FAILED):
            return smoke
        try:
            state = runs.task_state(tenant_id=tenant, task_id=str(smoke["task_id"]))
        except ApiError as exc:
            log.info("first team smoke: its task could not be read code=%s", exc.code)
            return smoke
        if state != smoke.get("state"):
            smoke["state"] = state
            if state != RUNNING:
                smoke["finished_at"] = now
            record.write(record.team_path(team_id), {"smoke": smoke})
        return smoke
    repositories = list((team.get("grant") or {}).get("repositories") or [])
    if not pool_accounts or not repositories:
        return smoke or None
    repository = repositories[0]
    try:
        task_id = runs.submit_task(tenant_id=tenant, group=str(team.get("group") or ""),
                                   repository=repository, prompt=SMOKE_PROMPT,
                                   label=SMOKE_LABEL)
    except ApiError as exc:
        smoke = {"state": FAILED, "repository": repository, "started_at": now,
                 "finished_at": now, "reason": exc.code}
    else:
        smoke = {"state": RUNNING, "task_id": task_id, "repository": repository,
                 "started_at": now}
        log.info("first team smoke started team=%s task=%s", team_id, task_id)
    record.write(record.team_path(team_id), {"smoke": smoke})
    return smoke


def team_row(request: Request, ctx: AppContext, record: ir.InstallRecord, team_id: str,
             team: dict[str, Any], *, directory: MembershipResolver | None, first: bool,
             runs: Any, flow: ManifestFlow, github: GitHubSetupApi, by: str) -> dict[str, Any]:
    """A team's row, read live: its workspace's state now, each invitee's (and
    the first admin's) membership per the Groups API, its pool accounts, and
    for the first team its grant and smoke, advanced on this read."""
    tenant = str(team.get("workspace") or team_id)
    group = str(team.get("group") or "")
    workspace = ctx.submissions.workspaces.get(tenant) or {}
    people = sorted({str(e) for e in team.get("invites") or []}
                    | ({str(team["first_admin"])} if team.get("first_admin") else set()))
    members = {email: _membership(directory, email, group) for email in people}
    try:
        pool_accounts = _pool_labels(request, ctx, tenant)
    except ApiError as exc:
        log.info("setup page: the pool could not be read code=%s", exc.code)
        pool_accounts = []
    row = dict(team)
    row.update({"id": team_id, "workspace_state": workspace.get("state"),
                "members": [{"email": e, "member": m} for e, m in members.items()],
                "pool_accounts": pool_accounts, "first": first})
    if first:
        now = ctx.now()
        team = {**team, "grant": ensure_grant(
            record, team_id, team, flow=flow, github=github,
            org=ctx.settings.setup_github_org, by=by, now=now)}
        row["grant"] = team["grant"]
        row["smoke"] = advance_smoke(record, team_id, team, runs=runs,
                                     pool_accounts=pool_accounts, now=now)
    return row


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
    request: Request,
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    directory: MembershipResolver | None = Depends(workspace_directory),
    flow: ManifestFlow = Depends(manifest_flow),
    github: GitHubSetupApi = Depends(setup_github_api),
    runs: Any = Depends(setup_runs),
) -> dict:
    """The page's whole state. Each team's row is read live (`team_row`), and
    the first team's grant and smoke are advanced on the read the page polls."""
    engine = record.read_engine()
    teams = record.teams()
    return {
        "env": record.env,
        "engine": engine,
        "cli_running": _cli_running(engine, ctx.now()),
        "github_app": _github_view(record.read(record.console_path(ir.GITHUB_APP))),
        "teams": [team_row(request, ctx, record, team_id, doc, directory=directory,
                           first=index == 0, runs=runs, flow=flow, github=github,
                           by=caller.email)
                  for index, (team_id, doc) in enumerate(teams)],
        "verify_runs": record.read(record.console_path(ir.VERIFY_RUNS)),
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
    directory: MembershipResolver | None = Depends(workspace_directory),
    flow: ManifestFlow = Depends(manifest_flow),
    github: GitHubSetupApi = Depends(setup_github_api),
) -> JSONResponse:
    """A team: the group workspace through the existing workspace path
    (`People.create`, as `POST /v1/admin/workspaces`; `swarm-workspace-apply`
    then does the namespace, quota, verify-keys and identity, the one writer
    of design §5.7), its first admin, and `teams/<id>`.

    The FIRST team also gets its team-level grant over the App installation's
    repositories (decision 3, `ensure_grant`). Each invite comes back with the
    Google Admin page that adds the person to the group (decision 4); the
    page's row then polls their membership through the Groups API."""
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
    group = str(workspace.get("principal") or body.group).lower()
    invites = sorted({i.strip().lower() for i in body.invites if i.strip()}
                     | set(existing.get("invites") or []))
    first = _first_team(record)
    team = record.write(record.team_path(team_id), {
        "group": group,
        "workspace": team_id,
        "first_admin": first_admin,
        "invites": invites,
        "progress": progress,
    })
    grant = None
    if first is None or first[0] == team_id:
        grant = ensure_grant(record, team_id, team, flow=flow, github=github,
                             org=ctx.settings.setup_github_org, by=caller.email, now=ctx.now())
    members_url = admin_members_url(directory, group) if invites else None
    return JSONResponse(status_code=status, content=jsonable_encoder({
        "team": team_id, "workspace": content, "first_admin": granted, "grant": grant,
        "invites": [{"email": email, "add_members_url": members_url,
                     "member": _membership(directory, email, group)} for email in invites]}))


def _first_team(record: ir.InstallRecord) -> tuple[str, dict[str, Any]] | None:
    teams = record.teams()
    return teams[0] if teams else None


class InviteCreate(StrictModel):
    email: str = Field(min_length=3, max_length=320)


@router.post("/teams/{team_id}/invites", dependencies=[Depends(admin_auth)])
def invite(
    team_id: str,
    body: InviteCreate,
    caller: SetupCaller = Depends(admin_setup_caller),
    record: ir.InstallRecord = Depends(install_record),
    directory: MembershipResolver | None = Depends(workspace_directory),
) -> dict:
    """Decision 4: record the invite and answer the Google Admin page that adds
    the person to the team's group. swarm-api holds Groups Reader only, so it
    adds no one; the page's row polls the membership (`GET /v1/admin/setup`)
    until the Groups API says they are in."""
    team = record.read(record.team_path(team_id))
    if team is None:
        raise NotFound(f"no team {team_id!r}")
    email = roles.subject_for_write(body.email)
    group = str(team.get("group") or "")
    invites = sorted(set(team.get("invites") or []) | {email})
    if invites != list(team.get("invites") or []):
        record.write(record.team_path(team_id), {"invites": invites})
    log.info("setup invite recorded team=%s by=%s", team_id, caller.email)
    return {"team": team_id, "email": email, "group": group,
            "add_members_url": admin_members_url(directory, group),
            "member": _membership(directory, email, group)}


def _granted_repository(record: ir.InstallRecord) -> tuple[str, dict[str, Any], str]:
    """(team id, team, repository): the first team and the first repository of
    its grant, which every run Admin › Setup starts uses (decision 3)."""
    first = _first_team(record)
    if first is None:
        raise Conflict("create the first team first")
    team_id, team = first
    repositories = list((team.get("grant") or {}).get("repositories") or [])
    if not repositories:
        raise Conflict("the first team has no granted repository yet: install the GitHub "
                       "App on the organisation, on at least one repository",
                       detail={"grant": (team.get("grant") or {}).get("state")})
    return team_id, team, repositories[0]


@router.post("/smoke", dependencies=[Depends(admin_auth)])
def restart_smoke(
    request: Request,
    caller: SetupCaller = Depends(admin_setup_caller),  # noqa: ARG001 - the gate
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    runs: Any = Depends(setup_runs),
) -> dict:
    """Start the first team's smoke again after it failed; a running or passed
    smoke is answered as it is."""
    team_id, team, _ = _granted_repository(record)
    smoke = dict(team.get("smoke") or {})
    if smoke.get("state") == FAILED:
        team = {**team, "smoke": None}
    tenant = str(team.get("workspace") or team_id)
    smoke = advance_smoke(record, team_id, team, runs=runs,
                          pool_accounts=_pool_labels(request, ctx, tenant), now=ctx.now())
    if smoke is None:
        raise Conflict("the first team's smoke waits for its first pool account")
    return {"team": team_id, "smoke": smoke}


@router.post("/verify-runs", dependencies=[Depends(admin_auth)])
def start_verify_runs(
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    runs: Any = Depends(setup_runs),
) -> dict:
    """The console's two verification runs, one task and one workflow, as the
    first team on a repository of its grant (decision 3: the team's own slot,
    never a person's token). Their ids go to `console/verify_runs` as
    `verify_runs: {console_task, console_workflow}`, which the engine copies
    into `state.outputs["verify_runs"]`, so nobody pastes an id. Called again,
    it answers the runs already started and starts nothing."""
    path = record.console_path(ir.VERIFY_RUNS)
    done = record.read(path) or {}
    started = dict(done.get("verify_runs") or {})
    if started.get("console_task") and started.get("console_workflow"):
        return {"verify_runs": started, "repository": done.get("repository"), "started": False}
    team_id, team, repository = _granted_repository(record)
    tenant = str(team.get("workspace") or team_id)
    group = str(team.get("group") or "")
    if not started.get("console_task"):
        started["console_task"] = runs.submit_task(
            tenant_id=tenant, group=group, repository=repository, prompt=VERIFY_TASK_PROMPT,
            label=VERIFY_LABEL)
        record.write(path, {"verify_runs": dict(started), "repository": repository,
                            "started_at": ctx.now(), "by": caller.email})
    started["console_workflow"] = runs.submit_workflow(
        tenant_id=tenant, group=group, repository=repository, label=VERIFY_LABEL)
    record.write(path, {"verify_runs": dict(started), "repository": repository,
                        "started_at": done.get("started_at") or ctx.now(), "by": caller.email})
    log.info("console verify runs started task=%s workflow=%s by=%s",
             started["console_task"], started["console_workflow"], caller.email)
    return {"verify_runs": started, "repository": repository, "started": True}


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
    returned or logged. Their link is consumed only AFTER the write, so a
    missing value or a failed write leaves it usable. A team's git container
    is created on first use (an install has no Terraform tenant to make it)."""
    peeked = links.peek(body.token, email=auth.email)
    kind = peeked.get("kind")
    if kind == sl.POOL_ACCOUNT:
        link = links.redeem(body.token, email=auth.email)
        return {"kind": kind, "label": link.get("label"), "tenant": link.get("tenant_id"),
                "sign_in": {"authorize": "/v1/accounts/authorize",
                            "exchange": "/v1/accounts/exchange",
                            "header": TENANT_HEADER}}
    if body.value is None:
        raise ValidationFailed(f"a {kind} link is opened with its value; the link is still "
                               "unused, open it again with the value")
    labels: dict[str, str] | None = None
    if kind == sl.TEAM_GIT:
        tenant_id = str(peeked.get("tenant_id"))
        secret_id = sl.team_git_secret(tenant_id)
        labels = {"managed-by": "swarm-api", "tenant": tenant_id}
    else:
        secret_id = sl.ADDON_SECRETS[str(peeked.get("label"))]
    secrets_.add_version(secret_id, body.value, create_labels=labels)
    link = links.redeem(body.token, email=auth.email)
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
    flow: ManifestFlow = Depends(manifest_flow),
    github: GitHubSetupApi = Depends(setup_github_api),
    runs: Any = Depends(setup_runs),
) -> dict:
    """Write `console/finished` once every required console step is done: the
    App installed and its self-check passed, a first team, a pool account
    signed in to it, and the first team's smoke passed (`advance_smoke`,
    started here too if the page has not started it yet). A labelled account seen in the first team's pool gets its
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
    smoke_passed = False
    if first is not None and signed:
        team_id, team = first
        grant = (team.get("grant") or {})
        if grant.get("state") != "granted" or not grant.get("repositories"):
            grant = ensure_grant(record, team_id, team, flow=flow, github=github,
                                 org=ctx.settings.setup_github_org, by=caller.email,
                                 now=ctx.now())
            team = {**team, "grant": grant}
        smoke = advance_smoke(record, team_id, team, runs=runs, pool_accounts=sorted(signed),
                              now=ctx.now()) or {}
        smoke_passed = smoke.get("state") == SUCCEEDED
    if not smoke_passed:
        missing.append("first_team_smoke")
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
