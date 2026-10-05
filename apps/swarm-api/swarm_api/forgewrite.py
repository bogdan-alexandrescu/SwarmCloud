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

WHAT THIS DOES NOT DO: decide anything. Which comment to write, when, and what
it says is `issuesync` and `issuecomments`; this is the wire.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlparse

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


#: What each write needs, named in the 403's sentence.
ISSUES_WRITE = "issues: write"
PULLS_WRITE = "pull_requests: write"
#: What each CI read needs.
CHECKS_READ = "checks: read"
STATUSES_READ = "statuses: read"
RULES_READ = "metadata: read"

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

#: `(method, url, headers, body or None, timeout) -> (status, body bytes)`.
#: Injected by the tests; `_urllib_send` in production.
WriteSend = Callable[[str, str, dict[str, str], "bytes | None", float], "tuple[int, bytes]"]


def _urllib_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    if not is_pinned_host(url):
        raise ForgeWriteError(
            f"the forge client sends the tenant's credential to {GITHUB_API_HOST} only"
        )
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as answer:
        try:
            raw = answer.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raw = b""
        return answer.code, raw


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


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    ) -> Any:
        """One request, its status mapped to a code, its JSON answer (or None)."""
        headers = github_headers(token)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._send(method, url, headers, body, self._timeout)
        except ForgeWriteError:
            raise
        except Exception as exc:
            # The type only: a transport's message can quote the request.
            raise ForgeWriteError(
                f"{what} could not be sent to GitHub ({type(exc).__name__})"
            ) from None
        if status == 401:
            raise ForgeWriteUnauthorized(
                f"GitHub refused the tenant's forge credential for {what} (HTTP 401): "
                "it is invalid or has expired; store a new one with "
                "scripts/create-secrets.sh --stdin"
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

    # -- pull requests ----------------------------------------------------------

    def read_pull(self, ref: IssueRef, number: int, token: str) -> PullSnapshot:
        what = f"{ref.repository}#{int(number)}"
        data = self._call(
            "GET", self._url(ref, f"pulls/{int(number)}"), token, what, needs=PULLS_WRITE,
        )
        if not isinstance(data, dict) or _int(data.get("number")) is None:
            raise ForgeWriteError(f"GitHub's answer for {what} is not a pull request")
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        url = data.get("html_url")
        body = data.get("body")
        title = data.get("title")
        state = data.get("state")
        return PullSnapshot(
            number=int(data["number"]),
            url=url if isinstance(url, str) else f"{ref.repository_url}/pull/{int(number)}",
            head_sha=str(head.get("sha") or ""),
            head_ref=str(head.get("ref") or ""),
            body=body if isinstance(body, str) else "",
            state=state if state in ("open", "closed") else "open",
            merged=data.get("merged") is True,
            base_ref=str(base.get("ref") or ""),
            title=title if isinstance(title, str) else "",
        )

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
        data = self._call(
            "GET", self._url(ref, f"rules/branches/{quote(branch, safe='')}?per_page=100"),
            token, what, needs=RULES_READ, forbidden=ChecksForbidden,
        )
        if not isinstance(data, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        return [rule for rule in data if isinstance(rule, dict)]

    def check_runs(self, ref: IssueRef, sha: str, token: str) -> list[dict[str, Any]]:
        """The LATEST check run of each name at `sha` (`filter=latest`), every page."""
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
        return runs

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
