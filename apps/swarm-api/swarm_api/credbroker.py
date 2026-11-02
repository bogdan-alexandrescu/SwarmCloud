"""The forge-credential broker: a person's slot, released only to their own attempt.

docs/design/user-scoped-secrets.md §3.2 is the design this implements; read it
before changing anything here. A group tenant's workers share one service
account, and every agent can mint its worker's token, so IAM cannot tell
Alice's attempt from Bob's. swarm-api can: an attempt proves itself with the
Ed25519 key its worker registered before the agent existed
(`childkey`, the child path's proof), and the task it names says, under the
signature swarm-api made at submission, who submitted it and which slot it
runs with.

OFF UNLESS FORGE_BROKER IS ON (`ApiSettings.forge_broker`, terraform
`forge_broker`): off, the default and every environment's setting until the
owner turns it on, `release` answers 503 forge_credential_unavailable before
reading anything.

`ForgeCredentialBroker.release` checks, in the design's order, and reads no
secret before the last step:

  1. the ID token is this tenant's worker service account;
  2. the attempt proof verifies against the attested registration;
  3. the task exists under the token's tenant (else 404, as for no task);
  4. the tuple is the task's CURRENT attempt (invariant 5);
  5. the task's spec signature verifies, at format 3;
  6. the slot is a `git-u-` user slot;
  7. the slot is the submitter's, recomputed from the signed `submitted_by`
     and repository owner, never read from a document;
  8. the submitter's grant on the repository still exists, with write for a
     write;
  9. the slot's latest version, through `forge_tokens.read_slot`.

Checks 4 and 6-8 are pure (`is_current`, `decide_slot`), so the rule that keeps
Alice's token from Bob's attempt is testable without a route.

A SERVICE SUBMISSION'S RUN (DAYONE-SERVICE-APP-TOKEN; lesson 43). A task whose
SIGNED `submitted_by` is a service account (`validation.is_service_submitter`,
the rule submission's service path uses) never reaches checks 6-9: after
checks 1-5 it is released an App installation token minted for this one call
(`decide_run_token`, then `forge_installation.InstallationTokens.run_token`):
THIS tenant's installation of the signed repository's owner, that one
repository, `contents` and `pull_requests` write for a write and read for a
read, never held and never written to a slot. The answer names the task's
signed slot as `secret`, the name the worker checks it against. No
installation, a mint GitHub refuses, or no minter wired
(FORGE_INSTALLATION_READS off) is 403 `app_installation_unavailable`, and the
worker keeps today's service rule: it reads that slot itself. This path does
not wait for FORGE_BROKER, which is about a PERSON'S slot: it needs only the
minter and swarm-child-key (checks 1-5 are the attempt proof). With
FORGE_BROKER off a person's task is still refused 503, after check 5.

NEVER A VALUE. The token is returned in the 200's body and nowhere else: not
a log line, an event, a Firestore field, a response header or an error. Logs
carry the decision code, the task id and the secret's NAME.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Mapping

from pydantic import Field

from swarm_common.models import FORGE_CREDENTIAL
from swarm_common.states import TaskState

from . import verdictpublish
from .childkey import AttemptTuple
from .children import (
    _FORGE_SPEC_FORMAT,
    ChildService,
    ChildUnauthenticated,
    ChildUnproven,
    _AttemptFields,
    _lease_is_live,
    parse_body,
)
from .errors import ApiError, ValidationFailed
from .forge import NoForgeCredential
from .forge_installation import RunMintRefused
from .gittokens import Scope, owner_suffix, provider_suffix, secret_name_for
from .repositories import repo_id_for
from .store import LEASES, TASKS
from .validation import github_repository, is_service_submitter

log = logging.getLogger(__name__)

#: A person's slot: their App slot or a fallback slot for one owner, both
#: `git-u-` and 16 hex (`gittokens.provider_suffix`, `gittokens.owner_suffix`).
USER_SLOT = re.compile(r"^git-u-[0-9a-f]{16}$")

#: Only these states run an attempt that may push. A PARKED, READY or
#: finished task has no current attempt to release to.
LIVE_STATES = frozenset({TaskState.STARTING.value, TaskState.RUNNING.value})

#: The lowest spec format whose signature covers `forge_credential` and
#: `forge_access` beside `submitted_by` and `repository_url`
#: (`swarm_common.specsign._FORMAT_3_FIELDS`).
MIN_SPEC_FORMAT = _FORGE_SPEC_FORMAT

_WRITE = "write"
_READ = "read"


# --------------------------------------------------------------------------
# Refusals: one stable code each (§3.2). None carries a value.
# --------------------------------------------------------------------------


class ForgeCredentialUnavailable(ApiError):
    status_code = 503
    code = "forge_credential_unavailable"


class ForgeCredentialUnauthenticated(ApiError):
    status_code = 403
    code = "forge_credential_unauthenticated"


class ForgeCredentialUnproven(ApiError):
    status_code = 403
    code = "forge_credential_unproven"


class TaskNotFound(ApiError):
    status_code = 404
    code = "task_not_found"


class AttemptSuperseded(ApiError):
    status_code = 403
    code = "attempt_superseded"


class SpecUnverified(ApiError):
    status_code = 403
    code = "spec_unverified"


class CredentialNotUserSlot(ApiError):
    status_code = 403
    code = "credential_not_user_slot"


class CredentialNotSubmitters(ApiError):
    status_code = 403
    code = "credential_not_submitters"


class GrantRemoved(ApiError):
    status_code = 403
    code = "grant_removed"


class GrantReadOnly(ApiError):
    status_code = 403
    code = "grant_read_only"


class CredentialUnavailable(ApiError):
    status_code = 403
    code = "credential_unavailable"


class CredentialReadFailed(ApiError):
    status_code = 502
    code = "credential_read_failed"


class AppInstallationUnavailable(ApiError):
    """A service submission's run gets no installation token; the worker
    keeps today's service rule (`agent_worker` reads the task's slot)."""

    status_code = 403
    code = "app_installation_unavailable"


# --------------------------------------------------------------------------
# The body: the attempt tuple and the access it needs. No secret is named.
# --------------------------------------------------------------------------


class ForgeCredentialRequest(_AttemptFields):
    task_id: str = Field(min_length=1, max_length=128)
    access: Literal["read", "write"]

    def attempt(self) -> AttemptTuple:
        return AttemptTuple(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            lease_id=self.lease_id,
            generation=self.generation,
        )


@dataclass(frozen=True)
class Release:
    """What `decide_slot` allows: the slot's suffix and the grant it rests on."""

    suffix: str
    repo_id: str


@dataclass(frozen=True)
class Released:
    """The answer. `repr=False` on the token: a repr is what a traceback prints."""

    secret: str
    token: str = field(repr=False)


# --------------------------------------------------------------------------
# The pure checks
# --------------------------------------------------------------------------


def is_current(task: Mapping[str, Any] | None, lease: Mapping[str, Any] | None,
               attempt: AttemptTuple, now: datetime) -> bool:
    """Check 4: `attempt` is the task's live attempt, in a state that runs one.

    The child route's fence (`children._lease_is_live`) plus the state: a
    finished or parked task's last lease is not an attempt that may push.
    """
    if task is None or task.get("state") not in LIVE_STATES:
        return False
    return _lease_is_live(dict(task), dict(lease) if lease is not None else None, attempt, now)


def decide_slot(task: Mapping[str, Any], *, tenant_id: str, access: str,
                grant_mode: Callable[[str, str, str], str | None]) -> Release:
    """Checks 6-8 over a task whose signature already verified.

    The slot must be a `git-u-` user slot; it must equal the submitter's App
    slot or their fallback slot for the repository's owner, both RECOMPUTED
    from `submitted_by` and `repository_url`; and `grant_mode(tenant_id,
    submitted_by, repo_id)` must still answer a mode, `write` for a write. Raises the
    refusal; never reads a secret.
    """
    suffix = task.get("forge_credential")
    if not isinstance(suffix, str) or not USER_SLOT.fullmatch(suffix):
        raise CredentialNotUserSlot(
            "this task's forge credential is not a person's slot; a tenant or repository "
            "token is read by the worker directly"
        )
    submitted_by = task.get("submitted_by")
    if not isinstance(submitted_by, str) or not submitted_by.strip():
        raise CredentialNotSubmitters("this task names no submitter whose slot it could be")
    try:
        named = github_repository(task.get("repository_url"))
    except ValidationFailed:
        named = None
    owners = {provider_suffix(Scope.USER, user=submitted_by)}
    if named is not None:
        owners.add(owner_suffix(submitted_by, named[0]))
    if suffix not in owners:
        raise CredentialNotSubmitters(
            "this task's forge credential is not its submitter's; a person's slot is "
            "released only to the tasks they submitted"
        )
    if named is None:
        raise GrantRemoved("this task names no GitHub repository a grant could cover")
    repo_id = repo_id_for(tenant_id, named[0], named[1])
    mode = grant_mode(tenant_id, submitted_by, repo_id)
    if mode is None:
        raise GrantRemoved(
            f"the submitter's grant on {repo_id} has been removed since the task was submitted"
        )
    if access == _WRITE:
        if task.get("forge_access") != _WRITE:
            raise GrantReadOnly("this task was submitted for read access to its repository")
        if mode != _WRITE:
            raise GrantReadOnly(f"the submitter's grant on {repo_id} is now read-only")
    return Release(suffix=suffix, repo_id=repo_id)


@dataclass(frozen=True)
class RunRelease:
    """What `decide_run_token` allows: the slot the answer names, the one
    repository the token is minted for, and whether it may write."""

    suffix: str
    owner: str
    repo: str
    write: bool


def decide_run_token(task: Mapping[str, Any], *, access: str) -> RunRelease:
    """The service-submission path's checks over a task whose signature
    already verified: the signed submitter is a service account, the signed
    slot is a forge credential suffix, the signed repository is on GitHub,
    and a write was signed for write. Raises the refusal; mints nothing."""
    submitted_by = task.get("submitted_by")
    if not isinstance(submitted_by, str) or not is_service_submitter(submitted_by):
        raise CredentialNotSubmitters(
            "this task was not submitted by a service account; an installation token is "
            "minted only for a service submission's run")
    suffix = task.get("forge_credential")
    if not isinstance(suffix, str) or not FORGE_CREDENTIAL.fullmatch(suffix):
        raise AppInstallationUnavailable(
            "this task's forge credential is not a forge credential suffix")
    try:
        named = github_repository(task.get("repository_url"))
    except ValidationFailed:
        named = None
    if named is None:
        raise AppInstallationUnavailable(
            "this task names no GitHub repository an installation token could cover")
    if access == _WRITE and task.get("forge_access") != _WRITE:
        raise GrantReadOnly("this task was submitted for read access to its repository")
    return RunRelease(suffix=suffix, owner=named[0], repo=named[1], write=access == _WRITE)


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class ForgeCredentialBroker:
    """`POST /v1/attempts/forge-credential`'s logic. Built per request.

    `children` authenticates the worker and verifies the attempt proof, the
    child route's own checks, reused; `grant_mode` is
    `SubmissionService._grant_mode`, the check submission resolved the slot
    with; `tokens` is the context's `forge_tokens` (`SecretManagerForgeTokens`);
    `installations` the context's `forge_installations`
    (`forge_installation.InstallationTokens`), which mints a service
    submission's run token, or None when FORGE_INSTALLATION_READS is off.
    """

    def __init__(self, *, settings: Any, db: Any, store: Any, children: ChildService,
                 grant_mode: Callable[[str, str, str], str | None], tokens: Any,
                 now: Callable[[], datetime], installations: Any = None) -> None:
        self._settings = settings
        self._db = db
        self._store = store
        self._children = children
        self._grant_mode = grant_mode
        self._tokens = tokens
        self._now = now
        self._installations = installations

    def _mints_run_tokens(self) -> bool:
        return callable(getattr(self._installations, "run_token", None))

    def _refuse(self, error: ApiError, body: ForgeCredentialRequest | None,
                secret: str | None = None) -> ApiError:
        log.info("forge credential refused decision=%s tenant=%s task=%s secret=%s",
                 error.code, body.tenant_id if body else "-", body.task_id if body else "-",
                 secret or "-")
        return error

    def release(self, authorization: str | None, raw: bytes, *, path: str,
                proof: str | None, timestamp: str | None) -> Released:
        broker_on = getattr(self._settings, "forge_broker", False) is True
        if not broker_on and not self._mints_run_tokens():
            raise self._refuse(ForgeCredentialUnavailable(
                "the forge broker is switched off here (FORGE_BROKER), so it releases no "
                "person's slot; workers read their own"), None)
        if not self._children._keys.configured:
            raise self._refuse(ForgeCredentialUnavailable(
                "this deployment has no swarm-child-key, so it releases no person's slot"), None)
        body: ForgeCredentialRequest = parse_body(ForgeCredentialRequest, raw)
        attempt = body.attempt()

        # 1 and 2: the tenant's worker, then the attempt's own key.
        try:
            self._children.authenticate_worker(authorization, body.tenant_id)
        except ChildUnauthenticated:
            raise self._refuse(ForgeCredentialUnauthenticated(
                "only this tenant's worker service account may ask; a person has no "
                "attempt"), body) from None
        try:
            self._children.verify_proof(attempt, method="POST", path=path, body=raw,
                                        proof=proof, timestamp=timestamp)
        except ChildUnproven:
            raise self._refuse(ForgeCredentialUnproven(
                "the attempt proof did not verify for this attempt"), body) from None

        # 3: by id, under the token's tenant only (invariant 9).
        snap = self._db.collection(TASKS).document(body.task_id).get()
        task = (snap.to_dict() or {}) if snap.exists else None
        if task is None or task.get("tenant_id") != body.tenant_id:
            raise self._refuse(TaskNotFound("no such task in this tenant"), body)

        # 4: the current attempt, or nothing (invariant 5).
        lease_snap = self._db.collection(LEASES).document(body.lease_id).get()
        lease = (lease_snap.to_dict() or {}) if lease_snap.exists else None
        if not is_current(task, lease, attempt, self._now()):
            raise self._refuse(AttemptSuperseded(
                "this attempt is not the task's current attempt; a superseded worker "
                "receives nothing"), body)

        # 5: `submitted_by`, `repository_url` and `forge_credential` are
        # trusted only under the signature made at submission.
        refusal = verdictpublish.spec_refusal(self._settings, task, body.task_id)
        spec_format = task.get("spec_format")
        if refusal is None and (isinstance(spec_format, bool) or not isinstance(spec_format, int)
                                or spec_format < MIN_SPEC_FORMAT):
            refusal = "the step's spec is signed at a format that does not cover its forge fields"
        if refusal is not None:
            raise self._refuse(SpecUnverified(refusal), body)

        # A service submission's run: an installation token, never a slot.
        if is_service_submitter(task.get("submitted_by")):
            return self._release_run_token(task, body)
        if not broker_on:
            raise self._refuse(ForgeCredentialUnavailable(
                "the forge broker is switched off here (FORGE_BROKER), so it releases no "
                "person's slot; workers read their own"), body)

        # 6-8: the submitter's own slot, under a grant that still holds.
        try:
            release = decide_slot(task, tenant_id=body.tenant_id, access=body.access,
                                  grant_mode=self._grant_mode)
        except ApiError as exc:
            suffix = task.get("forge_credential")
            named = (secret_name_for(body.tenant_id, suffix)
                     if isinstance(suffix, str) and USER_SLOT.fullmatch(suffix) else None)
            raise self._refuse(exc, body, named) from None

        # 9: the latest version, through the reader swarm-api already has.
        secret = secret_name_for(body.tenant_id, release.suffix)
        tenant = self._store.get_tenant(body.tenant_id)
        if tenant is None:
            raise self._refuse(CredentialUnavailable(
                f"tenant {body.tenant_id!r} is not registered"), body, secret)
        try:
            value = self._tokens.read_slot(tenant, release.suffix)
        except NoForgeCredential:
            raise self._refuse(CredentialUnavailable(
                f"{secret} holds no enabled version; its owner must connect GitHub again"),
                body, secret) from None
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            raise self._refuse(CredentialReadFailed(
                f"{secret} could not be read ({code})"), body, secret) from None
        log.info("forge credential released decision=released tenant=%s task=%s "
                 "generation=%s access=%s secret=%s", body.tenant_id, body.task_id,
                 body.generation, body.access, secret)
        return Released(secret=secret, token=value.value)

    def _release_run_token(self, task: Mapping[str, Any],
                           body: ForgeCredentialRequest) -> Released:
        """`decide_run_token`, then one fresh mint. The token is in the
        answer only; the log names the decision, the repository and the
        slot's NAME, never a value."""
        try:
            release = decide_run_token(task, access=body.access)
        except ApiError as exc:
            raise self._refuse(exc, body) from None
        secret = secret_name_for(body.tenant_id, release.suffix)
        repository = f"{release.owner}/{release.repo}"
        if not self._mints_run_tokens():
            raise self._refuse(AppInstallationUnavailable(
                "this deployment mints no installation tokens (FORGE_INSTALLATION_READS is "
                "off); the run keeps its slot"), body, secret)
        try:
            token = self._installations.run_token(
                body.tenant_id, release.owner, release.repo, write=release.write)
        except RunMintRefused as exc:
            raise self._refuse(AppInstallationUnavailable(
                f"no installation token for {repository} ({exc.reason}); the run keeps "
                "its slot"), body, secret) from None
        except Exception as exc:  # noqa: BLE001 -- the type only, never a message
            raise self._refuse(AppInstallationUnavailable(
                f"no installation token for {repository} ({type(exc).__name__}); the run "
                "keeps its slot"), body, secret) from None
        if not isinstance(token, str) or not token:
            raise self._refuse(AppInstallationUnavailable(
                f"no installation token for {repository}; the run keeps its slot"),
                body, secret)
        log.info("forge credential released decision=app_installation tenant=%s task=%s "
                 "generation=%s access=%s repository=%s secret=%s", body.tenant_id,
                 body.task_id, body.generation, body.access, repository, secret)
        return Released(secret=secret, token=token)
