"""GitHub App installation tokens for swarm-api's background reads (#1483, QUOTA-INSTALL).

WHY. On 2026-10-17 every GitHub call SwarmCloud made for the eng and
u-bogdan workspaces went out as one person: both workspaces connect through
the App's USER token for bogdan-alexandrescu (`method: app_user`), and the
`-git` slot `ForgeTokens.token_for` reads is that token. The merge wake's CI
reads alone (mergewake.py, one park every `issueci.CI_READ_SECONDS`) cost
~3,450 calls an hour, the stranded-PR sweep ~200-400 more; the person's
5,000/h ran out, push tests read as missing permissions and ten lane
attempts failed at publish. An INSTALLATION token is the App acting as
itself, on its own per-installation quota, so a background read spends
nothing of a person's.

WHAT THIS IS. `InstallationTokens.token_for_repo(tenant, owner, repo)`:

  1. the owner's installation, from the tenant's OWN `forge_orgs` documents
     (access.py writes one per enabled owner: `install_state: installed`
     with the `installation_id` GitHub listed). An owner reached through a
     person's fallback token (`method: pat`, no installation) has none, and
     the answer is None: the caller keeps today's token. Another tenant's
     enabled owner is never this tenant's installation (invariant 9).
  2. `POST /app/installations/{id}/access_tokens`, with the App JWT
     `ForgeApp.signed_app_jwt` signs, scoped to the ONE repository being
     read (`repositories`) and to READ permissions only (`READ_PERMISSIONS`):
     the token can read that repository's pulls, checks and statuses and
     can write nothing.
  3. held in memory until `REFRESH_BEFORE` before GitHub's `expires_at`
     (tokens live an hour), so a park read every 30 s mints about once an
     hour per repository.

A MINT THAT FAILS falls back: the answer is None, the caller reads with
today's token, the failure is logged ONCE per repository (its type and the
HTTP status, never a value) until a mint succeeds again, and the repository
is not minted again for `FAILURE_HOLD`, so an App missing a permission is
not asked every 30 s.

WHAT NEVER LEAVES. The JWT and the minted token are registered with the
redaction filter while in hand; neither is logged, persisted, put in an
exception's text or returned anywhere but to the caller that reads with it.

WRITES STAY THE PERSON'S, with ONE exception. Nothing here is used to open a
pull request, comment, re-run a job, merge, or read as the person: only the
background reads that switched (mergewake `_read`, issueci's CI read, the
stranded-PR sweep, the repository index's head poll) ask for one.

THE EXCEPTION: A WORKFLOW-FILE PUSH (#2029, WF-APP-TOKEN; BGD, 2026-10-22).
`workflow_push_token(tenant, owner, repo)` mints the token gittools'
`git_push_branch` pushes a range touching `.github/workflows/**` or
`.github/actions/**` with, and only for a registration whose `workflow_files`
switch is on (the caller checks). The same installation lookup
(`installation_for`, the tenant's OWN `forge_orgs`, never a `method: pat`
owner), ONE repository, and exactly `WORKFLOW_PUSH_PERMISSIONS` --
`contents: write` and `workflows: write`, nothing else. It is minted fresh
for every push and NEVER held: it does not enter `_held`, so the read cache
can never hand it out, and the read mint asks for `READ_PERMISSIONS` only.
A failure is a typed `WorkflowMintRefused` (`MINT_*`), constant text, never
GitHub's message or the transport's.

THE SECOND EXCEPTION: A GITHUB ACTIONS SECRET (SSC-ACTIONS-API, part of
#2158; docs/design/secrets-scopes.md §3, BGD's Q3 and Q4 of its §11).
`actions_token(tenant, owner, repo, use)` mints the token
`actions_secrets.py` writes one Actions secret with, or reads one
environment's branch rule with. No second App: the same installation lookup,
ONE repository, and exactly one entry of `ACTIONS_PERMISSIONS` -- the one
permission that GitHub call needs (`secrets: write` for a repository secret,
`environments: write` for an environment's, `actions: read` for an
environment's rule, read levels for the name lists). Never `administration`:
SwarmCloud creates, edits and deletes no environment until user-scoped-
secrets step 5. Minted fresh, never held, as the workflow push token is. An
installation that has not granted the permission is a typed
`ActionsMintRefused` carrying the installation's account, so the caller can
answer with `forgeperms.review_url`; nothing is written.

WHOSE QUOTA (QUOTA-THROTTLE). `read_credential` answers the token AND the
identity its quota is kept under in `forge_limits.QUOTAS`,
`installation:<id>`: the caller reads inside `forge_limits.acting_as(that)`
so every answer updates the installation's quota, and asks
`QUOTAS.defer(that)` before a background read. A mint's own answer is the
App JWT's quota (`forge_limits.APP_CREDENTIAL`).

`FORGE_INSTALLATION_READS` (settings `forge_installation_reads`, ON by
default) turns the switch off: off, `build_context` wires no source and every
read is today's.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from google.cloud.firestore_v1.base_query import FieldFilter

from . import forge_limits
from .gittokens import redaction_literal

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS_ENV_READ", "APP_INSTALLATION_SOURCE", "ACTIONS_ENV_WRITE", "ACTIONS_PERMISSIONS", "ACTIONS_REPO_READ",
    "ACTIONS_REPO_WRITE", "ACTIONS_RULE_READ", "ActionsMintRefused", "ActionsToken",
    "FAILURE_HOLD", "InstallationSource", "InstallationTokens", "MINT_FAILED",
    "MINT_NO_INSTALLATION", "MINT_NOT_COVERED", "MINT_NOT_GRANTED", "MINT_RUN_NOT_GRANTED",
    "MintRefused", "READ_PERMISSIONS", "RUN_READ_PERMISSIONS", "RUN_WRITE_PERMISSIONS",
    "RunMintRefused",
    "REFRESH_BEFORE", "WORKFLOW_PUSH_PERMISSIONS", "WorkflowMintRefused",
    "build_installation_tokens", "installation_record_in", "read_credential", "read_token",
]

#: `metadata.forge_credential_source` on a service submission's task whose
#: repository's owner has the App installed (`service._resolve_forge`): the
#: broker mints its run a token (`InstallationTokens.run_token`). A label the
#: broker never trusts: it recomputes the decision from the signed fields.
APP_INSTALLATION_SOURCE = "app_installation"
#: The collection access.py writes an enabled owner to (forgeapp.ORGS).
ORGS = "forge_orgs"
#: access.INSTALLED: an owner the App is installed on and the person enabled.
INSTALLED = "installed"
#: forgeapp.METHOD_PAT: an owner reached through a person's token, not the App.
METHOD_PAT = "pat"
#: A tenant holds a handful of enabled owners; more than this is not read.
MAX_ORG_DOCS = 200

API = "https://api.github.com"
#: What a background read needs and nothing more: the pull request, its
#: base branch's rules (metadata), check runs, commit statuses and the
#: default branch's head (contents). GitHub refuses a mint (422) that asks
#: for a permission the installation did not grant; that is a fallback.
READ_PERMISSIONS: Mapping[str, str] = {
    "metadata": "read",
    "contents": "read",
    "pull_requests": "read",
    "checks": "read",
    "statuses": "read",
}
#: What a workflow-file push needs and nothing more (#2029): the push itself
#: (contents) and GitHub's extra demand on a range that touches a workflow
#: file (workflows). Never held, never served to a read.
WORKFLOW_PUSH_PERMISSIONS: Mapping[str, str] = {
    "contents": "write",
    "workflows": "write",
}
#: What a service submission's run clones, pushes and opens its pull request
#: with (DAYONE-SERVICE-APP-TOKEN; lesson 43): its repository's contents and
#: its pull requests, nothing else (`metadata: read` comes with any token).
#: Never `workflows`: a run's push that touches a workflow file still goes
#: through gittools' own mint. Never held, as the workflow push token is not.
RUN_WRITE_PERMISSIONS: Mapping[str, str] = {
    "contents": "write",
    "pull_requests": "write",
}
#: The same run asking only to read (a fetch, a clone).
RUN_READ_PERMISSIONS: Mapping[str, str] = {
    "contents": "read",
    "pull_requests": "read",
}

#: The GitHub calls an Actions secret mint may be for, each with the ONE
#: permission it needs and nothing more (SSC-ACTIONS-API; GitHub's
#: "Permissions required for GitHub Apps", read 2026-10-24): the repository
#: secrets' public key and PUT/DELETE are Secrets; an environment secret's
#: public key and PUT/DELETE are Environments: write, NOT Secrets; an
#: environment and its deployment branch policies are read under Actions.
#: No entry asks `administration`, and none is ever held (Q4).
ACTIONS_REPO_WRITE = "repository_secret_write"
ACTIONS_ENV_WRITE = "environment_secret_write"
ACTIONS_REPO_READ = "repository_secret_names"
ACTIONS_ENV_READ = "environment_secret_names"
ACTIONS_RULE_READ = "environment_rule"
ACTIONS_PERMISSIONS: Mapping[str, Mapping[str, str]] = {
    ACTIONS_REPO_WRITE: {"secrets": "write"},
    ACTIONS_ENV_WRITE: {"environments": "write"},
    ACTIONS_REPO_READ: {"secrets": "read"},
    ACTIONS_ENV_READ: {"environments": "read"},
    ACTIONS_RULE_READ: {"actions": "read"},
}
#: Why a workflow push token was not minted (`WorkflowMintRefused.reason`).
#: The tenant has no App installation of the owner (or reaches it only
#: through a person's token).
MINT_NO_INSTALLATION = "no_installation"
#: GitHub refused the permissions: the installation has not accepted
#: "Workflows: Read and write" (BGD's decision 4, accepted per installation).
MINT_NOT_GRANTED = "workflows_not_granted"
#: GitHub refused the repository: the installation does not cover it.
MINT_NOT_COVERED = "repository_not_covered"
#: Anything else: no JWT, no answer, another status, an unreadable answer.
MINT_FAILED = "mint_failed"
#: GitHub refused a run token's permissions: the installation has not
#: accepted "Contents" or "Pull requests: Read and write".
MINT_RUN_NOT_GRANTED = "run_permissions_not_granted"
#: A token is used until this long before GitHub's `expires_at` (tokens
#: live an hour), so none is sent that could expire mid-read.
REFRESH_BEFORE = 300.0
#: After a failed mint the repository is read with today's token for this
#: long before the App is asked again.
FAILURE_HOLD = 300.0
TIMEOUT_SECONDS = 10.0


class InstallationSource(Protocol):
    def token_for_repo(self, tenant_id: str, owner: str, repo: str) -> str | None: ...


@dataclass
class _Held:
    token: str = field(repr=False)
    use_until: float


class _MintFailed(Exception):
    """A mint that produced no token. Constant text: a kind and a status.
    `refused` is what a 403/422 refused (`MINT_NOT_GRANTED`,
    `MINT_NOT_COVERED`), read from GitHub's message and never quoting it."""

    def __init__(self, kind: str, status: int | None = None,
                 refused: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.refused = refused


@dataclass(frozen=True)
class _Installation:
    """One enabled owner's installation, as the tenant's `forge_orgs` says."""

    installation_id: int
    account: str
    account_type: str | None


class MintRefused(Exception):
    """No token. `reason` is one of the `MINT_*` words; the text is the
    reason and a status, never a value or GitHub's message. `installation`,
    when one was found, is what `forgeperms.review_url` is built from."""

    def __init__(self, reason: str, status: int | None = None, *,
                 installation: _Installation | None = None) -> None:
        super().__init__(reason if status is None else f"{reason} (HTTP {status})")
        self.reason = reason
        self.status = status
        self.installation = installation


class WorkflowMintRefused(MintRefused):
    """No workflow push token (#2029)."""


class RunMintRefused(MintRefused):
    """No run token for a service submission (DAYONE-SERVICE-APP-TOKEN)."""


class ActionsMintRefused(MintRefused):
    """No GitHub Actions secret token (SSC-ACTIONS-API). A permission the
    installation has not granted is `<permission>_not_granted`, e.g.
    `secrets_not_granted`."""


@dataclass(frozen=True)
class ActionsToken:
    """One minted Actions token and the installation it acts as."""

    token: str = field(repr=False)
    installation: _Installation
    permissions: Mapping[str, str]


def _refusal_kind(answer: Any) -> str | None:
    """What a 403/422 mint answer refused, from GitHub's message: the
    repositories (`MINT_NOT_COVERED`) or the permissions (`MINT_NOT_GRANTED`,
    also the answer when the message names neither)."""
    try:
        data = answer.json()
    except Exception:
        data = None
    message = str(data.get("message") or "").lower() if isinstance(data, dict) else ""
    if "repositor" in message and "permission" not in message:
        return MINT_NOT_COVERED
    return MINT_NOT_GRANTED


def _expiry(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class InstallationTokens:
    """Mints and holds one read-only installation token per (installation, repository)."""

    def __init__(
        self,
        db: Any,
        *,
        app_jwt: Callable[[], str],
        send: Callable[..., Any],
        clock: Callable[[], float] = time.time,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._db = db
        self._app_jwt = app_jwt
        self._send = send
        self._clock = clock
        self._timeout = timeout
        self._held: dict[tuple[int, str], _Held] = {}
        self._failed_until: dict[tuple[str, str], float] = {}
        self._logged: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    # -- which installation --------------------------------------------------

    def installation_for(self, tenant_id: str, owner: str) -> int | None:
        """The installation of `owner` THIS tenant enabled, or None."""
        found = self.installation_record(tenant_id, owner)
        return found.installation_id if found is not None else None

    def installation_record(self, tenant_id: str, owner: str) -> _Installation | None:
        """`installation_for`, with the account it is installed on."""
        return installation_record_in(self._db, tenant_id, owner)

    # -- the token -----------------------------------------------------------

    def token_for_repo(self, tenant_id: str, owner: str, repo: str) -> str | None:
        """A read-only token for `owner/repo` as the App, or None: no
        installation for the owner, or the mint failed (logged once)."""
        found = self.credential_for_repo(tenant_id, owner, repo)
        return found[0] if found is not None else None

    def credential_for_repo(self, tenant_id: str, owner: str,
                            repo: str) -> tuple[str, int] | None:
        """`token_for_repo`'s token and the installation it acts as, or None."""
        key = (owner.lower(), repo.lower())
        now = self._clock()
        with self._lock:
            if self._failed_until.get(key, 0.0) > now:
                return None
        try:
            installation = self.installation_for(tenant_id, owner)
        except Exception as exc:
            self._failed(key, _MintFailed(f"installation lookup {type(exc).__name__}"), now)
            return None
        if installation is None:
            return None
        slot = (installation, key[1])
        with self._lock:
            held = self._held.get(slot)
            if held is not None and held.use_until > now:
                return held.token, installation
        try:
            token, expires = self._mint(installation, repo)
        except _MintFailed as failed:
            self._failed(key, failed, now)
            return None
        except Exception as exc:
            self._failed(key, _MintFailed(type(exc).__name__), now)
            return None
        with self._lock:
            self._held[slot] = _Held(token=token, use_until=expires - REFRESH_BEFORE)
            self._failed_until.pop(key, None)
            recovered = key in self._logged
            self._logged.discard(key)
        if recovered:
            log.info("installation token minted again owner=%s repo=%s installation=%s",
                     key[0], key[1], installation)
        return token, installation

    def workflow_push_token(self, tenant_id: str, owner: str, repo: str) -> str:
        """A token for ONE push to `owner/repo` that may touch workflow files,
        minted now from THIS tenant's installation of `owner` with exactly
        `WORKFLOW_PUSH_PERMISSIONS`. Never held: every call mints, and nothing
        here keeps the value once it is returned. Raises `WorkflowMintRefused`."""
        token, _installation = self._mint_now(
            tenant_id, owner, repo, WORKFLOW_PUSH_PERMISSIONS, what="workflow push token",
            refused=WorkflowMintRefused, not_granted=lambda: MINT_NOT_GRANTED)
        return token

    def actions_token(self, tenant_id: str, owner: str, repo: str, use: str) -> ActionsToken:
        """A token for ONE GitHub Actions secret call on `owner/repo`, minted
        now from THIS tenant's installation of `owner` with exactly
        `ACTIONS_PERMISSIONS[use]`: one repository, one permission. Never
        held. Raises `ActionsMintRefused`; `KeyError` for a `use` not in the
        table, so no caller can ask for anything else."""
        permissions = ACTIONS_PERMISSIONS[use]
        token, installation = self._mint_now(
            tenant_id, owner, repo, permissions, what="actions secret token",
            refused=ActionsMintRefused,
            not_granted=lambda: f"{next(iter(permissions))}_not_granted")
        return ActionsToken(token=token, installation=installation, permissions=permissions)

    def run_token(self, tenant_id: str, owner: str, repo: str, *, write: bool) -> str:
        """A token for ONE run of a service submission on `owner/repo`
        (DAYONE-SERVICE-APP-TOKEN), minted now from THIS tenant's installation
        of `owner`: one repository, exactly `RUN_WRITE_PERMISSIONS` for a write
        and `RUN_READ_PERMISSIONS` for a read. Never held: every call mints,
        and nothing here keeps the value once it is returned. Raises
        `RunMintRefused`."""
        token, _installation = self._mint_now(
            tenant_id, owner, repo, RUN_WRITE_PERMISSIONS if write else RUN_READ_PERMISSIONS,
            what="run token", refused=RunMintRefused,
            not_granted=lambda: MINT_RUN_NOT_GRANTED)
        return token

    def _mint_now(self, tenant_id: str, owner: str, repo: str,
                  permissions: Mapping[str, str], *, what: str,
                  refused: type[MintRefused],
                  not_granted: Callable[[], str]) -> tuple[str, _Installation]:
        """One fresh, unheld mint for one repository, or `refused`."""
        try:
            installation = self.installation_record(tenant_id, owner)
        except Exception as exc:
            log.warning("%s not minted owner=%s repo=%s (installation lookup %s)",
                        what, owner.lower(), repo.lower(), type(exc).__name__)
            raise refused(MINT_FAILED) from None
        if installation is None:
            log.info("%s not minted owner=%s repo=%s (%s)",
                     what, owner.lower(), repo.lower(), MINT_NO_INSTALLATION)
            raise refused(MINT_NO_INSTALLATION)
        inst = installation.installation_id
        try:
            token, _expires = self._mint(inst, repo, permissions)
        except _MintFailed as failed:
            reason = failed.refused or MINT_FAILED
            if reason == MINT_NOT_GRANTED:
                reason = not_granted()
            log.warning("%s not minted owner=%s repo=%s installation=%s (%s%s)",
                        what, owner.lower(), repo.lower(), inst, reason,
                        "" if failed.status is None else f" HTTP {failed.status}")
            raise refused(reason, failed.status, installation=installation) from None
        except Exception as exc:
            log.warning("%s not minted owner=%s repo=%s installation=%s (%s)",
                        what, owner.lower(), repo.lower(), inst, type(exc).__name__)
            raise refused(MINT_FAILED, installation=installation) from None
        log.info("%s minted owner=%s repo=%s installation=%s",
                 what, owner.lower(), repo.lower(), inst)
        return token, installation

    def _failed(self, key: tuple[str, str], failed: _MintFailed, now: float) -> None:
        with self._lock:
            self._failed_until[key] = now + FAILURE_HOLD
            first = key not in self._logged
            self._logged.add(key)
        if first:
            log.warning("installation token not minted owner=%s repo=%s (%s%s); "
                        "reading with the tenant's token", key[0], key[1], failed.kind,
                        "" if failed.status is None else f" HTTP {failed.status}")

    def _mint(self, installation: int, repo: str,
              permissions: Mapping[str, str] = READ_PERMISSIONS) -> tuple[str, float]:
        try:
            jwt = self._app_jwt()
        except Exception as exc:
            raise _MintFailed(f"app jwt {type(exc).__name__}") from None
        with redaction_literal(jwt):
            headers = {"Accept": "application/vnd.github+json",
                       "Authorization": f"Bearer {jwt}",
                       "Content-Type": "application/json",
                       "User-Agent": "swarmcloud-swarm-api",
                       "X-GitHub-Api-Version": "2022-11-28"}
            body = json.dumps({"repositories": [repo],
                               "permissions": dict(permissions)}).encode("utf-8")
            try:
                answer = self._send("POST", f"{API}/app/installations/{int(installation)}"
                                    "/access_tokens", headers, body, self._timeout)
            except Exception as exc:
                # The type only: a transport's message can quote the request.
                raise _MintFailed(f"no answer {type(exc).__name__}") from None
            finally:
                headers.clear()
                jwt = ""
        # The mint spends the App JWT's quota, not the installation's.
        forge_limits.QUOTAS.observe_answer(answer, credential=forge_limits.APP_CREDENTIAL)
        status = getattr(answer, "status", None)
        if status != 201:
            raise _MintFailed("refused", status if isinstance(status, int) else None,
                              _refusal_kind(answer) if status in (403, 422) else None)
        try:
            data = answer.json()
        except Exception:
            data = None
        token = data.get("token") if isinstance(data, dict) else None
        expires = _expiry(data.get("expires_at")) if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or expires is None:
            raise _MintFailed("unreadable answer", status)
        return token, expires


def installation_record_in(db: Any, tenant_id: str, owner: str) -> _Installation | None:
    """The installation of `owner` THIS tenant enabled, from its own
    `forge_orgs` documents, or None: an owner reached through a person's
    token (`method: pat`) or not installed has none. Reads Firestore only,
    so submission can ask it without the App's key (`run_token` mints)."""
    wanted = owner.lower()
    query = (db.collection(ORGS)
             .where(filter=FieldFilter("tenant_id", "==", tenant_id))
             .limit(MAX_ORG_DOCS))
    for snap in query.stream():
        doc = snap.to_dict() or {}
        # The query filtered on it; asked again (invariant 9).
        if doc.get("tenant_id") != tenant_id or str(doc.get("owner") or "").lower() != wanted:
            continue
        if doc.get("method") == METHOD_PAT or doc.get("install_state") != INSTALLED:
            continue
        installation = doc.get("installation_id")
        if isinstance(installation, int) and not isinstance(installation, bool) \
                and installation > 0:
            kind = doc.get("owner_type")
            return _Installation(installation, str(doc.get("owner") or owner),
                                 kind if isinstance(kind, str) else None)
    return None


def read_token(source: InstallationSource | None, tenant_id: str, owner: str,
               repo: str) -> str:
    """The installation token a background read of `owner/repo` uses, or ""
    when there is none and the caller keeps today's token. Never raises."""
    if source is None or not owner or not repo:
        return ""
    try:
        return source.token_for_repo(tenant_id, owner, repo) or ""
    except Exception as exc:
        log.warning("installation token source failed owner=%s repo=%s (%s)",
                    owner.lower(), repo.lower(), type(exc).__name__)
        return ""


def read_credential(source: InstallationSource | None, tenant_id: str, owner: str,
                    repo: str) -> tuple[str, str]:
    """`read_token`, with the identity its quota is kept under: ("", "")
    when there is no installation token. A source that cannot name its
    installation (`token_for_repo` only) is keyed by the owner it serves."""
    if source is None or not owner or not repo:
        return "", ""
    named = getattr(source, "credential_for_repo", None)
    try:
        if callable(named):
            found = named(tenant_id, owner, repo)
            if found is None or not found[0]:
                return "", ""
            return found[0], forge_limits.installation_credential(int(found[1]))
        token = source.token_for_repo(tenant_id, owner, repo) or ""
    except Exception as exc:
        log.warning("installation token source failed owner=%s repo=%s (%s)",
                    owner.lower(), repo.lower(), type(exc).__name__)
        return "", ""
    return (token, forge_limits.installation_credential(f"owner/{owner.lower()}")) if token \
        else ("", "")


def build_installation_tokens(db: Any, project_id: str, *,
                              now: Callable[[], datetime] | None = None) -> InstallationTokens:
    """Production wiring over the App `forgeapp.build_forge_app` describes:
    its JWT (`signed_app_jwt`, the private key swarm-api already reads for the
    key self-check and forgeperms, `github_app_accessor`) and its transport
    (urllib, github.com hosts only, no redirects). Builds no client."""
    from swarm_common.models import utcnow

    from .forgeapp import build_forge_app

    app = build_forge_app(db, project_id, now=now or utcnow)
    return InstallationTokens(db, app_jwt=app.signed_app_jwt, send=app.send)
