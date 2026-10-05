"""Shared machinery for the runners that drive a coding-agent CLI.

`claude_code` and `codex` differ only in which binary they start and which flags
that binary takes. Everything else -- finding the binary, refusing to start
without the tenant's key, capping output, turning a rate limit into a park
signal, turning the transcript into an artifact -- is identical, and identical
code is the only way those two runners stay identical in behaviour.

On flags: the argv prefix has a conservative default and can be overridden with
an environment variable set by the PLATFORM (the image, or the Cloud Run Job
definition) -- never by a caller, whose input never reaches argv except as the
prompt. That is what keeps these runners working across CLI releases without
anybody guessing at flags in a Dockerfile.

Two properties that are load-bearing rather than incidental:

* **`CLAUDE_CODE_BIN` / `CODEX_BIN` and their `_ARGS` really are platform-set.**
  They arrive in this process's environment, and the only things the worker puts
  there are its own configuration and the values the frozen profile declares in
  `RunnerProfile.secrets`. `secrets.resolve_credentials` exports exactly those
  declared names and nothing else, which is what stops somebody who may only ADD
  a secret version -- a per-tenant admin, who is deliberately not trusted to read
  the key back -- from shipping `{"CLAUDE_CODE_BIN": "/bin/sh"}` and getting a
  shell.

* **The CLI's output is redacted before it becomes an artifact or a summary.**
  The worker's logger scrubs its own log lines, but this process writes the raw
  stdout and stderr into `artifacts/`, writes the transcript, and returns up to
  2000 characters of that stdout as the summary that becomes
  `task.result_summary` in Firestore. A CLI that echoes its configuration, or a
  tool error quoting an `Authorization` header, would otherwise land verbatim in
  a GCS object and a Firestore document. This runner holds the key, so this
  runner scrubs it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .. import expected_outputs as expected_mod
from .. import issue as issue_mod
from ..logs import StructuredLogger
from ..procman import TRUNCATION_MARK, ChildProcess, ChildResult, run_child
from ..redact import collect_secrets, scrub_file, scrub_text
from .base import (
    SPEND_KEYS,
    CredentialRevokedSignal,
    QuotaExhaustedSignal,
    RunnerContext,
    RunnerFailure,
)
from .limits import platform_ceilings, resolve_limits
from .streams import AgentStreamFiles

BASE_PATH = "/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"

#: The transcript artifact is written WHOLE or not at all. It used to be
#: `json.dumps(parsed, indent=2)[:4_000_000]`, which cut a long run's document
#: mid-token into JSON nothing could parse, under a name that promised JSON.
#: Past this size it is omitted and `transcript_skipped: "too_large"` says so
#: (the worker carries it into `result_summary.agent_streams`); the agent's own
#: stdout -- the NDJSON stream -- is the canonical transcript in every case and
#: is always uploaded.
TRANSCRIPT_MAX_CHARS = 4_000_000

#: Environment values that are copied through to the CLI and may themselves
#: carry a credential. A proxy URL routinely embeds `user:password@`, so it is
#: registered for redaction alongside the provider key rather than assumed
#: harmless. `NO_PROXY` and `NODE_EXTRA_CA_CERTS` are a host list and a path,
#: never a secret, so they are passed through without being redacted -- a path
#: appearing in output is diagnostic information worth keeping readable.
_SENSITIVE_PASSTHROUGH: tuple[str, ...] = ("HTTPS_PROXY", "HTTP_PROXY")
_PLAIN_PASSTHROUGH: tuple[str, ...] = ("NO_PROXY", "NODE_EXTRA_CA_CERTS")

#: Substrings that mean "the provider said no, try later".
_RATE_LIMIT_MARKERS = (
    "rate_limit_error",
    "rate limit",
    "rate-limited",
    "ratelimit",
    "429",
    "too many requests",
    "overloaded_error",
    "insufficient_quota",
    "quota exceeded",
    "resource_exhausted",
    "usage limit reached",
    # A CLI that surfaces the raw HTTP header and nothing else. `Retry-After` is
    # only ever sent with a 429 or a 503, so it is a rate limit or an outage --
    # both of which must park rather than burn one of the task's three attempts.
    # `_RETRY_AFTER_PATTERNS` then reads the value out of the same line.
    "retry-after",
    "retry_after",
)

#: Substrings that mean "this credential is no longer usable" -- as opposed to
#: "wait and try again", which is what _RATE_LIMIT_MARKERS covers.
#:
#: THIS EXISTS BECAUSE OF HOW SUBSCRIPTION CREDENTIALS BEHAVE. Refreshing an
#: OAuth credential REVOKES the previously issued access token; the platform
#: refreshes every registered account on a timer, including accounts an agent
#: is using right now. So a long-running attempt can have the token in its
#: environment revoked underneath it, mid-run, through no fault of its own.
#: The remedy is not to wait -- the token is gone permanently -- it is to read
#: the secret again and restart with the credential that replaced it.
#:
#: Deliberately NOT including a bare "401": it occurs in ordinary agent output
#: (a transcript discussing HTTP, a test fixture) and a false positive here
#: silently restarts a healthy run. Every marker below names an authentication
#: failure explicitly.
_CREDENTIAL_MARKERS = (
    "oauth access token has been revoked",
    "authentication_error",
    "invalid api key",
    "invalid_api_key",
    "invalid bearer token",
    "fix external api key",
    "please run /login",
    "unauthorized",
    "\"api_error_status\":401",
    "api_error_status: 401",
)

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[-_ ]?after[\"':= ]+(\d+)", re.IGNORECASE),
    re.compile(r"try again in (\d+)\s*second", re.IGNORECASE),
    re.compile(r"try again in (\d+)\s*minute", re.IGNORECASE),
    re.compile(r"resets? in (\d+)\s*minute", re.IGNORECASE),
)
_RESET_AT_PATTERN = re.compile(
    r"\"(?:reset_at|resets_at|resetsAt|resets_at_utc)\"\s*:\s*\"([^\"]+)\"", re.IGNORECASE
)


@dataclass(frozen=True)
class CliAgentSpec:
    name: str
    provider: str
    #: Environment variable naming the binary, then the default binary name.
    binary_env: str
    binary_default: str
    #: Environment variable holding a JSON list of argv flags, then the default.
    args_env: str
    args_default: tuple[str, ...]
    #: Environment variable that must carry the tenant's credential.
    key_env: str
    #: Alternative credential variables, tried in order when `key_env` is unset.
    #:
    #: Claude Code accepts EITHER a pay-per-token API key (ANTHROPIC_API_KEY) or
    #: a subscription OAuth token (CLAUDE_CODE_OAUTH_TOKEN, from
    #: `claude setup-token`). A tenant paying for a Claude subscription has no
    #: API key at all, and requiring one would mean buying metered API access
    #: they already have a plan for. Whichever variable the tenant's secret
    #: supplies is the one passed to the child, and only that one.
    alt_key_envs: tuple[str, ...] = ()
    #: Flag used to select a model, if the CLI supports one.
    model_flag: str | None = "--model"
    transcript_name: str = "transcript.json"
    #: The flag that continues an earlier session by id, when the CLI has one
    #: (claude-code: `--resume`). Set, it is what lets an attempt move to
    #: another account mid-run and carry on where it stopped (S13/S14); None,
    #: the runner never watches its stream for that and behaves as before.
    resume_flag: str | None = None
    #: Resume the session ONCE, with `FINISH_PROMPT`, when the run ends on an
    #: answer that announces pending work or with a background shell open
    #: (owner decision 2026-10-05; see `pending_work`). Needs `resume_flag`.
    finish_on_pending: bool = False
    #: After the agent's turn ends, run the worker's expected-outputs check and
    #: publish credential scan against the tree, and resume the session for up
    #: to `REPAIR_MAX_TURNS` repair turns naming what failed (#624, owner
    #: decision 2026-10-05; see `repair_problems`). Needs `resume_flag`.
    repair_checks: bool = False


# ---------------------------------------------------------------------------
# The account channel: what a runner on a pool account tells its worker
# ---------------------------------------------------------------------------
#
# An attempt holding a pool account can move to another account mid-run
# (S13/S14) and forwards its rate-limit readings to the broker (S15). The
# worker owns the hold and the broker; THIS process owns the CLI and its
# stream. They talk through two small files in the attempt's private directory
# (`ws.private`, never the agent's working tree), named by the worker in the
# environment, and only when the attempt holds an account (the worker sets the
# variables then and never otherwise):
#
#   ACCOUNT_STREAM_ENV  written here, read by the worker: the session id, the
#                       latest reading of each window, how many turns have
#                       ended, and -- when this runner stopped the CLI for a
#                       swap -- why. Rewritten atomically, mode 0600.
#   ACCOUNT_MOVE_ENV    written by the worker when its hold is marked to move
#                       (a drain); its presence asks this runner to stop the
#                       CLI at the next turn boundary.
#
# And one variable the worker sets on the restart that follows a swap:
#
#   RESUME_SESSION_ENV  the session to continue with `spec.resume_flag`.
#
# THE SESSION ID IS NEVER LOGGED. It is in the channel file and the restarted
# CLI's argv and nowhere else: the `child started` line prints the argv with
# it masked, and the runner's logger is told it is a secret.
ACCOUNT_STREAM_ENV = "SWARM_ACCOUNT_STREAM"
ACCOUNT_MOVE_ENV = "SWARM_ACCOUNT_MOVE"
RESUME_SESSION_ENV = "SWARM_RESUME_SESSION"

#: What a resumed CLI is told. The conversation, the tool results and the
#: workspace are all as they were; this is the one new user message.
RESUME_PROMPT = (
    "Your session was moved to another account at a turn boundary. Continue "
    "the task exactly where you left off; nothing in the workspace changed."
)

#: Why this runner stopped the CLI at a turn boundary.
STOP_EXHAUSTED = "exhausted"
STOP_DRAIN = "drain"

#: What a session id looks like. Anything else is not passed to `--resume`.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_WINDOW_NAME = re.compile(r"^[a-z0-9_]{1,40}$")

#: How often the watched loop looks at the stream. The turn boundary it waits
#: for is seconds apart at the fastest, so a quarter of a second loses nothing.
_WATCH_SECONDS = 0.25


def _reading_of(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(window, {utilization, resets_at}) from one `rate_limit_event`, or None.

    `rateLimitType` names the window, `resetsAt` is epoch seconds, and
    `utilization` the 0-1 fraction. A REJECTED reading with no utilization is
    a full window: the provider refused the request on it. Anything that does
    not say all of that is not a reading and is dropped rather than guessed at.
    """
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    name = info.get("rateLimitType")
    resets = info.get("resetsAt")
    if not isinstance(name, str) or not _WINDOW_NAME.match(name):
        return None
    if isinstance(resets, bool) or not isinstance(resets, (int, float)):
        return None
    utilization = info.get("utilization")
    if utilization is None and info.get("status") == "rejected":
        utilization = 1.0
    if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
        return None
    if not 0.0 <= float(utilization) <= 1.0:
        return None
    try:
        at = datetime.fromtimestamp(float(resets), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return name, {"utilization": float(utilization), "resets_at": at.isoformat()}


class AccountStreamWatcher:
    """Reads the CLI's stream as it is written, for the account channel.

    `poll()` reads the complete lines appended since the last call -- never
    per line on a timer, and never the whole file again -- and answers with a
    stop reason at a TURN BOUNDARY when the CLI must stop there:

      * a TURN BOUNDARY is a top-level `user` event: the tool results of one
        turn are complete and the next model request has not been answered.
        Resuming from there loses nothing the session did not record.
        (`parent_tool_use_id` set means a subagent's turn, not the session's.)
      * stop for `exhausted` when a `rate_limit_event` was REJECTED since the
        last boundary: the account has no quota left and the next request
        will fail;
      * stop for `drain` when the worker has written the move file.

    It never raises on what the stream contains; a line that is not JSON is
    not an event.

    THE STREAM COMES FROM THE PIPE, NOT THE FILE, once `tap()` is called (the
    B11 review). The capture file of a `keep_tail` run stops growing at its
    head and holds the end in memory until the CLI exits, so a watcher reading
    the file saw nothing of a long session past the cap: no turn boundary, no
    reading, no drain. `_run_watched` hands `tap()`'s callable to the child's
    stdout capture, which calls it with every chunk before the cap applies.
    Without a tap (a test driving the watcher by hand) it reads the file.
    """

    def __init__(self, stdout_path: Path, channel: Path, move: Path | None) -> None:
        self._stdout = stdout_path
        self._channel = channel
        self._move = move
        self._offset = 0
        self._partial = b""
        self._fed: list[bytes] | None = None
        self._fed_lock = threading.Lock()
        self.session_id: str | None = None
        self.readings: dict[str, dict[str, Any]] = {}
        self.turns = 0
        self.exhausted = False
        self.stopped_for: str | None = None
        self._dirty = False

    def poll(self) -> str | None:
        stop: str | None = None
        for event in self._new_events():
            kind = event.get("type")
            sid = event.get("session_id")
            if isinstance(sid, str) and _SESSION_ID.match(sid) and sid != self.session_id:
                self.session_id = sid
                self._dirty = True
            if kind == "rate_limit_event":
                reading = _reading_of(event)
                if reading is not None and self.readings.get(reading[0]) != reading[1]:
                    self.readings[reading[0]] = reading[1]
                    self._dirty = True
                info = event.get("rate_limit_info")
                status = info.get("status") if isinstance(info, dict) else None
                if (status or event.get("status")) == "rejected":
                    self.exhausted = True
            elif kind == "user" and not event.get("parent_tool_use_id"):
                self.turns += 1
                self._dirty = True
                if stop is None and self.stopped_for is None:
                    if self.exhausted:
                        stop = STOP_EXHAUSTED
                    elif self._move is not None and self._move.exists():
                        stop = STOP_DRAIN
                    if stop is not None:
                        self.stopped_for = stop
                        # Nothing after this boundary is read: the CLI is
                        # being stopped here.
                        break
        if self._dirty:
            self.write()
        return stop

    def tap(self) -> Callable[[bytes], None]:
        """The callable the stdout capture feeds; from now on the file is not read."""
        self._fed = []
        return self._feed

    def _feed(self, chunk: bytes) -> None:
        # The capture's pump thread. Drained every `_WATCH_SECONDS` by `poll`,
        # so what is held here is a quarter second of output at most.
        with self._fed_lock:
            if self._fed is not None:
                self._fed.append(chunk)

    def _read_new(self) -> bytes:
        if self._fed is not None:
            with self._fed_lock:
                chunk = b"".join(self._fed)
                self._fed = []
            return chunk
        try:
            with self._stdout.open("rb") as stream:
                stream.seek(self._offset)
                chunk = stream.read()
        except OSError:
            return b""
        self._offset += len(chunk)
        return chunk

    def _new_events(self) -> list[dict[str, Any]]:
        chunk = self._read_new()
        if not chunk:
            return []
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        events: list[dict[str, Any]] = []
        for raw in lines:
            raw = raw.strip()
            if not raw.startswith(b"{"):
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def write(self) -> None:
        """The channel file, replaced atomically so the worker never reads half."""
        self._dirty = False
        body = json.dumps(
            {
                "session_id": self.session_id,
                "turns": self.turns,
                "readings": self.readings,
                "stopped_for": self.stopped_for,
            }
        )
        tmp = self._channel.with_name(f".{self._channel.name}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
            os.replace(tmp, self._channel)
        except OSError:
            # The channel is advisory: a run whose channel cannot be written
            # behaves exactly as a run on no account, and parks as today.
            pass


def _account_watcher(spec: CliAgentSpec, stdout_path: Path) -> AccountStreamWatcher | None:
    """The watcher, when this run holds a pool account and the CLI can resume."""
    channel = os.environ.get(ACCOUNT_STREAM_ENV, "").strip()
    if not spec.resume_flag or not channel:
        return None
    move = os.environ.get(ACCOUNT_MOVE_ENV, "").strip()
    return AccountStreamWatcher(stdout_path, Path(channel), Path(move) if move else None)


def _run_watched(
    argv: list[str],
    *,
    ctx: RunnerContext,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    limits: Any,
    log: Any,
    log_argv: list[str],
    watcher: AccountStreamWatcher,
) -> ChildResult:
    """`run_child`, with the stream read as it is produced.

    The same `ChildProcess`, caps and deadline as `run_child`; the difference
    is the loop, which asks the watcher after every slice and stops the CLI at
    a turn boundary when it says so -- and stops it on a SIGTERM to this
    runner, which `run_child` leaves to the worker's SIGKILL.
    """
    child = ChildProcess(
        argv,
        cwd=cwd,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        max_stdout_bytes=limits.max_stdout_bytes,
        max_stderr_bytes=limits.max_stderr_bytes,
        logger=log,
        keep_tail=True,
        log_argv=log_argv,
        stdout_tap=watcher.tap(),
    )
    child.start()
    deadline = time.monotonic() + limits.timeout_seconds
    while True:
        exited = child.wait(_WATCH_SECONDS) is not None
        stop = watcher.poll()
        if exited:
            break
        if stop is not None:
            log.info(
                "stopping the agent at a turn boundary to move it to another account",
                reason=stop,
                turns=watcher.turns,
            )
            child.terminate(limits.grace_seconds, reason=f"account swap ({stop})")
            break
        if ctx.stop_requested:
            child.terminate(limits.grace_seconds, reason="runner asked to stop")
            break
        if time.monotonic() >= deadline:
            child.mark_timed_out()
            child.terminate(limits.grace_seconds, reason="timeout")
            break
    result = child.finish()
    watcher.poll()
    watcher.write()
    return result


def cli_stream_files(spec: CliAgentSpec) -> AgentStreamFiles:
    """The files this runner captures the agent CLI into, under `artifacts/`.

    The ONE place their names are built; `streams.agent_stream_files` calls it
    for the worker, and `run_cli_agent` below writes to exactly these paths.
    """
    return AgentStreamFiles(
        stdout=f"{spec.name}.stdout.log",
        stderr=f"{spec.name}.stderr.log",
        transcript=spec.transcript_name,
    )


#: Claude subscription tokens from `claude setup-token` carry this prefix.
#: Metered API keys are `sk-ant-api...`, so the two are distinguishable by value.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat"


def _credential_env(spec: CliAgentSpec) -> str | None:
    """Which credential variable to hand the child, chosen by the VALUE's shape.

    A tenant has ONE secret per provider -- `swarm-tenant-<id>-anthropic` -- and
    the Cloud Run Job projects it into every variable the profile declares. So
    both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN arrive holding the SAME
    string, and picking by name order would put a subscription token into the
    API-key variable, where Claude Code would reject it.

    The value itself says which it is: `claude setup-token` mints
    `sk-ant-oat...`, while metered keys are `sk-ant-api...`. So the shape picks
    the variable, and only that one is passed to the child -- the other is
    dropped rather than handed over holding a credential of the wrong kind.
    """
    present = [(name, os.environ.get(name, "")) for name in (spec.key_env, *spec.alt_key_envs)]
    present = [(name, value) for name, value in present if value]
    if not present:
        return None

    oauth_names = [n for n in spec.alt_key_envs if "OAUTH" in n.upper()]
    looks_oauth = any(v.startswith(_OAUTH_TOKEN_PREFIX) for _, v in present)
    if looks_oauth and oauth_names:
        return oauth_names[0]
    return present[0][0]


def _argv_prefix(spec: CliAgentSpec) -> list[str]:
    raw = os.environ.get(spec.args_env, "").strip()
    if not raw:
        return list(spec.args_default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunnerFailure(f"{spec.args_env} must be a JSON list of strings: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise RunnerFailure(f"{spec.args_env} must be a JSON list of strings")
    return parsed


def _find_binary(spec: CliAgentSpec) -> str:
    candidate = os.environ.get(spec.binary_env, "").strip() or spec.binary_default
    resolved = candidate if os.path.isabs(candidate) else shutil.which(candidate, path=BASE_PATH)
    if not resolved or not os.path.exists(resolved):
        raise RunnerFailure(
            f"{spec.name}: {candidate!r} is not installed in this image; "
            f"set {spec.binary_env} or rebuild agent-runtime-base"
        )
    return resolved


def agent_working_directory(ctx: RunnerContext) -> Path:
    """Where the agent CLI starts: the checkout when the task has one, else `work/`.

    Owner decision of 2026-09-26 (#226): a SwarmCloud step behaves like a local
    lane wherever the difference is a choice. Locally Claude Code starts in the
    repository and loads its `CLAUDE.md` by itself; here it started in `work/`,
    with the checkout at `./repo`, and read `CLAUDE.md` only when a prompt told
    it to. So with a repository attached it starts in `work/repo`.

    `ctx.repo_dir` comes from `SWARM_REPO_DIR`, which the worker sets after the
    clone and a caller cannot. A checkout the worker named that is not there,
    or not inside `work/`, FAILS the runner: starting the agent in `work/`
    instead would run it without the code and without its instructions, and it
    would still report success.
    """
    if ctx.repo_dir is None:
        return ctx.work_dir
    repo = Path(ctx.repo_dir)
    try:
        inside = repo.resolve().is_relative_to(Path(ctx.work_dir).resolve())
    except OSError:
        inside = False
    if not inside or not repo.is_dir():
        raise RunnerFailure(
            f"the repository checkout {str(repo)!r} is not a directory inside the "
            "work directory; the agent is not started outside it, where it would "
            "have neither the code nor the repository's CLAUDE.md"
        )
    return repo


def detect_rate_limit(text: str) -> tuple[bool, int | None, str | None]:
    """Look for a provider rate limit in CLI output.

    Returns (hit, retry_after_seconds, reset_at). Heuristic by necessity: a CLI
    reports a 429 as prose or as JSON depending on the release, and treating an
    unrecognised rate limit as an ordinary failure would burn one of the task's
    three attempts on something that is not the task's fault.
    """
    lowered = text.lower()
    hit = any(marker in lowered for marker in _RATE_LIMIT_MARKERS)
    if not hit:
        return False, None, None
    retry_after: int | None = None
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match:
            value = int(match.group(1))
            if "minute" in pattern.pattern:
                value *= 60
            retry_after = value if retry_after is None else min(retry_after, value)
            break
    reset_match = _RESET_AT_PATTERN.search(text)
    return True, retry_after, (reset_match.group(1) if reset_match else None)


def detect_credential_failure(text: str) -> tuple[bool, str | None]:
    """Look for a refused credential in CLI output. Returns (hit, marker).

    Checked only AFTER `detect_rate_limit` has said no. A 429 body sometimes
    mentions authentication in passing, and mistaking a rate limit for a dead
    credential would reload a perfectly good secret and restart immediately
    into the same 429 -- turning a wait into a hot loop.
    """
    lowered = text.lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            return True, marker
    return False, None


def _tail(path: Path, limit: int = 8000) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-limit:]


# ---------------------------------------------------------------------------
# Pending work: an answer that ends the session before the work is finished
# ---------------------------------------------------------------------------
#
# Owner decision 2026-10-05 (lane review W1+G5). In 3 of 17 implement steps the
# agent backgrounded its test run and ended its turn with "the suite is still
# running, I'll report". In `--print` mode that answer ENDS the session: nobody
# is there to be reported to, the CLI exits 0, and the worker commits a tree
# whose tests never finished. The runner refuses background calls up front
# (claude_code's generated settings); this is the net under that: an answer
# that announces pending work, or a run that left a background shell open, is
# resumed ONCE with `FINISH_PROMPT`, inside what is left of the step's budget.
# Once: an agent that still announces pending work after being told to finish
# is not going to be talked out of it by a third start, and every resume costs
# a model turn.

#: The one user message a session resumed to finish is given.
FINISH_PROMPT = "Finish: wait for every command you started, report its result, then end."

#: Below this much of the step's budget a finish pass is not started: one model
#: turn that waits for a test run and reports needs about a minute, and a pass
#: killed at the deadline turns a success that announced pending work into a
#: timeout, which is worse for everyone reading the result.
FINISH_MIN_SECONDS = 60.0

#: Phrases that announce work still in flight, each in the shape a pending
#: announcement takes rather than as a bare word, so a finished report that
#: merely uses the words ("the suite was still running when I first checked;
#: it has since passed", "nothing is waiting on review") does not fire.
_PENDING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("still running", re.compile(
        r"\b(?:is|are|'s|'re)\s+still\s+running\b|\bstill\s+running\s+in\s+the\s+background\b",
        re.IGNORECASE,
    )),
    ("I'll report", re.compile(
        r"\bI(?:'ll|\s+will)\s+(?:report|post|share|check)\b"
        r"(?=\s*(?:back\b|when\b|once\b|as\s+soon\b|after\b|[.!;,]|$)"
        r"|[^.!?\n]{0,60}\b(?:when|once|as\s+soon\s+as|after)\b)",
        re.IGNORECASE,
    )),
    ("waiting on", re.compile(
        r"\b(?:I'm|I\s+am|still|currently|now)\s+waiting\s+(?:on|for)\b",
        re.IGNORECASE,
    )),
    ("once it finishes", re.compile(
        r"\b(?:once|when|after|as\s+soon\s+as)\s+(?:it|they|that|this|the\s+[\w-]+(?:\s+[\w-]+)?)\s+"
        r"(?:finish(?:es)?|completes?|is\s+done|are\s+done)\b",
        re.IGNORECASE,
    )),
)

#: A finish clause ("once it finishes") is pending only when it is about this
#: agent's own next step; "the runner resumes once it finishes" describes code.
_FIRST_PERSON_FUTURE = re.compile(r"\b(?:I(?:'ll|\s+will)|let\s+me|I'm\s+going\s+to)\b", re.IGNORECASE)

#: What is QUOTED is mentioned, not said: fenced and inline code, and text in
#: double, curly or single quotes. A single quote opens only where no letter
#: precedes it and closes only where none follows, so "I'll" and "it's" are
#: never taken for quotes; an apostrophe inside a quote is kept when a letter
#: follows it ('I'll report').
_QUOTED = re.compile(
    r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’"
    r"|(?<![\w])'(?:[^'\n]|'(?=\w))*'(?!\w)",
    re.DOTALL,
)

#: The tools that read or stop a background shell. A call to one after a
#: background start means the agent went back for it.
_BACKGROUND_READERS = frozenset({"BashOutput", "KillShell", "KillBash", "TaskOutput", "TaskStop"})


def announces_pending_work(answer: str | None) -> str | None:
    """The pending-work phrase `answer` announces, or None for a finished one."""
    if not isinstance(answer, str) or not answer.strip():
        return None
    said = _QUOTED.sub(" ", answer)
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", said):
        for label, pattern in _PENDING_PATTERNS:
            if not pattern.search(sentence):
                continue
            if label == "once it finishes" and not _FIRST_PERSON_FUTURE.search(sentence):
                continue
            return label
    return None


def _content_blocks(event: Any) -> list[dict[str, Any]]:
    message = event.get("message") if isinstance(event, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content or [] if isinstance(block, dict)] if isinstance(
        content, list
    ) else []


def open_background_shells(parsed: Any) -> int:
    """How many background shells the streamed run started and never went back to.

    A `run_in_background` tool call whose result was not an error started a
    shell; a later call to one of `_BACKGROUND_READERS` collects every shell
    started before it. A refused call (the hook's exit 2 comes back as an
    error result) started nothing. Heuristic by necessity -- the stream does
    not say which shell a reader read -- and erring towards "collected", since
    the answer's own words are the other half of the test.
    """
    if not isinstance(parsed, list):
        return 0
    started: dict[str, bool] = {}
    for event in parsed:
        for block in _content_blocks(event):
            kind = block.get("type")
            if kind == "tool_use":
                tool_input = block.get("input")
                if block.get("name") in _BACKGROUND_READERS:
                    started.clear()
                elif isinstance(tool_input, dict) and tool_input.get("run_in_background") is True:
                    started[str(block.get("id"))] = False
            elif kind == "tool_result":
                tool_id = str(block.get("tool_use_id"))
                if tool_id in started:
                    if block.get("is_error"):
                        del started[tool_id]
                    else:
                        started[tool_id] = True
    return sum(1 for ran in started.values() if ran)


def _final_answer(parsed: Any) -> str | None:
    """The `result` text of the run's last result event, or None."""
    events = [parsed] if isinstance(parsed, dict) else list(reversed(parsed or []))
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("result"), str):
            return event["result"]
    return None


def pending_work(parsed: Any) -> str | None:
    """Why this run ended before its work did, or None when it did not."""
    phrase = announces_pending_work(_final_answer(parsed))
    if phrase is not None:
        return f"the answer announces pending work ({phrase!r})"
    shells = open_background_shells(parsed)
    if shells:
        return f"{shells} background shell(s) left open"
    return None


def _session_of(parsed: Any) -> str | None:
    """The last well-formed session id the stream carried, or None."""
    events = [parsed] if isinstance(parsed, dict) else list(reversed(parsed or []))
    for event in events:
        sid = event.get("session_id") if isinstance(event, dict) else None
        if isinstance(sid, str) and _SESSION_ID.match(sid):
            return sid
    return None


# ---------------------------------------------------------------------------
# Repair turns: the checks the worker would fail, shown while they can be fixed
# ---------------------------------------------------------------------------
#
# Issue #624, owner decision 2026-10-05 (history I1). About $201, 14.9% of all
# spend, went to agents that finished and then failed a check they never saw:
# an expected output not written ($90.13 over 19 retried attempts), a
# credential-shaped line in the final tree ($64.29 over 23), and 8 whole lanes
# lost at the fix step ($96.28). The worker runs both checks only after this
# process has exited (`lifecycle._finalise`: `_fail_for_missing_outputs`,
# `_fail_for_final_tree_leak`), and then the session is gone.
#
# So after the agent's turn ends -- and after a finish pass, so the checks
# read the tree its last turn left -- the runner runs the same two checks
# against the tree and, when either fails, resumes the session with a prompt
# naming exactly what failed, up to REPAIR_MAX_TURNS times inside what is left
# of the step's budget (FINISH_MIN_SECONDS, the same floor as a finish pass:
# one model turn that writes a file or edits a line and reports). Then the
# attempt ends exactly as before: the worker's own checks still decide, and a
# repair that did not take fails the attempt the way it always has.
#
# ONE IMPLEMENTATION OF EACH CHECK, NOT A COPY. The expected names are the ones
# this runner told the agent (`told`), compared with
# `expected_outputs.missing_outputs`. The credential scan is
# `agent_worker.publish_scan.scan` from `publish_scan.default_base`: the
# worker's own `_DiffLeakScanner` and `_credential_in`, over the diff the
# publish will read (the clone base the worker exports as SWARM_CLONE_BASE
# against the working tree, untracked files included). A task's REGISTERED
# secrets are known only to the worker, so they are not checked here; the
# publish still refuses one.
#
# NEVER THE MATCHED TEXT. A hit is `path:line rule` (`ScanHit` holds no part of
# the value), and that is all the prompt, the log and the result carry.

#: How many repair turns a step may take. Two: a third start rarely succeeds
#: where two named failures did not, and every start costs a model turn.
REPAIR_MAX_TURNS = 2

#: At most this many items of each kind are named in a prompt or recorded per
#: turn. The runner's whole output reaches `result_summary.runner.output`
#: through an 8000-character cap (`lifecycle._truncate_json`), and a tree with
#: hundreds of hits needs the first ones fixed before the rest matter.
REPAIR_LIST_CAP = 25


def written_outputs_missing(names: Sequence[str], artifacts_dir: Path | str) -> list[str]:
    """The expected names not written as regular files in the artifacts directory.

    A link is not counted: the worker refuses it when it uploads (`refused`),
    so it would fail the same check a moment later.
    """
    root = Path(artifacts_dir)
    produced: list[str] = []
    for name in names:
        path = root / name
        try:
            if path.is_file() and not path.is_symlink():
                produced.append(name)
        except OSError:
            continue
    return expected_mod.missing_outputs(names, produced)


def credential_lines(repo: Path) -> tuple[list[str], str | None]:
    """`path:line rule` for every credential-shaped line the publish would refuse.

    Returns the lines and None, or no lines and why the scan could not run --
    which is never read as clean and never as a failure to repair: a repair
    turn needs something to name.
    """
    from .. import publish_scan  # lazy: imports the worker's lifecycle module

    try:
        hits = publish_scan.scan(repo, publish_scan.default_base(repo))
    except publish_scan.ScanError as exc:
        return [], str(exc)
    except OSError as exc:
        return [], f"{type(exc).__name__}: the scan could not run"
    return [f"{hit.path}:{hit.line} {hit.rule}" for hit in hits], None


def repair_prompt(missing: Sequence[str], flagged: Sequence[str], artifacts_dir: Path | str) -> str:
    """The one user message a repair turn is given: what failed, exactly."""
    directory = PurePosixPath(os.path.abspath(os.fspath(artifacts_dir)))
    lines = [
        "Before this step ends, the platform ran the checks it runs after you "
        "exit, and they failed. Fix exactly these, then end with a short report."
    ]
    if missing:
        lines.append(
            "These files later steps need were not written. Write each one, as a "
            "regular file, at exactly this path:"
        )
        lines += [f"- {directory / name}" for name in missing[:REPAIR_LIST_CAP]]
        if len(missing) > REPAIR_LIST_CAP:
            lines.append(f"- and {len(missing) - REPAIR_LIST_CAP} more")
    if flagged:
        lines.append(
            "These added lines look like a credential, and the publish step refuses "
            "a diff that adds one (path:line rule). Remove the value from each line; "
            "a test value is built at runtime from pieces, never written as one literal:"
        )
        lines += [f"- {entry}" for entry in flagged[:REPAIR_LIST_CAP]]
        if len(flagged) > REPAIR_LIST_CAP:
            lines.append(f"- and {len(flagged) - REPAIR_LIST_CAP} more")
    return "\n".join(lines)


def _combined_spend(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """The spend of two invocations of one session: numbers summed, at any depth.

    A resumed invocation's `result` event totals that invocation only, so the
    step's spend is both. A value that is not a number on both sides is the
    later one's.
    """
    out = dict(first)
    for key, value in second.items():
        prior = out.get(key)
        if isinstance(prior, dict) and isinstance(value, dict):
            out[key] = _combined_spend(prior, value)
        elif (
            isinstance(prior, (int, float)) and isinstance(value, (int, float))
            and not isinstance(prior, bool) and not isinstance(value, bool)
        ):
            out[key] = prior + value
        else:
            out[key] = value
    return out


def run_cli_agent(
    ctx: RunnerContext,
    spec: CliAgentSpec,
    *,
    extra_args: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload = ctx.payload
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RunnerFailure(f"{spec.name} requires a non-empty string input.prompt")
    credential_env = _credential_env(spec)
    if credential_env is None:
        accepted = " or ".join((spec.key_env, *spec.alt_key_envs))
        raise RunnerFailure(
            f"{spec.name} requires {accepted}; the tenant has no {spec.provider} "
            "credential mounted for this attempt"
        )

    binary = _find_binary(spec)
    argv: list[str] = [binary, *_argv_prefix(spec), *extra_args]
    # THE MODEL IS THE JOB'S, NEVER THE CALLER'S (#226, owner decision of
    # 2026-09-26). `MODEL` is set on the profile's Cloud Run Job by Terraform
    # (`local.runner_models` in terraform/infra/locals.tf; `MODEL` on a Job the
    # scheduler creates comes from the same value) and the worker hands its
    # `config.model` to this process under the same name
    # (`lifecycle._build_child_env`). This used to read `input.model` first,
    # so a caller of the API or the console chose the model an agent ran -- an
    # execution parameter from a caller, which invariant 10 forbids. The API
    # now refuses `input.model` (claude-code and codex declare only `issue`,
    # #213 and #265) and the worker drops a stored one; it is not read here
    # either way.
    model = os.environ.get("MODEL", "").strip() or None
    if model and spec.model_flag:
        if not re.fullmatch(r"[A-Za-z0-9._:\-]{1,128}", model):
            raise RunnerFailure(f"MODEL {model!r} contains unsupported characters")
        argv += [spec.model_flag, model]
    # WHERE THE AGENT STARTS (#226). In the checkout when the task has one, so
    # Claude Code loads the repository's own CLAUDE.md (and codex its
    # AGENTS.md) the way a local lane does; in `work/` otherwise, as before.
    cwd = agent_working_directory(ctx)
    # WHERE DELIVERABLES GO, AND WHAT LATER STEPS NEED FROM THIS ONE. Every
    # prompt ends with one line naming $SWARM_ARTIFACTS_DIR (#184, owner
    # decision of 2026-09-26: `expected_mod.deliverables_line`). When a
    # dependant's `input_from` stages files from this task, the worker has put
    # their names in input.json, and they follow that line (#149). Both go into
    # the PROMPT because the prompt is the one instruction channel every CLI
    # runner shares. A system-prompt flag would have to exist in whichever CLI
    # release the image carries, and `*_ARGS` can replace the whole flag set.
    # This is the ONE call that builds them, for claude-code and codex alike.
    # See agent_worker/expected_outputs.py for what is measured and why.
    expected = expected_mod.parse_names(payload.get(expected_mod.METADATA_KEY))
    # THIS RUNNER'S OWN FILES ARE LEFT OUT. A dependant may stage the upstream
    # runner's log or transcript, and the API records that name like any
    # other, but this process writes those three files itself -- the logs
    # while the agent is running. An agent told to write one would be writing
    # into a file this runner holds open, or one it overwrites afterwards.
    files = cli_stream_files(spec)
    own_files = files.names()
    told = expected_mod.without_platform_names(expected.names, own_files)
    # STAGED INPUTS BY ABSOLUTE PATH, ONLY WHEN THE AGENT STARTS IN THE
    # CHECKOUT (#226). They land in `work/`, which is then the checkout's
    # parent, so a prompt's "read scan-01.md" resolves inside the repository
    # and finds nothing. A task with no repository starts in `work/`, where the
    # relative name works, and the owner kept its prompt unchanged.
    staged = (
        expected_mod.staged_paths(payload.get("staged_inputs"), ctx.work_dir)
        if cwd != ctx.work_dir
        else ()
    )
    # THE ISSUE THIS STEP WAS POINTED AT (#265, contract request 28). The
    # worker fetched it into `work/issue.md` before starting this process, or
    # failed the attempt; its line follows the caller's prompt and comes
    # before the platform's instructions, which still end the prompt. A task
    # that asks for an issue whose file is not there is refused here rather
    # than started with a prompt about an issue it cannot read.
    if payload.get("issue") is not None:
        issue_file = issue_mod.issue_path(ctx.work_dir)
        if issue_file.is_symlink() or not issue_file.is_file():
            raise RunnerFailure(
                f"input.issue is set but {issue_mod.FILE_NAME} is not in the work "
                "directory; the agent is not started without the issue it was pointed at"
            )
        prompt = f"{prompt}\n\n{issue_mod.prompt_line(issue_file)}"
    # CHILD TASKS (docs/design/child-tasks.md). The worker sets SWARM_CHILDREN
    # only for an attempt with a child path, and writes the guide into it; the
    # prompt names where the guide is, and the guide says the rest. No
    # variable, no line: a child, or an attempt without the path, reads nothing
    # about a feature it cannot use.
    spool = os.environ.get("SWARM_CHILDREN", "").strip()
    if spool:
        from .. import children as children_mod  # lazy: the runner rarely needs it

        prompt = f"{prompt}\n\n{children_mod.prompt_line(spool)}"
    # A RESUMED SESSION (S13/S14). The worker moved this attempt to another
    # account at a turn boundary and restarted this runner to continue the
    # session it stopped: `--resume <id>` and one short user message, in the
    # same workspace. The original prompt is already in the session.
    resume = os.environ.get(RESUME_SESSION_ENV, "").strip()
    # The argv every start of this step shares; a finish pass (below) adds its
    # own `--resume <id>` and prompt to it.
    base_argv = list(argv)
    if resume and not (spec.resume_flag and _SESSION_ID.match(resume)):
        raise RunnerFailure(f"{spec.name} was asked to resume a session it cannot resume")
    if resume:
        argv += [spec.resume_flag, resume]
        argv.append(RESUME_PROMPT)
    else:
        # The prompt is the only caller-controlled value that reaches argv,
        # and it is passed as a single trailing argument with no shell.
        argv.append(expected_mod.with_instructions(prompt, told, ctx.artifacts_dir, staged))
    # ...and it is the one argument `run_child`'s `child started` line must not
    # print (the PR #229 review): this process's stderr is served by `/logs`,
    # and the task routes serve the prompt masked. Its length says what the
    # line needs to say -- that a prompt was passed, and how big. A resumed
    # session's id is masked the same way: it never reaches a log line.
    log_argv = [*argv[:-1], f"<prompt: {len(argv[-1])} characters>"]
    if resume:
        log_argv = [("<session>" if arg == resume else arg) for arg in log_argv]

    limits = resolve_limits(payload, platform_ceilings())
    log = StructuredLogger(stream=sys.stderr, component=f"{spec.name}-runner")
    log.info(
        "the agent starts in the repository checkout"
        if cwd != ctx.work_dir
        else "the agent starts in the work directory; the task has no repository",
        cwd=str(cwd),
        home=str(ctx.work_dir),
        model=model,
    )
    if told:
        log.info(
            "told the agent which files later steps need and where to write them",
            expected_outputs=list(told),
            artifacts_dir=os.path.abspath(ctx.artifacts_dir),
        )
    if staged:
        log.info(
            "named the staged inputs in the prompt by absolute path, because the "
            "agent starts in the checkout and they are in the work directory",
            staged_inputs=list(staged),
        )
    if len(told) != len(expected.names):
        log.info(
            "left this runner's own files out of the agent's instructions; the "
            "runner writes them itself",
            left_out=[name for name in expected.names if name not in told],
        )
    if expected.rejected:
        log.warning(
            "left unusable expected_outputs entries out of the agent's instructions: "
            + ", ".join(repr(entry) for entry in expected.rejected)
        )
    # This process's own stderr is captured by the worker and uploaded, and
    # `run_child` logs the argv it starts. Register the key here as well as in
    # the worker: a runner is a separate process and does not inherit the
    # worker logger's registered set.
    log.register_secret(os.environ.get(credential_env))
    for passthrough in _SENSITIVE_PASSTHROUGH:
        log.register_secret(os.environ.get(passthrough))
    if resume:
        log.register_secret(resume)
    if limits.clamped:
        log.warning(
            "requested limits exceed the platform ceiling and were clamped",
            clamped=list(limits.clamped),
            effective=limits.as_dict(),
        )
    stdout_path = ctx.artifacts_dir / files.stdout
    stderr_path = ctx.artifacts_dir / files.stderr

    env = {
        "PATH": BASE_PATH,
        # HOME IS THE ATTEMPT'S OWN `work/`, NEVER THE CHECKOUT, even when the
        # agent starts in the checkout (#226, owner decision of 2026-09-26).
        # The CLI writes its own state under HOME (`.claude.json`, `.claude/`,
        # `.codex/`), some of it describing the account it ran as; in the
        # checkout that would be in the harvest's patch and the pushed branch.
        "HOME": str(ctx.work_dir),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "CI": "1",
        "NO_COLOR": "1",
        # WHERE TO PUT ITS WORK. runners/base.py documents SWARM_ARTIFACTS_DIR
        # as the contract -- "files written here are uploaded when the attempt
        # ends" -- and runners/generic.py exports it. This runner did not, so a
        # claude-code agent was never told where its output should go.
        #
        # Measured on the first real multi-agent workflow
        # (wf_bcdc9180e4fb4a209f31, step `research`): the agent replied "The
        # environment variable SWARM_ARTIFACTS_DIR is not set in this
        # environment, so I can't determine the target directory" and exited 0
        # having written nothing. The attempt still SUCCEEDED, because writing
        # an artifact is not a success condition.
        #
        # The consequence was the platform's headline feature: this runner uses
        # ctx.artifacts_dir for its OWN stdout/stderr (above), so
        # result_summary.artifacts always contained exactly the runner's logs
        # and the transcript and never anything the agent produced. With no
        # agent artifact there is nothing for a downstream step's `input_from`
        # to stage, so work could not be routed between agents at all -- while
        # `input_from` itself was correct and tested, against the generic
        # runner.
        "SWARM_ARTIFACTS_DIR": str(ctx.artifacts_dir),
        "SWARM_WORK_DIR": str(ctx.work_dir),
        credential_env: os.environ[credential_env],
    }
    # The runner's own switches for the CLI (claude_code: background tasks
    # off). Platform constants from the runner module, never caller input;
    # they cannot replace the credential or the paths above.
    for name, value in (extra_env or {}).items():
        if name not in env:
            env[name] = value
    for passthrough in (*_SENSITIVE_PASSTHROUGH, *_PLAIN_PASSTHROUGH):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]

    # `env` is the set of values this process was given, so it is exactly the
    # set of secrets it could have leaked.
    secrets = collect_secrets(
        env.get(name) for name in (spec.key_env, *spec.alt_key_envs, *_SENSITIVE_PASSTHROUGH)
    )

    def start(
        run_argv: list[str], run_log_argv: list[str], timeout_seconds: float
    ) -> tuple[ChildResult, AccountStreamWatcher | None]:
        """One start of the CLI, its captures redacted before anything reads them."""
        run_limits = replace(limits, timeout_seconds=timeout_seconds)
        run_watcher = _account_watcher(spec, stdout_path)
        if run_watcher is None:
            run_result = run_child(
                run_argv,
                cwd=cwd,
                env=env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout_seconds=run_limits.timeout_seconds,
                grace_seconds=run_limits.grace_seconds,
                max_stdout_bytes=run_limits.max_stdout_bytes,
                max_stderr_bytes=run_limits.max_stderr_bytes,
                logger=log,
                log_argv=run_log_argv,
                # THE END OF A CAPPED STREAM IS KEPT (#188 review). Under
                # stream-json the stdout is the whole conversation, and its
                # LAST line is the `result` event: the answer, the spend, the
                # evidence the rate-limit decision below reads. A capture that
                # kept only the first `max_stdout_bytes` lost exactly that line
                # on every long session.
                keep_tail=True,
            )
        else:
            # On a pool account: the same child, the stream read as it is
            # written (see `AccountStreamWatcher`), so the readings reach the
            # worker while the agent runs and a swap can happen at a turn
            # boundary.
            run_result = _run_watched(
                run_argv,
                ctx=ctx,
                cwd=cwd,
                env=env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                limits=run_limits,
                log=log,
                log_argv=run_log_argv,
                watcher=run_watcher,
            )
        # Redact before anything is read back out. Everything below this
        # either becomes an artifact in GCS or a field in Firestore, and both
        # outlive the pod.
        for captured in (stdout_path, stderr_path):
            scrub_file(captured, secrets)
        return run_result, run_watcher

    def judge(
        run_result: ChildResult,
        run_watcher: AccountStreamWatcher | None,
        spend: dict[str, Any],
        combined: str,
    ) -> None:
        """Raise the signal or failure one start of the CLI ended in, if any."""
        if (
            run_watcher is not None
            and run_watcher.stopped_for is not None
            and not run_result.timed_out
        ):
            # STOPPED HERE, AT A TURN BOUNDARY, FOR A SWAP. Not a failure of the
            # task: the worker reads the channel, moves the hold and resumes the
            # session. If it cannot -- no other account -- it checkpoints and
            # parks exactly as for a rate limit, which this is the cheap form of.
            if run_watcher.stopped_for == STOP_EXHAUSTED:
                raise QuotaExhaustedSignal(
                    provider=spec.provider,
                    detail=f"{spec.name} stopped at a turn boundary: the account's quota is spent",
                    spend=spend,
                )
            raise RunnerFailure(
                f"{spec.name} stopped at a turn boundary to move to another account "
                f"({run_watcher.stopped_for})",
                spend=spend,
            )

        if run_result.exit_code != 0 or run_result.timed_out:
            hit, retry_after, reset_at = detect_rate_limit(combined)
            if hit:
                raise QuotaExhaustedSignal(
                    provider=spec.provider,
                    retry_after_seconds=retry_after,
                    reset_at=reset_at,
                    detail=f"{spec.name} reported a provider rate limit",
                    spend=spend,
                )
            # Checked only after the rate-limit test has said no: a 429 body
            # sometimes mentions authentication in passing, and reading that as a
            # dead credential would reload a perfectly good secret and restart
            # straight back into the same 429 -- turning a wait into a hot loop.
            refused, marker = detect_credential_failure(combined)
            if refused:
                raise CredentialRevokedSignal(
                    provider=spec.provider,
                    detail=f"{spec.name} was refused its credential",
                    marker=marker or "",
                    spend=spend,
                )
        if run_result.timed_out:
            raise RunnerFailure(
                f"{spec.name} timed out after {limits.timeout_seconds:.0f}s", spend=spend
            )
        if run_result.exit_code != 0:
            raise RunnerFailure(
                f"{spec.name} exited {run_result.exit_code}: {_tail(stderr_path, 2000).strip()}",
                spend=spend,
            )

    def detection_text(raw: str, parsed_run: Any) -> str:
        # What the rate-limit and credential heuristics read. For a streamed
        # run this is NOT the raw stdout tail -- see `_detection_text`. Neither
        # half includes the capture's own notices, which count bytes: `429` is
        # a marker.
        return (
            _detection_text(raw, parsed_run)
            + "\n"
            + _without_capture_notices(_tail(stderr_path))
        )

    result, watcher = start(argv, log_argv, limits.timeout_seconds)
    # Reported on every outcome from here on -- `write_result` carries it --
    # so a run that failed or parked after passing its cap still says so.
    capture = result.capture_report()
    ctx.report.update(capture)
    if result.stdout_truncated or result.stderr_truncated:
        log.warning(
            "the agent's output passed its size cap: the start and the end of each "
            "capped stream were kept and the middle dropped, with a notice where",
            **capture,
            max_stdout_bytes=limits.max_stdout_bytes,
            max_stderr_bytes=limits.max_stderr_bytes,
        )

    # PARSED BEFORE THE EXIT CODE IS JUDGED, not after. A CLI that fails still
    # prints its result object -- `claude --output-format json` reports
    # `is_error: true` WITH `usage` and `total_cost_usd` -- and every raise
    # below used to happen first, so the numbers for a run that hit a 429 an
    # hour in, or failed after doing real work, were discarded here and the
    # attempt read "not reported" in every cost figure. `spend` rides out on
    # the signal instead, and `run_runner` writes it into result.json.
    raw_stdout = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
    parsed = _parse_cli_output(raw_stdout)
    spend = _scrub_json(_spend_of(parsed), secrets)
    combined = detection_text(raw_stdout, parsed)
    judge(result, watcher, spend, combined)

    # RESUMED ONCE TO FINISH (owner decision 2026-10-05; see `pending_work`).
    # The run succeeded, but its answer announces work still in flight -- or
    # it left a background shell open -- and in print mode nobody will hear
    # the report it promised. Continue the same session once, told to wait
    # and report, inside what is left of the step's budget, never more.
    resumed_to_finish = False
    finish_skipped: str | None = None
    duration_seconds = result.duration_seconds
    stdout_bytes, stderr_bytes = result.stdout_bytes, result.stderr_bytes

    def resume_pass(session: str, pass_prompt: str, pass_log_prompt: str, remaining: float) -> Any:
        """Continue `session` once with `pass_prompt`; return that pass's own parsed output.

        Shared by the finish pass and the repair turns. The step's record is
        kept whole: the captures, the spend, the durations and what the
        transcript and summary read cover every start.
        """
        nonlocal result, watcher, capture, raw_stdout, parsed, spend, combined
        nonlocal duration_seconds, stdout_bytes, stderr_bytes
        first_stdout = stdout_path.read_bytes() if stdout_path.exists() else b""
        first_stderr = stderr_path.read_bytes() if stderr_path.exists() else b""
        first_spend = spend
        pass_argv = [*base_argv, str(spec.resume_flag), session, pass_prompt]
        pass_log_argv = [
            ("<session>" if arg == session else arg) for arg in pass_argv[:-1]
        ] + [pass_log_prompt]
        result, watcher = start(pass_argv, pass_log_argv, remaining)
        # ONE RECORD OF THE STEP. Each start truncates the captures, so the
        # earlier starts' are put back in front of this one's: the stdout log
        # stays the whole conversation, in order.
        pass_stdout = stdout_path.read_bytes() if stdout_path.exists() else b""
        stdout_path.write_bytes(first_stdout + pass_stdout)
        stderr_path.write_bytes(
            first_stderr + (stderr_path.read_bytes() if stderr_path.exists() else b"")
        )
        second = result.capture_report()
        capture = {
            "stdout_truncated": capture["stdout_truncated"] or second["stdout_truncated"],
            "stderr_truncated": capture["stderr_truncated"] or second["stderr_truncated"],
            "stdout_dropped_bytes": capture["stdout_dropped_bytes"]
            + second["stdout_dropped_bytes"],
            "stderr_dropped_bytes": capture["stderr_dropped_bytes"]
            + second["stderr_dropped_bytes"],
        }
        ctx.report.update(capture)
        # This pass's own output is what its outcome is judged on; the whole
        # conversation is what the transcript and summary read.
        pass_raw = pass_stdout.decode("utf-8", errors="replace")
        pass_parsed = _parse_cli_output(pass_raw)
        raw_stdout = stdout_path.read_text(errors="replace")
        parsed = _parse_cli_output(raw_stdout)
        spend = _combined_spend(first_spend, _scrub_json(_spend_of(pass_parsed), secrets))
        combined = detection_text(pass_raw, pass_parsed)
        judge(result, watcher, spend, combined)
        duration_seconds += result.duration_seconds
        stdout_bytes += result.stdout_bytes
        stderr_bytes += result.stderr_bytes
        return pass_parsed

    pending = pending_work(parsed) if spec.finish_on_pending and spec.resume_flag else None
    if pending is not None:
        remaining = limits.timeout_seconds - result.duration_seconds
        session = _session_of(parsed)
        if session is None:
            finish_skipped = "no_session"
            log.warning(
                "the agent ended before its work did, and the stream carried no "
                "session to resume; the result stands as it is",
                why=pending,
            )
        elif remaining < FINISH_MIN_SECONDS:
            finish_skipped = "budget"
            log.warning(
                "the agent ended before its work did, and too little of the step's "
                "budget is left to resume it; the result stands as it is",
                why=pending,
                remaining_seconds=round(remaining, 1),
                minimum_seconds=FINISH_MIN_SECONDS,
            )
        else:
            log.register_secret(session)
            log.warning(
                "the agent ended before its work did; resuming the session once to finish",
                why=pending,
                remaining_seconds=round(remaining, 1),
            )
            # Said before the pass starts, so a pass that fails or parks
            # still reports that it was resumed (`write_result` merges it).
            resumed_to_finish = True
            ctx.report["resumed_to_finish"] = True
            finish_parsed = resume_pass(session, FINISH_PROMPT, FINISH_PROMPT, remaining)
            still = pending_work(finish_parsed)
            if still is not None:
                log.warning(
                    "the resumed session still ended before its work did; it is not "
                    "resumed again",
                    why=still,
                )

    # REPAIR TURNS (#624; see `REPAIR_MAX_TURNS`). The run, and any finish
    # pass, succeeded; now the checks the worker runs after this process exits
    # are run while the session can still be continued.
    repair_turns = 0
    repairs: list[dict[str, Any]] = []
    repair_skipped: str | None = None
    repair_scan_error: str | None = None
    unresolved: dict[str, list[str]] | None = None
    if spec.repair_checks and spec.resume_flag:
        # The scan reads the diff the publish reads, so only a task with a
        # checkout has one to scan.
        scan_repo = cwd if cwd != ctx.work_dir else None

        def run_checks() -> tuple[list[str], list[str]]:
            nonlocal repair_scan_error
            missing_now = written_outputs_missing(told, ctx.artifacts_dir)
            flagged_now: list[str] = []
            if scan_repo is not None:
                flagged_now, repair_scan_error = credential_lines(scan_repo)
            return missing_now, flagged_now

        ctx.report["repair_turns"] = 0
        missing, flagged = run_checks()
        if repair_scan_error is not None:
            log.warning(
                "the publish credential scan could not run before the step ended; "
                "the publish runs it again",
                error=repair_scan_error,
            )
        while missing or flagged:
            if repair_turns >= REPAIR_MAX_TURNS:
                log.warning(
                    f"the repair turns did not clear every check; after {REPAIR_MAX_TURNS} "
                    "the attempt ends as it is and the worker's own checks decide",
                    missing_outputs=missing[:REPAIR_LIST_CAP],
                    credential_lines=flagged[:REPAIR_LIST_CAP],
                )
                break
            session = _session_of(parsed)
            remaining = limits.timeout_seconds - duration_seconds
            if session is None:
                repair_skipped = "no_session"
                log.warning(
                    "a check the worker runs would fail, and the stream carried no "
                    "session to resume; the result stands as it is",
                    missing_outputs=missing[:REPAIR_LIST_CAP],
                    credential_lines=flagged[:REPAIR_LIST_CAP],
                )
                break
            if remaining < FINISH_MIN_SECONDS:
                repair_skipped = "budget"
                log.warning(
                    "a check the worker runs would fail, and too little of the step's "
                    "budget is left to resume it; the result stands as it is",
                    missing_outputs=missing[:REPAIR_LIST_CAP],
                    credential_lines=flagged[:REPAIR_LIST_CAP],
                    remaining_seconds=round(remaining, 1),
                    minimum_seconds=FINISH_MIN_SECONDS,
                )
                break
            repair_turns += 1
            ctx.report["repair_turns"] = repair_turns
            log.register_secret(session)
            log.warning(
                f"a check the worker runs would fail; repair turn {repair_turns} of "
                f"{REPAIR_MAX_TURNS}, resuming the session with what failed",
                missing_outputs=missing[:REPAIR_LIST_CAP],
                credential_lines=flagged[:REPAIR_LIST_CAP],
                remaining_seconds=round(remaining, 1),
            )
            text = repair_prompt(missing, flagged, ctx.artifacts_dir)
            resume_pass(session, text, f"<prompt: {len(text)} characters>", remaining)
            after_missing, after_flagged = run_checks()
            # What a turn fixed is what it was asked to fix and no longer fails.
            # A credential line is named by `path:line rule`, so one that only
            # moved when an earlier line was removed reads as fixed here and as
            # newly flagged on the next check.
            repairs.append({
                "turn": repair_turns,
                "missing_outputs": missing[:REPAIR_LIST_CAP],
                "credential_lines": flagged[:REPAIR_LIST_CAP],
                "fixed_outputs": [n for n in missing if n not in after_missing][:REPAIR_LIST_CAP],
                "fixed_lines": [e for e in flagged if e not in after_flagged][:REPAIR_LIST_CAP],
            })
            ctx.report["repairs"] = repairs
            missing, flagged = after_missing, after_flagged
        if missing or flagged:
            unresolved = {
                "missing_outputs": missing[:REPAIR_LIST_CAP],
                "credential_lines": flagged[:REPAIR_LIST_CAP],
            }

    # WHOLE OR NOT AT ALL -- see TRANSCRIPT_MAX_CHARS. The stdout capture above
    # is the canonical transcript and is uploaded either way.
    transcript_skipped: str | None = None
    if capture["stdout_truncated"]:
        # A transcript re-serialised from a stream whose middle was dropped
        # would be valid JSON that says nothing of the gap. The stdout capture
        # carries the notice at the cut; it is the record.
        transcript_skipped = "capture_truncated"
    elif parsed is not None:
        document = json.dumps(parsed, indent=2)
        if len(document) <= TRANSCRIPT_MAX_CHARS:
            ctx.write_artifact(spec.transcript_name, scrub_text(document, secrets))
        else:
            transcript_skipped = "too_large"
            log.warning(
                "the transcript was not written: it is larger than the cap, and a "
                "cut JSON document parses as nothing; the agent's stdout is the "
                "complete record",
                transcript=spec.transcript_name,
                chars=len(document),
                cap_chars=TRANSCRIPT_MAX_CHARS,
            )

    # A rate limit can also appear in a run the CLI still reports as successful.
    hit, retry_after, reset_at = detect_rate_limit(combined)
    if hit and not _looks_complete(parsed):
        raise QuotaExhaustedSignal(
            provider=spec.provider,
            retry_after_seconds=retry_after,
            reset_at=reset_at,
            detail=f"{spec.name} output contains a rate-limit notice without a completed result",
            spend=spend,
        )

    return {
        # `result.json` is read by the worker and stored as `task.result_summary`,
        # which is a Firestore document. Neither the summary nor the structured
        # output may carry the key that produced it.
        "summary": scrub_text(_summarise(parsed, raw_stdout), secrets),
        "provider": spec.provider,
        # The model this runner ASKED for, the Job's MODEL. What the CLI says it
        # actually used is `modelUsage` in `structured_output`, which the worker
        # lifts into `result_summary.runner.usage.models`.
        "model": model if model and spec.model_flag else None,
        "exit_code": result.exit_code,
        # A streamed (one-object-per-line) run parses to a LIST, which this
        # field never carried -- so its numbers were dropped even on success.
        # The spend subset of its final result event stands in for it then.
        "structured_output": (
            _scrub_json(parsed, secrets)
            if isinstance(parsed, dict)
            else (spend or None)
        ),
        "limits": limits.as_dict(),
        # Read by the worker into `result_summary.agent_streams`; null when the
        # transcript was written (or there was no parsed output to write).
        "transcript_skipped": transcript_skipped,
        # Whether the session was resumed once to finish work its answer left
        # in flight, and, when that was called for but not done, why not.
        "resumed_to_finish": resumed_to_finish,
        "finish_skipped": finish_skipped,
        # The repair turns taken after the checks the worker runs failed
        # (#624), what each was asked to fix and what it did; what still fails
        # after them; and, when a repair was called for but not taken, why
        # not. A scan that could not run says so here and is never read as
        # clean. `path:line rule` only, never a matched value.
        "repair_turns": repair_turns,
        "repairs": repairs,
        "repair_unresolved": unresolved,
        "repair_skipped": repair_skipped,
        "repair_scan_error": repair_scan_error,
        # Also in `ctx.report`, which `write_result` merges anyway; stated here
        # so this function's own return value is the whole answer.
        **capture,
        "metrics": {
            "duration_seconds": round(duration_seconds, 3),
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
        },
    }


def _parse_cli_output(raw_stdout: str) -> Any:
    """The CLI's stdout as JSON: one object, or the objects of a streamed run.

    None when neither shape is there -- a CLI that crashed before printing, or
    one whose output is prose.
    """
    try:
        return json.loads(raw_stdout)
    except json.JSONDecodeError:
        # Some CLI versions stream one JSON object per line.
        events = []
        for line in raw_stdout.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events or None


#: Statuses a `rate_limit_event` reports when the request went through. Such an
#: event is a READING of the account's windows, not a refusal, and it is in
#: every streamed run -- so it must never be what parks one.
_RATE_LIMIT_ALLOWED = frozenset({"allowed", "allowed_warning"})


def _not_evidence(event: dict[str, Any]) -> bool:
    """True for a streamed event the rate-limit heuristics must not read.

    * `assistant` and `user` events are the agent's own words and its tools'
      output. An agent that reads a file about HTTP 429s, or runs a test named
      `test_rate_limit`, prints the markers `_RATE_LIMIT_MARKERS` looks for,
      and a FAILED run whose transcript mentioned them would be parked as
      rate-limited instead of failing -- a false park that also skips the
      failure a person needs to see. The exception is an event the CLI marks
      as an API error (`isApiErrorMessage`, or an `error` key): that is the
      provider speaking, not the agent.
    * a `rate_limit_event` whose status says the request was allowed.

    Everything else -- the `result` event, `system` events, any line that is
    not JSON, any event type this function does not know -- is kept, so an
    unrecognised way of reporting a 429 still reaches the heuristics.
    """
    kind = event.get("type")
    if kind in ("assistant", "user"):
        return not (event.get("isApiErrorMessage") or event.get("error"))
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        status = event.get("status")
        if status is None and isinstance(info, dict):
            status = info.get("status")
        return isinstance(status, str) and status in _RATE_LIMIT_ALLOWED
    return False


def _detection_text(raw_stdout: str, parsed: Any) -> str:
    """The part of stdout the rate-limit and credential heuristics may read.

    One JSON object, or output that is not JSON at all: the last 8000
    characters, exactly as before. A STREAMED run (`--output-format
    stream-json`, one event per line, which claude-code runs with since #184):
    every line except the ones `_not_evidence` names, last 8000 characters.
    Under the single-object format the heuristics saw the final result and
    nothing else; this keeps that for a streamed run instead of widening it
    to the whole conversation.

    NEVER THE CAPTURE'S OWN NOTICE (#188 review). Where the output cap cut a
    stream, `procman.StreamCapture` writes a line counting the bytes it
    dropped -- and `429` is a marker matched anywhere, so 4,290,117 dropped
    bytes would park a failed run as rate-limited. Nor, in a streamed run,
    the line the cut went through, just above the notice: half an event is
    the agent's words or a tool's output as often as the provider's, and
    cannot be told apart.
    """
    if not isinstance(parsed, list):
        return _without_capture_notices(raw_stdout[-8000:])
    kept: list[str] = []
    previous_unparsed = False
    for line in raw_stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_capture_notice(stripped):
            if previous_unparsed and kept:
                kept.pop()
            previous_unparsed = False
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)
            previous_unparsed = True
            continue
        previous_unparsed = False
        if isinstance(event, dict) and _not_evidence(event):
            continue
        kept.append(json.dumps(_without_identifiers(event)) if isinstance(event, dict) else stripped)
    return "\n".join(kept)[-8000:]


#: Keys whose values are random identifiers. `429` is a marker matched
#: anywhere, and a uuid contains it about one time in 115 -- so a FAILED run
#: whose `init` or `result` event carried such a `session_id` was parked as
#: rate-limited, and on a pool account moved to another account as exhausted.
#: An identifier is never the provider saying no.
_IDENTIFIER_KEYS = frozenset(
    {"session_id", "uuid", "id", "parent_tool_use_id", "tool_use_id", "request_id"}
)


def _without_identifiers(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _without_identifiers(v) for k, v in value.items() if k not in _IDENTIFIER_KEYS
        }
    if isinstance(value, list):
        return [_without_identifiers(v) for v in value]
    return value


_CAPTURE_MARK = TRUNCATION_MARK.decode("ascii")


def _is_capture_notice(line: str) -> bool:
    return line.lstrip().startswith(_CAPTURE_MARK)


def _without_capture_notices(text: str) -> str:
    """`text` less every line the output capture wrote where it cut a stream."""
    if _CAPTURE_MARK not in text:
        return text
    return "\n".join(line for line in text.splitlines() if not _is_capture_notice(line))


def _spend_of(parsed: Any) -> dict[str, Any]:
    """The `SPEND_KEYS` subset of a CLI result, or {} when it reported none.

    For a streamed run, the LAST event that carries them: that is the final
    `result` event, whose totals cover the whole session. Earlier events carry
    per-message usage, nested, and summing those would count the same tokens
    the result event already totals.
    """
    candidates: list[Any]
    if isinstance(parsed, dict):
        candidates = [parsed]
    elif isinstance(parsed, list):
        candidates = list(reversed(parsed))
    else:
        return {}
    for candidate in candidates:
        if isinstance(candidate, dict) and any(key in candidate for key in SPEND_KEYS):
            return {key: candidate[key] for key in SPEND_KEYS if key in candidate}
    return {}


def _scrub_json(value: Any, secrets: Sequence[str]) -> Any:
    """Redact every string in a JSON-able structure, keys included."""
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if isinstance(value, dict):
        return {scrub_text(str(k), secrets): _scrub_json(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v, secrets) for v in value]
    return value


def _looks_complete(parsed: Any) -> bool:
    if isinstance(parsed, dict):
        return bool(parsed.get("result") or parsed.get("output") or parsed.get("content"))
    if isinstance(parsed, list):
        return bool(parsed)
    return False


def _summarise(parsed: Any, raw: str) -> str:
    if isinstance(parsed, dict):
        for key in ("result", "output", "summary", "text", "content"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:2000]
    if isinstance(parsed, list) and parsed:
        # The LAST event that carries text, not the last event: a streamed run
        # can end with a reading (`rate_limit_event`) after its `result`.
        for event in reversed(parsed):
            if not isinstance(event, dict):
                continue
            for key in ("result", "text", "content"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:2000]
    return raw.strip()[-2000:] or "agent produced no textual output"
