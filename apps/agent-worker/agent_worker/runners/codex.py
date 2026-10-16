"""Runner: OpenAI Codex CLI, non-interactive.

Same shape as the Claude Code runner -- deliberately, because the two profiles
must behave identically from the platform's point of view: the same workspace,
the same artifact layout, the same rate-limit handling, the same park decision.
Only the binary, the flags and the credential differ, and all three are data.

THE GIT TOOLS (docs/runner-git-tools.md §1.3, lane G6). When the worker sets
`SWARM_GIT` -- only for an attempt offered the runner git tools -- the
`swarm-git` shim is named in codex's MCP configuration as `-c` overrides on
`codex exec`, the same server claude-code gets in `--mcp-config`. Overrides,
not `$HOME/.codex/config.toml`: HOME is `work/`, which a checkpoint restores,
so a file written there would be the agent's to edit for the next attempt and
would outlive an attempt that is not offered the tools.

THE GRAPH TOOLS (docs/design/context-quality.md §4.4, lane CTXQ-S). When the
worker staged the repository's graph snapshot it sets `SWARM_GRAPH_SNAPSHOT`,
and `swarm-graph` is named the same way, as `-c mcp_servers.swarm-graph.*`.
`SWARM_GRAPH_UNSTAGED` adds the one prompt line claude-code adds.

SERVED INSTEAD OF STAGED (docs/design/graph-service.md §4.2, lane GS-WORKER).
With `SWARM_GRAPH_PIN` and `SWARM_GRAPH_TICKET_FILE` set, the same keys name
the pod's shim (`graphstage.shim_argv`), plus
`-c mcp_servers.swarm-graph.env={"SWARM_GRAPH_URL" = …}`: a TOML inline table,
because codex parses every `-c` value as TOML (the design's sketch wrote it
as JSON, which TOML does not read as a table).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .. import graphstage
from ..children import git_shim_argv
from ..gitmcp.server import SERVER_NAME as GIT_MCP_SERVER_NAME
from ..gitmcp.server import SPOOL_ENV as GIT_SPOOL_ENV
from .base import RunnerContext, run_runner
from .cliagent import CliAgentSpec, run_cli_agent

#: `$HOME/.codex/sessions/YYYY/MM/DD/rollout-<local time>-<session uuid>.jsonl`,
#: one per session, appended to as it runs and again when it is resumed.
_ROLLOUT_GLOB = ".codex/sessions/*/*/*/rollout-*.jsonl"
_ROLLOUT_NAME = re.compile(
    r"^rollout-.+-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.jsonl$"
)


def latest_session(home: Path, since: float) -> str | None:
    """The session of the newest rollout written at or after `since`, or None.

    `codex exec` names its session only here: its stdout is the final answer
    in prose. NOT the newest rollout of all: a checkpoint restores `.codex/`
    with the workspace (`checkpoint.py`), so the previous attempt's session is
    on disk before this run writes a line, and resuming it would continue a
    conversation this attempt never had. A link is not followed -- the agent
    owns this directory -- and the id comes from the name, which codex writes
    and the `_SESSION_ID` shape check in `cliagent` still guards.
    """
    best: tuple[float, str] | None = None
    for path in home.glob(_ROLLOUT_GLOB):
        match = _ROLLOUT_NAME.match(path.name)
        if match is None:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_mtime < since:
            continue
        if best is None or info.st_mtime > best[0]:
            best = (info.st_mtime, match.group(1))
    return best[1] if best else None


SPEC = CliAgentSpec(
    name="codex",
    provider="openai",
    binary_env="CODEX_BIN",
    binary_default="codex",
    args_env="CODEX_ARGS",
    args_default=("exec", "--skip-git-repo-check"),
    key_env="OPENAI_API_KEY",
    model_flag="--model",
    transcript_name="codex-transcript.json",
    # `codex exec -` reads the prompt from stdin, where `cliagent` writes it:
    # never in the argv, which every process listing shows (owner decision
    # 2026-10-06, observer P9).
    stdin_arg=("-",),
    # A refused credential is reloaded and the session CONTINUED, not started
    # again from the prompt (#626): `codex exec [flags] resume <id> -`, the
    # flags still the parent `exec`'s, the reload's message on stdin.
    resume_flag="resume",
    # Its stdout does not name the session; its rollout does.
    session_locator=latest_session,
)


def git_tools_args(spool: str) -> list[str]:
    """`-c` overrides registering the `swarm-git` shim as a stdio MCP server.

    Each value is TOML; a JSON string and a JSON array of strings are both
    valid TOML, so `json.dumps` quotes them.
    """
    command, *args = git_shim_argv(spool)
    key = f"mcp_servers.{GIT_MCP_SERVER_NAME}"
    return ["-c", f"{key}.command={json.dumps(command)}",
            "-c", f"{key}.args={json.dumps(args)}"]


def graph_tools_args(snapshot: str, workdir: str | None) -> list[str]:
    """`-c` overrides registering `swarm-graph`, as `git_tools_args` does the shim."""
    command, *args = graphstage.server_argv(snapshot, workdir)
    key = f"mcp_servers.{graphstage.SERVER_NAME}"
    return ["-c", f"{key}.command={json.dumps(command)}",
            "-c", f"{key}.args={json.dumps(args)}"]


def graph_shim_args(pin: str, ticket_file: str, url: str, workdir: str | None) -> list[str]:
    """`-c` overrides registering the graph shim as `swarm-graph`, its URL in `env`."""
    command, *args = graphstage.shim_argv(pin, ticket_file, workdir)
    key = f"mcp_servers.{graphstage.SERVER_NAME}"
    env = "{" + f"{json.dumps(graphstage.URL_ENV)} = {json.dumps(url)}" + "}"
    return ["-c", f"{key}.command={json.dumps(command)}",
            "-c", f"{key}.args={json.dumps(args)}",
            "-c", f"{key}.env={env}"]


def body(ctx: RunnerContext) -> dict[str, Any]:
    spool = os.environ.get(GIT_SPOOL_ENV, "").strip()
    extra_args: list[str] = []
    extra_env: dict[str, str] = {}
    if spool:
        extra_args += git_tools_args(spool)
        extra_env[GIT_SPOOL_ENV] = spool
    snapshot = os.environ.get(graphstage.SNAPSHOT_ENV, "").strip()
    served = graphstage.served_env(os.environ)
    workdir = str(ctx.repo_dir) if ctx.repo_dir else None
    if served:
        extra_args += graph_shim_args(*served, workdir)
    elif snapshot:
        extra_args += graph_tools_args(snapshot, workdir)
    if served or snapshot:
        context = os.environ.get(graphstage.CONTEXT_ENV, "").strip()
        if context:
            extra_env[graphstage.CONTEXT_ENV] = context
    unstaged = graphstage.unstaged_note(os.environ)
    prompt = ctx.payload.get("prompt")
    if unstaged and isinstance(prompt, str) and prompt.strip():
        # A new dict on the same context: `run_runner` holds this object.
        ctx.payload = {**ctx.payload, "prompt": f"{prompt.rstrip()}\n\n{unstaged}"}
    if not extra_args and not extra_env:
        return run_cli_agent(ctx, SPEC)
    return run_cli_agent(ctx, SPEC, extra_args=extra_args, extra_env=extra_env)


def main() -> int:
    return run_runner(body, name="codex")


if __name__ == "__main__":
    raise SystemExit(main())
