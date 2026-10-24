"""Child tasks, the worker's half (docs/design/child-tasks.md §3.1-§3.3, §6.4).

The agent never holds a credential. It writes a request into a SPOOL in its
workspace; this worker -- the non-dumpable process beside it, which holds the
attempt key -- validates it and relays it to swarm-api, signed.

    $SWARM_CHILDREN = work/.swarm-children/
      requests/<request_id>.json   the agent's, one child each (write .tmp, rename)
      responses/<request_id>.json  this worker's: {"task_id": ...} or {"refused": {...}}
      await                        the agent's: presence asks to await, then exit 0
      results/children.json        this worker's, on resume: every child's end
      results/<child_task_id>/...  this worker's, on resume: a SUCCEEDED child's artifacts

WHY A SPOOL AND NOT A LOCALHOST ENDPOINT (§2): no listening socket, nothing
from the runner image, and `work/` is checkpointed, so a resumed attempt knows
which requests were already answered.

THE ATTEMPT KEY (§3.2). An Ed25519 keypair generated in this process's heap
once `make_non_dumpable` reports PROTECTED, its public half registered with
swarm-api while the task is STARTING -- before the agent exists -- by spending
the one-use nonce the scheduler passed in SWARM_CHILD_NONCE. The private key is
never written anywhere: not the environment, a file, `work/`, a log line or an
event. A worker that cannot protect its heap registers a TOMBSTONE in the slot
instead, so the nonce the agent can read from /proc/1/environ is spent, and
offers no child path (§5 F12).

THE SAME KEY SIGNS THE RUNNER GIT TOOLS (docs/runner-git-tools.md §1.3,
§1.4; lane G6). Their relay (`gittools.GitTools`) lives inside this object,
the one holder of the key, rather than beside it: `prepare` composes its spool
(`$SWARM_GIT`) and `has_requests`/`tick` answer it, so the lifecycle's existing
calls carry it and its fenced exit is the child path's. HAVING A KEY AND BEING
OFFERED THE CHILD PATH ARE SEPARATE: a child task is keyed for its git tools
(the scheduler issues it a nonce, lane G2) but is never given `SWARM_CHILDREN`,
because a child may not submit children.

WHAT THE AGENT GETS BACK. A child's id when it is created, and -- only after
`await` parks the parent and the scheduler promotes it -- every child's end in
`results/children.json`. There is no channel that reports a running child's
progress (§1), so an agent that tries to busy-wait has nothing to wait on and
waiting always gives the slot back (invariants 1 and 4).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlencode

from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TERMINAL_STATES, TaskState

from .accountlease import BrokerUnavailable, fetch_identity_token
from .continuation import TASK_ID_RE, publish_branch
from .errors import InputUnavailable, WorkerError
from .gitidentity import commit_identity
from .hardening import PROTECTED
from . import inputs as inputs_mod

#: The spool's directory under `work/`. `Workspace.children_dir` is the one
#: path; this is its name.
SPOOL_DIR_NAME = ".swarm-children"
#: The variable that tells an agent it has a child path, and where.
CHILDREN_ENV = "SWARM_CHILDREN"

#: `swarm_api.children.REQUEST_ID`, restated (this image does not carry
#: swarm-api); tests/unit/worker/test_child_tasks_worker.py holds them equal.
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
#: What a request may say about its child (§6.1). Anything else is refused here
#: and by the route's strict model.
CHILD_FIELDS = frozenset(
    {"kind", "runner_profile", "resource_class", "input", "provider", "model",
     "timeout_seconds", "repository_ref", "model_class"}
)
#: The child kinds, `swarm_api.children.KIND_*` restated (docs/design/agent-
#: autonomy.md §3.6): an agent (no `kind` is an agent), an issue run of this
#: task's repository, or a saved, approved workflow spec by name.
CHILD_KINDS = ("agent", "issue_run", "workflow")
#: What a run-kind request may say: `swarm_api.children.ChildIssueRun` and
#: `ChildWorkflow`'s fields. A workflow is a NAME, never a spec body.
ISSUE_RUN_FIELDS = frozenset({"kind", "issue", "after_merged"})
WORKFLOW_FIELDS = frozenset({"kind", "spec_name"})
#: `swarm_api.childkey.REQUEST_PURPOSE` and the proof headers, restated.
REQUEST_PURPOSE = "swarm-child-req/v1"
PROOF_HEADER = "X-Swarm-Attempt-Proof"
TIMESTAMP_HEADER = "X-Swarm-Attempt-Timestamp"
#: The tombstone's refusal (§5 F12).
WORKER_UNPROTECTED = "worker_unprotected"
#: Why a registered attempt offers no child path: the scheduler said
#: SWARM_CHILD_PATH=false, which it does for a child on a person's slot (#1041).
CHILD_PATH_OFF = "child_path_off"
#: The guide the agent reads, written into the spool as README.md by `prepare`
#: and named by the one prompt line the CLI runners add (`prompt_line`). The
#: prompt is the one instruction channel every CLI runner shares, so it says
#: only where the guide is; the guide says everything else. docs/child-tasks-
#: for-agents.md carries the same text for people, and
#: tests/unit/worker/test_child_tasks_worker.py holds the two equal.
GUIDE_NAME = "README.md"
AGENT_GUIDE = """\
# Child tasks: how this agent submits helpers

This task may submit CHILD TASKS: other agents that run in their own
containers, on this task's tenant, while you work. `$SWARM_CHILDREN` is this
directory. There is no network call to make and no credential to use: you
write files here, and the platform's worker beside you submits them.

## Submit a child

Write ONE JSON object per child to `requests/<request_id>.json`, atomically:
write `requests/<request_id>.tmp`, then rename it to `.json`. `<request_id>`
is yours to choose, `[a-z0-9-]{1,64}`, and is the child's idempotency key:
writing the same id again never makes a second child.

    {
      "request_id": "split-tests-2",
      "runner_profile": "claude-code",
      "input": {"prompt": "Write unit tests for src/parser.py"},
      "resource_class": "standard",
      "timeout_seconds": 1800,
      "repository_ref": "main"
    }

`runner_profile` is required and names a profile, never an image or a
command. `input` is the child's input, `{"prompt": ...}` for the CLI agents.
`resource_class`, `provider`, `model`, `timeout_seconds`, `repository_ref`
and `model_class` are optional; nothing else is accepted. `model_class` names
the model a claude-code child runs, `opus`, `fable`, `sonnet` or `haiku`;
without it the child runs its own profile's default, never this task's. A
child works on this task's repository, runs no longer than this task, and is
at most 256 KiB as a file.

## Read the answer

Within about ten seconds the worker writes `responses/<request_id>.json` and
removes the request:

    {"task_id": "task_...", "request_id": "split-tests-2"}

or a refusal:

    {"refused": {"code": "child_fan_out_exceeded", "message": "...", "retryable": false}}

`retryable: true` (`api_unavailable`) means write the same request again with
the same `request_id`. Anything else will be refused again as written. Limits:
16 children per task across all its attempts; a child cannot submit children
of its own; about four requests are answered per ten seconds.

## Start an issue run or a saved workflow

A request may instead name a `kind` (an agent child is `"kind": "agent"`, or
no `kind` at all):

    {"request_id": "lane-b", "kind": "issue_run", "issue": 7,
     "after_merged": "crun_..."}

    {"request_id": "nightly", "kind": "workflow", "spec_name": "nightly-docs"}

An `issue_run` plans, builds, reviews and merges an issue of THIS task's
repository under the run's own gates; `issue` is its number (or
`owner/repo#N` of this repository; any other repository is refused).
`after_merged`, optional, names another issue run this task started, by the
`child_id` it was answered with: the new run is HELD, starting nothing, until
that one is DONE with its pull request merged, and is cancelled if it ends
any other way. A `workflow` runs a saved spec of this tenant BY NAME,
`spec_name`, one a platform admin approved; a spec body (`steps`, `spec`) is
refused. The answer names the run by `child_id`:

    {"child_id": "crun_...", "request_id": "lane-b", "kind": "issue_run",
     "state": "HELD", "run_id": null, "workflow_id": null}

Both count toward the 16, and a child cannot start either. Waiting for them
is the same `await` as below; in `results/children.json` each is listed by
`child_id` with its run's `state`, whether it `ended`, and its pull request.

## Wait for the children

There is no way to watch a running child. To wait, first write everything you
will need to continue into your working files (your process ends; the
platform checkpoints the work directory and restores it), then create the
empty file `await` here and EXIT 0. The task gives its slot back and sleeps
until every child has ended. Then you are started again with the same input,
and `results/children.json` exists -- that is how you know you are resuming:

    {"listing": "complete",
     "children": [{"task_id": "task_...", "request_id": "split-tests-2",
                   "state": "SUCCEEDED", "end_cause": null,
                   "parent_attempt_id": "att_...", "outputs": "staged",
                   "files": ["task_.../notes.md"]}]}

Each SUCCEEDED child's artifacts are under `results/<task_id>/`; `files` lists
them relative to `results/`. `outputs` is `staged`, `none`, or `unavailable`
with a `reason` (an expired or over-cap artifact: still information, not a
failure). A child that failed or was cancelled is listed with its `state` and
`end_cause`; deciding what to do about it is yours. `listing: unavailable`
means the platform could not list them this time.

If you exit 0 while children are still running, you are made to wait anyway:
a task never succeeds over running children. If you exit non-zero, this
attempt fails as usual and the children keep running for the next attempt.
"""


#: The runners the `swarm-git` shim is named in: claude-code's `--mcp-config`,
#: codex's MCP configuration (docs/runner-git-tools.md §7, lane G6).
#: `scheduler.dispatch.GIT_TOOL_RUNNERS`, restated (this image does not carry
#: the scheduler); tests/unit/worker/test_git_tools_wiring.py holds them equal,
#: so the scheduler issues a child its nonce exactly when this worker offers it
#: the tools the nonce is for.
GIT_TOOL_RUNNERS = ("agent_worker.runners.claude_code", "agent_worker.runners.codex")
#: The shim's module, as the runners name it.
GIT_SHIM_MODULE = "agent_worker.gitmcp"
#: Git requests answered per control poll. ONE: a push mirrors, scans and
#: bundles before its signed call, and the tick runs under the run loop's
#: `_heartbeat_meanwhile`, so one call per poll keeps every tick short. The
#: shim waits minutes for its answer (`gitmcp.server.WAIT_SECONDS`), and the
#: agent's own budget is 60 calls an hour (`gittools.CALLS_PER_HOUR`), far
#: below one per ten-second poll.
GIT_REQUESTS_PER_TICK = 1


def offers_git_tools(profile_name: str) -> bool:
    """Whether a task on this profile is offered the runner git tools.

    `scheduler.dispatch.offers_git_tools`, restated: read from the frozen
    catalogue, so a new profile on one of these runners is covered. A worker
    action runs no agent; the mock, generic and browser runners have no MCP
    configuration to name the shim in.
    """
    profile = RUNNER_PROFILES.get(profile_name)
    if profile is None or profile.worker_action is not None:
        return False
    return any(part in GIT_TOOL_RUNNERS for part in profile.runner_argv)


def git_shim_argv(spool: str) -> list[str]:
    """The command a CLI runs as the `swarm-git` MCP server.

    This runner's own interpreter, so the shim is the installed package's.
    `-I` keeps the agent's working directory -- the checkout, where the CLI
    starts the server -- off `sys.path`, so a module the agent wrote there is
    never what runs as the shim. The spool is named, not inherited: the CLI
    builds its servers' environment itself.
    """
    return [sys.executable, "-I", "-m", GIT_SHIM_MODULE, "--spool", spool]


#: The search tool's runner (docs/search-tool.md, lane SEARCH-TOOL): claude-code
#: only, the runner whose `--mcp-config` the live browser's tools are in.
SEARCH_TOOL_RUNNERS = ("agent_worker.runners.claude_code",)
#: The search shim's module, as the runner names it.
SEARCH_SHIM_MODULE = "agent_worker.searchmcp"
#: Search requests answered per control poll. One call is one HTTP round trip
#: to swarm-api (and its provider's, bounded at 10 s there), so a tick stays short.
SEARCH_REQUESTS_PER_TICK = 2


def offers_search_tool(profile_name: str) -> bool:
    """Whether a task on this profile may be offered the search tool.

    A live-browser profile (`RunnerProfile.live_browser`, read from the frozen
    catalogue) whose runner is claude-code: the tool is for an agent that opens
    its results in the live browser. swarm-api checks the same property of the
    task's profile on every call (`swarm_api.search.offers_search`); whether
    the deployment offers it at all is swarm-api's flag, answered at key
    registration (`ChildPath.search_flag`).
    """
    profile = RUNNER_PROFILES.get(profile_name)
    if profile is None or profile.worker_action is not None or not profile.live_browser:
        return False
    return any(part in SEARCH_TOOL_RUNNERS for part in profile.runner_argv)


def search_shim_argv(spool: str) -> list[str]:
    """The command a CLI runs as the `swarm-search` MCP server: `git_shim_argv`'s
    shape, for the same reasons (this interpreter, `-I`, the spool named)."""
    return [sys.executable, "-I", "-m", SEARCH_SHIM_MODULE, "--spool", spool]


def _word(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def git_push_policy(
    task: Any, *, task_id: str, branch_prefix: str
) -> tuple[bool, tuple[str, ...]]:
    """(whether this task's strategy pushes, the branches it may push).

    From the task's SIGNED dispatch block, the way the end-of-task publish
    reads it (`Worker._publish_git`): an unknown strategy is `collect` and an
    unknown `single-pr` role is `reader`, the readings that push nothing; an
    amender pushes its author's branch, a `direct-pr` continuation the branch
    it continues, everything else `<prefix><own task id>`. A name that is not
    a task id gives NO branch, never a guessed one, so the worker refuses the
    push locally as `not_this_tasks_branch`. swarm-api derives the same set
    (`swarm_api.gittools.push_policy`) and its answer is the one that counts;
    tests/unit/worker/test_git_tools_wiring.py holds the two equal.
    """
    metadata = task.get("metadata") if isinstance(task, dict) else None
    block = metadata.get("dispatch") if isinstance(metadata, dict) else None
    block = block if isinstance(block, dict) else {}
    if _word(block.get("strategy")) == "single-pr":
        role = _word(block.get("pr_role"))
        role = role if role in ("author", "reader", "amender", "none") else "reader"
        if role in ("reader", "none"):
            return False, ()
        if role == "amender":
            author = block.get("pr_author")
            if isinstance(author, str) and TASK_ID_RE.match(author.strip()):
                return True, (f"{branch_prefix}{author.strip()}",)
            return True, ()
        return True, (f"{branch_prefix}{task_id}",)
    strategy = _word(block.get("strategy"))
    strategy = strategy if strategy in ("collect", "direct-pr", "integrate") else "collect"
    if strategy == "collect" and _word(block.get("carrier")) != "branches":
        return False, ()
    try:
        return True, (publish_branch(metadata, branch_prefix, task_id),)
    except WorkerError:
        return True, ()


def _repository_name(url: str) -> str:
    """`owner/name` from a repository URL, for the relay's record only."""
    path = url.rstrip("/").split("://", 1)[-1].split(":", 1)[-1]
    parts = [p for p in path.split("/") if p]
    name = "/".join(parts[-2:]) if len(parts) >= 2 else path
    return name[:-4] if name.endswith(".git") else name


def prompt_line(spool: str) -> str:
    """The one line a CLI runner adds to the prompt when the agent has a spool."""
    return (
        f"You can submit child tasks (helper agents) and wait for their results "
        f"through files in {spool}; read {spool}/{GUIDE_NAME} first. Nothing there "
        "is required."
    )


#: `ChildPath.tick`'s default `limit`: `max_child_requests_per_tick`. A
#: sentinel, because None is the drain's "every request in the spool".
PER_TICK: Any = object()

_TIMEOUT = 10
_UA = "swarm-agent-worker/children"


def request_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one request (§3.2 step 5)."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{REQUEST_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class AttemptKey:
    """The attempt key, in this process's heap only. Never serialised."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private = Ed25519PrivateKey.generate()
        self.public = _b64url(
            self._private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    def headers(self, method: str, path: str, body: bytes, *, now: float) -> dict[str, str]:
        timestamp = str(int(now))
        return {
            PROOF_HEADER: self.sign(request_message(method, path, body, timestamp)),
            TIMESTAMP_HEADER: timestamp,
        }

    def sign(self, message: bytes) -> str:
        """`message`'s Ed25519 signature, base64url. The caller builds the
        message under its own purpose: `request_message` here, and
        `statecall.state_request_message` for the state routes, whose prefix
        keeps either proof from being replayed as the other."""
        return _b64url(self._private.sign(message))

    def __repr__(self) -> str:  # never the key, whatever logs this object
        return "AttemptKey(<heap only>)"

    def __reduce__(self) -> Any:
        raise TypeError("an attempt key is never serialised")


# --------------------------------------------------------------------------
# The client for the worker-only routes
# --------------------------------------------------------------------------


class ChildApiError(Exception):
    """A route's refusal, or no answer. `retryable` is the caller's to act on."""

    def __init__(self, status: int | None, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class ChildSubmitFenced(ChildApiError):
    """409 child_submit_fenced: this attempt is superseded (invariant 5)."""


class ChildApi:
    """swarm-api's three worker-only child routes, over urllib.

    The ID token is minted for the API's audience from the metadata server at
    every call and never logged; an error never carries a header or a body
    this worker sent.
    """

    def __init__(
        self,
        base_url: str,
        *,
        audience: str | None = None,
        token_fetcher: Callable[[str], str] | None = None,
        timeout: int = _TIMEOUT,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._audience = (audience or self._base).rstrip("/")
        self._fetch_token = token_fetcher or fetch_identity_token
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen

    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            token = self._fetch_token(self._audience)
        except BrokerUnavailable as exc:
            raise ChildApiError(None, "api_unavailable", "no workload identity token", retryable=True) from exc
        req = urllib.request.Request(f"{self._base}{path}", data=body, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _UA)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with self._open(req, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read() if hasattr(exc, "read") else b""
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ChildApiError(
                None, "api_unavailable", type(exc).__name__, retryable=True
            ) from None
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if status >= 400:
            code = str(payload.get("code") or f"http_{status}")
            message = str(payload.get("message") or "")[:300]
            if code == "child_submit_fenced":
                raise ChildSubmitFenced(status, code, message, retryable=False)
            raise ChildApiError(status, code, message, retryable=status >= 500 or status == 429)
        return status, payload


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"refused": {"code": self.code, "message": self.message, "retryable": self.retryable}}


class ChildPath:
    """One attempt's attempt key, and what it signs: the child path
    (registration, the spool, the await, the staging) and the runner git tools'
    relay."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        memory: Any,
        api: ChildApi | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._memory = memory
        self._api = api
        if self._api is None and cfg.swarm_api_url:
            self._api = ChildApi(cfg.swarm_api_url, audience=cfg.swarm_api_audience)
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._key: AttemptKey | None = None
        #: Why this attempt has no child path, once decided; None while it has one.
        self.unavailable: str | None = "not_registered"
        #: WHY there is no child path, as swarm-api's code or the worker's own
        #: sentence (`_note`'s `why`): what the live path's "live browser
        #: unavailable" event names, since no key means no live session.
        self.unavailable_reason: str | None = None
        #: The task is itself a child (`parent_task_id`), learnt from the
        #: verified task in `prepare`: keyed for its git tools, never offered
        #: the child path (§1.4 of docs/runner-git-tools.md).
        self.is_child = False
        #: The git tools' relay (`gittools.GitTools`), built once by `prepare`
        #: when the attempt is offered them; None otherwise.
        self._git: Any = None
        #: Why this attempt has no git tools, once decided; None while it has them.
        self.git_unavailable: str | None = "not_prepared"
        #: Whether swarm-api answered the key registration with `search_tool:
        #: true` -- SEARCH_TOOL_ENABLED is on there (docs/search-tool.md).
        self.search_flag = False
        #: The search tool's relay (`searchtools.SearchTools`), built once by
        #: `prepare` when the attempt is offered it; None otherwise.
        self._search: Any = None
        #: Why this attempt has no search tool, once decided; None while it has it.
        self.search_unavailable: str | None = "not_prepared"
        #: Whether the attempt key was registered by `state/open` for the
        #: `state` purpose (`adopt`): the state reads sign under
        #: `swarm-state-req/v1` then, and under `swarm-child-req/v1` otherwise.
        self.state_signed = False
        #: The state reads' relay (`statemcp.relay.StateReads`), built once by
        #: `prepare` when the attempt is offered them; None otherwise.
        self._state_reads: Any = None
        #: Why this attempt has no state reads, once decided; None while it has them.
        self.state_reads_unavailable: str | None = "not_prepared"

    @property
    def keyed(self) -> bool:
        """Whether this attempt registered an attempt key: what both paths sign with."""
        return self._key is not None and self.unavailable is None

    @property
    def offered(self) -> bool:
        """Whether this attempt has a child path: the variable and the spool exist."""
        return self.keyed and not self.is_child

    @property
    def git_offered(self) -> bool:
        """Whether this attempt has the runner git tools: `$SWARM_GIT` and its relay."""
        return self._git is not None

    @property
    def rebased_onto(self) -> str | None:
        """The sha the latest `git_rebase_onto` rebased the clone onto, or None
        (`gittools.GitTools.rebased_onto`; read by the publish, SPEED-D1)."""
        onto = getattr(self._git, "rebased_onto", None) if self._git is not None else None
        return onto if isinstance(onto, str) and onto else None

    def pushed_tip(self, branch: str) -> str | None:
        """The sha swarm-api answered this attempt's last `git_push_branch` to
        `branch` with, or None: no relay, no push to that branch, or no sha
        (`gittools.GitTools.pushed_tip`; read by the publish, #1188)."""
        read = getattr(self._git, "pushed_tip", None) if self._git is not None else None
        tip = read(branch) if callable(read) else None
        return tip if isinstance(tip, str) and tip else None

    @property
    def search_offered(self) -> bool:
        """Whether this attempt has the search tool: `$SWARM_SEARCH` and its relay."""
        return self._search is not None

    @property
    def configured(self) -> bool:
        """Whether this deployment gave the attempt a nonce and an API to spend it at."""
        return bool(self.cfg.child_nonce) and self._api is not None

    # -- §3.2 step 3: register before the agent exists --------------------

    def register(self, note: Callable[[dict[str, Any]], Any]) -> None:
        """Spend the nonce: a key when the heap is protected, a tombstone when not.

        Called while the task is STARTING. Never raises: a registration that
        cannot be made leaves the attempt without a child path, the residual
        §3.2 states, recorded as `child_key_unregistered` so it is visible.
        """
        if not self.cfg.child_nonce:
            self.unavailable = "no_nonce"
            return
        if self._api is None:
            self.unavailable = "child_key_unregistered"
            self._note(note, "child_key_unregistered", "no SWARM_API_URL for this attempt")
            return
        protected = getattr(self._memory, "status", None) == PROTECTED
        key = AttemptKey() if protected else None
        body: dict[str, Any] = {
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "nonce": self.cfg.child_nonce,
        }
        if key is not None:
            body["public_key"] = key.public
        else:
            body.update({"key": None, "refused": WORKER_UNPROTECTED})
        path = f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/child-key"
        try:
            answer = self._with_retry(
                lambda: self._api.call("POST", path, body=json.dumps(body).encode("utf-8"))
            )
        except ChildApiError as exc:
            self.unavailable = "child_key_unregistered"
            self._note(note, "child_key_unregistered", exc.code)
            return
        if key is None:
            self.unavailable = WORKER_UNPROTECTED
            self._note(note, "child_key_tombstone", WORKER_UNPROTECTED)
            return
        self._key = key
        if not getattr(self.cfg, "child_path", True):
            # Registered, so the forge broker can tell this attempt from any
            # other (docs/design/user-scoped-secrets.md §3.2), but no spool and
            # no variable: the scheduler marked this attempt a child.
            self.unavailable = CHILD_PATH_OFF
            self.log.info("attempt key registered; no child path for a child task")
            return
        self.unavailable = None
        # The search flag is swarm-api's, answered here so that a deployment
        # with it off names no search tool to any agent (docs/search-tool.md).
        payload = answer[1] if isinstance(answer, tuple) and len(answer) == 2 else None
        self.search_flag = isinstance(payload, dict) and payload.get("search_tool") is True
        self.log.info("child path registered for this attempt")

    # -- the key's one other use: the forge broker's proof (#1041) ---------

    @property
    def api(self) -> ChildApi | None:
        """The worker-only routes' client, which the forge broker reuses."""
        return self._api

    @property
    def can_sign(self) -> bool:
        """Whether this attempt registered a key, whatever its child path."""
        return self._key is not None

    def sign_request(self, method: str, path: str, body: bytes) -> dict[str, str]:
        """The proof headers for one request to a worker-only route.

        The signature is made here and only the headers leave: the key stays
        in this process's heap (`AttemptKey`), never serialised. Raises
        `LookupError` naming why when no key was registered -- no nonce, a
        refused registration, or a tombstone -- so a caller that needs a
        proof fails closed instead of asking unproven.
        """
        if self._key is None:
            raise LookupError(self.unavailable or "not_registered")
        return self._key.headers(method, path, body, now=self._wall())

    def retrying(self, fn: Callable[[], Any]) -> Any:
        """`fn` under the registration's retry bound (`child_submit_retry_seconds`)."""
        return self._with_retry(fn)
    def adopt(
        self,
        key: AttemptKey | None,
        registration: Any,
        note: Callable[[dict[str, Any]], Any],
    ) -> None:
        """Use the key `state/open` registered, instead of registering one here.

        With the tenant switched to the state routes (`SWARM_STATE_VIA_API`,
        docs/design/tenant-settings-integrity.md §4.3), the control plane's
        `state/open` is the worker's FIRST call: it spends the nonce on ONE
        key per attempt, which signs both kinds of request. `register` would
        find that slot taken. So the child path takes the same key, and is
        offered exactly as a registration here would have offered it:

          * a key swarm-api registered for the `child` purpose: the child
            path, the git tools and every relay signed with it;
          * a key from a heap `make_non_dumpable` could not protect, which
            swarm-api registers for `state` only (owner decision Q4): no child
            path, noted as the tombstone is today (`worker_unprotected`);
          * no key at all: no child path, noted `child_key_unregistered`.

        Never raises, as `register` never does. The search tool's flag rides on
        `state/open`'s registration as on `child-key`'s answer (`search_tool`,
        lane ISO-SEARCH), and is read exactly as `register` reads it.
        """
        payload = registration if isinstance(registration, dict) else {}
        purposes = payload.get("purpose")
        purposes = purposes if isinstance(purposes, list) else []
        if key is None:
            self.unavailable = "child_key_unregistered"
            self._note(note, "child_key_unregistered", "state/open registered no key")
            return
        if payload.get("unprotected") is True or "child" not in purposes:
            self.unavailable = WORKER_UNPROTECTED
            self._note(note, "child_key_tombstone", WORKER_UNPROTECTED)
            return
        self._key = key
        self.unavailable = None
        self.state_signed = "state" in purposes
        self.search_flag = payload.get("search_tool") is True
        self.log.info("child path registered for this attempt", registered_by="state/open")

    def _note(self, note: Callable[[dict[str, Any]], Any], what: str, why: str) -> None:
        self.unavailable_reason = why
        self.log.warning("no child path for this attempt", child_path=what, reason=why)
        try:
            note({"phase": "child_key", "child_path": what, "reason": why})
        except Exception:  # a note must never stop the attempt
            pass

    def _retry_deadline(self) -> float:
        return self._clock() + max(0, int(self.cfg.child_submit_retry_seconds))

    def _with_retry(self, fn: Callable[[], Any], *, deadline: float | None = None) -> Any:
        """Retry a retryable failure within `child_submit_retry_seconds`, never longer.

        `deadline` is the caller's remaining budget (a tick's, or a drain's):
        retries never outlast it either, so the bound a tick or a drain
        promises holds across every request it answers, not per request.
        """
        own = self._retry_deadline()
        deadline = own if deadline is None else min(deadline, own)
        delay = 1.0
        while True:
            try:
                return fn()
            except ChildApiError as exc:
                if not exc.retryable:
                    raise
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise
                self._sleep(min(delay, remaining))
                delay = min(delay * 2, 8.0)

    # -- the spool ---------------------------------------------------------

    @staticmethod
    def spool(work: Path) -> Path:
        """`Workspace.children_dir`, from the work directory alone."""
        return work / SPOOL_DIR_NAME

    def prepare(
        self,
        work: Path,
        *,
        task: dict[str, Any] | None = None,
        checkout: Path | None = None,
        repository_url: str | None = None,
        clone_base: str | None = None,
        private_dir: Path | None = None,
        logs_dir: Path | None = None,
        store: Any = None,
    ) -> dict[str, str]:
        """Create the spools this attempt is offered; return the agent's variables.

        `SWARM_CHILDREN` when the attempt has the child path, `SWARM_GIT` when
        it has the git tools (`_prepare_git`), both, or neither. `task` is the
        VERIFIED task document, read for `parent_task_id` and the signed
        dispatch block only; everything else is the worker's own.
        """
        if isinstance(task, dict) and task.get("parent_task_id"):
            self.is_child = True
        env = self._prepare_children(work)
        env.update(self._prepare_git(
            work, task=task, checkout=checkout, repository_url=repository_url,
            clone_base=clone_base, private_dir=private_dir, logs_dir=logs_dir, store=store,
        ))
        env.update(self._prepare_search(work))
        env.update(self._prepare_state_reads(work))
        return env

    def _prepare_search(self, work: Path) -> dict[str, str]:
        """The search tool's spool, when this attempt is offered it.

        Offered when the attempt is keyed (every call is signed with the key),
        swarm-api's flag is on (`search_flag`) and the profile is a live-browser
        claude-code one (`offers_search_tool`). Built once, so the relay's rate
        window survives the credential reload's rebuild of the environment.
        """
        if self._search is not None:
            return self._search.prepare(work)
        if not self.keyed or self._api is None:
            why: str | None = "no_attempt_key"
        elif not self.search_flag:
            why = "search_tool_disabled"
        elif not offers_search_tool(self.cfg.runner_profile):
            why = "profile_has_no_search_tool"
        else:
            why = None
        if why is not None:
            self.search_unavailable = why
            return {}
        assert self._key is not None and self._api is not None
        from . import searchtools  # lazy: searchtools imports this module

        self._search = searchtools.SearchTools(
            searchtools.SearchToolsConfig(
                tenant_id=self.cfg.tenant_id,
                task_id=self.cfg.task_id,
                attempt_id=self.cfg.attempt_id,
                lease_id=self.cfg.lease_id,
                generation=int(self.cfg.generation),
            ),
            key=self._key,
            api=self._api,
            logger=self.log,
            clock=self._clock,
            wall=self._wall,
        )
        self.search_unavailable = None
        self.log.info("search tool offered for this attempt")
        return self._search.prepare(work)

    def _prepare_state_reads(self, work: Path) -> dict[str, str]:
        """The state reads' spool (`$SWARM_STATE`), when this attempt holds a key.

        Every keyed attempt is offered them (docs/design/agent-autonomy.md
        §3.4): a key `state/open` registered for the state purpose signs under
        `swarm-state-req/v1`, any other registered key -- the child purpose,
        `/child-key`'s -- under `swarm-child-req/v1`, which swarm-api accepts
        on the read route (BGD, 2026-10-22 08:54Z). `no_attempt_key` only when
        the worker holds no key: no nonce, a refused registration, a
        tombstone or an unprotected heap's state-only key, which `adopt`
        never takes. Built once, so the relay's hourly budget survives the
        credential reload's rebuild of the environment.
        """
        if self._state_reads is not None:
            return self._state_reads.prepare(work)
        if self._key is None or self._api is None:
            self.state_reads_unavailable = "no_attempt_key"
            return {}
        from .statemcp import relay as state_relay  # lazy: it imports this module

        self._state_reads = state_relay.StateReads(
            state_relay.StateReadsConfig(
                tenant_id=self.cfg.tenant_id,
                task_id=self.cfg.task_id,
                attempt_id=self.cfg.attempt_id,
                lease_id=self.cfg.lease_id,
                generation=int(self.cfg.generation),
                purpose=state_relay.PURPOSE_STATE if self.state_signed
                else state_relay.PURPOSE_CHILD,
            ),
            sign=self._key.sign,
            api=self._api,
            logger=self.log,
            clock=self._clock,
            wall=self._wall,
        )
        self.state_reads_unavailable = None
        self.log.info("state reads offered for this attempt",
                      purpose=self._state_reads.cfg.purpose)
        return self._state_reads.prepare(work)

    def _prepare_children(self, work: Path) -> dict[str, str]:
        if not self.offered:
            return {}
        root = self.spool(work)
        for name in ("requests", "responses", "results"):
            (root / name).mkdir(parents=True, exist_ok=True)
        # Rewritten every attempt: the platform's text, never one a restored
        # checkpoint carried (a link the agent made is replaced, not followed).
        guide = root / GUIDE_NAME
        tmp = root / f".{GUIDE_NAME}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(AGENT_GUIDE, encoding="utf-8")
        os.replace(tmp, guide)
        return {CHILDREN_ENV: str(root)}

    def _prepare_git(
        self,
        work: Path,
        *,
        task: dict[str, Any] | None,
        checkout: Path | None,
        repository_url: str | None,
        clone_base: str | None,
        private_dir: Path | None,
        logs_dir: Path | None,
        store: Any,
    ) -> dict[str, str]:
        """The git tools' spool, when this attempt is offered them (lane G6).

        Offered when the attempt is keyed, its profile's runner names the shim
        (`offers_git_tools`), and it has a checkout whose clone base the worker
        recorded -- the one sha it trusts origin to have before any call
        (`gittools` module docstring). Built ONCE: the credential reload
        rebuilds the runner's environment mid-run, and the relay's rate window
        and what it learnt origin has must survive that.
        """
        if self._git is not None:
            # The values registered since (a reloaded credential) are scanned
            # for too: the relay's leak gate reads the registered set.
            self._git._secrets = self._registered_secrets()
            return self._git.prepare(work)
        why = self._git_refusal(task, checkout, repository_url, clone_base, private_dir,
                                logs_dir)
        if why is not None:
            self.git_unavailable = why
            return {}
        assert self._key is not None and self._api is not None and task is not None
        from . import gittools  # lazy: gittools imports this module

        pushes, pushable = git_push_policy(
            task, task_id=self.cfg.task_id, branch_prefix=self.cfg.git_branch_prefix
        )
        bot = (self.cfg.git_author_name, self.cfg.git_author_email)
        person = commit_identity(task)
        config = gittools.GitToolsConfig(
            tenant_id=self.cfg.tenant_id,
            task_id=self.cfg.task_id,
            attempt_id=self.cfg.attempt_id,
            lease_id=self.cfg.lease_id,
            generation=int(self.cfg.generation),
            repository=_repository_name(str(repository_url)),
            clone_base=str(clone_base),
            pushable=pushable,
            # EXACTLY the identity the agent commits as: `git_identity_env`
            # reads the same `commit_identity(task)` into the runner's
            # GIT_AUTHOR_*/GIT_COMMITTER_*, and a mid-task push sends the
            # agent's commits as they are. Not `Worker._publish_identity`,
            # which swaps BOT_GIT_IDENTITY for the worker's `git_author_*`:
            # that swap made every mid-task push of a task naming nobody (a
            # service account's, as acceptance's are) foreign_author from
            # #1265 on (GITTOOLS-AUTHOR). The worker's own identity stays
            # beside it, for commits the worker wrote into the clone. A
            # person's task still refuses BOT_GIT_IDENTITY: it is not theirs.
            author=person,
            also_own=(bot,),
            pushes=pushes,
            branch_prefix=self.cfg.git_branch_prefix,
            timeout_seconds=int(self.cfg.git_clone_timeout_seconds),
        )
        self._git = gittools.GitTools(
            config,
            key=self._key,
            api=self._api,
            clone=Path(checkout),  # type: ignore[arg-type]
            private_dir=Path(private_dir) / "gittools",  # type: ignore[arg-type]
            logs_dir=Path(logs_dir),  # type: ignore[arg-type]
            logger=self.log,
            secrets=self._registered_secrets(),
            store=store,
            clock=self._clock,
            wall=self._wall,
            # The attempt's artifacts dir ($SWARM_ARTIFACTS_DIR): `logs_dir` is
            # `ws.logs`, and `workspace.create` puts `artifacts` beside it. Every refusal the relay answers is
            # recorded there, so a refused push is on record (#1805).
            answers_path=Path(logs_dir).with_name("artifacts")  # type: ignore[arg-type]
            / gittools.ANSWERS_ARTIFACT,
        )
        self.git_unavailable = None
        self.log.info("git tools offered for this attempt", pushes=pushes,
                      branches=list(pushable), child=self.is_child)
        return self._git.prepare(work)

    def _git_refusal(
        self,
        task: Any,
        checkout: Path | None,
        repository_url: str | None,
        clone_base: str | None,
        private_dir: Path | None,
        logs_dir: Path | None,
    ) -> str | None:
        """Why this attempt is not offered the git tools, or None."""
        if not isinstance(task, dict):
            return "no_verified_task"
        if not self.keyed or self._api is None:
            return "no_attempt_key"
        if not offers_git_tools(self.cfg.runner_profile):
            return "profile_has_no_git_tools"
        if not repository_url or checkout is None or not Path(checkout).is_dir():
            return "no_repository"
        if not clone_base:
            return "clone_base_unknown"
        if private_dir is None or logs_dir is None:
            return "no_private_directory"
        return None

    def _registered_secrets(self) -> tuple[str, ...]:
        """The values this worker's logger redacts: the task's registered secrets.

        Shared by reference across every bound logger (`logs.StructuredLogger
        .bind`), so this is the whole set, whichever logger was handed here.
        """
        return tuple(sorted(getattr(self.log, "_secrets", None) or ()))

    def known_children(self, work: Path) -> list[str]:
        """Child ids this task's attempts were answered with, from the spool."""
        out: list[str] = []
        responses = self.spool(work) / "responses"
        if not responses.is_dir():
            return out
        for path in sorted(responses.glob("*.json")):
            data = _read_small_json(path, 64 * 1024)
            if isinstance(data, dict) and isinstance(data.get("task_id"), str):
                out.append(data["task_id"])
            elif isinstance(data, dict) and isinstance(data.get("child_id"), str):
                # A child issue run or workflow (§3.6), by its ledger id.
                out.append(data["child_id"])
        return out

    def await_requested(self, work: Path) -> bool:
        return (self.spool(work) / "await").exists()

    def clear_await(self, work: Path) -> None:
        try:
            (self.spool(work) / "await").unlink()
        except FileNotFoundError:
            pass

    def has_requests(self, work: Path) -> bool:
        """Whether the agent has spooled anything, on either path; a cheap listing."""
        return (
            self._has_child_requests(work)
            or (self._git is not None and self._git.has_requests(work))
            or (self._search is not None and self._search.has_requests(work))
            or (self._state_reads is not None and self._state_reads.has_requests(work))
        )

    def _has_child_requests(self, work: Path) -> bool:
        requests = self.spool(work) / "requests"
        return self.offered and requests.is_dir() and any(requests.glob("*.json"))

    def tick(
        self,
        work: Path,
        *,
        limit: int | None | object = PER_TICK,
        deadline: float | None = None,
    ) -> int:
        """Answer the child requests (`_tick_children`), then the git requests.

        Git requests are answered `GIT_REQUESTS_PER_TICK` at a time, after the
        children's, and only while the tick's deadline has not passed. A git
        call this attempt is fenced on raises `gittools.GitToolFenced` -- a
        `ChildSubmitFenced` -- after the agent is answered `fenced`, so the
        lifecycle ends the attempt through the same fenced exit
        (`Worker._exit_fenced_mid_run`), never releasing the lease
        (invariant 5).
        """
        own = self._retry_deadline()
        deadline = own if deadline is None else min(deadline, own)
        answered = self._tick_children(work, limit=limit, deadline=deadline)
        if self._git is not None and self._clock() < deadline:
            answered += self._git.tick(work, limit=GIT_REQUESTS_PER_TICK)
        # The search tool's requests, last, on the same budget. A fenced search
        # raises `searchtools.SearchToolFenced`, a `ChildSubmitFenced`: the
        # same fenced exit as the git tools' (invariant 5).
        if self._search is not None and self._clock() < deadline:
            answered += self._search.tick(work, limit=SEARCH_REQUESTS_PER_TICK)
        # The state reads', after the search tool's, on the same budget. A
        # fenced read raises `statemcp.relay.StateReadsFenced`, a
        # `ChildSubmitFenced`: the same fenced exit (invariant 5).
        if self._state_reads is not None and self._clock() < deadline:
            answered += self._state_reads.tick(work, limit=SEARCH_REQUESTS_PER_TICK)
        return answered

    def _tick_children(
        self,
        work: Path,
        *,
        limit: int | None | object = PER_TICK,
        deadline: float | None = None,
    ) -> int:
        """Answer up to `limit` outstanding child requests. Returns how many were answered.

        `limit` is `max_child_requests_per_tick` by default (`PER_TICK`), a
        number, or None for every request in the spool (the drain before a
        park).

        Bounded in time as well as count: one `child_submit_retry_seconds` in
        total for this call, retries included -- the remaining budget is what
        each request's retries get -- so one tick is never the long in-worker
        wait invariant 4 forbids. A caller with its own budget passes it as
        `deadline` and the tick keeps to the earlier of the two. Raises
        `ChildSubmitFenced` when the route says this attempt is superseded:
        the lifecycle ends the way a fenced worker does mid-run.
        """
        own = self._retry_deadline()
        deadline = own if deadline is None else min(deadline, own)
        if not self.offered:
            return 0
        root = self.spool(work)
        requests = root / "requests"
        responses = root / "responses"
        if not requests.is_dir():
            return 0
        cap = self.cfg.max_child_requests_per_tick if limit is PER_TICK else limit
        answered = 0
        for path in sorted(requests.glob("*.json")):
            if isinstance(cap, int) and answered >= cap:
                break
            if self._clock() >= deadline:
                break
            request_id = path.stem
            if not REQUEST_ID.fullmatch(request_id) or path.is_symlink() or not path.is_file():
                continue
            response = responses / f"{request_id}.json"
            if response.exists():
                # Answered before a crash took the removal (§5 F1).
                path.unlink(missing_ok=True)
                continue
            answer = self._answer(path, request_id, deadline=deadline)
            _write_atomic(response, answer)
            path.unlink(missing_ok=True)
            answered += 1
        return answered

    def drain(self, work: Path) -> int:
        """Answer EVERY outstanding request before a park (§5 F3), in bounded time.

        Ticks until the spool is empty or ONE `child_submit_retry_seconds` has
        passed in total, retries included (the drain's remaining budget is
        what each tick, and each request's retries, get); whatever is left
        then is answered with the retryable `api_unavailable` refusal, without
        a call, so a park never carries a request it did not answer and the
        wait stays bounded (invariant 4).
        """
        if not self.offered:
            return 0
        deadline = self._retry_deadline()
        answered = 0
        while self._clock() < deadline:
            done = self._tick_children(work, limit=None, deadline=deadline)
            answered += done
            if not done or not self._has_child_requests(work):
                break
        root = self.spool(work)
        for path in sorted((root / "requests").glob("*.json")):
            request_id = path.stem
            if not REQUEST_ID.fullmatch(request_id) or path.is_symlink() or not path.is_file():
                continue
            response = root / "responses" / f"{request_id}.json"
            if not response.exists():
                _write_atomic(
                    response,
                    Refusal(
                        "api_unavailable",
                        "not submitted before the await; write the request again with the "
                        "same request_id",
                        retryable=True,
                    ).as_dict(),
                )
                answered += 1
            path.unlink(missing_ok=True)
        return answered

    def _answer(
        self, path: Path, request_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        refusal = self._local_refusal(path, request_id)
        if isinstance(refusal, Refusal):
            self.log.warning("child request refused by the worker", request_id=request_id,
                             code=refusal.code)
            return refusal.as_dict()
        child = refusal
        body = json.dumps(
            {
                "tenant_id": self.cfg.tenant_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
                "request_id": request_id,
                "child": child,
            }
        ).encode("utf-8")
        route = f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/children"
        assert self._key is not None and self._api is not None

        def _submit() -> tuple[int, dict[str, Any]]:
            headers = self._key.headers("POST", route, body, now=self._wall())
            return self._api.call("POST", route, body=body, headers=headers)

        try:
            _, payload = self._with_retry(_submit, deadline=deadline)
        except ChildSubmitFenced:
            raise
        except ChildApiError as exc:
            if exc.retryable:
                return Refusal(
                    "api_unavailable",
                    "the platform did not answer; write the request again with the same request_id",
                    retryable=True,
                ).as_dict()
            return Refusal(exc.code, exc.message or exc.code).as_dict()
        run = payload.get("child")
        if isinstance(run, dict):
            # A child issue run or workflow (§3.6): answered by its ledger id.
            child_id = run.get("child_id")
            if not isinstance(child_id, str):
                return Refusal("api_unavailable", "the platform answered without a child id",
                               retryable=True).as_dict()
            self.log.info("child run submitted", request_id=request_id, child_id=child_id,
                          kind=run.get("kind"), state=run.get("state"),
                          created=bool(payload.get("created")))
            return {"child_id": child_id, "request_id": request_id,
                    **{k: run.get(k) for k in ("kind", "state", "run_id", "workflow_id")}}
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        task_id = task.get("id")
        if not isinstance(task_id, str):
            return Refusal("api_unavailable", "the platform answered without a task id",
                           retryable=True).as_dict()
        self.log.info("child submitted", request_id=request_id, child_task_id=task_id,
                      created=bool(payload.get("created")))
        return {"task_id": task_id, "request_id": request_id}

    def _local_refusal(self, path: Path, request_id: str) -> Refusal | dict[str, Any]:
        """What the route would refuse anyway, refused here for free (§3.1 step 2)."""
        try:
            size = path.stat().st_size
        except OSError:
            return Refusal("request_invalid", "the request file could not be read")
        if size > self.cfg.max_child_request_bytes:
            return Refusal(
                "request_too_large",
                f"a request is at most {self.cfg.max_child_request_bytes} bytes",
            )
        data = _read_small_json(path, self.cfg.max_child_request_bytes)
        if not isinstance(data, dict):
            return Refusal("request_invalid", "a request is one JSON object")
        named = data.get("request_id", request_id)
        if named != request_id:
            return Refusal("request_invalid", "request_id must equal the file's name")
        child = {k: v for k, v in data.items() if k != "request_id"}
        kind = child.get("kind", "agent")
        if kind not in CHILD_KINDS:
            return Refusal("request_invalid", f"kind is one of {list(CHILD_KINDS)}")
        if kind != "agent":
            allowed = ISSUE_RUN_FIELDS if kind == "issue_run" else WORKFLOW_FIELDS
            unknown = sorted(set(child) - allowed)
            if unknown:
                return Refusal(
                    "request_invalid",
                    f"unknown keys {unknown}; an issue_run names an issue of this task's "
                    "repository, and a workflow names a saved spec by spec_name, never a "
                    "spec body, an image or a command",
                )
            return child
        unknown = sorted(set(child) - CHILD_FIELDS)
        if unknown:
            return Refusal(
                "request_invalid",
                f"unknown keys {unknown}; a child is named by runner_profile and "
                f"resource_class, never by an image, a command or a figure",
            )
        profile = RUNNER_PROFILES.get(child.get("runner_profile"))  # type: ignore[arg-type]
        if profile is None:
            return Refusal("unknown_runner_profile", "runner_profile names no profile")
        if profile.worker_action is not None:
            return Refusal(
                "worker_action_profile",
                f"{profile.name} acts on a forge with a credential no agent may steer",
            )
        if "input" in child and not isinstance(child["input"], dict):
            return Refusal("request_invalid", "input must be an object")
        return child

    # -- §3.3: the await ---------------------------------------------------

    def list_children(self) -> list[dict[str, Any]]:
        """Every child of this task, as swarm-api lists them (§6.2). Raises
        `ChildApiError` when no answer came within the retry bound."""
        assert self._key is not None and self._api is not None
        query = urlencode(
            {
                "tenant_id": self.cfg.tenant_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
            }
        )
        route = f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/children"
        signed = f"{route}?{query}"

        def _list() -> tuple[int, dict[str, Any]]:
            headers = self._key.headers("GET", signed, b"", now=self._wall())
            return self._api.call("GET", signed, headers=headers)

        _, payload = self._with_retry(_list)
        rows = payload.get("children")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def live_children(self, work: Path) -> list[str] | None:
        """Ids of children still running, [] for none, None when it is unknown.

        Asked only when this task has a child it knows of, so a task that never
        used the path pays nothing.
        """
        if not self.offered or not self.known_children(work):
            return []
        try:
            rows = self.list_children()
        except ChildApiError as exc:
            self.log.warning("could not list this task's children", code=exc.code)
            return None
        live = []
        for r in rows:
            if r.get("child_id"):
                # A run kind says whether it ended: a run's states are not a
                # task's (§3.6: DONE, FAILED, REJECTED, CANCELLED, NOT_READY).
                if not r.get("ended"):
                    live.append(str(r.get("child_id")))
            elif _state(r.get("state")) not in TERMINAL_STATES:
                live.append(str(r.get("task_id")))
        return live

    # -- §3.3 step 7: stage the children's results on resume ---------------

    def stage_results(self, work: Path, *, store: Any, max_total_bytes: int) -> dict[str, Any] | None:
        """Write `results/children.json` and stage each SUCCEEDED child's artifacts.

        A lost output is information for the agent, not a reason to fail it
        (§5 F9): such a child is listed with `outputs: unavailable` and why.
        Every object key is built from THIS attempt's tenant
        (`inputs.artifact_reference` -> `artifact_key`), so another tenant's
        artifact is unreachable by construction (invariant 9).
        """
        if not self.offered or not self.known_children(work):
            return None
        results = self.spool(work) / "results"
        # The worker's own directory: anything a restored checkpoint put here
        # (a link the agent made, last attempt's files) is replaced.
        if results.is_symlink() or results.is_file():
            results.unlink()
        elif results.is_dir():
            shutil.rmtree(results)
        results.mkdir(parents=True, exist_ok=True)
        try:
            rows = self.list_children()
        except ChildApiError as exc:
            document = {"children": [], "listing": "unavailable", "reason": exc.code}
            _write_atomic(results / "children.json", document)
            return document
        budget = max(0, int(max_total_bytes))
        out = []
        for row in sorted(rows, key=lambda r: str(r.get("task_id") or r.get("child_id"))):
            if row.get("child_id"):
                # A child issue run or workflow (§3.6): no artifacts to stage;
                # its state, and its run's pull request, are the result.
                out.append({
                    **{k: row.get(k) for k in ("child_id", "kind", "request_id", "state", "ended",
                                               "run_id", "workflow_id", "issue", "spec_name",
                                               "pull_request", "error")},
                    "outputs": "none",
                    "files": [],
                })
                continue
            child_id = str(row.get("task_id") or "")
            entry: dict[str, Any] = {
                "task_id": child_id,
                "request_id": row.get("request_id"),
                "state": row.get("state"),
                "end_cause": row.get("end_cause"),
                "parent_attempt_id": row.get("parent_attempt_id"),
                "outputs": "none",
                "files": [],
            }
            if _state(row.get("state")) is TaskState.SUCCEEDED and child_id:
                staged, used, reason = self._stage_child(
                    results, child_id, row.get("artifacts") or [], store=store, budget=budget
                )
                budget -= used
                entry["files"] = staged
                if reason is not None:
                    entry["outputs"] = "unavailable"
                    entry["reason"] = reason
                else:
                    entry["outputs"] = "staged" if staged else "none"
            out.append(entry)
        document = {"children": out, "listing": "complete"}
        _write_atomic(results / "children.json", document)
        self.log.info("staged this task's children's results", children=len(out))
        return document

    def _stage_child(
        self,
        results: Path,
        child_id: str,
        artifacts: list[Any],
        *,
        store: Any,
        budget: int,
    ) -> tuple[list[str], int, str | None]:
        doc = {"state": TaskState.SUCCEEDED.value, "result_summary": {"artifacts": artifacts}}
        base = results / child_id
        staged: list[str] = []
        used = 0
        for entry in artifacts:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            try:
                destination = _safe_destination(base, name)
                reference = inputs_mod.artifact_reference(
                    doc, tenant_id=self.cfg.tenant_id, upstream_task_id=child_id, filename=name
                )
                if used + reference.size_bytes > budget:
                    return staged, used, "over the staging cap for one attempt"
                destination.parent.mkdir(parents=True, exist_ok=True)
                used += int(store.download_file(reference.key, destination))
            except InputUnavailable as exc:
                return staged, used, str(exc)[:300]
            except Exception as exc:  # an expired object, a network fault
                return staged, used, f"{type(exc).__name__}"[:300]
            staged.append(destination.relative_to(results).as_posix())
        return staged, used, None


def _state(value: Any) -> TaskState | None:
    try:
        return TaskState(value)
    except ValueError:
        return None


def _safe_destination(base: Path, name: str) -> Path:
    """`base/<name>`, refusing anything that would leave `base`."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(p in ("", ".", "..") for p in pure.parts):
        raise InputUnavailable(f"artifact name {name!r} is not a plain relative path")
    destination = base.joinpath(*pure.parts)
    resolved_base = base.resolve()
    if not str(destination.resolve()).startswith(str(resolved_base) + os.sep):
        raise InputUnavailable(f"artifact name {name!r} resolves outside its directory")
    return destination


def _read_small_json(path: Path, limit: int) -> Any:
    """Read at most `limit` bytes of JSON; None for anything unreadable."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, default=str))
    os.replace(tmp, path)
