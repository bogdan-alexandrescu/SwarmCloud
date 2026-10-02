"""The forge: asking GitHub what this token may do, and opening one pull request.

Kept apart from `gitops.py` because the two answer different questions and fail
in different ways. `gitops.py` runs git; this runs HTTP. A forge that is down,
or a token that turns out to be read-only, must not look like a git failure --
the operator response to each is completely different.

WHY THE PERMISSION IS ASKED FOR RATHER THAN ASSUMED
---------------------------------------------------
`GET /repos/{owner}/{repo}` returns a `permissions` object when the request is
authenticated, and `permissions.push` is the authoritative answer for a classic
PAT, a fine-grained PAT and a GitHub App installation token alike. The obvious
alternative -- reading the `X-OAuth-Scopes` response header -- works for classic
PATs ONLY and is absent for the other two, so a platform that trusted it would
read every fine-grained token as unscoped and refuse work it was entitled to do.

The probe is also what makes the read-only path a real path rather than a
degraded one. With no write permission the worker is TOLD so, by the forge, in
one request with no side effect, and records the reason where a reader can see
it. It does not discover it by attempting a push and parsing the rejection.

WHY `default_branch` IS READ BACK
---------------------------------
The push refusal list in `gitops.push_branch` needs the repository's real
default branch. Assuming `main` was the bug waiting to happen: plenty of
repositories still default to `master`, and a guard that protects a branch the
repository does not have protects nothing.

RATE
----
There is no rate limiter in this module and it needs none, because the shape of
the caller makes one unnecessary: the worker opens at most ONE pull request per
attempt, on the terminal path only, onto a branch whose name it derives from the
task id. The agent chooses neither the branch, the base, nor the number of
calls. An agent instructed by a malicious repository to "open four hundred pull
requests" has no mechanism to do so -- not a quota it would exhaust first.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

_UA = "swarmcloud-agent-worker"
_TIMEOUT = 30


class ForgeError(RuntimeError):
    """The forge could not be reached, or refused in a way worth surfacing."""


#: The only hosts github.com's own API answers on (case-insensitive; `host` is
#: always lower-cased by `parse_repo`/`urlparse`, never carries a port or
#: userinfo). Anything else -- a GitHub Enterprise Server install, or a host
#: merely named "github.com" in its path or as a subdomain suffix -- is not
#: this set, by exact string equality alone. Callers that decide whether a
#: tenant's forge token may be attached to a request (`issue.py`) key off
#: this, not off `api_base`, which answers a different question (where do we
#: ask) and is populated for both cases.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def may_receive_forge_token(host: str | None) -> bool:
    """True when the tenant's forge token may be sent to `host`.

    THE one rule for where the token goes (#307), read by the clone, the push
    and the integrator's fetch (`gitops._write_credentials`) and the forge API
    calls below. The issue fetch (`issue.py`, #270) applies the same set,
    `GITHUB_HOSTS`, to the same lower-cased host directly. The token is the
    tenant's credential for github.com; a task's `repository_url` is a claim
    made by whoever submitted the task, so a host it names is never trusted
    with the token on that say-so. A GitHub Enterprise Server host is not in
    the set either: the worker holds no record of a tenant's own forge host,
    and one read from the task would be exactly the claim this refuses.

    `host` must be a bare hostname (`urlparse().hostname`); callers refuse a
    URL that carries a port before asking.
    """
    return bool(host) and host.lower() in GITHUB_HOSTS


@dataclass(frozen=True)
class RepoRef:
    host: str
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def api_base(self) -> str:
        # github.com is the only host with a separate api. subdomain; every
        # GitHub Enterprise Server install serves the same API under /api/v3.
        if self.host in GITHUB_HOSTS:
            return "https://api.github.com"
        return f"https://{self.host}/api/v3"


@dataclass(frozen=True)
class RepoAccess:
    ref: RepoRef
    default_branch: str
    can_push: bool
    #: Always populated, including on success, because "why can I not push"
    #: is the question this whole path exists to answer legibly.
    reason: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str
    #: False when an open pull request for this branch already existed. A
    #: resumed attempt pushing again must update that one, never open a second.
    created: bool
    #: True when that existing pull request's title and body were replaced
    #: (`open_pull_request(update_existing=True)`, #214).
    updated: bool = False
    #: The title GitHub reports for an ADOPTED pull request (empty on one this
    #: call created). Read only so `open_pull_request` can tell a title a
    #: human may have written from the platform's own stale one (`retitle_if`,
    #: #259 follow-up); nothing else in this module or its caller uses it.
    title: str = ""


def parse_repo(url: str) -> RepoRef | None:
    """Split a clone URL into host/owner/name, or None if it is not a forge URL.

    Returning None rather than raising is deliberate: a repository on a host
    this module does not understand is a perfectly ordinary situation, and the
    caller's response is to harvest a patch instead of publishing -- not to
    fail the attempt.
    """
    if not url:
        return None
    candidate = url
    if candidate.startswith("git@"):
        # scp-style: git@host:owner/name.git
        head, _, tail = candidate.partition(":")
        candidate = f"ssh://{head}/{tail}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        # A non-numeric port component -- not a URL this module understands.
        return None
    if port is not None:
        # `api_base` derives its URL from `host` alone (`api.github.com`, or
        # `https://<host>/api/v3`); a port here would be silently dropped and
        # every request would go to the wrong endpoint -- or, for a host that
        # happens to equal "github.com" on a non-standard port, to the real
        # api.github.com when the repository is not actually served there.
        # Refuse rather than guess: same response as a host this module does
        # not understand.
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, name = parts[-2], parts[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not owner or not name:
        return None
    return RepoRef(host=host, owner=owner, name=name)


def _request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", _UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            parsed = {"message": raw[:500]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise ForgeError(f"could not reach {urlparse(url).hostname}: {exc.reason}") from exc


def probe_repository(*, url: str, token: str | None) -> RepoAccess | None:
    """Ask the forge what this token may do. One GET, no side effect.

    None means "this is not a forge I can publish to" -- an unrecognised host,
    or no credential at all. The caller harvests a patch and says so.
    """
    ref = parse_repo(url)
    if ref is None:
        return None
    if token and not may_receive_forge_token(ref.host):
        # Asked BEFORE any request: the probe sends the token as a bearer
        # header, so asking `https://<any host>/api/v3` would hand it over
        # ahead of any push (#307).
        return RepoAccess(
            ref=ref,
            default_branch="",
            can_push=False,
            reason=(
                f"the tenant's git credential is sent only to github.com, "
                f"and {ref.host} is not github.com"
            ),
        )
    if not token:
        return RepoAccess(
            ref=ref,
            default_branch="",
            can_push=False,
            reason="no git credential is registered for this tenant",
        )

    status, data = _request(f"{ref.api_base}/repos/{ref.owner}/{ref.name}", token=token)
    if status == 404:
        # 404 rather than 403 is what GitHub returns for a private repository
        # the token cannot see AT ALL, so it is not necessarily "missing".
        return RepoAccess(
            ref=ref,
            default_branch="",
            can_push=False,
            reason="the token cannot see this repository (404)",
        )
    if status == 401:
        return RepoAccess(
            ref=ref, default_branch="", can_push=False, reason="the token was rejected (401)"
        )
    if status != 200 or not isinstance(data, dict):
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or "")
        return RepoAccess(
            ref=ref,
            default_branch="",
            can_push=False,
            reason=f"the forge answered {status}{': ' + message if message else ''}",
        )

    permissions = data.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    # `is True` and not truthiness: a forge that omitted the key, or sent null,
    # must read as "no", and `permissions.get("push")` returning None would be
    # falsy today and is one refactor away from being treated as unknown.
    can_push = permissions.get("push") is True
    default_branch = str(data.get("default_branch") or "")
    if can_push:
        reason = "the token has write permission on this repository"
    elif permissions:
        granted = sorted(k for k, v in permissions.items() if v is True) or ["none"]
        reason = f"the token has {', '.join(granted)} but not push"
    else:
        reason = "the forge reported no permissions for this token"
    return RepoAccess(
        ref=ref, default_branch=default_branch, can_push=can_push, reason=reason
    )


def open_pull_request(
    *,
    access: RepoAccess,
    token: str,
    head: str,
    base: str,
    title: str,
    body: str,
    update_existing: bool = False,
    retitle_if: Callable[[str], bool] | None = None,
) -> PullRequest:
    """Open one pull request, or adopt the open one this branch already has.

    Idempotent on purpose. A task that is retried after a park pushes the same
    branch a second time, and a second pull request for the same work would be
    noise that a human has to close by hand. GitHub answers 422 for the
    duplicate; that is looked up rather than treated as a failure.

    `update_existing` replaces an adopted pull request's title and body with
    these (#214). The worker asks for it only when the AGENT wrote them
    (`pr-title.txt`, `pr-body.md`): a retry whose agent wrote a new
    `Closes #N` must put it on the pull request it reuses, and a retry with
    nothing of the agent's to say must not overwrite a title or body a human
    edited by hand. A refused update is not a failure -- the pull request is
    still adopted, with `updated` False.

    `retitle_if`, separately, retitles an adopted pull request whose CURRENT
    title it answers True for -- the worker passes the owner's 2026-09-28 rule
    that a title never carries the task id, so a pull request left with the
    old `[swarm] task_...` fallback is retitled the next time this task's
    branch is pushed, whether or not the agent wrote anything of its own. Only
    the title is replaced on that path: the body may be a human's, and
    nothing about it broke the rule.
    """
    ref = access.ref
    if not may_receive_forge_token(ref.host):
        # `probe_repository` never reports can_push for such a host; this is
        # the same rule held where the token would actually leave (#307).
        raise ForgeError(
            f"refusing to send the tenant's git credential to {ref.host}: "
            "it is sent only to github.com"
        )
    status, data = _request(
        f"{ref.api_base}/repos/{ref.owner}/{ref.name}/pulls",
        token=token,
        method="POST",
        payload={"title": title, "head": head, "base": base, "body": body, "draft": False},
    )
    if status == 201 and isinstance(data, dict):
        return PullRequest(
            number=int(data.get("number") or 0),
            url=str(data.get("html_url") or ""),
            state=str(data.get("state") or "open"),
            created=True,
        )

    if status == 422:
        existing = _find_open_pull_request(access=access, token=token, head=head)
        if existing is not None:
            if update_existing and existing.number:
                return _update_pull_request(
                    access=access, token=token, existing=existing, title=title, body=body
                )
            if retitle_if is not None and existing.number and retitle_if(existing.title):
                return _update_pull_request(
                    access=access, token=token, existing=existing, title=title, body=None
                )
            return existing
        message = ""
        if isinstance(data, dict):
            errors = data.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    message = str(first.get("message") or "")
            message = message or str(data.get("message") or "")
        # The other common 422 is "No commits between base and head", which is
        # a real outcome worth reporting verbatim rather than paraphrasing.
        raise ForgeError(f"the forge refused the pull request: {message or '422'}")

    message = str(data.get("message")) if isinstance(data, dict) else ""
    raise ForgeError(f"could not open a pull request ({status}){': ' + message if message else ''}")


def _find_open_pull_request(
    *, access: RepoAccess, token: str, head: str
) -> PullRequest | None:
    ref = access.ref
    status, data = _request(
        f"{ref.api_base}/repos/{ref.owner}/{ref.name}/pulls"
        f"?head={ref.owner}:{head}&state=open&per_page=1",
        token=token,
    )
    if status != 200 or not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    return PullRequest(
        number=int(first.get("number") or 0),
        url=str(first.get("html_url") or ""),
        state=str(first.get("state") or "open"),
        created=False,
        title=str(first.get("title") or ""),
    )


def _update_pull_request(
    *, access: RepoAccess, token: str, existing: PullRequest, title: str, body: str | None
) -> PullRequest:
    """PATCH an adopted pull request's title and body (title only when `body`
    is None); `updated` says whether it took."""
    ref = access.ref
    payload: dict[str, Any] = {"title": title}
    if body is not None:
        payload["body"] = body
    try:
        status, _ = _request(
            f"{ref.api_base}/repos/{ref.owner}/{ref.name}/pulls/{existing.number}",
            token=token,
            method="PATCH",
            payload=payload,
        )
    except ForgeError:
        # The pull request exists either way; an unreachable forge on the
        # update must not read as "no pull request was opened".
        status = 0
    return PullRequest(
        number=existing.number,
        url=existing.url,
        state=existing.state,
        created=False,
        updated=status == 200,
    )
