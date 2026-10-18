"""Set up an attempt's Jev: the shared ledger, and the agent's `jev_decide` tool.

Lane CTXQ-JW wires this into the runners (`runners/claude_code.py`'s
`write_mcp_config` takes the `servers` mapping this adds to); this lane only
exposes it.

* `prepare` resolves the backend from the stored settings and creates the
  attempt's ledger. The worker's own calls (§5.2's J0-J5) go through
  `worker_client`.
* `register_jev_tool` does the same and adds the `swarm-jev` server to the
  agent's MCP servers -- ONLY when the workspace opted in to Jev for code and
  the repository did not turn it off (owner decision 2026-10-14). The tool
  shares the worker's ledger, so its calls count against the same 8 calls and
  120k tokens.

THE KEY, PER BACKEND.
* Internal Jev / Internal Laya (the default): no credential exists. Nothing
  is written but the config and the ledger.
* TypeSafe: the workspace's key is written to `<attempt>/private/jev-key`,
  mode 0600, and the config names the file. The tool process reads it, and
  that process is the agent's child, running as the agent's user: THE AGENT
  CAN READ THE KEY. That is the residual the owner accepted when choosing the
  MCP tool over the worker-only design (context-quality.md §5.1, override
  note). The key is never in the config, the MCP entry, an environment
  variable, a log or an audit row.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from agent_worker.judge import budget, client, settings as settings_mod
from agent_worker.judge.mcp import SERVER_NAME

JUDGE_DIR = "judge"
CONFIG_FILE = "config.json"
LEDGER_FILE = "ledger.json"
KEY_DIR = "private"
KEY_FILE = "jev-key"
CONFIG_VERSION = 1
MODULE = "agent_worker.judge"


@dataclass(frozen=True)
class Registered:
    registered: bool
    #: Why not (the pack's `jev: off (reason)`), or None.
    reason: str | None = None
    config_path: Path | None = None
    ledger_path: Path | None = None
    backend: settings_mod.Backend | None = field(default=None, repr=False)
    hard_stop_paths: tuple[str, ...] = ()


def _write_private(path: Path, text: str) -> None:
    """Mode 0600, replaced rather than followed: a link planted at the name
    is removed, never written through."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


def prepare(*, attempt_dir: Path, settings: settings_mod.JudgeSettings, key: str | None,
            environ: Mapping[str, str] | None = None, hard_stop_paths: Iterable[str] = (),
            secret_env_names: Iterable[str] = (), month_used_tokens: int = 0,
            task_calls: int = 0, task_input_tokens: int = 0,
            shim_url: str | None = None) -> Registered:
    """The attempt's Jev, or why there is none. Writes nothing when off.

    `month_used_tokens` is the workspace's spend this month, read by the
    caller (`budget.FirestoreMonthlyLedger.used`); `task_calls` and
    `task_input_tokens` carry an earlier attempt's spend on the same task.
    `shim_url` is the worker's TypeSafe shim (`settings.typesafe_url`); with
    it, `key` is the shim's local token and is written where a key would be.
    """
    chosen = settings_mod.resolve_backend(settings, key=key, environ=environ,
                                          shim_url=shim_url)
    if isinstance(chosen, settings_mod.Off):
        return Registered(False, chosen.reason)
    attempt_dir = Path(attempt_dir)
    home = attempt_dir / JUDGE_DIR
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    ledger = budget.Ledger.create(home / LEDGER_FILE,
                                  monthly_cap_tokens=settings.monthly_cap_tokens,
                                  month_used_tokens=month_used_tokens, calls=task_calls,
                                  input_tokens=task_input_tokens)
    key_path: Path | None = None
    if chosen.key:
        key_path = attempt_dir / KEY_DIR / KEY_FILE
        _write_private(key_path, chosen.key)
    stops = tuple(p for p in hard_stop_paths if isinstance(p, str) and p)
    config = {
        "version": CONFIG_VERSION,
        "backend": {"name": chosen.name, "url": chosen.url, "model": chosen.model,
                    "stringify_state": chosen.stringify_state},
        "key_file": str(key_path) if key_path else None,
        "ledger": str(ledger.path),
        "hard_stop_paths": list(stops),
        # NAMES only: the tool process reads the values from the environment
        # it inherits from the agent, which already holds them.
        "secret_env_names": [n for n in secret_env_names if isinstance(n, str) and n],
    }
    config_path = home / CONFIG_FILE
    _write_private(config_path, json.dumps(config, indent=2) + "\n")
    return Registered(True, None, config_path, ledger.path, chosen, stops)


def server_argv(config_path: Path, python: str = sys.executable) -> list[str]:
    """This interpreter, `-I`, the module and its config: `search_shim_argv`'s shape."""
    return [python, "-I", "-m", MODULE, "--config", str(config_path)]


def register_jev_tool(servers: dict[str, Any], **kwargs: Any) -> Registered:
    """`prepare`, then add `swarm-jev` to `servers` (the `mcpServers` mapping).
    Leaves `servers` untouched when Jev is off for this task."""
    done = prepare(**kwargs)
    if done.registered and done.config_path is not None:
        command, *args = server_argv(done.config_path)
        servers[SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    return done


def worker_client(done: Registered, *, secrets: Iterable[str] = (),
                  transport: client.Transport = client.urllib_transport,
                  sleep: Callable[[float], None] | None = None,
                  log: Callable[..., None] | None = None) -> client.JudgeClient:
    """The worker's own client over the attempt's shared ledger."""
    if not done.registered or done.backend is None or done.ledger_path is None:
        raise ValueError("Jev is off for this attempt; check Registered.registered first")
    extra: dict[str, Any] = {}
    if sleep is not None:
        extra["sleep"] = sleep
    if log is not None:
        extra["log"] = log
    return client.JudgeClient(done.backend, budget.Ledger(done.ledger_path), transport=transport,
                              hard_stop_paths=done.hard_stop_paths,
                              secrets=tuple(s for s in secrets if isinstance(s, str) and s),
                              **extra)
