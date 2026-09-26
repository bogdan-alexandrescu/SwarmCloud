"""The pre-publish reap: kill every foreign process, then verify none survive.

`procman.reap_foreign_processes` is what makes the forge-token guarantee hold
against a process the agent double-forked out of the runner's session (see
`test_forge_token_isolation.py` for the end-to-end property). This file pins the
mechanism itself:

* `_live_foreign_pids` counts the right processes and no others -- not PID 1, not
  this process, not a zombie (already dead), not another uid (kernel threads);
* the orchestration kills, re-checks, and returns survivors so the caller can
  refuse rather than publish;
* and one real-process test runs the real `os.kill(-1, SIGKILL)` inside a private
  PID namespace, where it can be exercised without taking the test runner down.

The real killer is never run in the shared test process: `os.kill(-1, SIGKILL)`
there would kill pytest. Every test but the namespaced one injects a fake killer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker import procman
from agent_worker.procman import _live_foreign_pids, reap_foreign_processes


class _Logger:
    def __init__(self) -> None:
        self.errors: list[tuple] = []
        self.infos: list[tuple] = []

    def info(self, *args, **kwargs) -> None:
        self.infos.append((args, kwargs))

    def error(self, *args, **kwargs) -> None:
        self.errors.append((args, kwargs))

    def warning(self, *args, **kwargs) -> None:
        pass


# -- /proc parsing ----------------------------------------------------------


def _write_status(proc: Path, pid: int, *, uid: int, state: str) -> None:
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(
        f"Name:\tproc{pid}\nState:\t{state} (whatever)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
    )


def test_live_foreign_pids_counts_only_live_processes_this_uid_owns(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    me = 4242
    mine = 9000

    _write_status(proc, 1, uid=mine, state="S")          # PID 1 (tini) -- excluded
    _write_status(proc, me, uid=mine, state="R")         # this process -- excluded
    _write_status(proc, 5001, uid=mine, state="R")       # a live foreign process -- COUNTED
    _write_status(proc, 5002, uid=mine, state="S")       # sleeping foreign process -- COUNTED
    _write_status(proc, 5003, uid=mine, state="Z")       # zombie -- already dead, excluded
    _write_status(proc, 5004, uid=0, state="R")          # another uid (kernel) -- excluded
    (proc / "not-a-pid").mkdir()                          # non-numeric entry -- ignored

    survivors = _live_foreign_pids(proc_root=str(proc), uid=mine, self_pid=me)
    assert set(survivors) == {5001, 5002}, survivors


def test_live_foreign_pids_survives_an_entry_that_vanishes_mid_scan(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    # A directory named like a pid but with no status file -- the process exited
    # between listdir and open. It must be skipped, not raise.
    (proc / "7777").mkdir()
    _write_status(proc, 8888, uid=123, state="R")
    survivors = _live_foreign_pids(proc_root=str(proc), uid=123, self_pid=1)
    assert set(survivors) == {8888}, survivors


# -- orchestration: kill, verify, and refuse when it cannot be cleaned -------


def test_reap_returns_clean_once_the_kill_removes_the_survivors():
    """A killer that works: the first check sees a survivor, the kill removes it,
    and the second check is clean, so the reap reports no survivors."""
    state = {"alive": [321]}

    def killer() -> None:
        state["alive"] = []

    def lister() -> tuple[int, ...]:
        return tuple(state["alive"])

    log = _Logger()
    survivors = reap_foreign_processes(logger=log, killer=killer, lister=lister, delay=0)
    assert survivors == ()
    assert log.infos and not log.errors


def test_reap_refuses_when_a_process_will_not_die():
    """A process the kill cannot remove -- the check keeps seeing it -- makes the
    reap return it after a bounded number of tries, which the caller turns into a
    refusal to publish. It must not loop forever."""
    kills = {"n": 0}

    def killer() -> None:
        kills["n"] += 1

    def lister() -> tuple[int, ...]:
        return (999,)  # never goes away

    log = _Logger()
    survivors = reap_foreign_processes(
        logger=log, killer=killer, lister=lister, attempts=4, delay=0
    )
    assert survivors == (999,)
    assert kills["n"] == 5  # one up-front kill, then one per retry
    assert log.errors, "a reap that could not clean the container must log an error"


def test_reap_kills_even_when_the_first_check_looks_clean():
    """The kill runs before the first check, so a process mid-fork when the reap
    starts is still signalled. The killer is always invoked at least once."""
    kills = {"n": 0}

    def killer() -> None:
        kills["n"] += 1

    survivors = reap_foreign_processes(
        logger=_Logger(), killer=killer, lister=lambda: (), delay=0
    )
    assert survivors == ()
    assert kills["n"] == 1


# -- the real kill, in a namespace where it cannot reach the test runner ------


def _userns_pid_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        proc = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@pytest.mark.skipif(
    not _userns_pid_available(),
    reason="unprivileged PID+user namespaces are unavailable; cannot run os.kill(-1) safely",
)
def test_reap_kills_a_real_escaped_process_in_a_private_pid_namespace():
    """End-to-end with the real killer and the real /proc verifier. Inside a
    fresh PID namespace this helper is PID 1, so `os.kill(-1, SIGKILL)` reaches
    only the victim it spawned -- never the test runner."""
    helper = Path(__file__).with_name("reap_realproc_helper.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [
            "unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
            sys.executable, str(helper),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"the real reap did not clean the namespace:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "SURVIVORS=() VICTIM_ALIVE=False" in proc.stdout, proc.stdout
