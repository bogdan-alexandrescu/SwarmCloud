"""QA targets: what a workspace registers to have any web application visually
QA'd, its host approval, its test credential, its run, and the worker's read of
it (docs/design/visual-qa-any-app.md §1, §2.1, §2.2's route, §6's `:run`; lane
VQ-2 TARGETS of docs/design/agent-autonomy.md §6, part of #1933).

THE TARGET, `qa_targets/{target_id}`, one document per registration, in the
registering tenant. A WORKSPACE ADMIN registers, changes and deletes one (§1:
a target sends the tenant's agents to an outside site and files into the
tenant's repository in its name); a member may run one and may not change it.
Every change appends an `admin_audit` row (`qa_target_created`, `_changed`,
`_deleted`, `_host_approved`, `_credential_set`) IN THE SAME TRANSACTION as
the document, as `swarm_api.admins` does, so a change without its row cannot
exist.

THE HOST (§1.2). There is no hostname allowlist for workers, so the approval
is the allowlist. `base_url` is `https` only, no userinfo, query or fragment,
not an IP literal (`qaspec.base_url`, the renderer's own rule), and its host
must be a public name: not single-label, not `localhost`, no internal suffix
(`INTERNAL_SUFFIXES`, swarm_egress.policy's list, restated because swarm-api
cannot import swarm-egress; tests/unit/control_plane/test_qa_targets.py holds
the two equal), and every address it resolves to public unicast. A new target
is `pending`; no run starts until a PLATFORM ADMIN approves it (VQ-Q1, "any
platform admin, audited"), naming the host they approve, so an approval can
never land on a host that changed under the approver. Changing the host or
the login's `extra_origin` sets it back to `pending`.

THE CREDENTIAL (§2.1, VQ-Q2). One Secret Manager secret per tenant,
`swarm-tenant-<tenant>-qa`, declared empty by Terraform (lane VQ-3) and its
value a JSON map keyed by target id. swarm-api adds a version (IAM V-1) and
reads the latest (V-2); NO worker account is bound to it, which is why this
module never goes through `swarm_api.credentials` (that store binds the
tenant's worker account as accessor). Until VQ-3 is applied every read and
write answers `503 qa_credential_unavailable`. The value is accepted on ONE
route (`PUT /v1/qa-targets/{id}/credential`), only with a one-time link
(`qa_target_credential_links/{sha256(token)}`: single use, bound to the
minting person's email, the tenant and the target, 15 minutes), and is never
returned, logged or written to Firestore: the target records only
`credential: {set, set_at, set_by, version}`.

THE RUN (§1.1, §5, §6). A member names a target; swarm-api renders the
workflow from it (`qaspec.render`) and stamps each browser step's agent with
`metadata.qa_target = {id, revision}`. A caller never sends the target, its
URL, its sign-in or its credential (invariant 10). Before anything spends:
the host must be `approved` (`409 qa_target_host_pending`), and a run that
files issues needs its repository registration's `agents_may_file_issues`
(AU-0's flag; off is `403 issue_writes_off` unless the run asked for
`file: "none"`). The submission is the member's own, in their tenant, through
`SubmissionService.submit_workflow` like any other, and its browser step
tasks are recorded in `qa_target_runs/{workflow_id}`.

WHY A RUN RECORD AS WELL AS THE MARK. `metadata.qa_target` is one of
`validation.RESERVED_METADATA_KEYS` (VQ2-RESERVE): a caller's mark is refused
at submission, and only `run_target` and a `visual-qa` schedule's firing
(`schedtypes/visual_qa.py` `create`, the same `:run` on a schedule) write it,
each inside `validation.qa_target_mark_allowed`. The record stays as the first defence,
which predates the reservation: the worker's route serves a target only to a
task the run record names, for that target, in that tenant, so a mark this
module did not write reads as missing however it reached the store.

THE WORKER'S ROUTE, `POST /v1/attempts/{attempt_id}/qa-target` (§2.2), is
proven and fenced as the QA identity's is (`swarm_api.qa_identity`): the
tenant's worker ID token, the attempt key's Ed25519 signature over the raw
body under this route's own purpose (`QA_TARGET_PURPOSE`), the task RUNNING
under this attempt's live lease with no cancel requested. THE TENANT IS THE
LEASE'S: every read below is scoped to `lease.tenant_id`, and another
tenant's target answers exactly as a missing one (404). A task with no mark
answers 204, the common case. Every answer with a target appends a
`live_audit` row (`qa_target_issued`: the target, the attempt, the
generation, never a value). The route is in `main.UNCAPTURED_ROUTES`.

DISCOVERY (§4.1; lane QA-DISCOVER, BGD 2026-10-23: "build discovery, so a
crawl target is started entirely from the console"). A target that asks to
`discover` and lists no `screens` runs from what its discovery found. A member
starts one (`POST /v1/qa-targets/{id}:discover`): `qaspec.render_discovery`'s
one browser step, submitted and marked exactly as `:run`'s are, and recorded
in `qa_target_runs` so the gate signs it in (start clicks included). One at a
time per target: the target's `discovering` holds the running one, claimed in
a transaction before the submission, so a second call is 409
(`detail.reason` `qa_discovery_running`) naming the workflow. When the step ends, the
`task_finished` push (`routes/admin.py` `task_finished_push`, the hook
`verdictpublish` already answers) calls `QaTargets.settle_discovery`: a
SUCCEEDED step's `screens.json` is read through the API's own tenant-checked,
redacted artifact read, held to `qaspec.discovered_screens`, and stored as
`discovered` with `discovered_at`, `_by` and `_workflow`. Anything else (a
failed step, no artifact, a refused one, nothing left) stores only
`discovery_error`, and an earlier `discovered` list stays. A wake can be lost
and a task the scheduler or reconciler ends rings none, so every read of the
target settles a discovery whose task has ended too (`settle_running`).
Storing the list never bumps `revision`: the mark a run already carries
stays the target's, and nothing a caller sends ever writes `discovered`.

EVERY REFUSAL is a fixed code and a fixed sentence: nothing from the request,
and nothing from the target's own text, is interpolated into a message.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.models import new_id, utcnow
from swarm_common.states import TERMINAL_STATES, TaskState

from . import qaspec
from .admins import AUDIT_COLLECTION
from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _AttemptFields, _lease_is_live, parse_body
from .codec import console_origin
from .errors import ApiError, Conflict, Forbidden, NotFound, ValidationFailed
from .gittokens import secret_name_for
from .handoff import LIVE_AUDIT
from .qa_identity import BadSignature, LeaseNotLive, StaleGeneration
from .repositories import (
    FORGE_HOST,
    Repositories,
    agents_may_file_issues_of,
    parse_repository,
    repo_id_for,
    repository_url,
)
from .store import LEASES, TASKS

log = logging.getLogger(__name__)

COLLECTION = "qa_targets"
#: One document per QA run, keyed by its workflow id: which step tasks this
#: module stamped with which target. The worker's route reads it.
RUNS_COLLECTION = "qa_target_runs"
#: One document per minted credential link, keyed by sha256(token). Never the token.
LINKS_COLLECTION = "qa_target_credential_links"

#: §2.1: fifteen minutes, single use. Long enough to open the link in a
#: browser and type a test account's password; short enough that a link found
#: later in a scrollback or a chat log is dead.
LINK_TTL = timedelta(minutes=15)

#: The tenant's QA secret is `swarm-tenant-<tenant>-qa` (VQ-Q2).
SECRET_SUFFIX = "qa"

#: The metadata key each browser step's agent carries (§1.1).
METADATA_KEY = "qa_target"

HOST_PENDING = "pending"
HOST_APPROVED = "approved"
HOST_REVOKED = "revoked"

AUDIT_CREATED = "qa_target_created"
AUDIT_CHANGED = "qa_target_changed"
AUDIT_DELETED = "qa_target_deleted"
AUDIT_HOST_APPROVED = "qa_target_host_approved"
AUDIT_CREDENTIAL_SET = "qa_target_credential_set"
#: The `live_audit` entry each answer with a target appends (§2.2).
AUDIT_ISSUED = "qa_target_issued"

#: The worker's signing purpose for this route; never the QA identity's, the
#: live session's, the egress ticket's or a child's.
QA_TARGET_PURPOSE = "swarm-qa-target-request/v1"
MAX_REQUEST_BYTES = 4096

#: swarm_egress.policy.INTERNAL_SUFFIXES, restated: swarm-api's image installs
#: swarm-api alone. tests/unit/control_plane/test_qa_targets.py holds them equal.
INTERNAL_SUFFIXES = (
    ".local", ".localhost", ".internal", ".cluster.local", ".svc", ".localdomain",
    ".home.arpa", ".intranet", ".lan",
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_HOST_CHARS = 253

#: §1's rate: never faster than one navigation a second, and the default ten
#: requests a second is the ceiling a registrant may lower, not raise.
DEFAULT_RATE = {"navigation_ms": 2000, "requests_per_s": 10}
MIN_NAVIGATION_MS = 1000
MAX_NAVIGATION_MS = 60_000
MAX_REQUESTS_PER_S = 10

MAX_READ_POSTS = 20
SELECTOR_MAX = 200
NAME_MAX = 100
AUTHORIZED_BY_MAX = 200
#: The credential's own bounds, checked in the route's body model.
#: The discovery step's artifact: what `qaspec.render_discovery`'s prompt has
#: `swarm_qa discover` write (`--out $SWARM_ARTIFACTS_DIR/screens.json`).
DISCOVERY_ARTIFACT = "screens.json"
#: The most of it read. 60 listed routes of 512-character paths, with the
#: tool's `discovered` summary, is well under this; past it is refused.
DISCOVERY_ARTIFACT_BYTES = 512 * 1024
#: A claim whose submission never recorded its workflow (the process died
#: between the two) frees the target after this.
DISCOVERY_CLAIM_TTL = timedelta(minutes=5)

USERNAME_MAX = 256
PASSWORD_MAX = 1024

#: The fields a registration body carries, besides `authorized_by`. Each is
#: stored only when given, so `qaspec`'s defaults apply to the rest.
SPEC_FIELDS = (
    "name", "base_url", "repository", "sign_in", "screens", "discover", "flows",
    "widths", "themes", "exclusions", "environment", "read_posts", "design_docs",
    "file", "rate", "clock",
)
REQUIRED_FIELDS = ("name", "base_url", "repository", "authorized_by")

_SELECTOR_KEYS = ("username_selector", "password_selector", "submit_selector")
_SIGN_IN_KEYS = frozenset({"kind", "login_path", "signed_in_selector", "extra_origin", "start_clicks",
                           *_SELECTOR_KEYS})
_FORBIDDEN_TEXT = re.compile(r"[\x00-\x1f\x7f`]")
_PATH = re.compile(r"/(?!/)[A-Za-z0-9._~%!$&()*+,;=:@/-]{0,511}")
_TARGET_ID = re.compile(r"^qat_[0-9a-f]{16}$")


# --------------------------------------------------------------------------
# Refusals: a code and a fixed sentence each
# --------------------------------------------------------------------------


class QaTargetHostPending(Conflict):
    code = "qa_target_host_pending"


class IssueWritesOff(Forbidden):
    code = "issue_writes_off"


class QaCredentialLinkInvalid(Forbidden):
    code = "qa_credential_link_invalid"


class QaCredentialUnavailable(ApiError):
    """503: the tenant's QA secret cannot be read or written by swarm-api --
    until lane VQ-3 declares it and binds IAM V-1 and V-2, every time."""

    status_code = 503
    code = "qa_credential_unavailable"


HOST_PENDING_MESSAGE = (
    "this QA target's host is not approved yet; a platform admin approves each host "
    "(POST /v1/qa-targets/{id}:approve-host) before any run starts against it"
)
ISSUE_WRITES_OFF_MESSAGE = (
    "the target's repository registration does not let agents file issues "
    "(agents_may_file_issues is off); a workspace admin turns it on in Repositories, "
    'or run with file: "none" for a report only'
)
ADMIN_REQUIRED_MESSAGE = (
    "only a workspace admin may register, change or delete a QA target or set its "
    "credential; a member may run one"
)
LINK_INVALID_MESSAGE = (
    "this credential link is unknown, already used, expired, or was minted for someone "
    "else or another target; ask for a new one"
)
UNAVAILABLE_MESSAGE = (
    "the workspace's QA secret (swarm-tenant-<tenant>-qa) cannot be reached by swarm-api; "
    "it is declared, with swarm-api's adder and accessor bindings, by lane VQ-3"
)
NOT_FOUND_MESSAGE = "no such QA target"
NO_SCREENS_MESSAGE = (
    "the target lists no screens and none were discovered; press Run discovery on the QA "
    "targets page (or call POST /v1/qa-targets/{id}:discover), then run it"
)
#: The 409's `detail.reason` while a discovery holds the target.
DISCOVERY_RUNNING = "qa_discovery_running"
NOT_DISCOVERING_MESSAGE = "this QA target lists its screens and does not ask to discover them"


def _invalid(message: str, *, field: str | None = None) -> ValidationFailed:
    return ValidationFailed(message, detail={"reason": "qa_target_invalid",
                                             **({"field": field} if field else {})})


def _spec_refusal(exc: qaspec.QaSpecError) -> ValidationFailed:
    # The renderer's own code travels as the detail's reason: its message
    # never quotes a value from the target.
    return ValidationFailed(exc.message, detail={"reason": exc.code})


# --------------------------------------------------------------------------
# The host
# --------------------------------------------------------------------------

Resolver = Callable[[str], Sequence[str]]


def resolve_host(host: str) -> list[str]:
    """Every address `host` resolves to, for the public-address rule. A name
    that does not resolve raises OSError."""
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def check_public_host(host: str, resolve: Resolver) -> str:
    """`host`, lower-cased, when it is a public name whose every address is
    public unicast; a 422 otherwise. swarm_egress.policy's rule, plus the
    resolution its CONNECT-time check makes at the proxy."""
    host = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _invalid("a QA target names a host, not an IP address", field="base_url")
    labels = host.split(".")
    if len(host) > MAX_HOST_CHARS or not all(_LABEL.fullmatch(label) for label in labels):
        raise _invalid("the QA target's host is not a host name", field="base_url")
    if len(labels) < 2 or host == "localhost" or any(
            ("." + host).endswith(suffix) for suffix in INTERNAL_SUFFIXES):
        raise _invalid("a QA target's host must be a public name, not an internal one",
                       field="base_url")
    try:
        addresses = list(resolve(host))
    except OSError:
        raise _invalid("the QA target's host does not resolve", field="base_url") from None
    if not addresses:
        raise _invalid("the QA target's host does not resolve", field="base_url")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            raise _invalid("the QA target's host resolves to something that is not an address",
                           field="base_url") from None
        if not address.is_global or address.is_multicast:
            raise _invalid("the QA target's host resolves to a private or internal address",
                           field="base_url")
    return host


def _origin_of(url: str) -> tuple[str, str]:
    """(origin, host) of an https URL already checked by `qaspec.base_url`."""
    parts = urlsplit(url)
    return f"https://{parts.netloc.lower()}", (parts.hostname or "").lower()


def _extra_origin(raw: Any, resolve: Resolver) -> str:
    """A login's other origin (§2.3): `https://host[:port]` and nothing more,
    held to the same host rule as `base_url`."""
    if not isinstance(raw, str) or len(raw) > 300:
        raise _invalid("sign_in.extra_origin must be https://host[:port]", field="sign_in")
    try:
        normal = qaspec.base_url({"base_url": raw})
    except qaspec.QaSpecError:
        raise _invalid("sign_in.extra_origin must be https://host[:port]", field="sign_in") from None
    origin, host = _origin_of(normal)
    if normal != origin:
        raise _invalid("sign_in.extra_origin must be https://host[:port], with no path",
                       field="sign_in")
    check_public_host(host, resolve)
    return origin


# --------------------------------------------------------------------------
# The registration's own checks, then the renderer's
# --------------------------------------------------------------------------


def _one_line(value: Any, what: str, *, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit \
            or _FORBIDDEN_TEXT.search(value):
        raise _invalid(f"{what} must be one line of text, at most {limit} characters, "
                       "with no backtick", field=field)
    return value.strip()


def _sign_in(raw: Any, resolve: Resolver, *, exclusions: Any = None) -> dict[str, Any]:
    if raw is None:
        return {"kind": "none"}
    if not isinstance(raw, Mapping) or set(raw) - _SIGN_IN_KEYS:
        raise _invalid("sign_in must be {kind: none} or {kind: own_login, login_path, "
                       "signed_in_selector, username_selector?, password_selector?, "
                       "submit_selector?, extra_origin?, start_clicks?}", field="sign_in")
    kind = raw.get("kind")
    if kind == "none" and raw.get("start_clicks") is not None:
        raise _invalid("sign_in.start_clicks is allowed only with kind own_login: they are clicks the "
                       "gate makes on the login page before it signs in", field="sign_in")
    if kind == "none":
        if set(raw) != {"kind"}:
            raise _invalid("a sign_in of kind none carries nothing else", field="sign_in")
        return {"kind": "none"}
    if kind != "own_login":
        raise _invalid("sign_in.kind must be none or own_login", field="sign_in")
    login_path = raw.get("login_path")
    if not isinstance(login_path, str) or not _PATH.fullmatch(login_path):
        raise _invalid("sign_in.login_path must be an origin-relative path", field="sign_in")
    out: dict[str, Any] = {
        "kind": "own_login",
        "login_path": login_path,
        # Required: "the page changed" is not "signed in" (§1).
        "signed_in_selector": _one_line(raw.get("signed_in_selector"), "sign_in.signed_in_selector",
                                        limit=SELECTOR_MAX, field="sign_in"),
    }
    for key in _SELECTOR_KEYS:
        if raw.get(key) is not None:
            out[key] = _one_line(raw[key], f"sign_in.{key}", limit=SELECTOR_MAX, field="sign_in")
    if raw.get("extra_origin") is not None:
        out["extra_origin"] = _extra_origin(raw["extra_origin"], resolve)
    if raw.get("start_clicks") is not None:
        # §2.2: the exclusion words are the target's too, so the check reads them.
        try:
            out["start_clicks"] = qaspec.start_clicks({"sign_in": raw, "exclusions": exclusions})
        except qaspec.QaSpecError as exc:
            raise _invalid(exc.message, field="sign_in") from None
    return out


def _rate(raw: Any) -> dict[str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) - set(DEFAULT_RATE):
        raise _invalid("rate must be {navigation_ms, requests_per_s}", field="rate")
    out = {**DEFAULT_RATE, **raw}
    nav, rps = out["navigation_ms"], out["requests_per_s"]
    if not isinstance(nav, int) or isinstance(nav, bool) \
            or not MIN_NAVIGATION_MS <= nav <= MAX_NAVIGATION_MS:
        raise _invalid(f"rate.navigation_ms must be {MIN_NAVIGATION_MS}..{MAX_NAVIGATION_MS}: "
                       "never faster than one navigation a second", field="rate")
    if not isinstance(rps, int) or isinstance(rps, bool) or not 1 <= rps <= MAX_REQUESTS_PER_S:
        raise _invalid(f"rate.requests_per_s must be 1..{MAX_REQUESTS_PER_S}", field="rate")
    return {"navigation_ms": nav, "requests_per_s": rps}


def _read_posts(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_READ_POSTS or not all(
            isinstance(p, str) and _PATH.fullmatch(p) for p in raw):
        raise _invalid(f"read_posts must be at most {MAX_READ_POSTS} origin-relative paths",
                       field="read_posts")
    return list(raw)


def check_spec(doc: Mapping[str, Any]) -> None:
    """Everything `qaspec` will refuse at `:run`, refused at registration
    instead, so a target that is stored is one that renders."""
    if doc.get("screens") is None and doc.get("discover") is None:
        raise _invalid("a QA target lists its screens or asks to discover them", field="screens")
    try:
        qaspec._names(doc.get("widths"), qaspec.PRESETS, qaspec.DEFAULT_WIDTHS, "widths")
        qaspec._names(doc.get("themes"), qaspec.THEMES, qaspec.DEFAULT_THEMES, "themes")
        qaspec._flows(doc)
        qaspec._file_settings(doc)
        qaspec._design_docs(doc)
        qaspec._clock(doc)
        if doc.get("discover") is not None:
            qaspec.render_discovery(doc)
        if doc.get("screens") is not None:
            qaspec.render(doc, file="none")
    except qaspec.QaSpecError as exc:
        raise _spec_refusal(exc) from None


def _repository(db: Any, tenant_id: str, raw: Any, *, now: Callable[[], datetime]) -> tuple[str, str]:
    """(repository URL, repo_id) of one of the tenant's REGISTERED repositories
    (§1: a URL alone is refused; filing reaches a repository only through a
    registration whose forge slot the tenant holds)."""
    text = raw.strip() if isinstance(raw, str) else ""
    prefix = f"https://{FORGE_HOST}/"
    if text.lower().startswith(prefix):
        text = text[len(prefix):].rstrip("/")
    try:
        owner, repo = parse_repository(text)
    except ValueError:
        raise _invalid("repository must be owner/repo, one of this workspace's registered "
                       "repositories", field="repository") from None
    repo_id = repo_id_for(tenant_id, owner, repo)
    record = Repositories(db, now=now).find(tenant_id, repo_id)
    if record is None:
        raise _invalid("repository must be one of this workspace's registered repositories",
                       field="repository")
    return repository_url(record.get("owner") or owner, record.get("repo") or repo), repo_id


# --------------------------------------------------------------------------
# The credential store: the tenant's one QA secret, a map by target id
# --------------------------------------------------------------------------


class QaCredentialStore(Protocol):
    def read(self, tenant_id: str) -> dict[str, Any]:
        """The tenant's map, `{}` when no version holds one. Raises
        QaCredentialUnavailable when the secret cannot be read."""

    def write(self, tenant_id: str, values: Mapping[str, Any]) -> str:
        """Add `values` as the secret's new version, disable the older ones,
        and return the new version's name. Raises QaCredentialUnavailable."""


class SecretManagerQaCredentials:
    """`swarm-tenant-<tenant>-qa`, as swarm-api's own account reaches it: add
    a version (V-1), access the latest (V-2). It never creates the secret and
    never touches its IAM policy -- Terraform declares both (lane VQ-3) -- so
    no worker account is ever bound to it from here. The client is built on
    first use, so building the app opens no connection."""

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def secret(self, tenant_id: str) -> str:
        return f"projects/{self._project_id}/secrets/{secret_name_for(tenant_id, SECRET_SUFFIX)}"

    def read(self, tenant_id: str) -> dict[str, Any]:
        from google.api_core import exceptions as gexc

        if not self._project_id:
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE)
        name = f"{self.secret(tenant_id)}/versions/latest"
        try:
            response = self._secret_client().access_secret_version(request={"name": name})
        except gexc.NotFound:
            # Declared and empty, or not declared: both read as "no map". The
            # write that follows tells the two apart.
            return {}
        except gexc.FailedPrecondition:
            return {}  # the latest version is disabled or destroyed: no map
        except gexc.GoogleAPICallError as exc:
            log.warning("qa secret unreadable tenant=%s (%s)", tenant_id, type(exc).__name__)
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE) from None
        try:
            values = json.loads(bytes(response.payload.data).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            log.warning("qa secret is not a JSON map tenant=%s", tenant_id)
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE) from None
        return values if isinstance(values, dict) else {}

    def write(self, tenant_id: str, values: Mapping[str, Any]) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        if not self._project_id:
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE)
        client = self._secret_client()
        parent = self.secret(tenant_id)
        payload = json.dumps(dict(values), sort_keys=True).encode("utf-8")
        try:
            version = client.add_secret_version(
                request={"parent": parent, "payload": secretmanager.SecretPayload(data=payload)})
        except gexc.GoogleAPICallError as exc:
            log.warning("qa secret unwritable tenant=%s (%s)", tenant_id, type(exc).__name__)
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE) from None
        finally:
            payload = b""
        name = str(getattr(version, "name", "") or f"{parent}/versions/latest")
        self._disable_older(client, parent, name, tenant_id, gexc)
        return name

    @staticmethod
    def _disable_older(client: Any, parent: str, keep: str, tenant_id: str, gexc: Any) -> None:
        """§2.1 step 3: the previous version is disabled. Best effort: the
        reader always takes `latest`, so a version left enabled is never read
        here; a refusal (V-1 is `secretVersionAdder`, which cannot disable) is
        logged with its count and is not the write's failure."""
        disabled = 0
        try:
            for version in client.list_secret_versions(
                    request={"parent": parent, "filter": "state:ENABLED"}):
                if version.name != keep:
                    client.disable_secret_version(request={"name": version.name})
                    disabled += 1
        except gexc.GoogleAPICallError as exc:
            log.warning("qa secret older versions not disabled tenant=%s disabled=%d (%s)",
                        tenant_id, disabled, type(exc).__name__)
            return
        log.info("qa secret older versions disabled tenant=%s disabled=%d", tenant_id, disabled)


# --------------------------------------------------------------------------
# Serving a target
# --------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def credential_status(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Whether a credential is set: never a value, a length or any part of one."""
    raw = doc.get("credential") or {}
    is_set = bool(raw.get("set"))
    return {
        "set": is_set,
        "set_at": _iso(raw.get("set_at")) if is_set else None,
        "set_by": raw.get("set_by") if is_set else None,
        "version": raw.get("version") if is_set else None,
    }


def to_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    body = {
        "id": doc.get("id"),
        "tenant_id": doc.get("tenant_id"),
        "revision": doc.get("revision"),
        "host": doc.get("host"),
        "host_state": doc.get("host_state"),
        "approved_by": doc.get("approved_by"),
        "approved_at": _iso(doc.get("approved_at")),
        "approved_origins": list(doc.get("approved_origins") or []),
        "repo_id": doc.get("repo_id"),
        "credential": credential_status(doc),
        "authorized_by": doc.get("authorized_by"),
        "attested_by": doc.get("attested_by"),
        "attested_at": _iso(doc.get("attested_at")),
        "created_by": doc.get("created_by"),
        "created_at": _iso(doc.get("created_at")),
        "updated_by": doc.get("updated_by"),
        "updated_at": _iso(doc.get("updated_at")),
    }
    for key in SPEC_FIELDS:
        if key in doc:
            body[key] = doc[key]
    body.update(discovery_status(doc))
    return body


def discovery_status(doc: Mapping[str, Any]) -> dict[str, Any]:
    """What the console draws for discovery: the stored list and who ran it,
    the last failure's sentence, and the running one's workflow."""
    running = doc.get("discovering") or None
    return {
        "discovered": [dict(s) for s in doc.get("discovered") or []] if doc.get("discovered") else None,
        "discovered_at": _iso(doc.get("discovered_at")),
        "discovered_by": doc.get("discovered_by"),
        "discovered_workflow": doc.get("discovered_workflow"),
        "discovery_error": doc.get("discovery_error"),
        "discovering": None if not isinstance(running, Mapping) else {
            "workflow_id": running.get("workflow_id"),
            "by": running.get("by"),
            "at": _iso(running.get("at")),
        },
    }


# --------------------------------------------------------------------------
# The registrations
# --------------------------------------------------------------------------


def _audit(txn: Any, db: Any, *, action: str, tenant_id: str, target_id: str, by: str,
           now: datetime, detail: Mapping[str, Any]) -> None:
    """One `admin_audit` row, inside `txn`. Names and ids only: never a
    credential, and never the target's own free text beyond its host."""
    txn.set(db.collection(AUDIT_COLLECTION).document(), {
        "action": action,
        "target_tenant_id": tenant_id,
        "target_qa_target_id": target_id,
        "by": by,
        "at": now,
        "detail": dict(detail),
    })


def _link_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class QaTargets:
    """`qa_targets/`, `qa_target_credential_links/` and `qa_target_runs/`.
    Every read is checked against the caller's tenant: another tenant's
    target answers as a missing one."""

    def __init__(self, db: Any, *, now: Callable[[], datetime] = utcnow,
                 resolve: Resolver = resolve_host) -> None:
        self._db = db
        self._now = now
        self._resolve = resolve

    def _ref(self, target_id: str) -> Any:
        return self._db.collection(COLLECTION).document(target_id)

    # -- reads ---------------------------------------------------------------

    def find(self, tenant_id: str, target_id: str) -> dict[str, Any] | None:
        if not _TARGET_ID.match(target_id or ""):
            return None
        snap = self._ref(target_id).get()
        data = (snap.to_dict() or {}) if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            return None
        return data

    def get(self, tenant_id: str, target_id: str) -> dict[str, Any]:
        data = self.find(tenant_id, target_id)
        if data is None:
            raise NotFound(NOT_FOUND_MESSAGE)
        return data

    def any_tenant(self, target_id: str) -> dict[str, Any]:
        """For the platform admin's approval, which acts on any workspace's."""
        if not _TARGET_ID.match(target_id or ""):
            raise NotFound(NOT_FOUND_MESSAGE)
        snap = self._ref(target_id).get()
        if not snap.exists:
            raise NotFound(NOT_FOUND_MESSAGE)
        return snap.to_dict() or {}

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        rows = self._db.collection(COLLECTION).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)).stream()
        docs = [snap.to_dict() or {} for snap in rows]
        return sorted((d for d in docs if d.get("tenant_id") == tenant_id),
                      key=lambda d: str(d.get("name") or "").lower())

    def _name_taken(self, tenant_id: str, name: str, *, besides: str | None = None) -> bool:
        return any(str(d.get("name") or "").lower() == name.lower() and d.get("id") != besides
                   for d in self.list(tenant_id))

    # -- the document a body makes ----------------------------------------------

    def _build(self, tenant_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """The checked spec fields of a registration, normalised. Raises 422."""
        doc: dict[str, Any] = {}
        for key in SPEC_FIELDS:
            if values.get(key) is not None:
                doc[key] = values[key]
        doc["name"] = _one_line(values.get("name"), "name", limit=NAME_MAX, field="name")
        try:
            doc["base_url"] = qaspec.base_url(values)
        except qaspec.QaSpecError as exc:
            raise _invalid(exc.message, field="base_url") from None
        origin, host = _origin_of(doc["base_url"])
        doc["host"] = check_public_host(host, self._resolve)
        doc["origin"] = origin
        doc["repository"], doc["repo_id"] = _repository(self._db, tenant_id, values.get("repository"),
                                                        now=self._now)
        doc["sign_in"] = _sign_in(values.get("sign_in"), self._resolve, exclusions=values.get("exclusions"))
        rate = _rate(values.get("rate"))
        if rate is not None:
            doc["rate"] = rate
        posts = _read_posts(values.get("read_posts"))
        if posts is not None:
            doc["read_posts"] = posts
        check_spec(doc)
        return doc

    # -- create, change, delete --------------------------------------------------

    def create(self, tenant_id: str, values: Mapping[str, Any], *, by: str) -> dict[str, Any]:
        authorized_by = _one_line(values.get("authorized_by"), "authorized_by",
                                  limit=AUTHORIZED_BY_MAX, field="authorized_by")
        doc = self._build(tenant_id, values)
        if self._name_taken(tenant_id, doc["name"]):
            raise Conflict("a QA target of this workspace already has this name")
        now = self._now()
        target_id = "qat_" + secrets.token_hex(8)
        doc.update({
            "id": target_id,
            "tenant_id": tenant_id,
            "revision": 1,
            "host_state": HOST_PENDING,
            "approved_by": None,
            "approved_at": None,
            "approved_origins": [],
            "credential": {"set": False, "set_at": None, "set_by": None, "version": None},
            "authorized_by": authorized_by,
            "attested_by": by,
            "attested_at": now,
            "created_by": by,
            "created_at": now,
            "updated_by": by,
            "updated_at": now,
        })
        ref = self._ref(target_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            txn.set(ref, doc)
            _audit(txn, self._db, action=AUDIT_CREATED, tenant_id=tenant_id, target_id=target_id,
                   by=by, now=now, detail={"host": doc["host"], "repo_id": doc["repo_id"]})

        _apply(transaction)
        log.info("qa target created tenant=%s target=%s", tenant_id, target_id)
        return doc

    def patch(self, tenant_id: str, target_id: str, changes: Mapping[str, Any], *,
              by: str) -> dict[str, Any]:
        """`changes` holds only the fields the caller sent; a None clears an
        optional one. A change of host or `extra_origin` resets the approval."""
        if not changes:
            raise _invalid("a change names at least one field")
        for key in REQUIRED_FIELDS:
            if key in changes and changes[key] is None:
                raise _invalid(f"{key} cannot be cleared", field=key)
        current = self.get(tenant_id, target_id)
        merged = {k: current[k] for k in SPEC_FIELDS if k in current}
        for key, value in changes.items():
            if key == "authorized_by":
                continue
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        # The repository is stored as its URL; `_repository` reads either.
        doc = self._build(tenant_id, merged)
        if doc["name"].lower() != str(current.get("name") or "").lower() \
                and self._name_taken(tenant_id, doc["name"], besides=target_id):
            raise Conflict("a QA target of this workspace already has this name")
        now = self._now()
        origins_before = (current.get("host"), (current.get("sign_in") or {}).get("extra_origin"))
        origins_after = (doc["host"], doc["sign_in"].get("extra_origin"))
        host_reset = origins_before != origins_after
        changed = sorted(k for k in SPEC_FIELDS if current.get(k) != doc.get(k))
        kept = {k: current[k] for k in current if k not in SPEC_FIELDS and k not in ("host", "origin",
                                                                                       "repo_id")}
        new = {**kept, **doc, "revision": int(current.get("revision") or 0) + 1,
               "updated_by": by, "updated_at": now}
        if "authorized_by" in changes:
            new["authorized_by"] = _one_line(changes["authorized_by"], "authorized_by",
                                             limit=AUTHORIZED_BY_MAX, field="authorized_by")
            new["attested_by"], new["attested_at"] = by, now
            changed.append("authorized_by")
        if host_reset:
            new.update(host_state=HOST_PENDING, approved_by=None, approved_at=None,
                       approved_origins=[])
        ref = self._ref(target_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists or (snap.to_dict() or {}).get("revision") != current.get("revision"):
                raise Conflict("this QA target changed while it was being changed; read it again")
            txn.set(ref, new)
            _audit(txn, self._db, action=AUDIT_CHANGED, tenant_id=tenant_id, target_id=target_id,
                   by=by, now=now, detail={"fields": changed, "host": new["host"],
                                           "host_reset": host_reset,
                                           "revision": new["revision"]})

        _apply(transaction)
        log.info("qa target changed tenant=%s target=%s revision=%d host_reset=%s",
                 tenant_id, target_id, new["revision"], host_reset)
        return new

    def delete(self, tenant_id: str, target_id: str, *, by: str,
               credentials: QaCredentialStore | None) -> None:
        """The document, its audit row, and its entry in the tenant's QA
        secret. The secret first: a target whose credential cannot be removed
        is not deleted, so the password never outlives its registration."""
        current = self.get(tenant_id, target_id)
        if credential_status(current)["set"]:
            if credentials is None:
                raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE)
            values = credentials.read(tenant_id)
            if target_id in values:
                values = {k: v for k, v in values.items() if k != target_id}
                credentials.write(tenant_id, values)
        now = self._now()
        ref = self._ref(target_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            txn.delete(ref)
            _audit(txn, self._db, action=AUDIT_DELETED, tenant_id=tenant_id, target_id=target_id,
                   by=by, now=now, detail={"host": current.get("host")})

        _apply(transaction)
        log.info("qa target deleted tenant=%s target=%s", tenant_id, target_id)

    # -- the host approval (§1.2, VQ-Q1) -----------------------------------------

    def approve_host(self, target_id: str, *, host: Any, extra_origin: Any,
                     by: str) -> dict[str, Any]:
        """A platform admin's approval of the host they NAME, which must be the
        target's host now. The declared `extra_origin` is covered only when it
        is named too (§1.2)."""
        current = self.any_tenant(target_id)
        if not isinstance(host, str) or host.strip().lower().rstrip(".") != current.get("host"):
            raise Conflict("the host named is not this QA target's host now; read it again")
        origin = current.get("origin") or f"https://{current.get('host')}"
        declared = (current.get("sign_in") or {}).get("extra_origin")
        approved = [origin]
        if extra_origin is not None:
            if not declared or extra_origin != declared:
                raise Conflict("the extra origin named is not this QA target's declared one")
            approved.append(declared)
        now = self._now()
        tenant_id = str(current.get("tenant_id") or "")
        new = {**current, "host_state": HOST_APPROVED, "approved_by": by, "approved_at": now,
               "approved_origins": approved}
        ref = self._ref(target_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists or (snap.to_dict() or {}).get("revision") != current.get("revision"):
                raise Conflict("this QA target changed while it was being approved; read it again")
            txn.set(ref, new)
            _audit(txn, self._db, action=AUDIT_HOST_APPROVED, tenant_id=tenant_id,
                   target_id=target_id, by=by, now=now,
                   detail={"host": current.get("host"), "origins": approved,
                           "registrant": current.get("created_by")})

        _apply(transaction)
        log.info("qa target host approved tenant=%s target=%s", tenant_id, target_id)
        return new

    # -- the credential (§2.1) -----------------------------------------------------

    def mint_link(self, tenant_id: str, target_id: str, *, email: str,
                  console: str | None) -> dict[str, Any]:
        self.get(tenant_id, target_id)
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires_at = now + LINK_TTL
        self._db.collection(LINKS_COLLECTION).document(_link_digest(token)).set({
            "email": email, "tenant_id": tenant_id, "target_id": target_id,
            "created_at": now, "expires_at": expires_at, "used_at": None,
        })
        path = f"/qa-targets/{target_id}/credential?t={token}"
        base = console_origin(console)
        log.info("minted a qa credential link tenant=%s target=%s", tenant_id, target_id)
        # The URL and its expiry, and the path for the console's own use when
        # no console URL is configured: never the link document.
        return {"url": f"{base}{path}" if base else None, "path": path,
                "expires_at": expires_at.isoformat()}

    def consume_link(self, token: Any, *, email: str, tenant_id: str, target_id: str) -> None:
        """Mark the link used when it is this person's, for this tenant and
        target, unused and unexpired. Otherwise 403 and nothing is written:
        someone else presenting a leaked link does not burn it."""
        if not isinstance(token, str) or not token or len(token) > 256:
            raise QaCredentialLinkInvalid(LINK_INVALID_MESSAGE)
        ref = self._db.collection(LINKS_COLLECTION).document(_link_digest(token))
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _consume(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            link = (snap.to_dict() or {}) if snap.exists else None
            if not link or link.get("used_at") is not None:
                return False
            expires_at = link.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                return False
            if (link.get("email"), link.get("tenant_id"), link.get("target_id")) != \
                    (email, tenant_id, target_id):
                return False
            txn.update(ref, {"used_at": now})
            return True

        if not _consume(transaction):
            raise QaCredentialLinkInvalid(LINK_INVALID_MESSAGE)

    def set_credential(self, tenant_id: str, target_id: str, *, username: str, password: str,
                       link_token: Any, by: str, credentials: QaCredentialStore) -> dict[str, Any]:
        """§2.1 steps 2 and 3: the link, then the secret's new version, then
        the record on the target with its audit row. The value is a local
        here and in the store, and nothing else."""
        current = self.get(tenant_id, target_id)
        if (current.get("sign_in") or {}).get("kind") != "own_login":
            raise _invalid("only a target with sign_in.kind own_login takes a credential",
                           field="sign_in")
        self.consume_link(link_token, email=by, tenant_id=tenant_id, target_id=target_id)
        values = credentials.read(tenant_id)
        values[target_id] = {"username": username, "password": password}
        try:
            version = credentials.write(tenant_id, values)
        finally:
            values = {}
            username = password = ""
        now = self._now()
        ref = self._ref(target_id)
        record = {"set": True, "set_at": now, "set_by": by, "version": version}
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                raise NotFound(NOT_FOUND_MESSAGE)
            doc = {**(snap.to_dict() or {}), "credential": record}
            txn.set(ref, doc)
            _audit(txn, self._db, action=AUDIT_CREDENTIAL_SET, tenant_id=tenant_id,
                   target_id=target_id, by=by, now=now, detail={"version": version})
            return doc

        doc = _apply(transaction)
        log.info("qa target credential set tenant=%s target=%s", tenant_id, target_id)
        return credential_status(doc)

    # -- the run (§6) ------------------------------------------------------------

    def runnable(self, tenant_id: str, target_id: str, *, file: str) -> dict[str, Any]:
        """The target, when a run may start against it now; the refusal
        otherwise, before anything spends."""
        target = self._startable(self.get(tenant_id, target_id), tenant_id, file=file)
        if target.get("screens") is None and not target.get("discovered"):
            # The discovery is the console's button (lane QA-DISCOVER); the
            # code is the renderer's, which said "run its discovery first"
            # when nothing could.
            raise ValidationFailed(NO_SCREENS_MESSAGE, detail={"reason": "qa_target_no_screens"})
        return target

    def _startable(self, target: dict[str, Any], tenant_id: str, *, file: str) -> dict[str, Any]:
        """`target` when anything may browse it now: its host approved, its
        repository registered (and filing allowed, for `file="issues"`), its
        own login's credential set. The refusal otherwise."""
        if target.get("host_state") != HOST_APPROVED:
            raise QaTargetHostPending(HOST_PENDING_MESSAGE)
        record = Repositories(self._db, now=self._now).find(tenant_id, str(target.get("repo_id") or ""))
        if record is None:
            raise Conflict("the QA target's repository is no longer registered in this workspace")
        if file == "issues" and not agents_may_file_issues_of(record):
            raise IssueWritesOff(ISSUE_WRITES_OFF_MESSAGE)
        if (target.get("sign_in") or {}).get("kind") == "own_login" \
                and not credential_status(target)["set"]:
            raise Conflict("the QA target signs in with its own login and no credential is set; "
                           "a workspace admin sets it from a credential link")
        return target

    def record_run(self, *, tenant_id: str, target: Mapping[str, Any], workflow_id: str,
                   task_ids: Sequence[str], file: str, by: str, kind: str = "run") -> None:
        self._db.collection(RUNS_COLLECTION).document(workflow_id).set({
            "workflow_id": workflow_id,
            "tenant_id": tenant_id,
            "target_id": target.get("id"),
            "revision": target.get("revision"),
            "task_ids": list(task_ids),
            "file": file,
            "kind": kind,
            "by": by,
            "at": self._now(),
        })

    # -- discovery (§4.1, lane QA-DISCOVER) ------------------------------------------

    def discoverable(self, tenant_id: str, target_id: str, *, inspection: Any) -> dict[str, Any]:
        """The target, when a discovery may start against it now: it asks to
        discover, and it may be browsed (`_startable`, with nothing filed).
        A discovery whose task has already ended is settled first, so a lost
        wake never holds the target."""
        [target] = self.settle_running(tenant_id, [self.get(tenant_id, target_id)], inspection)
        if target.get("discover") is None:
            raise _invalid(NOT_DISCOVERING_MESSAGE, field="discover")
        return self._startable(target, tenant_id, file="none")

    @staticmethod
    def _running(doc: Mapping[str, Any], now: datetime) -> Mapping[str, Any] | None:
        """The discovery that holds the target, or None. A claim that never
        recorded its workflow holds it for `DISCOVERY_CLAIM_TTL` only."""
        running = doc.get("discovering")
        if not isinstance(running, Mapping):
            return None
        if not running.get("workflow_id"):
            at = running.get("at")
            if not isinstance(at, datetime) or now - at > DISCOVERY_CLAIM_TTL:
                return None
        return running

    def claim_discovery(self, tenant_id: str, target: Mapping[str, Any], *, by: str) -> str:
        """Hold the target for one discovery, in a transaction: two calls at
        once cannot both pass. 409 (`qa_discovery_running`) when one holds it."""
        target_id = str(target.get("id"))
        ref = self._ref(target_id)
        now = self._now()
        claim = secrets.token_hex(8)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else None
            if doc is None or doc.get("tenant_id") != tenant_id:
                raise NotFound(NOT_FOUND_MESSAGE)
            running = self._running(doc, now)
            if running is not None:
                workflow_id = running.get("workflow_id")
                # `conflict`, an established code, with the reason as the
                # detail (as `_spec_refusal` does): a new code ships behind a
                # refusal switch, and this one guards a route that is new.
                detail = {"reason": DISCOVERY_RUNNING, "workflow_id": workflow_id or None}
                if workflow_id:
                    raise Conflict(f"a discovery of this QA target is running (workflow {workflow_id}); "
                                   "wait for it to end", detail=detail)
                raise Conflict("a discovery of this QA target is starting; wait for it", detail=detail)
            txn.update(ref, {"discovering": {"claim": claim, "workflow_id": None, "task_id": None,
                                             "by": by, "at": now}})

        _apply(transaction)
        return claim

    def _claimed(self, target_id: str, claim: str, change: Callable[[Mapping[str, Any]], dict[str, Any]]) -> None:
        """Apply `change(discovering)` to the target while `claim` holds it."""
        ref = self._ref(target_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            running = ((snap.to_dict() or {}) if snap.exists else {}).get("discovering")
            if isinstance(running, Mapping) and running.get("claim") == claim:
                txn.update(ref, change(running))

        _apply(transaction)

    def record_discovery(self, target_id: str, claim: str, *, workflow_id: str, task_id: str) -> None:
        self._claimed(target_id, claim, lambda running: {
            "discovering": {**running, "workflow_id": workflow_id, "task_id": task_id}})
        log.info("qa target discovery started target=%s workflow=%s", target_id, workflow_id)

    def release_discovery(self, target_id: str, claim: str) -> None:
        """The submission failed: free the target at once."""
        self._claimed(target_id, claim, lambda running: {"discovering": None})

    def settle_running(self, tenant_id: str, docs: Sequence[dict[str, Any]],
                       inspection: Any) -> list[dict[str, Any]]:
        """`docs`, each running discovery whose task has ended settled and
        read again. One task read per running discovery, none otherwise."""
        out: list[dict[str, Any]] = []
        for doc in docs:
            running = doc.get("discovering")
            task_id = running.get("task_id") if isinstance(running, Mapping) else None
            if isinstance(task_id, str) and task_id and self.settle_discovery(
                    task_id, inspection, tenant_id=tenant_id):
                doc = self.find(tenant_id, str(doc.get("id"))) or doc
            out.append(doc)
        return out

    def settle_discovery(self, task_id: str, inspection: Any, *, tenant_id: str | None = None) -> bool:
        """The discovery `task_id` ran has ended: store what it found, or why
        nothing. True when the target was written. A doorbell, as
        `verdictpublish.on_task_finished` is: everything is read again, and a
        task that is not a running discovery's, not ended, or of another
        tenant than the wake said is a no-op."""
        snap = self._db.collection(TASKS).document(task_id).get()
        task = (snap.to_dict() or {}) if snap.exists else None
        mark = ((task or {}).get("metadata") or {}).get(METADATA_KEY)
        if task is None or not isinstance(mark, Mapping) or not isinstance(mark.get("id"), str):
            return False
        owner = str(task.get("tenant_id") or "")
        if not owner or (tenant_id and tenant_id != owner):
            return False
        try:
            state = TaskState(task.get("state"))
        except ValueError:
            return False
        if state not in TERMINAL_STATES:
            return False
        target = self.find(owner, mark["id"])
        running = (target or {}).get("discovering")
        if not isinstance(running, Mapping) or running.get("task_id") != task_id:
            return False
        workflow_id = str(running.get("workflow_id") or "")
        found: list[dict[str, str]] | None = None
        error: str | None = None
        if state is not TaskState.SUCCEEDED:
            error = (f"the discovery step ended {state.value} before its screens could be read; "
                     f"workflow {workflow_id} says why")
        else:
            try:
                found = qaspec.discovered_screens(target, self._artifact(inspection, owner, task_id))
            except qaspec.QaSpecError as exc:
                error = f"the discovery's {DISCOVERY_ARTIFACT} was not stored: {exc.message}"
        now = self._now()
        if found is not None:
            change: dict[str, Any] = {
                "discovered": found, "discovered_at": now, "discovered_by": running.get("by"),
                "discovered_workflow": workflow_id, "discovery_error": None, "discovering": None,
            }
        else:
            change = {"discovery_error": error, "discovering": None}
        ref = self._ref(str(target["id"]))
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            current = (snap.to_dict() or {}).get("discovering") if snap.exists else None
            if not isinstance(current, Mapping) or current.get("task_id") != task_id:
                return False
            txn.update(ref, change)
            return True

        written = _apply(transaction)
        if written:
            log.info("qa target discovery settled tenant=%s target=%s task=%s screens=%s",
                     owner, target["id"], task_id, len(found) if found is not None else "none")
        return written

    @staticmethod
    def _artifact(inspection: Any, tenant_id: str, task_id: str) -> Any:
        """The step's `screens.json`, parsed, through the API's own
        tenant-checked, redacted artifact read (`InspectionService`). Raises
        QaSpecError saying what is wrong, never what it holds."""
        def refused(why: str) -> qaspec.QaSpecError:
            return qaspec.QaSpecError("qa_target_invalid", why)

        if inspection is None:
            raise refused("SwarmCloud could not read the step's artifacts")
        try:
            row = inspection.read_artifact(tenant_id, task_id, submitted_by=None, name=DISCOVERY_ARTIFACT,
                                           limit_bytes=DISCOVERY_ARTIFACT_BYTES)
        except ApiError:
            raise refused(f"the step uploaded no {DISCOVERY_ARTIFACT}") from None
        total = row.get("total_bytes")
        if isinstance(total, int) and total > DISCOVERY_ARTIFACT_BYTES:
            raise refused(f"it is over {DISCOVERY_ARTIFACT_BYTES} bytes")
        if row.get("status") != "ok" or row.get("truncated") or row.get("invalid_utf8_bytes"):
            raise refused("it could not be read whole as text")
        if row.get("redacted"):
            raise refused("it holds text the redaction masks")
        try:
            return json.loads(row.get("content") or "")
        except ValueError:
            raise refused("it is not JSON") from None


def render_run(target: Mapping[str, Any], *, screens: Sequence[str] | None,
               file: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """(the `WorkflowCreate` body, the mark, the browser step ids) for a run
    of `target`. The body is `qaspec.render`'s spec in the API's shape, with
    `metadata.qa_target` on each browser step: written here, never accepted
    from a caller (§1.1). Raises 422 for anything the spec cannot hold."""
    try:
        spec = qaspec.render(target, screens=screens, file=file)
        mark = qaspec.qa_target_metadata(target)
    except qaspec.QaSpecError as exc:
        raise _spec_refusal(exc) from None
    return _api_workflow(spec, mark)


def render_discovery_run(target: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """`render_run` for the target's discovery (§4.1): `qaspec.render_discovery`'s
    one browser step, marked as a run's are, so the gate signs it in."""
    try:
        spec = qaspec.render_discovery(target)
        mark = qaspec.qa_target_metadata(target)
    except qaspec.QaSpecError as exc:
        raise _spec_refusal(exc) from None
    return _api_workflow(spec, mark)


def _api_workflow(spec: Mapping[str, Any],
                  mark: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """A `qaspec` spec in the API's `WorkflowCreate` shape, the mark on each browser step."""
    browser = set(qaspec.browser_steps(spec))
    steps: list[dict[str, Any]] = []
    for step in spec["steps"]:
        body: dict[str, Any] = {
            "step_id": step["step_id"],
            "runner_profile": step["runner_profile"],
            "input": {"prompt": step["prompt"], **(step.get("inputs") or {})},
            "depends_on": list(step.get("depends_on") or []),
            "input_from": dict(step.get("input_from") or {}),
            "timeout_seconds": step["timeout_seconds"],
            "allow_empty_diff": bool(step.get("allow_empty_diff")),
            "model_class": step["model_class"],
        }
        if step["step_id"] in browser:
            body["metadata"] = {METADATA_KEY: dict(mark)}
        steps.append(body)
    workflow = {
        "steps": steps,
        "strategy": spec["strategy"],
        "repository_url": spec["repository_url"],
        "on_step_failure": spec["on_step_failure"],
        "metadata": {"origin": "qa-target", "unit": spec["label"], "title": spec["title"],
                     "merge": spec["merge"]},
    }
    return workflow, mark, sorted(browser)


# --------------------------------------------------------------------------
# The worker's route (§2.2)
# --------------------------------------------------------------------------


#: A flow step's id is this prefix and the flow's id (`qaspec.render`).
FLOW_STEP_PREFIX = "flow-"


class QaTargetRequest(_AttemptFields):
    task_id: str = Field(min_length=1, max_length=128)


def qa_target_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs to ask for its attempt's target: the live-session
    shape under this route's own purpose."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{QA_TARGET_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


class QaTargetIssuer:
    def __init__(self, *, settings: Any, db: Any, children: ChildService | None,
                 credentials: QaCredentialStore | None, now: Callable[[], datetime] = utcnow,
                 clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._db = db
        self._children = children
        self._credentials = credentials
        self._now = now
        self._clock = clock

    def _prove(self, authorization: str | None, attempt: AttemptTuple, *, path: str,
               body: bytes, proof: str | None, timestamp: str | None) -> None:
        children = self._children
        unproven = BadSignature("the attempt proof did not verify for this attempt")
        if children is None or not children._keys.configured:
            raise unproven
        try:
            children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise BadSignature("only this tenant's worker may ask for its QA target") from None
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self._settings, "child_proof_skew_seconds", 120))
        if abs(self._clock() - int(timestamp)) > skew:
            raise unproven
        registration = children._registration(attempt)
        if registration is None:
            raise unproven
        if not verify_proof(registration["public_key"], proof,
                            qa_target_message("POST", path, body, timestamp)):
            raise unproven

    def _fence(self, attempt: AttemptTuple) -> tuple[dict[str, Any], dict[str, Any]]:
        task_snap = self._db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self._db.collection(LEASES).document(attempt.lease_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if task is None or task.get("tenant_id") != attempt.tenant_id:
            raise NotFound("no such task")
        if int(task.get("current_generation") or 0) != attempt.generation:
            raise StaleGeneration("this attempt's generation is not the task's live one")
        if (
            task.get("state") != TaskState.RUNNING.value
            or task.get("cancel_requested")
            or not _lease_is_live(task, lease, attempt, self._now())
        ):
            raise LeaseNotLive(
                "the task is not RUNNING under this attempt's live lease, or a cancel was requested")
        return task, dict(lease or {})

    def _run_names(self, tenant_id: str, task: Mapping[str, Any], target_id: str,
                   task_id: str) -> bool:
        workflow_id = task.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return False
        snap = self._db.collection(RUNS_COLLECTION).document(workflow_id).get()
        run = (snap.to_dict() or {}) if snap.exists else None
        return bool(run) and run.get("tenant_id") == tenant_id and run.get("target_id") == target_id \
            and task_id in (run.get("task_ids") or [])

    def issue(self, authorization: str | None, attempt_id: str, raw: bytes, *, path: str,
              proof: str | None, timestamp: str | None) -> dict[str, Any] | None:
        """The attempt's target, or None (204) when its agent has none."""
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValidationFailed(f"a QA target request is at most {MAX_REQUEST_BYTES} bytes")
        body: QaTargetRequest = parse_body(QaTargetRequest, raw)
        if body.attempt_id != attempt_id:
            raise ValidationFailed("the body's attempt_id is not the path's")
        attempt = AttemptTuple(body.tenant_id, body.task_id, body.attempt_id, body.lease_id,
                               body.generation)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof, timestamp=timestamp)
        task, lease = self._fence(attempt)
        # THE TENANT IS THE LEASE'S (invariant 9), never a name in the body.
        tenant_id = str(lease.get("tenant_id") or "")
        mark = (task.get("metadata") or {}).get(METADATA_KEY)
        if mark is None:
            return None
        missing = NotFound(NOT_FOUND_MESSAGE)
        if not isinstance(mark, Mapping) or not isinstance(mark.get("id"), str):
            raise missing
        target_id = mark["id"]
        target = QaTargets(self._db, now=self._now).find(tenant_id, target_id)
        if target is None or not self._run_names(tenant_id, task, target_id, attempt.task_id):
            raise missing
        if target.get("host_state") != HOST_APPROVED:
            raise QaTargetHostPending(HOST_PENDING_MESSAGE)
        if target.get("revision") != mark.get("revision"):
            raise Conflict("the QA target changed since this run started; start a new run")
        answer = self._answer(target)
        answer["mutating"] = self._mutating(target, task)
        sign_in = target.get("sign_in") or {"kind": "none"}
        if sign_in.get("kind") == "own_login":
            answer["sign_in"].update(self._credential(tenant_id, target))

        now = self._now()
        entry_id = new_id("la")
        self._db.collection(LIVE_AUDIT).document(entry_id).set({
            "id": entry_id,
            "entry": AUDIT_ISSUED,
            "task_id": attempt.task_id,
            "tenant_id": tenant_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
            "email": None,
            "at": now,
            "detail": {"target_id": target_id, "revision": target.get("revision"),
                       "sign_in": sign_in.get("kind")},
        })
        log.info("qa target issued tenant=%s task=%s attempt=%s generation=%d target=%s",
                 tenant_id, attempt.task_id, attempt.attempt_id, attempt.generation, target_id)
        return answer

    @staticmethod
    def _answer(target: Mapping[str, Any]) -> dict[str, Any]:
        """The target's public settings: what the gate enforces (§3) and where
        it signs in (§2.2). Nothing a caller could not read on GET."""
        sign_in = dict(target.get("sign_in") or {"kind": "none"})
        origin = target.get("origin") or f"https://{target.get('host')}"
        served_sign_in: dict[str, Any] = {"kind": sign_in.get("kind", "none")}
        if served_sign_in["kind"] == "own_login":
            served_sign_in["login_url"] = origin + sign_in["login_path"]
            for key in ("signed_in_selector", *_SELECTOR_KEYS):
                if sign_in.get(key) is not None:
                    served_sign_in[key] = sign_in[key]
            if sign_in.get("start_clicks"):
                # §2.2: the gate clicks these on the login page before the form.
                served_sign_in["start_clicks"] = [dict(c) for c in sign_in["start_clicks"]]
        paths, controls = qaspec._exclusions(target)
        return {
            "target_id": target.get("id"),
            "revision": target.get("revision"),
            "base_url": target.get("base_url"),
            # Only what a platform admin approved: the base origin, and the
            # login's other origin when it was named in the approval.
            "origins": list(target.get("approved_origins") or [origin]),
            "environment": target.get("environment") or "production",
            "rate": dict(target.get("rate") or DEFAULT_RATE),
            "read_posts": list(target.get("read_posts") or []),
            "exclusions": {"paths": paths, "controls": controls},
            "sign_in": served_sign_in,
        }

    @staticmethod
    def _mutating(target: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
        """True only for a flow step of this run whose flow the stored target
        declares `mutating: true`, on a `non_production` target (§3; BGD,
        2026-10-22, lane VQ4-MUTATING). The flow is the task's own `step_id`,
        `flow-<id>` as `qaspec.render` names it: the mark is one value for
        every browser step of the run (`validation.qa_target_mark_allowed`
        admits only that value), and only a rendered run carries the mark, so
        the step id beside it was written by this service too. Nothing in the
        request is read. A capture or discovery step, an undeclared flow, a
        production target, and anything unreadable answer False."""
        if target.get("environment") != "non_production":
            return False
        step_id = task.get("step_id")
        if not isinstance(step_id, str) or not step_id.startswith(FLOW_STEP_PREFIX):
            return False
        flow_id = step_id[len(FLOW_STEP_PREFIX):]
        flows = target.get("flows")
        if not flow_id or not isinstance(flows, list):
            return False
        return any(isinstance(flow, Mapping) and flow.get("id") == flow_id
                   and flow.get("mutating") is True for flow in flows)

    def _credential(self, tenant_id: str, target: Mapping[str, Any]) -> dict[str, str]:
        if not credential_status(target)["set"]:
            raise Conflict("the QA target signs in with its own login and no credential is set")
        if self._credentials is None:
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE)
        entry = self._credentials.read(tenant_id).get(str(target.get("id")))
        if not isinstance(entry, Mapping) or not isinstance(entry.get("username"), str) \
                or not isinstance(entry.get("password"), str):
            raise QaCredentialUnavailable(UNAVAILABLE_MESSAGE)
        return {"username": entry["username"], "password": entry["password"]}


__all__ = [
    "COLLECTION",
    "INTERNAL_SUFFIXES",
    "LINK_TTL",
    "QA_TARGET_PURPOSE",
    "QaCredentialUnavailable",
    "QaTargetIssuer",
    "QaTargets",
    "SecretManagerQaCredentials",
    "check_public_host",
    "qa_target_message",
    "render_run",
    "resolve_host",
    "to_api",
]
