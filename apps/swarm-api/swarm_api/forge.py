"""The issue preview's forge read: the tenant's git token, and GitHub's issue API (#454).

The owner picked intake mock-up 1A on 2026-10-02 (PICKS.md), accepting what it
costs: swarm-api becomes the SECOND reader of a tenant's forge token
`swarm-tenant-<tenant>-git`, beside that tenant's worker. Before this the API
had no read path to any secret (`credentials.py`, "READ PATH. There is no read
path"), and that module still has none. This one reads exactly one secret per
request, for the caller's own tenant, and holds what it read in a local for
the length of one request.

THE TOKEN GOES IN ONE PLACE: the `Authorization` header of one GET to
`api.github.com`. Each of the ways it could leave from there is closed here:

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


def _urllib_send(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != GITHUB_API_HOST:
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


class GitHubIssues:
    """`GET /repos/{owner}/{repo}/issues/{n}` on api.github.com, nothing else."""

    def __init__(self, *, send: Send | None = None, timeout: float = TIMEOUT_SECONDS) -> None:
        self._send = send or _urllib_send
        self._timeout = timeout

    def fetch(self, ref: IssueRef, token: str) -> IssueSnapshot:
        url = (
            f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/"
            f"{quote(ref.repo, safe='')}/issues/{int(ref.number)}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            status, raw = self._send(url, headers, self._timeout)
        except ForgeReadError:
            raise
        except Exception as exc:
            # The type only. A transport's message can quote the request.
            raise IssueReadFailed(
                f"{ref.short} could not be read from GitHub ({type(exc).__name__})"
            ) from None
        if status in (404, 410):
            raise IssueNotFound(
                f"{ref.short} was not found or not visible to the tenant's forge "
                "credential (GitHub answers 404 for a private repository it cannot see)"
            )
        if status in (401, 403):
            raise IssueNoAccess(
                f"GitHub refused the tenant's forge credential for {ref.short} "
                f"(HTTP {status}): it may lack access to the repository, or have expired"
            )
        if status != 200:
            raise IssueReadFailed(
                f"GitHub answered HTTP {status} for {ref.short}"
                + (" (a redirect, which is never followed)" if 300 <= status < 400 else "")
            )
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


# --------------------------------------------------------------------------
# what the preview serves
# --------------------------------------------------------------------------

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
