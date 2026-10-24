"""Is this person an admin of this repository, says GitHub, now? (SSC-REPO-SCOPE, part of #2158)

docs/design/secrets-scopes.md §2.5 and §3.6, owner decisions Q2 and Q6 (§11,
2026-10-23). A repository's secrets for agents may be set by a workspace
admin, a platform admin, OR a member whom GitHub calls admin of that
repository; SSC-ACTIONS-API reuses this module for its GitHub Actions writes,
where the GitHub admin rule is required of every writer.

THE RULE, AND WHAT IT IS NEVER READ FROM:

  * LIVE, AT EACH WRITE. `GET /repos/{owner}/{repo}` answers
    `permissions.admin` for the token that asks (`forge.repository_from`,
    `RepositoryRead.can_admin`). It is asked at the moment of the write and
    never stored: an admin demoted on GitHub a minute ago is refused now, not
    at the next sync.
  * WITH THE PERSON'S OWN GITHUB USER TOKEN: their forge connection
    (`forgeapp.py`, read through `AccessService.user_token`, which refreshes it
    when it is near expiry). NEVER the App's installation token and never the
    tenant's or a repository's forge token: GitHub answers `permissions` for
    whoever holds the token, so an installation token's answer is the App's
    reach, not the person's. A credential whose scope is not the user's is
    refused before anything is sent.
  * FAIL CLOSED. No connection, an expired or refused token, a repository
    GitHub does not show this person (404), a refusal (401, 403), a rate
    limit, a 5xx or a transport failure: each is "not an admin", with a
    `reason` the console can show. Nothing here guesses yes.

The token is a local of `check` and goes to exactly one call, the GET. No
answer, log line or error carries it; `AccessService.user_token` registers it
with the redaction filter for the request.

THE REFUSAL. `require_repo_admin` raises 403 `github_admin_required` with
`detail.reason`. It is a new refusal, so it ships behind its switch
(`refusals.SWITCHES`); while that is off it is logged report-only and the
caller is STILL refused, with the established `forbidden` code and the same
reason. The switch changes the code a caller sees, never whether a person
GitHub does not call admin gets through.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from .access import AccessRefused, NotConnected
from .errors import ApiError, Forbidden
from .forge import IssueNoAccess, IssueNotFound, IssueReadFailed
from .forgeapp import Caller, user_hash
from .refusals import refuse
from .repositories import SCOPE_USER, HeldCredential

log = logging.getLogger(__name__)

#: The refusal's code, and its switch's key in `refusals.SWITCHES`.
GITHUB_ADMIN_REQUIRED = "github_admin_required"

#: Why GitHub's answer was not "admin". The console shows each by its copy.
ADMIN = "admin"
NOT_CONNECTED = "github_not_connected"
TOKEN_EXPIRED = "github_token_expired"
NOT_VISIBLE = "repository_not_visible"
NOT_ADMIN = "github_not_admin"
UNAVAILABLE = "github_unavailable"

#: One sentence per reason. Constant text: nothing GitHub said is repeated.
COPY: dict[str, str] = {
    NOT_CONNECTED: "you have not connected GitHub in this workspace, so GitHub cannot say "
                   "whether you administer this repository: connect GitHub on Config › "
                   "GitHub access and try again",
    TOKEN_EXPIRED: "your GitHub authorisation has expired or was refused, so GitHub cannot "
                   "say whether you administer this repository: reconnect GitHub and try again",
    NOT_VISIBLE: "GitHub does not show you this repository, so it does not call you its admin",
    NOT_ADMIN: "GitHub does not call you an admin of this repository; ask a workspace admin, "
               "or a repository admin on GitHub",
    UNAVAILABLE: "GitHub could not be asked whether you administer this repository just now; "
                 "nothing was written, and trying again in a minute is safe",
}


class UserTokens(Protocol):
    """`AccessService`'s one method this module uses: the person's own token."""

    def user_token(self, caller: Caller) -> AbstractContextManager[HeldCredential]: ...


class RepositoryReader(Protocol):
    """`forge.GitHubIssues.repository`: `GET /repos/{owner}/{repo}` with `token`."""

    def repository(self, owner: str, repo: str, token: str) -> Any: ...


@dataclass(frozen=True)
class AdminAnswer:
    """What GitHub said, as a verdict and a reason. Never a token."""

    admin: bool
    reason: str
    #: The GitHub login the person's connection acts as, when one was read.
    login: str | None = None

    @property
    def message(self) -> str:
        return COPY.get(self.reason, "")


class GitHubAdminRequired(ApiError):
    status_code = 403
    code = GITHUB_ADMIN_REQUIRED


def check(tokens: UserTokens, github: RepositoryReader, caller: Caller, owner: str,
          repo: str) -> AdminAnswer:
    """Ask GitHub, with `caller`'s own user token, whether they administer
    `owner/repo`. Every failure is a no with its reason."""
    who = user_hash(caller.key)
    try:
        with tokens.user_token(caller) as held:
            if held.scope != SCOPE_USER or not held.value:
                # Only the person's own connection may answer for the person.
                log.warning("github repo admin check tenant=%s user_hash=%s: not a user "
                            "credential (scope=%s); refused", caller.tenant_id, who, held.scope)
                return AdminAnswer(False, NOT_CONNECTED)
            login = held.login
            try:
                read = github.repository(owner, repo, held.value)
            except IssueNotFound:
                return _no(caller, owner, repo, NOT_VISIBLE, login)
            except IssueNoAccess:
                return _no(caller, owner, repo, TOKEN_EXPIRED, login)
            except IssueReadFailed:
                return _no(caller, owner, repo, UNAVAILABLE, login)
    except NotConnected:
        return _no(caller, owner, repo, NOT_CONNECTED, None)
    except AccessRefused as refused:
        failure = refused.detail.get("failure_code") if isinstance(refused.detail, dict) else None
        return _no(caller, owner, repo,
                   TOKEN_EXPIRED if failure == "REFRESH_FAILED" else UNAVAILABLE, None)
    except Exception as exc:  # noqa: BLE001 -- said by type, never a message
        log.warning("github repo admin check tenant=%s user_hash=%s repo=%s/%s: %s",
                    caller.tenant_id, who, owner, repo, type(exc).__name__)
        return AdminAnswer(False, UNAVAILABLE)
    if getattr(read, "can_admin", False) is not True:
        return _no(caller, owner, repo, NOT_ADMIN, login)
    log.info("github repo admin check tenant=%s user_hash=%s repo=%s/%s: admin",
             caller.tenant_id, who, owner, repo)
    return AdminAnswer(True, ADMIN, login)


def _no(caller: Caller, owner: str, repo: str, reason: str, login: str | None) -> AdminAnswer:
    log.info("github repo admin check tenant=%s user_hash=%s repo=%s/%s: %s",
             caller.tenant_id, user_hash(caller.key), owner, repo, reason)
    return AdminAnswer(False, reason, login)


def refusal_detail(answer: AdminAnswer, repository: str) -> dict[str, Any]:
    return {"reason": answer.reason, "repository": repository}


def require_repo_admin(tokens: UserTokens, github: RepositoryReader, caller: Caller,
                       owner: str, repo: str) -> AdminAnswer:
    """`check`, raising when the answer is no: 403 `github_admin_required`
    with its switch on, 403 `forbidden` while it is off. Either way refused."""
    answer = check(tokens, github, caller, owner, repo)
    if answer.admin:
        return answer
    detail = refusal_detail(answer, f"{owner}/{repo}")
    refuse(GitHubAdminRequired(answer.message, detail=detail))
    raise Forbidden(answer.message, detail=detail)
