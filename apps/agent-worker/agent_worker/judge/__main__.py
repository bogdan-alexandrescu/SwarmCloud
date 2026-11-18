"""`python -I -m agent_worker.judge --config <attempt>/judge/config.json`.

The `swarm-jev` MCP server (`register.server_argv`). It refuses to start on a
config it cannot read, so a missing one is a failed server the agent's CLI
reports, not a tool answering from nothing.
"""

from __future__ import annotations

import argparse
import sys

from agent_worker.judge.mcp import ConfigError, JevServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarm-jev", description=__doc__)
    parser.add_argument("--config", required=True, help="the attempt's judge/config.json")
    args = parser.parse_args(argv)
    try:
        server = JevServer.from_config(args.config)
    except ConfigError as exc:
        print(f"swarm-jev: {exc}", file=sys.stderr)
        return 2
    server.serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
