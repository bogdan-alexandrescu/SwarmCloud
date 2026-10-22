"""`python -m agent_worker.statemcp`: the MCP server, or one read from a shell.

    python -m agent_worker.statemcp [--spool <dir>]
    python -m agent_worker.statemcp agents_list --state FAILED --window-hours 7
    python -m agent_worker.statemcp agent_log_tail --agent-id task_... --stream agent_stderr

The spool is `--spool`, else `$SWARM_STATE`, which the worker exports only to
an attempt offered the state reads. The CLI form prints the worker's answer
as JSON and exits 0 when the read succeeded, 1 when it was refused, 2 on a
usage error -- so a runner without MCP has the same tools.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_worker.gitmcp.server import submit
from agent_worker.statemcp.server import (
    ARGUMENTS,
    SPOOL_ENV,
    TOOL_NAMES,
    TOOLS,
    StateServer,
)

#: The arguments that are integers; every other one is text.
_INTEGERS = frozenset({"window_hours", "limit"})


def _spool(value: str | None) -> Path | None:
    chosen = value or os.environ.get(SPOOL_ENV, "")
    return Path(chosen) if chosen else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in TOOL_NAMES:
        return _one_call(args[0], args[1:])
    parser = argparse.ArgumentParser(prog="swarm-state", description=__doc__)
    parser.add_argument("--spool", default=None, help=f"the spool directory (default ${SPOOL_ENV})")
    parsed = parser.parse_args(args)
    spool = _spool(parsed.spool)
    if spool is None:
        print(f"swarm-state: no spool: this attempt was not offered state reads "
              f"(${SPOOL_ENV} is unset)", file=sys.stderr)
        return 2
    StateServer(spool).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


def _one_call(tool: str, rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"swarm-state {tool}")
    parser.add_argument("--spool", default=None)
    required = next(t.get("inputSchema", {}).get("required", ()) for t in TOOLS
                    if t["name"] == tool)
    for name in sorted(ARGUMENTS[tool]):
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name,
                            type=int if name in _INTEGERS else str,
                            required=name in required)
    parsed = parser.parse_args(rest)
    spool = _spool(parsed.spool)
    if spool is None:
        print(f"swarm-state: no spool (${SPOOL_ENV} is unset)", file=sys.stderr)
        return 2
    arguments = {name: getattr(parsed, name) for name in sorted(ARGUMENTS[tool])
                 if getattr(parsed, name) is not None}
    answer = submit(spool, tool, arguments)
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 1 if "refused" in answer else 0


if __name__ == "__main__":
    sys.exit(main())
