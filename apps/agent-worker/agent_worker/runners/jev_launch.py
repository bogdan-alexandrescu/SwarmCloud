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

THE FORWARDER (SEC-MIGRATE-TYPESAFE, lane TS-MIGRATE-WORKER). With the
worker's `SWARM_TYPESAFE_INJECT` on, the file is not a key: it is the
worker's shim file (`agent_worker.typesafe_shim`, one JSON object of exactly
`shim_url` and `token`). Then JEV_API_URL is the shim's loopback URL, so
jev-browser's calls go to the worker's shim and on to the TypeSafe forwarder
with the attempt's current ticket, and TYPESAFE_API_KEY is the shim's local
token, which the shim checks and replaces: no TypeSafe key is in this
process. The URL is passed only when it is exactly the shim's shape
(127.0.0.1, a port, /v1/systemone). The image's wrapper drops JEV_API_URL for
the TypeSafe backend unless it passes the shim's URL through, which a wrapper
says by carrying SHIM_PASSTHROUGH_MARKER; against an older wrapper jev would
send to api.typesafe.ai with no key, so this refuses to start rather than
send anything. A key file -- the switch off -- is read exactly as before.
"""

from __future__ import annotations

import json
import os
import re
import sys

KEY_ENV = "TYPESAFE_API_KEY"
#: jev-browser's API address (`jev.mjs` API_URL), the shim's in shim mode.
URL_ENV = "JEV_API_URL"
#: `typesafe_shim.SHIM_URL_FIELD` / `TOKEN_FIELD`, restated: this file runs
#: with the standard library alone. tests/unit/worker/test_typesafe_shim.py
#: holds them equal.
SHIM_URL_FIELD = "shim_url"
TOKEN_FIELD = "token"
SHIM_URL = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}/v1/systemone")
#: What the image's `bin/jev-browser-mcp` carries once it passes the shim's
#: URL through for the TypeSafe backend (the patch in this lane's PR body).
SHIM_PASSTHROUGH_MARKER = "swarm-typesafe-shim-passthrough/v1"


def _shim(text: str) -> tuple[str, str] | None:
    """`(url, token)` when `text` is the worker's shim file, else None."""
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != {SHIM_URL_FIELD, TOKEN_FIELD}:
        return None
    url, token = parsed[SHIM_URL_FIELD], parsed[TOKEN_FIELD]
    if not isinstance(url, str) or not isinstance(token, str) or not token:
        return None
    return url, token


def _passes_shim_url(server: str) -> bool:
    try:
        with open(server, "rb") as handle:
            return SHIM_PASSTHROUGH_MARKER.encode("ascii") in handle.read(1 << 20)
    except OSError:
        return False


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
    shim = _shim(key)
    if shim is not None:
        url, token = shim
        if not SHIM_URL.fullmatch(url):
            sys.stderr.write(f"jev_launch: the shim file {key_file} names no loopback shim\n")
            return 1
        if not _passes_shim_url(server):
            sys.stderr.write("jev_launch: refused: this image's jev-browser-mcp drops the "
                             "worker shim's URL, so jev would call TypeSafe with no key; "
                             "jev is unavailable on this attempt\n")
            return 1
        env[URL_ENV] = url
        env[KEY_ENV] = token
        os.execve(server, [server, *args], env)
        return 1  # pragma: no cover -- execve does not return
    env[KEY_ENV] = key
    os.execve(server, [server, *args], env)
    return 1  # pragma: no cover -- execve does not return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
