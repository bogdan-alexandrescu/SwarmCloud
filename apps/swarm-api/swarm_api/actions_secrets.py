"""GitHub Actions secrets, written write-only (SSC-ACTIONS-API, part of #2158).

Design: docs/design/secrets-scopes.md §3, §6.2, §6.3, with BGD's answers of
its §11 (2026-10-23), which override §3 where they differ:

  * Q3, NO SECOND APP. Every GitHub call here is made with an installation
    token of the EXISTING SwarmCloud App, minted by
    `forge_installation.InstallationTokens.actions_token` for ONE repository
    and the ONE permission that call needs (`ACTIONS_PERMISSIONS`), never
    held, never logged, registered with the redaction filter while in hand.
    An installation that has not granted the permission is refused with its
    reason and `forgeperms.review_url`, before anything is written.
  * Q4, NO ENVIRONMENT WRITES. Nothing here creates, edits or deletes an
    environment, and no mint asks `administration`. `rule` reads whether an
    existing environment lets only the repository's default branch deploy
    ("main only"); a missing one is answered with the GitHub page where a
    person creates it. A secret is written into an environment whose rule is
    not main-only ONLY when the caller types that environment's name, because
    a secret there reaches the workflows of every branch anyone with push
    access creates.
  * Q5, SEALED HERE. The value is sealed with PyNaCl (`nacl.public.SealedBox`,
    libsodium's `crypto_box_seal`) to the public key GitHub serves for the
    repository or the environment, PUT with that key's id, and dropped. A key
    GitHub rotated between the read and the PUT is read again and sealed
    again once; a second rotation is 409 `actions_key_rotated`.
  * Q6, WHO. A workspace manager (`workspace_secrets.require_manager`) AND a
    person GitHub calls admin of that repository, asked live with their own
    token (`github_repo_admin.require_repo_admin`). Reads need only the
    workspace and the registration.
  * Q8, HOW THE VALUE ARRIVES. In one request body, from the console's
    one-time link form (`mint_link`: ten minutes, one use, bound to the
    caller, the workspace, the repository, the environment and the name) or
    from a terminal reading `--stdin`. A link grants nothing: the write is
    authorised again when it is submitted.

WHAT SWARMCLOUD KEEPS (§3.8). Names and timestamps: one
`actions_secret_writes` document per destination (repository, environment,
name) with who set it, when, GitHub's status and `set`/`deleted`, and one
`admin_audit` row per write in the same transaction. NO response, log line,
Firestore document or error carries a value, a ciphertext or a token. The
value is a local of `ActionsSecrets.set`; GitHub's error text is never
repeated (a status and a constant reason only).

INVARIANTS. 1: nothing here creates infrastructure demand. 9: the
repository is a registration OF THE CALLER'S WORKSPACE (another's is 404),
and the installation is the one THAT workspace enabled. 10: callers name a
repository by its registration, a secret and an environment by GitHub's
names; nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets as _random
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

from google.cloud import firestore

from swarm_common.admission import _snapshot

from . import forge_limits
from .admins import AUDIT_COLLECTION
from .codec import console_origin
from .errors import Conflict, NotFound, UpstreamUnavailable, ValidationFailed
from .forge_installation import (
    ACTIONS_ENV_READ,
    ACTIONS_ENV_WRITE,
    ACTIONS_REPO_READ,
    ACTIONS_REPO_WRITE,
    ACTIONS_RULE_READ,
    MINT_FAILED,
    MINT_NO_INSTALLATION,
    MINT_NOT_COVERED,
    ActionsMintRefused,
    ActionsToken,
)
from .forgeapp import HttpAnswer, HttpSend
from .forgeperms import ACTIONS_SECRETS_REQUIREMENTS, review_url
from .gittokens import redaction_literal
from .workspace_secrets import SecretLinkInvalid

log = logging.getLogger(__name__)

#: One document per destination (repository or environment, and name). Never a value.
WRITES_COLLECTION = "actions_secret_writes"
#: One document per minted form link, keyed by sha256(token). Never the token.
LINKS_COLLECTION = "actions_secret_links"
LINK_TTL = timedelta(minutes=10)

AUDIT_SET = "actions_secret_set"
AUDIT_DELETED = "actions_secret_deleted"

SET = "set"
DELETED = "deleted"

#: The rule an existing environment has (§3.4), as the console shows it.
RULE_MAIN_ONLY = "main_only"
RULE_PROTECTED = "protected_branches"
RULE_CUSTOM = "custom_branches"
RULE_ANY = "any_branch"

#: Why a write or a read was refused: `detail.reason`, under established
#: codes (409 `conflict`, 422 `validation_failed`, 503), so no new code ships.
APP_NOT_INSTALLED = "actions_app_not_installed"
NOT_COVERED = "actions_repository_not_covered"
ENVIRONMENT_MISSING = "actions_environment_missing"
NOT_MAIN_ONLY = "actions_environment_not_main_only"
KEY_ROTATED = "actions_key_rotated"
GITHUB_REFUSED = "actions_github_refused"

#: GitHub's rules for a secret's name (§3.9): letters, digits and `_`, not
#: led by a digit, not `GITHUB_`-prefixed (compared without case, as GitHub does).
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,199}")
RESERVED_PREFIX = "GITHUB_"
MAX_ENVIRONMENT_CHARS = 255
#: GitHub's value limit is 48 KB.
MAX_VALUE_BYTES = 48 * 1024
#: A value this short is not registered with the redaction filter: it would
#: mask every occurrence of a common substring in every log line meanwhile.
#: It is never logged either way.
MIN_REDACTED_CHARS = 8
#: Environments listed per repository, and names per list.
MAX_ENVIRONMENTS = 30
PER_PAGE = 100

API = "https://api.github.com"
TIMEOUT_SECONDS = 10.0
RUNBOOK = "docs/runbooks/github-app.md#writing-github-actions-secrets"


@dataclass(frozen=True)
class _Bearer:
    """A minted token in hand, and the quota GitHub keeps it under."""

    token: str = field(repr=False)
    credential: str


class ActionsMint(Protocol):
    """`InstallationTokens.actions_token`: the one mint this module uses."""

    def actions_token(self, tenant_id: str, owner: str, repo: str, use: str) -> ActionsToken: ...


# ---------------------------------------------------------------------------
# names, the seal
# ---------------------------------------------------------------------------


def check_secret_name(name: str) -> str:
    """GitHub's rules, refused before any call. GitHub stores names upper-case."""
    if not isinstance(name, str) or not SECRET_NAME.fullmatch(name):
        raise ValidationFailed(
            "a GitHub Actions secret name is letters, digits and underscores, not starting "
            "with a digit, at most 200 characters")
    if name.upper().startswith(RESERVED_PREFIX):
        raise ValidationFailed("GitHub reserves secret names starting with GITHUB_")
    return name.upper()


def check_environment(environment: str | None) -> str | None:
    if environment is None:
        return None
    if not isinstance(environment, str) or not environment \
            or len(environment) > MAX_ENVIRONMENT_CHARS or environment != environment.strip() \
            or "/" in environment or environment in (".", "..") \
            or any(ord(c) < 32 or ord(c) == 127 for c in environment):
        # `.` and `..` are refused too: a dot segment in the path would be a
        # different GitHub endpoint once anything normalised it.
        raise ValidationFailed(
            "an environment name is GitHub's name for it: 1-255 characters, no '/', not "
            "'.' or '..', no control characters, no leading or trailing space")
    return environment


def check_value(name: str, value: str) -> bytes:
    """The value's bytes, refused when empty, too long for GitHub, or the name itself."""
    raw = value.encode("utf-8") if isinstance(value, str) else b""
    if not raw:
        raise ValidationFailed("the value is empty")
    if len(raw) > MAX_VALUE_BYTES:
        raise ValidationFailed("GitHub accepts a secret value of at most 48 KB")
    if value.strip().upper() == name.upper():
        raise ValidationFailed("the value is the secret's name; paste the value itself")
    return raw


def seal(public_key: str, raw: bytes) -> str:
    """libsodium's sealed box of `raw` to GitHub's base64 Curve25519 key,
    base64: the only form GitHub accepts. Encrypt-only: nobody here can open it."""
    from nacl.public import PublicKey, SealedBox

    try:
        key = PublicKey(base64.b64decode(public_key, validate=True))
    except Exception:
        raise UpstreamUnavailable("GitHub served a public key that is not a Curve25519 key; "
                                  "nothing was written") from None
    return base64.b64encode(SealedBox(key).encrypt(raw)).decode("ascii")


def create_environment_url(owner: str, repo: str) -> str:
    """GitHub's page where a person creates an environment (Q4: SwarmCloud does not)."""
    return f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}" \
           "/settings/environments/new"


def environment_settings_url(owner: str, repo: str) -> str:
    return f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}" \
           "/settings/environments"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_id(tenant_id: str, repo_id: str, environment: str | None, name: str) -> str:
    return _digest(f"{tenant_id}~{repo_id}~{environment or ''}~{name.upper()}")[:40]


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class ActionsSecrets:
    """Reads, writes and deletes one repository's GitHub Actions secrets.

    `registration` is a `repositories/{repo_id}` document the route has
    already checked belongs to the caller's workspace and derives its id."""

    def __init__(self, db: Any, *, mint: ActionsMint | None, send: HttpSend,
                 now: Callable[[], datetime], app_slug: str = "",
                 timeout: float = TIMEOUT_SECONDS) -> None:
        self._db = db
        self._mint = mint
        self._send = send
        self._now = now
        self._slug = app_slug
        self._timeout = timeout

    # -- tokens --------------------------------------------------------------

    def _token(self, held: ExitStack, tenant_id: str, registration: Mapping[str, Any],
               use: str) -> _Bearer:
        owner, repo = str(registration["owner"]), str(registration["repo"])
        if self._mint is None:
            raise UpstreamUnavailable("swarm-api has no GitHub App to mint a token with; "
                                      "nothing was written")
        try:
            minted = self._mint.actions_token(tenant_id, owner, repo, use)
        except ActionsMintRefused as refused:
            raise self._mint_refusal(refused, owner, repo) from None
        held.enter_context(redaction_literal(minted.token))
        return _Bearer(minted.token, forge_limits.installation_credential(
            minted.installation.installation_id))

    def _mint_refusal(self, refused: ActionsMintRefused, owner: str, repo: str) -> Exception:
        repository = f"{owner}/{repo}"
        if refused.reason == MINT_FAILED:
            return UpstreamUnavailable(
                "the GitHub App could not mint a token for this repository just now; "
                "nothing was written, and trying again in a minute is safe",
                detail={"reason": MINT_FAILED, "repository": repository})
        if refused.reason == MINT_NO_INSTALLATION:
            install = (f"https://github.com/apps/{quote(self._slug, safe='')}/installations/new"
                       if self._slug else None)
            return Conflict(
                "the SwarmCloud GitHub App is not installed for this repository's owner in "
                "this workspace; install it and enable the owner, then try again",
                detail={"reason": APP_NOT_INSTALLED, "repository": repository,
                        "install_url": install})
        inst = refused.installation
        link = review_url(inst.account, inst.installation_id, inst.account_type) \
            if inst is not None else None
        if refused.reason == MINT_NOT_COVERED:
            return Conflict(
                "the SwarmCloud GitHub App's installation does not cover this repository; "
                "its owner adds it to the installation",
                detail={"reason": NOT_COVERED, "repository": repository, "review_url": link})
        return Conflict(
            "the SwarmCloud GitHub App's installation has not granted the permission this "
            "needs (Secrets and Environments: Read and write); the installation's owner "
            "accepts it on GitHub, and nothing was written",
            detail={"reason": refused.reason, "repository": repository, "review_url": link,
                    "requires": [r.to_api() for r in ACTIONS_SECRETS_REQUIREMENTS],
                    "runbook": RUNBOOK})

    # -- GitHub --------------------------------------------------------------

    def _call(self, method: str, path: str, bearer: _Bearer,
              body: Mapping[str, Any] | None = None) -> HttpAnswer:
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {bearer.token}",
                   "User-Agent": "swarmcloud-swarm-api", "X-GitHub-Api-Version": "2022-11-28"}
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(dict(body)).encode("utf-8")
        try:
            answer = self._send(method, f"{API}{path}", headers, raw, self._timeout)
        except Exception as exc:
            # The type only: a transport's message can quote the request.
            log.warning("actions secrets: GitHub %s %s: no answer (%s)", method,
                        path.split("/secrets/")[0], type(exc).__name__)
            raise UpstreamUnavailable("GitHub did not answer; nothing was written, and "
                                      "trying again in a minute is safe") from None
        finally:
            headers.clear()
            raw = None
        forge_limits.QUOTAS.observe_answer(answer, credential=bearer.credential)
        status = getattr(answer, "status", 0)
        if status >= 500 or status == 429:
            raise UpstreamUnavailable(f"GitHub answered HTTP {status}; nothing was written, "
                                      "and trying again in a minute is safe")
        return answer

    def _refused(self, what: str, answer: HttpAnswer, repository: str) -> Conflict:
        """GitHub answered no. Its text is never repeated: a status only."""
        return Conflict(f"GitHub refused {what} (HTTP {answer.status})",
                        detail={"reason": GITHUB_REFUSED, "repository": repository,
                                "github_status": answer.status})

    # -- the rule (Q4) -------------------------------------------------------

    def rule(self, tenant_id: str, registration: Mapping[str, Any],
             environment: str) -> dict[str, Any]:
        """Whether `environment` exists and lets only the default branch deploy."""
        with ExitStack() as held:
            token = self._token(held, tenant_id, registration, ACTIONS_RULE_READ)
            return self._rule(token, registration, environment)

    def _rule(self, token: _Bearer, registration: Mapping[str, Any],
              environment: str) -> dict[str, Any]:
        owner, repo = str(registration["owner"]), str(registration["repo"])
        default = registration.get("default_branch")
        branch = default if isinstance(default, str) and default else "main"
        base = _repo_path(owner, repo, f"/environments/{quote(environment, safe='')}")
        answer: dict[str, Any] = {
            "environment": environment, "exists": False, "rule": None, "main_only": False,
            "branch": branch, "branches": [], "create_url": None,
            "settings_url": environment_settings_url(owner, repo)}
        got = self._call("GET", base, token)
        if got.status == 404:
            answer["create_url"] = create_environment_url(owner, repo)
            answer["steps"] = (
                f"Create the environment {environment!r} on GitHub, set its deployment "
                f"branches to 'Selected branches and tags' with the one rule {branch!r}, "
                "then check its rule here again")
            return answer
        if got.status != 200 or not isinstance(got.json(), dict):
            raise self._refused("the environment read", got, f"{owner}/{repo}")
        answer["exists"] = True
        policy = got.json().get("deployment_branch_policy")
        if not isinstance(policy, dict):
            answer["rule"] = RULE_ANY
            return answer
        if policy.get("protected_branches") is True:
            answer["rule"] = RULE_PROTECTED
            return answer
        if policy.get("custom_branch_policies") is not True:
            answer["rule"] = RULE_ANY
            return answer
        listed = self._call("GET", f"{base}/deployment-branch-policies?per_page={PER_PAGE}",
                            token)
        data = listed.json() if listed.status == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get("branch_policies"), list):
            raise self._refused("the environment's branch rules read", listed, f"{owner}/{repo}")
        rules = [{"name": str(p.get("name")), "type": str(p.get("type") or "")}
                 for p in data["branch_policies"] if isinstance(p, dict)]
        answer["branches"] = rules
        complete = data.get("total_count") == len(rules)
        main_only = complete and rules == [{"name": branch, "type": "branch"}]
        answer["rule"] = RULE_MAIN_ONLY if main_only else RULE_CUSTOM
        answer["main_only"] = main_only
        return answer

    # -- names ---------------------------------------------------------------

    def list(self, tenant_id: str, registration: Mapping[str, Any]) -> dict[str, Any]:
        """Names and times, the repository's and each environment's, read
        live (§3.3), each beside SwarmCloud's own record of its writes."""
        owner, repo = str(registration["owner"]), str(registration["repo"])
        repo_id = str(registration["repo_id"])
        mine = self._written(tenant_id, repo_id)
        with ExitStack() as held:
            token = self._token(held, tenant_id, registration, ACTIONS_REPO_READ)
            repository = self._names(token, _repo_path(owner, repo, "/actions/secrets"),
                                     mine, None, f"{owner}/{repo}")
        environments: list[dict[str, Any]] = []
        with ExitStack() as held:
            token = self._token(held, tenant_id, registration, ACTIONS_RULE_READ)
            got = self._call("GET", _repo_path(owner, repo,
                                               f"/environments?per_page={MAX_ENVIRONMENTS}"),
                             token)
            data = got.json() if got.status == 200 else None
            if got.status != 200 or not isinstance(data, dict):
                raise self._refused("the environments read", got, f"{owner}/{repo}")
            names = [str(e.get("name")) for e in data.get("environments") or []
                     if isinstance(e, dict) and isinstance(e.get("name"), str)]
            for name in names[:MAX_ENVIRONMENTS]:
                environments.append({"environment": name,
                                     "rule": self._rule(token, registration, name)})
        if environments:
            with ExitStack() as held:
                token = self._token(held, tenant_id, registration, ACTIONS_ENV_READ)
                for entry in environments:
                    path = _repo_path(owner, repo, "/environments/"
                                      f"{quote(entry['environment'], safe='')}/secrets")
                    entry["secrets"] = self._names(token, path, mine, entry["environment"],
                                                   f"{owner}/{repo}")
        # `repository_secrets`, not `repository`: the route serves `repository`
        # as 'owner/repo', and a list under the same key overwrote it. The
        # create page is served on the list itself, so a repository with no
        # environment yet still links where a person makes one (Q4).
        return {"repository_secrets": repository, "environments": environments,
                "environments_truncated": len(environments) >= MAX_ENVIRONMENTS,
                "create_url": create_environment_url(owner, repo)}

    def _names(self, token: _Bearer, path: str, mine: Mapping[tuple[str | None, str], dict],
               environment: str | None, repository: str) -> list[dict[str, Any]]:
        got = self._call("GET", f"{path}?per_page={PER_PAGE}", token)
        data = got.json() if got.status == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get("secrets"), list):
            raise self._refused("the secret names read", got, repository)
        rows = []
        for item in data["secrets"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            ours = mine.get((environment, item["name"].upper()))
            rows.append({"name": item["name"], "created_at": item.get("created_at"),
                         "updated_at": item.get("updated_at"),
                         "set_by_swarmcloud": ours is not None,
                         "set_by": ours.get("set_by") if ours else None,
                         "set_at": _iso(ours.get("set_at")) if ours else None})
        return rows

    def _written(self, tenant_id: str, repo_id: str) -> dict[tuple[str | None, str], dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = (self._db.collection(WRITES_COLLECTION)
                 .where(filter=FieldFilter("repo_id", "==", repo_id)).limit(500))
        found: dict[tuple[str | None, str], dict] = {}
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if doc.get("tenant_id") == tenant_id and doc.get("state") == SET:
                found[(doc.get("environment"), str(doc.get("name")))] = doc
        return found

    # -- the write -----------------------------------------------------------

    def guard_environment(self, tenant_id: str, registration: Mapping[str, Any],
                          environment: str, confirm: str | None) -> dict[str, Any]:
        """Q4: refuse a write into a missing environment, or into one whose
        rule is not main-only unless `confirm` is its name, typed."""
        rule = self.rule(tenant_id, registration, environment)
        if not rule["exists"]:
            raise Conflict(
                f"the environment {environment!r} does not exist; SwarmCloud does not create "
                "environments, so create it on GitHub with its deployment branches set to the "
                "default branch only, then try again",
                detail={"reason": ENVIRONMENT_MISSING, "environment": environment,
                        "create_url": rule["create_url"], "steps": rule.get("steps")})
        if not rule["main_only"] and confirm != environment:
            raise Conflict(
                f"the environment {environment!r} lets {_rule_words(rule)} deploy, so a "
                "secret there reaches the workflows of those branches; type the "
                "environment's name to write it anyway",
                detail={"reason": NOT_MAIN_ONLY, "environment": environment,
                        "rule": rule["rule"], "branches": rule["branches"],
                        "settings_url": rule["settings_url"]})
        return rule

    def set(self, *, tenant_id: str, registration: Mapping[str, Any], name: str,
            environment: str | None, value: str, by: str,
            via_link: bool) -> dict[str, Any]:
        """Seal `value` to GitHub's key and PUT it. The value, its bytes and
        the ciphertext are locals of this call and of nothing else."""
        owner, repo = str(registration["owner"]), str(registration["repo"])
        raw = check_value(name, value)
        if environment is None:
            use, base = ACTIONS_REPO_WRITE, _repo_path(owner, repo, "/actions/secrets")
        else:
            use = ACTIONS_ENV_WRITE
            base = _repo_path(owner, repo,
                              f"/environments/{quote(environment, safe='')}/secrets")
        put_path = f"{base}/{quote(name, safe='')}"
        with ExitStack() as held:
            if len(value) >= MIN_REDACTED_CHARS:
                held.enter_context(redaction_literal(value))
            token = self._token(held, tenant_id, registration, use)
            answer = None
            for _attempt in range(2):
                key_id, key = self._public_key(token, base, f"{owner}/{repo}")
                sealed = seal(key, raw)
                answer = self._call("PUT", put_path, token,
                                    {"encrypted_value": sealed, "key_id": key_id})
                sealed = ""
                if answer.status in (201, 204):
                    break
                if answer.status != 422:
                    raise self._refused("the secret write", answer, f"{owner}/{repo}")
                # A 422 is the key it was sealed to no longer being GitHub's
                # (re-read, re-seal, once) or GitHub refusing the value.
                current, _ = self._public_key(token, base, f"{owner}/{repo}")
                if current == key_id:
                    raise self._refused("the secret's value", answer, f"{owner}/{repo}")
            else:
                raise Conflict(
                    "GitHub rotated the public key while the secret was being sealed, twice; "
                    "nothing was written, and setting it again is safe",
                    detail={"reason": KEY_ROTATED, "repository": f"{owner}/{repo}"})
            raw = b""
        status = answer.status if answer is not None else 0
        record = self._record(tenant_id=tenant_id, registration=registration, name=name,
                              environment=environment, by=by, state=SET, status=status,
                              action=AUDIT_SET, via_link=via_link)
        log.info("actions secret set tenant=%s repo_id=%s environment=%s name=%s status=%s",
                 tenant_id, registration.get("repo_id"), environment, name, status)
        return {"secret": record, "created": status == 201}

    def _public_key(self, token: _Bearer, base: str, repository: str) -> tuple[str, str]:
        got = self._call("GET", f"{base}/public-key", token)
        data = got.json() if got.status == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get("key_id"), str) \
                or not isinstance(data.get("key"), str):
            raise self._refused("the public key read", got, repository)
        return data["key_id"], data["key"]

    def delete(self, *, tenant_id: str, registration: Mapping[str, Any], name: str,
               environment: str | None, by: str) -> dict[str, Any]:
        owner, repo = str(registration["owner"]), str(registration["repo"])
        if environment is None:
            use, base = ACTIONS_REPO_WRITE, _repo_path(owner, repo, "/actions/secrets")
        else:
            use = ACTIONS_ENV_WRITE
            base = _repo_path(owner, repo,
                              f"/environments/{quote(environment, safe='')}/secrets")
        with ExitStack() as held:
            token = self._token(held, tenant_id, registration, use)
            answer = self._call("DELETE", f"{base}/{quote(name, safe='')}", token)
        if answer.status == 404:
            raise NotFound("GitHub has no secret by that name there")
        if answer.status != 204:
            raise self._refused("the secret delete", answer, f"{owner}/{repo}")
        record = self._record(tenant_id=tenant_id, registration=registration, name=name,
                              environment=environment, by=by, state=DELETED, status=204,
                              action=AUDIT_DELETED, via_link=False)
        log.info("actions secret deleted tenant=%s repo_id=%s environment=%s name=%s",
                 tenant_id, registration.get("repo_id"), environment, name)
        return {"secret": record, "deleted": True}

    def _record(self, *, tenant_id: str, registration: Mapping[str, Any], name: str,
                environment: str | None, by: str, state: str, status: int, action: str,
                via_link: bool) -> dict[str, Any]:
        """Names and timestamps only (§3.8), with its audit row, in one transaction."""
        at = self._now()
        repo_id = str(registration["repo_id"])
        doc_id = write_id(tenant_id, repo_id, environment, name)
        doc = {"repo_id": repo_id, "tenant_id": tenant_id,
               "repository": f"{registration['owner']}/{registration['repo']}",
               "environment": environment, "name": name, "set_by": by, "set_at": at,
               "github_status": status, "state": state}
        ref = self._db.collection(WRITES_COLLECTION).document(doc_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()

        @firestore.transactional
        def _write(txn: Any) -> None:
            txn.set(ref, doc)
            txn.set(audit, {"action": action, "by": by, "at": at, "target_secret_id": doc_id,
                            "detail": {"tenant_id": tenant_id, "repo_id": repo_id,
                                       "environment": environment, "name": name,
                                       "github_status": status, "via_link": via_link}})

        _write(self._db.transaction())
        return {**doc, "set_at": at.isoformat()}

    # -- the one-time link (§6.3, Q8) -----------------------------------------

    def mint_link(self, *, email: str, tenant_id: str, repo_id: str, environment: str | None,
                  name: str, console: str | None) -> dict[str, Any]:
        token = _random.token_urlsafe(32)
        now = self._now()
        expires_at = now + LINK_TTL
        self._db.collection(LINKS_COLLECTION).document(_digest(token)).set({
            "email": email, "tenant_id": tenant_id, "repo_id": repo_id,
            "environment": environment, "name": name, "created_at": now,
            "expires_at": expires_at, "used_at": None})
        path = f"/config/secrets?scope=repository&repo={repo_id}&dest=actions&name={name}"
        if environment is not None:
            path += f"&env={quote(environment, safe='')}"
        path += f"&t={token}"
        base = console_origin(console)
        log.info("minted an actions secret form link tenant=%s repo_id=%s environment=%s "
                 "name=%s", tenant_id, repo_id, environment, name)
        return {"url": f"{base}{path}" if base else None, "path": path, "name": name,
                "environment": environment, "expires_at": expires_at.isoformat()}

    def consume_link(self, token: str | None, *, email: str, tenant_id: str, repo_id: str,
                     environment: str | None, name: str) -> bool:
        """False when no link was presented (a terminal's `--stdin` write). A
        link that is unknown, used, expired, or minted for another person,
        workspace, repository, environment or name is refused (403) and left
        unburnt; the message never carries the token."""
        if token is None:
            return False
        ref = self._db.collection(LINKS_COLLECTION).document(_digest(token)) \
            if 0 < len(token) <= 256 else None
        now = self._now()

        @firestore.transactional
        def _consume(txn: Any) -> bool:
            if ref is None:
                return False
            snap = _snapshot(txn.get(ref))
            link = (snap.to_dict() or {}) if snap.exists else None
            if not link or link.get("used_at") is not None:
                return False
            expires_at = link.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                return False
            if (link.get("email"), link.get("tenant_id"), link.get("repo_id"),
                    link.get("environment"), link.get("name")) \
                    != (email, tenant_id, repo_id, environment, name):
                return False
            txn.update(ref, {"used_at": now})
            return True

        if not _consume(self._db.transaction()):
            raise SecretLinkInvalid(
                "this GitHub Actions secret link is unknown, already used, expired, or was "
                "minted for someone else, another repository, environment or secret; mint a "
                "new one")
        return True


def _repo_path(owner: str, repo: str, rest: str = "") -> str:
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}{rest}"


def _rule_words(rule: Mapping[str, Any]) -> str:
    if rule.get("rule") == RULE_PROTECTED:
        return "every protected branch"
    if rule.get("rule") == RULE_CUSTOM:
        return "branches other than, or besides, " + str(rule.get("branch"))
    return "any branch"


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value
