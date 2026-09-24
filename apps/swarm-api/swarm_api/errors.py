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
