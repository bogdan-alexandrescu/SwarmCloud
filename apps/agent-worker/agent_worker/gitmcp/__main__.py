"""`python -m agent_worker.gitmcp`: the MCP server, or one call from a shell.

    python -m agent_worker.gitmcp [--spool <dir>]
    python -m agent_worker.gitmcp git_push_branch [--branch swarm/...]
    python -m agent_worker.gitmcp git_rebase_onto --ref main

The spool is `--spool`, else `$SWARM_GIT`, which the worker exports only to an
attempt offered git tools. The CLI form prints the worker's answer as JSON and
exits 0 when the call succeeded, 1 when it was refused, 2 on a usage error --
so the `generic` runner, which speaks no MCP, has the same five tools.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_worker.gitmcp.server import SPOOL_ENV, TOOL_NAMES, GitServer, submit


def _spool(value: str | None) -> Path | None:
    chosen = value or os.environ.get(SPOOL_ENV, "")
    return Path(chosen) if chosen else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in TOOL_NAMES:
        return _one_call(args[0], args[1:])
    parser = argparse.ArgumentParser(prog="swarm-git", description=__doc__)
    parser.add_argument("--spool", default=None, help=f"the spool directory (default ${SPOOL_ENV})")
    parsed = parser.parse_args(args)
    spool = _spool(parsed.spool)
    if spool is None:
        print(f"swarm-git: no spool: this attempt was not offered git tools "
              f"(${SPOOL_ENV} is unset)", file=sys.stderr)
        return 2
    GitServer(spool).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


def _one_call(tool: str, rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"swarm-git {tool}")
    parser.add_argument("--spool", default=None)
    parser.add_argument("--ref")
    parser.add_argument("--branch")
    parser.add_argument("--repo")
    parser.add_argument("--path")
    parser.add_argument("--pr", type=int)
    parsed = parser.parse_args(rest)
    spool = _spool(parsed.spool)
    if spool is None:
        print(f"swarm-git: no spool (${SPOOL_ENV} is unset)", file=sys.stderr)
        return 2
    arguments = {
        name: getattr(parsed, name)
        for name in ("ref", "branch", "repo", "path", "pr")
        if getattr(parsed, name) is not None
    }
    answer = submit(spool, tool, arguments)
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 1 if "refused" in answer else 0


if __name__ == "__main__":
    sys.exit(main())
