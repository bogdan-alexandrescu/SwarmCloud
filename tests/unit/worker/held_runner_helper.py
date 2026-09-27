"""The catalogue's runner, held alive at its end until the test lets it exit.

Used by `test_cpu_sampler.py`
(`test_a_run_writes_the_attempts_own_figures_while_it_runs_and_at_the_end`).
That test proves the worker writes an attempt's CPU figures while the runner
is still alive. Whether a periodic reading lands inside a short run's lifetime
is a race between two clocks, and it went the wrong way on CI. So this process
runs the real runner module in-process -- its CPU burn, its sleeps, its
result.json, its exit code -- and then, instead of exiting, waits for GATE to
exist. The test creates GATE when it sees a write made while this process was
alive.

BOUNDED. If GATE does not appear within BOUND seconds, this writes GAVE_UP and
exits anyway. A worker that has stopped writing while the runner runs then
fails the test with its own message, instead of holding the test until the
task timeout.

    python held_runner_helper.py GATE GAVE_UP BOUND -m agent_worker.runners.mock

Not part of the product.
"""

from __future__ import annotations

import runpy
import sys
import time
from pathlib import Path


def main() -> None:
    gate, gave_up, bound = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
    if sys.argv[4:5] != ["-m"] or len(sys.argv) < 6:
        raise SystemExit(f"expected GATE GAVE_UP BOUND -m MODULE, got {sys.argv[1:]!r}")
    module = sys.argv[5]
    sys.argv = [module, *sys.argv[6:]]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        code: object = 0
    except SystemExit as exc:
        code = exc.code
    deadline = time.monotonic() + bound
    while not gate.exists():
        if time.monotonic() >= deadline:
            gave_up.touch()
            break
        time.sleep(0.05)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
