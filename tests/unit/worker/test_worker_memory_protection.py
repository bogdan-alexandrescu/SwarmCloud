"""The agent cannot read the tenant git token out of the worker's memory.

The worker reads the tenant's git token at the clone and holds it until the
publish. The agent runs through all of it, as the same uid. Where the kernel
allows same-uid ptrace or `/proc/<pid>/mem` access, the agent could read the
token from the worker's heap during the run, before the pre-publish reap kills
anything. `hardening.make_non_dumpable` closes that at startup. This file pins:

* the call itself: every outcome on every platform, and no path that raises;
* the entrypoint makes it before anything that can reach a credential, and
  hands what it established to the worker;
* a worker whose memory is not protected never reads the token (the clone and
  publish halves are in `test_forge_token_isolation.py`, next to their fixtures);
* on Linux, against real processes: the process the entrypoint starts is
  non-dumpable by the time it reads its configuration, and another process of
  the same uid is refused both `/proc/<pid>/environ` and `/proc/<pid>/mem`. A
  control, the same entrypoint with the call disabled, shows those reads do
  succeed when nothing protects the process.

Every test but the real-process one stands in for the prctl call. The real one
runs it in a child, so this test process's own dumpability never changes.
"""

from __future__ import annotations

import errno
import io
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from conftest import seed_tenant
from dumpable_entry_helper import CANARY_ENV, read_process

HELPER = Path(__file__).with_name("dumpable_entry_helper.py")


def _hardening():
    # Imported inside each test: a module that does not import is then one
    # failing test per property, not one collection error that hides them all.
    from agent_worker import hardening

    return hardening


class _Prctl:
    """A stand-in for libc's prctl that records what it was asked."""

    def __init__(self, *, reads_back: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.reads_back = reads_back
        self.raises = raises

    def __call__(self, option: int, arg2: int) -> int:
        self.calls.append((option, arg2))
        if self.raises is not None:
            raise self.raises
        return self.reads_back if option == 3 else 0


# -- the call ------------------------------------------------------------------


def test_on_linux_the_process_is_made_non_dumpable_and_the_kernel_is_asked_back():
    h = _hardening()
    prctl = _Prctl(reads_back=0)
    memory = h.make_non_dumpable(platform="linux", prctl=prctl)
    assert prctl.calls == [(h.PR_SET_DUMPABLE, 0), (h.PR_GET_DUMPABLE, 0)], prctl.calls
    assert (h.PR_SET_DUMPABLE, h.PR_GET_DUMPABLE) == (4, 3)  # <linux/prctl.h>
    assert memory.status == h.PROTECTED, memory
    assert memory.git_token_refusal is None


def test_a_failed_call_on_linux_is_reported_not_raised_and_refuses_the_token():
    h = _hardening()
    prctl = _Prctl(raises=OSError(errno.EPERM, os.strerror(errno.EPERM)))
    memory = h.make_non_dumpable(platform="linux", prctl=prctl)
    assert memory.status == h.FAILED, memory
    refusal = memory.git_token_refusal
    assert refusal and "holds no tenant git token" in refusal, refusal
    assert os.strerror(errno.EPERM) in refusal, refusal


def test_a_call_that_leaves_the_process_dumpable_is_a_failure():
    """Returning 0 is not the property. The flag read back from the kernel is."""
    h = _hardening()
    memory = h.make_non_dumpable(platform="linux", prctl=_Prctl(reads_back=1))
    assert memory.status == h.FAILED, memory
    assert "dumpable (1)" in memory.detail, memory
    assert memory.git_token_refusal


def test_a_libc_without_prctl_on_linux_is_a_failure_not_a_crash(monkeypatch):
    h = _hardening()

    def no_symbol():
        raise AttributeError("undefined symbol: prctl")

    monkeypatch.setattr(h, "_libc_prctl", no_symbol)
    memory = h.make_non_dumpable(platform="linux")
    assert memory.status == h.FAILED, memory
    assert "undefined symbol" in memory.detail
    assert memory.git_token_refusal


def test_off_linux_there_is_no_call_a_warning_status_and_no_refusal():
    """A developer's machine: no prctl exists, and the worker carries on."""
    h = _hardening()
    prctl = _Prctl()
    memory = h.make_non_dumpable(platform="darwin", prctl=prctl)
    assert prctl.calls == [], "prctl was called on a platform that has none"
    assert memory.status == h.UNSUPPORTED, memory
    assert memory.git_token_refusal is None


# -- the entrypoint --------------------------------------------------------------


def _drive_entrypoint(monkeypatch, tmp_path: Path, memory: Any) -> tuple[list[str], dict, str]:
    """Run `__main__.main` in this process up to the worker's `run`.

    Stood in for: the prctl call (so this process stays as it was), the root
    log handler and the signal handlers (both process-wide, and pytest's),
    the DNS preflight, and `build_worker` (a worker whose `run` records that
    it was reached). Every credential the worker reads is read inside `run`,
    after the configuration.
    """
    h = _hardening()
    import agent_worker.__main__ as entrypoint
    from agent_worker import startup
    from swarm_common.config import Settings

    trace: list[str] = []
    seen: dict[str, Any] = {}
    stream = io.StringIO()

    def make_non_dumpable(**_kwargs):
        trace.append("make_non_dumpable")
        return memory

    monkeypatch.setattr(h, "make_non_dumpable", make_non_dumpable)
    real_bootstrap = startup.bootstrap_logger
    monkeypatch.setattr(startup, "bootstrap_logger", lambda *a, **k: real_bootstrap({}, stream=stream))
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *a, **k: trace.append("logging"))
    monkeypatch.setattr(startup, "route_signals", lambda *a, **k: False)

    real_settings = Settings.from_env

    def settings_from_env(cls):
        trace.append("configuration")
        return real_settings()

    monkeypatch.setattr(Settings, "from_env", classmethod(settings_from_env))
    monkeypatch.setattr(startup, "preflight_hosts", lambda *a, **k: ["firestore.example"])
    monkeypatch.setattr(
        startup,
        "dns_preflight_with_retries",
        lambda hosts, **k: startup.PreflightOutcome(
            results=[{"host": h_, "ok": True, "addresses": []} for h_ in hosts],
            attempts=1,
            seconds=0.0,
        ),
    )

    class _Worker:
        def run(self) -> int:
            trace.append("worker.run")
            return 0

    def build_worker(config, settings, **kwargs):
        trace.append("build_worker")
        seen.update(kwargs)
        return _Worker()

    monkeypatch.setattr(entrypoint, "build_worker", build_worker)
    monkeypatch.delenv("QUOTA_BROKER_URL", raising=False)
    for name, value in {
        "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
        "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
        "PROJECT_ID": "swarm-test", "LOCAL_ARTIFACT_ROOT": str(tmp_path / "gcs"),
        "WORKSPACE_ROOT": str(tmp_path / "ws"), "DISABLE_CLOUD_MONITORING": "1",
    }.items():
        monkeypatch.setenv(name, value)

    code = entrypoint.main()
    assert code == 0, (code, trace, stream.getvalue())
    return trace, seen, stream.getvalue()


def test_the_entrypoint_closes_its_memory_before_anything_can_reach_a_credential(
    monkeypatch, tmp_path
):
    h = _hardening()
    protected = h.MemoryProtection(h.PROTECTED, "stand-in")
    trace, seen, log = _drive_entrypoint(monkeypatch, tmp_path, protected)
    assert trace[:1] == ["make_non_dumpable"], (
        f"the entrypoint did something before closing its memory: {trace}"
    )
    assert trace == ["make_non_dumpable", "logging", "configuration", "build_worker",
                     "worker.run"], trace
    # And it hands the worker what it established, which is what the lifecycle
    # decides the git token on.
    assert seen.get("memory") is protected, seen
    records = [json.loads(line) for line in log.splitlines() if line.strip()]
    assert records[0]["message"] == "worker process started", records
    assert any(r.get("memory_protection") == h.PROTECTED for r in records), records


def test_the_entrypoint_warns_and_carries_on_where_prctl_does_not_exist(monkeypatch, tmp_path):
    h = _hardening()
    unsupported = h.MemoryProtection(h.UNSUPPORTED, "prctl does not exist on darwin")
    trace, seen, log = _drive_entrypoint(monkeypatch, tmp_path, unsupported)
    assert trace[-1] == "worker.run", trace
    assert seen.get("memory") is unsupported
    [line] = [json.loads(x) for x in log.splitlines() if "memory_protection" in x]
    assert line["severity"] == "WARNING", line
    assert "NOT protected" in line["message"], line


def test_the_entrypoint_says_a_failed_call_costs_the_git_token(monkeypatch, tmp_path):
    h = _hardening()
    failed = h.MemoryProtection(h.FAILED, "OSError: [Errno 1] Operation not permitted")
    trace, seen, log = _drive_entrypoint(monkeypatch, tmp_path, failed)
    assert seen.get("memory") is failed, "the failure was not handed to the worker"
    [line] = [json.loads(x) for x in log.splitlines() if "memory_protection" in x]
    assert line["severity"] == "ERROR", line
    assert "holds no tenant git token" in line.get("refusal", ""), line


# -- the worker: an unprotected memory never holds the token -----------------------


def test_an_unprotected_worker_never_reads_the_git_token(worker_factory, db):
    """The token is refused at the read, so it is never in the heap at all.
    The control, a protected worker, reads it from the same secret."""
    from fakes import FakeSecretClient

    h = _hardening()
    seed_tenant(db, credentials=["git"])
    client = FakeSecretClient()
    worker, _, _ = worker_factory(secret_client=client)
    failed = h.make_non_dumpable(platform="linux", prctl=_Prctl(raises=OSError(errno.EPERM, "no")))
    assert failed.status == h.FAILED
    worker.memory = failed

    assert worker._git_token() is None, "a worker whose memory is readable held the git token"
    assert not [s for s in client.accessed if s.endswith("-git")], client.accessed

    control, _, _ = worker_factory(secret_client=client)
    control.memory = h.MemoryProtection(h.PROTECTED, "stand-in")
    token = control._git_token()
    assert token, "the control could not read the token; the refusal above proves nothing"
    assert [s for s in client.accessed if s.endswith("-git")], client.accessed


def test_a_worker_that_was_handed_no_protection_holds_no_token(worker_factory, db):
    """None is what a worker built without the entrypoint has. It fails closed."""
    from fakes import FakeSecretClient

    seed_tenant(db, credentials=["git"])
    client = FakeSecretClient()
    worker, _, _ = worker_factory(secret_client=client)
    worker.memory = None
    assert worker._git_token() is None
    assert not [s for s in client.accessed if s.endswith("-git")], client.accessed


# -- real processes (Linux) --------------------------------------------------------


def _can_bypass_ptrace_checks() -> bool:
    """CAP_SYS_PTRACE in this process's effective set (bit 19 of CapEff).

    A process holding it may read any process's memory, dumpable or not, so
    the property cannot be observed from here.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << 19))
    except OSError:
        pass
    return False


def _require_linux_without_ptrace_capability() -> None:
    why = ""
    if not sys.platform.startswith("linux") or not Path("/proc/self/status").exists():
        why = f"dumpability is a Linux property; this is {sys.platform}"
    elif _can_bypass_ptrace_checks():
        why = "this process holds CAP_SYS_PTRACE, which reads any process's memory"
    if not why:
        return
    if os.environ.get("CI") == "true":
        # CI is where the real kernel answers. A skip there hides the property
        # as well as a pass would (see test_procman_reap.py, which skipped
        # unnoticed in this PR's first green run).
        pytest.fail(f"CI must run the real non-dumpable check, and cannot here: {why}")
    pytest.skip(why)


def _start_entry(tmp_path: Path, canary: str, *, without_prctl: bool, name: str):
    report = tmp_path / f"{name}.json"
    log = (tmp_path / f"{name}.log").open("wb")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env[CANARY_ENV] = canary
    argv = [sys.executable, str(HELPER), "entry", str(report)]
    if without_prctl:
        argv.append("--without-prctl")
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    deadline = time.monotonic() + 60
    while not report.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            log.close()
            raise AssertionError(
                f"the worker entry never reached its configuration (exit {proc.returncode}):\n"
                + (tmp_path / f"{name}.log").read_text(errors="replace")[-4000:]
            )
        time.sleep(0.05)
    log.close()
    return proc, json.loads(report.read_text())


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=30)


def _read_from_a_sibling(pid: int, canary: str) -> dict[str, str]:
    """The reads, from a separate process of this uid that is not the entry's
    ancestor: the agent's position."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env.pop(CANARY_ENV, None)  # the sibling must find the canary in the target, not itself
    out = subprocess.run(
        [sys.executable, str(HELPER), "read", str(pid), canary],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_process_of_the_same_uid_cannot_read_the_worker_memory_or_environment(tmp_path):
    _require_linux_without_ptrace_capability()
    canary = f"canary-{uuid.uuid4().hex}"

    control, control_report = _start_entry(tmp_path, canary, without_prctl=True, name="control")
    try:
        assert control_report["dumpable"] == 1, control_report
        control_sibling = _read_from_a_sibling(control_report["pid"], canary)
        control_parent = read_process(control_report["pid"], canary)
    finally:
        _stop(control)
    # The control: the SAME entry, left dumpable, IS readable from here. The
    # environment by any process of the uid. The memory by its parent: Yama's
    # default policy (ptrace_scope 1) already keeps a sibling out of a dumpable
    # process's memory, so only the parent can show that memory is readable.
    assert control_sibling["environ"] == "found", (
        f"a sibling could not read an UNPROTECTED process's environment ({control_sibling}); "
        "the refusal below would prove nothing on this host"
    )
    assert control_parent["mem"] == "found", (
        f"the parent could not read an UNPROTECTED process's memory ({control_parent}); "
        "the refusal below would prove nothing on this host"
    )

    worker, report = _start_entry(tmp_path, canary, without_prctl=False, name="worker")
    try:
        # Asked from inside the worker process, when it reached its
        # configuration: before any client exists or any credential is read.
        assert report["dumpable"] == 0, (
            f"the worker entry is still dumpable when it reads its configuration: {report}"
        )
        sibling = _read_from_a_sibling(report["pid"], canary)
        parent = read_process(report["pid"], canary)
    finally:
        _stop(worker)
    assert sibling == {"environ": "denied:PermissionError", "mem": "denied:PermissionError"}, (
        f"a process of the same uid read the worker: {sibling}"
    )
    # Stricter than the agent's position: even the parent, whom Yama would let
    # read a dumpable process, is refused.
    assert parent == {"environ": "denied:PermissionError", "mem": "denied:PermissionError"}, (
        f"the worker's parent read the worker: {parent}"
    )
