"""Runner git tools, the worker's half (docs/runner-git-tools.md §1.2-§1.4; lane G3).

The agent asks for git work through the `swarm-git` shim (`agent_worker.gitmcp`),
which writes one request per call into the spool `work/.swarm-git/`. This
module answers it, inside the worker -- the non-dumpable process that holds
the attempt key -- and never with a forge token: the container holds none
(§0), and swarm-api is the one that touches the forge (owner Q4 = A).

For each request it:

1. VALIDATES it, with the worker-side refusals of §1.2: the tool and its
   arguments are exactly `gitmcp.server.ARGUMENTS`; a push names only a branch
   this task may push (`not_this_tasks_branch`, `protected_branch`,
   `strategy_pushes_nothing`); a ref goes through `gitops.validate_ref`; a
   path is plain and relative. All of it before any request leaves.
2. For a PUSH, MIRRORS the agent's commits into a worker-owned repository
   (`gitops.prepare_publish_repo`: fresh `git init`, object-checked fetch, no
   alternates, nothing of the agent's `.git` read), checks every new commit's
   authorship (`foreign_author`) and scans its added text with the publish
   scan's own scanner (`leak_found`, naming `path:line rule`, never the
   match), then writes a bundle of exactly `<what origin has>..HEAD`.
3. SIGNS the request with the attempt key (`children.AttemptKey`) and sends it
   with the worker's ID token (`children.ChildApi`) to
   `POST /v1/attempts/{attempt_id}/git/{tool}`. A 409 is the fence (invariant
   5): `GitToolFenced` is raised, a `ChildSubmitFenced`, so the lifecycle ends
   the attempt the way it ends a fenced child submit, without touching the
   lease.
4. For a FETCH, applies the bundle swarm-api returns to the agent's clone
   (`refs/remotes/origin/<ref>`, or `refs/swarm/fetched/<sha>`); a REBASE then
   runs `git rebase` there, and a conflict leaves the rebase in progress with
   the conflicted paths listed.

THE CHECKS HERE ARE THE FAST ANSWER, NOT THE GATE. swarm-api re-derives every
policy from the task's signed spec (§1.2: "the API's check holds either
way"); this exists so an agent gets a specific refusal without a round trip,
and so a bundle holding a credential is never uploaded at all.

WHAT RUNS IN THE AGENT'S CLONE. The fetch's `git fetch <bundle>` and the
rebase. Neither carries a secret, so a hook, alias or driver the agent planted
in `.git/config` gains nothing it could not already do as the same uid (§1.3
step 2). Everything the push DECIDES -- the commit list, the authorship, the
scans, the bundle -- runs in the worker-owned repository instead, because
those are what the agent would want to fool.

WHAT THE WORKER TRUSTS AS "ORIGIN HAS IT": only shas it learnt itself -- the
clone base it recorded before the agent ran, every sha a fetch resolved to,
every tip a push answered with, and every sha swarm-api CONFIRMED origin has
when a push asked (#2413). Never a ref in the agent's clone.

WHY A PUSH ASKS (#2413). A step that runs `git fetch origin B && git reset
--hard B` in its own shell, B a pull request head that does not descend from
the clone base A, teaches the relay nothing, and the publish repository is
shallow AT A (`gitops.prepare_publish_repo` takes the recorded base as the
upload side's boundary), so `^A` excludes A alone. Every commit of B's
history then reads as new, and the first by somebody else -- old history,
long on origin/main -- was refused `foreign_author`. So before it refuses,
the push asks swarm-api which of the foreign commits origin has (`git_fetch`
with `confirm`, at most `MAX_CONFIRM`, the newest of each line, answered from
the forge) and excludes exactly those and their history from the check, the
scan and the bundle. The answer is read as a subset of the question. A
commit origin does not have is refused as before, and the refusal says so.

The same question widens the publish repository's BOUNDARY (#2559). A step
that runs `git fetch --depth=N origin X` in its own shell and merges X leaves
a clone cut off inside X, and a boundary of A alone sends the upload walk into
X's history the clone never had: the fetch fails. So `prepare_push` hands
`prepare_publish_repo` a confirmer (`confirm_on_origin`): the clone's
`.git/shallow` lines, read there as data, are asked about, and only those
origin confirms join the boundary, so a line the agent forged never does.

Wiring this into `ChildPath`'s tick and naming the shim in the runners is lane
G6; the route it calls is lane G5.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote

from swarm_redaction import redact as redact_text

from . import gitops
from .children import AttemptKey, ChildApi, ChildApiError, ChildSubmitFenced
from .gitmcp.server import (
    ARGUMENTS,
    MAX_GIT_REQUEST_BYTES,
    REQUEST_ID,
    REQUIRED,
    SPOOL_DIR_NAME,
    SPOOL_ENV,
    TOOL_NAMES,
)
from .gitops import EMPTY_CLONE_BASE, GitError
from .lifecycle import REGISTERED_SECRET_RULE, SCAN_OVERLAP_CHARS, SCAN_WINDOW_CHARS, ScanHit
from .lifecycle import _DiffLeakScanner, _first_difference
from .publish_scan import _DIFF_FLAGS, ScanError, _AllHits, _empty_tree, _stream
from .redact import scrub_text

# --------------------------------------------------------------------------
# The limits (§1.2; owner Q6 = A, "as written")
# --------------------------------------------------------------------------

#: The largest bundle either way. Said in the refusal.
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
#: A bundle up to this size rides in the signed body; a larger one goes to the
#: tenant's own GCS prefix and only its sha256 is signed (§1.3 step 2).
INLINE_BUNDLE_BYTES = 24 * 1024 * 1024
#: The largest file `read_private` returns.
MAX_FILE_BYTES = 1024 * 1024
#: Per attempt, per hour: every call, and pushes. swarm-api counts too; this
#: only spares a looping agent's round trips.
CALLS_PER_HOUR = 60
PUSHES_PER_HOUR = 20
_HOUR = 3600.0

#: The route's fenced answer (§1.4). Any 409 from the route is read as this.
GIT_FENCED = "git_fenced"
#: Lane AU-1's tools (docs/design/agent-autonomy.md §3.1, §3.2, §3.7): swarm-api
#: reads and writes GitHub for them, so the worker validates their shape and
#: relays them. Every one acts on the task's own repository only.
RELAYED_TOOLS = frozenset({"pr_view", "run_list", "run_view", "issue_view", "issue_list",
                           "release_status", "issue_create", "issue_comment"})
#: The tools that act on the task's own repository only.
OWN_REPOSITORY_TOOLS = frozenset({"git_fetch", "git_rebase_onto", "git_push_branch",
                                  "pr_checks"}) | RELAYED_TOOLS
#: The tools swarm-api runs git against the forge for, under its
#: `gitexec.CALL_DEADLINE_SECONDS` (`git_rebase_onto` is served as the fetch it
#: begins). The relay waits `GIT_CALL_TIMEOUT_SECONDS` for them; every other
#: tool keeps `ChildApi`'s short timeout.
GIT_CALL_TOOLS = frozenset({"git_push_branch", "git_fetch", "git_rebase_onto"})
#: How long the relay waits for one of `GIT_CALL_TOOLS` (#2576): swarm-api's
#: `gitexec.CALL_DEADLINE_SECONDS` (600) plus 60 s for the bundle upload and
#: the answer, and below the agent's shim's `gitmcp.server.WAIT_SECONDS` (900)
#: so the shim is still waiting when the answer comes. With ChildApi's 10 s a
#: workflow-touching push (a fetch, then the workflow-path walk) came back
#: `api_unavailable` while swarm-api was still pushing, and the step failed
#: `publish_failed`. A mirrored value (docs/mirrored-values.md), held by
#: tests/unit/worker/test_gittools_call_timeout.py.
GIT_CALL_TIMEOUT_SECONDS = 660
#: Each relayed argument's JSON type, checked before anything leaves.
_RELAYED_TYPES: dict[str, tuple[type, ...]] = {
    "number": (int,), "run_id": (int,), "limit": (int,), "failed_only": (bool,),
    "reopen": (bool,), "workflow": (str,), "branch": (str,), "head_sha": (str,),
    "status": (str,), "state": (str,), "query": (str,), "sha": (str,), "form": (str,),
    "title": (str,), "body": (str,), "dedupe_key": (str,), "labels": (list,),
    "sections": (dict, list),
}
#: What an agent might pass to aim one of those four elsewhere.
_REPOSITORY_KEYS = frozenset({"repo", "repository", "url", "remote"})

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
#: `owner/name` as GitHub spells it; `.` and `..` are not names.
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Where a large bundle is uploaded, under the tenant's own prefix (invariant 9).
BUNDLE_OBJECT = "tenants/{tenant}/attempts/{attempt}/git/{call}.bundle"
#: The publish repository's local branch, and so the bundle's one head.
_PUBLISH_BRANCH = "swarm-publish"
#: The artifact every refusal this relay answers is appended to (#1805), one
#: `{"step", "tool", "request_id", "answer"}` line each. The same file the
#: git-tools acceptance prompts have the agent append its answers to
#: (`tests/acceptance/test_runner_git_tools.py` ANSWERS), so a refusal the
#: worker answered before any request left -- one swarm-api never audits --
#: is on record even when the agent wrote nothing.
ANSWERS_ARTIFACT = "git-answers.jsonl"
#: The `step` of the worker's lines: no prompt names a step this, so a judge
#: reading the agent's last answer for a step never reads one of these.
ANSWERS_STEP = "worker"
#: The largest line the relay appends; a longer answer is not recorded.
_MAX_ANSWER_LINE = 64 * 1024
#: The most foreign commits one push asks origin about (#2413): swarm-api's
#: `gitexec.MAX_HAVES`, one depth-1 fetch each there. Only the newest commit
#: of each foreign line is asked -- origin having it means origin has its
#: history -- so a pull request head is one sha, not its thousand ancestors.
MAX_CONFIRM = 16


# --------------------------------------------------------------------------
# Refusals and the fence
# --------------------------------------------------------------------------


class GitToolRefused(Exception):
    """A §1.2 refusal: the code, a message for the agent, and any detail rows."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        refused: dict[str, Any] = {"code": self.code, "message": self.message,
                                   "retryable": self.retryable}
        refused.update(self.detail)
        return {"refused": refused}


class GitToolFenced(ChildSubmitFenced):
    """409 from a git route: this attempt is superseded (invariant 5).

    A `ChildSubmitFenced`, so the lifecycle's existing fenced exit
    (`Worker._exit_fenced_mid_run`) is the one path for both."""


# --------------------------------------------------------------------------
# What the worker knows about the attempt
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GitToolsConfig:
    """Everything here comes from the worker's config and the task's SIGNED
    spec, never from a request.

    `pushable` is the set of branches this task may push, derived by the
    caller exactly as the end-of-task publish derives its branch
    (`continuation.publish_branch`, `Worker._pr_author_branch`). `pushes` is
    False when the strategy promises to push nothing (`collect` without a
    carrier, a `single-pr` reader). `protected` is the repository's default
    branch as the forge reported it, when the worker knows it.
    """

    tenant_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    generation: int
    repository: str
    clone_base: str
    pushable: tuple[str, ...]
    author: tuple[str, str]
    pushes: bool = True
    protected: tuple[str, ...] = ()
    branch_prefix: str = "swarm/"
    also_own: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = 300
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    inline_bundle_bytes: int = INLINE_BUNDLE_BYTES
    max_file_bytes: int = MAX_FILE_BYTES
    calls_per_hour: int = CALLS_PER_HOUR
    pushes_per_hour: int = PUSHES_PER_HOUR


@dataclass
class _Origin:
    """Shas the worker itself learnt origin has. Never read from the clone."""

    haves: list[str] = field(default_factory=list)
    tips: dict[str, str] = field(default_factory=dict)

    def learn(self, sha: str, branch: str | None = None) -> None:
        if _FULL_SHA.match(sha) and sha not in self.haves:
            self.haves.append(sha)
        if branch and _FULL_SHA.match(sha):
            self.tips[branch] = sha


# --------------------------------------------------------------------------
# Validation (§1.2), pure
# --------------------------------------------------------------------------


def validate_request(tool: Any, arguments: Any) -> tuple[str, dict[str, Any]]:
    """The tool and its arguments, or `GitToolRefused`. Shape only."""
    if not isinstance(tool, str) or tool not in TOOL_NAMES:
        raise GitToolRefused("unknown_tool", f"tools are {', '.join(TOOL_NAMES)}")
    args = {} if arguments is None else arguments
    if not isinstance(args, dict):
        raise GitToolRefused("request_invalid", "arguments must be an object")
    if tool in OWN_REPOSITORY_TOOLS and _REPOSITORY_KEYS & set(args):
        raise GitToolRefused(
            "other_repository",
            f"{tool} acts on this task's own repository only; read_private is the one "
            "tool that takes a repository",
        )
    unknown = sorted(set(args) - ARGUMENTS[tool])
    if unknown:
        raise GitToolRefused("request_invalid", f"{tool} takes {sorted(ARGUMENTS[tool])}; "
                                                f"unknown {unknown}")
    missing = sorted(REQUIRED[tool] - set(args))
    if missing:
        raise GitToolRefused("request_invalid", f"{tool} needs {missing}")
    if "ref" in args:
        validate_tool_ref(args["ref"])
    if "branch" in args and not isinstance(args["branch"], str):
        raise GitToolRefused("bad_ref", "branch must be a string")
    if "repo" in args and (not isinstance(args["repo"], str) or not _REPO.match(args["repo"])
                           or args["repo"].split("/")[1] in (".", "..")):
        raise GitToolRefused("bad_ref", "repo must be owner/name")
    if "path" in args:
        validate_path(args["path"])
    if "pr" in args and (isinstance(args["pr"], bool) or not isinstance(args["pr"], int)
                         or args["pr"] < 1):
        raise GitToolRefused("request_invalid", "pr must be a pull request number")
    if tool in RELAYED_TOOLS:
        for name, value in args.items():
            wanted = _RELAYED_TYPES.get(name, ())
            if not isinstance(value, wanted) or (isinstance(value, bool) and bool not in wanted):
                raise GitToolRefused("request_invalid", f"{tool}'s {name} has the wrong type")
            if name in ("number", "run_id") and value < 1:
                raise GitToolRefused("request_invalid", f"{name} must be at least 1")
    return tool, dict(args)


def validate_tool_ref(ref: Any) -> str:
    """A branch or tag name, or a 40-hex sha. Never a full ref or a refspec."""
    if not isinstance(ref, str) or not ref:
        raise GitToolRefused("bad_ref", "ref must be a branch, a tag or a 40-hex sha")
    try:
        gitops.validate_ref(ref)
    except GitError as exc:
        raise GitToolRefused("bad_ref", str(exc)) from None
    if ref.startswith(("refs/", "/")) or ref.endswith(("/", ".lock")) or "//" in ref \
            or ref in ("HEAD", "@"):
        raise GitToolRefused("bad_ref", f"{ref!r} is not a branch, a tag or a sha")
    return ref


def validate_path(path: Any) -> str:
    """`checkpoint_content.py`'s rule: relative, no `..`, no empty segment, no
    backslash, no control character. The empty path is the repository's root."""
    if not isinstance(path, str):
        raise GitToolRefused("bad_path", "path must be a string")
    if path == "":
        return path
    if path.startswith("/") or "\\" in path or _CONTROL.search(path):
        raise GitToolRefused("bad_path", "path must be relative, with no backslash or "
                                         "control character")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise GitToolRefused("bad_path", "path must have no empty, '.' or '..' segment")
    return path


def branch_refusal(branch: str, cfg: GitToolsConfig) -> GitToolRefused | None:
    """Why this task may not push `branch`, or None. `protected` first, so the
    default branch is named as what it is."""
    if not isinstance(branch, str) or not branch:
        return GitToolRefused("not_this_tasks_branch", "no branch to push")
    if branch in cfg.protected:
        return GitToolRefused("protected_branch",
                              f"{branch!r} is the repository's protected default branch")
    try:
        gitops.validate_ref(branch)
    except GitError as exc:
        return GitToolRefused("bad_ref", str(exc))
    if not branch.startswith(cfg.branch_prefix) or branch not in cfg.pushable:
        allowed = ", ".join(cfg.pushable) or "none"
        return GitToolRefused("not_this_tasks_branch",
                              f"{branch!r} is not a branch this task may push ({allowed})")
    return None


# --------------------------------------------------------------------------
# The worker's half of each tool
# --------------------------------------------------------------------------


class GitTools:
    """The spool's relay, the push's preparation, the fetch's application.

    One per attempt. `clone` is the agent's working clone; `private_dir` is the
    worker's scratch outside `work/`; `secrets` are the task's registered
    secret values, so a push that would publish one is `leak_found` too.
    """

    def __init__(
        self,
        cfg: GitToolsConfig,
        *,
        key: AttemptKey,
        api: ChildApi,
        clone: Path,
        private_dir: Path,
        logs_dir: Path,
        logger: Any,
        secrets: Sequence[str] = (),
        store: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        git_binary: str = "git",
        answers_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        #: Where refusals are appended (`ANSWERS_ARTIFACT` in the attempt's
        #: artifacts dir), or None to record nothing.
        self.answers_path = Path(answers_path) if answers_path is not None else None
        self.log = logger
        self._key = key
        self._api = api
        self.clone = Path(clone)
        self.private_dir = Path(private_dir)
        self.logs_dir = Path(logs_dir)
        self._secrets = tuple(s for s in secrets if s)
        self._store = store
        self._clock = clock
        self._wall = wall
        self._git = git_binary
        self._calls: deque[float] = deque()
        self._pushes: deque[float] = deque()
        self.origin = _Origin()
        #: The commit the latest `git_rebase_onto` rebased the clone onto: a
        #: sha this relay fetched itself, so origin has it. The end-of-task
        #: publish diffs from it when HEAD still descends from it
        #: (`lifecycle.Worker._adopt_rebase_base`, SPEED-D1), so a lane rebased
        #: onto a newer base does not republish the base's commits as its own.
        self.rebased_onto: str | None = None
        #: The tip each successful `git_push_branch` left on a branch: the
        #: `new_sha` swarm-api answered with, which is the tip it recorded for
        #: this task and branch (`record_tip`). Only pushes write it -- never a
        #: fetch, whose tips are in `origin.tips` -- because the end-of-task
        #: publish leases on it (`pushed_tip`, #1188), and a lease on a tip
        #: this attempt did not itself push would replace somebody's work.
        self.pushed_tips: dict[str, str] = {}
        if cfg.clone_base != EMPTY_CLONE_BASE:
            self.origin.learn(cfg.clone_base)

    # -- the spool -----------------------------------------------------------

    @staticmethod
    def spool(work: Path) -> Path:
        return Path(work) / SPOOL_DIR_NAME

    def prepare(self, work: Path) -> dict[str, str]:
        """Create the spool; the agent's variable naming it."""
        root = self.spool(work)
        for name in ("requests", "responses"):
            (root / name).mkdir(parents=True, exist_ok=True)
        return {SPOOL_ENV: str(root)}

    def has_requests(self, work: Path) -> bool:
        requests = self.spool(work) / "requests"
        return requests.is_dir() and any(requests.glob("*.json"))

    def tick(self, work: Path, *, limit: int | None = None) -> int:
        """Answer up to `limit` spooled requests (all when None). Returns how many.

        Raises `GitToolFenced` after writing the agent its `fenced` answer."""
        root = self.spool(work)
        requests = root / "requests"
        if not requests.is_dir():
            return 0
        answered = 0
        for path in sorted(requests.glob("*.json")):
            if limit is not None and answered >= limit:
                break
            request_id = path.stem
            if not REQUEST_ID.fullmatch(request_id) or path.is_symlink() or not path.is_file():
                continue
            response = root / "responses" / f"{request_id}.json"
            if response.exists():
                path.unlink(missing_ok=True)
                continue
            try:
                answer = self.answer_file(path, request_id)
            except GitToolFenced:
                fenced = GitToolRefused(
                    "fenced", "this attempt no longer holds the task; it is ending").as_dict()
                _write_atomic(response, fenced)
                self._record(None, request_id, fenced)
                path.unlink(missing_ok=True)
                raise
            _write_atomic(response, answer)
            path.unlink(missing_ok=True)
            answered += 1
        return answered

    def answer_file(self, path: Path, request_id: str) -> dict[str, Any]:
        data = _read_small_json(path, MAX_GIT_REQUEST_BYTES)
        if not isinstance(data, dict):
            return GitToolRefused("request_invalid",
                                  f"a request is one JSON object of at most "
                                  f"{MAX_GIT_REQUEST_BYTES} bytes").as_dict()
        return self.answer(data.get("tool"), data.get("arguments"), request_id=request_id)

    def answer(self, tool: Any, arguments: Any, *, request_id: str) -> dict[str, Any]:
        """One request's answer: `{"tool", "result"}` or `{"refused"}`.

        Raises `GitToolFenced` only."""
        try:
            name, args = validate_request(tool, arguments)
            self._spend(name)
            handler = {
                "git_fetch": lambda: self.fetch(args["ref"], request_id=request_id),
                "git_rebase_onto": lambda: self.rebase_onto(args["ref"], request_id=request_id),
                "git_push_branch": lambda: self.push_branch(args.get("branch"),
                                                            request_id=request_id),
                "read_private": lambda: self.read_private(args["repo"], args["path"],
                                                          args["ref"], request_id=request_id),
                "pr_checks": lambda: self.pr_checks(args.get("pr"), request_id=request_id),
                **{relayed: (lambda tool=relayed: self.relay(tool, args, request_id=request_id))
                   for relayed in RELAYED_TOOLS},
            }[name]
            result = handler()
        except GitError as exc:
            refusal = GitToolRefused("git_failed", str(exc)[:300])
            self.log.warning("git tool failed", tool=str(tool)[:40], request_id=request_id)
            return self._record(tool, request_id, refusal.as_dict())
        except GitToolRefused as refusal:
            # The message is what says WHY (#1188: a `forge_refused` from
            # pr_checks logged only its code, so a 401 and a 403 -- and which
            # read -- looked the same). swarm-api scrubbed it of the token
            # (`_forge_refusal`); scrubbed here again of the task's registered
            # values and of anything credential-shaped.
            self.log.warning("git tool refused", tool=str(tool)[:40], code=refusal.code,
                             refusal=self._scrub_message(refusal.message),
                             request_id=request_id)
            return self._record(tool, request_id, refusal.as_dict())
        self.log.info("git tool answered", tool=name, request_id=request_id)
        return {"tool": name, "result": result}

    def _scrub_message(self, text: Any) -> str:
        """`text` for a log line: registered values and credential shapes masked, cut."""
        return str(scrub_text(redact_text(str(text)[:600]).text, self._secrets))[:300]

    def pushed_tip(self, branch: str) -> str | None:
        """The tip this attempt's own last `git_push_branch` left on `branch`, or None.

        Answered only for a branch this task may push (`cfg.pushable`), so the
        publish is never handed a lease on a branch it was not granted. Read by
        `lifecycle.Worker._adopt_tool_pushed_tip` (#1188).
        """
        if branch not in self.cfg.pushable or not self.cfg.pushes:
            return None
        tip = self.pushed_tips.get(branch)
        return tip if isinstance(tip, str) and _FULL_SHA.match(tip) else None

    def _record(self, tool: Any, request_id: str, answer: dict[str, Any]) -> dict[str, Any]:
        """Append a refusal to `answers_path`; return it unchanged.

        The directory is the agent's to write, so the file is opened with
        O_NOFOLLOW (a link the agent planted is not followed) and O_NONBLOCK
        (a fifo it planted does not hang the relay), and written only when it
        is a regular file. A refusal carries no secret: the worker holds no
        forge token, and `_foreign_author` scrubs the registered values. Never
        raises: recording is a courtesy, the answer is what the agent needs."""
        if self.answers_path is None:
            return answer
        line = json.dumps({
            "step": ANSWERS_STEP,
            "tool": tool if isinstance(tool, str) and tool in TOOL_NAMES else None,
            "request_id": request_id,
            "answer": answer,
        }, sort_keys=True, default=str) + "\n"
        raw = line.encode("utf-8")
        if len(raw) > _MAX_ANSWER_LINE or not self.answers_path.parent.is_dir():
            return answer
        try:
            fd = os.open(self.answers_path,
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o644)
        except OSError:
            self.log.warning("git tool answer not recorded", request_id=request_id)
            return answer
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.write(fd, raw)
        except OSError:
            self.log.warning("git tool answer not recorded", request_id=request_id)
        finally:
            os.close(fd)
        return answer

    def _spend(self, tool: str) -> None:
        now = self._clock()
        for window in (self._calls, self._pushes):
            while window and now - window[0] >= _HOUR:
                window.popleft()
        if len(self._calls) >= self.cfg.calls_per_hour:
            raise GitToolRefused("rate_limited",
                                 f"at most {self.cfg.calls_per_hour} git tool calls an hour",
                                 retryable=True)
        if tool == "git_push_branch" and len(self._pushes) >= self.cfg.pushes_per_hour:
            raise GitToolRefused("rate_limited",
                                 f"at most {self.cfg.pushes_per_hour} pushes an hour",
                                 retryable=True)
        self._calls.append(now)
        if tool == "git_push_branch":
            self._pushes.append(now)

    # -- the signed call (§1.3 step 2.3, §1.4) --------------------------------

    def route(self, tool: str) -> str:
        return f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/git/{tool}"

    def call(self, tool: str, fields: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        """POST the tool's body, signed with the attempt key. The body carries
        the attempt's fencing fields; a 409 raises `GitToolFenced`."""
        body = json.dumps({
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "attempt_id": self.cfg.attempt_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "request_id": request_id,
            **fields,
        }, sort_keys=True).encode("utf-8")
        route = self.route(tool)
        headers = self._key.headers("POST", route, body, now=self._wall())
        started = self._clock()
        try:
            # Only the git tools name a timeout; the rest keep the client's.
            if tool in GIT_CALL_TOOLS:
                _, payload = self._api.call("POST", route, body=body, headers=headers,
                                            timeout=GIT_CALL_TIMEOUT_SECONDS)
            else:
                _, payload = self._api.call("POST", route, body=body, headers=headers)
        except ChildApiError as exc:
            if exc.status == 409 or exc.code in (GIT_FENCED, "child_submit_fenced"):
                raise GitToolFenced(exc.status, GIT_FENCED, exc.message, retryable=False) from None
            if exc.retryable:
                # Named, so a step's summary says what the platform answered
                # (#2452: two refusals read only "did not answer", and a
                # timeout could not be told from a 503). The status and
                # swarm-api's code, or with no answer the exception class
                # `ChildApi` recorded and the seconds waited (#2576: a client
                # timeout reads "after 660 s"); never swarm-api's free text.
                waited = max(0, round(self._clock() - started))
                cause = (f"no answer ({exc.message or exc.code} after {waited} s)"
                         if exc.status is None else f"HTTP {exc.status} {exc.code}")
                raise GitToolRefused("api_unavailable",
                                     f"the platform did not answer: {cause}; ask again",
                                     retryable=True) from None
            raise GitToolRefused(exc.code, exc.message or exc.code) from None
        return payload

    # -- git_fetch -------------------------------------------------------------

    def fetch(self, ref: str, *, request_id: str) -> dict[str, Any]:
        """Ask swarm-api for `ref` and apply its bundle to the agent's clone."""
        ref = validate_tool_ref(ref)
        payload = self.call("git_fetch", {"ref": ref, "haves": list(self.origin.haves)},
                            request_id=request_id)
        sha = payload.get("resolved_sha")
        if not isinstance(sha, str) or not _FULL_SHA.match(sha):
            raise GitToolRefused("api_unavailable", "the platform answered without a sha",
                                 retryable=True)
        by_sha = bool(_FULL_SHA.match(ref))
        local = f"refs/swarm/fetched/{sha}" if by_sha else f"refs/remotes/origin/{ref}"
        bundle = self._received_bundle(payload, request_id=request_id)
        try:
            if bundle is not None:
                self._apply_bundle(bundle, sha, local)
            else:
                # Nothing to send: swarm-api found the sha among the haves.
                self._clone_git(["update-ref", local, sha], "gittools-fetch-ref")
        finally:
            if bundle is not None:
                bundle.unlink(missing_ok=True)
        self.origin.learn(sha, None if by_sha else ref)
        return {"ref": ref, "resolved_sha": sha, "local_ref": local}

    def _received_bundle(self, payload: dict[str, Any], *, request_id: str) -> Path | None:
        inline = payload.get("bundle")
        obj = payload.get("bundle_object")
        if inline is None and obj is None:
            return None
        if inline is not None:
            encoded_cap = (self.cfg.max_bundle_bytes * 4) // 3 + 4
            if not isinstance(inline, str) or len(inline) > encoded_cap:
                raise GitToolRefused("too_large", f"a bundle is at most "
                                                  f"{self.cfg.max_bundle_bytes} bytes")
            try:
                raw = base64.b64decode(inline.encode("ascii"), validate=True)
            except (ValueError, UnicodeEncodeError):
                raise GitToolRefused("api_unavailable", "the platform's bundle is not base64",
                                     retryable=True) from None
        else:
            if self._store is None or not isinstance(obj, str) \
                    or not obj.startswith(f"tenants/{self.cfg.tenant_id}/attempts/"):
                raise GitToolRefused("api_unavailable", "the platform named no bundle this "
                                                        "worker can read", retryable=True)
            raw = self._store.download_bytes(obj)
        if len(raw) > self.cfg.max_bundle_bytes:
            raise GitToolRefused("too_large", f"a bundle is at most "
                                              f"{self.cfg.max_bundle_bytes} bytes")
        digest = payload.get("bundle_sha256")
        if digest is not None and hashlib.sha256(raw).hexdigest() != digest:
            raise GitToolRefused("api_unavailable", "the bundle does not match its sha256",
                                 retryable=True)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=str(self.private_dir), prefix="fetch-", suffix=".bundle")
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return Path(name)

    def _apply_bundle(self, bundle: Path, sha: str, local: str) -> None:
        code, heads = self._git_in(self.private_dir, ["bundle", "list-heads", str(bundle)],
                                   "gittools-bundle-heads")
        if code != 0:
            raise GitToolRefused("api_unavailable", "the platform's bundle is unreadable",
                                 retryable=True)
        names = [line.split(" ", 1)[1] for line in heads.splitlines()
                 if line.startswith(sha + " ") and " " in line]
        if not names:
            raise GitToolRefused("api_unavailable", "the bundle does not hold the resolved sha",
                                 retryable=True)
        code, _ = self._clone_git(
            ["-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true",
             "-c", "fetch.recurseSubmodules=false", "-c", "gc.auto=0",
             "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
             str(bundle), f"+{names[0]}:{local}"],
            "gittools-fetch",
        )
        if code != 0:
            raise GitToolRefused("fetch_failed", "the bundle did not apply to the clone (its "
                                                 "prerequisites are not here, or an object "
                                                 "failed git's checks)")
        code, landed = self._clone_git(["rev-parse", "--verify", "--quiet", local],
                                       "gittools-fetch-check")
        if code != 0 or landed.strip() != sha:
            raise GitToolRefused("fetch_failed", f"{local} did not land on {sha}")

    # -- git_rebase_onto ---------------------------------------------------------

    def rebase_onto(self, ref: str, *, request_id: str) -> dict[str, Any]:
        """`git_fetch(ref)`, then `git rebase <sha>` in the agent's clone."""
        if self._rebase_in_progress():
            raise GitToolRefused("rebase_in_progress",
                                 "a rebase is in progress: resolve it and git rebase --continue "
                                 "(or --abort) first")
        fetched = self.fetch(ref, request_id=request_id)
        onto = fetched["resolved_sha"]
        # Recorded before the rebase runs: a conflicted one the agent then
        # continues lands on it too, and the publish checks that HEAD descends.
        self.rebased_onto = onto
        name, email = self.cfg.author
        code, _ = self._clone_git(
            ["-c", f"user.name={name}", "-c", f"user.email={email}",
             "-c", f"committer.name={name}", "-c", f"committer.email={email}",
             "rebase", "--no-autostash", onto],
            "gittools-rebase", extra_env={"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"},
        )
        if code == 0:
            _, head = self._clone_git(["rev-parse", "HEAD"], "gittools-rebase-head")
            return {"onto": onto, "head": head.strip(), "conflicts": []}
        if not self._rebase_in_progress():
            raise GitToolRefused("rebase_failed", "git rebase did not start (is the working "
                                                  "tree clean?); nothing was changed")
        _, listed = self._clone_git(["diff", "--name-only", "-z", "--diff-filter=U"],
                                    "gittools-rebase-conflicts")
        conflicts = sorted({p for p in listed.split("\0") if p})
        return {"onto": onto, "head": None, "conflicts": conflicts, "in_progress": True}

    def _rebase_in_progress(self) -> bool:
        git_dir = self.clone / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    # -- git_push_branch -------------------------------------------------------------

    def push_branch(self, branch: str | None, *, request_id: str) -> dict[str, Any]:
        """Scan, bundle and send HEAD's new commits to a branch this task may push."""
        if not self.cfg.pushes:
            raise GitToolRefused("strategy_pushes_nothing",
                                 "this task's strategy promises to push nothing")
        own = self.cfg.pushable[0] if self.cfg.pushable else ""
        chosen = own if branch is None else branch
        refusal = branch_refusal(chosen, self.cfg)
        if refusal is not None:
            raise refusal
        if self._rebase_in_progress():
            raise GitToolRefused("rebase_in_progress", "finish the rebase before pushing")
        prepared = self.prepare_push(chosen, request_id=request_id)
        try:
            payload = self.call("git_push_branch", prepared.fields, request_id=request_id)
        finally:
            prepared.discard()
        new_sha = payload.get("new_sha")
        if not isinstance(new_sha, str) or not _FULL_SHA.match(new_sha):
            raise GitToolRefused("api_unavailable", "the platform answered without a sha",
                                 retryable=True)
        self.origin.learn(new_sha, chosen)
        self.pushed_tips[chosen] = new_sha
        return {"branch": chosen, "old_sha": payload.get("old_sha"), "new_sha": new_sha}

    def prepare_push(self, branch: str, *, request_id: str) -> "PreparedPush":
        """Mirror, check and bundle; the signed body's fields. Raises a refusal."""
        head = gitops.read_head_as_data(self.clone)
        if not head:
            raise GitToolRefused("nothing_to_push", "HEAD has no commit")
        self.private_dir.mkdir(parents=True, exist_ok=True)
        # `prepare_publish_repo` reads a confirmer that raises as "origin
        # confirmed nothing", so what it raised is kept here. The fence is
        # raised whatever the fetch did (invariant 5: a superseded attempt
        # stops); a retryable refusal becomes the push's answer if the fetch
        # then fails, so the agent asks again rather than reading a
        # missing-history failure.
        unanswered: list[Exception] = []

        def confirm(shas: list[str]) -> list[str]:
            try:
                return self.confirm_on_origin(shas, request_id=request_id)
            except (GitToolRefused, GitToolFenced) as exc:
                unanswered.append(exc)
                raise

        try:
            repo = gitops.prepare_publish_repo(
                source_repo=self.clone, work_head=head, private_dir=self.private_dir,
                logs_dir=self.logs_dir, timeout_seconds=self.cfg.timeout_seconds,
                logger=self.log, base=self.cfg.clone_base, git_binary=self._git,
                local_branch=_PUBLISH_BRANCH, confirm_on_origin=confirm,
            )
        except GitError as exc:
            if unanswered:
                raise unanswered[-1] from None
            # Long enough for the fixed text and the end of git's own reason
            # (`prepare_publish_repo` appends it, redacted, #2559).
            raise GitToolRefused("push_failed", str(exc)[:700]) from None
        prepared = PreparedPush(repo=repo, fields={})
        if any(isinstance(exc, GitToolFenced) for exc in unanswered):
            prepared.discard()
            raise next(exc for exc in unanswered if isinstance(exc, GitToolFenced))
        try:
            negatives = [sha for sha in self.origin.haves if self._has_commit(repo, sha)]
            commits = self._new_commits(repo, negatives)
            if not commits:
                raise GitToolRefused("nothing_to_push", "every commit of HEAD is already on origin")
            authorship = self._authorship(repo)
            on_origin = self._confirm_foreign(repo, commits, negatives, authorship,
                                              request_id=request_id)
            if on_origin.confirmed:
                negatives = [*negatives, *on_origin.confirmed]
                commits = self._new_commits(repo, negatives)
                if not commits:
                    raise GitToolRefused("nothing_to_push",
                                         "every commit of HEAD is already on origin")
            self._check_authorship(repo, commits, authorship=authorship, on_origin=on_origin)
            hits = self.leaks(repo, commits)
            if hits:
                raise GitToolRefused(
                    "leak_found",
                    f"{len(hits)} credential-shaped line(s) in the commits to push; nothing was "
                    "uploaded. Remove them (rewrite the commits), then push again",
                    detail={"hits": hits},
                )
            bundle = repo / ".git" / "swarm-push.bundle"
            code, _ = self._git_in(
                repo, ["bundle", "create", str(bundle), f"refs/heads/{_PUBLISH_BRANCH}",
                       *(f"^{sha}" for sha in negatives)], "gittools-bundle")
            if code != 0 or not bundle.is_file():
                raise GitToolRefused("push_failed", "could not write the bundle")
            size = bundle.stat().st_size
            if size > self.cfg.max_bundle_bytes:
                raise GitToolRefused("too_large", f"the bundle is {size} bytes; at most "
                                                  f"{self.cfg.max_bundle_bytes}")
            raw = bundle.read_bytes()
            fields: dict[str, Any] = {
                "branch": branch,
                "old_sha": self.origin.tips.get(branch),
                "new_sha": head,
                "haves": negatives,
                "commits": len(commits),
                "bundle_bytes": size,
                "bundle_sha256": hashlib.sha256(raw).hexdigest(),
            }
            if size <= self.cfg.inline_bundle_bytes:
                fields["bundle"] = base64.b64encode(raw).decode("ascii")
            elif self._store is not None:
                key = BUNDLE_OBJECT.format(tenant=self.cfg.tenant_id, attempt=self.cfg.attempt_id,
                                           call=request_id)
                self._store.upload_bytes(key, raw, "application/x-git-bundle")
                fields["bundle_object"] = key
            else:
                raise GitToolRefused("too_large", f"the bundle is {size} bytes, over the "
                                                  f"{self.cfg.inline_bundle_bytes} sent inline, "
                                                  "and this worker has no bucket to upload it to")
            prepared.fields = fields
            return prepared
        except BaseException:
            prepared.discard()
            raise

    def confirm_on_origin(self, shas: list[str], *, request_id: str) -> list[str]:
        """The shas of `shas` origin has, asked of swarm-api (`git_fetch` with
        `confirm`, #2413), for `prepare_publish_repo`'s upload boundary (#2559).

        At most `MAX_CONFIRM` are asked; the answer is kept only where it
        names a sha that was asked, and each one kept is learnt as origin's.
        A retryable refusal is the agent's to ask again, so it is raised (the
        push is refused `api_unavailable`, as `_confirm_foreign` does); any
        other refusal, or no answer, confirms nothing, and the boundary stays
        the recorded base."""
        asked = [sha for sha in shas if _FULL_SHA.match(sha)][:MAX_CONFIRM]
        if not asked:
            return []
        try:
            payload = self.call("git_fetch", {"confirm": asked}, request_id=request_id)
        except GitToolRefused as exc:
            if exc.retryable:
                raise
            return []
        listed = payload.get("confirmed") if isinstance(payload, dict) else None
        if not isinstance(listed, list):
            return []
        confirmed = [sha for sha in asked if sha in listed]
        for sha in confirmed:
            self.origin.learn(sha)
        return confirmed

    def _has_commit(self, repo: Path, sha: str) -> bool:
        code, _ = self._git_in(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], "gittools-have")
        return code == 0

    def _new_commits(self, repo: Path, negatives: Iterable[str],
                     tip: str = f"refs/heads/{_PUBLISH_BRANCH}") -> list[str]:
        """Every commit the bundle would carry, oldest first: HEAD (or `tip`)
        not reachable from anything origin is known to have."""
        code, text = self._git_in(
            repo, ["rev-list", "--reverse", tip, *(f"^{sha}" for sha in negatives)],
            "gittools-commits")
        if code != 0:
            raise GitToolRefused("push_failed", "could not list the commits to push")
        return text.split()

    def _confirm_foreign(self, repo: Path, commits: list[str], negatives: list[str],
                         authorship: "_Authorship", *, request_id: str) -> "_OnOrigin":
        """Ask swarm-api which of the range's foreign commits origin has (#2413).

        Newest first, a foreign commit is asked about unless an asked one
        already reaches it, so one sha stands for a whole line of somebody
        else's history; at most `MAX_CONFIRM`. Nothing is asked when every
        commit is the task's own. The answer is kept only where it names a sha
        that was asked: it can narrow the question, never widen it. A platform
        that cannot answer (one from before `confirm`) leaves the range as it
        was, and the refusal says origin could not be asked; one that is
        unavailable is the agent's to ask again."""
        asked: list[str] = []
        covered: set[str] = set()
        for sha in reversed(commits):
            if sha in covered or not authorship.problems(sha):
                continue
            asked.append(sha)
            if len(asked) == MAX_CONFIRM:
                break
            covered.update(self._new_commits(repo, negatives, tip=sha))
        if not asked:
            return _OnOrigin()
        try:
            payload = self.call("git_fetch", {"confirm": asked}, request_id=request_id)
        except GitToolRefused as exc:
            if exc.retryable:
                raise
            return _OnOrigin(asked=asked, unanswered=exc.code)
        listed = payload.get("confirmed") if isinstance(payload, dict) else None
        if not isinstance(listed, list):
            return _OnOrigin(asked=asked, unanswered="no_answer")
        confirmed = [sha for sha in asked if sha in listed]
        for sha in confirmed:
            self.origin.learn(sha)
        return _OnOrigin(asked=asked, confirmed=confirmed)

    def _authorship(self, repo: Path) -> "_Authorship":
        """This task's identities, asked of git once, and a reader of commits."""
        def run(argv: list[str], slug: str) -> tuple[int, str]:
            return self._git_in(repo, argv[1:], slug)

        name, email = self.cfg.author
        g = [self._git, *gitops._NO_HOOKS, *gitops._worker_identity(name, email)]
        author, committer = gitops._worker_idents(run, g)
        also = [
            gitops._worker_idents(run, [self._git, *gitops._NO_HOOKS,
                                        *gitops._worker_identity(n, e)])
            for n, e in self.cfg.also_own if (n, e) != (name, email)
        ]
        # WHAT A REBASE WRITES (#1805). git keeps a replayed commit's author
        # and makes the rebaser its committer, and this task rebases as
        # `cfg.author` -- `rebase_onto` passes it, and the agent's own
        # `git rebase --continue` runs under the same GIT_COMMITTER_*. So a
        # commit of this task's branch written as one of its OTHER identities
        # (the worker's `git_author_*`, which the lane's publish folds as when
        # the task names nobody) comes back authored as that identity and
        # committed as `cfg.author`. Accepted only in that shape: the author
        # is still one of this task's own, so nobody else's commit is pushed,
        # and the committer is the one identity this attempt rebases as.
        rebased = [(mine, committer) for mine, _ in also]
        authors = [author, *(mine for mine, _ in also)]

        def judge(sha: str) -> tuple[Any, list[str]]:
            commit = gitops._read_commit(run, g, sha)
            return commit, gitops._foreign(commit, author, committer, also=[*also, *rebased])

        return _Authorship(judge, authors, [committer, *(c for _, c in also)])

    def _check_authorship(self, repo: Path, commits: list[str], *,
                          authorship: "_Authorship | None" = None,
                          on_origin: "_OnOrigin | None" = None) -> None:
        """`gitops.verify_worker_authorship`'s rule, over exactly the commits
        sent -- which, after a rebase onto origin's newer tip, is not the
        first-parent chain from the clone base that function walks.

        A commit origin said it does NOT have is named first: it is the one
        the agent has to remove, where the oldest foreign commit may be
        history origin was never asked about."""
        authorship = authorship or self._authorship(repo)
        on_origin = on_origin or _OnOrigin()
        denied = [sha for sha in commits if on_origin.found(sha) is False]
        rest = [sha for sha in commits if sha not in set(denied)]
        for sha in [*denied, *rest]:
            commit, problems = authorship.read(sha)
            if problems:
                raise self._foreign_author(commit, problems, authorship.authors,
                                           authorship.committers, on_origin)

    def _foreign_author(self, commit: Any, problems: list[str],
                        authors: list[tuple[str, str]],
                        committers: list[tuple[str, str]],
                        on_origin: "_OnOrigin | None" = None) -> GitToolRefused:
        """The refusal, naming the commit, the field(s) and both identities.

        A field is `author` or `committer` when that identity is none of this
        task's own, both when each is but the pair is not one it writes, and
        `signature` or `message` for a signed commit or attribution. Emails
        and names only, scrubbed of every registered secret: an agent can put
        any text in an identity, and this message reaches the artifacts."""
        fields: list[str] = []
        if commit.author not in authors:
            fields.append("author")
        if commit.committer not in committers:
            fields.append("committer")
        mixed = not fields and any(p.endswith("a pair the worker never writes") for p in problems)
        if mixed:
            fields = ["author", "committer"]
        if commit.signed:
            fields.append("signature")
        if any(p.startswith("its message carries attribution") for p in problems):
            fields.append("message")
        expected = list(dict.fromkeys(e for _, e in authors))
        found = {"author": commit.author[1], "committer": commit.committer[1]}
        on_origin = on_origin or _OnOrigin()
        message = (
            f"commit {commit.sha[:12]} ({', '.join(fields)}): its author is "
            f"{commit.author[0]} <{commit.author[1]}> and its committer "
            f"{commit.committer[0]} <{commit.committer[1]}>; this task pushes only commits "
            f"authored as {' or '.join(f'{n} <{e}>' for n, e in authors)}"
            f" ({'; '.join(problems)}); {on_origin.says(commit.sha)}. Nothing was uploaded"
        )
        scrub = (lambda text: str(scrub_text(text, self._secrets))) if self._secrets else str
        return GitToolRefused("foreign_author", scrub(message), detail={
            "commit": commit.sha[:12],
            "fields": fields,
            "found": {k: scrub(v) for k, v in found.items()},
            "expected": [scrub(e) for e in expected],
            "on_origin": on_origin.found(commit.sha),
        })

    def leaks(self, repo: Path, commits: list[str]) -> list[str]:
        """`path:line rule` for every credential a commit ADDS, never the match.

        Each commit against its first parent (a root against the empty tree),
        read by the publish scan's own scanner and predicate, plus the task's
        registered secrets as the end-of-task publish checks them."""
        found: list[str] = []
        try:
            empty = _empty_tree(repo)
            for sha in commits:
                code, parents = self._git_in(repo, ["rev-list", "--parents", "-n", "1", sha],
                                             "gittools-parents")
                if code != 0:
                    raise GitToolRefused("push_failed", "could not read a commit's parent")
                listed = parents.split()
                parent = listed[1] if len(listed) > 1 else empty
                collect = _Hits(self._secrets)
                scanner = _DiffLeakScanner(collect, window=SCAN_WINDOW_CHARS,
                                           overlap=SCAN_OVERLAP_CHARS)
                collect.scanner = scanner
                _stream(repo, ["diff", *_DIFF_FLAGS, parent, sha, "--"], scanner.feed, (0,))
                scanner.close()
                found += [f"{hit.path}:{hit.line} {hit.rule}" for hit in collect.hits]
        except ScanError as exc:
            raise GitToolRefused("push_failed", f"the credential scan could not run: {exc}") \
                from None
        return sorted(set(found), key=found.index)

    # -- read_private and pr_checks: swarm-api's to answer -------------------------

    def read_private(self, repo: str, path: str, ref: str, *, request_id: str) -> dict[str, Any]:
        payload = self.call("read_private", {"repo": repo, "path": validate_path(path),
                                             "ref": validate_tool_ref(ref)},
                            request_id=request_id)
        text = payload.get("text")
        if isinstance(text, str) and len(text.encode("utf-8")) > self.cfg.max_file_bytes:
            raise GitToolRefused("too_large", f"a file is at most {self.cfg.max_file_bytes} bytes")
        return {k: payload[k] for k in ("repo", "path", "ref", "resolved_sha", "kind", "text",
                                        "entries") if k in payload}

    def pr_checks(self, pr: int | None, *, request_id: str) -> dict[str, Any]:
        payload = self.call("pr_checks", {"pr": pr}, request_id=request_id)
        return {k: payload[k] for k in ("pr", "head_sha", "state", "rows") if k in payload}

    def relay(self, tool: str, args: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        """An AU-1 tool: swarm-api reads or writes GitHub and answers; the
        worker passes the answer on as it came. Its text from GitHub is
        already redacted and between the call's nonce lines (`data_marker`)."""
        payload = self.call(tool, dict(args), request_id=request_id)
        return payload if isinstance(payload, dict) else {}

    # -- git, the worker's way ----------------------------------------------------------

    def _git_in(self, repo: Path, argv: list[str], slug: str) -> tuple[int, str]:
        return gitops._git_text(
            [self._git, *gitops._NO_HOOKS, *argv], repo=repo, private_dir=self.private_dir,
            logs_dir=self.logs_dir, slug=slug, timeout_seconds=self.cfg.timeout_seconds,
            logger=self.log,
        )

    def _clone_git(self, argv: list[str], slug: str, *,
                   extra_env: dict[str, str] | None = None) -> tuple[int, str]:
        """git in the AGENT's clone: nothing it runs carries a secret (§1.3)."""
        if not extra_env:
            return self._git_in(self.clone, argv, slug)
        from .procman import run_child

        out = self.logs_dir / f"git-{slug}.out.log"
        result = run_child(
            [self._git, *gitops._NO_HOOKS, *argv], cwd=self.clone,
            env={**gitops._git_env(self.private_dir), **extra_env}, stdout_path=out,
            stderr_path=self.logs_dir / f"git-{slug}.err.log",
            timeout_seconds=self.cfg.timeout_seconds, grace_seconds=5,
            max_stdout_bytes=256 * 1024, max_stderr_bytes=256 * 1024, logger=self.log,
        )
        if result.timed_out:
            raise GitError(f"git {slug} timed out after {self.cfg.timeout_seconds}s")
        try:
            text = out.read_text(errors="replace")
        except OSError:
            text = ""
        return result.exit_code, text


@dataclass(frozen=True)
class _Authorship:
    """`GitTools._authorship`: this task's identities and a memoised reader of
    what each commit's identities get wrong (`gitops._foreign`)."""

    judge: Callable[[str], tuple[Any, list[str]]]
    authors: list[tuple[str, str]]
    committers: list[tuple[str, str]]
    _read: dict[str, tuple[Any, list[str]]] = field(default_factory=dict)

    def read(self, sha: str) -> tuple[Any, list[str]]:
        if sha not in self._read:
            self._read[sha] = self.judge(sha)
        return self._read[sha]

    def problems(self, sha: str) -> list[str]:
        return self.read(sha)[1]


@dataclass(frozen=True)
class _OnOrigin:
    """What swarm-api said about the foreign commits a push asked it about."""

    asked: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    #: The refusal's code when the platform could not answer at all.
    unanswered: str | None = None

    def found(self, sha: str) -> bool | None:
        """False when origin was asked and lacks `sha`; None when unknown."""
        if self.unanswered is None and sha in self.asked:
            return sha in self.confirmed
        return None

    def says(self, sha: str) -> str:
        if self.found(sha) is False:
            return "origin does not have it (swarm-api checked)"
        if self.unanswered is not None:
            return f"origin could not be asked whether it has it ({self.unanswered})"
        return "it was not checked against origin"


@dataclass
class PreparedPush:
    """The worker-owned repository a push was prepared in, and the body's fields."""

    repo: Path
    fields: dict[str, Any]

    def discard(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)


class _Hits(_AllHits):
    """The publish scan's every-hit predicate, plus the task's registered
    secrets, which `Worker._leaks_in_added_text` checks first."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._registered = tuple(secrets)

    def __call__(self, path: str, text: str):
        if self._registered:
            scrubbed = str(scrub_text(text, self._registered))
            if scrubbed != text:
                scanner = self.scanner
                assert scanner is not None
                offset = _first_difference(text, scrubbed)
                line = scanner._file_line(scanner._window_line + text.count("\n", 0, offset))
                hit = ScanHit(path or "a file", REGISTERED_SECRET_RULE, line)
                if hit not in self._seen:
                    self._seen.add(hit)
                    self.hits.append(hit)
        return super().__call__(path, text)


def _read_small_json(path: Path, limit: int) -> Any:
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
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(document, indent=2, default=str))
    os.replace(tmp, path)
