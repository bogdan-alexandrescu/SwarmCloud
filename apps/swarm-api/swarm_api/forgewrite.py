"""An issue run's WRITES to GitHub: its comments, and its pull request's body (#454).

Issue #454 asks the platform to write back to the issue it was given: the plan
as a comment, one status comment edited in place as the run moves, and the
`Closes #N` / `part of #N` block in the pull request's body. The owner decided
(2026-10-01) that these writes use the run's own tenant's credential,
`swarm-tenant-<tenant>-git`, read through `forge.SecretManagerForgeTokens` --
the same secret, the same reader, the same per-request lifetime as the
preview's read. That credential therefore needs, beyond what an agent's push
needs, `Issues: Read and write` (the comments) and `Pull requests: Read and
write` (the body); docs/multi-tenancy.md says so for the operator who stores
it.

EVERY RULE `forge.py` KEEPS FOR THE TOKEN HOLDS HERE, by sharing its code
rather than restating it:

  * the URL is built from `forge.GITHUB_API_HOST` and checked again in the
    transport (`forge.is_pinned_host`);
  * a redirect is never followed (`forge._OPENER`): urllib would copy
    `Authorization` onto it;
  * the token is in the `Authorization` header (`forge.github_headers`) and
    nowhere else -- never in a URL, a body, an error or a log line;
  * every error is a constant sentence raised `from None`, never built from
    GitHub's answer or a caught exception's text.

A 403 on a write gets its OWN code, `writeback_forbidden`, whose sentence
names the permission the credential is missing: the commonest way this fails
is a token that can push and open a pull request but was never given
`issues: write`, and "GitHub answered 403" would send an operator looking
everywhere else first.

THE CI READS (#454's CI loop) are on the same wire: the default branch's
rules, the check runs and commit statuses at the pull request's head, a
check run's annotations -- each with its own 403 code, `checks_forbidden`,
naming the read permission -- and, best-effort, the tail of a failed Actions
job's log. That log is the one read that leaves api.github.com: GitHub
answers `actions/jobs/{id}/logs` with a redirect to a signed URL on its log
storage. The redirect is still never FOLLOWED with the token. `locate` makes
the authenticated request and returns only the Location; `fetch` then reads
that URL with NO Authorization header, only on an allow-listed log host
(`is_log_host`), only over https, only its tail. The signed URL is itself a
credential with an expiry, so it is never logged, stored or put in an error.
A token that cannot read Actions, or any failure on the way, answers None:
the check run's own output is still the excerpt.

Since #646 it also CLOSES an issue (`close_issue`, `issues: write`): an
issue run whose build found every planned requirement already met on main.

Since the observer files its proposals (docs/schedules.md §3.4) it also
OPENS an issue (`create_issue`, `issues: write`) and finds one it opened by
the marker its body begins with (`find_issue`): one epic per observer report,
in the repository the schedule names, with the same tenant credential.

Since #748 it also OPENS a pull request from a branch that is already pushed
(`create_branch`, `open_pull`, `contents: write` and `pull_requests: write`):
a MERGE verdict's integrator, published by swarm-api without a worker
(`verdictpublish`).

WHICH FAILURE IS WORTH RETRYING (WRITEBACK, 2026-10-12): GitHub unreachable,
a timeout, a 5xx or a rate limit is `ForgeUnreachable` (`is_transient`); a 401
or a 403 that is not a rate limit is the credential itself
(`is_credential_refusal`). The CI loop backs off on the first and fails the run
at once on the second.

WHAT THIS DOES NOT DO: decide anything. Which comment to write, when, and what
it says is `issuesync` and `issuecomments`; this is the wire.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlparse

from . import forge_limits, forgechecks
from .errors import ApiError
from .forge import (
    GITHUB_API_HOST,
    MAX_RESPONSE_BYTES,
    TIMEOUT_SECONDS,
    _OPENER,
    _USER_AGENT,
    github_headers,
    is_pinned_host,
)
from .validation import IssueRef

log = logging.getLogger(__name__)

#: Pages of an issue's comments read to find one of ours by its marker. An
#: issue with more than 1,000 comments is answered by posting a new comment.
MAX_COMMENT_PAGES = 10
PAGE_SIZE = 100


# --------------------------------------------------------------------------
# errors: constant text, never the token
# --------------------------------------------------------------------------

class ForgeWriteError(ApiError):
    """Every write refusal. The run records the code and the sentence."""

    status_code = 502
    code = "writeback_failed"


class ForgeWriteForbidden(ForgeWriteError):
    status_code = 403
    code = "writeback_forbidden"


class ForgeWriteUnauthorized(ForgeWriteError):
    status_code = 401
    code = "writeback_unauthorized"


class ForgeWriteNotFound(ForgeWriteError):
    """The issue, comment or pull request is gone, or not visible to the token."""

    status_code = 404
    code = "writeback_not_found"


class ChecksForbidden(ForgeWriteForbidden):
    """A CI read refused: the credential cannot read checks, statuses or rules."""

    code = "checks_forbidden"


class RulesUnavailableOnPlan(ChecksForbidden):
    """GitHub's 403 for a rules read on a plan that has no rulesets.

    GITTOOLS-PLAN403 (#1188, measured 2026-10-22): a PRIVATE repository owned
    by a user on the free plan answers `rules/branches/{b}` with HTTP 403
    "Upgrade to GitHub Pro or make this repository public to enable this
    feature." whatever token asks, so no token can fix it and no ruleset can
    exist there. `branch_rules` reads it as "no rules". It is a
    `ChecksForbidden`, so a reader that does not expect it still fails closed.
    """


#: How GitHub's plan answer begins. The one statement of it in swarm-api:
#: `rules_unavailable_on_plan` is what every reader asks.
_PLAN_LACKS_RULESETS = "Upgrade to GitHub Pro or make this repository public"


def rules_unavailable_on_plan(status: int, message: str) -> bool:
    """Whether a rules read's answer is GitHub saying the plan has no rulesets.

    Exactly a 403 whose message starts with the plan sentence. Every other 403
    -- "Resource not accessible by personal access token", an SSO demand, a
    blocked repository -- is the credential or the repository, and refuses."""
    return status == 403 and message.strip().startswith(_PLAN_LACKS_RULESETS)


def _answer_message(raw: bytes) -> str:
    """The `message` of a GitHub error body, or "" when it has none."""
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else ""


#: Repositories whose plan answer has been logged by this process: once each,
#: since every push and `pr_checks` on such a repository reads it again.
_PLAN_LOGGED: set[str] = set()


class ForgeUnreachable(ForgeWriteError):
    """GitHub could not be reached, timed out, answered 5xx or rate-limited.

    WRITEBACK (2026-10-12): the one failure worth retrying. Its code stays
    `writeback_failed`, which every reader already knows; the type is what
    says a later attempt may work (`is_transient`). run_0d784c412dbc421a98ec
    sat CHECKING 12 hours on "could not be sent to GitHub (URLError)" with
    nothing telling that apart from a credential that would never work.
    """


class ForgeRateLimited(ForgeUnreachable):
    """GitHub limited the request (`forge_limits.rate_limit`): transient, and
    `reset` says when GitHub said it may be asked again (None when it did
    not). QUOTA-THROTTLE: a background sweep stops on the first one and
    holds itself until `reset` rather than asking again every park."""

    def __init__(self, message: str, *, reset: Any = None) -> None:
        super().__init__(message)
        self.reset = reset


def is_transient(error: BaseException) -> bool:
    """Whether a later attempt of the same request may work."""
    return isinstance(error, ForgeUnreachable)


def is_credential_refusal(error: BaseException) -> bool:
    """Whether GitHub refused the credential itself (401, or a 403 that is not
    a rate limit): nothing changes until a person changes the credential."""
    return isinstance(error, (ForgeWriteUnauthorized, ForgeWriteForbidden))


def _transport_name(exc: BaseException) -> str:
    """The type of a transport failure, and of its cause when urllib wraps one
    ("URLError: TimeoutError"). Types only: a message can quote the request."""
    name = type(exc).__name__
    reason = getattr(exc, "reason", None)
    if isinstance(exc, urllib.error.URLError) and isinstance(reason, BaseException):
        return f"{name}: {type(reason).__name__}"
    return name


#: What each write needs, named in the 403's sentence.
ISSUES_WRITE = "issues: write"
PULLS_WRITE = "pull_requests: write"
#: Creating a branch (#748): the same permission an agent's push needs.
CONTENTS_WRITE = "contents: write"
#: What each CI read needs.
CHECKS_READ = "checks: read"
STATUSES_READ = "statuses: read"
RULES_READ = "metadata: read"
#: Re-running a failed Actions job (lane SPEED-D2: a flake costs no agent).
ACTIONS_WRITE = "actions: write"
#: A pull request's file list, and a comparison of two commits.
PULLS_READ = "pull_requests: read"
CONTENTS_READ = "contents: read"
#: The agent's GitHub reads (lane AU-1, docs/design/agent-autonomy.md §3.1):
#: an issue and its comments, and an Actions run, its jobs and its waits.
ISSUES_READ = "issues: read"
ACTIONS_READ = "actions: read"
#: The GitHub Actions App's id: the `app.id` of every check run Actions
#: reports, and what a required check pinned to Actions names.
GITHUB_ACTIONS_APP_ID = 15368
#: The most Actions runs at one sha `actions_check_runs` reads the jobs of.
MAX_ACTIONS_RUNS = 30
#: The most of an issue's comments, a pull request's reviews or a run's jobs
#: an agent's read asks for, newest kept: what one answer has room for.
MAX_JOB_PAGES = 3
#: Pages of a pull request's files read: GitHub serves at most 3000, and a
#: conflict round's prompt names far fewer than the 300 these hold.
MAX_FILE_PAGES = 3

#: Pages of check runs read at one sha. A head with more than 500 check runs
#: is read as far as that, which is far past any repository's CI.
MAX_CHECK_PAGES = 5
#: Annotations read per failing check run: what the excerpt has room for.
MAX_ANNOTATIONS = 50
#: The most of a job log's tail read.
MAX_LOG_TAIL_BYTES = 16 * 1024
#: Where GitHub's Actions log redirect may point. The read carries no token,
#: but a URL outside these is not GitHub's log storage and is not read.
LOG_HOST_SUFFIXES = (".actions.githubusercontent.com", ".blob.core.windows.net")


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------

#: `(method, url, headers, body or None, timeout) -> (status, body bytes)` or
#: `(status, body bytes, response headers)`. Injected by the tests;
#: `_urllib_send` in production, which answers the headers too: GitHub's
#: `X-RateLimit-*` are what `forge_limits.QUOTAS` is kept from.
WriteSend = Callable[[str, str, dict[str, str], "bytes | None", float], "tuple[Any, ...]"]


def _response_headers(response: Any) -> dict[str, str]:
    raw = getattr(response, "headers", None)
    try:
        return {str(k): str(v) for k, v in raw.items()} if raw is not None else {}
    except Exception:
        return {}


def _urllib_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes, dict[str, str]]:
    if not is_pinned_host(url):
        raise ForgeWriteError(
            f"the forge client sends the tenant's credential to {GITHUB_API_HOST} only"
        )
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return (response.status, response.read(MAX_RESPONSE_BYTES + 1),
                    _response_headers(response))
    except urllib.error.HTTPError as answer:
        try:
            raw = answer.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raw = b""
        return answer.code, raw, _response_headers(answer)


#: `(url, headers, timeout) -> (status, Location or "")`: one authenticated GET
#: on api.github.com whose redirect is reported, never followed.
Locate = Callable[[str, dict[str, str], float], "tuple[int, str]"]
#: `(url, headers, timeout) -> (status, body bytes)`: one GET with NO token.
Fetch = Callable[[str, dict[str, str], float], "tuple[int, bytes]"]


def is_log_host(url: str) -> bool:
    """True only for an https URL on GitHub's Actions log storage."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host.endswith(s) for s in LOG_HOST_SUFFIXES)


def _urllib_locate(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    if not is_pinned_host(url):
        raise ForgeWriteError(
            f"the forge client sends the tenant's credential to {GITHUB_API_HOST} only"
        )
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, ""
    except urllib.error.HTTPError as answer:
        location = answer.headers.get("Location") if answer.headers else None
        return answer.code, location or ""


def _urllib_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    if not is_log_host(url) or any(k.lower() == "authorization" for k in headers):
        raise ForgeWriteError("a job log is read from GitHub's log storage, without a credential")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            # A store that ignores the Range answers 200 with the whole log:
            # read as much as any forge answer is held, and keep its tail.
            return response.status, response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as answer:
        return answer.code, b""


@dataclass(frozen=True)
class CommentRef:
    """One issue comment as GitHub answered it: its id, its page, its author."""

    id: int
    url: str
    login: str | None = None


@dataclass(frozen=True)
class IssueCreated:
    """One issue as GitHub answered it: its number and its page."""

    number: int
    url: str


@dataclass(frozen=True)
class BranchMerge:
    """GitHub's answer to merging a commit into a branch (`POST /merges`).

    `sha` the merge commit it made; None with `conflict` False when the
    branch already contained the commit (204); `conflict` True on a 409."""

    sha: str | None
    conflict: bool = False
    message: str = ""


@dataclass(frozen=True)
class PullMerge:
    """GitHub's answer to merging a pull request: merged, or why not."""

    merged: bool
    sha: str | None = None
    message: str = ""


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    url: str
    head_sha: str
    head_ref: str
    body: str
    state: str
    merged: bool
    #: The branch the pull request merges into: whose rules name the required checks.
    base_ref: str = ""
    #: Its title: a squash merge makes it a commit message on the base branch,
    #: where a closing keyword in it closes the issue (`issuesync`).
    title: str = ""
    #: `owner/repo` of the head's and the base's repositories, "" when GitHub
    #: named none: a head in another repository is a fork (`resolve_merge_pr`).
    head_repo: str = ""
    base_repo: str = ""
    #: When GitHub says it merged (ISO 8601, UTC), "" while it has not: the
    #: run page prints it beside "merged" (#503).
    merged_at: str = ""
    #: GitHub's `mergeable` -- True, False, or None while it has not computed
    #: it -- and `mergeable_state` (`dirty` on a conflict). The merge step's
    #: wake tick reads them first: GitHub runs no checks on a pull request
    #: that conflicts with its base (#1076).
    mergeable: bool | None = None
    mergeable_state: str = ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _other_branch_push(event: str | None, branch: str | None, head_ref: str | None) -> bool:
    """Whether an Actions run was triggered by a push to a known branch that
    is not `head_ref` -- another branch's result at a shared sha, not the
    pull request's (#2946). The worker's `merge._other_branch_push` (#2365)
    is the same rule. False when anything is unknown; an empty `head_ref`
    (`PullSnapshot` when GitHub named no head ref) is unknown, as the worker
    reads it."""
    if not head_ref:
        return False
    return event == "push" and branch is not None and branch != head_ref


def _full_name(side: Mapping[str, Any]) -> str:
    """`owner/repo` of a pull request's head or base, "" when GitHub named none."""
    repo = side.get("repo")
    name = repo.get("full_name") if isinstance(repo, Mapping) else None
    return name if isinstance(name, str) else ""


def _comment(data: Any, what: str) -> CommentRef:
    if not isinstance(data, dict) or _int(data.get("id")) is None:
        raise ForgeWriteError(f"GitHub's answer for {what} is not a comment")
    user = data.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    url = data.get("html_url")
    return CommentRef(
        id=int(data["id"]),
        url=url if isinstance(url, str) else "",
        login=login if isinstance(login, str) else None,
    )


def _issue(ref: IssueRef, data: Any, what: str) -> IssueCreated:
    if not isinstance(data, dict) or _int(data.get("number")) is None:
        raise ForgeWriteError(f"GitHub's answer for {what} is not an issue")
    number = int(data["number"])
    url = data.get("html_url")
    return IssueCreated(number=number, url=url if isinstance(url, str) else f"{ref.repository_url}/issues/{number}")


def _pull(ref: IssueRef, data: Any, what: str) -> PullSnapshot:
    if not isinstance(data, dict) or _int(data.get("number")) is None:
        raise ForgeWriteError(f"GitHub's answer for {what} is not a pull request")
    number = int(data["number"])
    head = data.get("head") if isinstance(data.get("head"), dict) else {}
    base = data.get("base") if isinstance(data.get("base"), dict) else {}
    url = data.get("html_url")
    body = data.get("body")
    title = data.get("title")
    state = data.get("state")
    merged_at = data.get("merged_at")
    return PullSnapshot(
        number=number,
        url=url if isinstance(url, str) else f"{ref.repository_url}/pull/{number}",
        head_sha=str(head.get("sha") or ""),
        head_ref=str(head.get("ref") or ""),
        body=body if isinstance(body, str) else "",
        state=state if state in ("open", "closed") else "open",
        merged=data.get("merged") is True,
        base_ref=str(base.get("ref") or ""),
        title=title if isinstance(title, str) else "",
        head_repo=_full_name(head),
        base_repo=_full_name(base),
        merged_at=merged_at if isinstance(merged_at, str) else "",
        mergeable=data.get("mergeable") if isinstance(data.get("mergeable"), bool) else None,
        mergeable_state=(data.get("mergeable_state")
                         if isinstance(data.get("mergeable_state"), str) else ""),
    )


class GitHubWriter:
    """GitHub's issue-comment and pull-request routes on api.github.com."""

    def __init__(
        self,
        *,
        send: WriteSend | None = None,
        timeout: float = TIMEOUT_SECONDS,
        locate: Locate | None = None,
        fetch: Fetch | None = None,
    ) -> None:
        self._send = send or _urllib_send
        self._timeout = timeout
        self._locate = locate or _urllib_locate
        self._fetch = fetch or _urllib_fetch

    @staticmethod
    def _url(ref: IssueRef, path: str) -> str:
        return (
            f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/"
            f"{quote(ref.repo, safe='')}/{path}"
        )

    def _call(
        self, method: str, url: str, token: str, what: str, *, needs: str,
        payload: dict[str, Any] | None = None, ok: tuple[int, ...] = (200,),
        forbidden: type[ForgeWriteForbidden] = ForgeWriteForbidden,
        plan_gated: bool = False,
    ) -> Any:
        """One request, its status mapped to a code, its JSON answer (or None).

        `plan_gated`: the route is one GitHub withholds on some plans, and its
        plan answer is `RulesUnavailableOnPlan` rather than `forbidden`."""
        headers = github_headers(token)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            sent = self._send(method, url, headers, body, self._timeout)
        except ForgeWriteError:
            raise
        except Exception as exc:
            # The type only: a transport's message can quote the request.
            raise ForgeUnreachable(
                f"{what} could not be sent to GitHub ({_transport_name(exc)})"
            ) from None
        status, raw = sent[0], sent[1]
        # QUOTA-THROTTLE: every answer updates the quota of the credential
        # the caller acts as (`forge_limits.acting_as`); the rate-limit
        # reading is `forge_limits`', the one place it is made.
        limited = forge_limits.QUOTAS.observe_answer(forge_limits.Answer(
            status, sent[2] if len(sent) > 2 and sent[2] else {}, raw))
        if limited is not None:
            when = forge_limits.until(limited)
            raise ForgeRateLimited(
                f"GitHub's rate limit held back {what} for now (HTTP {status})"
                + (f"; it resets at {when}" if when else ""), reset=limited.reset)
        if status == 401:
            raise ForgeWriteUnauthorized(
                f"GitHub refused the tenant's forge credential for {what} (HTTP 401): "
                "it is invalid or has expired; store a new one with "
                "scripts/create-secrets.sh --stdin"
            )
        if status >= 500:
            raise ForgeUnreachable(f"GitHub answered HTTP {status} for {what}")
        if status == 403 and plan_gated and rules_unavailable_on_plan(
                status, _answer_message(raw[:MAX_RESPONSE_BYTES])):
            raise RulesUnavailableOnPlan(
                f"GitHub keeps {what} for paid plans or public repositories (HTTP 403): "
                "no ruleset can exist on this repository's plan"
            )
        if status == 403:
            raise forbidden(
                f"GitHub refused the tenant's forge credential for {what} (HTTP 403): "
                f"the credential needs `{needs}` on the repository "
                "(docs/multi-tenancy.md, \"What the forge credential must be allowed\")"
            )
        if status in (404, 410):
            raise ForgeWriteNotFound(
                f"{what}: not found or not visible to the tenant's forge credential"
            )
        if status not in ok:
            raise ForgeWriteError(
                f"GitHub answered HTTP {status} for {what}"
                + (" (a redirect, which is never followed)" if 300 <= status < 400 else "")
            )
        if not raw:
            return None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ForgeWriteError(f"GitHub's answer for {what} is larger than this client holds")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ForgeWriteError(f"GitHub's answer for {what} is not JSON") from None

    # -- issue comments --------------------------------------------------------

    def create_comment(self, ref: IssueRef, body: str, token: str) -> CommentRef:
        what = f"a comment on {ref.short}"
        data = self._call(
            "POST", self._url(ref, f"issues/{int(ref.number)}/comments"), token, what,
            needs=ISSUES_WRITE, payload={"body": body}, ok=(200, 201),
        )
        return _comment(data, what)

    def edit_comment(self, ref: IssueRef, comment_id: int, body: str, token: str) -> CommentRef:
        what = f"comment {int(comment_id)} on {ref.short}"
        data = self._call(
            "PATCH", self._url(ref, f"issues/comments/{int(comment_id)}"), token, what,
            needs=ISSUES_WRITE, payload={"body": body},
        )
        return _comment(data, what)

    def delete_comment(self, ref: IssueRef, comment_id: int, token: str) -> None:
        self._call(
            "DELETE", self._url(ref, f"issues/comments/{int(comment_id)}"), token,
            f"comment {int(comment_id)} on {ref.short}", needs=ISSUES_WRITE, ok=(200, 204),
        )

    def find_comment(
        self, ref: IssueRef, marker: str, token: str, *, login: str | None = None
    ) -> CommentRef | None:
        """The OLDEST comment on the issue whose body BEGINS with `marker`.

        How a retry finds the comment it posted when the id it stored was
        lost (a write that succeeded and a Firestore patch that did not). The
        marker must open the body, which only this platform's rendering
        does: a plan or an issue that QUOTES the marker quotes it mid-text.
        With `login` -- the author GitHub reported for an earlier write of
        this run -- a comment by anyone else is not ours whatever it says.
        The oldest wins because ours was posted before anyone could read the
        run id it carries.
        """
        what = f"the comments on {ref.short}"
        url = self._url(ref, f"issues/{int(ref.number)}/comments")
        for page in range(1, MAX_COMMENT_PAGES + 1):
            entries = self._call(
                "GET", f"{url}?per_page={PAGE_SIZE}&page={page}", token, what,
                needs=ISSUES_WRITE,
            )
            if not isinstance(entries, list):
                raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                body = entry.get("body")
                if not isinstance(body, str) or not body.startswith(marker):
                    continue
                found = _comment(entry, what)
                if login is not None and found.login != login:
                    continue
                return found
            if len(entries) < PAGE_SIZE:
                return None
        return None

    # -- the issue itself (#646) -------------------------------------------------

    def close_issue(self, ref: IssueRef, token: str) -> None:
        """Close the issue as completed.

        Called only for an `already_on_main` run whose build steps' tables
        say every planned requirement is met on main (`issuesync`), after the
        table is posted: the comment is the evidence the close cites. Needs
        the same `issues: write` as the comments.
        """
        self._call(
            "PATCH", self._url(ref, f"issues/{int(ref.number)}"), token,
            f"closing {ref.short}", needs=ISSUES_WRITE,
            payload={"state": "closed", "state_reason": "completed"},
        )

    def create_issue(
        self, ref: IssueRef, *, title: str, body: str, labels: tuple[str, ...] = (), token: str
    ) -> IssueCreated:
        """Open an issue in `ref`'s repository (`ref.number` is not read).

        GitHub drops a label silently when the credential cannot set labels;
        the issue is still opened, and `find_issue` does not filter on one.
        """
        what = f"a new issue in {ref.repository}"
        data = self._call(
            "POST", self._url(ref, "issues"), token, what, needs=ISSUES_WRITE,
            payload={"title": title, "body": body, "labels": list(labels)}, ok=(201,),
        )
        return _issue(ref, data, what)

    def add_labels(
        self, ref: IssueRef, number: int, labels: Sequence[str], token: str
    ) -> tuple[str, ...]:
        """Add `labels` to issue `number`; return the ones dropped (FR3-HOLDLABEL).

        GitHub's `POST issues/{n}/labels` CREATES a label the repository does
        not have, so a misspelt or deleted label would appear on it unasked.
        The repository's labels are read once and only those it already has
        are posted; the rest come back, as `issue_create` reports
        `labels_dropped`. Nothing left to add is no write. Adding is additive
        on GitHub's side, so a retry that adds the same label again is
        harmless.
        """
        wanted = list(dict.fromkeys(str(label) for label in labels))
        existing = self.label_names(ref, token) if wanted else set()
        kept = [label for label in wanted if label in existing]
        dropped = tuple(label for label in wanted if label not in existing)
        if kept:
            self._call(
                "POST", self._url(ref, f"issues/{int(number)}/labels"), token,
                f"labelling {ref.repository}#{int(number)}", needs=ISSUES_WRITE,
                payload={"labels": kept},
            )
        return dropped

    def find_issue(self, ref: IssueRef, marker: str, token: str) -> IssueCreated | None:
        """The OLDEST issue in `ref`'s repository whose body BEGINS with `marker`.

        How a retry finds the issue it opened when the number it stored was
        lost (an open that succeeded and a Firestore write that did not):
        `find_comment`'s rule, for an issue. Newest first, at most
        `MAX_COMMENT_PAGES` pages: ours was opened moments before the write
        that lost it. Pull requests, which GitHub lists here too, are skipped.
        """
        what = f"the issues of {ref.repository}"
        url = self._url(ref, "issues")
        found: IssueCreated | None = None
        for page in range(1, MAX_COMMENT_PAGES + 1):
            entries = self._call(
                "GET", f"{url}?state=all&sort=created&direction=desc&per_page={PAGE_SIZE}&page={page}",
                token, what, needs=ISSUES_WRITE,
            )
            if not isinstance(entries, list):
                raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
            for entry in entries:
                if not isinstance(entry, dict) or "pull_request" in entry:
                    continue
                body = entry.get("body")
                if isinstance(body, str) and body.startswith(marker):
                    found = _issue(ref, entry, what)  # newest first: the last match is the oldest
            if len(entries) < PAGE_SIZE:
                break
        return found

    # -- pull requests ----------------------------------------------------------

    def read_pull(self, ref: IssueRef, number: int, token: str) -> PullSnapshot:
        what = f"{ref.repository}#{int(number)}"
        data = self._call(
            "GET", self._url(ref, f"pulls/{int(number)}"), token, what, needs=PULLS_WRITE,
        )
        return _pull(ref, data, what)

    # -- opening a pull request from a pushed branch (#748) ---------------------

    def default_branch(self, ref: IssueRef, token: str) -> str:
        """The repository's default branch, which the pull request merges into."""
        what = f"the repository {ref.repository}"
        data = self._call(
            "GET",
            f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}",
            token, what, needs=PULLS_WRITE,
        )
        branch = data.get("default_branch") if isinstance(data, dict) else None
        if not isinstance(branch, str) or not branch:
            raise ForgeWriteError(f"GitHub's answer for {what} names no default branch")
        return branch

    def branch_head(self, ref: IssueRef, branch: str, token: str) -> str | None:
        """The commit `branch` points at, or None when there is no such branch."""
        what = f"the branch {branch} of {ref.repository}"
        try:
            data = self._call(
                "GET", self._url(ref, f"git/ref/heads/{quote(branch, safe='/')}"), token, what,
                needs=PULLS_WRITE,
            )
        except ForgeWriteNotFound:
            return None
        target = data.get("object") if isinstance(data, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ForgeWriteError(f"GitHub's answer for {what} names no commit")
        return sha

    def tag_head(self, ref: IssueRef, tag: str, token: str) -> str | None:
        """The object `tag` points at (a commit, or an annotated tag's own
        object), or None when there is no such tag: admission's second look
        at a `repository_ref` that is no branch (#2726)."""
        what = f"the tag {tag} of {ref.repository}"
        try:
            data = self._call(
                "GET", self._url(ref, f"git/ref/tags/{quote(tag, safe='/')}"), token, what,
                needs=PULLS_WRITE,
            )
        except ForgeWriteNotFound:
            return None
        target = data.get("object") if isinstance(data, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ForgeWriteError(f"GitHub's answer for {what} names no object")
        return sha

    def create_branch(self, ref: IssueRef, branch: str, sha: str, token: str) -> bool:
        """Point a new `branch` at `sha`. True when made, False when it was
        already there at `sha`; any other branch of that name is refused."""
        what = f"the branch {branch} of {ref.repository}"
        try:
            self._call(
                "POST", self._url(ref, "git/refs"), token, what, needs=CONTENTS_WRITE,
                payload={"ref": f"refs/heads/{branch}", "sha": sha}, ok=(201,),
            )
            return True
        except (ForgeWriteForbidden, ForgeWriteUnauthorized):
            raise
        except ForgeWriteError:
            # 422 when it exists. Read it: at the same commit it is ours, from
            # a run that lost its answer; at any other it is not ours to move.
            if self.branch_head(ref, branch, token) == sha:
                return False
            raise ForgeWriteError(
                f"{what} already exists at another commit; it was not moved"
            ) from None

    def find_open_pull(self, ref: IssueRef, head: str, token: str) -> PullSnapshot | None:
        """The open pull request from `head` (a branch of this repository), if any."""
        what = f"the open pull requests from {head} in {ref.repository}"
        query = f"head={quote(ref.owner, safe='')}:{quote(head, safe='/')}&state=open&per_page=1"
        entries = self._call(
            "GET", f"{self._url(ref, 'pulls')}?{query}", token, what, needs=PULLS_WRITE,
        )
        if not isinstance(entries, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        return _pull(ref, entries[0], what) if entries else None

    def open_pull(
        self, ref: IssueRef, *, head: str, base: str, title: str, body: str, token: str,
        draft: bool = False,
    ) -> tuple[PullSnapshot, bool]:
        """Open a pull request from `head` into `base`: `(it, True)`, or
        `(the open one already there, False)` when GitHub refuses a second.

        An existing one is adopted as it stands, never edited: whoever opened
        it wrote its title and body.
        """
        what = f"a pull request from {head} in {ref.repository}"
        try:
            data = self._call(
                "POST", self._url(ref, "pulls"), token, what, needs=PULLS_WRITE,
                payload={"title": title, "head": head, "base": base, "body": body,
                         "draft": draft},
                ok=(201,),
            )
        except (ForgeWriteForbidden, ForgeWriteUnauthorized):
            raise
        except ForgeWriteError:
            existing = self.find_open_pull(ref, head, token)
            if existing is None:
                raise
            return existing, False
        return _pull(ref, data, what), True

    def edit_pull_body(
        self, ref: IssueRef, number: int, body: str, token: str, *, title: str | None = None
    ) -> None:
        """The pull request's body, and its title too when `title` is given."""
        payload = {"body": body} if title is None else {"body": body, "title": title}
        self._call(
            "PATCH", self._url(ref, f"pulls/{int(number)}"), token,
            f"the body of {ref.repository}#{int(number)}", needs=PULLS_WRITE,
            payload=payload,
        )

    # -- CI at a sha (#454's CI loop) ---------------------------------------------

    def branch_rules(self, ref: IssueRef, branch: str, token: str) -> list[dict[str, Any]]:
        """The rules that apply to `branch` (`rules/branches/{branch}`), as GitHub lists them."""
        what = f"the rules of {ref.repository}@{branch}"
        try:
            data = self._call(
                "GET", self._url(ref, f"rules/branches/{quote(branch, safe='')}?per_page=100"),
                token, what, needs=RULES_READ, forbidden=ChecksForbidden, plan_gated=True,
            )
        except RulesUnavailableOnPlan:
            # GITTOOLS-PLAN403: none can exist on this plan, so there are none.
            # The default-branch and own-branch tests are the caller's, and
            # do not depend on this read.
            if ref.repository not in _PLAN_LOGGED:
                _PLAN_LOGGED.add(ref.repository)
                log.info("%s has no branch rules: GitHub keeps rulesets for paid plans "
                         "or public repositories (HTTP 403 on rules/branches); read as none",
                         ref.repository)
            return []
        if not isinstance(data, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        return [rule for rule in data if isinstance(rule, dict)]

    def check_runs(self, ref: IssueRef, sha: str, token: str, *,
                   head_ref: str | None = None) -> list[dict[str, Any]]:
        """The LATEST check run of each name at `sha` (`filter=latest`), every page,
        for the pull request whose head branch is `head_ref`.

        When a name is listed more than once, or a run finished neither green
        nor tolerated, the Actions runs at `sha` are read in ONE more read
        (`_workflow_runs`), exactly when the worker's `merge.checks_at` reads
        them. Each run then carries `event`, the event that triggered its
        check suite, which `forgechecks` groups a check's runs by
        (CHECK-RULE, part of #1999), and its Actions run's conclusion
        (`forgechecks.mark_workflow_runs`), so a red gate in a run
        concurrency cancelled reads as pending here as it does in the merge
        step (#2582). A head whose every check ran once and passed costs
        nothing more.

        `commits/{sha}/check-runs` lists every check suite at the sha, from
        whichever branch pushed it, and swarm/ lanes branch from one another:
        at a9009a19 a sibling branch's newer green push ci-gate stood as the
        PR's push result beside the PR branch's red one, so the merge wake
        read green and never asked the free re-run (#2946). So with a
        `head_ref`, a run whose suite's Actions run was triggered by a push to
        a KNOWN branch other than `head_ref` is dropped, from the same read,
        exactly as the worker's `merge.checks_at` drops it
        (`_other_branch_push`, #2365). pull_request suites (a fork's names the
        fork's branch), suites whose branch is unknown (no `actions: read`,
        another App) and, without a `head_ref`, every run are kept.
        """
        what = f"the check runs at {ref.repository}@{sha[:12]}"
        url = self._url(ref, f"commits/{quote(sha, safe='')}/check-runs")
        runs: list[dict[str, Any]] = []
        for page in range(1, MAX_CHECK_PAGES + 1):
            data = self._call(
                "GET", f"{url}?filter=latest&per_page={PAGE_SIZE}&page={page}", token, what,
                needs=CHECKS_READ, forbidden=ChecksForbidden,
            )
            entries = data.get("check_runs") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise ForgeWriteError(f"GitHub's answer for {what} is not a list of check runs")
            runs += [entry for entry in entries if isinstance(entry, dict)]
            if len(entries) < PAGE_SIZE:
                break
        names = [run.get("name") for run in runs]
        red = any(forgechecks.check_run_finished(run)
                  and (run.get("conclusion") or "") not in forgechecks.OTHER_TOLERATED
                  for run in runs)
        if len(names) != len(set(names)) or red:
            workflow_runs = self._workflow_runs(ref, sha, token)
            suites = {row["check_suite_id"]: (_str(row.get("event")), _str(row.get("head_branch")))
                      for row in workflow_runs if row.get("check_suite_id") is not None}
            kept: list[dict[str, Any]] = []
            for run in runs:
                suite = run.get("check_suite")
                event, branch = (suites.get(suite.get("id"), (None, None))
                                 if isinstance(suite, dict) else (None, None))
                if _other_branch_push(event, branch, head_ref):
                    continue
                if event is not None:
                    run["event"] = event
                kept.append(run)
            runs = forgechecks.mark_workflow_runs(kept, workflow_runs)
        return runs

    def _workflow_runs(self, ref: IssueRef, sha: str, token: str) -> list[dict[str, Any]]:
        """The Actions runs at `sha` (`actions/runs?head_sha=`), every page.

        A credential without `actions: read` (403), or Actions off (404),
        reads as none: no run then carries an event, so each check suite is
        its own group and a green suite still never outvotes a red one, and
        no red run is read as cancelled.
        """
        what = f"the Actions runs at {ref.repository}@{sha[:12]}"
        url = self._url(ref, "actions/runs")
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_CHECK_PAGES + 1):
            try:
                data = self._call(
                    "GET", f"{url}?head_sha={quote(sha, safe='')}&per_page={PAGE_SIZE}&page={page}",
                    token, what, needs=ACTIONS_READ,
                )
            except (ForgeWriteForbidden, ForgeWriteNotFound):
                return []
            entries = data.get("workflow_runs") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise ForgeWriteError(f"GitHub's answer for {what} is not a list of workflow runs")
            rows += [entry for entry in entries if isinstance(entry, dict)]
            if len(entries) < PAGE_SIZE:
                break
        return rows

    def actions_check_runs(self, ref: IssueRef, sha: str, token: str, *,
                           head_ref: str | None = None) -> list[dict[str, Any]]:
        """The Actions jobs at `sha`, each shaped as the check run it is.

        #1188 (measured 2026-10-27): a fine-grained personal access token has
        no "Checks" permission at all, so `commits/{sha}/check-runs` on a
        PRIVATE repository answers it 403 whatever it is granted, while it can
        hold `actions: read`. An Actions check run is its job: the same id
        (`issueci`), the same name, the same status and conclusion, under the
        GitHub Actions App. So the jobs of each run at `sha` -- its latest
        attempt's -- are those check runs, each carrying its suite and event
        as `check_runs` gives them. What this cannot see is a check run
        another App reported; a caller says the reading came from here.

        Each job also carries its run's conclusion, from the same read
        (`forgechecks.mark_workflow_runs`, #2582), so a red gate in a run
        concurrency cancelled reads as pending, as `check_runs` reads it.
        And its run's id as `workflow_run_id`, the run whose failed jobs the
        merge wake tick's free re-run asks GitHub to re-run (#2841).

        With a `head_ref`, the jobs of a run triggered by a push to a KNOWN
        branch other than `head_ref` are not read: another branch's result at
        a shared sha, dropped as `check_runs` drops it (#2946) and the
        worker's `merge.checks_at` does (#2365). Each job carries its run's
        `head_branch` beside its `event`.
        """
        what = f"the Actions runs at {ref.repository}@{sha[:12]}"
        # The newest MAX_ACTIONS_RUNS runs, one read: each costs a jobs read
        # more. A required job only an older run had reads as pending, never green.
        runs = self._list(
            self._url(ref, f"actions/runs?head_sha={quote(sha, safe='')}"
                           f"&per_page={MAX_ACTIONS_RUNS}"),
            token, what, needs=ACTIONS_READ, key="workflow_runs")
        checks: list[dict[str, Any]] = []
        for run in runs:
            run_id = _int(run.get("id"))
            if run_id is None or run.get("head_sha") not in (None, sha):
                continue
            event, branch = _str(run.get("event")), _str(run.get("head_branch"))
            if _other_branch_push(event, branch, head_ref):
                continue
            for job in self.run_jobs(ref, run_id, token):
                job_id = _int(job.get("id"))
                if job_id is None:
                    continue
                checks.append({
                    "id": job_id, "name": job.get("name"), "head_sha": sha,
                    "status": job.get("status"), "conclusion": job.get("conclusion"),
                    "app": {"id": GITHUB_ACTIONS_APP_ID, "slug": "github-actions"},
                    "check_suite": {"id": run.get("check_suite_id")},
                    "workflow_run_id": run_id,
                    **({"event": event} if event is not None else {}),
                    **({"head_branch": branch} if branch is not None else {}),
                })
        return forgechecks.mark_workflow_runs(checks, runs)

    def commit_statuses(self, ref: IssueRef, sha: str, token: str) -> list[dict[str, Any]]:
        """The latest commit status of each context at `sha` (the combined status)."""
        what = f"the commit statuses at {ref.repository}@{sha[:12]}"
        data = self._call(
            "GET", self._url(ref, f"commits/{quote(sha, safe='')}/status?per_page={PAGE_SIZE}"),
            token, what, needs=STATUSES_READ, forbidden=ChecksForbidden,
        )
        entries = data.get("statuses") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list of statuses")
        return [entry for entry in entries if isinstance(entry, dict)]

    def rerun_job(self, ref: IssueRef, job_id: int, token: str) -> None:
        """Ask GitHub to re-run one Actions job at the same commit (SPEED-D2).

        An Actions check run's id is its job's id. GitHub answers 201 and
        makes a new check run of the same name at the same sha, which
        `check_runs` (`filter=latest`) then reads in place of the failed one.
        """
        self._call(
            "POST", self._url(ref, f"actions/jobs/{int(job_id)}/rerun"), token,
            f"a re-run of Actions job {int(job_id)}", needs=ACTIONS_WRITE,
            payload={}, ok=(201, 204),
        )

    def rerun_failed_jobs(self, ref: IssueRef, run_id: int, token: str) -> None:
        """Ask GitHub to re-run the failed jobs of Actions run `run_id` (#2841).

        GitHub re-runs every failed job of the run AND their dependents. A
        job re-run (`rerun_job`) re-runs a job and its dependents, never its
        needs: re-running a fan-in gate such as application.yml's `ci-gate`
        alone never re-ran the flaky job it aggregates (PR 2790 at
        d6548f96), so the gate read red again. Same `actions: write`.
        """
        self._call(
            "POST", self._url(ref, f"actions/runs/{int(run_id)}/rerun-failed-jobs"), token,
            f"a re-run of the failed jobs of Actions run {int(run_id)}", needs=ACTIONS_WRITE,
            payload={}, ok=(201, 204),
        )

    def pull_files(self, ref: IssueRef, number: int, token: str) -> list[str]:
        """The paths pull request `number` changes, up to MAX_FILE_PAGES pages."""
        what = f"the files of {ref.repository}#{int(number)}"
        names: list[str] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            data = self._call(
                "GET", self._url(ref, f"pulls/{int(number)}/files?per_page={PAGE_SIZE}&page={page}"),
                token, what, needs=PULLS_READ,
            )
            if not isinstance(data, list):
                raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
            names += [str(f["filename"]) for f in data
                      if isinstance(f, dict) and isinstance(f.get("filename"), str)]
            if len(data) < PAGE_SIZE:
                break
        return names

    # -- a merge train's branch and landings (TRAIN-WS, docs/merge-trains.md §4) --

    def merge_into_branch(
        self, ref: IssueRef, *, branch: str, head: str, message: str, token: str
    ) -> BranchMerge:
        """Merge commit `head` into `branch` on GitHub (`POST /merges`)."""
        what = f"a merge of {head[:12]} into {branch} of {ref.repository}"
        data = self._call(
            "POST", self._url(ref, "merges"), token, what, needs=CONTENTS_WRITE,
            payload={"base": branch, "head": head, "commit_message": message},
            ok=(201, 204, 409),
        )
        if data is None:
            return BranchMerge(sha=None)
        sha = data.get("sha") if isinstance(data, dict) else None
        if isinstance(sha, str) and sha:
            return BranchMerge(sha=sha)
        text = data.get("message") if isinstance(data, dict) else None
        return BranchMerge(sha=None, conflict=True,
                           message=text if isinstance(text, str) else "merge conflict")

    def merge_pull(
        self, ref: IssueRef, number: int, *, sha: str, title: str, message: str, token: str
    ) -> PullMerge:
        """Squash-merge pull request `number`, pinned to head `sha`.

        GitHub's refusals (405 not mergeable, 409 head moved, 422 invalid)
        are answers, returned unmerged with GitHub's message; anything else
        raises as every call here does."""
        what = f"the merge of {ref.repository}#{int(number)}"
        data = self._call(
            "PUT", self._url(ref, f"pulls/{int(number)}/merge"), token, what,
            needs=CONTENTS_WRITE,
            payload={"merge_method": "squash", "sha": sha, "commit_title": title,
                     "commit_message": message},
            ok=(200, 405, 409, 422),
        )
        data = data if isinstance(data, dict) else {}
        text = data.get("message")
        merged_sha = data.get("sha")
        return PullMerge(merged=data.get("merged") is True,
                         sha=merged_sha if isinstance(merged_sha, str) else None,
                         message=text if isinstance(text, str) else "")

    def commit_tree(self, ref: IssueRef, sha: str, token: str) -> str:
        """The tree commit `sha` points at (`git/commits/{sha}`)."""
        what = f"the commit {sha[:12]} of {ref.repository}"
        data = self._call(
            "GET", self._url(ref, f"git/commits/{quote(sha, safe='')}"), token, what,
            needs=PULLS_WRITE,
        )
        tree = data.get("tree") if isinstance(data, dict) else None
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            raise ForgeWriteError(f"GitHub's answer for {what} names no tree")
        return tree_sha

    def close_pull(self, ref: IssueRef, number: int, token: str) -> None:
        self._call(
            "PATCH", self._url(ref, f"pulls/{int(number)}"), token,
            f"closing {ref.repository}#{int(number)}", needs=PULLS_WRITE,
            payload={"state": "closed"},
        )

    def delete_branch(self, ref: IssueRef, branch: str, token: str) -> bool:
        """Delete `branch`; False when it was already gone."""
        try:
            self._call(
                "DELETE", self._url(ref, f"git/refs/heads/{quote(branch, safe='/')}"), token,
                f"the branch {branch} of {ref.repository}", needs=CONTENTS_WRITE, ok=(204,),
            )
        except ForgeWriteNotFound:
            return False
        return True

    def compare_files(self, ref: IssueRef, base: str, head: str, token: str) -> list[str]:
        """The paths changed from the merge base of `base` and `head` to `head`.

        GitHub's three-dot comparison, so `compare_files(ref, pr_head, base_branch)`
        is what the base branch changed since the pull request's branch left it.
        """
        what = f"the comparison {base[:40]}...{head[:200]} of {ref.repository}"
        data = self._call(
            "GET",
            self._url(ref, f"compare/{quote(base, safe='')}...{quote(head, safe='/')}"
                           f"?per_page={PAGE_SIZE}"),
            token, what, needs=CONTENTS_READ,
        )
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            raise ForgeWriteError(f"GitHub's answer for {what} lists no files")
        return [str(f["filename"]) for f in files
                if isinstance(f, dict) and isinstance(f.get("filename"), str)]

    def check_annotations(self, ref: IssueRef, check_run_id: int, token: str) -> list[dict[str, Any]]:
        """Up to MAX_ANNOTATIONS annotations of one check run."""
        what = f"the annotations of check run {int(check_run_id)}"
        data = self._call(
            "GET",
            self._url(ref, f"check-runs/{int(check_run_id)}/annotations?per_page={MAX_ANNOTATIONS}"),
            token, what, needs=CHECKS_READ, forbidden=ChecksForbidden,
        )
        if not isinstance(data, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        return [entry for entry in data if isinstance(entry, dict)]

    def job_log_tail(
        self, ref: IssueRef, job_id: int, token: str, *, limit: int = MAX_LOG_TAIL_BYTES
    ) -> str | None:
        """The last `limit` bytes of an Actions job's log, or None.

        None for every failure -- a token without `actions: read`, a job that
        is not an Actions job, a redirect to anywhere but GitHub's log
        storage, a transport error -- because the log only adds to the check
        run's own output. The token goes to api.github.com only; the log
        storage is read without it (module docstring).
        """
        limit = max(1, min(int(limit), MAX_LOG_TAIL_BYTES))
        try:
            status, location = self._locate(
                self._url(ref, f"actions/jobs/{int(job_id)}/logs"), github_headers(token),
                self._timeout,
            )
        except Exception as exc:
            log.info("job %s log not located (%s)", int(job_id), type(exc).__name__)
            return None
        if status not in (301, 302, 303, 307, 308) or not location or not is_log_host(location):
            return None
        try:
            status, raw = self._fetch(
                location,
                {"Range": f"bytes=-{limit}", "User-Agent": _USER_AGENT},
                self._timeout,
            )
        except Exception as exc:
            # The type only: the URL is signed and must not reach a log.
            log.info("job %s log not read (%s)", int(job_id), type(exc).__name__)
            return None
        if status not in (200, 206) or not raw:
            return None
        cut = len(raw) > limit
        text = raw[-limit:].decode("utf-8", errors="replace")
        if cut and "\n" in text:
            # A whole-file answer (no range support), cut mid-line: drop the partial line.
            text = text.split("\n", 1)[1]
        return text

    # -- the agent's GitHub reads (lane AU-1, docs/design/agent-autonomy.md §3.1) --
    #
    # Raw GitHub documents, each checked for its shape and nothing more:
    # `swarm_api.gittools` picks the fields it serves, bounds and redacts
    # them, and fences every string a third party wrote. Each is a GET on
    # api.github.com through `_call`, so every rule above holds for it.

    def _list(self, url: str, token: str, what: str, *, needs: str,
              key: str | None = None) -> list[dict[str, Any]]:
        data = self._call("GET", url, token, what, needs=needs)
        entries = data.get(key) if key is not None and isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        return [entry for entry in entries if isinstance(entry, dict)]

    def read_issue(self, ref: IssueRef, number: int, token: str) -> dict[str, Any]:
        """Issue `number` as GitHub answers it (a pull request carries `pull_request`)."""
        what = f"{ref.repository}#{int(number)}"
        data = self._call("GET", self._url(ref, f"issues/{int(number)}"), token, what,
                          needs=ISSUES_READ)
        if not isinstance(data, dict) or _int(data.get("number")) is None:
            raise ForgeWriteError(f"GitHub's answer for {what} is not an issue")
        return data

    def issue_comments_tail(self, ref: IssueRef, number: int, token: str, *,
                            keep: int = 50) -> list[dict[str, Any]]:
        """The last `keep` comments on issue (or pull request) `number`, oldest first.

        GitHub lists them oldest first, so every page up to MAX_COMMENT_PAGES
        is read and the tail kept."""
        what = f"the comments on {ref.repository}#{int(number)}"
        url = self._url(ref, f"issues/{int(number)}/comments")
        comments: list[dict[str, Any]] = []
        for page in range(1, MAX_COMMENT_PAGES + 1):
            entries = self._list(f"{url}?per_page={PAGE_SIZE}&page={page}", token, what,
                                 needs=ISSUES_READ)
            comments += entries
            if len(entries) < PAGE_SIZE:
                break
        return comments[-keep:] if keep > 0 else []

    def pull_reviews(self, ref: IssueRef, number: int, token: str, *,
                     keep: int = 50) -> list[dict[str, Any]]:
        """The last `keep` reviews of pull request `number`, oldest first."""
        what = f"the reviews of {ref.repository}#{int(number)}"
        url = self._url(ref, f"pulls/{int(number)}/reviews")
        reviews: list[dict[str, Any]] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            entries = self._list(f"{url}?per_page={PAGE_SIZE}&page={page}", token, what,
                                 needs=PULLS_READ)
            reviews += entries
            if len(entries) < PAGE_SIZE:
                break
        return reviews[-keep:] if keep > 0 else []

    def pull_file_stats(self, ref: IssueRef, number: int, token: str) -> list[dict[str, Any]]:
        """The files pull request `number` changes, with GitHub's counts, up to
        MAX_FILE_PAGES pages (`pull_files` is the names alone)."""
        what = f"the files of {ref.repository}#{int(number)}"
        files: list[dict[str, Any]] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            entries = self._list(
                self._url(ref, f"pulls/{int(number)}/files?per_page={PAGE_SIZE}&page={page}"),
                token, what, needs=PULLS_READ)
            files += entries
            if len(entries) < PAGE_SIZE:
                break
        return files

    def workflow_runs(self, ref: IssueRef, workflow: str, token: str, *,
                      branch: str | None = None, head_sha: str | None = None,
                      status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """The newest runs of `.github/workflows/<workflow>`, filtered as GitHub filters."""
        what = f"the runs of {workflow} in {ref.repository}"
        query = [f"per_page={max(1, min(int(limit), PAGE_SIZE))}"]
        for name, value in (("branch", branch), ("head_sha", head_sha), ("status", status)):
            if value:
                query.append(f"{name}={quote(value, safe='')}")
        url = self._url(ref, f"actions/workflows/{quote(workflow, safe='')}/runs?{'&'.join(query)}")
        return self._list(url, token, what, needs=ACTIONS_READ, key="workflow_runs")

    def read_run(self, ref: IssueRef, run_id: int, token: str) -> dict[str, Any]:
        what = f"Actions run {int(run_id)} of {ref.repository}"
        data = self._call("GET", self._url(ref, f"actions/runs/{int(run_id)}"), token, what,
                          needs=ACTIONS_READ)
        if not isinstance(data, dict) or _int(data.get("id")) is None:
            raise ForgeWriteError(f"GitHub's answer for {what} is not a run")
        return data

    def run_jobs(self, ref: IssueRef, run_id: int, token: str) -> list[dict[str, Any]]:
        """The jobs of run `run_id`'s latest attempt, up to MAX_JOB_PAGES pages."""
        what = f"the jobs of Actions run {int(run_id)}"
        jobs: list[dict[str, Any]] = []
        for page in range(1, MAX_JOB_PAGES + 1):
            entries = self._list(
                self._url(ref, f"actions/runs/{int(run_id)}/jobs?per_page={PAGE_SIZE}&page={page}"),
                token, what, needs=ACTIONS_READ, key="jobs")
            jobs += entries
            if len(entries) < PAGE_SIZE:
                break
        return jobs

    def pending_deployments(self, ref: IssueRef, run_id: int, token: str) -> list[dict[str, Any]]:
        """The environments run `run_id` is waiting at for a reviewer."""
        return self._list(self._url(ref, f"actions/runs/{int(run_id)}/pending_deployments"),
                          token, f"the pending deployments of Actions run {int(run_id)}",
                          needs=ACTIONS_READ)

    def compare_status(self, ref: IssueRef, base: str, head: str, token: str) -> str:
        """GitHub's `status` of `base...head`: `ahead` or `identical` when `head`
        contains `base`, `behind` or `diverged` when it does not."""
        what = f"the comparison {base[:40]}...{head[:40]} of {ref.repository}"
        data = self._call(
            "GET", self._url(ref, f"compare/{quote(base, safe='')}...{quote(head, safe='')}"
                                  "?per_page=1"),
            token, what, needs=CONTENTS_READ)
        status = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status, str):
            raise ForgeWriteError(f"GitHub's answer for {what} names no status")
        return status

    def label_names(self, ref: IssueRef, token: str) -> set[str]:
        """Every label the repository has, up to MAX_FILE_PAGES pages."""
        what = f"the labels of {ref.repository}"
        names: set[str] = set()
        for page in range(1, MAX_FILE_PAGES + 1):
            entries = self._list(self._url(ref, f"labels?per_page={PAGE_SIZE}&page={page}"),
                                 token, what, needs=ISSUES_READ)
            names |= {str(e["name"]) for e in entries if isinstance(e.get("name"), str)}
            if len(entries) < PAGE_SIZE:
                break
        return names

    def search_issues(self, ref: IssueRef, query: str, token: str, *,
                      limit: int = 30) -> list[dict[str, Any]]:
        """GitHub's issue search. `query` is the caller's whole `q`, its
        `repo:` qualifier included: this method adds and removes nothing."""
        what = f"an issue search in {ref.repository}"
        url = (f"https://{GITHUB_API_HOST}/search/issues?q={quote(query, safe='')}"
               f"&per_page={max(1, min(int(limit), PAGE_SIZE))}&sort=updated&order=desc")
        return self._list(url, token, what, needs=ISSUES_READ, key="items")

    def find_marked_issue(self, ref: IssueRef, marker: str, token: str, *,
                          state: str = "all", pages: int = MAX_COMMENT_PAGES) -> IssueCreated | None:
        """The OLDEST issue whose body's first three lines include `marker` as a line.

        `find_issue`'s rule widened from the first line to the first three, so
        a body may open with more than one marker (an agent's issue carries
        its attempt's and, optionally, its dedupe key's). A body that QUOTES
        a marker further down is not a match. Pull requests are skipped."""
        what = f"the issues of {ref.repository}"
        url = self._url(ref, "issues")
        found: IssueCreated | None = None
        for page in range(1, max(1, min(pages, MAX_COMMENT_PAGES)) + 1):
            entries = self._list(
                f"{url}?state={quote(state, safe='')}&sort=created&direction=desc"
                f"&per_page={PAGE_SIZE}&page={page}", token, what, needs=ISSUES_READ)
            for entry in entries:
                body = entry.get("body")
                if "pull_request" in entry or not isinstance(body, str):
                    continue
                if state != "all" and entry.get("state") != state:
                    continue  # GitHub filtered already; held here all the same
                if marker in body.split("\n", 3)[:3]:
                    found = _issue(ref, entry, what)  # newest first: the last match is the oldest
            if len(entries) < PAGE_SIZE:
                break
        return found
