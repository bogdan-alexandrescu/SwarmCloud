"""The SwarmCloud API, from outside the platform.

IDENTITY IS BORROWED, NOT REINVENTED. The API sits behind IAP, so every request
needs a Google-signed ID token, and `scripts/lib/common.sh::id_token` already
knows the four places one can come from. Re-deriving that here would be a fifth
place for the rule to drift, so this asks `gcloud` the same question the shell
does and honours the same `SWARM_ID_TOKEN` override.

NOTHING IS CACHED TO DISK. An ID token is a bearer credential with an hour of
life. It lives in this process and nowhere else.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

#: An ID token is good for an hour. Re-minting a few minutes early costs one
#: subprocess and avoids the failure mode where a long `tail` dies at the
#: 59-minute mark with a 401 that looks like a permission problem.
_TOKEN_TTL_SECONDS = 45 * 60


#: The states in which a task has stopped and will not move again. Stated ONCE,
#: here, at the bottom of the package: `cli` and `server` each had their own
#: copy, and a third one in `follow` would have made it three places where "has
#: this agent finished" is decided -- which is the shape every drift in this
#: repository has taken. `DEAD_LETTER` and `DEAD_LETTERED` are both carried
#: because the frozen enum spells it `dead_lettered` and older payloads spell
#: it `DEAD_LETTER`; accepting only one would leave a finished task polled
#: forever.
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER", "DEAD_LETTERED"}


class SwarmError(RuntimeError):
    """Something the operator needs to read, not a stack trace.

    `status` is the HTTP code when this came from a response rather than from
    a subprocess or a missing variable, and `edge` says the body was Google's
    HTML rather than the API's JSON. Both are carried as DATA because the
    caller has to tell "this deployment has no such route" (a 404 from the
    API) from "the API is broken" and from "the edge refused you before the
    API saw it" (a 404 from Google) -- and sniffing those three apart by
    searching the message string is how the distinction quietly stops working
    the first time the message is reworded.
    """

    def __init__(self, message: str, *, status: int | None = None, edge: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.edge = edge


def _run(argv: list[str], *, timeout: int = 60) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SwarmError(f"{argv[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SwarmError(f"{argv[0]} timed out after {timeout}s") from exc
    if done.returncode != 0:
        raise SwarmError(f"{' '.join(argv[:2])} failed: {done.stderr.strip()[:400]}")
    return done.stdout.strip()


def resolve_api_url() -> str:
    """Where the control plane is.

    `SWARM_API_URL` wins, then `API_URL` -- the same variable the scripts read,
    so a shell that is already configured for `make smoke` needs nothing extra.
    Otherwise Cloud Run is asked, which is the one answer that cannot be stale.
    """
    for name in ("SWARM_API_URL", "API_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    project = os.environ.get("PROJECT_ID", "").strip()
    region = os.environ.get("REGION", "").strip() or "us-central1"
    service = os.environ.get("API_SERVICE", "").strip() or "swarm-api"
    if not project:
        raise SwarmError(
            "set SWARM_API_URL, or PROJECT_ID so the URL can be read from Cloud Run"
        )
    url = _run([
        "gcloud", "run", "services", "describe", service,
        "--project", project, "--region", region, "--format=value(status.url)",
    ])
    if not url:
        raise SwarmError(f"Cloud Run has no URL for {service} in {region}")
    return url.rstrip("/")



#: Google's edges answer before the application does, and they answer in HTML.
#: Printing that page verbatim buries the one useful line under a document, and
#: the code alone is worse: a 404 here almost never means "no such route".
_HTML = ("<html", "<!doctype html", "<HTML")

#: IAP does NOT answer in HTML, whatever its Content-Type says. Measured on
#: 2026-09-22 against the deployed front door, all three under
#: `content-type: text/html`:
#:
#:     no Authorization       302  Invalid IAP credentials: empty token
#:     a garbage bearer       401  Invalid IAP credentials: Unable to parse JWT
#:     an unsigned JWT        401  Invalid IAP credentials: JWT signature is invalid
#:
#: They are one short sentence of plain text, so the HTML test above says
#: "this came from the application" about every real IAP refusal there is --
#: including `Invalid JWT audience`, the exact message this module exists to
#: translate. Matched on the family prefix rather than on a sentence, because
#: the sentence is Google's to reword.
_IAP_REFUSAL = "invalid iap credentials"


def _is_edge_page(body: str) -> bool:
    """True when this body is the edge answering, not the API's JSON."""
    stripped = body.strip()
    return stripped.lower().startswith(_HTML) or _is_iap_refusal(stripped)


def _is_iap_refusal(body: str) -> bool:
    return body.strip().lower().startswith(_IAP_REFUSAL)


def _is_edge(status: int, body: str) -> bool:
    """`edge` as the caller grades on it: did the API ever see this request?

    A redirect is included whatever its body, because the API does not issue
    one -- a 30x from the API's own host is the edge sending the caller to a
    sign-in page, which means the request was never delivered.
    """
    return 300 <= status < 400 or _is_edge_page(body)


def _explain(status: int, body: str) -> str:
    """Turn an edge's refusal into the sentence it was trying to be."""
    stripped = body.strip()
    if _is_iap_refusal(stripped):
        # Google's own clause says WHICH way the credential was wrong, which
        # the remedy does not; the remedy says what to do about it, which
        # Google's does not. Both, or the reader has half an answer.
        return (
            f"IAP refused this before the API saw it -- {stripped[:200]}. A bearer "
            "token minted for anything other than the IAP OAuth client id reads as "
            "`Invalid JWT audience`; set SWARM_IAP_CLIENT_ID and SWARM_IMPERSONATE_SA, "
            "and check your account is on the IAP access list"
        )

    if 300 <= status < 400:
        return (
            f"the edge answered with a redirect (HTTP {status}), which the API never "
            "does -- this request reached a sign-in page, not the platform. It was "
            "NOT followed: urllib carries the Authorization header across a redirect, "
            "including to another host. Authenticate to IAP first"
        )

    if not _is_edge_page(body):
        try:
            parsed = json.loads(stripped)
            return str(parsed.get("detail") or parsed.get("message") or stripped[:300])
        except json.JSONDecodeError:
            return stripped[:300] or f"HTTP {status}"

    if status == 404:
        return (
            "an HTML 404 from Google's edge, not from the API. This is what "
            "Cloud Run returns when a service's INGRESS refuses the caller -- "
            "`internal-and-cloud-load-balancing` rejects a direct or proxied "
            "call from outside the VPC. Reach it through its load balancer, or "
            "deploy the solo profile, which does not restrict ingress"
        )
    if status in (401, 403):
        return (
            "an HTML sign-in page, so IAP rejected this before the API saw it. "
            "A bearer token minted for anything other than the IAP OAuth client "
            "id reads as `Invalid JWT audience`; set SWARM_IAP_CLIENT_ID and "
            "SWARM_IMPERSONATE_SA, and check your account is on the IAP list"
        )
    return f"an HTML error page from Google's edge (HTTP {status}), not from the API"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. Returning None is how urllib is told to stop.

    TWO REASONS, AND THE FIRST IS THE CREDENTIAL. CPython's redirect handler
    copies every header onto the new request except the content ones -- the
    Authorization header included -- and it never compares hosts, so it goes
    to whatever the `Location` names. Measured on 2026-09-22 over loopback
    against the DEFAULT opener: the redirect target received the bearer
    verbatim. An ID token minted for swarm-api would therefore reach
    accounts.google.com, which is the cross-audience mistake commit 411d086
    fixed one layer down and `fetch_accounts` refuses to make one layer up.

    THE SECOND IS THAT THE FOLLOWED REQUEST SUCCEEDS. Measured on 2026-09-22:
    an unauthenticated GET to the front door is a 302 that resolves, in two
    hops, to a 200 carrying Google's sign-in HTML. Every error path in this
    file hangs off `HTTPError`, so that arrived as a success and `json.loads`
    raised `JSONDecodeError` -- not a `SwarmError`, so `swarm doctor`, the
    command whose whole job is to explain this, ended in a traceback.

    With the redirect refused, urllib raises the 30x as an `HTTPError` and it
    takes the ordinary explained path.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects())


def _open(request: urllib.request.Request, timeout: int):
    """The one place this package speaks HTTP.

    A function rather than a call site so that a test can put a real
    application behind it, which is the only way the shape of a real response
    ever gets checked. `urllib.request.urlopen` is not used: it builds its own
    opener, with the redirect handler above replaced by the default one.
    """
    return _OPENER.open(request, timeout=timeout)


@dataclass
class _Token:
    value: str
    minted_at: float


def unwrap_task(payload: Any) -> dict[str, Any]:
    """The task document out of whatever the route wrapped it in.

    `POST /v1/tasks` answers `{"task": {...}, "scheduler_woken": ...}`,
    `GET /v1/tasks/{id}` answers `{"task": {...}}` and
    `POST /v1/tasks/{id}/cancel` answers `{"task": {...}, ...}`. Reading the
    envelope as the task does not error -- every field comes back absent, so a
    running task reads as state `None`, no commits, no patch and no pull
    request, which is indistinguishable from a task that has not started.

    The bare shape is still accepted: this is a bridge to whatever swarm-api a
    person has deployed, and unwrapping must not be the thing that breaks
    against one that does not wrap.
    """
    if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
        return payload["task"]
    return payload if isinstance(payload, dict) else {}


def task_id_of(task: dict[str, Any]) -> str:
    """A task's id. The API calls it `id`; this package called it `task_id`.

    `codec.task_to_api` emits `id`, after the frozen `Task.id`. Nothing under
    `/v1/tasks` ever emits `task_id` on a task document -- it appears only as
    a sibling key on the events, attempts and artifacts envelopes -- so the
    fallback is for those, not for a variant of this shape.
    """
    return str(task.get("id") or task.get("task_id") or "")


def service_name() -> str:
    return os.environ.get("API_SERVICE", "").strip() or "swarm-api"


def region() -> str:
    return os.environ.get("REGION", "").strip() or "us-central1"


def project_id() -> str:
    value = os.environ.get("PROJECT_ID", "").strip()
    if not value:
        value = _run(["gcloud", "config", "get-value", "project"])
    if not value or value == "(unset)":
        raise SwarmError("no project: set PROJECT_ID, or run `gcloud config set project`")
    return value


class SwarmClient:
    """A connection to one SwarmCloud, over whichever tier this machine has.

    THE TIER IS DETECTED, NOT CONFIGURED. The commonest failure for someone
    trying this for the first time is not choosing the wrong tier -- it is not
    knowing tiers exist, and reading "Invalid JWT audience" as a bug in the
    platform rather than as "your laptop cannot mint that kind of token".

    On the PROXY tier this object owns a subprocess, so it is a context manager.
    Using it without `with` still works for a single call; the proxy is then
    reaped when the process exits.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        audience: str | None = None,
        connect: bool = True,
    ) -> None:
        from . import auth as _auth  # local: avoids a circular import at module load

        self.detection = _auth.detect()
        self.tier = self.detection.tier
        self._proxy: _auth.Proxy | None = None
        self._token: _Token | None = None
        self._explicit_url = base_url or os.environ.get("SWARM_API_URL", "").strip() or None

        if self.tier is _auth.Tier.IAP:
            # The audience IAP accepts is its OAuth client id, never the
            # service URL. Getting this wrong is the "Invalid JWT audience"
            # that sends people looking at their IAM policy.
            self._audience = os.environ.get("SWARM_IAP_CLIENT_ID", "").strip()
        else:
            self._audience = audience or os.environ.get("API_AUDIENCE", "").strip() or None

        self.base_url = ""
        if connect:
            self.connect()

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        from . import auth as _auth

        if self.tier is _auth.Tier.PROXY and not self._explicit_url:
            self._proxy = _auth.Proxy(service_name(), region(), project_id())
            self.base_url = self._proxy.start().rstrip("/")
        else:
            self.base_url = (self._explicit_url or resolve_api_url()).rstrip("/")
        if not self._audience:
            self._audience = self.base_url

    def close(self) -> None:
        if self._proxy is not None:
            self._proxy.stop()
            self._proxy = None

    def __enter__(self) -> "SwarmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identity ----------------------------------------------------------
    @property
    def sends_own_token(self) -> bool:
        """False on the proxy tier, where gcloud supplies the Authorization
        header itself. Sending a second one would replace the only credential
        Cloud Run will accept with one it will not."""
        from . import auth as _auth

        return self.tier is not _auth.Tier.PROXY

    def _id_token(self) -> str:
        from . import auth as _auth

        now = time.monotonic()
        if self._token is not None and now - self._token.minted_at < _TOKEN_TTL_SECONDS:
            return self._token.value
        value = _auth.id_token_for(self._audience or self.base_url, tier=self.tier)
        if not value:
            raise SwarmError("no ID token available; run: gcloud auth login")
        self._token = _Token(value=value, minted_at=now)
        return value

    def access_token(self) -> str:
        """An OAuth access token, for GCS. Distinct from the ID token above:
        one proves WHO you are to IAP, the other authorises a bucket read."""
        override = os.environ.get("SWARM_ACCESS_TOKEN", "").strip()
        if override:
            return override
        return _run(["gcloud", "auth", "print-access-token"])

    # -- transport ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if self.sends_own_token:
            req.add_header("Authorization", f"Bearer {self._id_token()}")
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _open(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    # A 2xx that is not JSON is the edge answering with a page
                    # -- a sign-in form, a captive portal, a path rule pointing
                    # at the wrong backend. Letting JSONDecodeError out ends
                    # the CLI in a traceback about column 1 of a document
                    # nobody asked for.
                    raise SwarmError(
                        f"{method} {path} -> {response.status}, but the body is not "
                        f"JSON: {_explain(response.status, raw)}",
                        status=response.status,
                        edge=_is_edge(response.status, raw),
                    ) from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise SwarmError(
                f"{method} {path} -> {exc.code}: {_explain(exc.code, raw)}",
                status=exc.code,
                edge=_is_edge(exc.code, raw),
            ) from exc
        except urllib.error.URLError as exc:
            raise SwarmError(f"could not reach {self.base_url}: {exc.reason}") from exc

    # -- operations --------------------------------------------------------
    def dispatch(
        self,
        *,
        prompt: str,
        runner_profile: str = "claude-code",
        repository_url: str | None = None,
        repository_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Submit one task.

        `prompt` goes in `input`, never in a command line. Invariant 10: the
        caller picks a runner profile BY NAME and supplies data; it cannot
        supply an image, a command or a resource spec, and this client has no
        parameter that would let it try.
        """
        payload: dict[str, Any] = {
            "runner_profile": runner_profile,
            "input": {"prompt": prompt},
            "metadata": {"origin": "swarm-mcp", **(metadata or {})},
        }
        if repository_url:
            payload["repository_url"] = repository_url
        if repository_ref:
            payload["repository_ref"] = repository_ref
        if timeout_seconds:
            payload["timeout_seconds"] = timeout_seconds
        if model:
            payload["model"] = model
        # UNWRAPPED HERE, not by each caller. `cmd_dispatch` printed
        # `task.get("task_id", "")` off the envelope and printed an empty line,
        # which a shell then piped into `swarm tail`.
        return unwrap_task(self.request("POST", "/v1/tasks", payload=payload))

    def dispatch_batch(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Several tasks at once. A LIST, matching what `dispatch` returns for
        one -- `{"tasks": [...], "count": n, ...}` is the route's envelope and
        a caller iterating it would iterate its keys."""
        data = self.request("POST", "/v1/tasks/batch", payload={"tasks": tasks})
        created = data.get("tasks") if isinstance(data, dict) else None
        if created is None:
            raise SwarmError("the batch response carried no `tasks` field")
        return [unwrap_task(t) for t in created]

    def task(self, task_id: str) -> dict[str, Any]:
        return unwrap_task(self.request("GET", f"/v1/tasks/{task_id}"))

    def events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v1/tasks/{task_id}/events?limit={limit}")
        if isinstance(data, dict):
            return list(data.get("events") or [])
        return list(data or [])

    def logs(
        self,
        task_id: str,
        *,
        attempt_id: str | None = None,
        stream: str | None = None,
        source: str = "auto",
        offset: int = 0,
        limit_bytes: int | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """One window of one attempt's captured output, as the route serves it.

        NOT UNWRAPPED, and not flattened. The envelope is the answer here: it
        carries the attempt the window came from, the redaction statement, and
        a per-stream `status` of ok / absent / unreadable that a caller must
        read before it reads `content`. Collapsing it to the text would throw
        away the difference between an agent that printed nothing and a read
        that failed, which is the distinction the route was built to keep.

        The parameters are the route's own, deliberately: `offset` and
        `limit_bytes` are byte positions in the RAW object and belong to
        whoever is paging, and this client does not have an opinion about them.
        """
        params: list[tuple[str, str]] = [("source", source), ("offset", str(max(0, offset)))]
        if attempt_id:
            params.append(("attempt_id", attempt_id))
        if stream:
            params.append(("stream", stream))
        if limit_bytes is not None:
            params.append(("limit_bytes", str(limit_bytes)))
        query = urllib.parse.urlencode(params)
        data = self.request("GET", f"/v1/tasks/{task_id}/logs?{query}", timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
            raise SwarmError(
                f"GET /v1/tasks/{task_id}/logs answered without a `streams` list; "
                "this deployment's logs route is not the one this client speaks to"
            )
        return data

    def cancel(self, task_id: str) -> dict[str, Any]:
        return unwrap_task(self.request("POST", f"/v1/tasks/{task_id}/cancel", payload={}))
