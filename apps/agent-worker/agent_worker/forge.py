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

import base64
import functools
import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, TypeVar
from urllib.parse import quote, urlencode, urlparse

_UA = "swarmcloud-agent-worker"
_TIMEOUT = 30


class ForgeError(RuntimeError):
    """The forge could not be reached, or refused in a way worth surfacing."""


# ---------------------------------------------------------------------------
# Transient or permanent: one classification for every forge call a worker makes
# ---------------------------------------------------------------------------
#
# MEASURED 2026-10-04: issue run run_51e2e460eef54d208986 (#72) lost its
# review step to ONE "could not reach api.github.com: timed out". The issue
# fetch read that as "the issue is not there", ended the task
# INPUTS_UNAVAILABLE after a single attempt, and `on_step_failure=
# fail_workflow` cancelled the fix step behind it. The same timeout turned
# release 37017777271's acceptance red on 2026-10-02 ("no pull request was
# opened: could not reach the forge"). A blip and a refusal need opposite
# answers -- wait and ask again, or stop and say why -- so every forge call
# is put in one of two classes, here, once:
#
#   TRANSIENT (`ForgeUnavailable`): a connect or read timeout, a connection
#     reset, a DNS failure, 429, any 5xx, and GitHub's rate limits on a 403
#     -- the secondary limit says so in its message and usually carries
#     `Retry-After`; the primary one sends `x-ratelimit-remaining: 0`.
#   PERMANENT (any other `ForgeError`, or the caller's own refusal): 404
#     (absent, or invisible to this credential), 401 and 403 with no
#     rate-limit signal, 410, 422, a malformed reference, and a TLS
#     certificate the host could not prove -- asking again meets each of
#     these unchanged.
#
# A transient failure is retried IN THIS PROCESS a bounded number of times
# (`retry_transient`), within `max_in_worker_retry_delay_seconds` and the
# step's deadline. Past that the caller ends the ATTEMPT retryably, so the
# scheduler retries it within `max_attempts` and its capacity is released
# meanwhile -- never sleeping through a long wait (invariant 4).

#: 403 bodies GitHub sends for its rate limits: "You have exceeded a secondary
#: rate limit" and "API rate limit exceeded for ...". Matched on the message
#: because the secondary limit does not always send `Retry-After`.
_RATE_LIMIT_MESSAGE = re.compile(r"rate limit", re.IGNORECASE)


def _lowered(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _message_of(data: Any) -> str:
    return str(data.get("message") or "")[:300] if isinstance(data, dict) else ""


def transient_status(
    status: int, headers: Mapping[str, str] | None = None, data: Any = None
) -> bool:
    """True when an HTTP answer is an outage or a rate limit, not an answer."""
    if status == 429 or status >= 500:
        return True
    if status != 403:
        return False
    lowered = _lowered(headers)
    if "retry-after" in lowered:
        return True
    if lowered.get("x-ratelimit-remaining", "").strip() == "0":
        return True
    return bool(_RATE_LIMIT_MESSAGE.search(_message_of(data)))


def transient_network_error(exc: BaseException) -> bool:
    """True when a request that got no HTTP answer may get one if asked again.

    A timeout (connect or read), a reset or refused connection, a DNS failure,
    a connection the server dropped mid-answer. NOT a certificate the host
    could not prove: that is the host, or something pretending to be it, and
    it will be the same in a minute.
    """
    reason: Any = exc
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, str):
        return bool(_TRANSIENT_REASON.search(reason))
    return isinstance(reason, (OSError, http.client.HTTPException))


#: A network failure that arrives as TEXT rather than as an exception (a
#: `URLError` whose reason is a string, or a proxy's message): a timeout, a
#: connect failure, a DNS failure or a dropped connection. Only "timed out"
#: was read before, so "Failed to connect to github.com port 443" ended its
#: call as a permanent `ForgeError` (#623, 2026-10-05).
_TRANSIENT_REASON = re.compile(
    r"timed out|failed to connect|couldn't connect|could not resolve"
    r"|temporary failure in name resolution|name or service not known"
    r"|connection (?:reset|refused|closed|aborted)|network is unreachable"
    r"|no route to host|remote end closed connection",
    re.IGNORECASE,
)


def _network_reason(exc: BaseException) -> Any:
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        return exc.reason
    return exc


# -- the bounded in-process retry ---------------------------------------------

#: How many times one forge read is tried before the attempt gives up on it.
#: Four: the measured failures were single blips, which the second try clears;
#: two more cover a short outage, and past that the forge is down for longer
#: than this process should hold a concurrency slot waiting -- the scheduler's
#: retry, with the capacity released, is the right wait from there.
DEFAULT_READ_ATTEMPTS = 4
#: The first backoff; each one after doubles it (2, 4, 8 s: 14 s in all).
BACKOFF_BASE_SECONDS = 2.0

_T = TypeVar("_T")
_QUERY = re.compile(r"\?\S*")


@dataclass(frozen=True)
class RetryPolicy:
    """How a transient forge failure is retried in this process.

    `budget_seconds` bounds the WALL TIME from the first try, failed requests
    included, not only the sleeps: a try that timed out has already spent its
    request timeout. A wait (backoff or `Retry-After`) that would end past the
    budget is not slept at all; the failure goes to the caller, which ends the
    attempt retryably with the forge's `Retry-After` as the retry's delay.

    The budget is checked before each WAIT, not before each try, so the last
    try can run up to one request timeout (`_TIMEOUT`, or the issue fetch's
    own) past it. Deliberate: a budget that also reserved a request timeout
    would leave a 45 s budget room for one retry of a 30 s timeout at most,
    which is the blip this retry exists for. The overshoot is bounded by that
    one timeout.
    """

    attempts: int = DEFAULT_READ_ATTEMPTS
    budget_seconds: float = 45.0
    base_delay_seconds: float = BACKOFF_BASE_SECONDS
    sleep: Callable[[float], Any] = time.sleep
    clock: Callable[[], float] = time.monotonic
    #: Anything with `warning(message, **fields)`, or None.
    log: Any = None

    @classmethod
    def bounded(
        cls,
        *,
        attempts: int,
        max_in_worker_retry_delay_seconds: float,
        remaining_seconds: float,
        sleep: Callable[[float], Any] = time.sleep,
        log: Any = None,
    ) -> "RetryPolicy":
        """The platform's bound: the in-worker wait limit and the step's deadline."""
        budget = max(0.0, min(float(max_in_worker_retry_delay_seconds), float(remaining_seconds)))
        return cls(attempts=max(1, int(attempts)), budget_seconds=budget, sleep=sleep, log=log)


def loggable(text: str) -> str:
    """A failure's text for a log line: no query string, bounded."""
    return _QUERY.sub("", text)[:300]


def retry_transient(call: Callable[[], _T], *, policy: RetryPolicy | None, what: str) -> _T:
    """`call()`, tried again on `ForgeUnavailable` within `policy`; anything else raises.

    Each retry is logged once, with its attempt number and the failure's text
    stripped of any query string; the token is in no message this module
    writes. The `ForgeUnavailable` that ends it carries `tries`.
    """
    if policy is None:
        return call()
    started = policy.clock()
    attempt = 1
    while True:
        try:
            return call()
        except ForgeUnavailable as exc:
            exc.tries = attempt
            if attempt >= policy.attempts:
                raise
            if exc.retry_after_seconds is not None:
                wait = float(exc.retry_after_seconds)
            else:
                wait = policy.base_delay_seconds * (2 ** (attempt - 1))
            if (policy.clock() - started) + wait > policy.budget_seconds:
                raise
            if policy.log is not None:
                policy.log.warning(
                    "a forge call failed transiently; retrying it in-process",
                    what=what,
                    attempt=attempt,
                    attempts=policy.attempts,
                    wait_seconds=wait,
                    reason=loggable(str(exc)),
                )
            policy.sleep(wait)
            attempt += 1


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
    #: True when an adopted pull request was edited at all: retitled, a
    #: section appended to its body, or both (`retitled`, `appended`).
    updated: bool = False
    #: The title GitHub reports for an ADOPTED pull request (empty on one this
    #: call created). Read only so `open_pull_request` can tell a title a
    #: human may have written from the platform's own stale one (`retitle_if`,
    #: #259 follow-up); nothing else in this module or its caller uses it.
    title: str = ""
    #: The body GitHub reports for an ADOPTED pull request (empty on one this
    #: call created): what an amendment is appended to, and what the
    #: closing-line guard compares a new body against (#807).
    body: str = ""
    #: The adopted pull request's title was replaced.
    retitled: bool = False
    #: A section was appended to the adopted pull request's body (#807).
    appended: bool = False
    #: The closing-keyword lines a proposed body would have dropped, when the
    #: guard in `_update_pull_request` refused to send it (#807). Empty when
    #: no body was refused.
    body_refused: tuple[str, ...] = ()


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


# ---------------------------------------------------------------------------
# The one opener for every request that carries a forge token (#645, #307)
# ---------------------------------------------------------------------------
#
# `urllib.request.urlopen` follows 301/302/303/307/308 and builds the follow-up
# request with the original's headers, `Authorization` included, wherever the
# `Location` points. A forge answer of `302 Location: https://elsewhere/` would
# hand the tenant's token -- or the worker actions' installation token -- to a
# host that must never see it. So every request this worker makes with a forge
# token goes through `_NO_REDIRECT_OPENER`, via `open_without_redirects`: a 3xx
# comes back as an HTTPError carrying its own status and is never followed, to
# another host or to the same one. GitHub's API has no call here it answers
# with a redirect it needs followed; a renamed repository's 301 is refused and
# says so, which is the safe answer to a credential question.
#
# `_request` (the probe and the pull-request calls), `_open` (the pinned
# client's transport) and `issue._open` (the issue fetch) are the call sites;
# tests/unit/worker/test_forge_no_redirect.py fails if any of them, or any
# other module that imports this one, opens a URL another way.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Answer every redirect with None, so urllib raises it as an HTTPError.

    Passed to `build_opener`, a subclass of HTTPRedirectHandler REPLACES the
    default one, so no other handler follows it either.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class ConnectBound:
    """A connect timeout apart from the read timeout, and how often to retry it.

    urllib has one `timeout`, and http.client gives it to the TCP connect, the
    TLS handshake and every later read alike. A caller that wants a stalled
    connect to give up early and try again -- without cutting short a forge
    that connected and is slow to answer -- passes one of these to
    `open_without_redirects`. Only a timeout or a refused/reset connection is
    retried: a certificate the host could not prove (`ssl.SSLError`) and a
    DNS failure (`socket.gaierror`) are not `ConnectionError`s and raise at
    once, to the caller's own classification.
    """

    timeout: float
    tries: int = 1
    #: The wait before the second try; doubled before each later one.
    backoff_seconds: float = 0.0
    sleep: Callable[[float], Any] = time.sleep


#: The attribute of a `Request` that carries its `ConnectBound` to the handler.
#: urllib keeps `timeout` on the request the same way.
_CONNECT_BOUND_ATTR = "swarm_connect_bound"


class _ConnectBounded:
    """An http.client connection whose connect obeys a `ConnectBound`.

    `self.timeout` (urllib's `timeout`) is kept as the READ timeout: the
    connect runs under the bound's own timeout, and the connected socket --
    the TLS one, for https -- is then set to the read timeout. Subclassing the
    connection and overriding `connect` is the smallest correct way with
    urllib: the connect timeout is fixed at `socket.create_connection` and the
    TLS handshake inherits it, so nothing outside the connection can tell the
    two phases apart.
    """

    def __init__(self, *args: Any, connect_bound: ConnectBound, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        self._connect_bound = connect_bound

    def connect(self) -> None:
        bound = self._connect_bound
        read_timeout = self.timeout  # type: ignore[has-type]
        if read_timeout is socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
            read_timeout = socket.getdefaulttimeout()
        attempt = 1
        while True:
            self.timeout = bound.timeout
            try:
                super().connect()  # type: ignore[misc]
            except ssl.SSLError:
                self._drop_socket()
                raise
            except (TimeoutError, ConnectionError):
                self._drop_socket()
                if attempt >= bound.tries:
                    raise
                bound.sleep(bound.backoff_seconds * (2 ** (attempt - 1)))
                attempt += 1
                continue
            finally:
                self.timeout = read_timeout
            self.sock.settimeout(read_timeout)  # type: ignore[attr-defined]
            return

    def _drop_socket(self) -> None:
        # The socket only, never `close()`: http.client connects lazily inside
        # `send()`, after the request line is queued, and `close()` would reset
        # that request's state and fail the try that does connect.
        sock, self.sock = self.sock, None  # type: ignore[has-type]
        if sock is not None:
            sock.close()


class _ConnectBoundedHTTP(_ConnectBounded, http.client.HTTPConnection):
    pass


class _ConnectBoundedHTTPS(_ConnectBounded, http.client.HTTPSConnection):
    pass


_BOUNDED_CONNECTION = {
    http.client.HTTPConnection: _ConnectBoundedHTTP,
    http.client.HTTPSConnection: _ConnectBoundedHTTPS,
}


class _HonoursConnectBound:
    """Opens a request carrying a `ConnectBound` through `_ConnectBounded`.

    `do_open` is where urllib hands over the connection class, in every
    Python this worker runs; a request with no bound is opened exactly as the
    stock handler opens it.
    """

    def do_open(self, http_class, req, **http_conn_args):  # noqa: ANN001
        bound = getattr(req, _CONNECT_BOUND_ATTR, None)
        if bound is not None:
            http_class = functools.partial(_BOUNDED_CONNECTION[http_class], connect_bound=bound)
        return super().do_open(http_class, req, **http_conn_args)  # type: ignore[misc]


class _HTTPHandler(_HonoursConnectBound, urllib.request.HTTPHandler):
    pass


class _HTTPSHandler(_HonoursConnectBound, urllib.request.HTTPSHandler):
    pass


#: Subclasses of HTTPHandler and HTTPSHandler REPLACE the defaults in
#: `build_opener`, as `_NoRedirect` replaces the redirect handler.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect, _HTTPHandler, _HTTPSHandler)


def open_without_redirects(
    request: urllib.request.Request,
    *,
    timeout: float = _TIMEOUT,
    connect: ConnectBound | None = None,
) -> Any:
    """Send one request through the no-redirect opener. A 3xx raises as HTTPError.

    The only way a request carrying a forge token leaves this process. It
    reads `_NO_REDIRECT_OPENER` at call time, so a test that replaces it sees
    every call.

    With `connect`, `timeout` bounds each read only and the connect is bounded
    and retried by `connect` (`ConnectBound`); without it, `timeout` bounds
    the connect and each read alike, as urllib does.
    """
    setattr(request, _CONNECT_BOUND_ATTR, connect)
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


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
    host = urlparse(url).hostname
    try:
        with open_without_redirects(req, timeout=_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            # Never followed (`_NO_REDIRECT_OPENER`), and not read as an answer
            # either: the caller learns the forge redirected, and the token
            # went to the first host only. `Location` is not quoted -- it
            # names wherever the redirect pointed, which is not ours to log.
            exc.close()
            raise ForgeRedirectRefused(exc.code, urlparse(url).path) from None
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            parsed = {"message": raw[:500]}
        headers = dict(exc.headers.items()) if exc.headers else {}
        if transient_status(exc.code, headers, parsed):
            # The path only: a query string (`?head=owner:branch`) is not the
            # failure, and nothing here ever carries the token.
            message = _message_of(parsed)
            raise ForgeUnavailable(
                f"the forge answered {exc.code} to {urlparse(url).path}"
                + (f": {message}" if message else ""),
                retry_after_seconds=retry_after_from_headers(headers),
            ) from None
        return exc.code, parsed
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # OSError and HTTPException as well as URLError: a timeout DURING
        # `response.read()` is a bare TimeoutError, which escaped this
        # function before and ended a publish as a crash.
        reason = _network_reason(exc)
        if transient_network_error(exc):
            raise ForgeUnavailable(f"could not reach {host}: {reason}") from None
        raise ForgeError(f"could not reach {host}: {reason}") from None


def probe_repository(*, url: str, token: str | None) -> RepoAccess | None:
    """Ask the forge what this token may do. One GET, no side effect.

    None means the URL does not parse as a forge repository URL (no host, or
    fewer than two path segments); there is no host allow-list, so any host
    that parses is treated as GitHub. A missing credential, or a host the
    tenant's credential may not be sent to, returns a `RepoAccess` with
    `can_push=False` and the reason. Either way the caller harvests a patch
    and says so.
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
    if transient_status(status, None, data):
        # An outage is not "the token cannot push": raised, so a caller
        # retries it rather than reporting a read-only token.
        raise ForgeUnavailable(
            f"the forge answered {status}"
            + (f": {_message_of(data)}" if _message_of(data) else "")
        )
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


#: GitHub's own cap on a pull request body, in characters. An appended
#: section is cut to fit under it; the existing body never is (#807).
PR_BODY_MAX_CHARS = 65536

#: A line GitHub reads as closing an issue on merge -- `close`, `fix` or
#: `resolve` in any tense, an optional colon, then `#N` or `owner/repo#N` --
#: or a `part of #N` line, which is how the platform says an issue is NOT
#: closed. Either, dropped from a body, changes what the merge does to an
#: issue without anyone deciding it (#807: #775 lost three).
_CLOSING_LINE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+(?:[\w.-]+/[\w.-]+)?#\d+"
    r"|\bpart\s+of\s+(?:[\w.-]+/[\w.-]+)?#\d+",
    re.IGNORECASE,
)
#: Said where an appended section had to be cut to fit `PR_BODY_MAX_CHARS`.
_AMENDMENT_CUT_NOTE = "\n\n_[section cut to fit GitHub's body limit by the worker]_"


def closing_lines(body: str | None) -> list[str]:
    """Every line of `body`, stripped, that names a closing keyword or `part of #N`."""
    return [
        line.strip() for line in (body or "").splitlines() if _CLOSING_LINE_RE.search(line)
    ]


def dropped_closing_lines(current: str | None, proposed: str | None) -> list[str]:
    """The closing lines of `current` that `proposed` no longer has, in order."""
    kept = {line.strip() for line in (proposed or "").splitlines()}
    return [line for line in closing_lines(current) if line not in kept]


def appended_body(current: str | None, section: str) -> str | None:
    """`current`, verbatim, with `section` appended after a blank line -- or
    None when there is nothing to send.

    None when `section`'s first line (its heading) is already a line of
    `current`, so a publish retried by the same attempt appends nothing the
    second time, and when not even the heading fits under
    `PR_BODY_MAX_CHARS`. Only the SECTION is ever cut to fit: the existing
    body is a human's or another task's, and its closing lines are the point.
    """
    current = current or ""
    section = section.strip()
    if not section:
        return None
    heading = section.splitlines()[0].strip()
    if heading in {line.strip() for line in current.splitlines()}:
        return None
    base = current.rstrip()
    joiner = "\n\n" if base else ""
    body = f"{base}{joiner}{section}\n"
    if len(body) <= PR_BODY_MAX_CHARS:
        return body
    room = PR_BODY_MAX_CHARS - len(base) - len(joiner) - len(_AMENDMENT_CUT_NOTE) - 1
    if room < len(heading):
        return None
    return f"{base}{joiner}{section[:room].rstrip()}{_AMENDMENT_CUT_NOTE}\n"


def open_pull_request(
    *,
    access: RepoAccess,
    token: str,
    head: str,
    base: str,
    title: str,
    body: str,
    amendment: str | None = None,
    retitle_if: Callable[[str], bool] | None = None,
) -> PullRequest:
    """Open one pull request, or adopt the open one this branch already has.

    Idempotent on purpose. A task that is retried after a park pushes the same
    branch a second time, and a second pull request for the same work would be
    noise that a human has to close by hand. GitHub answers 422 for the
    duplicate; that is looked up rather than treated as a failure.

    `title` and `body` are used ONLY for a pull request this call opens. AN
    ADOPTED PULL REQUEST'S BODY IS NEVER REPLACED (#807): it is the
    implementer's, or a human's, and its `Closes #N` lines are what the merge
    closes issues by -- a CI fixer's republish that replaced it with its own
    provenance left three issues open on #775. `amendment`, when given, is
    APPENDED to the adopted body as one marked section (`appended_body`:
    once per heading, cut to fit, the existing text verbatim); without one
    the body is not touched. A pull request whose body was added to says so
    with `appended`.

    `retitle_if` retitles an adopted pull request whose CURRENT title it
    answers True for, to `title`. The worker passes two rules: the owner's
    2026-09-28 rule that a title never carries the task id (a pull request
    left with the old `[swarm] task_...` fallback), and #807's -- the agent
    wrote `pr-title.txt` and the current title is the worker's own default.
    Any other title, a human's or the implementer's, is kept.

    A refused update is not a failure -- the pull request is still adopted,
    with `updated` False.
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
            if not existing.number:
                return existing
            new_title = (
                title if retitle_if is not None and retitle_if(existing.title) else None
            )
            new_body = (
                appended_body(existing.body, amendment) if amendment is not None else None
            )
            if new_title is None and new_body is None:
                return existing
            return _update_pull_request(
                access=access, token=token, existing=existing, title=new_title, body=new_body
            )
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
    failure = ForgeUnavailable if transient_status(status, None, data) else ForgeError
    raise failure(f"could not open a pull request ({status}){': ' + message if message else ''}")


def _find_open_pull_request(
    *, access: RepoAccess, token: str, head: str
) -> PullRequest | None:
    ref = access.ref
    status, data = _request(
        f"{ref.api_base}/repos/{ref.owner}/{ref.name}/pulls"
        f"?head={ref.owner}:{head}&state=open&per_page=1",
        token=token,
    )
    if transient_status(status, None, data):
        # Not "there is none": that would turn the 422 above into "the forge
        # refused the pull request", which fails the step for good.
        raise ForgeUnavailable(f"could not look up the open pull request ({status})")
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
        body=str(first.get("body") or ""),
    )


def _update_pull_request(
    *,
    access: RepoAccess,
    token: str,
    existing: PullRequest,
    title: str | None,
    body: str | None,
) -> PullRequest:
    """PATCH an adopted pull request's title and/or body (each only when not
    None); `updated` says whether it took.

    THE GUARD (#807): a body that drops any closing-keyword or `part of #N`
    line the CURRENT body has is never sent, whoever built it. The title, if
    any, still goes; `body_refused` names the lines that would have been lost.
    """
    ref = access.ref
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    refused: tuple[str, ...] = ()
    if body is not None:
        refused = tuple(dropped_closing_lines(existing.body, body))
        if not refused:
            payload["body"] = body
    if not payload:
        return replace(existing, updated=False, body_refused=refused)
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
    took = status == 200
    return PullRequest(
        number=existing.number,
        url=existing.url,
        state=existing.state,
        created=False,
        updated=took,
        title=existing.title,
        body=existing.body,
        retitled=took and "title" in payload,
        appended=took and "body" in payload,
        body_refused=refused,
    )


# ---------------------------------------------------------------------------
# Issue comments: a review's minor findings on the tenant's wave epic (#638)
# ---------------------------------------------------------------------------

#: The most pages of an epic's comments read before refusing to file. The
#: dedup has to see EVERY comment: one it never read is a finding filed twice.
#: 30 pages of 100 is GitHub's own cap on a list read elsewhere in this module.
MAX_COMMENT_PAGES = 30
COMMENTS_PER_PAGE = 100


def _issue_comments_url(ref: RepoRef, number: int) -> str:
    if ref is None or not may_receive_forge_token(ref.host):
        # Asked where the token would leave, as `open_pull_request` asks (#307).
        raise ForgeError(
            "refusing to send the tenant's git credential to "
            f"{getattr(ref, 'host', None)}: it is sent only to github.com"
        )
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ForgeError(f"{number!r} is not an issue number")
    return f"{ref.api_base}/repos/{ref.owner}/{ref.name}/issues/{number}/comments"


def list_issue_comments(*, ref: RepoRef, token: str, number: int) -> list[str]:
    """The body of every comment on issue `number`, oldest first. GETs only.

    Raises `ForgeUnavailable` on an outage and `ForgeError` on anything else
    -- an issue that does not exist, a token that cannot read it, or more
    comments than `MAX_COMMENT_PAGES` pages -- because a partial list read as
    whole would let a finding be filed twice.
    """
    url = _issue_comments_url(ref, number)
    bodies: list[str] = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        status, data = _request(f"{url}?per_page={COMMENTS_PER_PAGE}&page={page}", token=token)
        if status != 200 or not isinstance(data, list):
            message = _message_of(data)
            failure = ForgeUnavailable if transient_status(status, None, data) else ForgeError
            raise failure(
                f"could not read the comments of issue #{number} ({status})"
                + (f": {message}" if message else "")
            )
        bodies += [str(c.get("body") or "") for c in data if isinstance(c, dict)]
        if len(data) < COMMENTS_PER_PAGE:
            return bodies
    raise ForgeError(
        f"issue #{number} has more than {MAX_COMMENT_PAGES * COMMENTS_PER_PAGE} comments; "
        "they were not all read, so nothing is filed on it"
    )


def create_issue_comment(*, ref: RepoRef, token: str, number: int, body: str) -> int:
    """Post one comment on issue `number`; its id. Never retried by this module:
    a POST whose answer was lost may have made the comment, and the caller's
    dedup on its next run is what finds it."""
    status, data = _request(
        _issue_comments_url(ref, number), token=token, method="POST", payload={"body": body}
    )
    if status == 201 and isinstance(data, dict):
        return int(data.get("id") or 0)
    message = _message_of(data)
    failure = ForgeUnavailable if transient_status(status, None, data) else ForgeError
    raise failure(
        f"could not comment on issue #{number} ({status})" + (f": {message}" if message else "")
    )


# ---------------------------------------------------------------------------
# The pinned forge client: the merge and post-verdict worker actions (#295)
# ---------------------------------------------------------------------------
#
# WHY A SECOND CLIENT. `_request` above serves the tenant's `-git` token, read
# by a worker an agent runs beside, against a host parsed out of the task's
# `repository_url`. The worker actions hold something worth far more -- an
# installation token minted from a GitHub App key no agent has held
# (docs/merge-step.md §0, §2.1) -- and so they talk to ONE host, fixed by the
# Job's Terraform-rendered environment and never by anything a tenant writes
# (§2.1b), through a client that:
#
#   * refuses at construction any host but `api.github.com`, by exact string
#     equality: `api.github.com.example` and `evil.example/api.github.com`
#     are not it;
#   * NEVER FOLLOWS A REDIRECT. urllib's default opener follows 301/302/303/
#     307/308 and resends the Authorization header to wherever `Location`
#     points. A redirect is how a credential leaves the pinned host, and
#     GitHub's API has no legitimate reason to answer any of these calls with
#     one, so a 3xx raises `ForgeRedirectRefused` instead (§2.1b, §6 row 33);
#   * follows `Link: rel="next"` only to the same host, over https, and stops
#     at a page and item cap rather than reading for ever (§5): a list it
#     could not finish is `PaginationCapReached`, never "checked and clean";
#   * carries the token in the Authorization header ONLY -- never a URL, a
#     query string, an exception message or a log line. It has no logger at
#     all, and its repr names the host and nothing else.

#: The one host the worker actions' credentials go to (§2.1b). The Job's
#: FORGE_HOST must be exactly this; an Enterprise Server host is not
#: supported by this client, so it is refused rather than guessed at.
PINNED_API_HOST = "api.github.com"
API_VERSION = "2022-11-28"
#: GitHub's own largest page.
PER_PAGE = 100
#: The most pages one list read follows before it refuses. 30 pages of 100 is
#: GitHub's own 3000-item cap on `pulls/{n}/files`, the longest list a merge
#: reads; every other list (check runs, reviews, rules) is far shorter.
MAX_PAGES = 30

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class ForgeRefused(ForgeError):
    """The client refused to make or continue a request. `code` names why."""

    code = "forge_refused"


class ForgeRedirectRefused(ForgeRefused):
    """The forge answered 3xx, and the client did not follow it."""

    code = "forge_redirect_refused"

    def __init__(self, status: int, path: str) -> None:
        super().__init__(
            f"the forge answered {status} to {path}; a credentialed request never "
            "follows a redirect (docs/merge-step.md §2.1b)"
        )
        self.status = status


class ForgeHostRefused(ForgeRefused):
    """A host, or a next-page URL, that is not the pinned one."""

    code = "forge_host_invalid"


class PaginationCapReached(ForgeRefused):
    """A list read reached its cap before its last page."""

    code = "pagination_cap"


class ForgeUnavailable(ForgeError):
    """Unreachable, 5xx, or rate-limited: an outage, not an answer.

    `retry_after_seconds` is the forge's own `retry-after` (or the time until
    `x-ratelimit-reset`), when it gave one.
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        #: How many times `retry_transient` tried before giving up; 1 untried.
        self.tries = 1


class ForgeAnswered(ForgeError):
    """The forge answered with a status the caller did not expect."""

    def __init__(self, status: int, path: str, message: str = "") -> None:
        super().__init__(
            f"the forge answered {status} to {path}" + (f": {message}" if message else "")
        )
        self.status = status


@dataclass(frozen=True)
class ForgeResponse:
    status: int
    data: Any
    headers: Mapping[str, str] = field(default_factory=dict)


#: `(request) -> (status, headers, body)`. The production transport is
#: `_open`; a test passes its own, which is the one seam the client has.
Transport = Callable[[urllib.request.Request], tuple[int, Mapping[str, str], bytes]]


def _open(request: urllib.request.Request) -> tuple[int, Mapping[str, str], bytes]:
    """Send one request with the no-redirect opener; a 3xx comes back as itself."""
    try:
        with open_without_redirects(request, timeout=_TIMEOUT) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, body
    except urllib.error.URLError as exc:
        if not transient_network_error(exc):
            raise ForgeError(f"could not reach the forge: {exc.reason}") from None
        raise ForgeUnavailable(f"could not reach the forge: {exc.reason}") from None
    except (OSError, http.client.HTTPException) as exc:
        raise ForgeUnavailable(f"could not reach the forge: {type(exc).__name__}") from None


def retry_after_from_headers(
    headers: Mapping[str, str], *, now: float | None = None
) -> int | None:
    """The forge's own wait, in seconds: `retry-after`, else until `x-ratelimit-reset`."""
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("retry-after")
    if raw is not None and str(raw).strip().isdigit():
        return int(str(raw).strip())
    reset = lowered.get("x-ratelimit-reset")
    if reset is not None and str(reset).strip().isdigit():
        current = time.time() if now is None else now
        return max(0, int(str(reset).strip()) - int(current))
    return None


class PinnedForgeClient:
    """Requests to `api.github.com` alone, with no redirect followed. See above."""

    def __init__(
        self,
        *,
        token: str,
        host: str = PINNED_API_HOST,
        transport: Transport | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if host != PINNED_API_HOST:
            # Not lower-cased first: FORGE_HOST is rendered by Terraform, and
            # one that is not exactly the pinned form is a misconfiguration to
            # refuse, not a spelling to repair.
            raise ForgeHostRefused(
                f"the forge host {host[:100]!r} is not {PINNED_API_HOST}; the worker "
                "actions send their credential to that host only"
            )
        if not token:
            raise ForgeRefused("no token: the pinned client never sends an anonymous request")
        self._host = host
        self.__token = token
        self._transport = transport or _open
        #: Applied to GETs only. A GET asks; a POST, PUT or DELETE that timed
        #: out may have been done, and the merge and the review post each
        #: have their own answer to that, which a blind resend is not.
        self._retry = retry

    def __repr__(self) -> str:
        return f"PinnedForgeClient(host={self._host!r})"

    @property
    def host(self) -> str:
        return self._host

    def _url(self, path: str, query: Mapping[str, Any] | None) -> str:
        if not path.startswith("/") or "//" in path or "://" in path or "?" in path or "#" in path:
            raise ForgeRefused("a request path is one absolute path on the pinned host")
        url = f"https://{self._host}{path}"
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        return url

    def _send(self, method: str, url: str, path: str, payload: Any) -> ForgeResponse:
        if method != "GET" or self._retry is None:
            return self._send_once(method, url, path, payload)
        return retry_transient(
            lambda: self._send_once(method, url, path, payload),
            policy=self._retry,
            what=f"GET {path}",
        )

    def _send_once(self, method: str, url: str, path: str, payload: Any) -> ForgeResponse:
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.__token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("User-Agent", _UA)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        status, headers, raw = self._transport(req)
        if 300 <= status < 400:
            raise ForgeRedirectRefused(status, path)
        text = raw.decode("utf-8", errors="replace") if raw else ""
        try:
            data = json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            data = {"message": text[:300]}
        if transient_status(status, headers, data):
            raise ForgeUnavailable(
                f"the forge answered {status} to {path}"
                + (f": {_message_of(data)}" if _message_of(data) else ""),
                retry_after_seconds=retry_after_from_headers(headers),
            )
        return ForgeResponse(status=status, data=data, headers=headers)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        query: Mapping[str, Any] | None = None,
    ) -> ForgeResponse:
        """One request. Raises on a redirect or an outage; returns any other answer."""
        return self._send(method, self._url(path, query), path, payload)

    def get(self, path: str, *, query: Mapping[str, Any] | None = None) -> ForgeResponse:
        return self.request("GET", path, query=query)

    def get_ok(self, path: str, *, query: Mapping[str, Any] | None = None) -> Any:
        """GET, and the body of a 200, or `ForgeAnswered`."""
        response = self.get(path, query=query)
        if response.status != 200:
            raise ForgeAnswered(response.status, path, _message_of(response.data))
        return response.data

    def paginate(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        key: str | None = None,
        max_items: int | None = None,
        max_pages: int = MAX_PAGES,
    ) -> list[Any]:
        """Every item of a list endpoint, to its last page, or a refusal.

        `key` names the list inside an object body (`check_runs`); None reads
        a body that is the list itself. REACHING `max_items` IS A REFUSAL even
        on the last page: GitHub truncates `pulls/{n}/files` at 3000 without
        saying so, so a list that long cannot be told from one cut short.
        """
        url = self._url(path, {**(query or {}), "per_page": PER_PAGE})
        items: list[Any] = []
        for _page in range(max_pages):
            response = self._send("GET", url, path, None)
            if response.status != 200:
                raise ForgeAnswered(response.status, path, _message_of(response.data))
            data = response.data
            page = data.get(key) if key is not None and isinstance(data, dict) else data
            if not isinstance(page, list):
                raise ForgeAnswered(response.status, path, "the body is not a list")
            items.extend(page)
            if max_items is not None and len(items) >= max_items:
                raise PaginationCapReached(
                    f"{path} reached {max_items} items; the list cannot be read completely"
                )
            link = {k.lower(): v for k, v in response.headers.items()}.get("link", "")
            match = _LINK_NEXT.search(link or "")
            if match is None:
                return items
            nxt = urlparse(match.group(1))
            if nxt.scheme != "https" or nxt.hostname != self._host or nxt.port is not None \
                    or nxt.username or nxt.password:
                raise ForgeHostRefused(
                    f"the next page of {path} is not on {self._host}; not followed"
                )
            url = match.group(1)
        raise PaginationCapReached(f"{path} has more than {max_pages} pages; not read further")

    def rules_for_branch(self, owner: str, repo: str, branch: str) -> list[dict[str, Any]]:
        """`GET /repos/{o}/{r}/rules/branches/{branch}`: every rule on the branch.

        GitHub's one endpoint that merges every applicable ruleset's rules and
        classic protection into one list (docs/merge-step.md §5.2), so the
        required checks are never read from one source without the other.
        The same read as auto-merge.yml's gate 3. Paginated.
        """
        rules = self.paginate(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/rules/branches/{quote(branch, safe='')}"
        )
        return [rule for rule in rules if isinstance(rule, dict)]


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    #: The App the rule pins the check to (`integration_id` in a ruleset's
    #: rule), or None when the rule names only the context.
    app_id: int | None


def required_status_checks(rules: list[dict[str, Any]]) -> list[RequiredCheck]:
    """The required checks every `required_status_checks` rule names, once each."""
    seen: dict[tuple[str, int | None], RequiredCheck] = {}
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        for check in parameters.get("required_status_checks") or []:
            if not isinstance(check, dict) or not isinstance(check.get("context"), str):
                continue
            raw = check.get("integration_id", check.get("app_id"))
            app_id = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
            seen.setdefault((check["context"], app_id), RequiredCheck(check["context"], app_id))
    return list(seen.values())


# -- the forge record a worker-action Job carries (§2.1b) --------------------


@dataclass(frozen=True)
class ForgeTarget:
    host: str
    owner: str
    repo: str
    review_app_id: int | None = None
    review_app_bot_id: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


_NAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def forge_target_from_env(
    environ: Mapping[str, str], *, need_bot_id: bool = False
) -> ForgeTarget:
    """FORGE_HOST/OWNER/REPO and the review App's ids, from the Job's environment.

    Terraform renders them onto the merge and post-verdict Jobs only
    (terraform/infra/locals.tf, `forge_env_names`); a task never supplies
    them. Missing or malformed is `ForgeHostRefused` (code
    `forge_host_invalid`), which the action ends CANNOT_START (§6 row 14).
    """
    host = (environ.get("FORGE_HOST") or "").strip()
    owner = (environ.get("FORGE_OWNER") or "").strip()
    repo = (environ.get("FORGE_REPO") or "").strip()
    if host != PINNED_API_HOST:
        raise ForgeHostRefused(
            f"FORGE_HOST is {host[:100]!r}, not {PINNED_API_HOST}" if host
            else "FORGE_HOST is not set on this Job"
        )
    if not _NAME_PART.match(owner) or not _NAME_PART.match(repo):
        raise ForgeHostRefused("FORGE_OWNER or FORGE_REPO is missing or malformed on this Job")

    def number(name: str) -> int | None:
        raw = (environ.get(name) or "").strip()
        if not raw:
            return None
        if not raw.isdigit():
            raise ForgeHostRefused(f"{name} is not a number")
        return int(raw)

    bot = number("REVIEW_APP_BOT_ID")
    if need_bot_id and bot is None:
        raise ForgeHostRefused("REVIEW_APP_BOT_ID is not set on this Job")
    return ForgeTarget(host, owner, repo, number("REVIEW_APP_ID"), bot)


# -- a GitHub App key, and the installation token minted from it (§2.1, §2.2) -


class AppRejected(ForgeError):
    """The App key was refused (401), or the App is not installed on the repository (404)."""


@dataclass(frozen=True)
class AppKey:
    app_id: int
    #: Never in a repr, an exception or a log line.
    private_key: str = field(repr=False)


def parse_app_key(payload: str) -> AppKey:
    """The JSON object a `-git-merge`/`-git-review` secret holds: an integer
    `app_id` and the App's PEM as `private_key` (docs/merge-step.md §2.1).
    A refusal names the shape, never the content."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise ForgeRefused("the App secret is not a JSON object of app_id and private_key")
    app_id = data.get("app_id")
    if isinstance(app_id, str) and app_id.strip().isdigit():
        app_id = int(app_id.strip())
    key = data.get("private_key")
    if isinstance(app_id, bool) or not isinstance(app_id, int) or not isinstance(key, str) \
            or "PRIVATE KEY" not in key:
        raise ForgeRefused("the App secret is not a JSON object of app_id and private_key")
    return AppKey(app_id=app_id, private_key=key)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def app_jwt(key: AppKey, *, now: int | None = None) -> str:
    """An RS256 JWT for the App, valid for nine minutes (GitHub allows ten).

    Issued 60 s in the past, GitHub's own advice for clock drift. Signed in
    this process with `cryptography`; the key reaches no file, environment
    variable, argv or subprocess.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(time.time() if now is None else now) - 60
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps(
        {"iat": issued, "exp": issued + 600, "iss": str(key.app_id)}, separators=(",", ":")
    ).encode())
    try:
        private = serialization.load_pem_private_key(key.private_key.encode("ascii"), password=None)
    except (ValueError, TypeError, UnicodeEncodeError):
        raise ForgeRefused("the App secret's private_key is not a PEM private key") from None
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ForgeRefused("the App secret's private_key is not an RSA key")
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64url(signature)}"


@dataclass(frozen=True)
class InstallationToken:
    #: Never in a repr, an exception or a log line.
    token: str = field(repr=False)
    expires_at: str
    installation_id: int
    app_id: int


def mint_installation_token(
    *,
    key: AppKey,
    owner: str,
    repo: str,
    permissions: Mapping[str, str],
    transport: Transport | None = None,
    now: int | None = None,
    retry: RetryPolicy | None = None,
) -> InstallationToken:
    """`GET /repos/{o}/{r}/installation` with the App's JWT, then one token for
    that repository alone with exactly `permissions` (§2.2 step 7).

    The installation read is also the proof the App is installed on THIS
    repository. 401 and 404 are `AppRejected` (§6 rows 16, 6a row 7).
    """
    jwt = app_jwt(key, now=now)
    # `retry` reaches the installation read only; the mint is a POST.
    client = PinnedForgeClient(token=jwt, transport=transport, retry=retry)
    del jwt
    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation"
    found = client.get(path)
    if found.status in (401, 403, 404):
        raise AppRejected(
            f"the App {key.app_id} was refused or is not installed on {owner}/{repo} "
            f"({found.status})"
        )
    if found.status != 200 or not isinstance(found.data, dict) \
            or not isinstance(found.data.get("id"), int):
        raise ForgeAnswered(found.status, path, _message_of(found.data))
    installation = int(found.data["id"])
    minted = client.request(
        "POST",
        f"/app/installations/{installation}/access_tokens",
        payload={"repositories": [repo], "permissions": dict(permissions)},
    )
    if minted.status in (401, 403, 404, 422):
        raise AppRejected(
            f"the App {key.app_id} could not mint a token for {owner}/{repo} ({minted.status})"
        )
    data = minted.data if isinstance(minted.data, dict) else {}
    token = data.get("token")
    if minted.status != 201 or not isinstance(token, str) or not token:
        raise ForgeAnswered(minted.status, "/app/installations/{id}/access_tokens",
                            _message_of(minted.data))
    return InstallationToken(
        token=token,
        expires_at=str(data.get("expires_at") or ""),
        installation_id=installation,
        app_id=key.app_id,
    )


def revoke_installation_token(client: PinnedForgeClient) -> bool:
    """`DELETE /installation/token` (§2.2 step 11). Never raises: an unrevoked
    token still expires within the hour; the revoke makes the window seconds."""
    try:
        return client.request("DELETE", "/installation/token").status == 204
    except ForgeError:
        return False
