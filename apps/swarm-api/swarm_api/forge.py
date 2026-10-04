"""The issue preview's forge read: the tenant's git token, and GitHub's issue API (#454).

The owner picked intake mock-up 1A on 2026-10-02 (PICKS.md), accepting what it
costs: swarm-api becomes the SECOND reader of a tenant's forge token
`swarm-tenant-<tenant>-git`, beside that tenant's worker. Before this the API
had no read path to any secret (`credentials.py`, "READ PATH. There is no read
path"), and that module still has none. This one reads exactly one secret per
request, for the caller's own tenant, and holds what it read in a local for
the length of one request.

THE TOKEN GOES IN ONE PLACE: the `Authorization` header of a GET to
`api.github.com` -- one for the preview; a bounded handful for an issue run's
open-work read (`read_open_work`: the repository's open issues, open pull
requests and their changed files, which the planner is shown so it can name
overlaps). Each of the ways it could leave from there is closed here:

  * NOT A RESPONSE, NOT AN ERROR. Every exception below carries a constant
    sentence and the issue reference; none is built from an upstream message
    or a caught exception's text, and every one is raised `from None`, so not
    even a traceback chains back to text that held it.
  * NOT A LOG LINE. Nothing here logs the request's headers, the token, or a
    caught exception; the route logs the outcome's code.
  * NOT ANOTHER HOST. The URL is built from a constant host, checked again in
    the transport, and a redirect is NEVER followed: urllib copies
    `Authorization` onto a redirected request wherever it points. (The
    worker's issue fetch follows a same-host redirect for a renamed
    repository; a preview of a renamed repository reads `read_failed` and
    says so, which is the cheaper failure.)
  * NOT THE BODY WE SERVE. The issue's own text is masked by the API's
    redaction with the token as a known literal, so an issue body somebody
    pasted the token into is masked like any other credential.

GitHub answers 404 for a private repository the token cannot see, exactly as
for one that does not exist, so `not_found` is worded "not found or not
visible" -- the preview cannot tell which, and must not pretend it can.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlparse

from swarm_common.models import Tenant

from .errors import ApiError
from .redaction import redact
from .validation import IssueRef

log = logging.getLogger(__name__)

#: The provider half of the tenant's forge secret name: `Tenant.secret_name`
#: (frozen) builds `swarm-tenant-<tenant>-git` from it, the name
#: `scripts/create-secrets.sh --stdin` stores and the worker reads.
GIT_PROVIDER = "git"

#: The one host the token is ever sent to.
GITHUB_API_HOST = "api.github.com"

#: The page the preview shows is a preview: the planner reads the whole issue
#: itself (`agent_worker.issue`). Past this the body is cut and says so.
MAX_PREVIEW_BODY_CHARS = 8_000

#: The most of one forge answer held in memory. An issue's JSON is a few KiB.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Per request. The console is waiting on it.
TIMEOUT_SECONDS = 10.0

_USER_AGENT = "swarmcloud-swarm-api"

# The open-work read: every open issue and pull request the planner is shown
# (#454: "reads ... all open issues and open PRs in the repository, to avoid
# conflicts and overlaps"). Each bound is explicit and each one that is hit is
# recorded on the result, so the planner is told the list is cut rather than
# left to believe it is whole. They bound three things at once: the time a
# `POST /v1/runs` waits on GitHub, the run document (Firestore's 1 MiB), and
# the planner's prompt, which `planner_prompt` bounds again by size.

#: Open issues listed, the planned issue itself not counted.
MAX_OPEN_ISSUES = 100
#: Open pull requests listed.
MAX_OPEN_PULLS = 50
#: Pull requests whose changed files are read: one or two GETs each. The rest
#: are listed with their titles and `files: null`.
MAX_PULLS_WITH_FILES = 30
#: Changed paths kept per pull request, and across all of them.
MAX_PR_FILES = 100
MAX_TOTAL_PR_FILES = 1_000
#: A title or a path past this is cut, with an ellipsis.
MAX_ITEM_TITLE_CHARS = 200
MAX_FILE_PATH_CHARS = 300
#: GitHub's largest page.
PAGE_SIZE = 100
#: `/issues` lists pull requests too, and they are dropped after reading, so
#: up to this many pages are read to find MAX_OPEN_ISSUES issues.
MAX_ISSUE_PAGES = 3
#: Wall clock for the whole read. Past it, no further pull request's files
#: are read; the lists already read are kept. The console is waiting.
OPEN_WORK_BUDGET_SECONDS = 30.0


# --------------------------------------------------------------------------
# errors: one code each, constant text, never the token
# --------------------------------------------------------------------------

class ForgeReadError(ApiError):
    """Every preview refusal. Each subclass is one code the console branches on."""


class IssueNotFound(ForgeReadError):
    status_code = 404
    code = "not_found"


class IssueNoAccess(ForgeReadError):
    status_code = 403
    code = "no_access"


class IssueIsPullRequest(ForgeReadError):
    status_code = 422
    code = "is_pull_request"


class NoForgeCredential(ForgeReadError):
    """The tenant has no `-git` secret, or it holds no usable version.

    409: the request is right and the tenant is not set up for it; an operator
    stores the token with `scripts/create-secrets.sh --stdin`.
    """

    status_code = 409
    code = "no_forge_credential"


class IssueReadFailed(ForgeReadError):
    status_code = 502
    code = "read_failed"


# --------------------------------------------------------------------------
# the tenant's token
# --------------------------------------------------------------------------

class ForgeTokens(Protocol):
    def token_for(self, tenant: Tenant) -> str: ...


class SecretManagerForgeTokens:
    """Reads `swarm-tenant-<tenant>-git`'s latest version, and only that.

    swarm-api's service account holds `secretAccessor` on each tenant's `-git`
    secret, by a per-secret binding (terraform/modules/secret_manager), and on
    no other secret of any tenant. The client is built on first use, never at
    construction, so `create_app()` builds no client.
    """

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def token_for(self, tenant: Tenant) -> str:
        from google.api_core import exceptions as gexc

        secret_id = tenant.secret_name(GIT_PROVIDER)
        name = f"projects/{self._project_id}/secrets/{secret_id}/versions/latest"
        try:
            version = self._secret_client().access_secret_version(request={"name": name})
        except (gexc.NotFound, gexc.FailedPrecondition):
            # No secret, or no enabled version: the tenant is not set up.
            raise NoForgeCredential(
                f"tenant {tenant.tenant_id!r} has no forge credential: store one as "
                f"{secret_id} with scripts/create-secrets.sh --stdin"
            ) from None
        except gexc.PermissionDenied:
            raise IssueReadFailed(
                f"swarm-api may not read {secret_id}: its accessor grant on the "
                "tenant's git secret is missing (terraform/modules/secret_manager)"
            ) from None
        except Exception as exc:
            raise IssueReadFailed(
                f"the tenant's forge credential could not be read ({type(exc).__name__})"
            ) from None
        try:
            token = bytes(version.payload.data).decode("utf-8").strip()
        except Exception:
            token = ""
        if not token:
            raise NoForgeCredential(
                f"tenant {tenant.tenant_id!r}'s forge credential {secret_id} is empty"
            )
        return token


# --------------------------------------------------------------------------
# the forge
# --------------------------------------------------------------------------

#: `(url, headers, timeout) -> (status, body bytes)`. Injected by the tests;
#: `_urllib_send` in production.
Send = Callable[[str, dict[str, str], float], "tuple[int, bytes]"]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. Returning None makes urllib raise the 3xx as an
    HTTPError, which `_urllib_send` answers as that status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def is_pinned_host(url: str) -> bool:
    """True only for an https URL on GITHUB_API_HOST: the one place a token may go."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == GITHUB_API_HOST


def github_headers(token: str) -> dict[str, str]:
    """The headers of every request that carries the tenant's token, read or write."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _urllib_send(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    if not is_pinned_host(url):
        # The URL is built from the constant, so this cannot fire; it is here
        # so that a change to how the URL is built cannot send the token
        # anywhere else either.
        raise IssueReadFailed(
            f"the forge client sends the tenant's credential to {GITHUB_API_HOST} only"
        )
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as answer:
        try:
            body = answer.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            body = b""
        return answer.code, body


@dataclass(frozen=True)
class IssueSnapshot:
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    comments: int
    url: str


@dataclass(frozen=True)
class OpenItem:
    """One open issue or pull request, as GitHub listed it, before masking."""

    number: int
    title: str
    #: A pull request's changed paths; None when they were not read (past
    #: MAX_PULLS_WITH_FILES, past the time budget, or the read failed).
    files: tuple[str, ...] | None = None
    files_truncated: bool = False


@dataclass(frozen=True)
class OpenWork:
    issues: tuple[OpenItem, ...]
    issues_truncated: bool
    pull_requests: tuple[OpenItem, ...]
    pull_requests_truncated: bool


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class GitHubIssues:
    """GitHub's issue API on api.github.com, read-only: one issue, and the open work."""

    def __init__(
        self,
        *,
        send: Send | None = None,
        timeout: float = TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        budget_seconds: float = OPEN_WORK_BUDGET_SECONDS,
    ) -> None:
        self._send = send or _urllib_send
        self._timeout = timeout
        self._clock = clock
        self._budget = budget_seconds

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return github_headers(token)

    def _get(self, url: str, token: str, what: str) -> bytes:
        """One GET, its status mapped to a code. `what` names the read in the error."""
        try:
            status, raw = self._send(url, self._headers(token), self._timeout)
        except ForgeReadError:
            raise
        except Exception as exc:
            # The type only. A transport's message can quote the request.
            raise IssueReadFailed(
                f"{what} could not be read from GitHub ({type(exc).__name__})"
            ) from None
        if status in (404, 410):
            raise IssueNotFound(
                f"{what} was not found or not visible to the tenant's forge "
                "credential (GitHub answers 404 for a private repository it cannot see)"
            )
        if status in (401, 403):
            raise IssueNoAccess(
                f"GitHub refused the tenant's forge credential for {what} "
                f"(HTTP {status}): it may lack access to the repository, or have expired"
            )
        if status != 200:
            raise IssueReadFailed(
                f"GitHub answered HTTP {status} for {what}"
                + (" (a redirect, which is never followed)" if 300 <= status < 400 else "")
            )
        return raw

    def fetch(self, ref: IssueRef, token: str) -> IssueSnapshot:
        url = (
            f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/"
            f"{quote(ref.repo, safe='')}/issues/{int(ref.number)}"
        )
        raw = self._get(url, token, ref.short)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IssueReadFailed(f"GitHub's answer for {ref.short} is larger than an issue")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = None
        if not isinstance(data, dict):
            raise IssueReadFailed(f"GitHub's answer for {ref.short} is not an issue")
        if "pull_request" in data:
            raise IssueIsPullRequest(
                f"{ref.short} is a pull request, not an issue; an issue run plans work "
                "from an issue"
            )
        labels = []
        for label in data.get("labels") or []:
            name = label.get("name") if isinstance(label, dict) else label
            if isinstance(name, str):
                labels.append(name)
        html_url = data.get("html_url")
        state = data.get("state")
        comments = data.get("comments")
        return IssueSnapshot(
            title=str(data.get("title") or ""),
            body=data.get("body") if isinstance(data.get("body"), str) else "",
            state=state if state in ("open", "closed") else "open",
            labels=tuple(labels),
            comments=comments if isinstance(comments, int) and not isinstance(comments, bool) else 0,
            # GitHub's own URL when it is the issue on github.com; ours otherwise.
            url=html_url if isinstance(html_url, str) and html_url.startswith(ref.repository_url + "/issues/") else ref.url,
        )

    # -- the open work --------------------------------------------------------

    def _repo_url(self, ref: IssueRef, path: str) -> str:
        return (
            f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/"
            f"{quote(ref.repo, safe='')}/{path}"
        )

    def _get_list(self, url: str, token: str, what: str) -> list[Any]:
        raw = self._get(url, token, what)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IssueReadFailed(f"GitHub's answer for {what} is larger than this read holds")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = None
        if not isinstance(data, list):
            raise IssueReadFailed(f"GitHub's answer for {what} is not a list")
        return data

    def _paged(
        self, url: str, token: str, what: str, *, keep: Callable[[Any], bool],
        cap: int, max_pages: int,
    ) -> tuple[list[Any], bool]:
        """Pages of `url` until `cap` kept entries, a short page, or `max_pages`.

        Truncated is True when an entry past the cap was seen, or when the
        last page read was full and no further page may be read: there may
        be more, and the planner is told the list is cut rather than whole.
        """
        kept: list[Any] = []
        joiner = "&" if "?" in url else "?"
        for page in range(1, max_pages + 1):
            entries = self._get_list(
                f"{url}{joiner}per_page={PAGE_SIZE}&page={page}", token, what
            )
            for entry in entries:
                if not keep(entry):
                    continue
                if len(kept) == cap:
                    return kept, True
                kept.append(entry)
            if len(entries) < PAGE_SIZE:
                return kept, False
        return kept, True

    def open_work(self, ref: IssueRef, token: str) -> OpenWork:
        """The repository's open issues (not `ref` itself) and open pull requests.

        WHAT A FAILURE DOES. A 401, 403, 404 or 410 on ANY read raises
        `IssueNoAccess`/`IssueNotFound` (owner decision, 2026-10-01: "A forge
        403 or 404 fails the planner"): a credential that cannot list the
        repository's work cannot be trusted to have listed it. Any other
        failure of either LIST (5xx, 429, a redirect, a transport error, an
        answer that is not a list) raises `IssueReadFailed`, because a plan
        made without the list is the plan #454 exists to stop. A failure of
        one pull request's FILES read other than those four statuses leaves
        that pull request listed with `files: None`, which the planner is
        told means "not read": its title is still the overlap signal.
        """
        repository = ref.repository
        issues, issues_cut = self._paged(
            self._repo_url(ref, "issues?state=open"), token,
            f"the open issues of {repository}",
            keep=lambda entry: (
                isinstance(entry, dict) and "pull_request" not in entry
                and _is_int(entry.get("number")) and entry.get("number") != ref.number
            ),
            cap=MAX_OPEN_ISSUES, max_pages=MAX_ISSUE_PAGES,
        )
        pulls, pulls_cut = self._paged(
            self._repo_url(ref, "pulls?state=open"), token,
            f"the open pull requests of {repository}",
            keep=lambda entry: isinstance(entry, dict) and _is_int(entry.get("number")),
            cap=MAX_OPEN_PULLS, max_pages=1,
        )
        started = self._clock()
        budget_left = MAX_TOTAL_PR_FILES
        listed_pulls: list[OpenItem] = []
        for index, pull in enumerate(pulls):
            number = int(pull["number"])
            files: tuple[str, ...] | None = None
            files_cut = False
            if (
                index < MAX_PULLS_WITH_FILES
                and budget_left > 0
                and self._clock() - started < self._budget
            ):
                files, files_cut = self._pull_files(ref, number, token, min(MAX_PR_FILES, budget_left))
                if files is not None:
                    budget_left -= len(files)
            listed_pulls.append(OpenItem(
                number=number, title=str(pull.get("title") or ""),
                files=files, files_truncated=files_cut,
            ))
        return OpenWork(
            issues=tuple(
                OpenItem(number=int(entry["number"]), title=str(entry.get("title") or ""))
                for entry in issues
            ),
            issues_truncated=issues_cut,
            pull_requests=tuple(listed_pulls),
            pull_requests_truncated=pulls_cut,
        )

    def _pull_files(
        self, ref: IssueRef, number: int, token: str, cap: int
    ) -> tuple[tuple[str, ...] | None, bool]:
        what = f"the changed files of {ref.repository}#{number}"
        try:
            entries, cut = self._paged(
                self._repo_url(ref, f"pulls/{number}/files"), token, what,
                keep=lambda entry: isinstance(entry, dict) and isinstance(entry.get("filename"), str),
                cap=cap, max_pages=2,
            )
        except (IssueNotFound, IssueNoAccess):
            raise
        except IssueReadFailed as failed:
            log.info("open-work read: %s not read (%s)", what, failed.code)
            return None, False
        return tuple(str(entry["filename"]) for entry in entries), cut


# --------------------------------------------------------------------------
# what the preview serves, and what a run stores of the open work
# --------------------------------------------------------------------------

_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def neutral_line(text: str, limit: int, *, literals: tuple[str, ...] = ()) -> str:
    """One line of forge text made safe to store, prompt with and post back.

    Masked by the API's redaction (with the token as a known literal), folded
    onto one line so it cannot fake the prompt's delimiters, its @-mentions
    broken with a zero-width space so a plan or status comment that quotes it
    later pings nobody, and cut to `limit` with an ellipsis.
    """
    masked = redact(text, extra=literals).text
    line = " ".join(_CONTROL.sub(" ", masked).split())
    line = _MENTION.sub("@\u200b", line)
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line


def read_open_work(
    ref: IssueRef, tenant: Tenant, *, tokens: ForgeTokens, issues: GitHubIssues,
    read_at: Any = None,
) -> dict[str, Any]:
    """The open-work snapshot an issue run stores and its planner is shown.

    Read with the run's own tenant's token (invariant 9), which lives in this
    frame only. Everything stored is masked and bounded: titles and paths go
    through `neutral_line`.
    """
    token = tokens.token_for(tenant)
    try:
        work = issues.open_work(ref, token)
        literals = (token,)
        snapshot: dict[str, Any] = {
            "repository": ref.repository,
            "read_at": read_at,
            "issues": [
                {"number": item.number,
                 "title": neutral_line(item.title, MAX_ITEM_TITLE_CHARS, literals=literals)}
                for item in work.issues
            ],
            "issues_truncated": work.issues_truncated,
            "pull_requests": [
                {
                    "number": item.number,
                    "title": neutral_line(item.title, MAX_ITEM_TITLE_CHARS, literals=literals),
                    "files": None if item.files is None else [
                        neutral_line(path, MAX_FILE_PATH_CHARS, literals=literals)
                        for path in item.files
                    ],
                    "files_truncated": item.files_truncated,
                }
                for item in work.pull_requests
            ],
            "pull_requests_truncated": work.pull_requests_truncated,
        }
    finally:
        token = ""
    return snapshot

def preview(ref: IssueRef, tenant: Tenant, *, tokens: ForgeTokens, issues: GitHubIssues) -> dict[str, Any]:
    """The preview document, masked and bounded. The token lives in this frame only."""
    token = tokens.token_for(tenant)
    try:
        issue = issues.fetch(ref, token)
        literals = (token,)
        title = redact(issue.title, extra=literals)
        body_text = issue.body
        truncated = len(body_text) > MAX_PREVIEW_BODY_CHARS
        if truncated:
            body_text = body_text[:MAX_PREVIEW_BODY_CHARS]
        body = redact(body_text, extra=literals, decoded=True)
        labels = [redact(label, extra=literals).text for label in issue.labels]
    finally:
        token = ""
    text = body.text + ("…" if truncated else "")
    return {
        **ref.to_dict(),
        "url": issue.url,
        "title": title.text,
        "body": text,
        "body_truncated": truncated,
        "body_redacted": bool(body.count or title.count),
        "labels": labels,
        "state": issue.state,
        "comments": issue.comments,
    }
