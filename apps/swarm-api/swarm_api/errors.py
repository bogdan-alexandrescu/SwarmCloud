"""API error types.

Every error raised by the service layer carries an HTTP status and a stable
machine-readable code, so a caller can branch on `code` rather than parsing
prose. Nothing here ever carries token material: `AuthError` messages from the
frozen `swarm_common.identity` module are deliberately token-free and we keep
that property here.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class ValidationFailed(ApiError):
    status_code = 422
    code = "validation_failed"


class Unauthenticated(ApiError):
    status_code = 401
    code = "unauthenticated"


class Forbidden(ApiError):
    status_code = 403
    code = "forbidden"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class Conflict(ApiError):
    status_code = 409
    code = "conflict"


class Gone(ApiError):
    """The record says it existed, and the object behind it no longer does.

    Distinct from `NotFound` on purpose (#184). A 404 from an artifact route
    means "this task lists no artifact of that name" -- the caller named
    something that never was, or has not been uploaded yet. A 410 means the
    manifest DOES list it and the bucket does not hold it: bucket retention
    (`artifact_retention_days`: dev 14, default 90, prod 180) reclaimed it, or
    the upload the manifest records never completed. A UI draws those two as
    different sentences, and it can only do that if the status differs.
    """

    status_code = 410
    code = "artifact_gone"


class RateLimited(ApiError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after_seconds: float) -> None:
        super().__init__(message, detail={"retry_after_seconds": round(retry_after_seconds, 3)})
        self.retry_after_seconds = retry_after_seconds


class UpstreamUnavailable(ApiError):
    status_code = 503
    code = "upstream_unavailable"


class Unpageable(ApiError):
    """A page boundary this service cannot place exactly, so it refuses to guess.

    Keyset pages order by a timestamp and break ties by document id, and to do
    that at a boundary they read every document sharing the boundary's instant.
    That read is bounded. Past the bound the choice is a wrong page -- a row
    skipped or served twice, with a 200 -- or this error, and a wrong page is
    the failure the whole pagination scheme exists to prevent.

    A 500, not a 4xx: the caller asked a correct question. No writer in this
    platform stamps a timestamp coarser than a microsecond, so reaching this
    means one did.
    """

    status_code = 500
    code = "unpageable"


# --------------------------------------------------------------------------
# Personal workspaces (docs/workspaces.md §1.3, §5.2; #847, lane W1)
#
# UPPER-CASE codes, as the owner wrote them and as the onboarding codes are,
# though every other `ApiError` code here is lower-case: the console and the
# plugin branch on these exact strings (§5.2).
# --------------------------------------------------------------------------


class WorkspaceNotReady(Forbidden):
    """A submission to a person's own tenant before its workspace is `ready`."""

    code = "WORKSPACE_NOT_READY"


class NoClaudeAccount(Forbidden):
    """A `ready` workspace with no Claude account to run on (WD6): every
    runner profile, because a ready workspace "runs nothing" until then."""

    code = "NO_CLAUDE_ACCOUNT"


class WorkspaceNotForServiceAccounts(Forbidden):
    code = "WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS"


class WorkspacePrincipalForbidden(Forbidden):
    code = "WORKSPACE_PRINCIPAL_FORBIDDEN"


class WorkspaceIdTaken(Forbidden):
    code = "WORKSPACE_ID_TAKEN"


class WorkspaceRequestTooSoon(Forbidden):
    """A new request inside 24 hours of a denial (§1.3). An admin may still
    approve the denied record at any time; only the person waits."""

    code = "WORKSPACE_REQUEST_TOO_SOON"


class WorkspaceFailed(Conflict):
    """A request against a `failed` record: a retry is an admin's action,
    because it starts a run of a privileged identity (§1.3)."""

    code = "WORKSPACE_FAILED"


class WorkspaceMigrating(Conflict):
    """An approval of a workspace whose personal tenant predates the workspace
    job (docs/workspaces.md §3.3): its identity was made by Terraform, so the
    apply's squat check would fail IDENTITY_NOT_OURS. The migration (lane W9)
    writes its record instead; nothing is published for it. A new refusal:
    raised only through `refusals.refuse`, behind its switch."""

    code = "WORKSPACE_MIGRATING"


class WorkspaceNotRequested(Conflict):
    """A loan request from a person who has not requested a workspace: the
    loan is for the workspace, and admins address it by its id."""

    code = "WORKSPACE_NOT_REQUESTED"


# --------------------------------------------------------------------------
# Workspace kinds: an admin's request for a personal, group or service
# workspace (docs/design/workspace-kinds.md §4.2.3; lane WK-RECORD). Raised
# by `POST /v1/admin/workspaces`, before any write, and the owner-only
# approval by the approve and retry routes on a service record, which only
# that route creates. None is a switch: no existing caller can meet one, and
# continuing past any of them is the breach itself
# (tests/unit/control_plane/test_new_refusals_ship_off.py, NOT_SWITCHABLE_CODES).
# --------------------------------------------------------------------------


class WorkspaceDomainNotAllowed(Forbidden):
    """The person or group named is outside `allowed_domains`: the domain
    check admits people at the front door, and a workspace admits nobody the
    check does not."""

    code = "WORKSPACE_DOMAIN_NOT_ALLOWED"


class WorkspaceIdTerraform(Forbidden):
    """The tenant id is one of Terraform's tenants (`var.tenants`, frozen to
    four under Q8 (a)): the job never builds over what Terraform owns."""

    code = "WORKSPACE_ID_TERRAFORM"


class WorkspaceIdNested(Forbidden):
    """The id and an existing tenant's id nest through `-git-`, so one
    tenant's user-slot names would start with the other's
    (`forge_user_slots.tf::forge_nested_tenants`)."""

    code = "WORKSPACE_ID_NESTED"


class WorkspaceGroupIdReserved(Forbidden):
    """A group whose id starts `u-`: that is a person's id, and the group
    would be handed their workspace (§2.1's collision)."""

    code = "WORKSPACE_GROUP_ID_RESERVED"


class WorkspaceGroupIsAdmin(Forbidden):
    """An ADMIN_GROUPS entry as a tenant would mix the two lookups `auth.py`
    keeps apart."""

    code = "WORKSPACE_GROUP_IS_ADMIN"


class WorkspaceGroupUnreadable(UpstreamUnavailable):
    """Cloud Identity did not answer the one-group check, so swarm-api cannot
    show the group exists and resolves. Retryable; nothing was written."""

    code = "WORKSPACE_GROUP_UNREADABLE"


class WorkspaceServiceNotInProject(Forbidden):
    """Not a user-managed service account of this platform's project (Q7 (a)):
    the job's S1 cannot read any other, so its checks would be the
    requester's word."""

    code = "WORKSPACE_SERVICE_NOT_IN_PROJECT"


class WorkspaceServicePlatform(Forbidden):
    """One of the platform's own accounts (`swarm-*`) or a Google-managed
    default: admitting it would let it file work as a tenant."""

    code = "WORKSPACE_SERVICE_PLATFORM"


class WorkspaceServiceAlreadyResolves(Forbidden):
    """The account already resolves somewhere: a tenant lists it
    (TENANT_SERVICE_ACCOUNTS) or ALLOWED_USERS admits it."""

    code = "WORKSPACE_SERVICE_ALREADY_RESOLVES"


class WorkspaceApprovalOwnerOnly(Forbidden):
    """Approving or retrying a service workspace: it admits a new principal
    at the front door, so only the platform owner decides it (Q2 (a))."""

    code = "WORKSPACE_APPROVAL_OWNER_ONLY"


class BrowserToolUnavailable(ValidationFailed):
    """A `claude-code-browser` task that names `browser_tool: jev` when jev
    cannot run for it: no TypeSafe key resolves for its submitter or tenant
    (KEYS-API), or this deployment's image has no jev (BSTATUS-API). A new
    refusal: raised only through `refusals.refuse`, behind its switch. Off,
    the task is admitted and its runner falls back to Playwright, saying why
    on the live session (`jev.reason`)."""

    code = "browser_tool_unavailable"


class KeyLinkInvalid(Forbidden):
    """A one-time key-form link (KEYS-API, docs/provider-keys.md §3) that is
    unknown, used, expired, someone else's, or for another scope or tenant.
    A new refusal: raised only through `refusals.refuse`, behind its switch.
    Off, the write goes ahead on the caller's own session, which is all the
    link ever proves anyway -- it grants nothing that session could not do."""

    code = "KEY_LINK_INVALID"


# --------------------------------------------------------------------------
# The user profile (docs/design/user-profile.md §4.2-§4.5; lane PROFILE-STORE)
# --------------------------------------------------------------------------

#: What a person reads when their preferences could not be read (Q9): what
#: happened, that nothing was submitted, and every way to name a workspace.
PREFERENCES_UNAVAILABLE_MESSAGE = (
    "Your SwarmCloud preferences could not be read, so your default workspace is "
    "not known and nothing was submitted. Name a workspace and retry: choose one "
    "in the console's workspace switcher, set SWARM_TENANT or the /sc plugin's "
    "tenant option, or pass --tenant to swarm."
)


class PreferencesUnavailable(UpstreamUnavailable):
    """The caller's `user_preferences` document could not be read (§4.5, Q9).

    A 503 and never a guess: a headerless request whose default workspace is
    unknown is refused rather than run in the computed default, which would
    misfile work into a workspace the person did not choose."""

    code = "preferences_unavailable"

    def __init__(self, message: str = PREFERENCES_UNAVAILABLE_MESSAGE, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)


class PreferencesChanged(Conflict):
    """A save whose `revision` is not the stored one (§4.2): another tab saved
    first. Nothing is stored; the page reloads, so two tabs never silently
    overwrite each other."""

    code = "preferences_changed"


class DisplayNameInvalid(ValidationFailed):
    """A display name that breaks §4.4 rules 1-4: its length, a character
    outside letters, marks, spaces and `.'-`, or letters from two scripts.
    The message names the rule."""

    code = "display_name_invalid"


class DisplayNameReserved(ValidationFailed):
    """A display name with a word that reads as a platform role (§4.4 rule 5)."""

    code = "display_name_reserved"


class DisplayNameTaken(ValidationFailed):
    """A display name equal to, or containing, another person's (§4.4 rule 6).
    The message never names the person whose name it collided with."""

    code = "display_name_taken"


# --------------------------------------------------------------------------
# Workspace roles (docs/design/workspace-roles.md §4.9; lane ROLES-API-CORE,
# part of #1518)
#
# Two of these are raised on EXISTING routes and are therefore switches,
# off at ship (`refusals.SWITCHES`, one per code, Q10): `role_member_required`
# and `role_admin_required`. The role write routes are new and raise
# `role_admin_required` with `raise`, never `refuse`: letting a non-admin
# write a role is the breach itself. The rest are raised only by the new
# routes (`NOT_SWITCHABLE_CODES` in the guard says why), and the 503s are not
# refusals at all.
# --------------------------------------------------------------------------


class RoleMemberRequired(Forbidden):
    """A `viewer` asked for something a `member` does (§4.1). A new refusal
    on existing routes: raised only through `refusals.refuse`, behind its
    switch."""

    code = "role_member_required"


class RoleAdminRequired(Forbidden):
    """A `viewer` or `member` asked for something only the workspace's
    `admin` does: its secrets, MCP servers, tenant provider keys, settings,
    or its people's roles (§4.1, Q6, Q13). Behind its switch on existing
    routes; raised directly by the role write routes."""

    code = "role_admin_required"


class RolePersonRequired(Forbidden):
    """A role write naming a service account (§4.6): a listed account has its
    own narrower scope, and a role must not widen it."""

    code = "role_person_required"


class PersonalWorkspaceRolesFixed(Conflict):
    """A role write against a personal (`u-`) workspace: its person is its
    `admin`, and nobody else holds a role there (§4.5)."""

    code = "personal_workspace_roles_fixed"


class DefaultRoleNotAllowed(Conflict):
    """`PUT …/roles/default {admin}`: the default is `viewer` or `member`,
    never `admin`, so a workspace has no admin until one is named (§4.6)."""

    code = "default_role_not_allowed"


class RoleUnchanged(Conflict):
    """A role write that would change nothing; nothing is written (§4.3)."""

    code = "role_unchanged"


class UnknownRole(ValidationFailed):
    """A role that is not `viewer`, `member` or `admin`."""

    code = "unknown_role"


class RoleUnavailable(UpstreamUnavailable):
    """A role could not be read -- Firestore failed, or the newest grants did
    not verify within the bounded read (§4.4) -- at a gate whose switch is
    on, or on a role route. Never a guess, and never a wider role."""

    code = "role_unavailable"


class RoleSigningUnavailable(UpstreamUnavailable):
    """A role write in an environment with no settings key: an unsigned grant
    would be skipped by every reader, so nothing is written (§4.13)."""

    code = "role_signing_unavailable"


class PlatformAdminAuditUnavailable(UpstreamUnavailable):
    """A platform admin's request that must be audited (§4.2.4) and whose
    `admin_audit` row could not be written. The route never runs: an action
    is never taken unaudited (Q12)."""

    code = "platform_admin_audit_unavailable"
