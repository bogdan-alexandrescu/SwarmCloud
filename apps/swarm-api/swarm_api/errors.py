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


class WorkspaceNotRequested(Conflict):
    """A loan request from a person who has not requested a workspace: the
    loan is for the workspace, and admins address it by its id."""

    code = "WORKSPACE_NOT_REQUESTED"
