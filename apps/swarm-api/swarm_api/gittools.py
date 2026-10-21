"""Runner git tools, swarm-api's half: the route's checks, policy, allowlist and audit.

docs/runner-git-tools.md §1.2-§1.4, §2 and §4 are the design; this is lane G5
of #1225. The worker's half (`agent_worker.gittools`, lane G3) validates,
scans, bundles and signs; `swarm_api.gitexec` (lane G4) runs git with the
token. This module is what stands between them, and it is the check that
counts: "the worker's check can be wrong, or the worker can be the agent's
target; the API's check holds either way" (§1.2).

EVERY CALL, IN THIS ORDER (§1.3 step 3):

1. **who.** `ChildService.authenticate_worker` (the ID token is the tenant's
   worker service account, audience-pinned) and `ChildService.verify_proof`
   (the signature verifies against the attempt key this attempt registered
   before its agent existed). EVERY failure is the same 403,
   `git_tool_unproven`, with one constant sentence: no token, a person's
   token, another tenant's worker, no proof, a stale timestamp, a key nobody
   registered, a body the proof did not sign, an attempt id in the path that
   is not the body's. An unproven caller writes nothing, not even an audit
   row, so the route cannot be used to fill the audit.
2. **the fence (invariant 5).** ONE transaction reads the task, its lease and
   the attempt's quota: the task must be RUNNING under exactly this
   attempt's live lease (`children._lease_is_live`) with no cancel requested.
   A stale attempt is 409 `git_fenced`, and its `fenced` audit row is written
   IN THAT TRANSACTION, so a fenced call is recorded whichever way it ends.
   The per-attempt limits (Q6 = A: 60 calls and 20 pushes an hour) are
   counted in the same transaction.
3. **the signed spec.** Every policy input -- the repository, the strategy,
   the dispatch block, the forge credential and its access mode -- is read
   from the task document only after its spec signature verifies
   (`verdictpublish.spec_refusal`, the worker's checks restated). A task
   document an agent rewrote is `spec_unverified`, before the forge is read.
4. **the policy (§1.2)**, then the slot (§2.1), read per call in this
   process (`read_slot`, never cached), then the tool.

THE PUSH POLICY, DERIVED, NEVER READ AS A NAME. The one branch a task may push
is computed from the signed dispatch block exactly as the end-of-task publish
computes it (`agent_worker.lifecycle.Worker._publish_git`): a `single-pr`
amender pushes `swarm/<pr_author>`, a `direct-pr` continuation
`swarm/<continues>`, everything else `swarm/<its own id>`; a `collect` step
without `carrier: branches` and a `single-pr` reader push nothing. The branch
in the request is only ever COMPARED with that one. Checked in this order, so
each refusal names what it is: `strategy_pushes_nothing` (before any token is
read), `forge_write_refused` (a read access mode, or the submitter's grant
re-read and no longer `write`; before the token is read), `protected_branch`
(the repository's default branch as the forge reports it, never assumed to
be `main`, and any branch the forge reports a blocking rule on),
`not_this_tasks_branch`.

Q1 = A, THE LEASE-GUARDED REWRITE. A push may replace the task's own branch
only under `--force-with-lease` on the tip swarm-api ITSELF recorded for that
task and branch: the tip its last push answered with, or the tip the task's
first fetch of the branch resolved to. Never a sha the request carries. The
record (`git_tool_tips`) is attested with an HMAC under `swarm-child-key`,
which no tenant identity holds, so a record a tenant identity rewrote to
point at someone else's commit is no lease at all and the push is only a
fast-forward. A branch someone else moved is therefore refused
`non_fast_forward`, and nothing is ever blind-forced. A push that replaced
history is audited `rewrite: true`.

THE ALLOWLIST (Q3 = A). `read_private` reads another repository only when
the task's tenant marked its registration `readable_by_tasks`
(`repositories.readable_by_tasks_of`). A repository off the list, one that
does not exist, and one the credential cannot read get ONE answer -- the same
status, code and sentence -- so the tool cannot be used to learn which
repositories exist. The credential is the repository's own `git-r-` slot when
the tenant registered one, else the task's slot. The read is a GET.

THE TOKEN. Read per call, held in this request's frame, handed to
`gitexec` (which keeps it out of every argv and environment) or to
`GitHubWriter` (which sends it only to api.github.com). Every message this
module returns or stores went through `gitexec.scrub` with the token. The
audit row names the slot by its SUFFIX (`git`, `git-u-<hex>`, `git-r-<hex>`),
never a value; it never carries file contents, log text or a URL.

THE AUDIT (§4). One row per proven call in `git_tool_audit`, id
`{attempt_id}:{inverted microseconds}:{random}` so an attempt's calls list
newest first by id (microseconds rather than §4's milliseconds: two calls of
one attempt inside a millisecond are not ordered by a millisecond clock). A
call that reaches the forge writes its row after the forge answers; if that
write fails the answer still returns and `git_audit_write_failed` is logged
with the ids. A tenant member reads a task's rows through
`GET /v1/tasks/{task_id}/git-calls`.

Q2 = A: there is no fallback token, so there is no `/git/token` route.

INVARIANTS. Nothing here creates infrastructure demand (1), touches capacity
(2, 3) or the lease (5: a fenced call is refused, never released). The
tenant's own slot, its own GCS prefix for a large bundle, and its own
repository records only (9). The caller names a tool and its arguments,
never a URL, a remote, a refspec or a credential (10).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import quote

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from swarm_common.admission import _snapshot
from swarm_common.models import FORGE_ACCESS, FORGE_CREDENTIAL, Tenant, utcnow
from swarm_common.states import TaskState

from .childkey import AttemptTuple
from .children import ChildService, ChildUnauthenticated, ChildUnproven, _lease_is_live, parse_body
from .cifix import BRANCH_PREFIX
from .errors import ApiError, NotFound
from .forge import GIT_PROVIDER
from .forgechecks import evaluate, required_status_checks
from .forgewrite import (
    ForgeUnreachable,
    ForgeWriteError,
    ForgeWriteForbidden,
    ForgeWriteNotFound,
    ForgeWriteUnauthorized,
    GitHubWriter,
)
from .gitexec import MAX_HAVES, GitExec, GitExecError, scrub
from .gittokens import COLLECTION as GIT_TOKENS
from .gittokens import Scope, provider_suffix, secret_name_for, token_id_for
from .repositories import Repositories, parse_repository, readable_by_tasks_of, repo_id_for
from .store import LEASES, TASKS, Store
from .validation import DISPATCH_METADATA_KEY, IssueRef
from .verdictpublish import grant_refusal, spec_refusal

log = logging.getLogger(__name__)

#: The five tools (§1.1). `git_rebase_onto` is served as the fetch it begins
#: with; the rebase itself runs in the agent's clone, which holds no secret.
FETCH, REBASE, PUSH, READ, CHECKS = (
    "git_fetch", "git_rebase_onto", "git_push_branch", "read_private", "pr_checks")
TOOLS = (FETCH, REBASE, PUSH, READ, CHECKS)

#: The collections, all written by swarm-api only. Not frozen (§9).
AUDIT = "git_tool_audit"
TIPS = "git_tool_tips"
QUOTA = "git_tool_quota"

#: Q6 = A (owner, 2026-10-13): per attempt, per hour. They keep a looping
#: agent from spending the tenant's forge quota; the worker counts too, but
#: only this count holds.
CALLS_PER_HOUR = 60
PUSHES_PER_HOUR = 20
_HOUR = 3600.0
#: Q6 = A: a bundle up to this rides in the body; a larger one is an object
#: under the tenant's own GCS prefix (`agent_worker.gittools.INLINE_BUNDLE_BYTES`).
INLINE_BUNDLE_BYTES = 24 * 1024 * 1024
#: Q6 = A: the largest file `read_private` returns, and each red row's log tail.
MAX_FILE_BYTES = 1024 * 1024
LOG_TAIL_BYTES = 8 * 1024
#: The most a directory listing carries; GitHub's contents API lists 1,000.
MAX_ENTRIES = 1000
#: A request body: a push carries its bundle base64'd, everything else a few names.
MAX_PUSH_BODY_BYTES = (INLINE_BUNDLE_BYTES * 4) // 3 + 64 * 1024
MAX_BODY_BYTES = 64 * 1024
#: `agent_worker.gittools.BUNDLE_OBJECT`: where a large push bundle is, under
#: the tenant's prefix (invariant 9). A large fetch bundle is put beside it.
BUNDLE_OBJECT = "tenants/{tenant}/attempts/{attempt}/git/{call}.bundle"
FETCH_OBJECT = "tenants/{tenant}/attempts/{attempt}/git/{call}.fetch.bundle"
#: The refusal when the bucket will not take a large fetch bundle. A 422, not
#: the bare 500 it was: the worker retries a 5xx, and this does not heal on retry.
FETCH_STORE_FAILED = "fetch_bundle_store_failed"

#: The most haves a body may carry. A have only shrinks a bundle, so the
#: newest `gitexec.MAX_HAVES` of them are used and the rest dropped.
MAX_HAVES_SENT = 1024
#: Domain separation for the tip record's attestation, beside childkey's.
TIP_PURPOSE = "swarm-git-tip/v1"
#: The fenced code the worker reads (`agent_worker.gittools.GIT_FENCED`).
GIT_FENCED = "git_fenced"
#: Rule types on a branch that a push from here must not meet (GitHub's
#: repository rules API, `rules/branches/{branch}`). A `swarm/` branch never
#: carries one; this is defence in depth behind the default-branch check.
BLOCKING_RULES = frozenset({
    "update", "creation", "deletion", "non_fast_forward", "pull_request",
    "required_status_checks", "required_deployments", "merge_queue",
})
#: The shape `swarm_common.models.new_id("task")` mints
#: (`agent_worker.continuation.TASK_ID_RE`).
TASK_ID = re.compile(r"^task_[0-9a-f]{20}$")
REQUEST_ID = r"^[a-z0-9-]{1,64}$"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = r"^[0-9a-f]{64}$"
_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)
#: `Task.forge_access`'s write value, from the frozen tuple.
_WRITE = FORGE_ACCESS[0]

#: The one sentence of every unproven answer (§1.4: none is an oracle).
UNPROVEN_MESSAGE = "this request is not proven for this attempt"
#: The one sentence of every allowlist refusal (§2.3).
NOT_ALLOWLISTED_MESSAGE = (
    "that repository is not on this tenant's list of repositories a task may read, "
    "or it cannot be read; a tenant admin adds one with readable_by_tasks"
)


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------


class GitToolUnproven(ApiError):
    status_code = 403
    code = "git_tool_unproven"


class GitToolFenced(ApiError):
    status_code = 409
    code = GIT_FENCED


class GitToolRefused(ApiError):
    """A §1.2 refusal. 422 unless said otherwise; NEVER 409, which the worker
    reads as the fence and ends its attempt on."""

    status_code = 422
    code = "git_tool_refused"

    def __init__(self, code: str, message: str, *, status: int = 422,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, detail=detail)
        assert status != 409, "409 is the fence's alone"
        self.code = code
        self.status_code = status

    @property
    def result(self) -> str:
        """The audit's `result`: a refusal is the policy's, a 5xx a failure."""
        return "failed" if self.status_code >= 500 else "refused"


class _FencedMidCall(Exception):
    """The fence failed when re-checked immediately before a push (#453's rule)."""


#: gitexec's codes, as answers: what the agent can act on is a refusal.
_EXEC_STATUS = {
    "bad_ref": 422, "bad_bundle": 422, "too_large": 422, "non_fast_forward": 422,
    "remote_rejected": 422, "busy": 503, "timed_out": 504, "bad_remote": 502, "failed": 502,
}


# --------------------------------------------------------------------------
# bodies: exactly what `agent_worker.gittools.GitTools.call` sends
# --------------------------------------------------------------------------


class _Body(BaseModel):
    # Strict: an unknown key -- a `repo` on a tool that acts on the task's own
    # repository, a `url`, a `refspec`, a `force` -- is a 422 (invariant 10).
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=1)
    request_id: str = Field(pattern=REQUEST_ID)


class FetchBody(_Body):
    ref: str = Field(min_length=1, max_length=255)
    #: Every sha the worker learnt origin has, oldest first: G3 sends them
    #: all (`agent_worker.gittools._Origin.haves`), and gitexec negotiates
    #: against at most `gitexec.MAX_HAVES`, so the newest are passed on.
    haves: list[str] = Field(default_factory=list, max_length=MAX_HAVES_SENT)


class PushBody(_Body):
    branch: str | None = Field(default=None, max_length=255)
    #: What the WORKER believes the remote tip is. Recorded, never a lease.
    old_sha: str | None = Field(default=None, max_length=40)
    new_sha: str = Field(min_length=40, max_length=40)
    haves: list[str] = Field(default_factory=list, max_length=MAX_HAVES_SENT)
    commits: StrictInt = Field(ge=0)
    bundle_bytes: StrictInt = Field(ge=1)
    bundle_sha256: str = Field(pattern=_SHA256)
    bundle: str | None = None
    bundle_object: str | None = Field(default=None, max_length=512)


class ReadBody(_Body):
    repo: str = Field(min_length=3, max_length=201)
    path: str = Field(max_length=1024)
    ref: str = Field(min_length=1, max_length=255)


class ChecksBody(_Body):
    pr: StrictInt | None = Field(default=None, ge=1)


MODELS: dict[str, type[_Body]] = {
    FETCH: FetchBody, REBASE: FetchBody, PUSH: PushBody, READ: ReadBody, CHECKS: ChecksBody,
}


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PushPolicy:
    """What the signed spec lets this task push: nothing, or exactly one branch."""

    pushes: bool
    branch: str
    reason: str = ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _word(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def push_policy(task: Mapping[str, Any], task_id: str) -> PushPolicy:
    """The branch this task may push, derived from its SIGNED dispatch block.

    `agent_worker.lifecycle.Worker._publish_git`'s rules, restated: an unknown
    strategy reads as `collect` and an unknown `single-pr` role as `reader`,
    the readings that push nothing. A `pr_author` or `continues` that is not
    a task id is refused, never guessed into a branch name.
    """
    block = _mapping(_mapping(task.get("metadata")).get(DISPATCH_METADATA_KEY))
    own = f"{BRANCH_PREFIX}{task_id}"
    if _word(block.get("strategy")) == "single-pr":
        role = _word(block.get("pr_role"))
        role = role if role in ("author", "reader", "amender", "none") else "reader"
        if role in ("reader", "none"):
            return PushPolicy(False, "", f"strategy is 'single-pr' and this step's pr_role is "
                                         f"{role!r}: it pushes nothing")
        if role == "amender":
            author = block.get("pr_author")
            if not isinstance(author, str) or not TASK_ID.match(author.strip()):
                raise GitToolRefused("not_this_tasks_branch", "this amender's pr_author is not a "
                                                              "task id; no branch is derived")
            return PushPolicy(True, f"{BRANCH_PREFIX}{author.strip()}")
        return PushPolicy(True, own)
    strategy = _word(block.get("strategy"))
    strategy = strategy if strategy in ("collect", "direct-pr", "integrate") else "collect"
    if strategy == "collect" and _word(block.get("carrier")) != "branches":
        return PushPolicy(False, "", "strategy is 'collect': the patch is harvested and nothing "
                                     "is pushed")
    if "continues" in block:
        continued = block.get("continues")
        if not isinstance(continued, str) or not TASK_ID.match(continued) \
                or strategy != "direct-pr":
            raise GitToolRefused("not_this_tasks_branch", "this step's continues is not a "
                                                          "direct-pr task id; no branch is derived")
        return PushPolicy(True, f"{BRANCH_PREFIX}{continued}")
    return PushPolicy(True, own)


def check_path(path: str) -> str:
    """`checkpoint_content.py`'s rule: relative, no `..`, `.` or empty segment,
    no backslash, no control character. "" is the repository's root."""
    if path == "":
        return path
    if path.startswith("/") or "\\" in path or _CONTROL.search(path) \
            or any(part in ("", ".", "..") for part in path.split("/")):
        raise GitToolRefused("bad_path", "path must be relative, with no empty, '.' or '..' "
                                         "segment, backslash or control character")
    return path


def check_ref(ref: str) -> str:
    """A branch or tag name, or a 40-hex sha: `gitexec`'s rule, as a refusal."""
    if _SHA.match(ref) or (
        _NAME.match(ref) and not ref.startswith(("-", "/", "refs/"))
        and not ref.endswith(("/", ".", ".lock")) and ".." not in ref and "//" not in ref
        and "/." not in ref and ref not in ("HEAD", "@")
    ):
        return ref
    raise GitToolRefused("bad_ref", "ref must be a branch or tag name, or a 40-hex sha")


def task_repository(task: Mapping[str, Any]) -> IssueRef:
    """The task's own repository, from its signed `repository_url`."""
    url = task.get("repository_url")
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    try:
        if matched is None:
            raise ValueError
        owner, repo = parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        raise GitToolRefused("other_repository", "the task's repository is not an owner/repo "
                                                 "on github.com") from None
    return IssueRef(owner=owner, repo=repo, number=0)


def task_slot(task: Mapping[str, Any]) -> tuple[str, bool]:
    """(the slot suffix the task's own publish would read, may it write).

    `verdictpublish._forge_slot`'s reading without its refusal of a read mode:
    a read-only task may still fetch, read and watch CI. `forge_credential`
    absent is `git`; another shape is refused, never defaulted.
    """
    access = task.get("forge_access")
    suffix = task.get("forge_credential")
    if suffix is None:
        suffix = GIT_PROVIDER
    if not isinstance(suffix, str) or not FORGE_CREDENTIAL.fullmatch(suffix):
        raise GitToolRefused("no_credential", "the task's forge_credential is not a forge "
                                              "credential suffix")
    return suffix, access is None or access == _WRITE


def remote_url(ref: IssueRef) -> str:
    """The forge URL git is handed: the task's own repository on github.com."""
    return f"https://github.com/{ref.owner}/{ref.repo}.git"


def audit_id(attempt_id: str, now: datetime) -> str:
    micros = int(now.timestamp() * 1_000_000)
    return f"{attempt_id}:{10**17 - micros:017d}:{secrets.token_hex(4)}"


# --------------------------------------------------------------------------
# large bundles: the tenant's own GCS prefix
# --------------------------------------------------------------------------


class BundleStore(Protocol):
    def download(self, key: str, dest: Path, *, max_bytes: int) -> int: ...

    def upload(self, key: str, src: Path) -> None: ...


class InMemoryBundles:
    """A bucket in a dict, for the tests and for a deployment with no bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def download(self, key: str, dest: Path, *, max_bytes: int) -> int:
        data = self.objects.get(key)
        if data is None:
            raise FileNotFoundError(key)
        if len(data) > max_bytes:
            raise OverflowError(key)
        dest.write_bytes(data)
        return len(data)

    def upload(self, key: str, src: Path) -> None:
        self.objects[key] = src.read_bytes()


class GcsBundles:
    """The artifact bucket. The client is built on first use, never at import."""

    def __init__(self, bucket: str, project_id: str | None = None) -> None:
        self._bucket_name = bucket
        self._project_id = project_id
        self._bucket: Any = None

    def _get(self) -> Any:
        if self._bucket is None:
            from google.cloud import storage

            self._bucket = storage.Client(project=self._project_id).bucket(self._bucket_name)
        return self._bucket

    def download(self, key: str, dest: Path, *, max_bytes: int) -> int:
        blob = self._get().get_blob(key)
        if blob is None:
            raise FileNotFoundError(key)
        if int(blob.size or 0) > max_bytes:
            raise OverflowError(key)
        # Pinned to the generation whose size was just read: a replacement
        # uploaded between the two reads is not the object that was bounded.
        blob.download_to_filename(str(dest), if_generation_match=blob.generation)
        size = dest.stat().st_size
        if size > max_bytes:
            raise OverflowError(key)
        return size

    def upload(self, key: str, src: Path) -> None:
        # Create-only: generation 0 means "no live object", so an existing one
        # is never replaced (the grant, terraform/infra/git_tools.tf, is
        # objectCreator, which could not overwrite it either).
        self._get().blob(key).upload_from_filename(str(src),
                                                   content_type="application/x-git-bundle",
                                                   if_generation_match=0)


# --------------------------------------------------------------------------
# one call's audit row
# --------------------------------------------------------------------------


@dataclass
class _Call:
    """The audit row being built for one proven call. Names and shas only."""

    tool: str
    attempt: AttemptTuple
    request_id: str
    started: float
    fields: dict[str, Any] = field(default_factory=dict)
    token: str = field(default="", repr=False)

    def scrub(self, text: str) -> str:
        return scrub(text, (self.token,) if self.token else ())

    def row(self, result: str, code: str | None, at: datetime) -> dict[str, Any]:
        t = self.attempt
        return {
            "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
            "lease_id": t.lease_id, "generation": t.generation, "tool": self.tool,
            "request_id": self.request_id, "repository": None, "ref": None,
            "resolved_sha": None, "branch": None, "old_sha": None, "new_sha": None,
            "rewrite": None, "path": None, "pr": None, "bytes": None, "slot": None,
            **self.fields,
            "result": result, "code": code,
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "at": at,
        }


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


@dataclass
class GitTools:
    """The five tools' route logic. Built per request from the app context."""

    settings: Any
    db: Any
    store: Store
    children: ChildService
    forge_tokens: Any
    writer: GitHubWriter
    gitexec: GitExec
    remote_url: Callable[[IssueRef], str] = remote_url
    bundles: BundleStore | None = None
    now: Callable[[], datetime] = utcnow
    clock: Callable[[], float] = time.time

    @classmethod
    def from_context(cls, ctx: Any, *, verifier: Any, gitexec: GitExec | None = None,
                     remote_url: Callable[[IssueRef], str] = remote_url,
                     bundles: BundleStore | None = None) -> "GitTools":
        if bundles is None and getattr(ctx.settings.core, "artifact_bucket", ""):
            bundles = GcsBundles(ctx.settings.core.artifact_bucket, ctx.settings.project_id)
        children = ChildService(
            settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
            verifier=verifier, limiter=ctx.limiter, metrics=ctx.metrics, now=ctx.now,
        )
        return cls(
            settings=ctx.settings, db=ctx.db, store=ctx.store, children=children,
            forge_tokens=ctx.forge_tokens, writer=ctx.forge_writer,
            gitexec=gitexec or GitExec(), remote_url=remote_url, bundles=bundles, now=ctx.now,
        )

    # -- the call ------------------------------------------------------------

    def handle(self, tool: str, attempt_id: str, raw: bytes, *, authorization: str | None,
               proof: str | None, timestamp: str | None, path: str) -> dict[str, Any]:
        if tool not in TOOLS:
            raise NotFound("no such git tool; the tools are " + ", ".join(TOOLS))
        self.children._require_configured()
        cap = MAX_PUSH_BODY_BYTES if tool == PUSH else MAX_BODY_BYTES
        if len(raw) > cap:
            raise GitToolRefused("too_large", f"a {tool} request is at most {cap} bytes; a larger "
                                              "bundle goes to the tenant's prefix")
        body = parse_body(MODELS[tool], raw)
        attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                               body.generation)
        self._prove(body, attempt, raw=raw, authorization=authorization, proof=proof,
                    timestamp=timestamp, path=path)
        call = _Call(tool, attempt, body.request_id, time.monotonic())
        task = self._admit(call)
        try:
            unverified = spec_refusal(self.settings, task, attempt.task_id)
            if unverified:
                raise GitToolRefused("spec_unverified", unverified)
            answer = {
                FETCH: self._fetch, REBASE: self._fetch, PUSH: self._push,
                READ: self._read_private, CHECKS: self._pr_checks,
            }[tool](call, task, body)
        except GitToolRefused as refused:
            refused.message = call.scrub(refused.message)
            self._audit(call, refused.result, refused.code)
            raise
        except _FencedMidCall:
            self._audit(call, "fenced", GIT_FENCED)
            raise GitToolFenced("this attempt lost its lease while the push was prepared; "
                                "nothing was pushed") from None
        except GitExecError as exc:
            status = _EXEC_STATUS.get(exc.code, 502)
            refused = GitToolRefused(exc.code, call.scrub(exc.message), status=status,
                                     detail={k: v for k, v in exc.detail.items()
                                             if k in ("branch", "remote_sha", "new_sha")})
            self._audit(call, refused.result, refused.code)
            raise refused from None
        except ForgeWriteError as exc:
            refused = _forge_refusal(exc, call)
            self._audit(call, refused.result, refused.code)
            raise refused from None
        self._audit(call, "ok", None)
        return answer

    def _prove(self, body: _Body, attempt: AttemptTuple, *, raw: bytes,
               authorization: str | None, proof: str | None, timestamp: str | None,
               path: str) -> None:
        """§1.3 step 3.1-3.2: one 403 for every cause."""
        try:
            self.children.authenticate_worker(authorization, body.tenant_id)
            if body.attempt_id != attempt.attempt_id:
                raise ChildUnproven("the body names another attempt")
            self.children.verify_proof(attempt, method="POST", path=path, body=raw,
                                       proof=proof, timestamp=timestamp)
        except (ChildUnauthenticated, ChildUnproven):
            raise GitToolUnproven(UNPROVEN_MESSAGE) from None

    def _admit(self, call: _Call) -> dict[str, Any]:
        """§1.3 step 3.3: the fence and the limits, in one transaction."""
        attempt = call.attempt
        task_ref = self.db.collection(TASKS).document(attempt.task_id)
        lease_ref = self.db.collection(LEASES).document(attempt.lease_id)
        quota_ref = self.db.collection(QUOTA).document(attempt.attempt_id)
        now = self.now()
        wall = self.clock()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[str, Any]:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            quota_snap = _snapshot(txn.get(quota_ref))
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            quota = (quota_snap.to_dict() or {}) if quota_snap.exists else {}
            audit_ref = self.db.collection(AUDIT).document(audit_id(attempt.attempt_id, now))
            if (task is None or task.get("state") != TaskState.RUNNING.value
                    or task.get("cancel_requested")
                    or not _lease_is_live(task, lease, attempt, now)):
                # Returned, not raised: a transaction that raises commits
                # nothing, and this row must commit with the check it records.
                txn.set(audit_ref, call.row("fenced", GIT_FENCED, now))
                return "fenced", None
            if quota.get("tenant_id") not in (None, attempt.tenant_id):
                quota = {}
            calls = [t for t in quota.get("calls") or [] if isinstance(t, (int, float))
                     and wall - t < _HOUR]
            pushes = [t for t in quota.get("pushes") or [] if isinstance(t, (int, float))
                      and wall - t < _HOUR]
            over = (f"at most {CALLS_PER_HOUR} git tool calls an hour per attempt"
                    if len(calls) >= CALLS_PER_HOUR else
                    f"at most {PUSHES_PER_HOUR} pushes an hour per attempt"
                    if call.tool == PUSH and len(pushes) >= PUSHES_PER_HOUR else "")
            if over:
                txn.set(audit_ref, call.row("refused", "rate_limited", now))
                return "rate_limited", over
            calls.append(wall)
            if call.tool == PUSH:
                pushes.append(wall)
            txn.set(quota_ref, {"tenant_id": attempt.tenant_id, "task_id": attempt.task_id,
                                "attempt_id": attempt.attempt_id, "calls": calls,
                                "pushes": pushes, "updated_at": now})
            return "ok", task

        outcome, value = _apply(self.db.transaction())
        if outcome == "fenced":
            raise GitToolFenced("this attempt is not RUNNING under its live lease; it was "
                                "superseded, and nothing was done")
        if outcome == "rate_limited":
            raise GitToolRefused("rate_limited", value, status=429)
        return value

    def _live(self, attempt: AttemptTuple) -> bool:
        task_snap = self.db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self.db.collection(LEASES).document(attempt.lease_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        return bool(task and task.get("state") == TaskState.RUNNING.value
                    and not task.get("cancel_requested")
                    and _lease_is_live(task, lease, attempt, self.now()))

    def _audit(self, call: _Call, result: str, code: str | None) -> None:
        now = self.now()
        try:
            self.db.collection(AUDIT).document(audit_id(call.attempt.attempt_id, now)).set(
                call.row(result, code, now))
        except Exception as exc:  # the call's answer still returns (§4)
            log.error("git_audit_write_failed tenant=%s task=%s attempt=%s tool=%s (%s)",
                      call.attempt.tenant_id, call.attempt.task_id, call.attempt.attempt_id,
                      call.tool, type(exc).__name__)

    # -- the slot ------------------------------------------------------------

    def _tenant(self, tenant_id: str) -> Tenant:
        tenant = self.store.get_tenant(tenant_id)
        if tenant is None:
            raise GitToolRefused("no_credential", f"tenant {tenant_id!r} is not registered")
        return tenant

    def _token(self, call: _Call, tenant: Tenant, suffix: str) -> str:
        """The slot's latest version, read now (§2.2: never cached)."""
        call.fields["slot"] = suffix
        try:
            value = self.forge_tokens.read_slot(tenant, suffix).value
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            raise GitToolRefused("no_credential", f"{secret_name_for(tenant.tenant_id, suffix)} "
                                                  f"could not be read ({code})") from None
        if not value:
            raise GitToolRefused("no_credential", "the task's forge credential is empty")
        call.token = value
        return value

    # -- the recorded tip (Q1 = A) ----------------------------------------------

    def _tip_ref(self, task_id: str, branch: str) -> Any:
        digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:16]
        return self.db.collection(TIPS).document(f"{task_id}__{digest}")

    def _attest(self, key: str, attempt: AttemptTuple, branch: str, sha: str) -> str:
        message = "\n".join((TIP_PURPOSE, attempt.tenant_id, attempt.task_id, branch, sha))
        return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    def recorded_tip(self, attempt: AttemptTuple, branch: str) -> str | None:
        """The tip swarm-api recorded for this task's `branch`, if its record holds.

        A record whose attestation does not verify under the current or the
        previous `swarm-child-key` is no record: a tenant identity can write
        a document, but not this HMAC.
        """
        snap = self._tip_ref(attempt.task_id, branch).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        sha = data.get("sha")
        if (data.get("tenant_id") != attempt.tenant_id or data.get("task_id") != attempt.task_id
                or data.get("branch") != branch or not isinstance(sha, str)
                or not _SHA.match(sha)):
            return None
        stored = str(data.get("attestation") or "")
        for key in (getattr(self.settings, "child_key", ""),
                    getattr(self.settings, "child_key_previous", "")):
            if key and hmac.compare_digest(stored, self._attest(key, attempt, branch, sha)):
                return sha
        return None

    def record_tip(self, attempt: AttemptTuple, branch: str, sha: str) -> None:
        key = getattr(self.settings, "child_key", "")
        if not key or not _SHA.match(sha):
            return
        try:
            self._tip_ref(attempt.task_id, branch).set({
                "tenant_id": attempt.tenant_id, "task_id": attempt.task_id, "branch": branch,
                "sha": sha, "attempt_id": attempt.attempt_id, "generation": attempt.generation,
                "at": self.now(), "attestation": self._attest(key, attempt, branch, sha),
            })
        except Exception as exc:
            # Without the record the next push is a fast-forward only: safe.
            log.error("git_tip_write_failed tenant=%s task=%s (%s)", attempt.tenant_id,
                      attempt.task_id, type(exc).__name__)

    # -- git_fetch and git_rebase_onto ------------------------------------------

    def _fetch(self, call: _Call, task: Mapping[str, Any], body: FetchBody) -> dict[str, Any]:
        attempt = call.attempt
        ref = task_repository(task)
        call.fields.update(repository=ref.repository, ref=body.ref)
        check_ref(body.ref)
        suffix, _ = task_slot(task)
        token = self._token(call, self._tenant(attempt.tenant_id), suffix)
        key = FETCH_OBJECT.format(tenant=attempt.tenant_id, attempt=attempt.attempt_id,
                                  call=call.request_id)

        def deliver(path: Path, size: int) -> dict[str, Any]:
            digest = _sha256_file(path)
            if size <= INLINE_BUNDLE_BYTES:
                return {"bundle": base64.b64encode(path.read_bytes()).decode("ascii"),
                        "bundle_sha256": digest}
            if self.bundles is None:
                raise GitToolRefused("too_large", f"the bundle is {size} bytes, over the "
                                                  f"{INLINE_BUNDLE_BYTES} sent inline, and this "
                                                  "deployment has no bucket for it")
            try:
                self.bundles.upload(key, path)
            except GoogleAPICallError as exc:
                # A 403 (no grant), a 412 (the object exists) or GCS failing:
                # a named refusal, audited, under 500 so the worker does not
                # ask again for a bundle that cannot be stored (#1815).
                log.warning("git_fetch bundle upload refused: %s %s", key, type(exc).__name__)
                raise GitToolRefused(FETCH_STORE_FAILED, f"the bundle is {size} bytes, over the "
                                                         f"{INLINE_BUNDLE_BYTES} sent inline, and "
                                                         "it could not be stored for the worker "
                                                         f"({type(exc).__name__})") from None
            return {"bundle_object": key, "bundle_sha256": digest}

        haves = list(dict.fromkeys(reversed(body.haves)))[:MAX_HAVES]
        fetched = self.gitexec.fetch_bundle(url=self.remote_url(ref), token=token, ref=body.ref,
                                            haves=haves, deliver=deliver)
        call.fields.update(resolved_sha=fetched.sha, bytes=fetched.size)
        # "Or the tip at the attempt's first fetch" (Q1 = A): the branch this
        # task may push, as the forge had it, when nothing is recorded yet.
        try:
            policy = push_policy(task, attempt.task_id)
        except GitToolRefused:
            policy = PushPolicy(False, "")
        if policy.pushes and body.ref == policy.branch \
                and self.recorded_tip(attempt, policy.branch) is None:
            self.record_tip(attempt, policy.branch, fetched.sha)
        answer: dict[str, Any] = {"ref": body.ref, "resolved_sha": fetched.sha}
        answer.update(fetched.delivered or {})
        return answer

    # -- git_push_branch ------------------------------------------------------

    def _push(self, call: _Call, task: Mapping[str, Any], body: PushBody) -> dict[str, Any]:
        attempt = call.attempt
        ref = task_repository(task)
        call.fields.update(repository=ref.repository, branch=body.branch,
                           old_sha=body.old_sha, new_sha=body.new_sha)
        policy = push_policy(task, attempt.task_id)
        if not policy.pushes:
            raise GitToolRefused("strategy_pushes_nothing", policy.reason)
        branch = body.branch if body.branch is not None else policy.branch
        call.fields["branch"] = branch
        suffix, writable = task_slot(task)
        call.fields["slot"] = suffix
        if not writable:
            raise GitToolRefused("forge_write_refused", f"the task's forge_access is "
                                                        f"{task.get('forge_access')!r}, not write")
        try:
            refused = grant_refusal(self.db, attempt.tenant_id, suffix, ref)
        except Exception as exc:  # unreadable is not revoked, but it is not a grant either
            raise GitToolRefused("forge_write_refused", f"the submitter's grant could not be "
                                                        f"read ({type(exc).__name__})") from None
        if refused:
            raise GitToolRefused("forge_write_refused", refused)
        token = self._token(call, self._tenant(attempt.tenant_id), suffix)
        if branch == self.writer.default_branch(ref, token):
            raise GitToolRefused("protected_branch", f"{branch!r} is the repository's default "
                                                     "branch; it is never pushed from a task")
        if branch != policy.branch:
            raise GitToolRefused("not_this_tasks_branch", f"{branch!r} is not a branch this task "
                                                          f"may push ({policy.branch})")
        try:
            rules = self.writer.branch_rules(ref, branch, token)
        except ForgeWriteNotFound:
            rules = []
        blocking = sorted({str(r.get("type")) for r in rules} & BLOCKING_RULES)
        if blocking:
            raise GitToolRefused("protected_branch", f"the forge reports rules on {branch!r} "
                                                     f"({', '.join(blocking)})")
        lease = self.recorded_tip(attempt, branch)
        with self._bundle(body, attempt, call) as bundle:
            def before_push() -> None:
                if not self._live(attempt):
                    raise _FencedMidCall()

            pushed = self.gitexec.push_bundle(url=self.remote_url(ref), token=token,
                                              bundle=bundle, branch=branch, lease=lease,
                                              before_push=before_push)
        self.record_tip(attempt, branch, pushed.new_sha)
        call.fields.update(old_sha=pushed.old_sha, new_sha=pushed.new_sha,
                           rewrite=pushed.rewrite, bytes=body.bundle_bytes)
        return {"branch": branch, "old_sha": pushed.old_sha, "new_sha": pushed.new_sha,
                "rewrite": pushed.rewrite}

    @contextmanager
    def _bundle(self, body: PushBody, attempt: AttemptTuple,
                call: _Call) -> Iterator[bytes | Path]:
        """The push's bundle, held to its declared size and sha256 before git sees it."""
        cap = self.gitexec.max_bundle_bytes
        if (body.bundle is None) == (body.bundle_object is None):
            raise GitToolRefused("bad_bundle", "send the bundle inline or as an object, once")
        if body.bundle_bytes > cap:
            raise GitToolRefused("too_large", f"a bundle is at most {cap} bytes")
        if body.bundle is not None:
            if body.bundle_bytes > INLINE_BUNDLE_BYTES:
                raise GitToolRefused("bad_bundle", f"a bundle over {INLINE_BUNDLE_BYTES} bytes "
                                                   "goes to the tenant's prefix")
            try:
                raw = base64.b64decode(body.bundle.encode("ascii"), validate=True)
            except (binascii.Error, ValueError, UnicodeEncodeError):
                raise GitToolRefused("bad_bundle", "the bundle is not base64") from None
            if len(raw) != body.bundle_bytes \
                    or hashlib.sha256(raw).hexdigest() != body.bundle_sha256:
                raise GitToolRefused("bad_bundle", "the bundle is not the size and sha256 the "
                                                   "signed body declares")
            yield raw
            return
        expected = BUNDLE_OBJECT.format(tenant=attempt.tenant_id, attempt=attempt.attempt_id,
                                        call=call.request_id)
        if body.bundle_object != expected:
            # Only this call's own object, under the tenant's own prefix: never
            # a key the request chose (invariant 9).
            raise GitToolRefused("bad_bundle", f"a large bundle is uploaded as {expected}")
        if self.bundles is None:
            raise GitToolRefused("too_large", "this deployment has no bucket for a large bundle")
        scratch = Path(tempfile.mkdtemp(prefix="gittools-"))
        try:
            dest = scratch / "in.bundle"
            try:
                size = self.bundles.download(expected, dest, max_bytes=cap)
            except OverflowError:
                raise GitToolRefused("too_large", f"a bundle is at most {cap} bytes") from None
            except Exception as exc:
                raise GitToolRefused("bad_bundle", f"the bundle object could not be read "
                                                   f"({type(exc).__name__})") from None
            if size != body.bundle_bytes or _sha256_file(dest) != body.bundle_sha256:
                raise GitToolRefused("bad_bundle", "the bundle is not the size and sha256 the "
                                                   "signed body declares")
            yield dest
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # -- read_private ---------------------------------------------------------

    def _read_private(self, call: _Call, task: Mapping[str, Any],
                      body: ReadBody) -> dict[str, Any]:
        attempt = call.attempt
        call.fields.update(repository=body.repo[:201], path=body.path, ref=body.ref)
        path = check_path(body.path)
        check_ref(body.ref)
        not_listed = GitToolRefused("repo_not_allowlisted", NOT_ALLOWLISTED_MESSAGE)
        try:
            owner, repo = parse_repository(body.repo)
        except ValueError:
            raise not_listed from None
        repo_id = repo_id_for(attempt.tenant_id, owner, repo)
        record = Repositories(self.db, now=self.now).find(attempt.tenant_id, repo_id)
        if not readable_by_tasks_of(record or {}):
            raise not_listed
        target = IssueRef(owner=owner, repo=repo, number=0)
        call.fields["repository"] = target.repository
        suffix = self._repository_slot(attempt.tenant_id, repo_id) or task_slot(task)[0]
        token = self._token(call, self._tenant(attempt.tenant_id), suffix)
        try:
            # Always resolved, a sha included: a repository that does not
            # exist, or that the credential cannot read, fails HERE, with the
            # same answer as one off the list.
            # The list route, one entry: `commits/{ref}` would carry every
            # file's patch, past what the writer holds for a large commit.
            data = self._get(target, f"commits?sha={quote(body.ref, safe='')}&per_page=1", token)
        except (ForgeWriteNotFound, ForgeWriteForbidden, ForgeWriteUnauthorized):
            raise not_listed from None
        first = data[0] if isinstance(data, list) and data else None
        sha = first.get("sha") if isinstance(first, dict) else None
        if not isinstance(sha, str) or not _SHA.match(sha):
            raise GitToolRefused("bad_ref", f"{body.ref!r} does not name a commit")
        call.fields["resolved_sha"] = sha
        try:
            where = f"contents/{quote(path, safe='/')}" if path else "contents"
            listed = self._get(target, f"{where}?ref={sha}", token)
        except ForgeWriteNotFound:
            raise GitToolRefused("no_such_path", f"{path!r} is not in {target.repository} at "
                                                 f"{sha[:12]}") from None
        answer: dict[str, Any] = {"repo": target.repository, "path": path, "ref": body.ref,
                                  "resolved_sha": sha}
        if isinstance(listed, list):
            entries = [
                {"name": str(e.get("name") or ""), "type": str(e.get("type") or ""),
                 "size": e.get("size") if isinstance(e.get("size"), int) else None}
                for e in listed[:MAX_ENTRIES] if isinstance(e, dict)
            ]
            call.fields["bytes"] = 0
            return {**answer, "kind": "dir", "entries": entries}
        if not isinstance(listed, dict) or listed.get("type") != "file":
            raise GitToolRefused("not_a_file", f"{path!r} is neither a file nor a directory")
        size = listed.get("size")
        if not isinstance(size, int) or size > MAX_FILE_BYTES or listed.get("encoding") != "base64":
            raise GitToolRefused("too_large", f"read_private returns a file of at most "
                                              f"{MAX_FILE_BYTES} bytes")
        try:
            raw = base64.b64decode(str(listed.get("content") or ""), validate=False)
            text = raw.decode("utf-8")
        except (binascii.Error, ValueError):
            raise GitToolRefused("not_text", f"{path!r} is not UTF-8 text") from None
        if len(raw) > MAX_FILE_BYTES:
            raise GitToolRefused("too_large", f"read_private returns a file of at most "
                                              f"{MAX_FILE_BYTES} bytes")
        call.fields["bytes"] = len(raw)
        return {**answer, "kind": "file", "text": text}

    def _repository_slot(self, tenant_id: str, repo_id: str) -> str | None:
        """The repository's own `git-r-` slot when the tenant registered one (§2.3)."""
        snap = self.db.collection(GIT_TOKENS).document(
            token_id_for(tenant_id, Scope.REPOSITORY, repo_id)).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        if data.get("tenant_id") != tenant_id or data.get("revoked_at") is not None \
                or repo_id not in (data.get("repo_ids") or []):
            return None
        return provider_suffix(Scope.REPOSITORY, repo_id=repo_id)

    def _get(self, ref: IssueRef, path: str, token: str) -> Any:
        """One GET on api.github.com through `GitHubWriter`'s wire: the pinned
        host, no redirect, the token in the header only, constant errors."""
        return self.writer._call("GET", self.writer._url(ref, path), token,
                                 f"{ref.repository} {path.split('?', 1)[0][:80]}",
                                 needs="contents: read")

    # -- pr_checks ------------------------------------------------------------

    def _pr_checks(self, call: _Call, task: Mapping[str, Any],
                   body: ChecksBody) -> dict[str, Any]:
        attempt = call.attempt
        ref = task_repository(task)
        call.fields.update(repository=ref.repository, pr=body.pr)
        suffix, _ = task_slot(task)
        token = self._token(call, self._tenant(attempt.tenant_id), suffix)
        if body.pr is not None:
            pull = self.writer.read_pull(ref, body.pr, token)
        else:
            try:
                branch = push_policy(task, attempt.task_id).branch or \
                    f"{BRANCH_PREFIX}{attempt.task_id}"
            except GitToolRefused:
                branch = f"{BRANCH_PREFIX}{attempt.task_id}"
            pull = self.writer.find_open_pull(ref, branch, token)
            if pull is None:
                return {"pr": None, "head_sha": None, "state": "none", "rows": []}
        call.fields["pr"] = pull.number
        if not pull.head_sha:
            raise GitToolRefused("no_head", f"GitHub reported no head sha for "
                                            f"{ref.repository}#{pull.number}", status=502)
        call.fields["resolved_sha"] = pull.head_sha
        rules = self.writer.branch_rules(ref, pull.base_ref, token) if pull.base_ref else []
        required = required_status_checks(rules)
        runs = self.writer.check_runs(ref, pull.head_sha, token)
        statuses = self.writer.commit_statuses(ref, pull.head_sha, token)
        reading = evaluate(required, runs, statuses)
        needed = {check.context for check in required}
        red_ids = {run.get("id") for run in reading.failing_runs}
        rows: list[dict[str, Any]] = []
        for run in runs:
            name = str(run.get("name") or "check")
            row = {"name": name, "status": run.get("status"), "conclusion": run.get("conclusion"),
                   "required": name in needed}
            run_id = run.get("id")
            app = run.get("app") if isinstance(run.get("app"), Mapping) else {}
            if run_id in red_ids and isinstance(run_id, int) and not isinstance(run_id, bool) \
                    and app.get("slug") == "github-actions":
                # An Actions check run's id is its job's id (`issueci`).
                tail = self.writer.job_log_tail(ref, run_id, token, limit=LOG_TAIL_BYTES)
                if tail and tail.strip():
                    row["log_tail"] = call.scrub(tail)
            rows.append(row)
        for status in statuses:
            name = str(status.get("context") or "status")
            state = status.get("state")
            rows.append({"name": name, "status": "pending" if state == "pending" else "completed",
                         "conclusion": None if state == "pending" else state,
                         "required": name in needed})
        return {"pr": pull.number, "head_sha": pull.head_sha, "state": reading.state,
                "rows": rows}

    # -- the audit read (§4) ----------------------------------------------------

    def calls_of(self, tenant_id: str, task_id: str, *,
                 submitted_by: str | None, limit: int = 500) -> list[dict[str, Any]]:
        """A task's git tool calls, newest first; 404 for a task not the caller's."""
        self.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        query = (self.db.collection(AUDIT)
                 .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                 .where(filter=FieldFilter("task_id", "==", task_id))
                 .limit(limit))
        rows = [(snap.id, snap.to_dict() or {}) for snap in query.stream()]
        rows = [(i, r) for i, r in rows if r.get("tenant_id") == tenant_id
                and r.get("task_id") == task_id]
        rows.sort(key=lambda item: item[0])
        return [_served(row) for _, row in rows]


def _served(row: Mapping[str, Any]) -> dict[str, Any]:
    at = row.get("at")
    return {**{k: v for k, v in row.items() if k != "at"},
            "at": at.isoformat() if isinstance(at, datetime) else at}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _forge_refusal(exc: ForgeWriteError, call: _Call) -> GitToolRefused:
    """A GitHub answer as a tool answer. The writer's sentences are constant
    and carry no token; they are scrubbed again all the same."""
    message = call.scrub(exc.message)
    if isinstance(exc, ForgeUnreachable):
        return GitToolRefused("forge_unavailable", message, status=503)
    if isinstance(exc, ForgeWriteNotFound):
        return GitToolRefused("not_found", message)
    if isinstance(exc, (ForgeWriteForbidden, ForgeWriteUnauthorized)):
        return GitToolRefused("forge_refused", message)
    return GitToolRefused("forge_failed", message, status=502)
