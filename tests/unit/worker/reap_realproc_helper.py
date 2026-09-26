"""Run the REAL pre-publish reap inside a private PID namespace.

Invoked by `test_procman_reap.py` under
``unshare --user --map-root-user --pid --fork --mount-proc``. Inside that
namespace this process is PID 1, so ``os.kill(-1, SIGKILL)`` can be exercised for
real without touching the test runner: it reaches only the victim this helper
spawned, never anything outside the namespace.

Not part of the product. It proves that `reap_foreign_processes`, with its real
killer and its real /proc verifier, kills an escaped process and then reports the
container clean.

Prints one line and exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import os
import signal
import sys
import time


class _SilentLogger:
    def info(self, *args, **kwargs) -> None:  # noqa: D401 - matches the worker logger shape
        pass

    def error(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass


def main() -> int:
    from agent_worker.procman import _live_foreign_pids, reap_foreign_processes

    # A victim in its own session, exactly like a process the agent double-forked
    # out of the runner's group. It just waits to be killed.
    victim = os.fork()
    if victim == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        while True:
            time.sleep(3600)
        os._exit(0)  # unreachable

    # Wait until the victim is visible as a live foreign process before reaping,
    # so the test is not racing the fork.
    for _ in range(200):
        if victim in _live_foreign_pids():
            break
        time.sleep(0.01)
    else:
        print("SETUP_FAILED victim never became visible", flush=True)
        return 3

    survivors = reap_foreign_processes(logger=_SilentLogger(), attempts=50, delay=0.02)

    # Reap the corpse so a lingering zombie does not read as alive.
    try:
        while True:
            reaped, _ = os.waitpid(-1, os.WNOHANG)
            if reaped == 0:
                break
    except ChildProcessError:
        pass

    victim_alive = True
    try:
        os.kill(victim, 0)
    except ProcessLookupError:
        victim_alive = False
    except OSError:
        victim_alive = False

    print(f"SURVIVORS={survivors} VICTIM_ALIVE={victim_alive}", flush=True)
    return 0 if (not survivors and not victim_alive) else 1


if __name__ == "__main__":
    sys.exit(main())
