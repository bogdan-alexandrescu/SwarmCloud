"""The SwarmCloud API, from outside the platform.

WHICH API is the user's to say, not this repository's: `config.py` resolves a
deployment from `--context`, the environment, the plugin's userConfig or the
current context, and reads this checkout's tfvars only in developer mode
(SWARM_MCP_CONFIG_FROM=repo). See that module for the order.

IDENTITY IS BORROWED WHERE IT CAN BE, AND SIGNED IN WHERE IT CANNOT. For CI and
in-VPC callers the four sources `scripts/lib/common.sh::id_token` knows are
used as they always were -- this asks `gcloud` the same question the shell does
and honours the same `SWARM_ID_TOKEN` override. A developer at an IAP front
door signs in with the deployment's Desktop OAuth client (`sc login`,
signin.py), because no gcloud user credential passes a Google-managed IAP
client.

NOTHING IS CACHED TO DISK. An ID token is a bearer credential with an hour of
life. It lives in this process and nowhere else.
"""

from __future__ import annotations

import collections
import copy
import email.utils
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any, Sequence

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


@dataclass(frozen=True)
class RawBody:
    """A 2xx answer read as bytes rather than JSON (`SwarmClient.artifact_raw`):
    the body, and the response headers with their names lower-cased."""

    data: bytes
    headers: dict[str, str]


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

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        edge: bool = False,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.edge = edge
        #: The API's machine-readable `code` (`plan_changed`, `not_found`, ...)
        #: and its `detail` object, when the body was this platform's JSON
        #: error. None for an edge page, a FastAPI string detail, or no body.
        #: Carried for the same reason `status` is: a caller that has to tell
        #: one refusal from another must not do it by reading the sentence.
        self.code = code
        self.detail = detail
        #: Seconds a 429's `Retry-After` asked for, when it named any; read by
        #: the bridge's retry (`SwarmClient._request_retrying`).
        self.retry_after: float | None = None


#: The `code` swarm-api's own token bucket refuses with: `RateLimited.code` in
#: `swarm_api.errors`, served by `main.api_error_handler` as
#: `{"code": "rate_limited", "message": ..., "detail": {"retry_after_seconds": ...}}`.
API_RATE_LIMITED_CODE = "rate_limited"

#: What a 429 without that code says (ANSWERS-429, part of #1816).
NO_INSTANCE_FREE = "SwarmCloud's API had no instance free (Cloud Run)"


class ApiRateLimited(SwarmError):
    """A 429 from the API's own rate limiter: this caller sent too much.

    WHY TWO 429s (#1816). Cloud Run's front end answers 429 too, when the
    service has no instance free to take the request, and before this both
    read as "you are polling too much" -- which sends the reader looking at
    their own polling for an outage of capacity. Told apart by the body, not
    the status: the API's refusal is its JSON error carrying
    `API_RATE_LIMITED_CODE`, and anything else (Cloud Run's text or page, or
    a body with no such code) is `NoInstanceFree`. Both keep `status` 429, so
    every retry and wait keyed on the status (BRIDGE-PACING's) is unchanged.
    """


class NoInstanceFree(SwarmError):
    """A 429 the API did not send: Cloud Run's front end had no instance free.

    Its message says `NO_INSTANCE_FREE` and whether the bridge retried it
    (`SwarmClient._request_retrying`). See `ApiRateLimited`.
    """


def _too_many(method: str, path: str, raw: str, edge: bool, exc: Any) -> SwarmError:
    """A 429 as the error it is: the API's own refusal, or Cloud Run's."""
    code, detail = (None, None) if edge else _error_fields(raw)
    if code == API_RATE_LIMITED_CODE:
        error: SwarmError = ApiRateLimited(
            f"{method} {path} -> 429: {_explain(429, raw)}", status=429, edge=edge, code=code, detail=detail
        )
    else:
        error = NoInstanceFree(
            f"{method} {path} -> 429: {NO_INSTANCE_FREE}; not retried", status=429, edge=edge, code=code, detail=detail
        )
    error.retry_after = _retry_after(exc)
    return error


def _give_up(error: SwarmError, tried: int) -> None:
    """Raise Cloud Run's 429 the bridge's retry gives up on, saying it retried.

    Returns for any other error, and for one never retried, which the caller
    re-raises as it is.
    """
    if not isinstance(error, NoInstanceFree) or tried == 0:
        return
    message = str(error).replace(f"{NO_INSTANCE_FREE}; not retried", f"{NO_INSTANCE_FREE}; retried {tried} time(s)")
    out = NoInstanceFree(message, status=error.status, edge=error.edge, code=error.code, detail=error.detail)
    out.retry_after = error.retry_after
    raise out from error


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


#: This package's directory, relative to the repository root. How a directory
#: is recognised as a checkout of this repository.
_BRIDGE_DIR = ("apps", "swarm-mcp")


def _is_checkout(root: Any) -> bool:
    return (root.joinpath(*_BRIDGE_DIR) / "pyproject.toml").is_file()


def _built_from_checkout() -> Any:
    """The checkout a NON-editable install was built from, or None.

    `SWARM_MCP_FROM=<checkout>/apps/swarm-mcp` -- the `sc` plugin's developer
    escape hatch -- makes `uv tool run` build the working copy and install it
    into uv's cache, so `__file__` is
    `<cache>/archive-v0/<hash>/lib/python3.11/site-packages/swarm_mcp/client.py`
    and nothing above it is the checkout. The install still says where it came
    from: PEP 610's `direct_url.json`, which uv writes for a directory source
    as `{"url": "file:///<checkout>/apps/swarm-mcp", "dir_info": {...}}`. A git
    install's record is `vcs_info` with an https URL, which names nothing on
    this machine and is ignored.

    Accepted only when the recorded directory IS `<root>/apps/swarm-mcp` of a
    root that is a checkout, so a stray copy of the package elsewhere is never
    taken for one.
    """
    from importlib import metadata
    from pathlib import Path

    try:
        raw = metadata.distribution("swarm-mcp").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    try:
        record = json.loads(raw or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or "dir_info" not in record:
        return None
    url = urllib.parse.urlsplit(str(record.get("url", "")))
    if url.scheme != "file":
        return None
    source = Path(urllib.request.url2pathname(url.path)).resolve()
    root = source.parent.parent
    if source == root.joinpath(*_BRIDGE_DIR).resolve() and _is_checkout(root):
        return root
    return None


def _repo_root() -> Any:
    """This repository's root, when the package is installed from it.

    `swarm-mcp` is an EDITABLE workspace member (`[tool.uv.sources]` in the root
    `pyproject.toml`), so in the case that matters most -- an operator running
    `uv run sc` in a checkout -- `__file__` is
    `<root>/apps/swarm-mcp/swarm_mcp/client.py` and the root is four parents up.

    THE `sc` PLUGIN'S ESCAPE HATCH IS NOT EDITABLE, and in #62's first version
    it therefore read uv's cache as the repository: it ran the working copy's
    code and lost the working copy's front door. `_built_from_checkout` finds the
    checkout from the install's own record instead.

    Installed from git there is no checkout at all, and every caller here
    treats "no root" as "nothing to read", never as an error: the front door is
    then simply not discovered from tfvars, and `is_front_door` falls back to
    the address's shape.
    """
    from pathlib import Path

    override = os.environ.get("SWARM_REPO_ROOT", "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve().parents[3]
    if _is_checkout(here):
        return here
    return _built_from_checkout() or here


def front_door_host() -> str:
    """The load balancer's hostname, or `""` when this deployment has no front door.

    THIS IS THE HALF THE BRIDGE WAS MISSING, and it is why `swarm doctor` called
    a healthy control plane UNREACHABLE. Measured from this laptop on
    2026-09-24, before the change:

        uv run swarm doctor
          api       UNREACHABLE
                    GET /v1/tenants/me -> 404: an HTML 404 from Google's edge

    while the same API answered `GET /v1/runtimes` with 200 and five profiles
    over the live deployment's front door. `resolve_api_url` only ever
    asked Cloud Run, and Cloud Run's answer for a `team` deployment is the
    *.run.app address, whose ingress is `internal-and-cloud-load-balancing` --
    an address that answers a byte-identical HTML 404 to every path, healthy or
    not. `scripts/lib/common.sh` has known this since 2026-09-22 and documents
    it at length; the Python bridge did not, so the two halves of this
    repository disagreed about where the API is, and the half a Claude Code
    session uses was the wrong one.

    THREE SOURCES, IN THE SAME ORDER `scripts/lib/common.sh::front_door_host`
    uses them, because a second order would be a second answer -- the second
    of them ONLY when SWARM_MCP_CONFIG_FROM=repo (developer mode; see below):

      1. `SWARM_API_HOST` or `API_HOST` -- the operator, or a test. Both
         spellings, for the same reason `resolve_api_url` takes both `SWARM_`
         and bare `API_` names: a shell already configured for `make smoke`
         needs nothing extra.
      2. `terraform/environments/<env>/<env>.tfvars` -- Track C's INPUT, READ
         rather than copied. `frontend_hostname` is where the hostname is
         decided, and reading it is the "change your side to match theirs" move
         rather than a third place for the name to drift. `enable_frontend =
         false` means the load balancer is not deployed, and the hostname is
         usually still written down next to it -- using it then would send
         every call at a name that resolves to nothing.

    `terraform output frontend_url` is the source `common.sh` puts SECOND and
    is deliberately absent here: it does not exist yet (Track C's
    `terraform/infra/outputs.tf` exports `frontend_iap_audiences` and
    `quota_broker_url`, not the URL), and shelling out to `terraform` from a
    bridge whose whole design note is "no third-party dependency, urllib and
    subprocess only" would put a 30-second `terraform init` in the path of
    `sc`. When that output lands, `common.sh` picks it up and this reads the
    same tfvars it is generated from.

    DELIBERATELY NOT CACHED, where `common.sh` caches in `_FRONT_DOOR_HOST`.
    There the cache buys a forked `terraform` and a `sed`; here it is one small
    file read, called at most twice per client, and a process-lifetime cache in
    a module a test suite imports once is a value that outlives the environment
    it was computed from -- the first test to set `API_HOST` would decide the
    answer for every test after it.
    """
    for name in ("SWARM_API_HOST", "API_HOST"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.removeprefix("https://").removeprefix("http://").rstrip("/")

    # DEVELOPER MODE ONLY, since 2026-09-25. Read unconditionally, this is what
    # pointed every installed `sc` plugin at the owner's deployment: the
    # hostname in Track C's tfvars is THIS repository's deployment, not the
    # user's. `config.py` is where a user's deployment comes from now, and
    # `tests/unit/mcp/test_contexts.py` holds this gate with an audit hook
    # that fails on any open of a tfvars file outside the mode.
    from .config import repo_mode

    if not repo_mode():
        return ""

    environment = os.environ.get("ENVIRONMENT", "").strip() or "dev"
    tfvars = _repo_root() / "terraform" / "environments" / environment / f"{environment}.tfvars"
    try:
        text = tfvars.read_text()
    except OSError:
        # Not a checkout, or no such environment. ABSENT, not an error: a
        # `solo` deployment has no front door at all and must keep working.
        return ""
    if re.search(r"^\s*enable_frontend\s*=\s*false", text, re.MULTILINE):
        return ""
    match = re.search(r'^\s*frontend_hostname\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def is_front_door(url: str) -> bool:
    """Is this address the IAP load balancer rather than Cloud Run?

    It asks about the address IN USE rather than about configuration, exactly
    as `scripts/lib/common.sh::api_is_front_door` does, so an operator who sets
    `SWARM_API_URL` to the load balancer by hand gets the IAP credential too --
    and one who sets it to the *.run.app address from inside the VPC does not.

    WHEN NO FRONT DOOR IS DECLARED, THE ADDRESS'S SHAPE DECIDES. The shell only
    ever runs inside a checkout, where the tfvars declare the host. The bridge
    no longer does: the `sc` plugin installs it from git into uv's cache, where
    there are no tfvars, and in #62's first version an operator there who set
    `SWARM_API_URL=https://<the front door>` -- the variable the plugin README
    named first -- was sent a Google ID token, which IAP refuses as `Invalid JWT
    audience`. So with nothing declared, an https address that is not
    *.run.app is taken as the load balancer: in this platform nothing else
    serves the API over https under another name (Terraform creates no Cloud
    Run domain mapping). A *.run.app address keeps its ID token, and plain http
    -- the local proxy, a local API -- is never a front door.

    A DECLARED host still decides alone. An access token is a broader credential
    than an ID token bound to one audience, so where a front door is named it
    goes to that host and not to a lookalike of it.
    """
    if not url:
        return False
    host = front_door_host()
    if host:
        return url == f"https://{host}" or url.startswith(f"https://{host}/")
    parts = urllib.parse.urlsplit(url)
    hostname = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and bool(hostname)
        and hostname != "run.app"
        and not hostname.endswith(".run.app")
    )


def resolve_api_url() -> str:
    """Where the control plane is.

    `SWARM_API_URL` wins, then `API_URL` -- the same variable the scripts read,
    so a shell that is already configured for `make smoke` needs nothing extra.
    Then the FRONT DOOR, when this deployment has one: on a `team` deployment
    the load balancer is the only address that serves anybody outside the VPC,
    so asking Cloud Run there hands back an address that cannot answer.
    Otherwise Cloud Run is asked, which is the one answer that cannot be stale.
    """
    for name in ("SWARM_API_URL", "API_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    host = front_door_host()
    if host:
        return f"https://{host}"
    project = os.environ.get("PROJECT_ID", "").strip()
    region = os.environ.get("REGION", "").strip() or "us-central1"
    service = os.environ.get("API_SERVICE", "").strip() or "swarm-api"
    if not project:
        raise SwarmError(
            "set SWARM_API_URL, or PROJECT_ID so the URL can be read from Cloud Run"
        )
    # THE INGRESS IS READ IN THE SAME CALL AS THE URL, for the reason
    # `common.sh` gives: a Cloud Run URL whose service refuses external traffic
    # is an address that answers 404 to everything, and 404 is the one status a
    # reader takes for "wrong path" rather than "wrong host". Handing it back
    # silently is what sent `swarm doctor` -- the command whose entire job is to
    # explain this -- to print UNREACHABLE about a healthy deployment.
    described = _run([
        "gcloud", "run", "services", "describe", service,
        "--project", project, "--region", region,
        '--format=value[separator="|"](status.url,'
        'metadata.annotations."run.googleapis.com/ingress")',
    ])
    url, _, ingress = described.partition("|")
    url = url.strip().rstrip("/")
    ingress = ingress.strip()
    if not url:
        raise SwarmError(f"Cloud Run has no URL for {service} in {region}")
    # `all` is the only ingress that serves the run.app hostname publicly.
    # An empty ingress annotation means the default, which IS `all`.
    if ingress and ingress != "all":
        environment = os.environ.get("ENVIRONMENT", "").strip() or "dev"
        raise SwarmError(
            f"{service} resolves to {url}, and that address cannot serve you: its "
            f"ingress is '{ingress}', so Google's frontend refuses external requests "
            "before they reach the container and renders the refusal as HTTP 404 -- "
            "which reads exactly like a missing route on a broken deployment. The "
            "service is fine. Reach it through the load balancer instead: set "
            "API_HOST to the frontend_hostname in "
            f"terraform/environments/{environment}/{environment}.tfvars, or set "
            "SWARM_API_URL explicitly if you are inside the VPC, where the run.app "
            "address does work"
        )
    return url



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


def _explain_json(parsed: Any, stripped: str) -> str:
    """The sentence out of an error body, whichever of two shapes it is in.

    TWO SHAPES, AND `detail` MEANS A DIFFERENT THING IN EACH. FastAPI's own
    `HTTPException` puts the whole message in `detail`, as a string. This
    platform's `ApiError.to_payload` puts the SENTENCE in `message` and the
    machine-readable evidence in `detail`, as an object.

    Preferring `detail` unconditionally therefore threw the sentence away every
    time this API refused something with evidence attached -- a cyclic workflow
    came back as `{'cycle': ['build', 'test', 'build']}` with the words "the
    workflow steps form a cycle" dropped, which is the half a reader needs first.
    Both halves are kept, sentence first.
    """
    if not isinstance(parsed, dict):
        return stripped[:300]
    detail = parsed.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    message = parsed.get("message")
    if isinstance(message, str) and message.strip():
        if isinstance(detail, dict) and detail:
            return f"{message} ({json.dumps(detail, default=str)})"
        return message
    if detail:
        return str(detail)
    return stripped[:300]


def _error_fields(body: str) -> tuple[str | None, dict[str, Any] | None]:
    """`(code, detail)` out of an `ApiError.to_payload` body, or `(None, None)`."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    code = parsed.get("code")
    detail = parsed.get("detail")
    return (
        code if isinstance(code, str) and code else None,
        detail if isinstance(detail, dict) else None,
    )


def _login_command() -> str:
    """`sc login`, spelled so it runs where this bridge runs -- through
    `invocation.terminal_command`, the package's one spelling of a command
    (#189). Imported here, not at the top: `invocation` imports this module."""
    from .invocation import terminal_command

    return terminal_command("sc login")


def _explain(status: int, body: str) -> str:
    """Turn an edge's refusal into the sentence it was trying to be."""
    stripped = body.strip()
    if _is_iap_refusal(stripped):
        # Google's own clause says WHICH way the credential was wrong, which
        # the remedy does not; the remedy says what to do about it, which
        # Google's does not. Both, or the reader has half an answer.
        return (
            f"IAP refused this before the API saw it -- {stripped[:200]}. The front "
            "door takes two credentials and no others: an ID token minted for an "
            "OAuth client IAP allowlists as a programmatic client -- which is what "
            f"`{_login_command()}` mints, with the deployment's Desktop client -- or a service "
            "account's OAuth ACCESS TOKEN (SWARM_IMPERSONATE_SA, for CI), whose "
            "principal holds roles/iap.httpsResourceAccessor. An ID token for any "
            "other audience reads as `Invalid JWT audience`: a deployment whose "
            "`iap` block sets no oauth2_client_id has a Google-managed client, and "
            "only allowlisted clients' tokens pass it. SWARM_IAP_CLIENT_ID applies "
            "only where the deployment configured its own OAuth client"
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
            return _explain_json(parsed, stripped)
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
            "The front door takes an OAuth ACCESS token, and a 403 that NAMES the "
            "caller means the credential was accepted and the principal is not on "
            "the list -- roles/iap.httpsResourceAccessor, granted through "
            "frontend_iap_members in terraform/bootstrap/terraform.tfvars, which "
            "the owner applies and CI does not. A 401 means the token itself was not "
            "accepted: a gcloud user credential never is by a Google-managed IAP "
            "client, so sign in with the deployment's Desktop OAuth client "
            f"(`{_login_command()}`), or set SWARM_IMPERSONATE_SA in CI. "
            "SWARM_IAP_CLIENT_ID applies only where the deployment configured its "
            "own OAuth client"
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


def _header_map(response: Any) -> dict[str, str]:
    """A response's headers, names lower-cased; `{}` for one that has none."""
    headers = getattr(response, "headers", None) or {}
    return {str(k).lower(): str(v) for k, v in dict(headers).items()}


#: How much of a gzip body is read off the wire at a time.
_GZIP_CHUNK = 64 * 1024
_GZIP_MAGIC = b"\x1f\x8b"


def _read_body(response: Any, limit: int | None = None) -> bytes:
    """A response's body, gunzipped when it says `Content-Encoding: gzip`.

    A plain body is read exactly as before -- `read()`, or `read(limit)` -- so
    an API that does not compress is read unchanged. A body labelled gzip that
    does not start with gzip's magic bytes was decoded on the way (a proxy, a
    test transport) and is read as it is. With `limit`, at most that many
    DECODED bytes come back: a small gzip body can inflate to any size, so the
    bound is on what it inflates to, never on the wire bytes.
    """
    if _header_map(response).get("content-encoding", "").strip().lower() != "gzip":
        return response.read() if limit is None else response.read(limit)
    chunk = response.read(_GZIP_CHUNK)
    if not chunk.startswith(_GZIP_MAGIC):
        rest = response.read() if limit is None else response.read(max(limit - len(chunk), 0))
        data = chunk + rest
        return data if limit is None else data[:limit]
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    while chunk:
        pending = chunk
        while pending:
            room = 0 if limit is None else limit - len(out)
            out += decoder.decompress(pending, room)
            if limit is not None and len(out) >= limit:
                return bytes(out[:limit])
            pending = decoder.unconsumed_tail
        if decoder.eof:
            break
        chunk = response.read(_GZIP_CHUNK)
    out += decoder.flush()
    return bytes(out if limit is None else out[:limit])


@dataclass
class _Token:
    value: str
    minted_at: float
    #: How long `value` may be reused. The usual 45 minutes, except for a token
    #: this process did not mint fresh -- see `_reuse_for`.
    ttl: float = _TOKEN_TTL_SECONDS


def _reuse_for(token: str) -> float:
    """How long a token handed over by gcloud may be reused: until five minutes
    before its own `exp`, never longer than the usual TTL, and not at all when
    it carries no `exp` that can be read.

    `gcloud auth print-identity-token` returns the identity token it already
    holds, which can have minutes left. Reusing that for 45 minutes would turn
    a long `swarm tail` into a 401 at the moment it expired -- a failure that
    reads like a permission problem.
    """
    from .signin import decode_claims

    try:
        expires = float(decode_claims(token).get("exp") or 0)
    except (SwarmError, TypeError, ValueError):
        return 0.0
    return max(0.0, min(float(_TOKEN_TTL_SECONDS), expires - time.time() - 5 * 60))


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


#: `session.METADATA_KEY`, here because `session` imports `config`, which
#: imports this module.
SESSION_KEY = "claude_session"


def session_tag() -> dict[str, str]:
    """`{claude_session: <this Claude Code session>}`, or `{}` when unknown
    (`session.current`)."""
    from . import session

    return session.tag()


def _refused_field(exc: SwarmError, field: str) -> bool:
    """A 422 that names `field`: a deployment whose schema forbids it."""
    return exc.status == 422 and field in str(exc)


#: The keys one `additional_repositories` entry may carry: `TaskCreate`'s
#: `AdditionalRepositoryCreate` (docs/design/multi-repository-steps.md §4.1).
#: Names only. A URL, a slot, a commit or a path is swarm-api's to resolve
#: from the tenant's registration, and pins, signs and stores (invariant 10).
ADDITIONAL_REPOSITORY_KEYS = ("repository", "ref", "name")

#: `owner/repo`, as GitHub spells it. A SHAPE check, so a URL or an ssh
#: remote is refused before anything travels; whether the tenant registered
#: it, and whether its tasks may read it, is swarm-api's (`extrarepos`).
_OWNER_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")


def check_additional_repositories(value: Any, *, where: str) -> list[dict[str, str]] | None:
    """The repositories a task or step reads beside its own (#1434), checked
    for shape and returned as the API's entries, or None when not given.

    `[]` is returned as `[]`, never as None: on a workflow step it means
    "none", replacing the workflow's list, which absence does not (§7 Q6).

    THE COUNT IS NOT CHECKED HERE. The limit is the platform setting
    `max_additional_repositories`, which an admin edits live and swarm-api
    reads at each submission; a copy of today's value here would refuse
    tomorrow's legal list. Nor are the registration, the grant, the ref or
    the name's spelling: swarm-api refuses each with its own code.

    A refused value is never repeated: a URL can carry a credential.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise SwarmError(
            f"{where}: additional_repositories must be a list of repositories your "
            'workspace registered, e.g. [{"repository": "acme/web", "ref": "main"}]. '
            "Nothing was sent"
        )
    entries: list[dict[str, str]] = []
    for index, entry in enumerate(value):
        at = f"{where}: additional_repositories[{index}]"
        if not isinstance(entry, dict):
            raise SwarmError(
                f'{at} is not an object; an entry is {{"repository": "owner/repo", "ref": ..., '
                '"name": ...}. Nothing was sent'
            )
        unknown = sorted(str(key) for key in entry if key not in ADDITIONAL_REPOSITORY_KEYS)
        if unknown:
            raise SwarmError(
                f"{at} carries {unknown}; an entry takes {list(ADDITIONAL_REPOSITORY_KEYS)}. "
                "SwarmCloud resolves the URL, the commit and the credential from your "
                "workspace's registration. Nothing was sent"
            )
        repository = entry.get("repository")
        if not isinstance(repository, str) or not _OWNER_REPO.fullmatch(repository):
            raise SwarmError(
                f"{at}: repository must be owner/repo, a repository your workspace "
                "registered, as GitHub spells it -- never a URL or a remote. Nothing was sent"
            )
        checked = {"repository": repository}
        for key in ("ref", "name"):
            if entry.get(key) is None:
                continue
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise SwarmError(f"{at}: {key} must be a non-empty string. Nothing was sent")
            checked[key] = entry[key]
        entries.append(checked)
    return entries


def task_payload(
    *,
    prompt: str,
    runner_profile: str = "claude-code",
    repository_url: str | None = None,
    repository_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    model: str | None = None,
    inputs: dict[str, Any] | None = None,
    strategy: str | None = None,
    additional_repositories: list[dict[str, str]] | None = None,
    model_class: str | None = None,
    publish_to_pr: int | str | None = None,
) -> dict[str, Any]:
    """One task's `TaskCreate` body, for `POST /v1/tasks` and each entry of
    `POST /v1/tasks/batch` alike.

    ONE BUILDER FOR BOTH SHAPES (S7). A batch whose entries were built by a
    second copy of this would drift from the single dispatch the first time a
    field was added to one of them -- the `origin` tag, a strategy -- and the
    batch is the shape least often read by a person. `SwarmClient.dispatch`
    documents what each field means and why none of them is an execution
    parameter (invariant 10).
    """
    payload: dict[str, Any] = {
        "runner_profile": runner_profile,
        "input": {**(inputs or {}), "prompt": prompt},
        # The Claude Code session that sent it (SC-SESSION-SCOPE): what makes
        # `/sc check`, `/sc attach --all` and the watch count it as that
        # session's own and no other's (`session.tag`, empty when unknown).
        "metadata": {"origin": "swarm-mcp", **session_tag(), **(metadata or {})},
    }
    if repository_url:
        payload["repository_url"] = repository_url
    if repository_ref:
        payload["repository_ref"] = repository_ref
    if timeout_seconds:
        payload["timeout_seconds"] = timeout_seconds
    if model:
        payload["model"] = model
    if strategy:
        payload["strategy"] = strategy
    if model_class:
        # The class BY NAME (#1435), top-level as `TaskCreate.model_class`,
        # never in `input`. Absent when not named: the profile's default runs.
        payload["model_class"] = model_class
    if additional_repositories is not None:
        # As checked by `check_additional_repositories`: names, sent as given.
        # Absent when not given, so a task naming none sends what it always did.
        payload["additional_repositories"] = additional_repositories
    if publish_to_pr is not None:
        # The pull request this task's commits are pushed onto (#2183), in
        # the form `workflows.check_publish_to_pr` returns, top-level as
        # `TaskCreate.publish_to_pr`. Absent when not given, so a task naming
        # none sends what it always did.
        payload["publish_to_pr"] = publish_to_pr
    return payload


def task_id_of(task: dict[str, Any]) -> str:
    """A task's id. The API calls it `id`; this package called it `task_id`.

    `codec.task_to_api` emits `id`, after the frozen `Task.id`. Nothing under
    `/v1/tasks` ever emits `task_id` on a task document -- it appears only as
    a sibling key on the events, attempts and artifacts envelopes -- so the
    fallback is for those, not for a variant of this shape.
    """
    return str(task.get("id") or task.get("task_id") or "")


def outputs_of(
    task: dict[str, Any],
    listing: dict[str, Any] | None = None,
    *,
    listing_error: str | None = None,
) -> dict[str, Any]:
    """What one task produced beyond its code, in the shape every reader shows (#143).

    Each artifact by name and size, the runner's own status and summary, the
    exit code, the duration and the inputs staged into it from upstream steps.
    `swarm result`, `sc task` and the `swarm_result` tool all read this one
    function, so the three cannot disagree about what a step produced. Before
    it, all three printed only "this task cloned no repository" for a step
    whose result summary listed output.txt, a runner summary and 60.6s.

    `listing` is `SwarmClient.artifacts`' answer. Without one -- a caller that
    did not ask -- the artifacts are read from `result_summary`, which is the
    manifest that route serves. `listing_error` is a failed listing, and then
    `artifacts` is None with the reason beside it: "could not be listed" is
    not "none".

    NULL IS NOT MEASURED, never 0: a task that never ran has no exit code and
    no duration, and 0 would say it exited cleanly in no time.
    """
    summary = task.get("result_summary") or {}
    out: dict[str, Any] = {}
    if listing_error is not None:
        out["artifacts"] = None
        out["artifacts_unavailable_because"] = listing_error
        out["artifacts_complete"] = None
        out["artifacts_skipped"] = []
    else:
        source = listing if listing is not None else summary
        rows = source.get("artifacts") if isinstance(source, dict) else None
        out["artifacts"] = [
            {"name": str(row.get("name")), "bytes": row.get("bytes")}
            for row in rows or []
            if isinstance(row, dict) and row.get("name")
        ]
        complete = listing.get("complete") if listing is not None else bool(summary)
        out["artifacts_complete"] = bool(complete)
        skipped = source.get("artifacts_skipped") if isinstance(source, dict) else None
        # `{name, cause}` per entry (#165), from the listing route and from a
        # task's own summary alike. A bare name -- a summary that predates
        # causes -- is `cause: None`: unknown, never assumed to be the cap.
        entries: list[dict[str, Any]] = []
        for entry in skipped if isinstance(skipped, list) else []:
            if isinstance(entry, dict):
                if not isinstance(entry.get("name"), str):
                    continue
                cause = entry.get("cause")
                entries.append({"name": entry["name"], "cause": cause if isinstance(cause, str) and cause else None})
            elif isinstance(entry, str):
                entries.append({"name": entry, "cause": None})
        out["artifacts_skipped"] = entries
    runner = summary.get("runner") if isinstance(summary.get("runner"), dict) else {}
    out["runner_status"] = runner.get("status")
    out["runner_summary"] = runner.get("summary") or None
    exit_code = summary.get("exit_code")
    out["exit_code"] = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
    seconds = summary.get("duration_seconds")
    out["duration_s"] = (
        round(float(seconds), 1)
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
        else None
    )
    out["staged_inputs"] = [
        {"filename": item.get("filename"), "bytes": item.get("bytes"), "from_task": item.get("task_id")}
        for item in summary.get("staged_inputs") or []
        if isinstance(item, dict)
    ]
    out.update(runs_of(summary))
    return out


#: The fields of one agent run, and of one account swap, the worker records
#: in `result_summary.runs` / `.swaps` (SWAP-RUNS, #2457).
_RUN_FIELDS = (
    "run", "started_at", "ended_at", "duration_s", "exit_code", "timed_out",
    "account", "resumed", "after_swap", "tokens", "cost_usd", "reread",
)
_SWAP_FIELDS = ("at", "from", "to", "reason", "after_run")


def runs_of(summary: dict[str, Any]) -> dict[str, Any]:
    """Every agent run of the attempt and each account swap, or {} for one run.

    The worker writes `runs` and `swaps` only when an attempt started more
    than one runner (SWAP-RUNS, #2457): then `duration_s` above is the SUM of
    the runs, `last_run_duration_s` the last run's alone, and `swap_cost` what
    the swaps cost in seconds and tokens before the agent acted again. A
    one-run attempt's outputs carry none of these keys, as before.
    """
    runs = summary.get("runs")
    if not isinstance(runs, list) or not runs:
        return {}
    out: dict[str, Any] = {
        "runs": [
            {key: run[key] for key in _RUN_FIELDS if key in run}
            for run in runs
            if isinstance(run, dict)
        ],
        "swaps": [
            {key: swap[key] for key in _SWAP_FIELDS if key in swap}
            for swap in summary.get("swaps") or []
            if isinstance(swap, dict)
        ],
    }
    last = summary.get("last_run_duration_seconds")
    if isinstance(last, (int, float)) and not isinstance(last, bool):
        out["last_run_duration_s"] = round(float(last), 1)
    cost = summary.get("swap_cost")
    if isinstance(cost, dict):
        out["swap_cost"] = dict(cost)
    return out


#: Which of the caller's tenants a request acts as (#447). The API honours it
#: ONLY when the caller's verified membership includes that tenant's group and
#: refuses anything else with 403 `tenant_not_member` (swarm_api/auth.py
#: `_select_tenant`): the header selects, it never grants. Absent, the API's
#: first-match rule picks the tenant exactly as before, so a client that was
#: told nothing sends nothing.
TENANT_HEADER = "X-Swarm-Tenant"


def chosen_tenant(flag: str | None, environ: Any = None) -> str | None:
    """The tenant to act as: `flag` if given, else `SWARM_TENANT`, else None.

    THE FLAG WINS, as every other option here beats its variable. Blank is no
    choice at all -- `SWARM_TENANT=` in a shell profile must not become an
    empty header, which is a difference on the wire for nothing. Read by the
    CLI and by `sc` once per invocation, and by the MCP server once at start;
    `SwarmClient` never reads it itself, so a caller that names its tenant (or
    deliberately none) is never moved behind its back.

    `sc` READS IT TOO (SC-TENANT, owner decision 2026-10-12: "sc reads
    SWARM_TENANT"). Once #1204 made a person's ready personal workspace their
    default tenant, a tool that sends no header moved off the group it had
    always read; `sc` sending none would have split one person's view
    between their personal workspace (`sc`) and their group (the bridge, the
    plugin, `swarm`). The sc plugin's `tenant` option reaches the bridge and
    its SessionStart hook as this same variable, and a SWARM_TENANT already in
    Claude Code's environment wins over that option.
    """
    env = os.environ if environ is None else environ
    for value in (flag, env.get("SWARM_TENANT")):
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


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


# --------------------------------------------------------------------------
# The bridge's pacing: shared reads, a rate gate, a 429 retry (#1437)
# --------------------------------------------------------------------------
#
# ONE BRIDGE IS ONE PRINCIPAL. swarm-api limits each caller to
# `core.requests_per_second` = 20 with a burst of `rate_limit_burst` = 40
# (apps/swarm-api/swarm_api/deps.py, TokenBucketLimiter), and every row a
# Claude Code session holds -- 60 to 85 of them with ~17 workflows attached,
# measured 2026-10-16 -- reads through this one process. Each hold re-read its
# routes every `progress.POLL_SECONDS`, nothing was shared between holds on
# the same task, and the API answered 429. Three things here, switched on by
# `SwarmClient.pace_for_bridge` and by nothing else: the CLI makes a handful
# of calls per command, and the test suite's fake transports change a task
# between two reads with no real time passing, which a cache would hide.

#: How long one read of a shared route serves every caller. Equal to
#: `progress.POLL_SECONDS` (5 s; a test holds the two together, and this
#: module cannot import that one): a hold never polls faster than that, so a
#: hold reads at most one poll's age -- what it would have seen anyway, had
#: its poll landed a moment earlier -- and N holds on one task cost one read
#: per poll instead of N.
SHARED_READ_TTL_SECONDS = 5.0

#: THE ROUTES THE HOLDS REPEAT, and only those: the task document, the
#: workflow document, a task's events page and its live log window. Matched on
#: the path with its query, so two different pages or windows are two keys.
#: Every other GET -- artifacts, answers, attempts, listings, the tenant -- and
#: every write goes to the API each time.
_SHARED_ROUTES = (
    re.compile(r"^/v1/tasks/(?!batch\b)[^/?]+(\?.*)?$"),
    re.compile(r"^/v1/tasks/[^/?]+/events(\?.*)?$"),
    re.compile(r"^/v1/tasks/[^/?]+/logs(\?.*)?$"),
    re.compile(r"^/v1/workflows/[^/?]+(\?.*)?$"),
)


def _shared_route(path: str) -> bool:
    return any(route.match(path) for route in _SHARED_ROUTES)


#: The bridge's own ceiling, below swarm-api's 20/s and 40 so that the console
#: in the same browser -- the same principal -- keeps 5/s and 10 of burst. The
#: environment may move them (`SWARM_MCP_RATE_PER_SECOND`,
#: `SWARM_MCP_RATE_BURST`), never past the API's own figures: a gate above the
#: limit it exists to stay under is no gate.
RATE_PER_SECOND_DEFAULT = 15.0
RATE_BURST_DEFAULT = 30
_API_RATE_PER_SECOND = 20.0
_API_RATE_BURST = 40

#: HOW QUICK CALLS GO FIRST: ONE BUCKET WITH A RESERVE, not two buckets. A
#: hold takes a token only while more than this many would remain, and never
#: while a quick call is waiting for one; a quick call may take the last. Two
#: buckets would split the rate for good -- a `swarm_check` that fans out a
#: dozen reads would be held to the quick share even with no row open -- where
#: the reserve gives holds the whole rate while nothing else asks, and keeps a
#: third of the burst for the call a person is waiting on.
RATE_HOLD_RESERVE_FRACTION = 1 / 3

#: A GET answered 429 is tried again after the response's `Retry-After`, else
#: after these delays (each stretched by up to `RETRY_JITTER` so that sixty
#: holds refused together do not come back together), and after the last one
#: the 429 is reported as it always was. Never a write: a POST the API may
#: have acted on is not the client's to repeat. `Retry-After` is honoured up
#: to `RETRY_AFTER_CAP_SECONDS`; past that the call reports rather than sits.
RETRY_429_DELAYS = (2.0, 4.0, 8.0)
RETRY_JITTER = 0.25
RETRY_AFTER_CAP_SECONDS = 60.0

#: The most a QUICK call's GET waits out 429s in all, summed over its retries;
#: a retry that would pass it is not taken, and the 429 is reported. A quick
#: call is one a person is waiting on -- a submit's read, a check, a status --
#: and under a 429 storm the cap above, three times over, held one for about
#: three minutes, and every caller joined to its shared read as long (review
#: of PR 1455, BRIDGE-POOL). 20 s still covers the default backoff whole
#: (2 + 4 + 8, at most 17.5 s with jitter), so a passing burst is ridden out,
#: while a `Retry-After` longer than that is the API saying the storm will
#: outlast the person's patience. A hold is not bounded by it: it is a row
#: waiting in the background, and its own poll already waits out a 429.
QUICK_RETRY_BUDGET_SECONDS = 20.0

#: Which lane the current thread serves: the bridge's hold pool marks its
#: threads at start (`enter_hold_lane`), and the rate gate reads it.
_LANE = threading.local()


def enter_hold_lane() -> None:
    """Mark this thread as one of the bridge's HOLD threads, for good: the
    hold pool's initializer, so every request a hold makes yields to quick ones."""
    _LANE.hold = True


def in_hold_lane() -> bool:
    return bool(getattr(_LANE, "hold", False))


def _env_number(environ: Any, name: str, default: float, low: float, high: float) -> float:
    raw = str(environ.get(name, "") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value:  # NaN
        return default
    return max(low, min(high, value))


class RateGate:
    """A token bucket: `rate` per second, `burst` at most, with a `reserve`
    a hold may not spend (see `RATE_HOLD_RESERVE_FRACTION`).

    WAITERS QUEUE, AND ONLY THE HEAD WAITS FOR THE REFILL. Every waiter used
    to sleep on one condition to the moment the bucket would hold its token,
    so 256 holds woke together at every refill, one took the token and 255
    slept again (review of PR 1455). Now the head of the queue -- the first
    quick caller, else the first hold -- sleeps to its refill, and a waiter
    behind it sleeps untimed until the one before it leaves and wakes it
    (`_wake_head`): one wake per token, in arrival order within a lane, and
    quick callers still before every hold."""

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        reserve: int | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.rate = float(rate)
        self.burst = max(1, int(burst))
        default_reserve = int(self.burst * RATE_HOLD_RESERVE_FRACTION)
        self.reserve = max(0, min(self.burst - 1, default_reserve if reserve is None else int(reserve)))
        self._clock = clock
        self._tokens = float(self.burst)
        self._stamp = clock()
        self._lock = threading.Lock()
        # One condition per waiter, on the one lock, so a wake names its thread.
        self._quicks: collections.deque[threading.Condition] = collections.deque()
        self._holds: collections.deque[threading.Condition] = collections.deque()
        self._quick_waiting = 0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    def _head(self) -> threading.Condition | None:
        if self._quicks:
            return self._quicks[0]
        return self._holds[0] if self._holds else None

    def _wake_head(self) -> None:
        head = self._head()
        if head is not None:
            head.notify()

    def take(self, *, hold: bool = False) -> None:
        """Return once this caller has a token."""
        with self._lock:
            floor = 1.0 + (self.reserve if hold else 0)
            queue = self._holds if hold else self._quicks
            me = threading.Condition(self._lock)
            queue.append(me)
            if not hold:
                self._quick_waiting += 1
            try:
                while True:
                    if self._head() is not me:
                        # Woken by the waiter before it, or by a quick caller
                        # taking the head from this hold.
                        me.wait()
                        continue
                    self._refill()
                    if self._tokens >= floor:
                        self._tokens -= 1.0
                        return
                    me.wait(timeout=max((floor - self._tokens) / self.rate, 0.001))
            finally:
                queue.remove(me)
                if not hold:
                    self._quick_waiting -= 1
                self._wake_head()


class _Flight:
    __slots__ = ("done", "value", "error", "hold", "generation")

    def __init__(self, hold: bool, generation: int) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None
        self.hold = hold
        # The write count when this read began: a read begun before a write
        # is not joined after it.
        self.generation = generation


class SharedReads:
    """One read per key per `ttl` for every caller, and single-flight: a
    caller of a key already being read waits for that answer.

    A QUICK CALLER NEVER WAITS ON A HOLD'S FLIGHT. A hold's read may be queued
    at the rate gate behind the reserve, and a quick call joining it would
    then wait behind a hold -- the one thing the two lanes exist to prevent.
    It reads for itself, and a hold joins either kind.

    Every answer is a deep copy, so no caller can change what the next is
    handed. A write anywhere clears the cache (`invalidate`), and a read that
    was in flight across a write is answered but not kept: after a cancel or a
    submit, the next read sees what the write did.
    """

    def __init__(self, ttl: float = SHARED_READ_TTL_SECONDS, *, clock: Any = time.monotonic) -> None:
        self.ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._kept: dict[Any, tuple[float, Any]] = {}
        self._flying: dict[tuple[Any, bool], _Flight] = {}
        self._generation = 0

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._kept.clear()

    def read(self, key: Any, fetch: Any, *, hold: bool = False) -> Any:
        with self._lock:
            now = self._clock()
            kept = self._kept.get(key)
            if kept is not None and kept[0] > now:
                return copy.deepcopy(kept[1])
            flight = self._flying.get((key, False))
            if flight is not None and flight.generation != self._generation:
                flight = None
            if flight is None and hold:
                flight = self._flying.get((key, True))
                if flight is not None and flight.generation != self._generation:
                    flight = None
            leading = flight is None
            if leading:
                generation = self._generation
                flight = _Flight(hold, generation)
                # A flight begun before a write is left to finish for its own
                # waiters; this one replaces it as the key's current flight.
                self._flying[(key, hold)] = flight
        assert flight is not None
        if not leading:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return copy.deepcopy(flight.value)
        try:
            value = fetch()
        except BaseException as exc:
            flight.error = exc
            raise
        else:
            flight.value = value
            with self._lock:
                if generation == self._generation:
                    now = self._clock()
                    for stale in [k for k, (until, _) in self._kept.items() if until <= now]:
                        del self._kept[stale]
                    self._kept[key] = (now + self.ttl, value)
            return copy.deepcopy(value)
        finally:
            with self._lock:
                if self._flying.get((key, flight.hold)) is flight:
                    del self._flying[(key, flight.hold)]
            flight.done.set()


def _retry_after(exc: Any) -> float | None:
    """Seconds from a response's `Retry-After` (a number or an HTTP date), or None."""
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - time.time()
    if seconds != seconds:  # NaN
        return None
    return max(0.0, seconds)


def _skew_refetch_statuses() -> tuple[int, ...]:
    from .discovery import REFETCH_STATUSES

    return REFETCH_STATUSES


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
        context: str | None = None,
        deployment: Any = None,
        tier: Any = None,
        tenant: str | None = None,
    ) -> None:
        # Local imports: `auth`, `config` and `signin` all import this module,
        # so importing them at load time would be a cycle.
        from . import auth as _auth
        from . import config as _config

        #: An explicit `base_url` is taken as given and resolves nothing -- it
        #: is how tests and in-VPC callers name an exact address. Otherwise the
        #: deployment is resolved from --context, the environment, the plugin
        #: and the config file (config.py), and never from this repository
        #: unless developer mode is on.
        self._explicit_url = (base_url or "").strip().rstrip("/") or None
        if deployment is None and self._explicit_url is None:
            deployment = _config.resolve(context=context)
        self.deployment = deployment
        #: `tier` is for the one caller that must test a SPECIFIC credential
        #: whatever else this machine has: `sc login`, checking the sign-in it
        #: just made. Everything else takes what `detect` finds.
        self.detection = (
            _auth.Detection(tier, "chosen by the caller") if tier is not None
            else _auth.detect(deployment)
        )
        self.tier = self.detection.tier
        self._proxy: _auth.Proxy | None = None
        self._token: _Token | None = None
        #: The OAuth access token, cached exactly as `_token` is. See
        #: `access_token` for why it was not, and what that cost.
        self._access: _Token | None = None
        #: ONE MINT AT A TIME. `sc` fetches concurrently, and without this
        #: every thread that finds the cache empty shells out to gcloud for
        #: the same token -- nine subprocesses for one screen. Re-entrant,
        #: because `credential` holds it while calling the two minters that
        #: take it themselves.
        self._mint_lock = threading.RLock()
        self._signed_in: Any = None
        #: The bridge's pacing (`pace_for_bridge`); None everywhere else.
        self._shared: SharedReads | None = None
        self._gate: RateGate | None = None
        self._sleep: Any = time.sleep

        if self.tier is _auth.Tier.IAP:
            # The audience IAP accepts is its OAuth client id, never the
            # service URL. Getting this wrong is the "Invalid JWT audience"
            # that sends people looking at their IAM policy.
            self._audience = os.environ.get("SWARM_IAP_CLIENT_ID", "").strip()
        else:
            # A service account through IAP addresses the API's backend; the
            # discovery document names it (`iap_audience`), so a one-value
            # install needs no API_AUDIENCE.
            self._audience = (
                audience
                or os.environ.get("API_AUDIENCE", "").strip()
                or getattr(deployment, "iap_audience", "")
                or None
            )

        #: The plugin/deployment version-skew check (`discovery.Checker`),
        #: made before every API call. Only a deployment the plugin configured
        #: carries one (`Deployment.skew`); an explicit `base_url` never does.
        self.skew: Any = None
        if self._explicit_url is None and getattr(deployment, "skew", ""):
            from . import discovery as _discovery

            self.skew = _discovery.Checker(
                deployment.url, required=deployment.skew == _config.SKEW_REQUIRED
            )

        #: The tenant every request names in `X-Swarm-Tenant`, or None to send
        #: no header and let the API pick (`chosen_tenant`).
        self.tenant = (tenant or "").strip() or None

        self.base_url = ""
        self.front_door = False
        if connect:
            self.connect()

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        from . import auth as _auth

        # A CONFIGURED DEPLOYMENT BEATS THE PROXY, and the order is the fix it
        # always was. `gcloud run services proxy` calls the *.run.app address
        # directly, and on a `team` deployment that address refuses everything
        # outside the VPC -- so the proxy is only for the one shape left over:
        # nothing configured, and PROJECT_ID naming a solo deployment. A
        # CONFIGURED solo deployment on user credentials is sent gcloud's own
        # identity token instead (`_id_token`), which needs no service name,
        # region or project to find.
        deployment = self.deployment
        if self._explicit_url:
            self.base_url = self._explicit_url
        elif deployment is not None:
            self.base_url = deployment.url.rstrip("/")
        elif not os.environ.get("PROJECT_ID", "").strip():
            # NOTHING CONFIGURED. This used to fall back to `gcloud config get-
            # value project` and start a proxy to whatever `swarm-api` that
            # project held -- a deployment the user never named. A client that
            # is told nothing says what to tell it.
            from .invocation import terminal_command

            add = terminal_command(
                "sc context add <name> --url https://<your deployment> "
                "--client-id <Desktop OAuth client id>"
            )
            raise SwarmError(
                f"no SwarmCloud deployment is configured. Add one with `{add}` "
                "(the sc plugin asks for it at install: /plugin configure "
                "sc@swarmcloud), or set SWARM_URL. For a solo deployment reached "
                "through Cloud Run, set PROJECT_ID"
            )
        elif self.tier is _auth.Tier.PROXY:
            self._proxy = _auth.Proxy(service_name(), region(), project_id())
            self.base_url = self._proxy.start().rstrip("/")
        else:
            self.base_url = resolve_api_url().rstrip("/")
        #: Decided from the ADDRESS, not from configuration, so an operator who
        #: points SWARM_API_URL at the load balancer by hand gets the IAP
        #: credential too -- and from the resolved deployment, which knows it
        #: is an IAP front door when it has a sign-in client or is not run.app.
        self.front_door = is_front_door(self.base_url) or bool(
            deployment is not None
            and deployment.front_door
            and self.base_url == deployment.url.rstrip("/")
        )
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
        """False when a gcloud PROXY is in use, because gcloud supplies the
        Authorization header itself. Sending a second one would replace the only
        credential Cloud Run will accept with one it will not.

        Asked of the PROXY OBJECT rather than of the tier. Those are no longer
        the same question: `connect` now prefers the front door over the proxy,
        so a machine on the user-credentials tier talking to a load balancer is
        on that tier and has no proxy -- and would have sent no credential at
        all, which IAP answers with a 302 to a sign-in page.
        """
        return self._proxy is None

    def credential(self) -> str:
        """The bearer THIS address expects. Two doors, two credentials.

        `scripts/lib/common.sh::api_credential` makes exactly this decision and
        records the measurements behind it; this is the same decision on the
        Python side, and it did not exist. The bridge sent a Google ID token
        everywhere, including at the load balancer, where IAP answers

            401 Invalid IAP credentials: Invalid JWT audience.

        -- a sentence that sends a reader to their IAM policy when the problem
        is the KIND of token. Re-measured on 2026-09-24 against the live front
        door of the live deployment, as its owner:

            user OAuth access token        -> 401, IAP error code 900
            impersonated SA access token   -> 403 "Access denied. For user
                                              swarm-verify@..."

        The 403 is the useful one: it NAMES the caller, which is IAP saying "I
        authenticated you and you are not on the list" -- one
        `roles/iap.httpsResourceAccessor` grant away from working, and that
        grant is `frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`
        (moved out of terraform/infra by #23; the owner applies it, CI does
        not, and it now lists swarm-verify). The 401 is not:
        no OAuth client this laptop can mint a user token from is one IAP will
        accept, because `iap { enabled = true }` in
        `terraform/modules/frontend/main.tf` deliberately sets no
        `oauth2_client_id` and Google manages the client. So at the front door
        this presents a service account's access token when one is configured,
        and the operator's own otherwise -- which is what `common.sh` does, and
        it is the caller's business to read the refusal, not this function's to
        pre-empt it.

        Nothing is lost by sending an access token: IAP forwards the backend NO
        Authorization header of its own and swarm-api authenticates the caller
        from `X-Goog-IAP-JWT-Assertion` instead
        (apps/swarm-api/swarm_api/deps.py, `current_auth`).

        ONE EXCEPTION, AND WITHOUT IT `Tier.IAP` BECOMES DEAD CODE. That tier
        exists for the other shape of IAP deployment: one that configured its
        OWN OAuth client, where `oauth2_client_id` is set on the backend service
        and an ID token minted for that client id is the documented programmatic
        path. `detect()` finds the tier, `swarm doctor` prints it, and
        `id_token_for` already mints with `--audiences=<client id>` -- so
        sending an access token there regardless would detect a tier, announce
        it, and then never use it, which is exactly the "declared but not
        implemented" shape this bridge is supposed to catch rather than commit.

        `scripts/lib/common.sh::api_credential` has no equivalent branch because
        it was written for THIS deployment, which has no client id. That is a
        deliberate divergence and not drift: the shell serves one cluster, and
        this package is what someone else deploys.
        """
        from . import auth as _auth

        with self._mint_lock:
            # SIGNED IN comes first: the developer's own ID token, minted for
            # the Desktop client IAP allowlists. It is the one credential this
            # laptop has that IAP admits AS the developer (see signin.py).
            if self.tier is _auth.Tier.SIGNED_IN:
                return self.signed_in().id_token()
            if self.front_door:
                if self.tier is _auth.Tier.IAP:
                    return self._id_token()
                return self.access_token()
            return self._id_token()

    def signed_in(self) -> Any:
        """The sign-in token source for this client's deployment (signin.SignedIn)."""
        from . import signin

        if self._signed_in is None:
            if self.deployment is None:
                raise SwarmError("there is no configured deployment to be signed in to")
            self._signed_in = signin.SignedIn(self.deployment)
        return self._signed_in

    def _id_token(self) -> str:
        with self._mint_lock:
            return self._mint_id_token()

    def _mint_id_token(self) -> str:
        from . import auth as _auth
        from .config import is_run_app

        now = time.monotonic()
        if self._token is not None and now - self._token.minted_at < self._token.ttl:
            return self._token.value
        if self.tier is _auth.Tier.PROXY and self._proxy is None and is_run_app(self.base_url):
            # A SOLO deployment the user CONFIGURED, on ordinary user
            # credentials. No proxy was started -- nothing named a project to
            # start one against -- and `id_token_for(PROXY)` has no token to
            # give. Before this branch every call here failed with "this tier
            # reaches the API through a local proxy instead", about a proxy
            # that did not exist, and a solo user had nothing to configure
            # that would fix it (review of PR #61, 2026-09-25).
            value = self.developer_id_token()
            ttl = _reuse_for(value)
        else:
            value = _auth.id_token_for(self._audience or self.base_url, tier=self.tier)
            ttl = _TOKEN_TTL_SECONDS
        if not value:
            raise SwarmError("no ID token available; run: gcloud auth login")
        self._token = _Token(value=value, minted_at=now, ttl=ttl)
        return value

    def developer_id_token(self) -> str:
        """gcloud's own identity token for the signed-in gcloud account.

        Cloud Run documents exactly this for a developer reaching a private
        service: `curl -H "Authorization: Bearer $(gcloud auth
        print-identity-token)" SERVICE_URL`, for an account holding
        run.routes.invoke (docs.cloud.google.com/run/docs/authenticating/
        developers). A user account cannot choose the token's audience, and
        Cloud Run takes it regardless.

        The same page says why it is a DEVELOPMENT path: the token has no
        audience of its own, so whoever receives it could replay it at any
        other Cloud Run service this account may invoke. It is therefore only
        ever sent to a *.run.app address the user configured (`_id_token`
        checks), never to a load balancer, a custom domain or anything else.
        """
        return _run(["gcloud", "auth", "print-identity-token"])

    def access_token(self) -> str:
        """An OAuth access token: for GCS, and for IAP at the front door.

        Distinct from the ID token above -- an ID token says who you are to a
        Cloud Run service that verifies it itself, an access token authorises a
        bucket read and is what IAP takes.

        SWARM_IMPERSONATE_SA IS HONOURED HERE, which it was not, and that was
        the gap that made the variable a lie on this path: `swarm doctor` tells
        an operator to set it to reach a team deployment, the shell's
        `access_token()` has minted an impersonated token since 2026-09-22, and
        this one ignored it and handed back the operator's own -- so setting the
        variable changed the ID-token path only and the front-door path
        presented a credential IAP refuses with a different error than the one
        the operator was told to expect. Same three sources as `common.sh`, in
        the same order.

        CACHED FOR `_TOKEN_TTL_SECONDS`, as the ID token has always been. It
        was not, and at the front door this is the credential EVERY request
        carries: each one ran `gcloud auth print-access-token
        --impersonate-service-account`, about a second apiece. Measured on
        2026-09-25 (#88, SC-F9) that made `tail` and `follow` crawl, and a poll
        that stacked them ended in "logs unavailable: gcloud timed out after
        60s". An access token lives an hour; the 45 minutes are the ID token's
        margin, kept for the same reason. gcloud can hand back a token it
        already held, with less than that left, and an access token -- unlike
        an ID token -- carries no `exp` to read; `request` therefore re-mints
        once and retries when a CACHED credential is refused with a 401.

        `SWARM_ACCESS_TOKEN` is not cached: it is the operator's, it costs
        nothing to read, and caching it would outlive their changing it.
        """
        override = os.environ.get("SWARM_ACCESS_TOKEN", "").strip()
        if override:
            return override
        with self._mint_lock:
            now = time.monotonic()
            cached = self._access
            if cached is not None and now - cached.minted_at < cached.ttl:
                return cached.value
            impersonate = os.environ.get("SWARM_IMPERSONATE_SA", "").strip()
            if impersonate:
                value = _run([
                    "gcloud", "auth", "print-access-token",
                    f"--impersonate-service-account={impersonate}",
                ])
            else:
                value = _run(["gcloud", "auth", "print-access-token"])
            if value:
                self._access = _Token(value=value, minted_at=now)
            return value

    def forget_credentials(self) -> bool:
        """Drop every cached token this client minted. True if there was one.

        For the one refusal a cache can cause: a token gcloud handed back with
        less life left than the cache assumed. A caller that was refused with a
        401 while holding a cached credential asks for a fresh one this way and
        retries ONCE; the answer is whether a retry could differ at all.
        """
        with self._mint_lock:
            held = self._token is not None or self._access is not None
            self._token = None
            self._access = None
            return held

    def _sends_cached_credential(self) -> bool:
        """Would the next request carry a token this client minted EARLIER?

        Not on the signed-in tier, whose token source (signin.py) keeps its own
        cache and refreshes itself; forgetting ours would change nothing there,
        and a 401 there is the deployment refusing its own Desktop client, which
        a retry cannot fix.
        """
        from . import auth as _auth

        if not self.sends_own_token or self.tier is _auth.Tier.SIGNED_IN:
            return False
        with self._mint_lock:
            return self._token is not None or self._access is not None

    # -- pacing ------------------------------------------------------------
    def pace_for_bridge(
        self,
        *,
        environ: Any = None,
        ttl: float = SHARED_READ_TTL_SECONDS,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        """Share the holds' reads, stay under the API's rate, and retry a GET's
        429 -- what the MCP bridge needs and a one-shot command does not (see
        the block above `RateGate`). The rate and burst come from
        `SWARM_MCP_RATE_PER_SECOND` and `SWARM_MCP_RATE_BURST`, held to at most
        the API's own 20 and 40."""
        env = os.environ if environ is None else environ
        rate = _env_number(env, "SWARM_MCP_RATE_PER_SECOND", RATE_PER_SECOND_DEFAULT,
                           0.5, _API_RATE_PER_SECOND)
        burst = int(_env_number(env, "SWARM_MCP_RATE_BURST", RATE_BURST_DEFAULT,
                                1, _API_RATE_BURST))
        self._shared = SharedReads(ttl, clock=clock)
        self._gate = RateGate(rate, burst, clock=clock)
        self._sleep = sleep

    # -- transport ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        """One API call, through the bridge's pacing when it is on.

        A GET of a shared route is answered from the shared read
        (`SharedReads`), keyed by method, path with query, and the tenant
        header, so no tenant is handed another's answer. Any other method
        clears the shared reads once it has been sent.
        """
        shared = self._shared
        if shared is None:
            return self._request_once(method, path, payload=payload, timeout=timeout)
        if method == "GET" and payload is None and _shared_route(path):
            key = (method, path, self.tenant or "")
            return shared.read(
                key,
                lambda: self._request_retrying(method, path, payload=payload, timeout=timeout),
                hold=in_hold_lane(),
            )
        try:
            return self._request_retrying(method, path, payload=payload, timeout=timeout)
        finally:
            if method != "GET":
                shared.invalidate()

    def _request_retrying(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        timeout: int,
        raw_limit: int | None = None,
    ) -> Any:
        """`_request_once`, and for a GET answered 429, up to
        `len(RETRY_429_DELAYS)` more tries (`RETRY_429_DELAYS`) -- for a quick
        call, only while their waits sum to `QUICK_RETRY_BUDGET_SECONDS`."""
        tried = 0
        waited = 0.0
        # A quick call stops at `QUICK_RETRY_BUDGET_SECONDS` of waiting; its
        # shared read's waiters are then handed the same 429 (`SharedReads`).
        budget = None if in_hold_lane() else QUICK_RETRY_BUDGET_SECONDS
        while True:
            try:
                return self._request_once(
                    method, path, payload=payload, timeout=timeout, raw_limit=raw_limit
                )
            except SwarmError as exc:
                if exc.status != 429 or method != "GET" or tried >= len(RETRY_429_DELAYS):
                    _give_up(exc, tried)
                    raise
                if exc.retry_after is not None:
                    delay = min(exc.retry_after, RETRY_AFTER_CAP_SECONDS)
                else:
                    delay = RETRY_429_DELAYS[tried] * (1.0 + random.uniform(0.0, RETRY_JITTER))
                if budget is not None and waited + delay > budget:
                    _give_up(exc, tried)
                    raise
                tried += 1
                waited += delay
                self._sleep(delay)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
        raw_limit: int | None = None,
    ) -> Any:
        """One API call; ONE retry when a cached credential is refused with 401.

        The retry exists because tokens are now cached (see `access_token`)
        and the cache cannot always know how long a token has left. It is
        taken only when the refused credential came from the cache, so a
        credential that is simply not accepted -- a gcloud user token at IAP,
        answered 401 with error code 900 -- costs one round trip, as before,
        and not two.
        """
        skew = self.skew
        if skew is not None:
            # Every call answers a skew, by name, before it is sent (plan §6.5).
            skew.check()
        cached = self._sends_cached_credential()
        try:
            return self._send(method, path, payload=payload, timeout=timeout, raw_limit=raw_limit)
        except SwarmError as exc:
            if skew is not None and exc.status in _skew_refetch_statuses():
                # A rotated client (401) or a route the deployment no longer
                # has (404/410): read the document again, and answer a skew it
                # now shows instead of the bare status.
                skew.check(refetch=True)
            if exc.status == 401 and cached and self.forget_credentials():
                return self._send(method, path, payload=payload, timeout=timeout, raw_limit=raw_limit)
            raise

    def _send(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        timeout: int,
        raw_limit: int | None = None,
    ) -> Any:
        """One request. With `raw_limit`, the 2xx body is NOT JSON-decoded: it
        comes back as `RawBody`, at most `raw_limit` bytes, or a SwarmError
        when there is more. An error answer is the JSON envelope either way."""
        if self._gate is not None:
            self._gate.take(hold=in_hold_lane())
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if self.sends_own_token:
            req.add_header("Authorization", f"Bearer {self.credential()}")
        if self.tenant:
            req.add_header(TENANT_HEADER, self.tenant)
        req.add_header("Accept", "application/json" if raw_limit is None else "*/*")
        # swarm-api gzips a whole answer of 1 KB or more for a caller that asks
        # (#1822): a board page is 1.43 MB plain, and this client is most of the
        # API's requests. An older API ignores it and answers plain, which
        # `_read_body` still reads.
        req.add_header("Accept-Encoding", "gzip")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _open(req, timeout=timeout) as response:
                if raw_limit is not None:
                    data = _read_body(response, raw_limit + 1)
                    if len(data) > raw_limit:
                        raise SwarmError(
                            f"{method} {path} -> {response.status}, but the body is over "
                            f"{raw_limit} bytes, the most this client reads into memory; "
                            "nothing was kept",
                            status=response.status,
                        )
                    headers = _header_map(response)
                    if headers.get("content-encoding") == "gzip":
                        # The bytes are decoded: their length and encoding are
                        # no longer the wire's.
                        headers.pop("content-encoding")
                        headers.pop("content-length", None)
                    return RawBody(data, headers)
                raw = _read_body(response).decode("utf-8", errors="replace")
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
            raw = _read_body(exc).decode("utf-8", errors="replace")
            edge = _is_edge(exc.code, raw)
            message = f"{method} {path} -> {exc.code}: {_explain(exc.code, raw)}"
            if edge and exc.code == 401 and self.tier.value == "signed-in":
                # The ONE refusal a signed-in developer cannot fix themselves:
                # the token was theirs and valid, and IAP still did not take
                # it. That is the deployment not having allowlisted its own
                # Desktop client -- an operator's one-time step.
                message += (
                    f" -- you are signed in to {self.deployment.context}, so this is "
                    "the deployment refusing its own Desktop OAuth client: its IAP "
                    "must list that client id as a programmatic client "
                    "(access_settings.oauth_settings.programmatic_clients on the "
                    "backend service). Ask its operator; for this repository's own "
                    "deployment the steps are docs/runbooks/iap-desktop-client.md"
                )
            if exc.code == 429:
                raise _too_many(method, path, raw, edge, exc) from exc
            code, detail = (None, None) if edge else _error_fields(raw)
            error = SwarmError(
                message, status=exc.code, edge=edge, code=code, detail=detail
            )
            raise error from exc
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
        inputs: dict[str, Any] | None = None,
        strategy: str | None = None,
        additional_repositories: list[dict[str, str]] | None = None,
        model_class: str | None = None,
        publish_to_pr: int | str | None = None,
    ) -> dict[str, Any]:
        """Submit one task.

        `prompt` goes in `input`, never in a command line. Invariant 10: the
        caller picks a runner profile BY NAME and supplies data; it cannot
        supply an image, a command or a resource spec, and this client has no
        parameter that would let it try.

        `inputs` is the rest of `input`: the inputs the profile DECLARES, which
        every caller here has already put through `profiles.check_inputs`
        (#142). It is data for the runner, merged under the prompt, and cannot
        replace it -- `check_inputs` refuses a `prompt` key.

        `model` IS ATTRIBUTION, NOT SELECTION. It becomes the task's top-level
        `model` field -- what `TaskCreate` calls "recorded for attribution and
        cost reporting; it selects nothing about the container" -- and nothing
        carries it into the execution environment: `worker_env` carries
        identifiers and endpoints only, and the runner reads only the Job's own
        MODEL, set per profile in Terraform (#226), which this does not set
        either. Sending it changes what
        the task record says and never what runs. swarm-api fills it from the
        pin of the task's class and refuses any other value (Q4 of #1435).

        `model_class` IS SELECTION, BY NAME: `opus`, `fable`, `sonnet` or
        `haiku` (the frozen `MODEL_CLASSES`), and the platform runs that
        class's pinned model -- the caller names a class and never a model ID,
        so invariant 10 holds as it does for `runner_profile`. Sent only when
        named; a task naming none runs its profile's default.

        `strategy` is how the work comes back -- `collect` (the API's default:
        the patch is harvested, nothing is pushed) or `direct-pr` (the agent's
        branch is pushed and a pull request opened). It is a delivery choice,
        not an execution parameter: it selects no image, command, resource or
        model. Sent only when given, so a caller that names none gets exactly
        the payload it got before the field existed.

        `additional_repositories` are the registered repositories the task
        reads beside its own (#1434), by NAME (`check_additional_repositories`):
        swarm-api resolves each to its registration and pins its ref to a
        commit. Never cloned from a URL given here, and never written back.

        `publish_to_pr` is an existing pull request this task's commits are
        pushed onto instead of opening one (#2183): a delivery choice, like
        `strategy`, checked by `workflows.check_publish_to_pr` and sent only
        when given. swarm-api reads the pull request and refuses one it may
        not push onto.
        """
        payload = task_payload(
            prompt=prompt,
            runner_profile=runner_profile,
            repository_url=repository_url,
            repository_ref=repository_ref,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            model=model,
            inputs=inputs,
            strategy=strategy,
            additional_repositories=additional_repositories,
            model_class=model_class,
            publish_to_pr=publish_to_pr,
        )
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

    def tenants_mine(self) -> list[dict[str, Any]]:
        """`GET /v1/tenants/mine`: every tenant this caller may choose, admin order.

        Each entry is `{tenant_id, display_name}`. Empty for a caller whose
        tenant is personal: that is not a membership and cannot be chosen.
        """
        listing = self.request("GET", "/v1/tenants/mine")
        if not isinstance(listing, list):
            raise SwarmError(
                f"GET /v1/tenants/mine answered {type(listing).__name__}, not a list: "
                "this deployment's API predates tenant choice (#447)"
            )
        return [entry for entry in listing if isinstance(entry, dict)]

    def task(self, task_id: str) -> dict[str, Any]:
        return unwrap_task(self.request("GET", f"/v1/tasks/{task_id}"))

    def events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v1/tasks/{task_id}/events?limit={limit}")
        if isinstance(data, dict):
            return list(data.get("events") or [])
        return list(data or [])

    def events_page(
        self,
        task_id: str,
        *,
        limit: int = 50,
        newest_first: bool = False,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """One page of a task's events: `(events, next_page_token, paged)`.

        `events` above reads the route with nothing but `limit`, which it
        answers with the OLDEST `limit` events -- the read `swarm tail` made on
        every poll, so it never printed an event after the 50th (#164). This
        one passes the route's `order` and `page_token`.

        `paged` is False when the response carried no `next_page_token` key at
        all: an API older than the paged route ignores both parameters and
        serves the oldest page, and a caller has to be able to say so rather
        than take that page for the newest.
        """
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if newest_first:
            params.append(("order", "desc"))
        if page_token:
            params.append(("page_token", page_token))
        query = urllib.parse.urlencode(params)
        data = self.request("GET", f"/v1/tasks/{task_id}/events?{query}")
        if not isinstance(data, dict):
            return list(data or []), None, False
        token = data.get("next_page_token")
        return (
            [e for e in data.get("events") or [] if isinstance(e, dict)],
            token if isinstance(token, str) and token else None,
            "next_page_token" in data,
        )

    def workflows(
        self,
        *,
        states: Sequence[str] = (),
        active: bool = False,
        limit: int | None = None,
        page_token: str | None = None,
        claude_session: str | None = None,
        repository: str | None = None,
    ) -> dict[str, Any]:
        """`GET /v1/workflows`: one page of the caller's tenant's workflows, newest first.

        `states` filters on the DERIVED state, after the route's rollup, so a
        page may come back short or empty with a `next_page_token` still set.
        `active` keeps the unfinished ones, and a route that serves it from its
        indexed stored-state query says so in `filter.stored_states`; an older
        route ignores it. The tenant is the route's (`tenant_scope`), never a
        parameter here.

        `claude_session` and `repository` (CHECK-FILTER, #1749) keep only the
        workflows of that Claude Code session / cloning that repository, on
        the server; a route that applied them echoes them in `filter`, and an
        older one ignores them -- so a caller still checks each row itself.
        """
        params: list[tuple[str, str]] = [("state", s) for s in states]
        if active:
            params.append(("active", "true"))
        if claude_session:
            params.append(("claude_session", claude_session))
        if repository:
            params.append(("repository", repository))
        if limit is not None:
            params.append(("limit", str(limit)))
        if page_token:
            params.append(("page_token", page_token))
        query = urllib.parse.urlencode(params)
        data = self.request("GET", f"/v1/workflows?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
            raise SwarmError(
                "GET /v1/workflows answered without a `workflows` list; this "
                "deployment's workflow route is not the one this client speaks to"
            )
        return data

    def artifacts(self, task_id: str) -> dict[str, Any]:
        """`GET /v1/tasks/{id}/artifacts`: the manifest, as the route serves it.

        NOT FLATTENED TO THE LIST. `complete` is what tells "no artifacts yet"
        (the task has not finished, and artifacts are uploaded when the
        attempt ends) from "this task produced none", and `artifacts_skipped`
        names what the worker dropped at the size cap. A caller that kept only
        `artifacts` would say "none" in both of those cases.
        """
        data = self.request("GET", f"/v1/tasks/{task_id}/artifacts")
        if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
            raise SwarmError(
                f"GET /v1/tasks/{task_id}/artifacts answered without an `artifacts` list; "
                "this deployment's artifacts route is not the one this client speaks to"
            )
        return data

    def artifact_content(
        self,
        task_id: str,
        name: str,
        *,
        offset: int = 0,
        limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        """`GET /v1/tasks/{id}/artifacts/content`: one window of one artifact.

        A NAME, NEVER A PATH -- the route matches it against the task's own
        manifest and rebuilds the object key itself, so nothing sent from here
        can address another object. The envelope is returned whole: `status`
        (ok / binary / absent / unreadable) must be read before `content`, and
        `next_offset` is where the next window starts when `truncated` is true.
        The content is redacted by the API at read time.
        """
        params: list[tuple[str, str]] = [("name", name), ("offset", str(max(0, offset)))]
        if limit_bytes is not None:
            params.append(("limit_bytes", str(limit_bytes)))
        query = urllib.parse.urlencode(params)
        data = self.request("GET", f"/v1/tasks/{task_id}/artifacts/content?{query}")
        if not isinstance(data, dict) or "status" not in data:
            raise SwarmError(
                f"GET /v1/tasks/{task_id}/artifacts/content answered without a `status`; "
                "this deployment's artifact route is not the one this client speaks to"
            )
        return data

    def artifact_raw(
        self, task_id: str, name: str, *, max_bytes: int, disposition: str | None = None
    ) -> RawBody:
        """`GET /v1/tasks/{id}/artifacts/raw`: one artifact's BYTES (#184, #2182).

        The same NAME-not-path resolution and the same tenant boundary as
        `artifact_content`: both are the route's, and nothing here re-derives
        them -- another tenant's task is the route's 404, raised as the
        SwarmError the text read raises. The route classifies the bytes and
        says so in `x-artifact-kind` (`image`, `text` redacted, `binary` not),
        which the caller must read before trusting the body to be an image.

        Never through the shared reads: a body of bytes is one caller's, read
        once, and is not a JSON answer to hand a second waiter. A GET's 429 is
        still retried, and a cached credential's 401 still retried once.

        `disposition` is the route's own (`inline` or `attachment`, its
        default); it changes the Content-Disposition header, never the bytes
        or their classification.
        """
        params = [("name", name)]
        if disposition is not None:
            params.append(("disposition", disposition))
        query = urllib.parse.urlencode(params)
        path = f"/v1/tasks/{task_id}/artifacts/raw?{query}"
        if self._shared is None:
            return self._request_once("GET", path, raw_limit=max_bytes)
        return self._request_retrying("GET", path, payload=None, timeout=60, raw_limit=max_bytes)

    def attempts(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Every attempt for one task, newest first, as the route serves them.

        WHY THIS IS NOT ANSWERABLE FROM THE TASK. `result_summary` is written
        once, by `finish()`, at terminal state -- so a task that failed twice
        and succeeded on the third attempt carries ONLY attempt three's
        numbers. The exit code, the error and the backend that actually ran are
        per-ATTEMPT, and `GET /v1/tasks/{id}/attempts` is where they live. That
        route already existed; this is the client method that reaches it, not a
        second way to get the same answer.

        An empty list is a MEASUREMENT here -- a task admitted but never
        attempted really has none -- so a failure to read raises rather than
        returning `[]`, which is the rule `sc` states at the top of its module
        and the one most worth keeping on a path that explains failures.
        """
        data = self.request("GET", f"/v1/tasks/{task_id}/attempts?limit={limit}")
        if isinstance(data, dict):
            attempts = data.get("attempts")
            if attempts is None:
                raise SwarmError(
                    f"GET /v1/tasks/{task_id}/attempts answered without an "
                    "`attempts` field"
                )
            return [a for a in attempts if isinstance(a, dict)]
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

    def answer(self, task_id: str, *, attempt_id: str | None = None) -> dict[str, Any]:
        """`GET /v1/tasks/{id}/answer`: the agent's final answer, as the route serves it.

        NOT FLATTENED TO THE TEXT. The route says which of four things is true
        -- `ok`, `not_yet`, `absent`, `unreadable` -- and where the answer came
        from: the last `result` event of the agent's own stdout, whole, or the
        runner's summary, which is cut at 2,000 characters and marked
        `complete: false` when it may have been. A caller that kept only
        `content` could not tell a cut answer from a whole one, or a failed
        read from an agent that said nothing.
        """
        query = urllib.parse.urlencode({"attempt_id": attempt_id} if attempt_id else {})
        data = self.request("GET", f"/v1/tasks/{task_id}/answer?{query}")
        if not isinstance(data, dict) or "status" not in data:
            raise SwarmError(
                f"GET /v1/tasks/{task_id}/answer answered without a `status`; this "
                "deployment's answer route is not the one this client speaks to"
            )
        return data

    def cancel(self, task_id: str) -> dict[str, Any]:
        return unwrap_task(self.request("POST", f"/v1/tasks/{task_id}/cancel", payload={}))

    # -- issue runs (#454) -------------------------------------------------
    #
    # `POST /v1/runs` plans an issue; the plan waits, holding no capacity
    # (invariant 1), until a person approves THE DIGEST THEY WERE SHOWN. Every
    # method here answers the run object, unwrapped from `{"run": ...}`, and
    # every refusal the developer can act on is re-worded by `run_refusal`.
    # The tenant is the route's (`tenant_scope`), never a parameter: another
    # tenant's run answers the same 404 as a missing one.

    def create_run(
        self,
        *,
        issue: str,
        plan_approval: str = "required",
        auto_merge: bool = False,
        fix_rounds: int | None = None,
        model_class: str | None = None,
    ) -> dict[str, Any]:
        """Plan `issue` (`owner/repo#N` or its URL). DATA only (invariant 10):
        no profile, image or command -- the planner and every step are the
        API's choice. `auto_merge` is sent as asked; the API refuses it until
        #295, and that refusal is passed back in words, not swallowed.

        `model_class` is the PLANNER's class, by name (#1435); each plan step's
        class is the planner's to write (R1). Sent only when named, and a
        deployment that does not take it yet refuses it with a 422 that is
        passed back as it came."""
        payload: dict[str, Any] = {
            "issue": issue,
            "plan_approval": plan_approval,
            "auto_merge": bool(auto_merge),
        }
        if fix_rounds is not None:
            payload["fix_rounds"] = fix_rounds
        if model_class:
            payload["model_class"] = model_class
        # The session that asked (SC-SESSION-SCOPE). swarm-api copies it onto
        # the planner task and every workflow the run starts, CI-fix rounds
        # included, so the whole run counts as this session's own.
        payload.update(session_tag())
        try:
            data = self.request("POST", "/v1/runs", payload=payload)
        except SwarmError as exc:
            if SESSION_KEY in payload and _refused_field(exc, SESSION_KEY):
                # A deployment older than the field refuses it as an extra
                # (RunCreate forbids extras). The run is still the caller's to
                # start; it is then untagged, and the answer says so.
                del payload[SESSION_KEY]
                try:
                    data = self.request("POST", "/v1/runs", payload=payload)
                except SwarmError as again:
                    raise run_refusal(again) from again
                run = unwrap_run(data, "POST /v1/runs")
                run["session_tag"] = (
                    "not stored: this deployment does not take `claude_session` on a run yet, so "
                    "the run is untagged and `/sc check` will not count it as this session's"
                )
                return run
            raise run_refusal(exc) from exc
        return unwrap_run(data, "POST /v1/runs")

    def runs(self, *, limit: int | None = None, page_token: str | None = None) -> dict[str, Any]:
        """One page of the caller's tenant's runs, newest first, as served."""
        params: list[tuple[str, str]] = []
        if limit is not None:
            params.append(("limit", str(limit)))
        if page_token:
            params.append(("page_token", page_token))
        query = urllib.parse.urlencode(params)
        data = self.request("GET", f"/v1/runs?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            raise SwarmError(
                "GET /v1/runs answered without a `runs` list; this deployment has no "
                "issue-run route this client speaks to"
            )
        return data

    def run(self, run_id: str) -> dict[str, Any]:
        """One run. The read ADVANCES it, as every run read does (`routes/runs.py`)."""
        try:
            data = self.request("GET", f"/v1/runs/{run_id}")
        except SwarmError as exc:
            raise run_refusal(exc, run_id=run_id) from exc
        return unwrap_run(data, f"GET /v1/runs/{run_id}")

    def approve_plan(self, run_id: str, *, plan_digest: str) -> dict[str, Any]:
        """Approve the plan whose digest is `plan_digest` -- the one SHOWN.

        Never call this with a digest read in the same breath as the approval:
        the digest is the evidence that a person saw the plan being approved,
        and a plan edited in between is refused with `plan_changed` exactly so
        that an approval cannot land on a plan nobody read.
        """
        try:
            data = self.request(
                "POST", f"/v1/runs/{run_id}/plan:approve", payload={"plan_digest": plan_digest}
            )
        except SwarmError as exc:
            raise run_refusal(exc, run_id=run_id, sent_digest=plan_digest) from exc
        return unwrap_run(data, f"approving run {run_id}")

    def edit_plan(self, run_id: str, *, plan_digest: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Replace the plan whose digest is `plan_digest` (the one opened) with `plan`."""
        try:
            data = self.request(
                "POST",
                f"/v1/runs/{run_id}/plan:edit",
                payload={"plan_digest": plan_digest, "plan": plan},
            )
        except SwarmError as exc:
            raise run_refusal(exc, run_id=run_id, sent_digest=plan_digest) from exc
        return unwrap_run(data, f"editing run {run_id}")

    def reject_plan(
        self, run_id: str, *, plan_digest: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        """Reject the run's plan. With `plan_digest`, only if it is still that plan."""
        payload: dict[str, Any] = {}
        if plan_digest:
            payload["plan_digest"] = plan_digest
        if reason:
            payload["reason"] = reason
        try:
            data = self.request("POST", f"/v1/runs/{run_id}/plan:reject", payload=payload)
        except SwarmError as exc:
            raise run_refusal(exc, run_id=run_id, sent_digest=plan_digest) from exc
        return unwrap_run(data, f"rejecting run {run_id}")

    # -- the knowledge graph (lane KG7) -------------------------------------
    def registration(self, owner: str, repo: str) -> dict[str, Any] | None:
        """The caller's tenant's registration of `owner/repo`, or None.

        Matched on the lower-cased `owner/repo` the API serves, because that
        is how the API names one registration (`repositories.repo_id_for`);
        the `repo_id` itself is a hash of the tenant, which this side does not
        always know. Walks at most REGISTRATION_PAGES pages of
        `GET /v1/repositories`: a tenant with more registrations than that is
        answered None, the same as an unregistered repository.
        """
        wanted = f"{owner}/{repo}".lower()
        token: str | None = None
        for _ in range(REGISTRATION_PAGES):
            query = urllib.parse.urlencode([("page_token", token)] if token else [])
            data = self.request("GET", f"/v1/repositories?{query}", timeout=TERRITORY_TIMEOUT)
            rows = data.get("repositories") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise SwarmError(
                    "GET /v1/repositories answered without a `repositories` list; this "
                    "deployment has no registration route this client speaks to"
                )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if f"{row.get('owner')}/{row.get('repo')}".lower() == wanted:
                    return row
            token = data.get("next_page_token") or None
            if not token:
                return None
        return None

    def territory(
        self,
        repo_id: str,
        files: list[str],
        *,
        depth: int | None = None,
        exclude_runs: Sequence[str] = (),
        exclude_pull_requests: Sequence[int] = (),
    ) -> dict[str, Any]:
        """`POST /v1/repositories/{repo_id}/territory` (lane KG3): files -> the
        territory a change to them reaches, its seams, and the open pull
        requests and live issue-run steps that already hold part of it.

        Read-only. The tenant is the route's (`tenant_scope`), never a
        parameter here, and so is every token the route reads the forge with.
        """
        payload: dict[str, Any] = {"files": list(files)}
        if depth is not None:
            payload["depth"] = depth
        if exclude_runs:
            payload["exclude_runs"] = list(exclude_runs)
        if exclude_pull_requests:
            payload["exclude_pull_requests"] = list(exclude_pull_requests)
        # `repo_id` is the API's own (`registration`), never a caller's text.
        data = self.request(
            "POST", f"/v1/repositories/{repo_id}/territory",
            payload=payload, timeout=TERRITORY_TIMEOUT,
        )
        if not isinstance(data, dict) or not isinstance(data.get("named"), list):
            raise SwarmError(
                f"POST /v1/repositories/{repo_id}/territory answered without a `named` "
                "list; this deployment's territory route is not the one this client speaks to"
            )
        return data


#: How many pages of `GET /v1/repositories` `registration` reads before it
#: gives up. A page is the API's default page size; ten covers every tenant
#: this platform has, and a dispatch must not spend its latency on a listing.
REGISTRATION_PAGES = 10

#: Seconds one knowledge-graph read may take. The territory route reads the
#: forge for open pull requests (seconds, `territory.OPEN_PULLS_TTL_SECONDS`
#: caches it); a dispatch waits for it, so it is bounded well under the
#: default 60.
TERRITORY_TIMEOUT = 20


def tenant_listing(client: Any) -> dict[str, Any]:
    """What `swarm tenants` and `swarm_tenants` show: the choices, one marked.

    `current` is what `GET /v1/tenants/me` says this client acts as -- asked
    with the same header every other call sends, so it is the API's answer and
    not this process's guess. With nothing chosen that is the first-match
    default; with a choice it is the choice, or the API's 403 refusing it,
    which propagates unchanged. `chosen` is what was asked for, None when
    nothing was. Two GETs, nothing written.

    `is_default` marks the tenant a request naming none acts in, and
    `default_source` says why (`profile`, `personal`, `first_group`, or
    `unavailable` when the preferences could not be read and such a request
    is refused 503): both the API's, from `tenants/mine`'s default entry
    (docs/design/user-profile.md §4.5). An API older than that marks no
    entry; its first entry is the headerless one by the route's documented
    order, so that one is marked and `default_source` is None, not a guess.

    `role` (`{name, source}`) and `via` (`member`, `personal`, or
    `platform_admin` for a group workspace a platform admin reaches without
    being in it) are each entry's, as the API sent them
    (docs/design/workspace-roles.md §4.3). None from an API older than roles,
    or when the API could not read the role: the listing never guesses one.
    """
    from .render import tenant_of

    mine = client.tenants_mine()
    current = tenant_of(client.request("GET", "/v1/tenants/me"))
    marked = any("is_default" in entry for entry in mine)
    default = next(
        (entry for index, entry in enumerate(mine)
         if (entry.get("is_default") is True if marked else index == 0)),
        None,
    )
    return {
        "current": current,
        "chosen": getattr(client, "tenant", None),
        "default": default.get("tenant_id") if default else None,
        "default_source": default.get("default_source") if default else None,
        "tenants": [
            {
                "tenant_id": entry.get("tenant_id"),
                "display_name": entry.get("display_name"),
                "current": entry.get("tenant_id") == current,
                "is_default": entry is default,
                "role": entry.get("role") if isinstance(entry.get("role"), dict) else None,
                "via": entry.get("via") if isinstance(entry.get("via"), str) else None,
            }
            for entry in mine
        ],
    }


def unwrap_run(payload: Any, what: str) -> dict[str, Any]:
    """The run out of `{"run": {...}}`, or a SwarmError naming what came back."""
    run = payload.get("run") if isinstance(payload, dict) else None
    if not isinstance(run, dict) or not run.get("id"):
        seen = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SwarmError(f"{what}: the response carried no `run` object (saw {seen})")
    return run


class RunRefused(SwarmError):
    """An issue-run refusal, re-worded so the developer knows what to do next.

    `status`, `code` and `detail` are the API's, unchanged, so a caller that
    branches on the refusal still can.
    """


def run_refusal(
    exc: SwarmError, *, run_id: str | None = None, sent_digest: str | None = None
) -> SwarmError:
    """The four refusals a run's caller acts on, said plainly; anything else as it came.

    * `plan_changed` (409): the plan is not the one shown. NOTHING WAS DONE,
      and the remedy is to read the run again -- never to retry with the new
      digest, which would approve a plan nobody was shown.
    * `auto_merge_unavailable` (422): visible but disabled until #295 (the
      issue's own build order). Nothing was created.
    * `invalid_plan` (422): the edit was refused and the run keeps its plan.
    * a 404 from the API: not one of this tenant's runs. Another tenant's run
      answers the same as a missing one, so neither is told apart here.
    """
    if exc.edge:
        return exc
    from .invocation import terminal_command

    def refused(text: str) -> RunRefused:
        return RunRefused(text, status=exc.status, edge=False, code=exc.code, detail=exc.detail)

    if exc.code == "plan_changed":
        current = (exc.detail or {}).get("plan_digest")
        show = terminal_command(f"sc plan show {run_id}") if run_id else "a fresh read of the run"
        return refused(
            f"the plan of run {run_id} changed since it was shown: it was {sent_digest}, it "
            f"is now {current}. Nothing was done. Read it again ({show}) and act on the "
            "plan it now holds -- do not resend with the new digest unseen"
        )
    if exc.code == "auto_merge_unavailable":
        return refused(
            "auto-merge is visible but not available yet, so nothing was created. Create "
            f"the run without auto-merge and merge its pull request yourself. The API said: {exc}"
        )
    if exc.code == "invalid_plan":
        where = f"run {run_id}" if run_id else "the run"
        return refused(
            f"the plan was refused as invalid and nothing changed: {where} still holds the "
            f"plan it had. Fix the plan and send it again. The API said: {exc}"
        )
    if exc.status == 404 and run_id:
        return refused(
            f"run {run_id} is not one of your tenant's runs, or does not exist. The API "
            f"said: {exc}"
        )
    return exc
