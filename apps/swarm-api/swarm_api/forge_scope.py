"""Can this tenant's forge credential push to this repository? (D13)

`carrier: branches` pushes the step's committed work to its branch at park,
cancel, SIGTERM and finish. With a token that cannot push, every one of those
pushes is refused at the forge, the worker logs it and goes on, and the caller
who asked for durable branches gets none. So the API refuses the submission
with a 422 instead, and decides from the token's REAL scope (owner decision,
2026-10-02), not from an operator's declaration.

THE SAME QUESTION, ASKED THE SAME WAY, AS THE WORKER'S PUBLISH. The worker
learns write scope only by asking the forge with the token
(`agent_worker.lifecycle._carrier_target` -> `agent_worker.forge.probe_repository`,
GitHub's `permissions.push`), so the control plane had no answer of its own to
reuse. This module calls those same two functions rather than restating them:

  1. `agent_worker.secrets.resolve_git_token` -- the tenant's own
     `swarm-tenant-<tenant>-git` secret (the frozen `Tenant.secret_name`),
     latest version, read only when `git` is in the tenant's `credentials`,
     a bare token or `{"GIT_TOKEN": ...}`;
  2. `agent_worker.forge.probe_repository` -- ONE `GET /repos/{owner}/{repo}`
     with that token; `can_push` is `permissions.push is True`.

WHAT IT READS, AND ONLY WHEN. One secret version and one forge GET, for a
submission that asks for `carrier: branches` -- never for `checkpoints`, which
pushes nothing. The token is a local inside `check()`: it is never returned,
stored, logged or put in an error. What leaves this module is `can_push` and
the probe's reason, which is a fixed sentence or the forge's own status line.

WHAT IT CANNOT DO WITHOUT IAM. Reading the secret needs
`roles/secretmanager.secretAccessor` for swarm-api's service account on each
tenant's `-git` secret, which today only the tenant's worker holds. Until that
grant exists the read fails, and the submission is answered 503 -- "write
scope could not be established" -- never admitted on a guess and never refused
as read-only on a guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from swarm_common.models import Tenant

from .errors import UpstreamUnavailable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteScope:
    """The forge's answer. `reason` is always set, and never carries the token."""

    can_push: bool
    reason: str


class ForgeWriteScope(Protocol):
    def check(self, tenant: Tenant, repository_url: str) -> WriteScope: ...


class _TokenLogger:
    """The `logger` `resolve_git_token` takes: it logs the secret's NAME only.

    `register_secret` is the worker's scrub registration. Nothing here logs
    the token, so there is nothing to scrub; it is accepted and dropped.
    """

    def register_secret(self, _value: str) -> None:
        return None

    def info(self, message: str, **fields: Any) -> None:
        secret = fields.get("secret")
        log.info("%s (%s)", message, secret if isinstance(secret, str) else "")


class SecretManagerForgeScope:
    """The deployed answer: the tenant's own token, asked of the forge."""

    def __init__(self, project_id: str, client: Any | None = None) -> None:
        from agent_worker.secrets import SecretManagerClient

        self._client = SecretManagerClient(project_id, client=client)

    def check(self, tenant: Tenant, repository_url: str) -> WriteScope:
        from agent_worker.forge import ForgeError, probe_repository
        from agent_worker.secrets import resolve_git_token

        try:
            token = resolve_git_token(tenant=tenant, client=self._client, logger=_TokenLogger())
        except Exception as exc:
            # The exception's TYPE only. A Secret Manager error carries no
            # payload, but nothing about this path should depend on that.
            log.warning(
                "forge write scope: the git secret of tenant %s could not be read (%s)",
                tenant.tenant_id,
                type(exc).__name__,
            )
            raise UpstreamUnavailable(
                "carrier 'branches' needs this tenant's forge credential to be able to "
                "push, and whether it can could not be established: the credential "
                "could not be read. Nothing was created; try again, or choose carrier "
                "'checkpoints'.",
                detail={"carrier": "branches", "forge_access": "unknown"},
            ) from None
        try:
            access = probe_repository(url=repository_url, token=token)
        except ForgeError as exc:
            # `ForgeError` names the host and the network reason, never the token.
            log.warning("forge write scope: the forge could not be reached (%s)", exc)
            raise UpstreamUnavailable(
                "carrier 'branches' needs this tenant's forge credential to be able to "
                "push, and whether it can could not be established: the forge could "
                "not be reached. Nothing was created; try again, or choose carrier "
                "'checkpoints'.",
                detail={"carrier": "branches", "forge_access": "unknown"},
            ) from None
        if access is None:
            return WriteScope(
                can_push=False,
                reason="this repository is not on a forge the worker can push to",
            )
        return WriteScope(can_push=access.can_push, reason=str(access.reason))


class StaticForgeScope:
    """A fixed answer, for local development and tests: no secret, no network."""

    def __init__(self, *, can_push: bool, reason: str | None = None) -> None:
        self._scope = WriteScope(
            can_push=can_push,
            reason=reason
            or (
                "the token has write permission on this repository"
                if can_push
                else "the token has pull but not push"
            ),
        )
        self.calls: list[tuple[str, str]] = []

    def check(self, tenant: Tenant, repository_url: str) -> WriteScope:
        self.calls.append((tenant.tenant_id, repository_url))
        return self._scope
