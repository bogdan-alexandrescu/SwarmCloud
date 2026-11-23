"""The clone's Python environment, warmed once per attempt before the agent's first turn.

WHY (PYTHON-WARMUP, owner-approved improvement plan of 2026-11-23: tests and
installs, not the model, take most of an agent task's wall-clock). Read
2026-11-23: the worker had no dependency install phase. At attempt start the
uv cache is empty (cachedirs.py empties it) and the clone has no `.venv`
(checkpoint.BUILD_DIRS leaves it out of every checkpoint), so the agent's
first `uv run` and the pre-publish gate's `uv run pytest` each paid a cold
`uv sync`: inside the agent's turn budget, and inside the gate's 600 s.

WHAT RUNS. When the clone has a `uv.lock` at its root, and only then, the
worker runs `WARM_SYNC_ARGV` once, from the clone's root, after the clone and
before the agent's first turn. It is the sync `uv run` does before it runs
anything -- the project's default groups (dev), no extras, inexact -- which
is what the gate's `uv run` and scripts/related-tests.sh's `uv run pytest`
rely on. `UV_ENV_FLAGS` states those flags once and the gate reads it
(`prepublish._pytest_project`). The warm-up adds only `--frozen`: it installs
uv.lock as it stands and never rewrites it, so the worker writes no lock into
a checkout the publish stages with `git add --all` (archived issue 1964). The
agent's `uv run` then finds `.venv` ready; the gate, whose environments live
outside the clone (`prepublish.uv_outside`), builds its own from a warm cache.

NEVER FATAL, AND BOUNDED. The sync is killed (its whole process group) at
`WARM_DEADLINE_SECONDS`; a timeout, a non-zero exit or a uv that cannot start
ends the warm-up only. The attempt goes on exactly as it did before this
module, and ONE event line says the warm-up did not finish and why.

ONCE PER ATTEMPT. `PythonEnvWarmup` remembers it ran, so a fresh session in
the same attempt asks and nothing runs. A clone whose environment already
carries this lock's marker (`is_ready`) is not synced again.

INVARIANTS 4 AND 5. `may_start` is asked first: it re-reads the generation
(a stale one raises `FencedError`, and nothing starts) and answers False when
the task's cancel was requested. The sync is not a provider wait: it is
bounded by the deadline, the lease is beaten through it by the caller's
`meanwhile`, and no quota or provider signal is waited on inside it.

NOTHING SECRET-SHAPED LEAVES. The sync runs with `sync_env`, an allow-list of
the agent's environment (no provider key, no forge token), and every byte it
printed passes through the caller's `redact` before it reaches an event.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Sequence

#: The flags of the sync `uv run` does before it runs a command, stated once:
#: inexact (`uv run`'s own default: extraneous packages are kept), the
#: project's default groups (dev) and no extras -- no `--group` or `--extra`,
#: so a repository without a `dev` group syncs too. The gate's `uv run` reads
#: this (`prepublish.UV_ENV_FLAGS`).
UV_ENV_FLAGS: tuple[str, ...] = ("--inexact",)

#: The warm-up's command, run from the clone's root. `--frozen`: install the
#: lock as it stands and never rewrite it (see the module's docstring).
WARM_SYNC_ARGV: tuple[str, ...] = ("uv", "sync", *UV_ENV_FLAGS, "--frozen")

#: The warm-up's hard deadline. A cold `uv sync` of this repository (about 90
#: locked packages) measured 3.3-3.6 s in an agent-runtime-base container on
#: 2026-11-23 (three runs, empty cache, new environment). 120 s is ~35 times
#: that, room for a tenant repository with heavy wheels, and a fifth of the
#: gate's 600 s budget; past it the sync is killed, and what it had downloaded
#: stays in the cache for the agent's own `uv run` to continue from.
WARM_DEADLINE_SECONDS = 120

#: The lock file whose presence at the clone's root turns the warm-up on.
UV_LOCK = "uv.lock"
#: The marker written into the environment after a sync that finished: the
#: lock's digest and the command, so a restored environment that carries it
#: for the same lock is not synced again.
READY_MARKER = ".swarm-python-env.json"
#: `detail.phase` on the attempt's event, where the scorecard reads the
#: warm-up's `seconds` and `status`.
EVENT_PHASE = "python_env"
#: How many lines of a failed sync's (redacted) output the event carries.
OUTPUT_TAIL_LINES = 20
#: How long a killed sync's pipes are read for before they are closed.
KILL_DRAIN_SECONDS = 5.0

READY = "ready"
ALREADY_READY = "already_ready"
NOT_STARTED = "not_started"
FAILED = "failed"
TIMED_OUT = "timed_out"
#: The outcomes in which a sync ran: the caller re-reads the generation after
#: them, since a fence may have landed meanwhile.
RAN = frozenset({READY, FAILED, TIMED_OUT})

#: What `sync_env` keeps of the agent's environment: what uv needs to find
#: itself, its cache, its certificates and the egress proxy -- never a
#: provider key or a forge token.
_ENV_NAMES = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
_ENV_PREFIXES = ("UV_", "XDG_")


@dataclass(frozen=True)
class Synced:
    """What one sync did: its exit code, its output (stdout and stderr), and
    whether it was killed at its deadline."""

    code: int
    output: str
    timed_out: bool = False


SyncRunner = Callable[[Sequence[str], Path, Mapping[str, str], float], Synced]


@dataclass(frozen=True)
class WarmResult:
    status: str
    seconds: float
    reason: str | None = None
    output_tail: str = ""

    def as_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "phase": EVENT_PHASE,
            "status": self.status,
            "seconds": round(self.seconds, 3),
            "command": " ".join(WARM_SYNC_ARGV),
        }
        if self.reason:
            detail["reason"] = self.reason
        if self.output_tail:
            detail["output_tail"] = self.output_tail
        return detail


def run_sync(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Synced:
    """Run `argv` in its own process group, killed whole at `timeout` -- and on
    anything that interrupts the wait (a SIGTERM raised into the startup), so
    no uv outlives the warm-up that started it."""
    proc = subprocess.Popen(
        list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=max(0.1, timeout))
        return Synced(proc.returncode, out.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        _kill(proc)
        return Synced(124, _drain(proc), timed_out=True)
    except BaseException:
        _kill(proc)
        _drain(proc)
        raise


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _drain(proc: subprocess.Popen[bytes]) -> str:
    """What a killed sync printed, read for at most `KILL_DRAIN_SECONDS`: a
    descendant that left the group could hold the pipe open."""
    try:
        out, _ = proc.communicate(timeout=KILL_DRAIN_SECONDS)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        out = b""
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=KILL_DRAIN_SECONDS)
    return (out or b"").decode("utf-8", errors="replace")


def sync_env(env: Mapping[str, str]) -> dict[str, str]:
    """The sync's environment: the allow-listed names of `env` (`_ENV_NAMES`,
    every `UV_*` and `XDG_*`), nothing else."""
    return {k: v for k, v in env.items()
            if k in _ENV_NAMES or k.startswith(_ENV_PREFIXES)}


def _environment(checkout: Path, env: Mapping[str, str] | None = None) -> Path:
    """Where uv puts the clone's environment: `UV_PROJECT_ENVIRONMENT` when the
    agent's environment sets it, else `<clone>/.venv`."""
    named = (env or {}).get("UV_PROJECT_ENVIRONMENT")
    if named:
        path = Path(named)
        return path if path.is_absolute() else checkout / path
    return checkout / ".venv"


def _lock_digest(checkout: Path) -> str:
    return hashlib.sha256((checkout / UV_LOCK).read_bytes()).hexdigest()


def is_ready(checkout: Path, env: Mapping[str, str] | None = None) -> bool:
    """True when the clone's environment carries the marker of a finished sync
    of this very lock by this very command."""
    try:
        marker = json.loads((_environment(checkout, env) / READY_MARKER).read_text())
        return (marker.get("lock_sha256") == _lock_digest(checkout)
                and marker.get("command") == list(WARM_SYNC_ARGV))
    except (OSError, ValueError, AttributeError):
        return False


def _mark_ready(checkout: Path, env: Mapping[str, str]) -> None:
    venv = _environment(checkout, env)
    with contextlib.suppress(OSError):
        if venv.is_dir():
            (venv / READY_MARKER).write_text(json.dumps(
                {"lock_sha256": _lock_digest(checkout), "command": list(WARM_SYNC_ARGV)}))


def applies(checkout: Path | None) -> bool:
    """True when there is a clone and it has a `uv.lock` at its root: the one
    case in which the warm-up runs."""
    return checkout is not None and (Path(checkout) / UV_LOCK).is_file()


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-OUTPUT_TAIL_LINES:])


class PythonEnvWarmup:
    """The attempt's one warm-up; `warm` runs it at most once."""

    def __init__(self, *, redact: Callable[[str], str], emit: Callable[[dict[str, Any]], None],
                 runner: SyncRunner = run_sync, clock: Callable[[], float] = time.monotonic,
                 deadline_seconds: float = WARM_DEADLINE_SECONDS) -> None:
        self._redact = redact
        self._emit = emit
        self._runner = runner
        self._clock = clock
        self._deadline = deadline_seconds
        self._asked = False

    def warm(self, checkout: Path | None, env: Mapping[str, str], *,
             may_start: Callable[[], bool],
             meanwhile: Callable[[str], ContextManager[Any]] | None = None) -> WarmResult | None:
        """Warm `checkout`'s environment, once. None when nothing applies: no
        clone, no `uv.lock` at its root, or this attempt already asked.

        `may_start` is asked before anything runs: False (a cancel was
        requested) records NOT_STARTED; a stale generation raises out of it,
        and nothing starts. `meanwhile(what)` wraps the sync (the lease's
        heartbeat). Every other outcome is recorded on one event and returned,
        never raised."""
        if self._asked or checkout is None or not applies(checkout):
            return None
        self._asked = True
        checkout = Path(checkout)
        if is_ready(checkout, env):
            return self._record(WarmResult(ALREADY_READY, 0.0))
        if not may_start():
            return self._record(WarmResult(
                NOT_STARTED, 0.0, reason="the task's cancel was requested before it started"))
        started = self._clock()
        try:
            with meanwhile(EVENT_PHASE) if meanwhile is not None else contextlib.nullcontext():
                ran = self._runner(WARM_SYNC_ARGV, checkout, sync_env(env), self._deadline)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            # uv missing or unstartable: a failed warm-up, never a failed attempt.
            return self._record(WarmResult(
                FAILED, self._clock() - started,
                reason=self._redact(f"{type(exc).__name__}: {exc}")[:300]))
        seconds = self._clock() - started
        tail = self._redact(_tail(ran.output))
        if ran.timed_out:
            return self._record(WarmResult(
                TIMED_OUT, seconds, output_tail=tail,
                reason=f"killed at its {self._deadline:g} s deadline "
                       f"(WARM_DEADLINE_SECONDS = {WARM_DEADLINE_SECONDS})"))
        if ran.code != 0:
            return self._record(WarmResult(
                FAILED, seconds, reason=f"uv sync ended with exit {ran.code}", output_tail=tail))
        _mark_ready(checkout, env)
        return self._record(WarmResult(READY, seconds))

    def _record(self, result: WarmResult) -> WarmResult:
        self._emit(result.as_detail())
        return result
