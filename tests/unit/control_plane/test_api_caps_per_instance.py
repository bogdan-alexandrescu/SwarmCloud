"""swarm-api's per-process caps hold per INSTANCE, whatever SWARM_API_WORKERS is.

The owner chose on 2026-11-10 to run two uvicorn workers on dev once these
caps are made per instance. Each worker calls create_app() itself, so every
in-process semaphore and token bucket is per worker: at two workers an
undivided cap doubles per instance -- 4 concurrent git calls of up to 512 MiB
each in a 2 GiB instance instead of 2, 8 multipart answer slots instead of 4,
and twice each per-principal and per-attempt rate. So each cap is the
per-instance figure divided by the worker count (at least 1), and one worker
keeps today's values.

The module-level caps are read in a fresh interpreter with the setting in its
environment, because they are fixed when the module is imported. Offline: no
cloud, no credentials, no emulator.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from swarm_api import workers
from swarm_api.deps import request_limiter
from swarm_api.live import LiveLimits

TWO = {"SWARM_API_WORKERS": "2"}


@pytest.mark.parametrize("value, expected", [
    (None, 1), ("", 1), ("1", 1), ("2", 2), (" 3 ", 3), ("0", 1), ("-2", 1), ("two", 1),
])
def test_the_worker_count_is_the_setting_and_at_least_one(value, expected):
    environ = {} if value is None else {"SWARM_API_WORKERS": value}
    assert workers.worker_count(environ) == expected


def test_a_cap_is_divided_by_the_worker_count_and_never_below_one():
    assert workers.per_worker(2, {}) == 2
    assert workers.per_worker(2, TWO) == 1
    assert workers.per_worker(4, TWO) == 2
    assert workers.per_worker(1, TWO) == 1
    assert workers.per_worker(5, TWO) == 2  # rounded down: never above the instance's figure
    assert workers.per_worker_rate(10.0, TWO) == 5.0
    assert workers.per_worker_rate(10.0, {}) == 10.0


_PROBE = """
import json
from swarm_api import attemptreads, awaitinput, gitexec
print(json.dumps({
    "git_calls": gitexec.MAX_CONCURRENT_CALLS,
    "git_slots": gitexec._CALL_SLOTS._initial_value,
    "multipart": awaitinput.MAX_CONCURRENT_MULTIPART,
    "attempt_burst": attemptreads._BUDGET.snapshot("probe"),
    "attempt_rate_per_hour": round(attemptreads._BUDGET._rate * 3600, 6),
}))
"""


def _module_caps(workers_value: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "SWARM_API_WORKERS"}
    if workers_value is not None:
        env["SWARM_API_WORKERS"] = workers_value
    done = subprocess.run([sys.executable, "-c", _PROBE], env=env, check=True,
                          capture_output=True, text=True, timeout=120)
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_one_worker_keeps_todays_module_caps():
    assert _module_caps(None) == {"git_calls": 2, "git_slots": 2, "multipart": 4,
                                  "attempt_burst": 120, "attempt_rate_per_hour": 120}


def test_two_workers_halve_the_module_caps():
    # MUTATION: build gitexec._CALL_SLOTS, awaitinput.MAX_CONCURRENT_MULTIPART
    # or attemptreads._BUDGET from the per-instance figure and this fails.
    assert _module_caps("2") == {"git_calls": 1, "git_slots": 1, "multipart": 2,
                                 "attempt_burst": 60, "attempt_rate_per_hour": 60}


def test_the_per_instance_figures_are_todays():
    from swarm_api import attemptreads, awaitinput, gitexec

    assert gitexec.MAX_CONCURRENT_CALLS_PER_INSTANCE == 2
    assert awaitinput.MAX_CONCURRENT_MULTIPART_PER_INSTANCE == 4
    assert attemptreads.CALLS_PER_HOUR == 120


def _settings(rps: int = 20, burst: int = 40) -> SimpleNamespace:
    return SimpleNamespace(core=SimpleNamespace(requests_per_second=rps), rate_limit_burst=burst)


def test_the_per_principal_limiter_keeps_todays_rate_with_one_worker():
    limiter = request_limiter(_settings(), environ={})
    assert limiter.snapshot("someone") == 40
    assert limiter._rate == 20


def test_the_per_principal_limiter_is_halved_with_two_workers():
    limiter = request_limiter(_settings(), environ=TWO)
    assert limiter.snapshot("someone") == 20
    assert limiter._rate == 10


def test_the_live_limits_per_person_are_divided_too():
    settings = SimpleNamespace(live_viewer_tickets_per_minute=30, live_takes_per_minute=6)
    one = LiveLimits(settings, environ={})
    assert (one.tickets.snapshot("p"), one.takes.snapshot("p")) == (30, 6)
    two = LiveLimits(settings, environ=TWO)
    assert (two.tickets.snapshot("p"), two.takes.snapshot("p")) == (15, 3)
    assert two.tickets._rate == pytest.approx(15 / 60)
