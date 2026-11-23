"""The worker warms the clone's Python environment once per attempt (pythonenv.py).

Read 2026-11-23: at attempt start the uv cache was empty and the clone had no
`.venv`, so the agent's first `uv run` and the pre-publish gate each paid a
cold `uv sync`, inside the agent's turn and inside the gate's budget. Now the
worker runs one `uv sync` after the clone and before the agent's first turn,
when the clone has a `uv.lock` at its root. These tests hold what it must and
must not do. The runner is a recording stand-in: no uv runs, no network.
"""

from __future__ import annotations

import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from agent_worker import prepublish, pythonenv
from agent_worker.errors import FencedError
from agent_worker.pythonenv import PythonEnvWarmup, Synced


class Recorder:
    """A stand-in for `pythonenv.SyncRunner` that records every call and, on
    success, leaves the `.venv` a real sync would."""

    def __init__(self, result: Synced | Exception | None = None) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str], float]] = []
        self.result = result if result is not None else Synced(0, "Resolved 3 packages\n")

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> Synced:
        self.calls.append((list(argv), Path(cwd), dict(env), timeout))
        if isinstance(self.result, Exception):
            raise self.result
        if self.result.code == 0:
            (Path(cwd) / ".venv").mkdir(exist_ok=True)
        return self.result


def _checkout(tmp_path: Path, *, lock: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    if lock:
        (repo / "uv.lock").write_text("version = 1\n")
    return repo


def _warmup(runner: Recorder, events: list[dict[str, Any]],
            redact: Any = lambda text: text) -> PythonEnvWarmup:
    return PythonEnvWarmup(redact=redact, emit=events.append, runner=runner)


ENV = {"PATH": "/usr/bin", "HOME": "/home/agent", "UV_CACHE_DIR": "/cache/uv"}


def test_runs_once_when_the_clone_has_a_uv_lock(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    runner, events = Recorder(), []
    result = _warmup(runner, events).warm(repo, ENV, may_start=lambda: True)
    assert len(runner.calls) == 1
    argv, cwd, env, timeout = runner.calls[0]
    assert argv == list(pythonenv.WARM_SYNC_ARGV)
    assert cwd == repo
    assert timeout == pythonenv.WARM_DEADLINE_SECONDS
    assert env["UV_CACHE_DIR"] == "/cache/uv"
    assert result is not None and result.status == pythonenv.READY
    assert len(events) == 1
    assert events[0]["phase"] == pythonenv.EVENT_PHASE
    assert events[0]["status"] == pythonenv.READY
    assert isinstance(events[0]["seconds"], float)


def test_the_sync_is_the_one_uv_run_does_and_the_gate_reads_the_same_flags() -> None:
    # One statement of the environment: the warm-up's `uv sync` and the gate's
    # `uv run` take the same flags, so the gate finds what the warm-up made.
    assert pythonenv.WARM_SYNC_ARGV[:2] == ("uv", "sync")
    assert tuple(pythonenv.WARM_SYNC_ARGV[2:2 + len(pythonenv.UV_ENV_FLAGS)]) \
        == pythonenv.UV_ENV_FLAGS
    assert prepublish.UV_ENV_FLAGS is pythonenv.UV_ENV_FLAGS
    # No extras and no named group: the project's default groups, as `uv run`
    # syncs them, and a repository without a `dev` group still syncs.
    assert not any(flag.startswith(("--extra", "--all-extras", "--group", "--only"))
                   for flag in pythonenv.WARM_SYNC_ARGV)


def test_the_gate_pytest_argv_carries_the_shared_flags(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float):
        seen.append(list(argv))
        return prepublish.Ran(0, "1 passed\n")

    prepublish._pytest_project(runner, tmp_path, "", ["tests/test_x.py"], {}, 60.0)
    flags = list(pythonenv.UV_ENV_FLAGS)
    assert seen[0][:2 + len(flags)] == ["uv", "run", *flags]


def test_nothing_runs_without_a_uv_lock(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, lock=False)
    runner, events = Recorder(), []
    assert _warmup(runner, events).warm(repo, ENV, may_start=lambda: True) is None
    assert runner.calls == [] and events == []


def test_nothing_runs_without_a_checkout() -> None:
    runner, events = Recorder(), []
    assert _warmup(runner, events).warm(None, ENV, may_start=lambda: True) is None
    assert runner.calls == [] and events == []


def test_a_timeout_is_recorded_and_the_attempt_goes_on(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    runner = Recorder(Synced(124, "Downloading torch\n", timed_out=True))
    events: list[dict[str, Any]] = []
    result = _warmup(runner, events).warm(repo, ENV, may_start=lambda: True)
    assert result is not None and result.status == pythonenv.TIMED_OUT
    assert len(events) == 1
    assert events[0]["status"] == pythonenv.TIMED_OUT
    assert str(pythonenv.WARM_DEADLINE_SECONDS) in events[0]["reason"]
    assert "seconds" in events[0]
    assert not pythonenv.is_ready(repo)


def test_a_failure_is_recorded_and_never_raised(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    events: list[dict[str, Any]] = []
    result = _warmup(Recorder(Synced(2, "error: lock is invalid\n")), events).warm(
        repo, ENV, may_start=lambda: True)
    assert result is not None and result.status == pythonenv.FAILED
    assert "exit 2" in events[0]["reason"]
    assert "lock is invalid" in events[0]["output_tail"]


def test_a_runner_that_raises_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    events: list[dict[str, Any]] = []
    result = _warmup(Recorder(OSError("no uv")), events).warm(
        repo, ENV, may_start=lambda: True)
    assert result is not None and result.status == pythonenv.FAILED
    assert events[0]["reason"].startswith("OSError")


def test_a_second_session_in_the_same_attempt_does_not_run_it_again(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    runner, events = Recorder(), []
    warmup = _warmup(runner, events)
    warmup.warm(repo, ENV, may_start=lambda: True)
    # A fresh session (or a retry after a failed warm-up) asks again: nothing runs.
    assert warmup.warm(repo, ENV, may_start=lambda: True) is None
    assert len(runner.calls) == 1 and len(events) == 1

    failed = _warmup(Recorder(Synced(1, "boom\n")), [])
    failed.warm(_checkout(tmp_path / "other"), ENV, may_start=lambda: True)
    assert failed.warm(tmp_path / "other" / "repo", ENV, may_start=lambda: True) is None


def test_a_restored_environment_that_is_ready_is_not_synced_again(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    _warmup(Recorder(), []).warm(repo, ENV, may_start=lambda: True)
    assert pythonenv.is_ready(repo)
    # The next attempt, on a checkpoint that carries the same environment.
    runner, events = Recorder(), []
    result = _warmup(runner, events).warm(repo, ENV, may_start=lambda: True)
    assert runner.calls == []
    assert result is not None and result.status == pythonenv.ALREADY_READY
    assert events[0]["status"] == pythonenv.ALREADY_READY


def test_a_changed_lock_makes_a_restored_environment_not_ready(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    _warmup(Recorder(), []).warm(repo, ENV, may_start=lambda: True)
    (repo / "uv.lock").write_text("version = 1\n# changed\n")
    assert not pythonenv.is_ready(repo)
    runner = Recorder()
    _warmup(runner, []).warm(repo, ENV, may_start=lambda: True)
    assert len(runner.calls) == 1


def test_a_cancelled_task_never_starts_it(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    runner, events = Recorder(), []
    result = _warmup(runner, events).warm(repo, ENV, may_start=lambda: False)
    assert runner.calls == []
    assert result is not None and result.status == pythonenv.NOT_STARTED
    assert events[0]["status"] == pythonenv.NOT_STARTED


def test_a_stale_generation_never_starts_it(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    runner, events = Recorder(), []

    def fenced() -> bool:
        raise FencedError(3, 4, "superseded")

    with pytest.raises(FencedError):
        _warmup(runner, events).warm(repo, ENV, may_start=fenced)
    assert runner.calls == [] and events == []


def test_its_output_goes_through_redaction(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    token = secrets.token_hex(16)
    runner = Recorder(Synced(1, f"error: index said {token} is not allowed\n"))
    events: list[dict[str, Any]] = []
    redacted: list[str] = []

    def redact(text: str) -> str:
        redacted.append(text)
        return text.replace(token, "[REDACTED]")

    _warmup(runner, events, redact=redact).warm(repo, ENV, may_start=lambda: True)
    assert redacted, "the output never reached the redaction"
    assert token not in repr(events)
    assert "[REDACTED]" in events[0]["output_tail"]


def test_the_sync_env_leaves_out_credentials() -> None:
    key = secrets.token_hex(12)
    env = pythonenv.sync_env({
        **ENV, "UV_LINK_MODE": "copy", "HTTPS_PROXY": "http://egress:3128",
        "ANTHROPIC_API" + "_KEY": key, "GH_TOKEN": key, "OPENAI_API" + "_KEY": key,
    })
    assert env["UV_CACHE_DIR"] == "/cache/uv" and env["UV_LINK_MODE"] == "copy"
    assert env["HTTPS_PROXY"] == "http://egress:3128"
    assert key not in env.values()


def test_the_heartbeat_runs_around_the_sync(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    order: list[str] = []

    @contextmanager
    def meanwhile(what: str) -> Iterator[None]:
        order.append(f"enter {what}")
        yield
        order.append("exit")

    class Ordered(Recorder):
        def __call__(self, *args: Any) -> Synced:
            order.append("sync")
            return super().__call__(*args)

    _warmup(Ordered(), []).warm(repo, ENV, may_start=lambda: True, meanwhile=meanwhile)
    assert order == ["enter python_env", "sync", "exit"]


def test_the_real_runner_kills_the_sync_at_the_deadline(tmp_path: Path) -> None:
    # A real child, not uv: a sleep that outlives its deadline is killed.
    started = time.monotonic()
    ran = pythonenv.run_sync([sys.executable, "-c", "import time; time.sleep(30)"],
                             tmp_path, {"PATH": "/usr/bin:/bin"}, 0.5)
    assert ran.timed_out
    assert time.monotonic() - started < 10


def test_the_real_runner_returns_the_output(tmp_path: Path) -> None:
    ran = pythonenv.run_sync([sys.executable, "-c", "print('synced')"], tmp_path,
                             {"PATH": "/usr/bin:/bin"}, 30)
    assert ran.code == 0 and "synced" in ran.output and not ran.timed_out


# ---------------------------------------------------------------------------
# the wiring in `lifecycle._prepare`, through `run` (invariants 4 and 5)
# ---------------------------------------------------------------------------
#
# The tests above drive `PythonEnvWarmup` alone. These run the whole worker on
# the worker conftest's fakes, with a clone that holds a `uv.lock` and a
# recording runner in `worker._python_env`, so a `may_start` of `lambda: True`,
# a dropped re-validation after the sync, or a warm-up moved before the quota
# preflight turns one of them red.

LIFECYCLE_URL = "https://github.com/acme/widgets.git"


def _lifecycle_worker(db, worker_factory, monkeypatch, runner: Recorder):
    """A worker whose clone lands a `uv.lock` at its root, whose warm-up runs
    `runner`, and which records every agent it starts."""
    from agent_worker import lifecycle
    from agent_worker.gitops import CloneResult

    from worker_seeds import seed_attempt

    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})

    def clone(**kwargs: Any) -> CloneResult:
        destination = Path(kwargs["destination"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / pythonenv.UV_LOCK).write_text("version = 1\n")
        return CloneResult(path=destination, url=kwargs["url"], ref=kwargs["ref"],
                           commit="a" * 40, duration_seconds=0.0)

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    started: list[str] = []
    real_child = lifecycle.ChildProcess

    class RecordedChild(real_child):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started.append("agent")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "ChildProcess", RecordedChild)
    worker, _, _ = worker_factory(repository_url=LIFECYCLE_URL)
    worker.egress_connect = lambda target, timeout=None: type(
        "Conn", (), {"close": lambda self: None})()
    worker._python_env = PythonEnvWarmup(redact=lambda text: text,
                                         emit=worker._note_python_env, runner=runner)
    return worker, started


def _python_env_events(db) -> list[dict[str, Any]]:
    return [e.get("detail") or {} for e in db.events("task_1")
            if (e.get("detail") or {}).get("phase") == pythonenv.EVENT_PHASE]


def _fence_when(worker, monkeypatch, decide) -> None:
    """`validate_generation` raises `FencedError` on the call `decide(phase)` picks."""
    real = worker.control.validate_generation

    def validate():
        if decide(worker.phases.current):
            raise FencedError(worker.control.generation, worker.control.generation + 1,
                              "superseded in the test")
        return real()

    monkeypatch.setattr(worker.control, "validate_generation", validate)


def test_lifecycle_runs_the_warm_up_under_the_heartbeat_before_the_agent(
        db, worker_factory, monkeypatch) -> None:
    """(e) The sync runs inside `_heartbeat_meanwhile('python_env')`, after the
    quota preflight, and before the agent starts."""
    from agent_worker.errors import ExitCode

    order: list[str] = []

    class Ordered(Recorder):
        def __call__(self, *args: Any) -> Synced:
            order.append("sync")
            return super().__call__(*args)

    runner = Ordered()
    worker, started = _lifecycle_worker(db, worker_factory, monkeypatch, runner)
    real_meanwhile = worker._heartbeat_meanwhile

    @contextmanager
    def meanwhile(what: str, **kwargs: Any) -> Iterator[None]:
        order.append(f"enter {what}")
        with real_meanwhile(what, **kwargs):
            yield
        order.append(f"exit {what}")

    monkeypatch.setattr(worker, "_heartbeat_meanwhile", meanwhile)
    real_poll = worker.control.poll

    def poll(*args: Any, **kwargs: Any):
        if worker.phases.current == "quota_preflight":
            order.append("quota_preflight")
        return real_poll(*args, **kwargs)

    monkeypatch.setattr(worker.control, "poll", poll)

    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert len(runner.calls) == 1
    assert started == ["agent"]
    at = order.index("sync")
    assert order[at - 1:at + 2] == ["enter python_env", "sync", "exit python_env"], order
    assert order.index("quota_preflight") < at, order
    assert [e["status"] for e in _python_env_events(db)] == [pythonenv.READY]


def test_lifecycle_a_stale_generation_before_the_warm_up_starts_nothing(
        db, worker_factory, monkeypatch) -> None:
    """(a) A `FencedError` from the `may_start` check: no sync, no agent, exit 70."""
    from agent_worker.errors import ExitCode

    runner = Recorder()
    worker, started = _lifecycle_worker(db, worker_factory, monkeypatch, runner)
    _fence_when(worker, monkeypatch, lambda phase: phase == pythonenv.EVENT_PHASE)

    assert worker.run() == ExitCode.GENERATION_FENCED
    assert runner.calls == []
    assert started == []


def test_lifecycle_a_cancel_before_the_warm_up_records_not_started(
        db, worker_factory, monkeypatch) -> None:
    """(b) A cancel requested by the `may_start` check: a not_started event and no sync."""
    runner = Recorder()
    worker, _started = _lifecycle_worker(db, worker_factory, monkeypatch, runner)
    real_enter = worker.phases.enter

    def enter(phase: str, *args: Any, **kwargs: Any):
        if phase == pythonenv.EVENT_PHASE:
            db.doc("tasks/task_1")["cancel_requested"] = True
        return real_enter(phase, *args, **kwargs)

    monkeypatch.setattr(worker.phases, "enter", enter)

    worker.run()
    assert runner.calls == []
    assert [e["status"] for e in _python_env_events(db)] == [pythonenv.NOT_STARTED]


def test_lifecycle_a_quota_park_pays_nothing_for_the_warm_up(
        db, worker_factory, monkeypatch) -> None:
    """(c) A park at the quota preflight returns before the warm-up: no sync, no agent."""
    from agent_worker import lifecycle
    from agent_worker.errors import ExitCode
    from agent_worker.quota import QuotaSignal
    from swarm_common.models import ProviderState

    runner = Recorder()
    worker, started = _lifecycle_worker(db, worker_factory, monkeypatch, runner)
    monkeypatch.setattr(lifecycle, "signal_from_control", lambda *_a, **_k: QuotaSignal(
        provider="anthropic", state=ProviderState.EXHAUSTED, source="control_plane",
        retry_after_seconds=1800, detail="exhausted in the test"))
    parked: list[str] = []

    def park(decision: Any, *, source: str) -> Any:
        parked.append(source)
        return lifecycle.Outcome(exit_code=ExitCode.PARKED)

    monkeypatch.setattr(worker, "_park_for_quota", park)

    assert worker.run() == ExitCode.PARKED
    assert parked == ["preflight"]
    assert runner.calls == []
    assert started == []
    assert _python_env_events(db) == []


def test_lifecycle_a_stale_generation_after_the_sync_propagates_out_of_prepare(
        db, worker_factory, monkeypatch) -> None:
    """(d) The sync ran; the generation read after it is stale: the `FencedError`
    leaves `_prepare` and the agent never starts."""
    from agent_worker.errors import ExitCode

    runner = Recorder()
    worker, started = _lifecycle_worker(db, worker_factory, monkeypatch, runner)
    _fence_when(worker, monkeypatch,
                lambda phase: phase == pythonenv.EVENT_PHASE and bool(runner.calls))
    raised: list[BaseException] = []
    real_prepare = worker._prepare

    def prepare():
        try:
            return real_prepare()
        except BaseException as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(worker, "_prepare", prepare)

    assert worker.run() == ExitCode.GENERATION_FENCED
    assert len(runner.calls) == 1
    assert [type(e) for e in raised] == [FencedError]
    assert started == []
