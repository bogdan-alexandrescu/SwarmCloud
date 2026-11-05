"""The machine's own watchdog (docs/design/voice-it-own-speech.md §4.5, stop 2).

Runs on the HOST, not in the container, every minute from
swarm-speech-watchdog.timer, with the host's own Python 3.10 (Ubuntu 22.04's)
and the standard library only: it must work when the server's image cannot be
pulled or the container will not start. It powers the machine off
(`shutdown -h now`, which leaves the instance TERMINATED and ends GPU billing)
after IDLE_SECONDS with no open authenticated socket, counted from the boot or
from the last moment the server said a socket was open (activity.py's file).

It is the last of three stops (§4.5): swarm-api's sweep and the ledger's cap
stop the machine through the Compute API; this one needs neither swarm-api nor
the network, so a machine swarm-api has forgotten still stops.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = Path("/var/lib/swarm-speech/state/activity.json")
# Ten minutes (§4.5): longer than the boot's slowest path to `ready` outside a
# model fetch (start, pull, load, warm-up; M1 measures it), so a machine
# swarm-api has just started is never stopped before swarm-api's socket opens.
# A first fetch of a model that is not on disk can take longer; swarm-api holds
# its socket open through the boot (that socket is how it reads the phases),
# so a long fetch counts as an open socket and is not cut short.
IDLE_SECONDS = 600
# A file that says a socket is open is believed for this long: three of the
# server's 30 s refreshes. Older, the server has stopped refreshing it (a
# crash), and the file's time is the last proof of a session.
FRESH_SECONDS = 90

log = logging.getLogger("swarm_speech.watchdog")


def read_state(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return {"open": int(raw.get("open", 0)), "at": float(raw.get("at", 0.0))}
    except (TypeError, ValueError):
        return None


def idle_since(now: float, boot_time: float, state: dict | None) -> float | None:
    """When the machine became idle, or None while a socket is open."""
    # An open claim counts only if this boot's server wrote it: a stop and a
    # start inside FRESH_SECONDS must not inherit the last boot's socket.
    if state and state["open"] > 0 and state["at"] > boot_time and now - state["at"] < FRESH_SECONDS:
        return None
    since = boot_time
    if state and state["at"] > boot_time:
        since = state["at"]
    return since


def should_power_off(now: float, boot_time: float, state: dict | None) -> bool:
    since = idle_since(now, boot_time, state)
    return since is not None and now - since >= IDLE_SECONDS


def boot_time(proc: Path = Path("/proc"), clock=time.time) -> float:
    uptime = float((proc / "uptime").read_text(encoding="ascii").split()[0])
    return clock() - uptime


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(name)s %(message)s")
    now = time.time()
    state = read_state(STATE_FILE)
    started = boot_time()
    if not should_power_off(now, started, state):
        return 0
    since = idle_since(now, started, state)
    log.info("no open speech socket for %d s: powering off", int(now - (since or now)))
    subprocess.run(["/usr/sbin/shutdown", "-h", "now"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
