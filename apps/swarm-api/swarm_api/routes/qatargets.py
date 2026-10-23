"""QA targets' routes (docs/design/visual-qa-any-app.md §1, §2.1, §2.2, §6, §7;
lane VQ-2 TARGETS, part of #1933).

    POST   /v1/qa-targets                          register (a workspace admin)
    GET    /v1/qa-targets                          the workspace's targets (any role)
    GET    /v1/qa-targets/{id}                     one (any role)
    PATCH  /v1/qa-targets/{id}                     change (a workspace admin); a new host is pending
    DELETE /v1/qa-targets/{id}                     delete, with its credential (a workspace admin)
    POST   /v1/qa-targets/{id}:approve-host        a PLATFORM admin approves the host (VQ-Q1)
    POST   /v1/qa-targets/{id}:run                 a member starts a run: {screens?, file?}
    POST   /v1/qa-targets/{id}:discover            a member starts its discovery (§4.1)
    POST   /v1/qa-targets/{id}/credential-links    a one-time link to the console's form (an admin)
    PUT    /v1/qa-targets/{id}/credential          the form's write, with the link's token
    POST   /v1/attempts/{attempt_id}/qa-target     WORKER ONLY: its attempt's target

Four kinds of caller. A WORKSPACE ADMIN registers, changes and deletes a
target and sets its credential: its group workspace's `admin`, the person of
a personal workspace, or a platform admin (who holds `admin` in every group
workspace, Q5). Anyone else is 403 before anything is read or written, firmly
and not through a report-only switch: these routes are new, so no existing
caller can be refused by them. A MEMBER runs a target (`require_member`, the
floor every submission meets) and reads its workspace's targets. A PLATFORM
ADMIN approves a host (`admin_auth`). THE WORKER reads its attempt's target,
as for the QA identity: no `current_auth`, the tenant its ID token checked
against the lease's, and the attempt key's signature over the raw body.

THE CREDENTIAL IS ACCEPTED ON THE PUT AND NOWHERE ELSE, and no route returns
it. Its body is not a `StrictModel`, whose whitespace stripping would change a
password; the validation handler never echoes a body. The PUT and the
worker's route are in `main.UNCAPTURED_ROUTES`.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..deps import AppContext, admin_auth, current_auth, get_context, tenant_scope
from ..errors import Forbidden
from ..mergesettings import is_owner
from ..qatargets import (
    ADMIN_REQUIRED_MESSAGE,
    PASSWORD_MAX,
    USERNAME_MAX,
    QaCredentialStore,
    QaTargetIssuer,
    QaTargets,
    SecretManagerQaCredentials,
    render_discovery_run,
    render_run,
    resolve_host,
    to_api,
)
from ..schemas import StrictModel, WorkflowCreate
from ..validation import qa_target_mark_allowed
from ..workspace_secrets import workspace_admin
from .children import child_service
from .tasks import require_member

router = APIRouter(prefix="/v1", tags=["qa-targets"])

#: The routes `main.UNCAPTURED_ROUTES` names: the one that takes the
#: credential, and the one that serves it to the worker.
CREDENTIAL_ROUTE = "/v1/qa-targets/{target_id}/credential"
ISSUE_ROUTE = "/v1/attempts/{attempt_id}/qa-target"


class TargetBody(StrictModel):
    """A registration, or a change of one. Each field is checked by
    `swarm_api.qatargets` and `swarm_api.qaspec`, so a refusal is their own
    sentence rather than a validation list. No credential: that is the PUT's."""

    name: Any = None
    base_url: Any = None
    repository: Any = None
    sign_in: Any = None
    screens: Any = None
    discover: Any = None
    flows: Any = None
    widths: Any = None
    themes: Any = None
    exclusions: Any = None
    environment: Any = None
    read_posts: Any = None
    design_docs: Any = None
    file: Any = None
    rate: Any = None
    clock: Any = None
    #: The registrant's statement of who said this application may be tested.
    authorized_by: Any = None


class ApproveBody(StrictModel):
    #: The host the admin approves: it must be the target's host now.
    host: str = Field(min_length=1, max_length=253)
    #: The login's other origin, approved only when named here (§1.2).
    extra_origin: str | None = Field(default=None, max_length=300)


class RunBody(StrictModel):
    screens: list[str] | None = Field(default=None, max_length=60)
    file: Literal["issues", "none"] = "issues"


class CredentialWrite(BaseModel):
    #: Write-only. Not stripped: a password's spaces are its own.
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=USERNAME_MAX)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)
    #: The `t` of the one-time link (`POST .../credential-links`): required.
    link_token: str = Field(min_length=1, max_length=256)


def _resolver(request: Request) -> Any:
    return getattr(request.app.state, "qa_target_resolver", None) or resolve_host


def _credentials(request: Request, ctx: AppContext) -> QaCredentialStore:
    """The app's store: a test's, or Secret Manager, built once on first use."""
    store = getattr(request.app.state, "qa_credential_store", None)
    if store is None:
        store = SecretManagerQaCredentials(str(getattr(ctx.settings, "project_id", "") or ""))
        request.app.state.qa_credential_store = store
    return store


def _targets(request: Request, ctx: AppContext) -> QaTargets:
    return QaTargets(ctx.db, now=ctx.now, resolve=_resolver(request))


def _email(auth: AuthContext) -> str:
    return (auth.email or "").strip().lower()


def require_workspace_admin(ctx: AppContext, auth: AuthContext, tenant_id: str) -> str:
    """The caller's email when they administer `tenant_id` (§1); 403 otherwise."""
    tenant = ctx.store.get_tenant(tenant_id)
    if auth.is_admin:
        return _email(auth)
    if tenant is not None and (is_owner(auth, tenant) or workspace_admin(ctx.roles, auth, tenant)):
        return _email(auth)
    raise Forbidden(ADMIN_REQUIRED_MESSAGE)


def _changes(body: BaseModel) -> dict[str, Any]:
    return {key: getattr(body, key) for key in body.model_fields_set}


@router.post("/qa-targets", status_code=status.HTTP_201_CREATED)
def create_target(
    body: TargetBody,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    by = require_workspace_admin(ctx, auth, tenant_id)
    doc = _targets(request, ctx).create(tenant_id, body.model_dump(), by=by)
    return {"qa_target": to_api(doc)}


@router.get("/qa-targets")
def list_targets(
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # A discovery whose task ended with no wake is settled as it is read.
    targets = _targets(request, ctx)
    docs = targets.settle_running(tenant_id, targets.list(tenant_id), ctx.inspection)
    return {"qa_targets": [to_api(d) for d in docs]}


@router.get("/qa-targets/{target_id}")
def get_target(
    target_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    targets = _targets(request, ctx)
    [doc] = targets.settle_running(tenant_id, [targets.get(tenant_id, target_id)], ctx.inspection)
    return {"qa_target": to_api(doc)}


@router.patch("/qa-targets/{target_id}")
def patch_target(
    target_id: str,
    body: TargetBody,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    by = require_workspace_admin(ctx, auth, tenant_id)
    doc = _targets(request, ctx).patch(tenant_id, target_id, _changes(body), by=by)
    return {"qa_target": to_api(doc)}


@router.delete("/qa-targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(
    target_id: str,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> Response:
    by = require_workspace_admin(ctx, auth, tenant_id)
    _targets(request, ctx).delete(tenant_id, target_id, by=by,
                                  credentials=_credentials(request, ctx))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/qa-targets/{target_id}:approve-host")
def approve_host(
    target_id: str,
    body: ApproveBody,
    request: Request,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A platform admin approves the host they name (VQ-Q1, audited)."""
    doc = _targets(request, ctx).approve_host(target_id, host=body.host,
                                              extra_origin=body.extra_origin, by=_email(auth))
    return {"qa_target": to_api(doc)}


@router.post("/qa-targets/{target_id}:run", status_code=status.HTTP_201_CREATED)
def run_target(
    target_id: str,
    body: RunBody,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A run of the target, as this member's submission in this workspace.
    Every refusal comes before anything is admitted (§5)."""
    require_member(ctx, auth)
    targets = _targets(request, ctx)
    target = targets.runnable(tenant_id, target_id, file=body.file)
    workflow, mark, browser = render_run(target, screens=body.screens, file=body.file)
    # The mark is reserved (`validation.QA_TARGET_METADATA_KEY`): only this
    # exact value, only for this submission, gets past the refusal.
    with qa_target_mark_allowed(mark):
        submission = ctx.submissions.submit_workflow(auth, WorkflowCreate.model_validate(workflow))
    workflow_id = submission.workflow.workflow_id
    task_ids = [t.id for t in submission.tasks if t.step_id in set(browser)]
    targets.record_run(tenant_id=tenant_id, target=target, workflow_id=workflow_id,
                       task_ids=task_ids, file=body.file, by=_email(auth))
    return {"workflow_id": workflow_id, "qa_target": mark, "file": body.file,
            "browser_steps": browser}


@router.post("/qa-targets/{target_id}:discover", status_code=status.HTTP_201_CREATED)
def discover_target(
    target_id: str,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The target's discovery (§4.1), as this member's submission, marked as
    `:run`'s browser steps are. One at a time per target: a second call while
    one runs is 409 (`detail.reason` `qa_discovery_running`), naming it. The result is stored
    on the target when the step ends (`QaTargets.settle_discovery`)."""
    require_member(ctx, auth)
    targets = _targets(request, ctx)
    target = targets.discoverable(tenant_id, target_id, inspection=ctx.inspection)
    workflow, mark, browser = render_discovery_run(target)
    by = _email(auth)
    claim = targets.claim_discovery(tenant_id, target, by=by)
    try:
        with qa_target_mark_allowed(mark):
            submission = ctx.submissions.submit_workflow(auth, WorkflowCreate.model_validate(workflow))
    except BaseException:
        targets.release_discovery(target_id, claim)
        raise
    workflow_id = submission.workflow.workflow_id
    task_ids = [t.id for t in submission.tasks if t.step_id in set(browser)]
    targets.record_run(tenant_id=tenant_id, target=target, workflow_id=workflow_id,
                       task_ids=task_ids, file="none", by=by, kind="discovery")
    targets.record_discovery(target_id, claim, workflow_id=workflow_id, task_id=task_ids[0])
    return {"workflow_id": workflow_id, "qa_target": mark}


@router.post("/qa-targets/{target_id}/credential-links", status_code=status.HTTP_201_CREATED)
def mint_credential_link(
    target_id: str,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The link's URL (and path) and its expiry, and nothing else: the
    plugin's `swarm_qa_target_credential_link` passes on only the URL and the
    expiry, as `keys.key_set_link` does (§2.1)."""
    by = require_workspace_admin(ctx, auth, tenant_id)
    return _targets(request, ctx).mint_link(tenant_id, target_id, email=by,
                                            console=ctx.settings.console_url)


@router.put("/qa-targets/{target_id}/credential")
def put_credential(
    target_id: str,
    body: CredentialWrite,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    by = require_workspace_admin(ctx, auth, tenant_id)
    status_ = _targets(request, ctx).set_credential(
        tenant_id, target_id, username=body.username, password=body.password,
        link_token=body.link_token, by=by, credentials=_credentials(request, ctx))
    return {"credential": status_}


@router.post("/attempts/{attempt_id}/qa-target")
async def issue_qa_target(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    ctx: AppContext = Depends(get_context),
    children: ChildService = Depends(child_service),
) -> Response:
    """WORKER ONLY: the attempt's QA target, 204 when its agent has none."""
    raw = await request.body()
    issuer = QaTargetIssuer(settings=ctx.settings, db=ctx.db, children=children,
                            credentials=_credentials(request, ctx), now=ctx.now)
    answer = await run_in_threadpool(
        lambda: issuer.issue(
            serverless_authorization or authorization,
            attempt_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    headers = {"Cache-Control": "no-store"}
    if answer is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)
    return Response(content=json.dumps(answer), media_type="application/json", headers=headers)
