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
from urllib.parse import quote

from .errors import ApiError
from .forge import (
    GITHUB_API_HOST,
    MAX_RESPONSE_BYTES,
    TIMEOUT_SECONDS,
    _OPENER,
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


#: What each write needs, named in the 403's sentence.
ISSUES_WRITE = "issues: write"
PULLS_WRITE = "pull_requests: write"


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

    def __init__(self, *, send: WriteSend | None = None, timeout: float = TIMEOUT_SECONDS) -> None:
        self._send = send or _urllib_send
        self._timeout = timeout

    @staticmethod
    def _url(ref: IssueRef, path: str) -> str:
        return (
            f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/"
            f"{quote(ref.repo, safe='')}/{path}"
        )

    def _call(
        self, method: str, url: str, token: str, what: str, *, needs: str,
        payload: dict[str, Any] | None = None, ok: tuple[int, ...] = (200,),
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
            raise ForgeWriteForbidden(
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

    # -- pull requests ----------------------------------------------------------

    def read_pull(self, ref: IssueRef, number: int, token: str) -> PullSnapshot:
        what = f"{ref.repository}#{int(number)}"
        data = self._call(
            "GET", self._url(ref, f"pulls/{int(number)}"), token, what, needs=PULLS_WRITE,
        )
        if not isinstance(data, dict) or _int(data.get("number")) is None:
            raise ForgeWriteError(f"GitHub's answer for {what} is not a pull request")
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        url = data.get("html_url")
        body = data.get("body")
        state = data.get("state")
        return PullSnapshot(
            number=int(data["number"]),
            url=url if isinstance(url, str) else f"{ref.repository_url}/pull/{int(number)}",
            head_sha=str(head.get("sha") or ""),
            head_ref=str(head.get("ref") or ""),
            body=body if isinstance(body, str) else "",
            state=state if state in ("open", "closed") else "open",
            merged=data.get("merged") is True,
        )

    def edit_pull_body(self, ref: IssueRef, number: int, body: str, token: str) -> None:
        self._call(
            "PATCH", self._url(ref, f"pulls/{int(number)}"), token,
            f"the body of {ref.repository}#{int(number)}", needs=PULLS_WRITE,
            payload={"body": body},
        )
