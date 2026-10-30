"""A person's forge slot, obtained only from swarm-api's broker (#1041).

docs/design/user-scoped-secrets.md §3.2 is the design; `swarm_api.credbroker`
is the other half. A group tenant's workers share one service account, and
every agent can mint its worker's token, so IAM cannot keep Alice's
`git-u-<hex>` slot from Bob's attempt. This worker therefore never reads a
person's slot from Secret Manager (its account no longer holds that read).
It POSTs `/v1/attempts/forge-credential` with:

  * its ID token for swarm-api's audience (`children.ChildApi`), which says
    which tenant's worker is asking;
  * the attempt tuple and the access it needs -- and nothing else: the body
    names no secret, so swarm-api picks the slot from the task's SIGNED
    `forge_credential` and `submitted_by`, never from this request;
  * a signature over that exact request by the attempt key this worker
    registered while the task was STARTING, before the agent existed
    (`children.ChildPath.sign_request`). The private key never leaves this
    process's heap.

FAILS CLOSED WHERE THE BROKER IS DEPLOYED. No registered key (no nonce, a refused registration, a
tombstone), a refusal, or no answer is a `ForgeCredentialRefused` naming the
secret and the code -- never a fallback to the tenant token, which would run
the task as somebody it was not resolved to. Two refusals keep the meaning
they already have here: `attempt_superseded` is the fence (invariant 5,
`FencedError`), and a removed or read-only grant asked for a write is
`ForgeWriteRefused`.

THE SWITCH IS FORGE_BROKER (`WorkerConfig.forge_broker`, terraform
`forge_broker`, off by default and off in every environment until the owner
turns it on; `ForgeBroker.deployed`). Off, nothing here is asked: the worker
reads the slot with its own account, exactly as it did before #1041, which
IAM allows while the bootstrap root's `forge_slot_reader` binding stands.
That binding is removed only after the switch has been on in dev and a live
run passed, as a separate owner step (docs/design/user-scoped-secrets.md §5).
On, the broker is the only path: a worker with no SWARM_API_URL is refused
`forge_broker_unconfigured` rather than falling back to a direct read.

NEVER A VALUE. The token is registered with the logger's redaction before it
is returned, and nothing here logs, raises or stores it: a log line carries
the secret's NAME and the decision code only.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_common.models import Tenant

from .children import ChildApiError, ChildPath
from .errors import FencedError
from .secrets import ForgeWriteRefused, SecretError, git_token_from_payload

#: The route (`swarm_api.routes.credentials`). The proof signs this path.
BROKER_PATH = "/v1/attempts/forge-credential"

#: The broker's refusals that mean a write's grant is gone, as the worker's
#: own causes (`lifecycle.FORGE_GRANT_REVOKED`, `lifecycle.FORGE_READ_GRANT`,
#: restated: lifecycle imports this module). test_forge_broker_client.py
#: holds them equal.
GRANT_CAUSES = {
    "grant_removed": "forge_grant_revoked",
    "grant_read_only": "forge_read_grant",
}
#: swarm-api's code for an attempt that is not the task's current one.
SUPERSEDED = "attempt_superseded"

#: swarm-api's code for a service submission's run it minted no installation
#: token for (`swarm_api.credbroker`, DAYONE-SERVICE-APP-TOKEN): the worker
#: keeps the run's slot (`secrets.resolve_git_token`).
APP_INSTALLATION_UNAVAILABLE = "app_installation_unavailable"

#: This worker's own codes, for refusals made before anything is asked.
NO_ATTEMPT_KEY = "attempt_key_unregistered"
NO_BROKER = "forge_broker_unconfigured"
BAD_ANSWER = "forge_broker_bad_answer"


class ForgeCredentialRefused(SecretError):
    """No person's slot for this attempt. `code` and `secret` are names, never values."""

    def __init__(self, code: str, secret: str, reason: str, *, status: int | None = None) -> None:
        super().__init__(f"{secret} was not released to this attempt ({code}): {reason}")
        self.code = code
        self.secret = secret
        self.status = status


class ForgeBroker:
    """One attempt's client for the broker. Inert until a user slot is asked for."""

    def __init__(self, cfg: Any, *, children: ChildPath, logger: Any) -> None:
        self.cfg = cfg
        self._children = children
        self.log = logger

    @property
    def deployed(self) -> bool:
        """Whether this deployment obtains a person's slot from the broker.

        The FORGE_BROKER switch, and nothing else: an address alone is not
        the switch, because dev has child tasks on (SWARM_API_URL reaches its
        workers) and the broker must not go live there with them. Off,
        `secrets.resolve_git_token` reads the slot with the worker's own
        account, as before #1041, which works while `forge_slot_reader`
        stands. On, `fetch` is the only path, and with no SWARM_API_URL it
        refuses (`NO_BROKER`) rather than reading directly.
        """
        return getattr(self.cfg, "forge_broker", False) is True

    def fetch(self, tenant: Tenant, suffix: str, *, write: bool) -> str:
        """The latest version of the task's `git-u-` slot, released to this attempt.

        `tenant` is this worker's own (`secrets.load_tenant`); the name built
        from it is only checked against the broker's answer and used in
        messages. Asked afresh at every call, never cached: a 401 re-read
        (`secrets.call_reading_again`) asks again.
        """
        secret = tenant.secret_name(suffix)
        api = self._children.api
        if api is None:
            raise ForgeCredentialRefused(
                NO_BROKER, secret, "this attempt has no SWARM_API_URL to ask")
        if not self._children.can_sign:
            why = self._children.unavailable or "not_registered"
            self.log.error("no attempt key; a person's slot is not asked for",
                           secret=secret, reason=why)
            raise ForgeCredentialRefused(
                NO_ATTEMPT_KEY, secret,
                f"this attempt registered no attempt key ({why}), so it cannot prove "
                "itself to the broker",
            )
        body = json.dumps({
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "attempt_id": self.cfg.attempt_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "access": "write" if write else "read",
        }).encode("utf-8")

        def ask() -> tuple[int, dict[str, Any]]:
            # Signed per try: the proof carries a timestamp swarm-api bounds.
            headers = self._children.sign_request("POST", BROKER_PATH, body)
            return api.call("POST", BROKER_PATH, body=body, headers=headers)

        try:
            _, answer = self._children.retrying(ask)
        except ChildApiError as exc:
            raise self._refused(exc, secret, write) from None
        named = answer.get("secret")
        value = answer.get("token")
        if named != secret or not isinstance(value, str):
            raise ForgeCredentialRefused(
                BAD_ANSWER, secret, "the broker's answer did not name this task's slot")
        self.log.register_secret(value.strip())
        token = git_token_from_payload(value, secret)
        del value, answer
        if not token:
            raise ForgeCredentialRefused(BAD_ANSWER, secret, "the broker released an empty slot")
        self.log.register_secret(token)
        self.log.info("forge credential released by the broker", secret=secret,
                      access="write" if write else "read")
        return token

    def _refused(self, exc: ChildApiError, secret: str, write: bool) -> Exception:
        self.log.warning("the forge broker refused this attempt", secret=secret,
                         decision=exc.code, status=exc.status)
        if exc.code == SUPERSEDED:
            return FencedError(self.cfg.generation, -1, f"forge credential: {exc.code}")
        if write and exc.code in GRANT_CAUSES:
            return ForgeWriteRefused(GRANT_CAUSES[exc.code], exc.message or exc.code)
        return ForgeCredentialRefused(exc.code, secret, exc.message or exc.code,
                                      status=exc.status)
