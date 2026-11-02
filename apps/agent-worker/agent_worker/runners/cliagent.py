"""Shared machinery for the runners that drive a coding-agent CLI.

`claude_code` and `codex` differ only in which binary they start and which flags
that binary takes. Everything else -- finding the binary, refusing to start
without the tenant's key, capping output, turning a rate limit into a park
signal, turning the transcript into an artifact -- is identical, and identical
code is the only way those two runners stay identical in behaviour.

On flags: the argv prefix has a conservative default and can be overridden with
an environment variable set by the PLATFORM (the image, or the Cloud Run Job
definition) -- never by a caller, whose input never reaches argv at all: the
prompt is written to the CLI's stdin. That is what keeps these runners working
across CLI releases without anybody guessing at flags in a Dockerfile.

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

import dataclasses
import json
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from swarm_redaction import redact as redact_text

from .. import expected_outputs as expected_mod
from .. import indexspec
from .. import issue as issue_mod
from .. import metrics
from ..cachedirs import AGENT_CACHE_ENV
from ..gitidentity import GIT_IDENTITY_ENV
from ..logs import StructuredLogger
from ..procman import TRUNCATION_MARK, ChildProcess, ChildResult, run_child
from ..redact import collect_secrets, replace_lone_surrogates, scrub_file, scrub_text
from .base import (
    SPEND_KEYS,
    CredentialRevokedSignal,
    QuotaExhaustedSignal,
    RunnerContext,
    RunnerFailure,
)
from . import ceiling as ceiling_mod
from .budgetwatch import BudgetStreamWatcher, budget_watcher
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
#: TYPESAFE_API_KEY is the jev-browser MCP server's key (BSTATUS-API; the
#: server inherits the CLI's environment, and no key is written into its MCP
#: config). Present only when the worker was given one; redacted like the rest.
_SENSITIVE_PASSTHROUGH: tuple[str, ...] = ("HTTPS_PROXY", "HTTP_PROXY", "TYPESAFE_API_KEY")
#: GIT_AUTHOR_* and GIT_COMMITTER_* name the person who dispatched the task
#: (P37, `agent_worker.gitidentity`): the worker sets them from the task
#: document, so an agent's `git commit` names that person instead of failing
#: for want of an identity. A name and an address, never a secret.
#: The cache variables (`cachedirs.AGENT_CACHE_ENV`, CACHE-SCRATCH) are the
#: worker's paths for the agent's tool caches, off the workspace volume, and
#: the switches that keep them small on Cloud Run. TMPDIR is set above with
#: the platform's own values, so it is not repeated here.
#: SWARM_TASK_ID is what `apps/swarm-ui/src/vitestWorkers.ts` gates on: vitest
#: sizes its workers to the container's memory only when it is set (#1641).
#: Without it a claude-code agent's vitest run started one jsdom worker per
#: node CPU and was OOM-killed at 8 GiB (task_45e9d3b7c0264ef78091). The
#: worker sets it for this process (`lifecycle._build_child_env`); it is a
#: task id, never a secret.
#: The memory budget (MEMORY_LIMIT_ENV, XDIST_WORKERS_ENV, #2504) is NOT a
#: passthrough: this process reads it from the cgroup and sets it after
#: `extra_env`, so no environment value replaces it.
_PLAIN_PASSTHROUGH: tuple[str, ...] = (
    "NO_PROXY",
    "NODE_EXTRA_CA_CERTS",
    *GIT_IDENTITY_ENV,
    *(name for name in AGENT_CACHE_ENV if name != "TMPDIR"),
    "SWARM_TASK_ID",
)

# ---------------------------------------------------------------------------
# The agent's memory budget (#2504)
# ---------------------------------------------------------------------------
#
# Five claude-code attempts (f-013, f-014, f-039, f-040, f-041) ran `pytest -n
# 8`, `tsc` with `--max-old-space-size=5000` and resident dev servers inside 5
# and 8 GiB containers. The kernel OOM-killed the whole container, the worker
# with it, and each attempt ended `lost_worker`. Nothing told the agent what it
# had: `nproc` said 6 and no variable or prompt line named the memory. So the
# agent's env carries the limit and a worker count, and its first prompt says
# what to do with them. PYTEST_XDIST_AUTO_NUM_WORKERS caps `-n auto` only; the
# prompt line is what covers an explicit `-n`.

#: The container's `memory.max`, in bytes, as the agent's env carries it.
MEMORY_LIMIT_ENV = "SWARM_MEMORY_LIMIT_BYTES"
#: What pytest-xdist's `-n auto` uses instead of the CPU count.
XDIST_WORKERS_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_GIB = 1 << 30
#: Kept out of the test workers' share: the worker, the agent CLI and the node
#: toolchain stay about 2 GiB resident (f-039's baseline was 1.47 GB before
#: the agent ran anything).
AGENT_MEMORY_HEADROOM_BYTES = 2 * _GIB
#: What one xdist worker of the api suite measured, about 1 GiB resident.
XDIST_WORKER_BYTES = 1 * _GIB
#: Kept out of node's heap (#2507): `--max-old-space-size` set to the whole
#: limit leaves nothing for the worker and the CLI that share the container.
OLD_SPACE_HEADROOM_BYTES = _GIB + _GIB // 2


def agent_resource_budget(memory_max: int | None, cpu_cores: float | None) -> dict[str, str]:
    """The budget variables for the agent's env; empty when `memory.max` is unknown.

    Workers: (limit - headroom) // per-worker, capped at the CPU limit (or,
    when that is unknown, at `os.cpu_count()`), and never below 1.
    """
    if memory_max is None or memory_max <= 0:
        return {}
    cap = math.floor(cpu_cores) if cpu_cores else (os.cpu_count() or 1)
    workers = max(1, min(cap, (memory_max - AGENT_MEMORY_HEADROOM_BYTES) // XDIST_WORKER_BYTES))
    return {MEMORY_LIMIT_ENV: str(memory_max), XDIST_WORKERS_ENV: str(workers)}


def container_limits() -> tuple[int | None, float | None]:
    """This container's cgroup `memory.max` and `cpu.max` (cores), each None if unknown."""
    return metrics.cgroup_memory_max(), metrics.cgroup_cpu_limit_cores()


def _gib(size: int) -> str:
    return f"{round(size / _GIB, 1):g} GiB"


def budget_line(memory_max: int, workers: int) -> str:
    """The one prompt line that states the budget, for a first start.

    With #2507's rules: two heavy commands at once, a test run backgrounded
    and forgotten, and a timed-out command whose children outlived it are the
    other ways the container reached its limit. Node's heap is capped
    `OLD_SPACE_HEADROOM_BYTES` below the limit, never below 256 MiB.
    """
    old_space = max(256, (memory_max - OLD_SPACE_HEADROOM_BYTES) // (1 << 20))
    return (
        f"Memory budget: this container has {_gib(memory_max)} (${MEMORY_LIMIT_ENV}), and "
        "at that limit the kernel kills the whole container, this attempt with it. Run "
        "one heavy command (a test run, tsc, a build) at a time, and never background a "
        "test run with & or nohup; when a command times out, kill its process group "
        "(kill -- -PGID) before starting another. Stop any dev server you started (next "
        "dev, uvicorn, postgres) before running tsc or the tests; never pass pytest -n "
        f"above {workers} (${XDIST_WORKERS_ENV}, which -n auto already uses); never set "
        f"--max-old-space-size above {old_space} (MiB: the {_gib(memory_max)} less "
        f"{_gib(OLD_SPACE_HEADROOM_BYTES)} for the worker and the CLI)."
    )

#: Substrings that mean "the provider said no, try later".
_RATE_LIMIT_MARKERS = (
    "rate_limit_error",
    "rate limit",
    "rate-limited",
    "ratelimit",
    # NOT a bare `429` (FIX-RATE-LIMIT-MARKER, part of #2379): a substring
    # match found it inside any hex token, uuid or request id -- one random
    # 32-hex token in 137 -- and parked a 500 as PROVIDER_QUOTA_EXHAUSTED.
    # `_RATE_LIMIT_STATUS` matches it only where it stands as a status.
    "too many requests",
    # NOT `overloaded_error` (RUNNER-ERROR-KINDS, #2302). A 529 is the
    # provider's capacity, not this account's quota: parking it as
    # PROVIDER_QUOTA_EXHAUSTED moved the attempt off a healthy account as
    # "exhausted" and reported a rate limit for the tenant. It is an error
    # kind on the account's health record now (`classify_account_error`).
    "insufficient_quota",
    "quota exceeded",
    "resource_exhausted",
    "usage limit reached",
)

#: A 429 standing as a status -- `API Error: 429 {...}`, `HTTP 429`,
#: `"status": 429`, `(429)` -- and not as three characters of an identifier.
#: Neither side may touch a letter, digit or underscore; nor may a `-`, `.` or
#: `,` come before it (a uuid group, a version, `1,429 bytes`), nor a `.` or
#: `,` followed by a digit after it (`429.5`, `429,117`).
_RATE_LIMIT_STATUS = re.compile(r"(?<![\w.,-])429(?!\w|[.,]\d)")

#: A CLI that surfaces the raw HTTP header and nothing else. `Retry-After` is
#: sent with a 429 or a 503, so it is a rate limit or an outage -- both of
#: which park rather than burn one of the task's three attempts -- UNLESS the
#: same output says the provider was overloaded (a 529 carries one too), which
#: is not a rate limit (`classify_account_error`). `_RETRY_AFTER_PATTERNS` then
#: reads the value out of the same line.
_RETRY_AFTER_MARKERS = ("retry-after", "retry_after")

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
#:
#: AN EXPIRED TOKEN IS ONE TOO (#1855). A 6,160 s attempt failed on "Failed to
#: authenticate. API Error: 401 OAuth access token has expired." -- no marker
#: here matched it, so it failed as an ordinary error. The broker refreshes a
#: held account's token before it expires (#626), so the reload this marker
#: triggers reads the replacement; the two phrases below are that line's halves.
_CREDENTIAL_MARKERS = (
    "oauth access token has been revoked",
    "oauth access token has expired",
    "failed to authenticate",
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
# ---------------------------------------------------------------------------
# Error kinds: what a failed run on a pool account tells the account's health
# ---------------------------------------------------------------------------
#
# BGD, 2026-10-25 ("Runner lane follow-up", #2302): the runner classifies the
# provider errors its CLI gave up on, so the held account's health record (the
# broker's `/account-error` route, which never touches AIMD) says which. The
# CLI retries a 529, a 5xx and a dropped connection itself before it exits, so
# by the time one reaches this runner it is the run's end, not a blip.
#
# A RATE LIMIT IS NOT ONE OF THEM. A 429 or a REJECTED `rate_limit_event` is
# `detect_rate_limit`'s, and is judged first; a refused credential is the
# worker's (`auth`, `lifecycle`), judged second. What is left and matches below
# is reported -- and the run fails exactly as it did before, as an ordinary
# runner failure the task's attempts budget covers: nothing parks, nothing
# moves, nothing halves.

#: The kinds the runner writes to the account channel. The broker's
#: `/account-error` route accepts these and `auth`, which the worker reports.
ERROR_OVERLOADED = "overloaded"
ERROR_SERVER = "server_error"
ERROR_NETWORK = "network"
ACCOUNT_ERROR_KINDS = frozenset({ERROR_OVERLOADED, ERROR_SERVER, ERROR_NETWORK})

#: Longest message written for one error: one short line, what a person reads.
_ERROR_MESSAGE_CHARS = 200

#: Each kind's shapes, first match wins: overloaded before server_error, since
#: a 529 is also a 5xx. Matched against the detection text (`_detection_text`),
#: which holds the provider's events and not the agent's own words, and only
#: for a run that failed.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (ERROR_OVERLOADED, re.compile(
        r"overloaded_error|\boverloaded\b|api error:?\s*\(?529\b"
        r"|api_error_status\W{0,3}529\b",
        re.IGNORECASE,
    )),
    (ERROR_SERVER, re.compile(
        r"\bapi_error\b|api error:?\s*\(?5\d\d\b|api_error_status\W{0,3}5\d\d\b"
        r"|internal server error|bad gateway|service unavailable|gateway time-?out",
        re.IGNORECASE,
    )),
    (ERROR_NETWORK, re.compile(
        r"\beconnreset\b|connection reset|\beconnrefused\b|connection refused"
        r"|\betimedout\b|\benotfound\b|\beai_again\b|\bepipe\b|socket hang up"
        r"|fetch failed|connection error|network error|request timed out",
        re.IGNORECASE,
    )),
)


def classify_account_error(text: str) -> tuple[str, str] | None:
    """(kind, message) for a provider error in CLI output, or None.

    `kind` is one of ACCOUNT_ERROR_KINDS. `message` is the line the error was
    found on, one line of at most `_ERROR_MESSAGE_CHARS` around the match,
    through `swarm_redaction` -- it leaves the pod for the broker's record.
    None for a rate limit (`detect_rate_limit`'s) and for anything else.
    """
    if _rate_limit_marked(text.lower()):
        return None
    for kind, pattern in _ERROR_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        line = text[start : len(text) if end < 0 else end]
        at = match.start() - start
        line = line[max(0, at - _ERROR_MESSAGE_CHARS // 4) :]
        # Redacted BEFORE it is cut, so a cut never leaves half a credential.
        message = " ".join(redact_text(line).text.split())
        return kind, message[:_ERROR_MESSAGE_CHARS]
    return None


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
    #: After the repair turns, the pre-publish gate: rebase onto the current
    #: base through the git tools (one conflict turn) and run the repository's
    #: own checks on what the diff touches (one fix turn); what still fails
    #: goes into the pull request body (SPEED-D1; `agent_worker.prepublish`).
    #: Needs `resume_flag` and a checkout.
    prepublish: bool = False
    #: What follows the flags to tell the CLI its prompt is on stdin. The
    #: prompt is ALWAYS written to the CLI's stdin, never put in its argv
    #: (owner decision 2026-10-06, observer P9; see `run_cli_agent`).
    #: claude-code in print mode reads stdin when no prompt argument is given,
    #: so it needs nothing; codex reads stdin when its prompt argument is `-`.
    stdin_arg: tuple[str, ...] = ()
    #: Stderr lines the CLI prints on every run, which say nothing about why a
    #: run failed. Dropped before `exit_error` chooses what to record (owner
    #: decision 2026-10-06), so a warning every run prints is never reported as
    #: the cause of a kill. Each pattern is matched at the start of a line.
    benign_stderr: tuple[re.Pattern[str], ...] = ()
    #: Where the session id is found when the CLI's stdout does not carry it:
    #: called with the CLI's HOME and the epoch second the run started, it
    #: returns the session that run wrote, or None. codex `exec` prints prose
    #: and names its session only in the rollout under `$HOME/.codex`
    #: (`codex.latest_session`); claude-code's stream-json names it on every
    #: event, so it needs none.
    session_locator: Callable[[Path, float], str | None] | None = None
    #: The files, under the CLI's HOME, whose lines carry its running usage
    #: when its stream reports usage only at the end of a start: the budget
    #: watcher reads what each gains while the start runs. codex's rollouts
    #: (`codex.rollout_files`, BGD's pick "Also read the rollout",
    #: 2026-10-27); claude-code's stream reports every message, so it needs none.
    usage_files: Callable[[Path], list[Path]] | None = None
    #: Directories put BEFORE `BASE_PATH` in the CLI's PATH, which its tools
    #: inherit. Platform constants from the runner module, never caller input.
    #: claude-code on the browser image puts /opt/venv/bin first, so the
    #: agent's `python` is the venv holding Playwright and `swarm_browser`
    #: (BROWSER-ENFORCE, part of #1030; `claude_code.VENV_BIN`).
    path_prefix: tuple[str, ...] = ()
    #: Stop a session at the turn boundary after its context passes 200k
    #: tokens or its tool calls pass 150, and carry the task on in a fresh
    #: session from a handoff, never `--resume` (TOKEN-CEILING, BGD
    #: 2026-10-27, #2524; `ceiling`). Reads claude-code's stream-json usage.
    context_ceiling: bool = False


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
#   RESUME_REASON_ENV   why it stopped: RESUME_MOVED or RESUME_RELOADED.
#
# An attempt on NO account has no channel, and its credential can still be
# refreshed out from under it (#626). For a CLI that can resume, the worker
# names one more file there, and the reload continues what it holds:
#
#   SESSION_FILE_ENV    written here when the CLI exits, whatever it exited
#                       with: `{"session_id": ...}`, mode 0600.
#
# THE SESSION ID IS NEVER LOGGED. It is in the channel file and the restarted
# CLI's argv and nowhere else: the `child started` line prints the argv with
# it masked, and the runner's logger is told it is a secret.
ACCOUNT_STREAM_ENV = "SWARM_ACCOUNT_STREAM"
ACCOUNT_MOVE_ENV = "SWARM_ACCOUNT_MOVE"
RESUME_SESSION_ENV = "SWARM_RESUME_SESSION"
RESUME_REASON_ENV = "SWARM_RESUME_REASON"
SESSION_FILE_ENV = "SWARM_SESSION_FILE"

#: Why a session is being resumed.
RESUME_MOVED = "moved"
RESUME_RELOADED = "credential_reloaded"

#: What a resumed CLI is told. The conversation, the tool results and the
#: workspace are all as they were; this is the one new user message.
RESUME_PROMPT = (
    "Your session was moved to another account at a turn boundary. Continue "
    "the task exactly where you left off; nothing in the workspace changed."
)
#: The same after a credential reload (#626): the account did NOT change, and
#: the CLI was stopped wherever it was, possibly mid-turn, so the last step it
#: took may not have finished.
RELOAD_RESUME_PROMPT = (
    "Your session was interrupted because its credential was refreshed, and "
    "it has been restarted on the same account. Continue the task where you "
    "left off. The last action you took may not have completed; check its "
    "result in the workspace before relying on it."
)


def resume_prompt(reason: str | None) -> str:
    """The resumed CLI's one new message, by why it was stopped."""
    return RELOAD_RESUME_PROMPT if reason == RESUME_RELOADED else RESUME_PROMPT

#: Why this runner stopped the CLI at a turn boundary.
STOP_EXHAUSTED = "exhausted"
STOP_DRAIN = "drain"

#: What a session id looks like. Anything else is not passed to `--resume`.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_WINDOW_NAME = re.compile(r"^[a-z0-9_]{1,40}$")

#: How often the watched loop looks at the stream. The turn boundary it waits
#: for is seconds apart at the fastest, so a quarter of a second loses nothing.
_WATCH_SECONDS = 0.25

# WHAT A RESUMED RUN SPENT GETTING BACK TO WORK (SWAP-RUNS, #2457). A swap
# restarts the CLI with `--resume`, and the restarted agent re-reads before it
# acts again. The watcher notes the first tool call that is not a read -- an
# edit, a write, a command that is not one of the reading commands below -- and
# the seconds and tokens the run had spent before it. The worker exposes the
# figure for a run that followed a swap (`lifecycle._runs`); how the swap
# happens does not change.
#
#: The CLI's tools that only read. Anything else -- Edit, Write, MultiEdit,
#: NotebookEdit, an MCP tool, a Bash command that is not a read -- is the first
#: action. `Task`/`Agent` start a subagent, whose own calls are judged as they
#: arrive, so the start itself is not an action.
READ_TOOLS = frozenset({
    "Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch",
    "TodoRead", "TodoWrite", "ToolSearch", "Task", "Agent", "BashOutput",
})
#: The first word of a shell command that only reads. A command reads when
#: every one of its pieces (split at `;`, `&&`, `||`, `|` and newlines) starts
#: with one of these; `sed -i` and a redirect (`>`) are writes whatever the
#: word. `git` reads only with one of `_READ_GIT`.
_READ_COMMANDS = frozenset({
    "cat", "cd", "echo", "find", "grep", "egrep", "head", "less", "ls", "nl",
    "pwd", "rg", "sed", "stat", "tail", "tree", "wc", "file", "awk", "sort",
    "uniq", "cut", "diff", "jq", "realpath", "dirname", "basename", "true",
})
_READ_GIT = frozenset({
    "log", "show", "diff", "status", "blame", "grep", "ls-files", "rev-parse",
    "branch", "describe", "shortlog",
})
_SHELL_PIECES = re.compile(r"&&|\|\||[;|\n]")
#: The usage counts summed per message for the re-read cost.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _shell_reads(command: Any) -> bool:
    """Whether a Bash tool call's command only reads. False when unsure."""
    if not isinstance(command, str) or not command.strip() or ">" in command:
        return False
    for piece in _SHELL_PIECES.split(command):
        words = piece.strip().split()
        if not words:
            continue
        word = words[0]
        if word not in _READ_COMMANDS and word != "git":
            return False
        if word == "sed" and any(w.startswith("-i") or w == "--in-place" for w in words[1:]):
            return False
        if word == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), None)
            if sub not in _READ_GIT:
                return False
    return True


def is_action(block: Mapping[str, Any]) -> bool:
    """Whether one `tool_use` block is the agent acting rather than reading."""
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return False
    if name in READ_TOOLS:
        return False
    if name == "Bash":
        tool_input = block.get("input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        return not _shell_reads(command)
    return True


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

    def __init__(
        self,
        stdout_path: Path,
        channel: Path,
        move: Path | None,
        *,
        carry: "AccountStreamWatcher | None" = None,
    ) -> None:
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
        #: Stopped at a turn boundary to wrap up before the limit (#2139).
        #: Never `stopped_for`, which the worker reads as a swap.
        self.stopped_to_wrap_up = False
        #: The provider error the run ended in, for the account's health
        #: (`classify_account_error`): {"kind", "message"}, set by `judge`.
        self.error: dict[str, str] | None = None
        self._dirty = False
        # THE RE-READ MEASURE (SWAP-RUNS). One runner process can start the
        # CLI more than once (a finish pass, repair turns), each with its own
        # watcher; `carry` is the previous one, so the clock and the tokens
        # run from the runner's FIRST start and a first action already seen
        # is kept. `_usage` is each message's usage, the LAST copy of each id
        # (the stream repeats a message once per content block).
        self._origin = carry._origin if carry is not None else time.monotonic()
        self._usage: dict[str, dict[str, int]] = dict(carry._usage) if carry is not None else {}
        self._anonymous = carry._anonymous if carry is not None else 0
        self.first_action: dict[str, Any] | None = carry.first_action if carry is not None else None

    def poll(self) -> str | None:
        stop: str | None = None
        for event in self._new_events():
            kind = event.get("type")
            sid = event.get("session_id")
            if isinstance(sid, str) and _SESSION_ID.match(sid) and sid != self.session_id:
                self.session_id = sid
                self._dirty = True
            if kind == "assistant":
                self._note_assistant(event)
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

    def _tokens(self, *, without: str | None = None) -> dict[str, int]:
        totals = dict.fromkeys(_TOKEN_FIELDS, 0)
        for key, usage in self._usage.items():
            if key == without:
                continue
            for field_name in _TOKEN_FIELDS:
                totals[field_name] += usage.get(field_name, 0)
        return totals

    def _note_assistant(self, event: dict[str, Any]) -> None:
        """Count one assistant event's usage; note the first action it takes."""
        message = event.get("message")
        if not isinstance(message, dict):
            return
        key = message.get("id")
        if not isinstance(key, str) or not key:
            self._anonymous += 1
            key = f"\x00{self._anonymous}"
        usage = message.get("usage")
        if isinstance(usage, dict):
            self._usage[key] = {
                f: usage[f]
                for f in _TOKEN_FIELDS
                if isinstance(usage.get(f), int) and not isinstance(usage.get(f), bool)
                and usage[f] >= 0
            }
        if self.first_action is not None:
            return
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and is_action(block):
                # BEFORE it: the message that issues the action is not counted,
                # nor anything after it.
                self.first_action = {
                    "seconds": round(time.monotonic() - self._origin, 3),
                    "tokens": self._tokens(without=key),
                    "tool": str(block.get("name"))[:80],
                    "turns": self.turns,
                }
                self._dirty = True
                return

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
                "error": self.error,
                # SWAP-RUNS: the first action and what was spent before it,
                # or null when the run has taken none (yet); and the tokens
                # and seconds the run has spent so far, which is the re-read
                # cost of a run that never got to act.
                "first_action": self.first_action,
                "spent": {
                    "seconds": round(time.monotonic() - self._origin, 3),
                    "tokens": self._tokens(),
                },
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


def run_session(spec: CliAgentSpec, parsed: Any, home: Path, since: float) -> str | None:
    """The session one start of the CLI ran, or None: its stream's, else its locator's."""
    session = _session_of(parsed)
    if session is None and spec.session_locator is not None:
        try:
            session = spec.session_locator(home, since)
        except OSError:
            session = None
    return session if isinstance(session, str) and _SESSION_ID.match(session) else None


def write_session_file(path: Path, session: str | None) -> None:
    """The session file the worker named (SESSION_FILE_ENV), replaced atomically.

    Advisory like the channel: a file that cannot be written is a reload that
    restarts from the prompt, as before #626, never a failed run.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"session_id": session}))
        os.replace(tmp, path)
    except OSError:
        pass


def _account_watcher(
    spec: CliAgentSpec, stdout_path: Path, carry: AccountStreamWatcher | None = None
) -> AccountStreamWatcher | None:
    """The watcher, when this run holds a pool account and the CLI can resume.

    `carry` is the watcher of this runner's previous start of the CLI, whose
    re-read measure the new one continues."""
    channel = os.environ.get(ACCOUNT_STREAM_ENV, "").strip()
    if not spec.resume_flag or not channel:
        return None
    move = os.environ.get(ACCOUNT_MOVE_ENV, "").strip()
    return AccountStreamWatcher(
        stdout_path, Path(channel), Path(move) if move else None, carry=carry
    )


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
    watcher: AccountStreamWatcher | None,
    stdin_data: bytes,
    budget: BudgetStreamWatcher | None = None,
    usage_files: Callable[[], list[Path]] | None = None,
    ceiling: ceiling_mod.CeilingWatcher | None = None,
    wrap_up_seconds: float = 0.0,
) -> ChildResult:
    """`run_child`, with the stream read as it is produced.

    The same `ChildProcess`, caps and deadline as `run_child`; the difference
    is the loop, which asks the watcher after every slice and stops the CLI at
    a turn boundary when it says so -- and stops it on a SIGTERM to this
    runner, which `run_child` leaves to the worker's SIGKILL.

    `budget` (#2459) reads the same stream through the same tap: it stops the
    CLI at a turn boundary once the worker has asked, or at once when the
    worker asked for the mid-turn stop. Either watcher may be absent.
    `usage_files` lists what else the budget reads this start's usage from
    (`CliAgentSpec.usage_files`). `ceiling` (#2524) stops the CLI at a turn
    boundary once the session passed its context or tool-call ceiling.

    With `wrap_up_seconds` and an account watcher (#2139), once less than that
    is left it also stops the CLI at the next turn boundary of a session it can
    resume, and sets `watcher.stopped_to_wrap_up` for `run_cli_agent` to
    resume it once with WRAP_UP_PROMPT. Not past the point where the pass
    would get less than FINISH_MIN_SECONDS: from there the run goes on to the
    hard deadline, which is unchanged.
    """
    taps = [watcher.tap()] if watcher is not None else []
    if budget is not None:
        taps.append(budget.tap(usage_files))
    if ceiling is not None:
        taps.append(ceiling.tap())

    def tap(chunk: bytes) -> None:
        for feed in taps:
            feed(chunk)

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
        stdout_tap=tap,
        stdin_data=stdin_data,
    )
    child.start()
    deadline = time.monotonic() + limits.timeout_seconds
    soft_deadline = (
        deadline - wrap_up_seconds if wrap_up_seconds > 0 and watcher is not None else None
    )
    # The turns already counted when the soft deadline passed: a boundary
    # after them is the one the CLI is stopped at.
    armed_at: int | None = None
    while True:
        exited = child.wait(_WATCH_SECONDS) is not None
        turns_before = watcher.turns if watcher is not None else 0
        stop = watcher.poll() if watcher is not None else None
        spent = budget.poll() if budget is not None else None
        full = ceiling.poll() if ceiling is not None else False
        if exited:
            if full:
                assert ceiling is not None
                # The CLI ended on its own: there is nothing to stop.
                ceiling.stopped_for = None
            break
        if spent is not None:
            log.info(
                "stopping the agent: its budget is spent"
                + (" (at a turn boundary)" if spent == "stop" else " (mid-turn, past 120%)"),
                turns=budget.meter.turns,
                tokens=budget.meter.tokens(),
            )
            child.terminate(limits.grace_seconds, reason=f"budget ({spent})")
            break
        if stop is not None:
            assert watcher is not None
            log.info(
                "stopping the agent at a turn boundary to move it to another account",
                reason=stop,
                turns=watcher.turns,
            )
            child.terminate(limits.grace_seconds, reason=f"account swap ({stop})")
            break
        if full:
            assert ceiling is not None
            log.info(
                "stopping the agent at a turn boundary: its session reached the context "
                "ceiling, and a fresh session carries the task on",
                why=ceiling.stopped_for,
                turns=ceiling.turns,
                context_tokens=ceiling.context_tokens,
                tool_calls=ceiling.tool_calls,
            )
            child.terminate(limits.grace_seconds, reason=f"context ceiling ({ceiling.stopped_for})")
            break
        if ctx.stop_requested:
            child.terminate(limits.grace_seconds, reason="runner asked to stop")
            break
        now = time.monotonic()
        if now >= deadline:
            child.mark_timed_out()
            child.terminate(limits.grace_seconds, reason="timeout")
            break
        if soft_deadline is not None and now >= soft_deadline:
            assert watcher is not None
            if armed_at is None:
                armed_at = turns_before
            if (
                watcher.turns > armed_at
                and watcher.session_id is not None
                and deadline - now >= FINISH_MIN_SECONDS + limits.grace_seconds
            ):
                log.info(
                    "stopping the agent at a turn boundary to wrap up before the step's limit",
                    turns=watcher.turns,
                    remaining_seconds=round(deadline - now, 1),
                )
                watcher.stopped_to_wrap_up = True
                child.terminate(limits.grace_seconds, reason="wrap-up")
                break
    result = child.finish()
    if watcher is not None:
        watcher.poll()
        watcher.write()
    if budget is not None:
        # The rest of the stream, counted; never a stop: the CLI has exited.
        budget.poll(honour=False)
        budget.write()
    if ceiling is not None:
        ceiling.poll(honour=False)
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
    if not _rate_limit_marked(text.lower()):
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


def _rate_limit_marked(lowered: str) -> bool:
    """Whether lower-cased `lowered` names a rate limit (`detect_rate_limit`)."""
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return True
    if _RATE_LIMIT_STATUS.search(lowered):
        return True
    if not any(marker in lowered for marker in _RETRY_AFTER_MARKERS):
        return False
    overloaded = _ERROR_PATTERNS[0][1]
    return overloaded.search(lowered) is None


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
# The error a non-zero exit records
# ---------------------------------------------------------------------------
#
# Owner decision 2026-10-06 (lane ERR). C1A's review (task_cb50036f4e264168b39e)
# was killed by SIGTERM and recorded "claude-code exited 143: Ignoring 23
# permissions.allow entries ... this workspace has not been trusted" -- a
# warning every headless run prints, which made a kill read as a permissions
# problem. So the spec's benign lines are dropped first, and an exit that is a
# signal says so in words, followed by the one stderr line most likely to say
# why. An ordinary non-zero exit records its stderr tail as before, less the
# benign lines.

#: A line that reads as the failure rather than as progress.
_ERROR_WORD = re.compile(r"\b(?:error|fatal|exception|panic|traceback|failed)\b", re.IGNORECASE)

#: Longest single stderr line kept after a signal sentence.
_SIGNAL_DETAIL_CHARS = 500


def _signal_of(exit_code: int) -> signal.Signals | None:
    """The signal `exit_code` reports, or None for an ordinary exit.

    Two shapes: a negative code is Popen's report of a child the signal killed
    outright; 128+N is a shell's (or a CLI that re-raises as an exit) report of
    one. A code above 128 that names no signal is an ordinary exit.
    """
    number = -exit_code if exit_code < 0 else exit_code - 128 if exit_code > 128 else 0
    if number <= 0:
        return None
    try:
        return signal.Signals(number)
    except ValueError:
        return None


def _without_benign(text: str, benign: Sequence[re.Pattern[str]]) -> list[str]:
    return [line for line in text.splitlines() if not any(p.match(line) for p in benign)]


def exit_error(
    name: str, exit_code: int | None, stderr: str, benign: Sequence[re.Pattern[str]]
) -> str:
    """The error recorded for a CLI that exited `exit_code` with `stderr`."""
    lines = _without_benign(stderr, benign)
    sig = _signal_of(exit_code) if exit_code is not None else None
    if sig is None:
        return f"{name} exited {exit_code}: " + "\n".join(lines).strip()
    verb = "killed" if sig == signal.SIGKILL else "terminated"
    sentence = f"{name} {verb} by {sig.name} (exit {exit_code})"
    candidates = [line.strip() for line in lines if line.strip() and not _is_capture_notice(line)]
    if not candidates:
        return sentence
    errors = [line for line in candidates if _ERROR_WORD.search(line)]
    detail = (errors or candidates)[-1]
    return f"{sentence}: {detail[:_SIGNAL_DETAIL_CHARS]}"


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

# WRAPPED UP BEFORE THE LIMIT (#2139, the optional part of its proposed fix).
# Measured 2026-10-23: three agents polling a background suite with ~600 s
# `sleep` calls were killed mid-sleep at the step's 7200 s limit, their work
# uncommitted -- and FINISH_PROMPT never ran, because it runs only after a CLI
# that exited by itself. So a watched run (`_run_watched`, on a pool account)
# is stopped at its first turn boundary once less than the wrap-up margin is
# left, and resumed ONCE with WRAP_UP_PROMPT inside what is left of the
# budget. A run that reaches no turn boundary, or is not watched, is killed at
# the hard deadline exactly as before.

#: The wrap-up margin: ten minutes is one commit, the pull request's two files
#: and the turn that writes them, with room for a slow model turn -- and about
#: one of the ~600 s sleeps the measured agents were killed in.
WRAP_UP_SECONDS = 600.0
#: At most this share of a short limit goes to the wrap-up, so a 20-minute
#: step still gets three quarters of its time for the work itself.
WRAP_UP_FRACTION = 0.25

#: The one user message a session resumed to wrap up is given, followed by
#: that pass's own `time_limit_line`. Fixed wording; it carries no rate-limit
#: or credential marker for a CLI that echoes it.
WRAP_UP_PROMPT = (
    "Time is nearly up: you were stopped so you can wrap up. Commit what you "
    "have now, write the pull request's title and body where you were told "
    "to, start nothing that takes long, then end."
)


def wrap_up_margin(timeout_seconds: float) -> float:
    """How long before the hard deadline a watched run is stopped to wrap up;
    0 when the margin would be below FINISH_MIN_SECONDS and no wrap-up pass
    could run in it."""
    margin = min(WRAP_UP_SECONDS, timeout_seconds * WRAP_UP_FRACTION)
    return margin if margin >= FINISH_MIN_SECONDS else 0.0


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
#: A turn asking ONLY for the pull request's title is given once (Proposal B,
#: owner decision 2026-10-08). After it the worker fails a step that has no
#: other title, or titles the pull request from its issue with a visible
#: `[title missing] ` prefix (`lifecycle.Worker._generated_pull_request_title`).
TITLE_REPAIR_MAX_TURNS = 1
#: `lifecycle.PR_TITLE_FILE`; named here because the runner never imports the
#: worker.
PR_TITLE_NAME = "pr-title.txt"
#: An index run's repo-index.json that promotion would refuse
#: (`agent_worker.indexspec`) is given ONE turn naming the errors (lane
#: INDEX-ROUTES, #1693): 3 of 19 ai-studio runs were refused for a shape the
#: agent can fix in one pass once it is told which fields. A document still
#: refused after it fails the attempt (`lifecycle.Worker._index_after_agent`).
INDEX_REPAIR_MAX_TURNS = 1
#: `indexrun.INDEXER_PROFILE`, the profile whose runs owe a repo-index.json;
#: `SWARM_RUNNER_PROFILE` is the worker's (`lifecycle._build_child_env`).
INDEXER_PROFILE = "indexer"
RUNNER_PROFILE_ENV = "SWARM_RUNNER_PROFILE"

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


#: THE ONE PLATFORM PARAGRAPH EVERY claude-code AND codex PROMPT WITH A
#: REPOSITORY ENDS WITH (Proposal A, owner decision 2026-10-08). The observer
#: read 31 answers on 2026-10-08 that said no pull request existed because the
#: agent had no GitHub credentials -- true of the agent, false of the step: the
#: worker pushes the committed branch and opens the pull request after the
#: agent exits. Fixed text, so it is the same in every prompt and a test can
#: hold it; it carries no rate-limit or credential marker
#: (`_RATE_LIMIT_MARKERS`, `_CREDENTIAL_MARKERS`) for a CLI that echoes it.
#: The no-change sentence is #2140: build-170 found its issue already built,
#: wrote pr-body.md and no verification.md, and failed `published_nothing`,
#: cancelling its dependant -- the worker reads "no change plus
#: `lifecycle.VERIFICATION_FILE`" as SUCCEEDED no_change (Proposal J), but no
#: prompt said so. It asks only "what you checked", so a brief that wants a
#: stricter verification.md (an issue run's table) still governs its format.
#: The last sentence is the descent rule (#2277): fix-h9 lost 85 turns to a
#: `git checkout -B fix-516 FETCH_HEAD`, because the harvest discards a HEAD
#: that does not descend from the step's start (`lifecycle._head_moved`), and
#: nothing had told the agent so.
PUBLISH_PARAGRAPH = (
    "You do not push or open a pull request; the worker publishes your committed "
    "branch. Commit your work. Write the pull request's title (one line) to "
    "$SWARM_ARTIFACTS_DIR/pr-title.txt and its body to "
    "$SWARM_ARTIFACTS_DIR/pr-body.md. Do not say in your answer that no pull "
    "request exists because you lack GitHub credentials: the worker opens it "
    "after you finish. If the work is already done and nothing needs changing, "
    "commit nothing and write $SWARM_ARTIFACTS_DIR/verification.md saying what "
    "you checked and what showed it is done; that ends the step as no change "
    "instead of a failure. Keep your commits on the branch you were cloned "
    "onto: to build on another branch, fetch it and merge it in (git merge "
    "FETCH_HEAD); never git checkout -B, git switch or git reset onto another "
    "line, because commits that do not descend from the commit you started on "
    "are discarded."
)


#: THE STEP'S WALL CLOCK, TOLD TO THE AGENT (#2139). Measured 2026-10-23 by the
#: failure-review schedule: three claude-code agents (f-003, f-007, f-015)
#: started the tenant's whole pytest suite in the background, polled it with
#: ~600 s `sleep` calls, and were killed mid-sleep at the step's 7200 s limit
#: (exit 137) -- one with a `timeout 7000` longer than its own step -- and a
#: dependant review was cancelled `failed_parent` (f-004). Nothing told them
#: the limit: at the deadline `_run_watched` terminates the CLI without a word,
#: and FINISH_PROMPT runs only after the CLI exits by itself. Fixed wording, so
#: a test can hold it; minutes rounded down and the end time in UTC, so it is
#: never later than the deadline it names. It carries no rate-limit or
#: credential marker for a CLI that echoes it -- the minutes are the one number
#: in it, and only a 429-minute limit would spell one.
def time_limit_line(timeout_seconds: float, now: datetime | None = None) -> str:
    """The one platform line naming this run's limit and when it ends."""
    start = now if now is not None else datetime.now(timezone.utc)
    end = start.astimezone(timezone.utc) + timedelta(seconds=timeout_seconds)
    minutes = int(timeout_seconds // 60)
    return (
        f"This step has a wall-clock limit of {minutes} minute{'' if minutes == 1 else 's'} "
        f"and is stopped at "
        f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')} (UTC), whatever it is doing; work "
        "that is not committed by then is lost. Commit your work and write the "
        "pull request's title and body well before that time. Do not run the "
        "whole test suite in the background and poll it with sleep: run the "
        "tests related to your change, in the foreground."
    )


def _with_reason(entry: str) -> str:
    """A `credential_lines` entry (`path:line rule`) with why the rule's match
    looked like a credential (`lifecycle.credential_reason`, #2030); the entry
    as it is when it is not of that shape."""
    from ..lifecycle import credential_reason

    where, _, rule = entry.rpartition(" ")
    path, _, line = where.rpartition(":")
    if not path or not line.isdigit() or not rule:
        return entry
    return f"{entry}: {credential_reason(rule, path)}"


def repair_prompt(
    missing: Sequence[str], flagged: Sequence[str], artifacts_dir: Path | str,
    index_problems: Sequence[str] = (),
) -> str:
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
        # Lazy: the runner never imports the worker at module level.
        from ..lifecycle import CREDENTIAL_ADVICE

        # Never advice to split, join or encode the literal (#2030, BGD's
        # decision 2026-10-23): that defeats the scan, and some repositories
        # forbid it, which left Product Oracle's agent no way through.
        lines.append(
            "These added lines look like a credential, and the publish step refuses "
            "a diff that adds one (path:line rule: why it looked like one). For each "
            f"line, {CREDENTIAL_ADVICE}:"
        )
        lines += [f"- {_with_reason(entry)}" for entry in flagged[:REPAIR_LIST_CAP]]
        if len(flagged) > REPAIR_LIST_CAP:
            lines.append(f"- and {len(flagged) - REPAIR_LIST_CAP} more")
    if index_problems:
        # `indexspec.problems` already stops at the 20 promotion names.
        lines.append(
            f"{directory / indexspec.INDEX_FILE} does not match {indexspec.SCHEMA}, and "
            "promotion refuses it. Fix each of these fields in that file (field: problem), "
            "keep only the keys the shape names, and write it again; you get this one "
            "turn, and a document still refused fails the run:"
        )
        lines += [f"- {entry}" for entry in index_problems]
    return "\n".join(lines)


#: THE FIGURES A CLI's `result` EVENT REPORTS FOR THE WHOLE SESSION (#667,
#: observer P14). Measured 2026-10-06 on C1C's implement step: the first pass
#: reported $1.8037 and 260,533 ms of API time; the same session resumed to
#: finish reported $1.8804 and 272,258 ms with 951 output tokens -- the CLI
#: restores its cost tracker on `--resume`, so the cost and the API time (and
#: `modelUsage`, which that tracker also keeps) are the session's running
#: totals. `usage`, `num_turns` and `duration_ms` are counted by the
#: invocation and are its own. Summing the first kind recorded $3.684 for a
#: step that cost about $1.88.
SESSION_SPEND_KEYS: tuple[str, ...] = ("total_cost_usd", "duration_api_ms", "modelUsage")


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def carries_session_totals(earlier: Mapping[str, Any], later: Mapping[str, Any]) -> bool:
    """True when `later`, from a resumed invocation, already includes `earlier`.

    Read off the cost, else the API time: a session's running total never
    falls, so a later figure at least the earlier one is the session's total.
    A LOWER one was not carried over -- a CLI that could not restore its
    tracker reports the invocation alone -- and then the two are added, so a
    resume never records less than was measured. False when neither figure
    is on both sides.
    """
    for key in ("total_cost_usd", "duration_api_ms"):
        before, after = _number(earlier.get(key)), _number(later.get(key))
        if before is not None and after is not None:
            return after >= before
    return False


def _combined_spend(
    first: dict[str, Any], second: dict[str, Any], *, fresh: bool = False
) -> dict[str, Any]:
    """The spend of two invocations of one session, `second` the resumed one.

    `SESSION_SPEND_KEYS` are the session's running totals, so `second`'s
    replace `first`'s whenever it carries them (`carries_session_totals`).
    Every other number is the invocation's own and is summed, at any depth.
    A value that is not a number on both sides is the later one's.
    `fresh`: `second` is another session (a context-ceiling handoff, #2524),
    whose totals never include `first`'s, so every number is summed.
    """
    session = not fresh and carries_session_totals(first, second)
    out = dict(first)
    for key, value in second.items():
        prior = out.get(key)
        if key == ceiling_mod.UNPRICED_KEY:
            # Per-model counts with bucket rows: merged, never replaced (#2896).
            out[key] = ceiling_mod.merged_usage_by_model(prior, value)
        elif session and key in SESSION_SPEND_KEYS:
            out[key] = value
        elif isinstance(prior, dict) and isinstance(value, dict):
            out[key] = _combined_spend(prior, value)
        elif (
            isinstance(prior, (int, float)) and isinstance(value, (int, float))
            and not isinstance(prior, bool) and not isinstance(value, bool)
        ):
            out[key] = prior + value
        else:
            out[key] = value
    return out


def _stopped_start_spend(watcher: ceiling_mod.CeilingWatcher, result: Any) -> dict[str, Any]:
    """The spend of a start stopped at the context ceiling, which printed no
    `result` event: its tokens and turns off the stream, its per-model counts
    for the worker to price (`ceiling.UNPRICED_KEY`, #2896), and its wall
    time as `duration_ms`, so the attempt's duration adds up every session."""
    spend = watcher.start_spend()
    seconds = getattr(result, "duration_seconds", None)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        spend["duration_ms"] = int(round(seconds * 1000))
    return spend


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
    # REPOSITORY CONTEXT (docs/design/context-quality.md §4.4, lane CTXQ-P).
    # When the stager put a graph snapshot beside the checkout, the pack is
    # built from it now -- `work/.swarm-context/CONTEXT.md`, outside the
    # checkout, so the publish never commits it -- and one line names it.
    # Nothing staged, no line; a pack that cannot be built is one line saying
    # so, and the agent starts all the same (`contextpack.prepare` never
    # raises). Not on a resume: the session already holds the line, and the
    # file is still where it said.
    # JEV (lane CTXQ-JW): the attempt's client when the worker set Jev up
    # (`judge.attempt`, the workspace opted in), else None and no call is ever
    # made. One ledger file under it holds J1-J5's shared budget.
    jev = _jev_client(spec)
    # A RESUMED SESSION (S13/S14). The worker moved this attempt to another
    # account at a turn boundary and restarted this runner to continue the
    # session it stopped: `--resume <id>` and one short user message, in the
    # same workspace. The original prompt is already in the session.
    resume = os.environ.get(RESUME_SESSION_ENV, "").strip()
    # THE CONTEXT CEILING (#2524; `ceiling`): one watcher for every start, and
    # its state beside the worker's channel, where a restarted runner reads
    # whether the session it was asked to resume ended over the ceiling. One
    # that did is NOT resumed: this run starts a fresh session from a handoff.
    ceiling_watcher: ceiling_mod.CeilingWatcher | None = None
    ceiling_state_path: Path | None = None
    ceiling_seed: dict[str, Any] | None = None
    resumed_over: str | None = None
    if spec.context_ceiling:
        ceiling_state_path = ceiling_mod.state_path(
            os.environ.get(ACCOUNT_STREAM_ENV, "").strip()
            or os.environ.get(SESSION_FILE_ENV, "").strip()
            or None
        )
        ceiling_seed = ceiling_mod.read_state(ceiling_state_path)
        ceiling_watcher = ceiling_mod.CeilingWatcher(
            ceiling_mod.resolve_limits(payload, os.environ.get(RUNNER_PROFILE_ENV)),
            fresh_sessions=ceiling_mod.spent_sessions(ceiling_seed),
        )
        if resume and ceiling_watcher.armed:
            resumed_over = ceiling_mod.over_state(ceiling_seed, resume, ceiling_watcher.limits)
            if resumed_over is not None:
                resume = ""
    if cwd != ctx.work_dir and not resume:
        from .. import contextpack  # lazy: most starts have nothing staged

        context_line = contextpack.prepare(ctx, cwd, judge=jev)
        if context_line:
            prompt = f"{prompt}\n\n{context_line}"
    # THE MEMORY BUDGET (#2504), read from this container's cgroup: the runner
    # and the agent share it. Its line follows the caller's prompt, with or
    # without a repository; not on a resume, where the session already holds
    # it. An unknown limit sets nothing and says nothing.
    memory_max, cpu_cores = container_limits()
    budget = agent_resource_budget(memory_max, cpu_cores)
    if budget and memory_max is not None and not resume:
        prompt = f"{prompt}\n\n{budget_line(memory_max, int(budget[XDIST_WORKERS_ENV]))}"
    # The argv every start of this step shares; a finish pass (below) adds its
    # own `--resume <id>` to it, and writes its own prompt to stdin.
    base_argv = list(argv)
    if resume and not (spec.resume_flag and _SESSION_ID.match(resume)):
        raise RunnerFailure(f"{spec.name} was asked to resume a session it cannot resume")
    # THE PROMPT GOES ON STDIN, NEVER IN THE ARGV (owner decision 2026-10-06,
    # observer P9). Measured on task_cb50036f4e264168b39e: the agent ran
    # `ps aux | grep "[p]ytest tests/unit/scripts" | awk '{print $2}' | xargs
    # -r kill`, its brief contained that very text, and the prompt was the
    # CLI's last argument -- so the grep matched the CLI's own command line
    # and the agent killed its own CLI (exit 143). Every process listing shows
    # the argv; nothing caller-written is in it now. One argv string is also
    # capped at 128 KiB (MAX_ARG_STRLEN), which a long brief could pass.
    # Resolved before the prompt, which names the limit (#2139).
    limits = resolve_limits(payload, platform_ceilings())
    timeout_asked = step_timeout_asked(os.environ)
    if timeout_asked is not None and "timeout_seconds" not in limits.clamped:
        limits = replace(limits, clamped=(*limits.clamped, "timeout_seconds"))
    # The task as a first session is given it: also what a fresh session's
    # handoff carries (#2524), so it is built on a resume as well. The
    # step's time-limit line is not in it: a fresh session is told what is
    # left when it starts, not the whole step's limit.
    task_prompt = expected_mod.with_instructions(prompt, told, ctx.artifacts_dir, staged)
    if ceiling_watcher is not None:
        # The platform's note on the ceiling: a fresh session may carry the
        # task on, from the handoff note the agent keeps (#2524).
        task_prompt = f"{task_prompt}\n\n{ceiling_mod.HANDOFF_NOTE}"
    # The step's wall clock (#2139): last for a task with no repository,
    # just before the platform paragraph for one with a repository.
    first_prompt = f"{task_prompt}\n\n{time_limit_line(limits.timeout_seconds)}"
    if cwd != ctx.work_dir:
        # A task with a repository (Proposal A, owner decision 2026-10-08):
        # the platform paragraph, last. Not on a resume: the session
        # already holds it.
        task_prompt = f"{task_prompt}\n\n{PUBLISH_PARAGRAPH}"
        first_prompt = f"{first_prompt}\n\n{PUBLISH_PARAGRAPH}"
    if resume:
        argv += [spec.resume_flag, resume]
        # A resumed session is told the time it has left, as a fresh start is
        # (#2139, owner decision 2026-10-28): `limits` is what the worker
        # handed this restart (#2363), the time left now, never the start's.
        stdin_prompt = (
            f"{resume_prompt(os.environ.get(RESUME_REASON_ENV, '').strip())}\n\n"
            f"{time_limit_line(limits.timeout_seconds)}"
        )
    else:
        stdin_prompt = first_prompt
    argv += spec.stdin_arg
    # `run_child`'s `child started` line prints the argv (the PR #229 review:
    # this process's stderr is served by `/logs`) and the prompt's size only.
    # A resumed session's id is masked: it never reaches a log line.
    log_argv = [("<session>" if resume and arg == resume else arg) for arg in argv]

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
    if budget:
        log.info("told the agent its memory budget", **budget)
    if limits.clamped:
        log.warning(
            "requested limits exceed the platform ceiling and were clamped",
            clamped=list(limits.clamped),
            effective=limits.as_dict(),
        )
    stdout_path = ctx.artifacts_dir / files.stdout
    stderr_path = ctx.artifacts_dir / files.stderr

    env = {
        "PATH": ":".join((*spec.path_prefix, BASE_PATH)),
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
    # The memory budget is the platform's: after `extra_env`, which cannot
    # replace it, and never a passthrough, so the environment cannot either.
    env.update(budget)
    for passthrough in (*_SENSITIVE_PASSTHROUGH, *_PLAIN_PASSTHROUGH):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]

    # `env` is the set of values this process was given, so it is exactly the
    # set of secrets it could have leaked.
    secrets = collect_secrets(
        env.get(name) for name in (spec.key_env, *spec.alt_key_envs, *_SENSITIVE_PASSTHROUGH)
    )

    # When the latest start began, for `spec.session_locator`: a session file
    # an earlier run left in the restored workspace is not this run's.
    started_at = [time.time()]
    # The last start's account watcher, which the next start's continues.
    last_watcher: list[AccountStreamWatcher | None] = [None]
    # THE SESSION FILE (#626), named by the worker only for a run on no
    # account; written after every start, refused or not, because a refused
    # start is exactly the one the worker's credential reload continues.
    session_file = os.environ.get(SESSION_FILE_ENV, "").strip() if spec.resume_flag else ""

    def note_session(parsed_run: Any) -> None:
        if session_file:
            write_session_file(
                Path(session_file), run_session(spec, parsed_run, ctx.work_dir, started_at[0])
            )

    # THE BUDGET (#2459), named by the worker only for a budgeted task: one
    # watcher for every start below, so a resumed pass adds to the usage.
    budget = budget_watcher()
    usage_files = (
        partial(spec.usage_files, Path(ctx.work_dir)) if spec.usage_files is not None else None
    )

    def start(
        run_argv: list[str],
        run_log_argv: list[str],
        run_prompt: str,
        timeout_seconds: float,
        *,
        fresh: bool = True,
        seed: Mapping[str, Any] | None = None,
        wrap_up: bool = False,
    ) -> tuple[ChildResult, AccountStreamWatcher | None]:
        """One start of the CLI, `run_prompt` on its stdin, its captures redacted
        before anything reads them. `fresh`: a new session, not a `--resume`.
        `wrap_up` arms the stop before the limit (#2139): the first start
        only, so the wrap-up runs at most once."""
        started_at[0] = time.time()
        stdin_data = run_prompt.encode("utf-8")
        run_limits = replace(limits, timeout_seconds=timeout_seconds)
        run_watcher = _account_watcher(spec, stdout_path, last_watcher[0])
        last_watcher[0] = run_watcher
        if ceiling_watcher is not None:
            ceiling_watcher.begin(fresh=fresh, seed=seed)
        if run_watcher is None and budget is None and ceiling_watcher is None:
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
                stdin_data=stdin_data,
                # THE END OF A CAPPED STREAM IS KEPT (#188 review). Under
                # stream-json the stdout is the whole conversation, and its
                # LAST line is the `result` event: the answer, the spend, the
                # evidence the rate-limit decision below reads. A capture that
                # kept only the first `max_stdout_bytes` lost exactly that line
                # on every long session.
                keep_tail=True,
            )
        else:
            # On a pool account, with a budget or under the context ceiling:
            # the same child, the stream read as it is written (see
            # `AccountStreamWatcher`), so the readings and the usage reach the
            # worker while the agent runs and a swap, the budget's stop or the
            # ceiling's can happen at a turn boundary.
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
                stdin_data=stdin_data,
                budget=budget,
                usage_files=usage_files,
                ceiling=ceiling_watcher,
                wrap_up_seconds=wrap_up_margin(timeout_seconds) if wrap_up else 0.0,
            )
        if ceiling_watcher is not None:
            ceiling_mod.write_state(ceiling_state_path, ceiling_watcher.state())
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
        if budget is not None and budget.stopped_for is not None:
            # STOPPED FOR THE BUDGET (#2459), at a turn boundary or past 120%
            # mid-turn. Not judged as a rate limit or a refused credential, and
            # no finish or repair pass follows: the worker reads the channel,
            # checkpoints, publishes and ends the step `budget_exceeded`.
            raise RunnerFailure(
                f"{spec.name} was stopped by its budget ({budget.stopped_for})", spend=spend
            )
        if (
            run_watcher is not None
            and run_watcher.stopped_to_wrap_up
            and not run_result.timed_out
        ):
            # STOPPED AT A TURN BOUNDARY TO WRAP UP (#2139): the exit status is
            # this runner's SIGTERM, not the agent's outcome. The wrap-up pass
            # below is judged instead.
            return
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
            # Neither: a provider error the CLI gave up on, if it is one. On a
            # pool account its kind goes to the channel for the account's
            # health record (the worker reports it); the run fails below
            # exactly as it always has. On no account there is no channel and
            # nothing to report.
            error = classify_account_error(combined)
            if error is not None and run_watcher is not None:
                run_watcher.error = {"kind": error[0], "message": error[1]}
                run_watcher.write()
        if run_result.timed_out:
            raise RunnerFailure(
                f"{spec.name} timed out after {limits.timeout_seconds:.0f}s", spend=spend
            )
        if run_result.exit_code != 0:
            raise RunnerFailure(
                exit_error(
                    spec.name, run_result.exit_code, _tail(stderr_path, 2000), spec.benign_stderr
                ),
                spend=spend,
            )

    def detection_text(raw: str, parsed_run: Any) -> str:
        # What the rate-limit and credential heuristics read. For a streamed
        # run this is NOT the raw stdout tail -- see `_detection_text`. Neither
        # half includes the capture's own notices, which count bytes: `429`
        # standing as a number is a marker.
        return (
            _detection_text(raw, parsed_run)
            + "\n"
            + _without_capture_notices(_tail(stderr_path))
        )

    def ceiling_stop_pending() -> bool:
        """Whether the last start was stopped at the context ceiling, and by
        nothing else: a budget stop, a swap and a timeout are judged as before."""
        return (
            ceiling_watcher is not None
            and ceiling_watcher.stopped_for is not None
            and not result.timed_out
            and (budget is None or budget.stopped_for is None)
            and (watcher is None or watcher.stopped_for is None)
        )

    def note_ceiling_stop(**fields: Any) -> dict[str, Any]:
        """Record a stop a fresh session follows, where `result_summary` reads it."""
        assert ceiling_watcher is not None
        stop = ceiling_watcher.record_stop(**fields)
        ceiling_mod.write_state(ceiling_state_path, ceiling_watcher.state())
        ctx.report["context_ceiling"] = ceiling_watcher.summary()
        log.warning(
            "the session passed its context ceiling; the task carries on in a fresh "
            "session from a handoff, not a --resume",
            **{k: v for k, v in stop.items() if k != "wip"},
        )
        return stop

    def ceiling_handoff(stop: dict[str, Any], then: str | None) -> str:
        """Commit the tree, and the prompt the fresh session starts from (#2524)."""
        from ..prepublish import CLONE_BASE_ENV, EMPTY_BASE  # lazy, as the gate's

        diff_stat: str | None = None
        commits: str | None = None
        wip: dict[str, Any] | None = None
        if cwd != ctx.work_dir:
            wip = ceiling_mod.commit_wip(
                cwd, env,
                f"WIP: context ceiling, fresh session {stop.get('fresh_session')} (#2524)",
            )
            stop["wip"] = {"status": wip.get("status"), "commit": wip.get("commit")}
            if wip.get("status") == "failed":
                log.warning("the work-in-progress commit failed; the fresh session works "
                            "in the same tree", detail=scrub_text(str(wip.get("detail")), secrets))
            base = os.environ.get(CLONE_BASE_ENV, "").strip()
            diff_stat, commits = ceiling_mod.branch_summary(
                cwd, env, base if base and base != EMPTY_BASE else None)
            ctx.report["context_ceiling"] = ceiling_watcher.summary() if ceiling_watcher else None
        # The previous session's notes (#2897): its note file, the notes
        # files it kept, its todo list and its own last words -- all of this
        # attempt's own artifacts and stream, scrubbed of the run's secrets.
        tail = ceiling_watcher.session_tail() if ceiling_watcher is not None else None
        return ceiling_mod.handoff_prompt(
            stop=stop, task_prompt=task_prompt, diff_stat=diff_stat, commits=commits,
            wip=wip, note=ceiling_mod.agent_note(Path(ctx.artifacts_dir)), then=then,
            session_tail=scrub_text(tail, secrets) if tail else None,
            todos=_scrub_json(ceiling_watcher.todos, secrets) if ceiling_watcher else None,
            files=ceiling_mod.notes_files(Path(ctx.artifacts_dir)),
        )

    if ceiling_watcher is not None:
        ctx.report["context_ceiling"] = ceiling_watcher.summary()
    if resumed_over is not None:
        # Asked to resume a session that ended over the ceiling (a swap, a
        # credential reload): a fresh session from a handoff instead.
        stop = note_ceiling_stop(
            at=ceiling_mod.AT_RESUME, why=resumed_over,
            resume_reason=os.environ.get(RESUME_REASON_ENV, "").strip() or None,
            **ceiling_mod.seed_numbers(ceiling_seed),
        )
        stdin_prompt = ceiling_handoff(stop, time_limit_line(limits.timeout_seconds))
    result, watcher = start(
        argv, log_argv, stdin_prompt, limits.timeout_seconds,
        fresh=not resume, seed=ceiling_seed if resume else None, wrap_up=True,
    )
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
    note_session(parsed)
    spend = _scrub_json(_spend_of(parsed), secrets)
    if ceiling_stop_pending() and not spend:
        # Stopped at the ceiling before the CLI printed its totals (#2524).
        assert ceiling_watcher is not None
        spend = _stopped_start_spend(ceiling_watcher, result)
    combined = detection_text(raw_stdout, parsed)
    if not ceiling_stop_pending():
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
    # The spend of the sessions a context-ceiling handoff ended, and of the
    # current one: a resume's totals are its session's alone (#2524).
    ended_spend: dict[str, Any] = {}
    session_spend = spend

    def resume_pass(session: str | None, pass_prompt: str, remaining: float) -> Any:
        """Continue `session` once with `pass_prompt`; return that pass's own parsed output.

        Shared by the finish pass and the repair turns. The step's record is
        kept whole: the captures, the spend, the durations and what the
        transcript and summary read cover every start. `session` None starts
        a fresh session instead (a context-ceiling handoff, #2524).
        """
        nonlocal result, watcher, capture, raw_stdout, parsed, spend, combined
        nonlocal duration_seconds, stdout_bytes, stderr_bytes, ended_spend, session_spend
        first_stdout = stdout_path.read_bytes() if stdout_path.exists() else b""
        first_stderr = stderr_path.read_bytes() if stderr_path.exists() else b""
        fresh = session is None
        pass_argv = [
            *base_argv, *([str(spec.resume_flag), session] if session else []), *spec.stdin_arg
        ]
        pass_log_argv = [("<session>" if session and arg == session else arg)
                         for arg in pass_argv]
        result, watcher = start(pass_argv, pass_log_argv, pass_prompt, remaining, fresh=fresh)
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
        note_session(pass_parsed)
        raw_stdout = stdout_path.read_text(errors="replace")
        parsed = _parse_cli_output(raw_stdout)
        pass_spend = _scrub_json(_spend_of(pass_parsed), secrets)
        if ceiling_stop_pending() and not pass_spend:
            assert ceiling_watcher is not None
            pass_spend = _stopped_start_spend(ceiling_watcher, result)
        if fresh:
            ended_spend = _combined_spend(ended_spend, session_spend, fresh=True)
            session_spend = pass_spend
        else:
            session_spend = _combined_spend(session_spend, pass_spend)
        spend = _combined_spend(ended_spend, session_spend, fresh=True)
        combined = detection_text(pass_raw, pass_parsed)
        if not ceiling_stop_pending():
            judge(result, watcher, spend, combined)
        duration_seconds += result.duration_seconds
        stdout_bytes += result.stdout_bytes
        stderr_bytes += result.stderr_bytes
        return pass_parsed

    def step_remaining() -> float:
        return limits.timeout_seconds - duration_seconds

    def carry_on(then: str | None, remaining: Callable[[], float]) -> Any:
        """While the last start stopped at the context ceiling, a fresh session
        from a handoff (#2524); the last one's parsed output, or None."""
        last = None
        while ceiling_stop_pending():
            assert ceiling_watcher is not None
            left = remaining()
            if left < FINISH_MIN_SECONDS:
                ceiling_watcher.skipped = "budget"
                ctx.report["context_ceiling"] = ceiling_watcher.summary()
                log.warning(
                    "the session passed its context ceiling with too little of the step's "
                    "budget left for a fresh session; the result stands as it is",
                    remaining_seconds=round(left, 1),
                    minimum_seconds=FINISH_MIN_SECONDS,
                )
                break
            stop = note_ceiling_stop(
                at=ceiling_mod.AT_TURN_BOUNDARY, why=ceiling_watcher.stopped_for)
            # A handoff with nothing else to ask ends with what is left of the
            # step's wall clock (#2139), which the fresh session was never told.
            last = resume_pass(
                None, ceiling_handoff(stop, then or time_limit_line(left)), left)
        return last

    def pass_within_ceiling(
        session: str, pass_prompt: str, remaining: Callable[[], float], reason: str
    ) -> Any:
        """`resume_pass`, unless the session ended over the context ceiling:
        then a fresh session from a handoff that ends with `pass_prompt`."""
        why = (ceiling_watcher.over()
               if ceiling_watcher is not None and ceiling_watcher.armed else None)
        if why is not None:
            stop = note_ceiling_stop(at=ceiling_mod.AT_RESUME, why=why, resume_reason=reason)
            done = resume_pass(None, ceiling_handoff(stop, pass_prompt), remaining())
        else:
            done = resume_pass(session, pass_prompt, remaining())
        later = carry_on(pass_prompt, remaining)
        return later if later is not None else done

    # RESUMED ONCE TO WRAP UP (#2139; see WRAP_UP_PROMPT). The watched run was
    # stopped at a turn boundary with less than the wrap-up margin left;
    # continue the session once, told to commit and end, inside what is left
    # of the step's budget. Only the first start is armed, so this never runs
    # twice, and the passes after it count every start's duration. A session
    # over the context ceiling wraps up in a fresh session instead (#2524).
    resumed_to_wrap_up = False
    if watcher is not None and watcher.stopped_to_wrap_up and not result.timed_out:
        remaining = step_remaining()
        session = _session_of(parsed) or watcher.session_id
        if session is None or remaining < FINISH_MIN_SECONDS:
            # `_run_watched` stops only with a session and the floor left, so
            # this is a stop that overran its grace: what the hard deadline
            # would have made of it.
            raise RunnerFailure(
                f"{spec.name} timed out after {limits.timeout_seconds:.0f}s", spend=spend
            )
        log.register_secret(session)
        log.warning(
            "the agent was stopped at a turn boundary before the step's limit; "
            "resuming the session once to wrap up",
            remaining_seconds=round(remaining, 1),
        )
        # Said before the pass starts, so a pass that fails or parks still
        # reports that it was resumed (`write_result` merges it).
        resumed_to_wrap_up = True
        ctx.report["resumed_to_wrap_up"] = True
        pass_within_ceiling(
            session, f"{WRAP_UP_PROMPT}\n\n{time_limit_line(remaining)}", step_remaining,
            "wrap_up",
        )

    # THE CONTEXT CEILING (#2524): a session stopped at it goes on fresh, and
    # what follows -- the finish pass, the repairs, the gate -- reads the last.
    carry_on(None, step_remaining)

    pending = pending_work(parsed) if spec.finish_on_pending and spec.resume_flag else None
    if pending is not None:
        # The whole step's starts, not the last one's: after a wrap-up pass
        # the finish pass gets what both left, never more (#2139).
        remaining = step_remaining()
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
            # Told what is left after every start so far (#2139), as a fresh
            # start is told its whole limit.
            finish_parsed = pass_within_ceiling(
                session, f"{FINISH_PROMPT}\n\n{time_limit_line(remaining)}", step_remaining,
                "finish",
            )
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

        # AN INDEX RUN'S DOCUMENT (#1693), checked with promotion's rules. An
        # absent file is not this check's: the worker skips the graph write.
        index_run = os.environ.get(RUNNER_PROFILE_ENV) == INDEXER_PROFILE

        def index_check() -> list[str]:
            if not index_run:
                return []
            return indexspec.problems_of_file(
                Path(ctx.artifacts_dir) / indexspec.INDEX_FILE) or []

        ctx.report["repair_turns"] = 0
        missing, flagged = run_checks()
        index_problems = index_check()
        index_turns = 0
        if repair_scan_error is not None:
            log.warning(
                "the publish credential scan could not run before the step ended; "
                "the publish runs it again",
                error=repair_scan_error,
            )
        while True:
            ask_index = index_problems if index_turns < INDEX_REPAIR_MAX_TURNS else []
            if not (missing or flagged or ask_index):
                if index_problems:
                    log.warning(
                        f"{indexspec.INDEX_FILE} is still refused after its repair turn; "
                        "the worker fails the attempt with the errors",
                        index_problems=index_problems,
                    )
                break
            if repair_turns >= TITLE_REPAIR_MAX_TURNS and not flagged and not ask_index \
                    and list(missing) == [PR_TITLE_NAME]:
                # Only the title is still missing: it had its one follow-up
                # turn (Proposal B), and the worker decides the rest.
                log.warning(
                    f"{PR_TITLE_NAME} is still missing after its follow-up turn; "
                    "the worker's own checks decide",
                )
                break
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
                index_problems=ask_index,
                remaining_seconds=round(remaining, 1),
            )
            if ask_index:
                index_turns += 1
            text = repair_prompt(missing, flagged, ctx.artifacts_dir, ask_index)
            pass_within_ceiling(session, text, step_remaining, "repair")
            after_missing, after_flagged = run_checks()
            index_problems = index_check()
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
                **({"index_problems": list(ask_index),
                    "index_fixed": bool(ask_index) and not index_problems}
                   if index_run else {}),
            })
            ctx.report["repairs"] = repairs
            missing, flagged = after_missing, after_flagged
        if missing or flagged or index_problems:
            unresolved = {
                "missing_outputs": missing[:REPAIR_LIST_CAP],
                "credential_lines": flagged[:REPAIR_LIST_CAP],
                **({"index_problems": index_problems} if index_run else {}),
            }

    # JEV'S POST-STEPS (lane CTXQ-JW; `judge.poststeps`). The planner's plan
    # gets J2 coverage and J5 duplicate in `plan-advisory.json`; the review's
    # verdict.json gets J3's `p_real` on its findings. Advisory: `verdict`
    # and plan.json are never changed, and with Jev unreachable nothing is.
    if jev is not None:
        jev_done = _jev_post_steps(ctx, jev, cwd, log)
        if any(v is not None for v in jev_done.values()):
            # Only what Jev answered: a failed call leaves the local result.
            ctx.report["jev"] = jev_done

    # THE PRE-PUBLISH GATE (SPEED-D1; `agent_worker.prepublish`). After the
    # repair turns, so the checks read the tree they left. A task with no
    # checkout has nothing to publish.
    prepublish_record: dict[str, Any] | None = None
    if spec.prepublish and spec.resume_flag and cwd != ctx.work_dir:
        # The checks' own time counts against the step's budget as the
        # agent's does: `duration_seconds` sums only the CLI's starts.
        gate_started, agent_before = time.monotonic(), duration_seconds

        from ..prepublish import deadline_left  # lazy, as the gate's

        def gate_remaining() -> float:
            outside = (time.monotonic() - gate_started) - (duration_seconds - agent_before)
            counted = limits.timeout_seconds - duration_seconds - max(0.0, outside)
            # NEVER PAST THE WORKER'S KILL (#2981). `counted` sums only the
            # CLI's starts and the gate's own time; this runner's setup before
            # its first start (the context pack) and the worker's after it
            # built the limit are in neither, so on task_2518ef3904ed47d0ac52
            # the gate believed it had ~18 minutes the worker did not give it.
            left = deadline_left(os.environ)
            return counted if left is None else min(counted, left)

        def resume_for_gate(text: str) -> bool:
            """One more turn of the session for the gate; False, and nothing
            started, with no session to resume or too little budget."""
            session = _session_of(parsed)
            remaining = gate_remaining()
            if session is None or remaining < FINISH_MIN_SECONDS:
                log.warning(
                    "the pre-publish gate could not resume the session",
                    why="no_session" if session is None else "budget",
                    remaining_seconds=round(remaining, 1),
                )
                return False
            log.register_secret(session)
            pass_within_ceiling(session, text, gate_remaining, "prepublish")
            return True

        prepublish_record = run_prepublish_gate(
            ctx, cwd=cwd, env=env, credential_env=credential_env, secrets=secrets,
            log=log, resume=resume_for_gate, remaining=gate_remaining, judge=jev,
            staged_patches=[p for p in staged if p.endswith(STAGED_PATCH_SUFFIXES)],
        )

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
        "limits": {**limits.as_dict(),
                   **({} if timeout_asked is None else {"timeout_seconds_asked": timeout_asked})},
        # Read by the worker into `result_summary.agent_streams`; null when the
        # transcript was written (or there was no parsed output to write).
        "transcript_skipped": transcript_skipped,
        # Whether the session was resumed once to finish work its answer left
        # in flight, and, when that was called for but not done, why not.
        "resumed_to_finish": resumed_to_finish,
        "finish_skipped": finish_skipped,
        # Whether the run was stopped at a turn boundary before its limit and
        # resumed once to wrap up (#2139).
        "resumed_to_wrap_up": resumed_to_wrap_up,
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
        # The pre-publish gate (SPEED-D1): the rebase, every check that ran
        # with its status and duration per round, whether the fix or conflict
        # turn was taken, and what still fails. Never a check's output.
        "prepublish": prepublish_record,
        # The context ceiling (#2524): its limits, and every stop with its
        # turn, context tokens, tool calls and why; null for a CLI it does
        # not watch.
        "context_ceiling": ceiling_watcher.summary() if ceiling_watcher is not None else None,
        # Also in `ctx.report`, which `write_result` merges anyway; stated here
        # so this function's own return value is the whole answer.
        **capture,
        "metrics": {
            "duration_seconds": round(duration_seconds, 3),
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
        },
    }


#: The `timeout_seconds` a step's spec asked for, exported by the worker only
#: when it is above what the platform gave the step (`lifecycle`, #2981): the
#: API lowers it to the profile's cap before the task is stored, so without it
#: `limits.clamped` said nothing of a 10,800 s ask run under a 7,200 s cap.
TIMEOUT_ASKED_ENV = "SWARM_TIMEOUT_ASKED_SECONDS"


def step_timeout_asked(env: Mapping[str, str]) -> int | None:
    """The clamped ask from `TIMEOUT_ASKED_ENV`, or None: unset or unusable."""
    raw = str(env.get(TIMEOUT_ASKED_ENV, "")).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


#: The staged inputs the gate reads as patches (#2981): an upstream step's
#: `swarm-work.patch` is how a review, visual QA or acceptance step gets the
#: diff it applies. Only these; a staged report is never applied.
STAGED_PATCH_SUFFIXES = (".patch", ".diff")


def run_prepublish_gate(
    ctx: RunnerContext,
    *,
    cwd: Path,
    env: Mapping[str, str],
    credential_env: str,
    secrets: Sequence[str],
    log: StructuredLogger,
    resume: Callable[[str], bool],
    remaining: Callable[[], float],
    judge: Any = None,
    staged_patches: Sequence[str] = (),
) -> dict[str, Any]:
    """The pre-publish gate over the checkout (`agent_worker.prepublish.Gate`).

    The checks run in the agent's environment WITHOUT its provider credential;
    the rebase goes through the git tools' spool when the worker offered it
    and named a base (`prepublish.PREPUBLISH_BASE_ENV`). Recorded in
    `ctx.report` as it goes, so a gate cut short still says what it did.
    """
    from .. import prepublish
    from ..gitmcp import server as gitmcp_server

    base = os.environ.get(prepublish.CLONE_BASE_ENV, "").strip() or None
    check_env = prepublish.check_env(env, drop=(credential_env,), base=base)
    rebase_ref = os.environ.get(prepublish.PREPUBLISH_BASE_ENV, "").strip()
    if rebase_ref:
        check_env[prepublish.PREPUBLISH_BASE_ENV] = rebase_ref
    spool = os.environ.get(gitmcp_server.SPOOL_ENV, "").strip()
    submit: Callable[..., dict[str, Any]] | None = None
    if spool:
        def submit(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return gitmcp_server.submit(Path(spool), tool, arguments,
                                        wait_seconds=prepublish.REBASE_WAIT_SECONDS)

    # What a resumed turn raises -- a rate limit, a credential refusal, a
    # kill -- ends the step exactly as it would after any turn; only the
    # gate's own failures are swallowed.
    from_turn: list[BaseException] = []

    def turn(text: str) -> bool:
        try:
            return resume(text)
        except BaseException as exc:
            from_turn.append(exc)
            raise

    record: dict[str, Any] = {}
    ctx.report["prepublish"] = record
    gate = prepublish.Gate(
        repo=Path(cwd), env=check_env, secrets=secrets, resume=turn, remaining=remaining,
        log=log, artifacts_dir=Path(ctx.artifacts_dir), submit=submit,
        python=sys.executable or "python3", record=record, judge=judge,
        staged_patches=tuple(staged_patches),
    )
    try:
        return gate.run()
    except Exception as exc:  # the gate never costs the step its result
        if from_turn and exc is from_turn[-1]:
            raise
        log.error("the pre-publish gate failed; publishing without it",
                  error=type(exc).__name__)
        record["error"] = type(exc).__name__
        return record


def _jev_client(spec: CliAgentSpec) -> Any:
    """This attempt's Jev client (`judge.attempt.load`), scrubbing this
    runner's own secrets too (§5.4 step 4), or None. Never raises."""
    try:
        from ..judge import attempt as judge_attempt

        client, _why = judge_attempt.load()
        if client is None:
            return None
        own = collect_secrets(os.environ.get(name) for name in
                              (spec.key_env, *spec.alt_key_envs, *_SENSITIVE_PASSTHROUGH))
        return dataclasses.replace(client, secrets=tuple(client.secrets) + tuple(own))
    except Exception:  # noqa: BLE001 -- Jev is never worth the step
        return None


def _jev_post_steps(ctx: RunnerContext, jev: Any, cwd: Path, log: StructuredLogger
                    ) -> dict[str, Any]:
    """J2/J5 for a planner, J3 for a review; what each did. Never raises."""
    from ..contextpack import context_dir
    from ..contextpack.tasktype import classify
    from ..judge import poststeps

    out: dict[str, Any] = {}
    try:
        kind = classify(ctx.payload, ctx.work_dir)
        checkout = cwd if cwd != ctx.work_dir else None
        if kind == "plan":
            advisory = poststeps.plan_post_step(jev, Path(ctx.artifacts_dir),
                                                context_dir(ctx.work_dir))
            out["plan"] = None if advisory is None else {
                "coverage_flagged": len((advisory.get("coverage") or {}).get("flagged") or []),
                "hold_for_human": advisory.get("hold_for_human")}
        elif kind == "review":
            out["review"] = poststeps.review_post_step(jev, Path(ctx.artifacts_dir), checkout,
                                                       Path(ctx.work_dir), ctx.payload)
    except Exception as exc:  # noqa: BLE001 -- advisory: never the step's failure
        log.warning("a Jev post-step failed; its outputs are the local-only ones",
                    error=type(exc).__name__)
        out["error"] = type(exc).__name__
    return out


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
    dropped -- and `429` standing as a number is a marker, so 429 dropped
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


#: Keys whose values are random identifiers. `429` was a marker matched
#: anywhere until FIX-RATE-LIMIT-MARKER, and a uuid contains it about one time
#: in 115 -- so a FAILED run
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
    """Redact every string in a JSON-able structure, keys included, and replace
    every lone surrogate in one with U+FFFD (#227): the CLI's JSON can carry a
    `\\ud800` escape, and the worker writes this structure toward a Firestore
    document, which cannot carry one. The worker counts what reaches the
    summary (`result_summary.runner.surrogates_replaced`)."""
    if isinstance(value, str):
        return replace_lone_surrogates(scrub_text(value, secrets))[0]
    if isinstance(value, dict):
        return {
            replace_lone_surrogates(scrub_text(str(k), secrets))[0]: _scrub_json(v, secrets)
            for k, v in value.items()
        }
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
