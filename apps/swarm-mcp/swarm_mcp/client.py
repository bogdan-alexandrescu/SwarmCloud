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

import json
import os
import re
import subprocess
import threading
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
    over the front door at https://swarm.saga.xyz. `resolve_api_url` only ever
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
        context: str | None = None,
        deployment: Any = None,
        tier: Any = None,
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

        if self.tier is _auth.Tier.IAP:
            # The audience IAP accepts is its OAuth client id, never the
            # service URL. Getting this wrong is the "Invalid JWT audience"
            # that sends people looking at their IAM policy.
            self._audience = os.environ.get("SWARM_IAP_CLIENT_ID", "").strip()
        else:
            self._audience = audience or os.environ.get("API_AUDIENCE", "").strip() or None

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
        door at https://swarm.saga.xyz, as bogdan@saga.xyz:

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

    # -- transport ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        """One API call; ONE retry when a cached credential is refused with 401.

        The retry exists because tokens are now cached (see `access_token`)
        and the cache cannot always know how long a token has left. It is
        taken only when the refused credential came from the cache, so a
        credential that is simply not accepted -- a gcloud user token at IAP,
        answered 401 with error code 900 -- costs one round trip, as before,
        and not two.
        """
        cached = self._sends_cached_credential()
        try:
            return self._send(method, path, payload=payload, timeout=timeout)
        except SwarmError as exc:
            if exc.status == 401 and cached and self.forget_credentials():
                return self._send(method, path, payload=payload, timeout=timeout)
            raise

    def _send(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        timeout: int,
    ) -> Any:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if self.sends_own_token:
            req.add_header("Authorization", f"Bearer {self.credential()}")
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
            raise SwarmError(message, status=exc.code, edge=edge) from exc
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
        identifiers and endpoints only, and the runner reads `input.model` or
        the Job's own MODEL, neither of which this sets. Sending it changes what
        the task record says and never what runs. That is deliberate rather than
        unfinished; choosing a model per task is an execution parameter from a
        caller, which invariant 10 forbids without a contract change.

        `strategy` is how the work comes back -- `collect` (the API's default:
        the patch is harvested, nothing is pushed) or `direct-pr` (the agent's
        branch is pushed and a pull request opened). It is a delivery choice,
        not an execution parameter: it selects no image, command, resource or
        model. Sent only when given, so a caller that names none gets exactly
        the payload it got before the field existed.
        """
        payload: dict[str, Any] = {
            "runner_profile": runner_profile,
            "input": {**(inputs or {}), "prompt": prompt},
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
        if strategy:
            payload["strategy"] = strategy
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
        query = f"?{urllib.parse.urlencode({'attempt_id': attempt_id})}" if attempt_id else ""
        data = self.request("GET", f"/v1/tasks/{task_id}/answer{query}")
        if not isinstance(data, dict) or "status" not in data:
            raise SwarmError(
                f"GET /v1/tasks/{task_id}/answer answered without a `status`; this "
                "deployment's answer route is not the one this client speaks to"
            )
        return data

    def cancel(self, task_id: str) -> dict[str, Any]:
        return unwrap_task(self.request("POST", f"/v1/tasks/{task_id}/cancel", payload={}))
