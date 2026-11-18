"""`python -m agent_worker.searchmcp`: the MCP server, or one search from a shell.

    python -m agent_worker.searchmcp [--spool <dir>]
    python -m agent_worker.searchmcp search --query "python 3.13 release notes" [--count 5]

The spool is `--spool`, else `$SWARM_SEARCH`, which the worker exports only to
an attempt offered the search tool. The CLI form prints the worker's answer as
JSON and exits 0 when the search succeeded, 1 when it was refused, 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent_worker.gitmcp.server import submit
from agent_worker.searchmcp.server import SPOOL_ENV, TOOL_NAME, SearchServer


def _spool(value: str | None) -> Path | None:
    chosen = value or os.environ.get(SPOOL_ENV, "")
    return Path(chosen) if chosen else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == TOOL_NAME:
        return _one_call(args[1:])
    parser = argparse.ArgumentParser(prog="swarm-search", description=__doc__)
    parser.add_argument("--spool", default=None, help=f"the spool directory (default ${SPOOL_ENV})")
    parsed = parser.parse_args(args)
    spool = _spool(parsed.spool)
    if spool is None:
        print(f"swarm-search: no spool: this attempt was not offered the search tool "
              f"(${SPOOL_ENV} is unset)", file=sys.stderr)
        return 2
    SearchServer(spool).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


def _one_call(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"swarm-search {TOOL_NAME}")
    parser.add_argument("--spool", default=None)
    parser.add_argument("--query", required=True)
    parser.add_argument("--count", type=int)
    parsed = parser.parse_args(rest)
    spool = _spool(parsed.spool)
    if spool is None:
        print(f"swarm-search: no spool (${SPOOL_ENV} is unset)", file=sys.stderr)
        return 2
    arguments: dict[str, object] = {"query": parsed.query}
    if parsed.count is not None:
        arguments["count"] = parsed.count
    answer = submit(spool, TOOL_NAME, arguments)
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 1 if "refused" in answer else 0


if __name__ == "__main__":
    sys.exit(main())
