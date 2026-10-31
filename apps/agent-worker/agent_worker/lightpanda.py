"""Lightpanda for the `claude-code-lightpanda` profile (docs/design/
lightpanda-runtime.md §6.2; contract request 127; part of #2688).

The profile runs the claude-code runner on `agent-runtime-lightpanda`, which
is agent-runtime-base plus the Lightpanda binary at `BINARY`. This module holds
what the runner's one branch (`runners/claude_code.py`) needs, as constants and
pure helpers, so each is testable without the CLI:

  * the `serve` child: `lightpanda serve` on 127.0.0.1:9223, started under
    `env -i` with only `CHILD_ENV_NAMES` set. The scrubbed environment means no
    token or secret is reachable by Lightpanda or by a page it loads, and
    `--block-private-networks` keeps a page's script off the metadata server
    and the pod network. Port 9223, never the live gate's 9222, so nothing
    written for the live browser's endpoint reaches Lightpanda by mistake;
  * `Supervisor`: restarts `serve` when it exits, at most `MAX_RESTARTS` times
    per attempt with `RESTART_FLOOR_SECONDS` between starts, one log line per
    exit and never the environment. It is started by the runner, which the
    worker starts only after the fencing check, and in the runner's own process
    group, so the worker's group kill of the attempt takes it too;
  * the `lightpanda` MCP server: `lightpanda mcp` under the same `env -i`.
    There is no `playwright` server on this profile;
  * `DISALLOWED_TOOLS`: `screenshot` crashes on heavy pages and its picture is
    not the page; `search` would call Brave without a key (the CLI's WebSearch
    serves); `getEnv` has nothing to return under `env -i`;
  * `LIGHTPANDA_NOTE`, appended to the prompt. There is no live view here, so
    `browser_tools.write_state` is never called and no `browser.json` exists.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: The catalogue's name (apps/common/swarm_common/profiles.py, request 127).
PROFILE = "claude-code-lightpanda"
#: The runner's profile, which the worker sets on the runner process
#: (`lifecycle._build_child_env`) from its own `RUNNER_PROFILE`: never input.
PROFILE_ENV = "SWARM_RUNNER_PROFILE"

#: Where images/agent-runtime-lightpanda/Dockerfile installs the binary.
BINARY = "/opt/swarm/lightpanda/lightpanda"
#: `env` by its path, so a PATH the agent can write never chooses it.
ENV_BIN = "/usr/bin/env"

HOST = "127.0.0.1"
#: Not 9222: that is the live gate's CDP port.
CDP_PORT = 9223
CDP_URL = f"http://{HOST}:{CDP_PORT}"
#: What the agent's Python connects to. `SWARM_CDP_ENDPOINT` is the live
#: browser's, and is never set on this profile.
CDP_ENV = "SWARM_LIGHTPANDA_CDP"

MCP_SERVER_NAME = "lightpanda"

#: The child's whole environment, in this order. The two LIGHTPANDA_* values
#: are fixed (`FIXED_ENV`); the image sets them too, but `env -i` drops it.
CHILD_ENV_NAMES = ("PATH", "HOME", "TMPDIR", "LIGHTPANDA_DISABLE_TELEMETRY",
                   "LIGHTPANDA_DISABLE_CORE_DUMP")
FIXED_ENV = {"LIGHTPANDA_DISABLE_TELEMETRY": "true", "LIGHTPANDA_DISABLE_CORE_DUMP": "1"}
#: PATH when the runner has none: the base image's system directories.
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
#: The child's HOME, under the attempt's cache directory.
HOME_DIR_NAME = "lightpanda"

SERVE_ARGS = ("serve", "--host", HOST, "--port", str(CDP_PORT), "--cdp-max-connections", "64",
              "--block-private-networks", "--log-level", "warn")
MCP_ARGS = ("mcp", "--block-private-networks")

DISALLOWED_TOOLS = (
    f"mcp__{MCP_SERVER_NAME}__screenshot",
    f"mcp__{MCP_SERVER_NAME}__search",
    f"mcp__{MCP_SERVER_NAME}__getEnv",
)

#: Restarts of `serve` per attempt after its first start, and the least time
#: between two starts.
MAX_RESTARTS = 20
RESTART_FLOOR_SECONDS = 1.0
#: How long a stopped `serve` has after SIGTERM before SIGKILL.
STOP_GRACE_SECONDS = 5.0

LIGHTPANDA_NOTE = (
    "Browser for this task: Lightpanda, a headless browser that runs "
    "JavaScript but draws no pixels. Read and act on pages with the lightpanda "
    "tools (goto, markdown, links, tree, interactiveElements, click, fill, "
    "press, waitForSelector). For many pages at once, script Playwright from "
    "Python: `playwright.chromium.connect_over_cdp(os.environ[\"SWARM_LIGHTPANDA_CDP\"])`, "
    "one connection per page (Lightpanda allows one browser context per "
    "connection). There is no live view, no screenshot and no hand-off here. "
    "Lightpanda does not load iframes, and a page's text includes text the page "
    "hides. For a web search, use the WebSearch tool. When a page fails here (an "
    "error, an empty page, content in an iframe, a sign-in, a CAPTCHA or a "
    "consent wall, or anything you must see), do not work around it: submit a "
    "child agent on runner_profile `claude-code-browser` through "
    "$SWARM_CHILDREN with those URLs and what to do there, wait for it, and say "
    "in your answer which pages went there and why. On any one site, keep to "
    "about one navigation a second."
)


def is_lightpanda(environ: Mapping[str, str]) -> bool:
    """Whether this runner runs the `claude-code-lightpanda` profile."""
    return environ.get(PROFILE_ENV, "").strip() == PROFILE


def child_env(environ: Mapping[str, str], cache_home: Path) -> dict[str, str]:
    """The Lightpanda child's whole environment: `CHILD_ENV_NAMES` and nothing
    else. HOME is `<cache_home>/lightpanda`; PATH and TMPDIR are the runner's
    own values (they name directories, never a credential)."""
    return {
        "PATH": environ.get("PATH", "").strip() or DEFAULT_PATH,
        "HOME": str(cache_home / HOME_DIR_NAME),
        "TMPDIR": environ.get("TMPDIR", "").strip() or "/tmp",
        **FIXED_ENV,
    }


def _env_args(env: Mapping[str, str]) -> list[str]:
    return ["-i", *(f"{name}={env[name]}" for name in CHILD_ENV_NAMES)]


def serve_argv(env: Mapping[str, str], binary: str = BINARY) -> list[str]:
    """`env -i <allow-list> lightpanda serve ...`."""
    return [ENV_BIN, *_env_args(env), binary, *SERVE_ARGS]


def mcp_server(env: Mapping[str, str], binary: str = BINARY) -> dict[str, Any]:
    """The `--mcp-config` entry for `lightpanda mcp`, under the same `env -i`.
    No `env` key: the CLI's environment, which holds the provider token, is
    dropped by `env -i` before Lightpanda starts."""
    return {"type": "stdio", "command": ENV_BIN, "args": [*_env_args(env), binary, *MCP_ARGS]}


def disallowed_tools_args() -> list[str]:
    """The CLI flag that denies `DISALLOWED_TOOLS`. Variadic in the CLI, so the
    caller puts another flag right after it."""
    return ["--disallowedTools", ",".join(DISALLOWED_TOOLS)]


class Supervisor:
    """Keeps one `serve` child running for the attempt (module docstring).

    `start` returns at once; a thread starts the child, waits for it, logs its
    exit and starts it again, at most `max_restarts` times and never sooner
    than `floor` seconds after the previous start. `stop` ends the thread and
    the child. The log line carries the exit code and counts, never argv or
    environment.
    """

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        log: Callable[..., None],
        max_restarts: int = MAX_RESTARTS,
        floor: float = RESTART_FLOOR_SECONDS,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._argv = list(argv)
        self._env = dict(env)
        self._log = log
        self._max_restarts = max_restarts
        self._floor = floor
        self._popen = popen
        self._clock = clock
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._proc: Any = None
        self._thread: threading.Thread | None = None
        #: How many times the child was started, and how many exits were seen.
        self.starts = 0
        self.exits = 0

    def start(self) -> None:
        home = self._env.get("HOME")
        if home:
            Path(home).mkdir(mode=0o700, parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="lightpanda-serve", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        last_start: float | None = None
        while not self._stopping.is_set():
            if last_start is not None:
                wait = self._floor - (self._clock() - last_start)
                if wait > 0 and self._stopping.wait(wait):
                    return
            with self._lock:
                if self._stopping.is_set():
                    return
                last_start = self._clock()
                self.starts += 1
                try:
                    # Not a new session: the child stays in the runner's
                    # process group, which the worker kills with the attempt.
                    self._proc = self._popen(self._argv, env=self._env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL)
                except OSError as exc:
                    self._proc = None
                    code: Any = None
                    reason = type(exc).__name__
                else:
                    reason = ""
            if self._proc is not None:
                code = self._proc.wait()
            if self._stopping.is_set():
                return
            self.exits += 1
            restarts = self.starts - 1
            gave_up = restarts >= self._max_restarts
            self._log("lightpanda_exited", returncode=code, restarts=restarts,
                      max_restarts=self._max_restarts, gave_up=gave_up,
                      **({"reason": reason} if reason else {}))
            if gave_up:
                return

    def stop(self) -> None:
        with self._lock:
            self._stopping.set()
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.join(STOP_GRACE_SECONDS)
