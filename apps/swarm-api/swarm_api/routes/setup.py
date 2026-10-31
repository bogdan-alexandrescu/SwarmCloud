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
never on a person's GitHub connection.

ONE PERSON OR A TEAM (lane DAYONE-SETUP-API-3, decision 7). The page serves
the install's `sizing` (`state.outputs["sizing"]`, which the engine copies
into its document) read-only, and its `mode`. For one person
(`sizing.mode == "individual"`) the first workspace is the installer's
personal workspace, recorded as `console/personal`: it gets the grant, its
pool accounts and the smoke the first team gets for a team; the first-team
and invites steps say they are not needed, and `finish` waits for the
personal workspace's smoke. An engine document with no `sizing` reads as a
team, as every install before this lane did.

No answer, log record or Firestore field carries a
secret: the App's three secrets go from GitHub's answer straight into Secret
Manager, and a link's value straight into its one container.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
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
from ..access import record_workspace_installation, write_workspace_grants
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
#: The dispatch strategies of the runs this module submits: the smoke only
#: has to finish, the console's verify task and workflow must each end in a
#: pull request (`verify.end-to-end` reports a run without one as a problem).
SMOKE_STRATEGY = "collect"
VERIFY_TASK_STRATEGY = "direct-pr"
VERIFY_WORKFLOW_STRATEGY = "integrate"
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
                    label: str, strategy: str = SMOKE_STRATEGY) -> str:
        """One task. The smoke keeps `collect` (it opens nothing); the
        console's verify task is `direct-pr`, because verify needs its pull
        request and a `collect` task's git tools refuse every push (audit 4d,
        F2)."""
        from ..schemas import TaskCreate

        spec = TaskCreate(runner_profile=RUN_PROFILE, input={"prompt": prompt},
                          repository_url=f"https://github.com/{repository}",
                          metadata={"unit": label}, strategy=strategy)
        result = self._ctx.submissions.submit_tasks(self._auth(tenant_id, group), [spec])
        return str(result.tasks[0].id)

    def submit_workflow(self, *, tenant_id: str, group: str, repository: str,
                        label: str) -> str:
        from ..schemas import WorkflowCreate

        spec = WorkflowCreate.model_validate({
            "repository_url": f"https://github.com/{repository}",
            "metadata": {"unit": label},
            # `open` builds on `write`'s branch, which only a strategy whose
            # every step pushes allows; `integrate` has the final step open
            # the one pull request verify reads (audit 4d, F1).
            "strategy": VERIFY_WORKFLOW_STRATEGY,
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

    def workflow_state(self, *, tenant_id: str, workflow_id: str) -> str:
        workflow = self._ctx.store.get_workflow(tenant_id, workflow_id, submitted_by=None)
        return run_state(workflow.state)


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
    kind = (found.get("account") or {}).get("type")
    return {"installation_id": found["id"], "account": org,
            "owner_type": kind if isinstance(kind, str) else None,
            "selection": found.get("repository_selection"), "repositories": sorted(set(names))}


def _grant_held(grant: dict[str, Any]) -> bool:
    """Whether a granted grant is fully on the submission path: enforced,
    and its App installation recorded for the workspace when it names one."""
    return bool(grant.get("enforced_at")) and (
        not grant.get("installation_id") or bool(grant.get("installation_recorded_at")))


def enforce_grant(db: Any, tenant: str, grant: dict[str, Any], *, now: datetime
                  ) -> dict[str, Any]:
    """The recorded grant made the one the submission path reads (finding N11):
    the workspace's grant documents (`access.write_workspace_grants`), which
    `SubmissionService._grant_mode` honours for everyone in the workspace, so
    a teammate's first task on these repositories is not refused under
    `repository_grants_enforced`. Marked `enforced_at` once written; a grant
    recorded before this existed is written on its next read.

    The App installation it came from is recorded as the workspace's own
    `forge_orgs` document (`access.record_workspace_installation`; audit 3,
    A4), so a service submission for this workspace -- the first team's
    smoke, the console's verification runs -- is minted an installation
    token (`SubmissionService._app_installed`). Marked
    `installation_recorded_at`; keyed by `tenant` alone (invariant 9)."""
    if grant.get("state") != "granted" or not grant.get("repositories") or _grant_held(grant):
        return grant
    if not grant.get("enforced_at"):
        write_workspace_grants(db, tenant, list(grant["repositories"]),
                               mode=str(grant.get("mode") or "write"), by=str(grant.get("by") or ""),
                               at=now, source="admin-setup")
        grant = {**grant, "enforced_at": now}
    installation = grant.get("installation_id")
    if installation and not grant.get("installation_recorded_at"):
        record_workspace_installation(
            db, tenant, owner=str(grant.get("account") or ""), installation_id=installation,
            owner_type=grant.get("owner_type"), selection=grant.get("selection"),
            by=str(grant.get("by") or ""), at=now)
        grant = {**grant, "installation_recorded_at": now}
    return grant


def ensure_grant(record: ir.InstallRecord, team_id: str, team: dict[str, Any], *,
                 flow: ManifestFlow, github: GitHubSetupApi, org: str, by: str,
                 now: datetime, db: Any, path: str | None = None) -> dict[str, Any]:
    """Decision 3: the first team's team-level grant over the repositories the
    App installation covers, recorded on `teams/<id>.grant` and shown on the
    page. Written once the App is installed on at least one repository; until
    then `state: waiting_for_app` (not installed) or `waiting_for_repositories`
    (installed on none, as a new organisation is), and the next read of the
    page tries again. A grant already recorded is kept as it is. `path` is the
    document it is recorded on: the team's, or for one person
    `console/personal`. A grant is enforced as it is recorded (`enforce_grant`):
    its workspace holds it on the submission path, not only on the page."""
    path = path or record.team_path(team_id)
    tenant = str(team.get("workspace") or team_id)
    grant = dict(team.get("grant") or {})
    if grant.get("state") == "granted" and grant.get("repositories"):
        if not _grant_held(grant):
            grant = enforce_grant(db, tenant, grant, now=now)
            record.write(path, {"grant": grant})
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
        grant = enforce_grant(db, tenant, {"state": "granted", "mode": "write", "at": now,
                                           "by": by, **found}, now=now)
        log.info("first team granted the App's repositories team=%s repositories=%d",
                 team_id, len(found["repositories"]))
    record.write(path, {"grant": grant})
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


def mark_signed_in(record: ir.InstallRecord, pool_accounts: list[str], *,
                   now: datetime) -> None:
    """Finding N7: each labelled account the first workspace's pool holds
    (`_pool_labels`) is signed in, so `console/accounts.<label>.signed_in_at`
    is stamped here, on the read the page polls, as `finish` stamps it; the
    page's "Hand over to swarm install" waits on it, and the link page that
    signed it in writes nothing of its own. A stamp already there is kept."""
    if not pool_accounts:
        return
    accounts = record.read(record.console_path(ir.ACCOUNTS)) or {}
    newly = {label: {**(accounts.get(label) or {}), "signed_in_at": now}
             for label in pool_accounts if not (accounts.get(label) or {}).get("signed_in_at")}
    if newly:
        record.write(record.console_path(ir.ACCOUNTS), newly)
        log.info("setup page: pool accounts signed in labels=%s", ",".join(sorted(newly)))


def advance_smoke(record: ir.InstallRecord, team_id: str, team: dict[str, Any], *,
                  runs: Any, pool_accounts: list[str], now: datetime,
                  path: str | None = None, principal: str | None = None
                  ) -> dict[str, Any] | None:
    """The first team's smoke (plan §5, stage 12): once its first pool account
    has landed and its grant names a repository, ONE small task on that
    repository, as the team; then its state, read live until it ends. The
    result is `teams/<id>.smoke`, which `finish` waits for. A failed smoke is
    started again only by `POST /v1/admin/setup/smoke`. For one person it is
    the personal workspace's, on `console/personal` (`path`), submitted with
    the installer as the tenant's principal (`principal`)."""
    path = path or record.team_path(team_id)
    group = principal if principal is not None else str(team.get("group") or "")
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
            record.write(path, {"smoke": smoke})
        return smoke
    repositories = list((team.get("grant") or {}).get("repositories") or [])
    if not pool_accounts or not repositories:
        return smoke or None
    if not app_self_checked(record):
        return {"state": SELF_CHECK_WAITING, "repository": repositories[0],
                "message": PASS_3_WAIT}
    repository = repositories[0]
    try:
        task_id = runs.submit_task(tenant_id=tenant, group=group,
                                   repository=repository, prompt=SMOKE_PROMPT,
                                   label=SMOKE_LABEL)
    except ApiError as exc:
        smoke = {"state": FAILED, "repository": repository, "started_at": now,
                 "finished_at": now, "reason": exc.code}
    else:
        smoke = {"state": RUNNING, "task_id": task_id, "repository": repository,
                 "started_at": now}
        log.info("first workspace smoke started workspace=%s task=%s", tenant, task_id)
    record.write(path, {"smoke": smoke})
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
                # The record's `w-` id, which POST /v1/admin/workspaces/{id}/retry
                # takes: a failed workspace is retried there, not by asking for
                # the team again (audit 5b, M6).
                "workspace_id": workspace.get("workspace_id"),
                # The group's members page on every row, so the invites made
                # with the team keep their link (audit 5b, M5).
                "add_members_url": admin_members_url(directory, group) if group else None,
                "members": [{"email": e, "member": m} for e, m in members.items()],
                "pool_accounts": pool_accounts, "first": first})
    if first:
        now = ctx.now()
        mark_signed_in(record, pool_accounts, now=now)
        team = {**team, "grant": ensure_grant(
            record, team_id, team, flow=flow, github=github,
            org=ctx.settings.setup_github_org, by=by, now=now, db=ctx.db)}
        row["grant"] = team["grant"]
        row["smoke"] = advance_smoke(record, team_id, team, runs=runs,
                                     pool_accounts=pool_accounts, now=now)
    return row


# --------------------------------------------------------------------------
# one person or a team (lane DAYONE-SETUP-API-3, decision 7)
# --------------------------------------------------------------------------

#: What the first-team and invites steps say for a one-person install.
NOT_NEEDED = "not needed for one person"


def install_mode(engine: dict[str, Any] | None) -> str:
    """`individual` when the engine's `sizing.mode` says so; anything else,
    an absent `sizing` included (an install from before sizing), is a team."""
    sizing = (engine or {}).get("sizing")
    mode = sizing.get("mode") if isinstance(sizing, dict) else None
    return ir.INDIVIDUAL if mode == ir.INDIVIDUAL else ir.TEAM


def sizing_view(engine: dict[str, Any] | None) -> dict[str, Any] | None:
    """The install's sizing as the page serves it, read-only: its named
    fields and nothing else; None until the engine has written it."""
    sizing = (engine or {}).get("sizing")
    if not isinstance(sizing, dict):
        return None
    return {name: sizing.get(name) for name in ir.SIZING_FIELDS}


def steps_view(mode: str, teams: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """The first-team and invites steps: needed for a team, and for one
    person `not_needed` with the reason the page shows."""
    if mode == ir.INDIVIDUAL:
        skipped = {"needed": False, "state": "not_needed", "reason": NOT_NEEDED}
        return {"first_team": dict(skipped), "invites": dict(skipped)}
    return {"first_team": {"needed": True, "state": "done" if teams else "to_do"},
            "invites": {"needed": True,
                        "state": "done" if any(t.get("invites") for _, t in teams)
                        else "to_do"}}


def installer_of(ctx: AppContext, engine: dict[str, Any] | None) -> str:
    """The installer's email: the engine's `installer` (state.outputs), else
    the deployment's platform owner; "" when neither is set."""
    found = str((engine or {}).get("installer") or ctx.settings.platform_owner or "")
    return found.strip().lower()


def _refuse_for_one_person(record: ir.InstallRecord, step: str) -> None:
    if install_mode(record.read_engine()) == ir.INDIVIDUAL:
        raise Conflict(f"a team is {NOT_NEEDED}: this install is for one person, whose "
                       "first workspace is their personal workspace",
                       detail={"mode": ir.INDIVIDUAL, "step": step})


def ensure_personal(record: ir.InstallRecord, ctx: AppContext, people: People, *,
                    installer: str, by: str, is_owner: bool) -> dict[str, Any] | None:
    """A one-person install's first workspace: the installer's personal
    workspace, filed through the existing workspace path (`People.create`,
    as `POST /v1/admin/workspaces`; 200 and nothing written when it stands),
    and recorded once as `console/personal`. None while no installer is
    known."""
    path = record.personal_path()
    doc = record.read(path) or {}
    if doc.get("workspace") and (not installer or doc.get("owner") == installer):
        return doc
    if not installer:
        return None
    _, workspace = people.create(
        ctx.submissions.workspaces, kind=ws.PERSONAL, principal=installer, display_name=None,
        limits=None, by=by, is_owner=is_owner)
    tenant = str(workspace.get("tenant_id") or ws.personal_tenant_id(installer))
    progress = dict(doc.get("progress") or {})
    progress.setdefault("created_at", ctx.now())
    progress["workspace_state"] = workspace.get("state")
    log.info("one-person install: the installer's personal workspace is the first "
             "workspace=%s", tenant)
    return record.write(path, {"owner": installer, "workspace": tenant, "progress": progress})


@dataclass(frozen=True)
class FirstWorkspace:
    """Where the grant, the pool accounts, the smoke and the verify runs go:
    the first team's workspace, or for one person the personal workspace."""

    key: str  # the team id, or the personal workspace's tenant id
    doc: dict[str, Any]
    path: str
    principal: str  # the team's group, or the installer
    personal: bool

    @property
    def tenant(self) -> str:
        return str(self.doc.get("workspace") or self.key)


def first_workspace(record: ir.InstallRecord, ctx: AppContext, people: People, *,
                    caller: SetupCaller, flow: ManifestFlow, github: GitHubSetupApi,
                    engine: dict[str, Any] | None = None,
                    mode: str | None = None) -> FirstWorkspace | None:
    """The first workspace for this install's mode, or None while there is
    none. For one person it is filed and granted here (`ensure_personal`,
    `ensure_grant`), so no step waits on a page read."""
    if mode is None:
        engine = record.read_engine() if engine is None else engine
        mode = install_mode(engine)
    if mode != ir.INDIVIDUAL:
        first = _first_team(record)
        if first is None:
            return None
        team_id, team = first
        return FirstWorkspace(key=team_id, doc=team, path=record.team_path(team_id),
                              principal=str(team.get("group") or ""), personal=False)
    installer = installer_of(ctx, engine if engine is not None else record.read_engine())
    auth = caller.auth
    doc = ensure_personal(record, ctx, people, installer=installer, by=caller.email,
                          is_owner=bool(auth.is_owner) if auth is not None else False)
    if doc is None:
        return None
    path = record.personal_path()
    tenant = str(doc["workspace"])
    doc = {**doc, "grant": ensure_grant(record, tenant, doc, flow=flow, github=github,
                                        org=ctx.settings.setup_github_org, by=caller.email,
                                        now=ctx.now(), db=ctx.db, path=path)}
    return FirstWorkspace(key=tenant, doc=doc, path=path, principal=installer, personal=True)


def personal_row(request: Request, ctx: AppContext, record: ir.InstallRecord,
                 first: FirstWorkspace, *, runs: Any) -> dict[str, Any]:
    """The personal workspace's row, read live: its workspace's state now,
    its pool accounts, its grant and its smoke, advanced on this read."""
    tenant = first.tenant
    workspace = ctx.submissions.workspaces.get(tenant) or {}
    try:
        pool_accounts = _pool_labels(request, ctx, tenant)
    except ApiError as exc:
        log.info("setup page: the pool could not be read code=%s", exc.code)
        pool_accounts = []
    row = dict(first.doc)
    row.update({"workspace_state": workspace.get("state"), "pool_accounts": pool_accounts})
    mark_signed_in(record, pool_accounts, now=ctx.now())
    row["smoke"] = advance_smoke(record, first.key, first.doc, runs=runs,
                                 pool_accounts=pool_accounts, now=ctx.now(), path=first.path,
                                 principal=first.principal)
    return row


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


def _as_datetime(value: Any) -> datetime | None:
    """A datetime, an ISO string, or epoch seconds (int or float): the engine
    writes `lease.heartbeat`, `hand_off_at`, `ready_at` and `stages[].at` as
    epoch seconds (`swarm_install.lease`, `state`; audit 3, A7)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
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


#: The self-check's states (audit finding 28). Before swarm install's infra
#: pass 3 renders GITHUB_APP_ID into swarm-api, `check_app_key` answers
#: `app_id_not_configured`: that is a wait for the pass, never a reason to
#: create the App again (pass 4 then waits on a passing check).
SELF_CHECK_PASSED = "passed"
SELF_CHECK_FAILED = "failed"
SELF_CHECK_WAITING = "waiting_for_infra_pass_3"
#: The `check_app_key` reasons that mean pass 3 has not landed yet.
BEFORE_PASS_3 = frozenset({"app_id_not_configured", "app_slug_not_configured"})


#: What a run that waits for infra pass 3 says (audit 4d, F4).
PASS_3_WAIT = ("waiting for swarm install's infra pass 3: until it gives swarm-api the "
               "App's id, no installation token can be minted for the run to clone with")


def app_self_checked(record: ir.InstallRecord) -> bool:
    """Whether `console/github_app.self_check` passed: the pass-3 signal the
    page already shows. The smoke and the console's verify runs clone with
    the App installation token, which `forgeapp.signed_app_jwt` mints only
    once pass 3 has rendered GITHUB_APP_ID into swarm-api; started before,
    the broker refuses (`app_installation_unavailable`) and the run fails
    (audit 4d, F4). So neither is submitted until this answers true."""
    app = record.read(record.console_path(ir.GITHUB_APP)) or {}
    return bool((app.get("self_check") or {}).get("ok"))


def self_check_view(result: dict[str, Any]) -> dict[str, Any]:
    """A recorded or fresh self-check with its `state` and the sentence the
    page shows; one recorded before the states existed is read the same way."""
    reason = result.get("reason")
    if result.get("ok"):
        state, message = SELF_CHECK_PASSED, "the App's private key signs and GitHub knows the App"
    elif reason in BEFORE_PASS_3:
        state = SELF_CHECK_WAITING
        message = ("waiting for swarm install's infra pass 3, which gives swarm-api the "
                   "App's id; the check can run once that pass has")
    else:
        state, message = SELF_CHECK_FAILED, f"the App's key self-check failed: {reason}"
    return {**result, "state": state, "message": message}


def _github_view(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    view = {k: v for k, v in doc.items() if k != "state_hash"}
    if isinstance(view.get("self_check"), dict):
        view["self_check"] = self_check_view(view["self_check"])
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
    people: People = Depends(people_service),
) -> dict:
    """The page's whole state. Each team's row is read live (`team_row`), and
    the first team's grant and smoke are advanced on the read the page polls.
    It serves the install's `sizing` and `mode` read-only; for one person the
    personal workspace's row (`personal`) takes the first team's place."""
    engine = record.read_engine()
    mode = install_mode(engine)
    teams = record.teams()
    personal = None
    if mode == ir.INDIVIDUAL:
        try:
            first = first_workspace(record, ctx, people, caller=caller, flow=flow,
                                    github=github, engine=engine, mode=mode)
        except ApiError as exc:
            log.info("setup page: the personal workspace could not be filed code=%s", exc.code)
            personal = {"owner": installer_of(ctx, engine) or None, "workspace": None,
                        "reason": exc.code}
        else:
            if first is not None:
                personal = personal_row(request, ctx, record, first, runs=runs)
    finished = record.read(record.console_path(ir.FINISHED))
    if finished is not None:
        # Once handed over, each read starts a verify run `finish` could not
        # start and restarts a failed one, up to MAX_VERIFY_RETRIES (B1).
        try:
            handed = first_workspace(record, ctx, people, caller=caller, flow=flow,
                                     github=github, engine=engine, mode=mode)
        except ApiError as exc:
            log.info("setup page: the first workspace could not be read code=%s", exc.code)
        else:
            keep_verify_runs(record, ctx, handed, runs=runs, by=caller.email)
    return {
        "env": record.env,
        "engine": engine,
        "mode": mode,
        "sizing": sizing_view(engine),
        "steps": steps_view(mode, teams),
        "personal": personal,
        "cli_running": _cli_running(engine, ctx.now()),
        "github_app": _github_view(record.read(record.console_path(ir.GITHUB_APP))),
        "teams": [team_row(request, ctx, record, team_id, doc, directory=directory,
                           first=index == 0, runs=runs, flow=flow, github=github,
                           by=caller.email)
                  for index, (team_id, doc) in enumerate(teams)],
        "verify_runs": record.read(record.console_path(ir.VERIFY_RUNS)),
        "accounts": _accounts_view(record.read(record.console_path(ir.ACCOUNTS))),
        "finished": finished,
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
    `github_app.self_check`. It answers `{ok, reason, at, state, message}`
    only; before infra pass 3 the state is `waiting_for_infra_pass_3`."""
    checked = request.app.state.forge_app.check_app_key()
    result = self_check_view({"ok": bool(checked.get("ok")), "reason": checked.get("reason"),
                              "at": ctx.now()})
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


def check_team_group(directory: MembershipResolver | None, installer: str,
                     group: str) -> None:
    """Audit finding 27: before a team is created, swarm-api must be able to
    read its group and the installer must be in it; the verify runs look for
    a team the installer is in. One per-group membership question, past the
    cache (`check_group_readable`). Either failure is a 409 whose detail names
    the reason and the Google Admin groups page; nothing is written."""
    detail: dict[str, Any] = {"group": group, "installer": installer,
                              "admin_groups_url": ADMIN_GROUPS}
    try:
        if directory is None:
            raise GroupLookupError("this deployment has no group directory to ask")
        member = check_group_readable(directory, installer, group)
    except GroupLookupError:
        raise Conflict(f"swarm-api cannot read the group {group}: create it, or check its "
                       f"address, in Google Admin ({ADMIN_GROUPS}), then try again",
                       detail={**detail, "reason": "group_unreadable"}) from None
    if not member:
        raise Conflict(f"{installer} is not a member of {group}: add them to it in Google "
                       f"Admin ({ADMIN_GROUPS}), then try again",
                       detail={**detail, "reason": "installer_not_member",
                               "add_members_url": admin_members_url(directory, group)})


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
    page's row then polls their membership through the Groups API.

    A one-person install has no team (decision 7): 409, nothing written. A
    group swarm-api cannot read, or one the installer is not in, is refused
    the same way, with the Google Admin groups page (`check_team_group`)."""
    _refuse_for_one_person(record, "first_team")
    installer = installer_of(ctx, record.read_engine()) or caller.email.strip().lower()
    check_team_group(directory, installer, body.group.strip().lower())
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
                             org=ctx.settings.setup_github_org, by=caller.email, now=ctx.now(),
                             db=ctx.db)
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
    until the Groups API says they are in. A one-person install has none: 409."""
    _refuse_for_one_person(record, "invites")
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


def _granted_repository(first: FirstWorkspace | None) -> tuple[FirstWorkspace, str]:
    """(first workspace, repository): the first team (for one person, the
    personal workspace) and the first repository of its grant, which every
    run Admin › Setup starts uses (decision 3)."""
    if first is None:
        raise Conflict("create the first team first")
    grant = first.doc.get("grant") or {}
    repositories = list(grant.get("repositories") or [])
    if not repositories:
        whose = "the personal workspace" if first.personal else "the first team"
        raise Conflict(f"{whose} has no granted repository yet: install the GitHub "
                       "App on the organisation, on at least one repository",
                       detail={"grant": grant.get("state")})
    return first, repositories[0]


@router.post("/smoke", dependencies=[Depends(admin_auth)])
def restart_smoke(
    request: Request,
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    runs: Any = Depends(setup_runs),
    people: People = Depends(people_service),
    flow: ManifestFlow = Depends(manifest_flow),
    github: GitHubSetupApi = Depends(setup_github_api),
) -> dict:
    """Start the first team's (for one person, the personal workspace's)
    smoke again after it failed; a running or passed smoke is answered as it
    is."""
    first, _ = _granted_repository(first_workspace(record, ctx, people, caller=caller,
                                                   flow=flow, github=github))
    doc = first.doc
    if (doc.get("smoke") or {}).get("state") == FAILED:
        doc = {**doc, "smoke": None}
    smoke = advance_smoke(record, first.key, doc, runs=runs,
                          pool_accounts=_pool_labels(request, ctx, first.tenant), now=ctx.now(),
                          path=first.path, principal=first.principal)
    if smoke is None:
        raise Conflict("the first workspace's smoke waits for its first pool account")
    return {"team": None if first.personal else first.key, "workspace": first.tenant,
            "smoke": smoke}


#: The console's two verify runs, by their `verify_runs` key.
VERIFY_KEYS = ("console_task", "console_workflow")
#: Fresh runs started per key after the recorded one failed or was cancelled
#: (BGD, 2026-10-30: "swarm-api restarts a failed key"). The engine waits for
#: a new id at most twice too (VERIFY-ENGINE-2), so a third would go unread.
MAX_VERIFY_RETRIES = 2
#: A key's retry count lives in the document's own `retries` field,
#: `{console_task: n, console_workflow: n}` (BGD, 2026-10-30), so
#: `verify_runs` holds exactly the INTERFACE SHEET's two id keys. A document
#: written before that kept each count beside its id, as `<key>_retries`:
#: it is still read (`_verify_retries`), and the next write moves it.
LEGACY_RETRIES_SUFFIX = "_retries"


def _verify_ids(runs: dict[str, Any]) -> dict[str, str]:
    """The two ids of a `verify_runs` value, without any legacy retry count."""
    return {key: str(runs[key]) for key in VERIFY_KEYS if runs.get(key)}


def _verify_retries(done: dict[str, Any]) -> dict[str, int]:
    """Each key's retry count: the `retries` field, else the count an older
    document kept inside `verify_runs`."""
    found = done.get("retries")
    if isinstance(found, dict):
        return {key: int(found.get(key) or 0) for key in VERIFY_KEYS}
    legacy = done.get("verify_runs") or {}
    return {key: int(legacy.get(key + LEGACY_RETRIES_SUFFIX) or 0) for key in VERIFY_KEYS}


def _verify_run_state(runs: Any, key: str, tenant: str, run_id: str) -> str:
    """A recorded verify run's state as `run_state` says it (CANCELLED and
    DEAD_LETTERED read as failed)."""
    if key == "console_task":
        return runs.task_state(tenant_id=tenant, task_id=run_id)
    return runs.workflow_state(tenant_id=tenant, workflow_id=run_id)


def _submit_verify_run(runs: Any, key: str, tenant: str, group: str, repository: str) -> str:
    if key == "console_task":
        return runs.submit_task(tenant_id=tenant, group=group, repository=repository,
                                prompt=VERIFY_TASK_PROMPT, label=VERIFY_LABEL,
                                strategy=VERIFY_TASK_STRATEGY)
    return runs.submit_workflow(tenant_id=tenant, group=group, repository=repository,
                                label=VERIFY_LABEL)


def ensure_verify_runs(record: ir.InstallRecord, ctx: AppContext, first: FirstWorkspace | None,
                       *, runs: Any, by: str) -> dict[str, Any]:
    """The console's two verification runs, one task and one workflow, as the
    first team (for one person, in the personal workspace) on a repository of
    its grant (decision 3: the team's own slot, never a person's token). Their
    ids go to `console/verify_runs` as `verify_runs: {console_task,
    console_workflow}`, which the engine copies into
    `state.outputs["verify_runs"]`, so nobody pastes an id.

    Idempotent: each recorded run is read, and one that failed or was
    cancelled counts as missing and a fresh run of its shape replaces its id,
    the other key kept; one still going, or succeeded, is left alone. Each
    key's fresh starts are counted in the document's `retries` field and
    answered as `retries`; a key that failed after `MAX_VERIFY_RETRIES` of
    them is a 409 saying so, and nothing is started. `finish`, the status
    route (once `console/finished` exists) and `POST /verify-runs` all call
    this, so the runs start without anyone pressing a button (audit 5b, B1)."""
    path = record.console_path(ir.VERIFY_RUNS)
    done = record.read(path) or {}
    started = _verify_ids(dict(done.get("verify_runs") or {}))
    retries = _verify_retries(done)
    first, repository = _granted_repository(first)
    tenant = first.tenant
    group = first.principal
    missing: list[str] = []
    for key in VERIFY_KEYS:
        if not started.get(key):
            missing.append(key)
        elif _verify_run_state(runs, key, tenant, started[key]) == FAILED:
            if retries[key] >= MAX_VERIFY_RETRIES:
                raise Conflict(
                    f"the console's verify {key.removeprefix('console_')} run {started[key]} "
                    f"failed after {MAX_VERIFY_RETRIES} fresh starts, so none is started "
                    "again: read that run for why",
                    detail={"key": key, "run": started[key], "retries": retries[key]})
            missing.append(key)
    if not missing:
        return {"verify_runs": started, "retries": retries,
                "repository": done.get("repository"), "started": False}
    if not app_self_checked(record):
        raise Conflict(f"the console's verify runs are {PASS_3_WAIT}",
                       detail={"state": SELF_CHECK_WAITING, "missing": missing})
    for key in missing:
        if started.get(key):
            retries[key] += 1
        started[key] = _submit_verify_run(runs, key, tenant, group, repository)
        record.write(path, {"verify_runs": dict(started), "retries": dict(retries),
                            "repository": repository,
                            "started_at": done.get("started_at") or ctx.now(),
                            "by": by})
    log.info("console verify runs started %s task=%s workflow=%s retries=%s by=%s",
             ",".join(missing), started["console_task"], started["console_workflow"],
             retries, by)
    return {"verify_runs": started, "retries": retries, "repository": repository,
            "started": True}


def keep_verify_runs(record: ir.InstallRecord, ctx: AppContext, first: FirstWorkspace | None,
                     *, runs: Any, by: str) -> dict[str, Any] | None:
    """`ensure_verify_runs` for a step that must not fail on it (`finish`,
    the status route): its answer, or None with the reason logged (a key
    whose retries are spent, a read that failed); the page then serves the
    document as it stands."""
    try:
        return ensure_verify_runs(record, ctx, first, runs=runs, by=by)
    except ApiError as exc:
        log.info("console verify runs not started code=%s", exc.code)
        return None


@router.post("/verify-runs", dependencies=[Depends(admin_auth)])
def start_verify_runs(
    caller: SetupCaller = Depends(admin_setup_caller),
    ctx: AppContext = Depends(get_context),
    record: ir.InstallRecord = Depends(install_record),
    runs: Any = Depends(setup_runs),
    people: People = Depends(people_service),
    flow: ManifestFlow = Depends(manifest_flow),
    github: GitHubSetupApi = Depends(setup_github_api),
) -> dict:
    """`ensure_verify_runs`, on request: `finish` and the status route start
    and restart the runs themselves, so the page offers this only once a
    key's retries are spent (a 409 says so)."""
    first = first_workspace(record, ctx, people, caller=caller, flow=flow, github=github)
    return ensure_verify_runs(record, ctx, first, runs=runs, by=caller.email)


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
    people: People = Depends(people_service),
    flow: ManifestFlow = Depends(manifest_flow),
    github: GitHubSetupApi = Depends(setup_github_api),
) -> dict:
    """One route, three kinds (review finding 21). The token is in this answer
    and nowhere else; Firestore keeps its digest.

    For one person (decision 7) a pool account is owned by the installer's
    personal workspace and its link is the installer's alone, and the
    `team-git` link names the personal workspace."""
    owner = roles.subject_for_write(body.owner)
    tenant_id: str | None = None
    team_id: str | None = None
    progress_path: str | None = None
    engine = record.read_engine()
    personal = None
    if install_mode(engine) == ir.INDIVIDUAL and body.kind in (sl.POOL_ACCOUNT, sl.TEAM_GIT):
        personal = first_workspace(record, ctx, people, caller=caller, flow=flow,
                                   github=github, engine=engine, mode=ir.INDIVIDUAL)
        if personal is None:
            raise Conflict("this one-person install names no installer yet")
    if personal is not None and body.kind == sl.POOL_ACCOUNT:
        if owner != personal.principal:
            raise Forbidden(f"a one-person install's pool accounts are the installer's; "
                            f"{owner} is not {personal.principal}")
        team_id, tenant_id = personal.key, personal.tenant
    elif personal is not None and body.kind == sl.TEAM_GIT and body.label == personal.key:
        team_id, tenant_id = personal.key, personal.tenant
        progress_path = personal.path
    elif body.kind == sl.POOL_ACCOUNT:
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
        progress_path = record.team_path(team_id)
    elif body.label not in sl.ADDON_SECRETS:
        raise ValidationFailed(f"no add-on secret for {body.label!r}",
                               detail={"addons": sorted(sl.ADDON_SECRETS)})
    token, link = links.mint(kind=body.kind, label=body.label, owner=owner, by=caller.email,
                             tenant_id=tenant_id, team_id=team_id)
    if body.kind == sl.POOL_ACCOUNT:
        record.write(record.console_path(ir.ACCOUNTS), {body.label: {
            "owner": owner, "link_hash": sl.digest(token), "link_expires": link["expires_at"],
        }})
    elif body.kind == sl.TEAM_GIT and progress_path is not None:
        team = record.read(progress_path) or {}
        progress = dict(team.get("progress") or {})
        progress["git_link"] = {"owner": owner, "expires_at": link["expires_at"]}
        record.write(progress_path, {"progress": progress})
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
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Open a link as the person it was minted for, once.

    `pool-account` answers the first team's workspace id and the label; the
    console then runs the existing sign-in (`POST /v1/accounts/authorize`,
    then `/exchange`) with that id as `X-Swarm-Tenant`, so the account lands
    in the first team. Until that workspace is `ready` the sign-in would be
    refused, so a `pool-account` link is answered 409 (`retry: true`) and NOT
    consumed; the page asks again. `team-git` and `addon-secret` take the value in the same
    request and write it straight into their one container; it is never
    returned or logged. Their link is consumed only AFTER the write, so a
    missing value or a failed write leaves it usable. A team's git container
    is created on first use (an install has no Terraform tenant to make it)."""
    peeked = links.peek(body.token, email=auth.email)
    kind = peeked.get("kind")
    if kind == sl.POOL_ACCOUNT:
        tenant_id = str(peeked.get("tenant_id") or "")
        state = (ctx.submissions.workspaces.get(tenant_id) or {}).get("state")
        if state != ws.READY:
            # The sign-in that follows names this tenant in X-Swarm-Tenant,
            # which is refused until the workspace is ready: consuming the
            # link now would spend it on a 403 (audit 4d, F6). The page
            # retries this route until it answers.
            raise Conflict(
                "this link's workspace is not ready yet, so its sign-in would be refused; "
                "the link is still unused, open it again in a moment",
                detail={"workspace": tenant_id, "workspace_state": state,
                        "retry": True})
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
    people: People = Depends(people_service),
) -> dict:
    """Write `console/finished` once every required console step is done: the
    App installed and its self-check passed, a first team, a pool account
    signed in to it, and the first team's smoke passed (`advance_smoke`,
    started here too if the page has not started it yet). A labelled account
    seen in the first team's pool gets its `signed_in_at` here. It then
    starts the console's two verify runs (`keep_verify_runs`, idempotent),
    answered as `verify_runs`; the CLI starts verify when `finished` exists
    and reads their ids from `console/verify_runs`.

    For one person (decision 7) the personal workspace takes the first team's
    place: no team, no invites; its pool account and its smoke
    (`personal_smoke`) are what `finish` waits for."""
    app = record.read(record.console_path(ir.GITHUB_APP)) or {}
    engine = record.read_engine()
    mode = install_mode(engine)
    first = first_workspace(record, ctx, people, caller=caller, flow=flow, github=github,
                            engine=engine, mode=mode)
    accounts = record.read(record.console_path(ir.ACCOUNTS)) or {}
    missing: list[str] = []
    if not app.get("installed_on"):
        missing.append("github_app_installed")
    if not (app.get("self_check") or {}).get("ok"):
        missing.append("github_app_self_check")
    if first is None:
        missing.append("personal_workspace" if mode == ir.INDIVIDUAL else "first_team")
    signed = {label for label, value in accounts.items() if (value or {}).get("signed_in_at")}
    if first is not None and accounts:
        tenant = first.tenant
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
        doc = first.doc
        grant = (doc.get("grant") or {})
        if grant.get("state") != "granted" or not grant.get("repositories"):
            grant = ensure_grant(record, first.key, doc, flow=flow, github=github,
                                 org=ctx.settings.setup_github_org, by=caller.email,
                                 now=ctx.now(), db=ctx.db, path=first.path)
            doc = {**doc, "grant": grant}
            first = replace(first, doc=doc)
        smoke = advance_smoke(record, first.key, doc, runs=runs, pool_accounts=sorted(signed),
                              now=ctx.now(), path=first.path, principal=first.principal) or {}
        smoke_passed = smoke.get("state") == SUCCEEDED
    if not smoke_passed:
        missing.append("personal_smoke" if mode == ir.INDIVIDUAL else "first_team_smoke")
    if missing:
        raise Conflict("setup is not finished yet", detail={"missing": missing})
    done = record.read(record.console_path(ir.FINISHED))
    if done is None:
        done = record.write(record.console_path(ir.FINISHED), {"at": ctx.now(), "by": caller.email})
    # The console's two verify runs start here, not on a button nobody is
    # told to press (audit 5b, B1): swarm install's verify waits for them.
    verify = keep_verify_runs(record, ctx, first, runs=runs, by=caller.email)
    return {"finished": done, "verify_runs": verify}


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


class GroupProbe(StrictModel):
    group: str = Field(min_length=3, max_length=320)
    member: str = Field(min_length=3, max_length=320)


class GroupsProbeRequest(StrictModel):
    #: The `{group, member}` pairs to probe (stage 8's identity steps). Empty
    #: or absent: every registered group, readability only (the doctor).
    groups: list[GroupProbe] = Field(default_factory=list, max_length=50)


#: A failed lookup's reason, by what its error says; the engine's `why_unmet`
#: turns each into the sentence a person can act on. The message itself
#: (Cloud Identity's error body) is never answered.
_PROBE_REASONS: tuple[tuple[str, str], ...] = (
    ("unauthorized_client", "unauthorized_client"),
    ("may not sign as itself", "sign_jwt_denied"),
    ("signjwt", "sign_jwt_denied"),
    ("returned 403", "permission_denied"),
    ("returned 404", "not_found"),
    ("returned no name", "group_not_found"),
)


def probe_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    return next((reason for needle, reason in _PROBE_REASONS if needle in text), "unreadable")


def probe_member(directory: MembershipResolver, member: str, group: str) -> bool:
    """One `checkTransitiveMembership`, past the cache. `is_member` when the
    directory has it (`CloudIdentityGroups`), so its error keeps Cloud
    Identity's status for `probe_reason`; else `groups_for` on one group."""
    ask = getattr(directory, "is_member", None)
    if callable(ask):
        return bool(ask(member, group, fresh=True))
    return check_group_readable(directory, member, group)


def _registered_groups(ctx: AppContext) -> list[str]:
    authenticator = ctx.authenticator
    registered = getattr(authenticator, "registered_group_workspaces", None)
    groups: list[str] = list(ctx.settings.tenant_groups) + list(ctx.settings.admin_groups)
    if callable(registered):
        try:
            groups += list(registered().values())
        except Exception:  # noqa: BLE001 - the configured groups still answer
            log.warning("groups probe: the registry could not be read")
    return groups


@router.post("/health/groups-probe")
def groups_probe(
    body: GroupsProbeRequest | None = None,
    caller: SetupCaller = Depends(setup_caller),
    ctx: AppContext = Depends(get_context),
    directory: MembershipResolver | None = Depends(workspace_directory),
) -> dict:
    """Stage 8's and the doctor's re-probe, asked with swarm-api's OWN group
    identity (review finding 12), one per-group membership question each
    (`searchTransitiveGroups` 403s here), past the cache.

    Asked with `{"groups": [{group, member}]}` (stage 8, `ApiClient.groups_probe`)
    it probes exactly those pairs: `met` when the member is in the group,
    `unmet` with `not_a_member` when the group answered no, and `unmet` with
    the failure's reason when it could not be read. Asked with none (the
    doctor), it probes every registered group as the caller: any answer is
    `met`. The answer is `{"mode": <GROUPS_AUTH>, "results": [...]}` (finding
    N6), with `groups` (the same rows) and `met` kept for the doctor's older
    readers; never a member list."""
    pairs: list[tuple[str, str | None]] = [
        (p.group.strip(), p.member.strip().lower()) for p in (body.groups if body else [])]
    if not pairs:
        pairs = [(group.strip().lower(), None) for group in _registered_groups(ctx)]
    # Each group is answered under the name it was asked by (the engine's
    # `GroupsProbe.of` matches it exactly), once per (group, member).
    seen: list[tuple[str, str | None]] = []
    keys: set[tuple[str, str | None]] = set()
    for group, member in pairs:
        key = (group.lower(), member)
        if group and key not in keys:
            keys.add(key)
            seen.append((group, member))
    results: list[dict[str, Any]] = []
    for group, member in seen:
        if directory is None:
            results.append({"group": group, "state": "unmet", "reason": "no_directory"})
            continue
        try:
            found = probe_member(directory, member or caller.email, group)
        except Exception as exc:  # noqa: BLE001 - every failure is this group's unmet
            results.append({"group": group, "state": "unmet", "reason": probe_reason(exc),
                            "detail": type(exc).__name__})
            continue
        if member is not None and not found:
            results.append({"group": group, "state": "unmet", "reason": "not_a_member"})
        else:
            results.append({"group": group, "state": "met"})
    log.info("groups probe by=%s groups=%d unmet=%d", caller.kind, len(results),
             sum(1 for r in results if r["state"] != "met"))
    return {"mode": ctx.settings.groups_auth, "results": results, "groups": results,
            "met": all(r["state"] == "met" for r in results)}


__all__ = ["admin_setup_caller", "links_router", "router", "setup_caller"]
