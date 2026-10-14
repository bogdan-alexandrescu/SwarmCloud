"""Start jev-browser's MCP server with the TypeSafe key in ITS environment only.

    python -I jev_launch.py <key file> <jev-browser-mcp> [args...]

The claude-code runner names this file, by absolute path, as the `jev`
server's command in the MCP config (`claude_code.write_mcp_config`), so the
config carries the key file's PATH and never the key. It reads the file the
worker wrote (`agent_worker.jevkey`), puts the key in TYPESAFE_API_KEY, and
execs the server: that one process is the only environment the key is in.

It does not delete the file. The CLI starts its MCP servers again on every
launch of an attempt -- a pending-work resume, a repair turn, an account move
(`cliagent`) -- and each start reads it. The runner removes it when the agent
has finished (`claude_code.body`), and it lives in `private/`, which is never
checkpointed and is destroyed with the workspace.

Standard library only, run with the runner's own interpreter in isolated
mode. A missing or empty file is a one-line refusal naming the file, never
its content.
"""

from __future__ import annotations

import os
import sys

KEY_ENV = "TYPESAFE_API_KEY"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("jev_launch: usage: jev_launch.py KEY_FILE SERVER [ARGS...]\n")
        return 2
    key_file, server, *args = argv
    try:
        with open(key_file, encoding="utf-8") as handle:
            key = handle.read().strip()
    except OSError as exc:
        sys.stderr.write(f"jev_launch: cannot read the TypeSafe key file {key_file} "
                         f"({type(exc).__name__})\n")
        return 1
    if not key:
        sys.stderr.write(f"jev_launch: the TypeSafe key file {key_file} is empty\n")
        return 1
    env = dict(os.environ)
    env[KEY_ENV] = key
    os.execve(server, [server, *args], env)
    return 1  # pragma: no cover -- execve does not return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
