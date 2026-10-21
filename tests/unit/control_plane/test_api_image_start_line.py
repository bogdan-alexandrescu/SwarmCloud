"""swarm-api's image prints one line before it starts the server (#1816 (e)).

Five swarm-api starts were killed on 2026-10-21 and logged nothing, so nobody
could tell whether the container ever ran or the server was merely slow to
bind. The image now writes one line to stdout before it execs uvicorn: the
next failed start shows which. These read the Dockerfile's ENTRYPOINT; they
need no Docker, no credentials and no emulator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "images/swarm-api/Dockerfile"


def _entrypoint_script() -> str:
    lines = [ln for ln in DOCKERFILE.read_text().splitlines() if ln.startswith("ENTRYPOINT ")]
    assert len(lines) == 1, f"expected one ENTRYPOINT, found {len(lines)}"
    argv = json.loads(lines[0][len("ENTRYPOINT ") :])
    assert argv[:4] == ["/usr/bin/tini", "--", "sh", "-c"], argv
    assert len(argv) == 5, argv
    return argv[4]


def test_a_start_up_line_is_printed_before_uvicorn_is_execed():
    script = _entrypoint_script()
    echo = script.find('echo "swarm-api container started')
    exec_at = script.find("exec uvicorn ")
    # MUTATION: remove the echo from the ENTRYPOINT and this fails.
    assert echo != -1, f"no start-up line in the ENTRYPOINT: {script!r}"
    # MUTATION: move the echo after `exec uvicorn` (it would never run) and this fails.
    assert exec_at != -1 and echo < exec_at, f"the start-up line must come before exec uvicorn: {script!r}"


def test_uvicorn_is_still_execed_with_the_same_arguments():
    script = _entrypoint_script()
    # MUTATION: drop `exec` (uvicorn becomes a grandchild of tini and misses
    # SIGTERM) or change its arguments, and this fails.
    assert script.endswith(
        "exec uvicorn --factory swarm_api.main:create_app --host 0.0.0.0 --port ${PORT:-8080}"
    ), script


def test_the_start_up_line_expands_nothing_but_the_date():
    script = _entrypoint_script()
    line = script[: script.find("exec uvicorn ")]
    # Only $(date ...) may expand in the printed line: a $VAR there could carry
    # an environment value (a secret_env one included) into the logs.
    expansions = re.findall(r"\$\(?[^)\s\"]*", line)
    # MUTATION: add `$PORT` or `$SWARM_SETTINGS_KEY` to the echo and this fails.
    assert expansions == ["$(date"], f"the start-up line expands {expansions!r}"
    assert "env" not in line.split() and "printenv" not in line, line
