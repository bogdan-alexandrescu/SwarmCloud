"""The pieces behind a loud startup, tested in-process: budgets, windows, preflight.

`test_startup_is_loud.py` runs the real entrypoint in a real process. This
file pins the parts a process cannot show from the outside:

  * **What a SIGTERM before the runner WRITES.** Measured on
    `FakeFirestore.writes`, the list of every write, as
    `test_fenced_sigterm.py` does. During the generation check, nothing at
    all. After it and before the runner, only this attempt's own document. The
    task, the lease and the event stream are left alone whether or not the
    attempt is fenced. PR #49's rule for a fenced worker is kept by never
    writing them on this path.
  * **The startup Firestore budget** applies inside the control plane's
    startup window and nowhere else. Every call the lifecycle makes in that
    window, transactions included, is measured in
    `test_startup_window_under_real_transactions.py`.
  * **A control plane the worker cannot read** at the generation check ends
    startup with one structured error, and writes nothing. Which exit depends
    on what Firestore said (owner, 2026-09-25: 78 is non-retryable). A
    REFUSAL (PERMISSION_DENIED) will be refused again, so it is 78, "cannot
    start", and its cause goes to the termination message. UNAVAILABLE or a
    spent budget may be gone on the next attempt, so it is 69, which the
    reconciler retries like any lost attempt.
  * the DNS preflight's deadline, its bounded retries, its choice of hosts,
    and the signal routing that keeps the SIGTERM stack dump armed while the
    worker starts and disarms it once the runner exists.

HOW THE SIGTERM IS DELIVERED. The test calls the handler that `run()`
installed, fetched with `signal.getsignal`, at the point the signal would have
landed. The interpreter does the same on a real signal: it calls that handler
on the main thread between two bytecodes. A real signal sent to the test
process would land in pytest. `test_startup_is_loud.py` sends real ones to a
real worker.
"""

from __future__ import annotations

import copy
import io
import json
import signal
import threading
import time
from typing import Any

import pytest
from fakes import (
    ExplodingChildProcess,
    FakeCollectionRef,
    FakeDocumentRef,
    FakeFirestore,
    FakeTransactionRunner,
)

from agent_worker import lifecycle
from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt

#: 128 + SIGTERM. Restated rather than imported: the number is the contract
#: with whoever reads the pod's exit status.
INTERRUPTED = 143

#: The worker could not start, and trying again would fail the same way. The
#: reconciler fails the task on it, with no retry. Restated for the same reason.
CANNOT_START = 78

#: A dependency was unavailable before the runner. The reconciler retries it.
UNAVAILABLE = 69


class _Exited(BaseException):
    """Stands in for `os._exit` so the test process survives the worker's exit."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _hard_exit(code: int) -> None:
    raise _Exited(code)


def _deliver_sigterm() -> None:
    """Call the handler the worker installed, as the interpreter would."""
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), f"no Python SIGTERM handler is installed: {handler!r}"
    handler(signal.SIGTERM, None)


def _records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


def _writes_after(db: FakeFirestore, mark: int) -> list[str]:
    return [path for _, path, _ in db.writes[mark:]]


# ---------------------------------------------------------------------------
# SIGTERM before the runner exists
# ---------------------------------------------------------------------------


def test_a_sigterm_during_the_generation_check_exits_at_once_having_written_nothing(
    db, worker_factory, log_stream, monkeypatch
):
    """The incident's window: blocked in the generation check's read.

    Nothing has been written, so there is nothing to record and nothing to
    undo. The worker names the phase and leaves.
    """
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker.phases.hard_exit = _hard_exit

    original = FakeDocumentRef.get
    delivered: list[bool] = []

    def read_then_sigterm(self: FakeDocumentRef, *args: Any, **kwargs: Any) -> Any:
        if self.path == "tasks/task_1" and not delivered:
            delivered.append(True)
            _deliver_sigterm()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FakeDocumentRef, "get", read_then_sigterm)

    with pytest.raises(_Exited) as exited:
        worker.run()

    assert exited.value.code == INTERRUPTED
    assert db.writes == [], f"a worker SIGTERMed before any write wrote {db.writes}"
    said = [r for r in _records(log_stream) if "SIGTERM" in r["message"]]
    assert said and said[-1]["phase"] == "validate_generation", said
    assert said[-1]["severity"] == "WARNING"


def _prepare_then_sigterm(worker: Any, db: FakeFirestore, monkeypatch, *, phase: str,
                          fence_first: bool = False) -> dict[str, Any]:
    """Deliver the SIGTERM from inside `phase`.

    Records how many writes preceded it, and the task, lease and pool
    documents as they stood at that instant.
    """
    mark: dict[str, Any] = {}
    method = {"clone": "_maybe_clone", "credentials": "_build_child_env"}[phase]

    def interrupted(*_args: Any, **_kwargs: Any) -> Any:
        if fence_first:
            # `invalidate_generation`, as the reconciler writes it before it
            # deletes the Job.
            db.doc("tasks/task_1")["current_generation"] = 2
        mark["writes"] = len(db.writes)
        mark["documents"] = copy.deepcopy({
            path: doc for path, doc in db.documents.items()
            if path == "tasks/task_1" or path.startswith(("leases/", "pools/"))
        })
        _deliver_sigterm()
        return {} if method == "_build_child_env" else None

    monkeypatch.setattr(worker, method, interrupted)
    # Proof the agent never runs: constructing a child fails the attempt loudly.
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    return mark


@pytest.mark.parametrize("phase", ["clone", "credentials"])
def test_a_sigterm_while_the_runner_is_prepared_writes_only_this_attempts_document(
    db, worker_factory, log_stream, monkeypatch, phase
):
    """After the generation check and before the runner: the startup window.

    The old handler set a flag that only the runner loop read. The worker
    finished its clone, started the agent, and only then parked the task
    SCHEDULED_RETRY, a park nothing promotes. Here the SIGTERM unwinds out of
    the phase it lands in. The task and the lease are left for the reconciler
    to requeue, and the attempt records why it ended.
    """
    seed_attempt(db)
    worker, _, _ = worker_factory()
    mark = _prepare_then_sigterm(worker, db, monkeypatch, phase=phase)

    exit_code = worker.run()

    assert exit_code == INTERRUPTED, _records(log_stream)[-5:]
    assert mark, "the SIGTERM was never delivered"
    after = _writes_after(db, mark["writes"])
    assert after and set(after) == {"attempts/att_1"}, after
    attempt = db.doc("attempts/att_1")
    assert attempt["exit_code"] == INTERRUPTED
    assert attempt["completed_at"] is not None
    assert phase in attempt["error"] and "SIGTERM" in attempt["error"], attempt["error"]
    assert attempt["tenant_id"] == TENANT

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value, "the task was moved on the way out"
    assert task["current_lease_id"] == "lease_1"
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is None, "the lease was released by the worker"
    for name, pool in ((p, d) for p, d in db.documents.items() if p.startswith("pools/")):
        assert pool["active"] == 1, f"{name} was decremented"

    said = [r for r in _records(log_stream) if "SIGTERM" in r["message"]]
    assert said and said[-1]["phase"] == phase, said


def test_a_fenced_worker_sigtermed_before_its_runner_leaves_the_task_and_the_lease_alone(
    db, worker_factory, log_stream, monkeypatch
):
    """The reconciler's order: fence, then delete the Job. PR #49's rule, kept.

    The old handler let startup continue into the fence re-check, which emitted
    `generation_fenced` into a stream that now belongs to the newer generation.
    The new one writes nothing but the attempt's own document, fenced or not.
    """
    seed_attempt(db)
    worker, _, _ = worker_factory()
    mark = _prepare_then_sigterm(worker, db, monkeypatch, phase="clone", fence_first=True)

    exit_code = worker.run()

    assert exit_code == INTERRUPTED
    after = _writes_after(db, mark["writes"])
    assert set(after) <= {"attempts/att_1"}, after
    assert mark["documents"]["tasks/task_1"]["current_generation"] == 2
    for path, doc in mark["documents"].items():
        assert db.documents.get(path) == doc, f"{path} changed after the SIGTERM"
    fenced_events = [
        e for e in db.events("task_1") if e["type"] == EventType.GENERATION_FENCED.value
    ]
    assert not fenced_events, fenced_events


def test_a_sigterm_once_the_runner_exists_still_stops_it_and_parks(db, worker_factory):
    """Unchanged on purpose: with a child, the handler only sets the flag.

    A regression guard for the window's other edge. It passes against the old
    code as well, and it is not one of the red-first proofs.
    """
    seed_attempt(db, task_input={"prompt": "long", "steps": 40, "sleep_seconds": 20.0})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=60, checkpoint_interval_seconds=60,
        control_poll_seconds=60, timeout_seconds=60,
    )
    fired = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            child = worker._child
            if child is not None and child.poll() is None and child.elapsed_seconds > 0.5:
                _deliver_sigterm()
                fired.set()
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    exit_code = worker.run()
    watcher.join(timeout=5)

    assert fired.is_set(), "the runner never started; nothing was tested"
    assert exit_code == ExitCode.PARKED
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"].get("cause") == "worker_interrupted", parked


# ---------------------------------------------------------------------------
# the lifecycle's phases
# ---------------------------------------------------------------------------


def test_the_lifecycle_announces_each_phase_once_and_in_order(db, worker_factory, log_stream):
    seed_attempt(db, task_input={"prompt": "phases", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    phases = [r["phase"] for r in _records(log_stream) if r["message"] == "startup phase"]
    assert phases == [
        "validate_generation",
        "record_attempt_start",
        "advance_to_running",
        "workspace",
        "restore_checkpoint",
        "clone",
        "stage_inputs",
        "credentials",
        "revalidate_generation",
        "quota_preflight",
        "runner",
    ], phases


# ---------------------------------------------------------------------------
# a control plane that cannot be read
# ---------------------------------------------------------------------------


def _unreachable(kind: str) -> BaseException:
    from google.api_core import exceptions as core

    if kind == "retry-budget-spent":
        return core.RetryError(
            "Timeout of 30.0s exceeded",
            cause=core.ServiceUnavailable("DNS resolution failed for firestore.googleapis.com:443"),
        )
    if kind == "unavailable":
        return core.ServiceUnavailable("failed to connect to all addresses")
    return core.PermissionDenied("Missing or insufficient permissions.")


@pytest.mark.parametrize(
    ("kind", "expected_exit"),
    [
        ("retry-budget-spent", UNAVAILABLE),
        ("unavailable", UNAVAILABLE),
        ("permission-denied", CANNOT_START),
    ],
)
def test_a_control_plane_it_cannot_read_ends_startup_and_writes_nothing(
    db, worker_factory, log_stream, monkeypatch, tmp_path, kind, expected_exit
):
    """Before #57: the exception left `run()` as a bare traceback, after up to 300 s.

    After it, every one of these was 78. With 78 now meaning "fail the task,
    do not retry", only the refusal keeps it. An outage that outlasted the
    30 s budget is 69 and the reconciler requeues the task, exactly as it did
    for these 78s before the reconciler read exit codes at all.
    """
    from agent_worker import startup

    termination_log = tmp_path / "termination-log"
    termination_log.write_text("")
    monkeypatch.setattr(startup, "TERMINATION_MESSAGE_PATH", str(termination_log), raising=False)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    error = _unreachable(kind)
    original = FakeDocumentRef.get

    def unreadable(self: FakeDocumentRef, *args: Any, **kwargs: Any) -> Any:
        if self.path.startswith("tasks/"):
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FakeDocumentRef, "get", unreadable)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    assert worker.run() == expected_exit
    assert db.writes == [], db.writes
    errors = [r for r in _records(log_stream) if r["severity"] == "ERROR"]
    assert errors, "nothing was logged"
    last = errors[-1]
    assert last["phase"] == "validate_generation"
    assert last["error_type"] == type(error).__name__
    assert last["exit_code"] == expected_exit
    if kind == "retry-budget-spent":
        assert "ServiceUnavailable" in last["cause"], last

    written = termination_log.read_text()
    if expected_exit == CANNOT_START:
        # The reconciler's only way to learn why: Firestore said no to this
        # worker, so the worker cannot write it there.
        record = json.loads(written)
        assert record["exit_code"] == CANNOT_START, record
        assert record["phase"] == "validate_generation", record
        assert "PermissionDenied" in record["cause"], record
    else:
        assert written == "", "a retryable exit wrote a cannot-start cause"


# ---------------------------------------------------------------------------
# the startup budget
# ---------------------------------------------------------------------------


class _RecordingRef(FakeDocumentRef):
    def get(self, *args: Any, **kwargs: Any) -> Any:
        self._db.calls.append(("get", self.path, dict(kwargs)))
        return super().get(*args, **kwargs)

    def set(self, data: dict[str, Any], merge: bool = False, **kwargs: Any) -> None:
        self._db.calls.append(("set", self.path, dict(kwargs)))
        super().set(data, merge=merge)


class _RecordingCollection(FakeCollectionRef):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        return _RecordingRef(self._db, super().document(doc_id).path)


class RecordingFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def collection(self, name: str) -> FakeCollectionRef:
        return _RecordingCollection(self, name)


def test_the_startup_budget_applies_inside_the_window_and_nowhere_else():
    """`ControlPlane.startup_budget()` is the one switch, for every call it makes.

    The lifecycle holds the window open from the generation check until the
    runner child exists. Outside it, the same calls keep the library's
    defaults: the mid-run control poll, for one, and how long a running agent
    tolerates a Firestore outage is a separate decision.
    """
    db = RecordingFirestore()
    seed_attempt(db)
    budget = {"retry": object(), "timeout": 10.0}
    control = ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
                            generation=1, runner_profile="mock", stream=io.StringIO()),
        txn_runner=FakeTransactionRunner(db),
        startup_call_options=budget,
    )

    with control.startup_budget():
        control.validate_generation()
    reads = {path: kwargs for op, path, kwargs in db.calls if op == "get"}
    assert set(reads) == {"tasks/task_1", "attempts/att_1", "leases/lease_1"}, db.calls
    for path, kwargs in reads.items():
        assert kwargs == budget, f"{path} was read without the startup budget: {kwargs}"

    db.calls.clear()
    with control.startup_budget():
        control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    assert db.calls == [("set", "attempts/att_1", budget)], db.calls

    db.calls.clear()
    control.poll(None)
    control.validate_generation()
    control.record_attempt_end(exit_code=0, error=None)
    assert db.calls and all(kwargs == {} for _, _, kwargs in db.calls), db.calls


def test_the_entrypoint_gives_the_control_plane_thirty_seconds_of_ten_second_tries(
    tmp_path, monkeypatch
):
    from google.api_core import exceptions as core

    import agent_worker.__main__ as entrypoint
    from agent_worker.config import WorkerConfig
    from swarm_common.config import Settings

    options = entrypoint.firestore_startup_call_options()
    assert options["timeout"] == 10
    assert options["retry"].timeout == 30
    predicate = options["retry"]._predicate
    for retried in (core.ServiceUnavailable("x"), core.DeadlineExceeded("x"),
                    core.InternalServerError("x")):
        assert predicate(retried), type(retried).__name__
    for final in (core.PermissionDenied("x"), core.NotFound("x"), core.InvalidArgument("x")):
        assert not predicate(final), type(final).__name__

    # And `build_worker` hands them to the control plane it builds.
    monkeypatch.delenv("QUOTA_BROKER_URL", raising=False)
    for name, value in {
        "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
        "TENANT_ID": TENANT, "GENERATION": "1", "RUNNER_PROFILE": "mock",
        "PROJECT_ID": "swarm-test", "LOCAL_ARTIFACT_ROOT": str(tmp_path / "gcs"),
        "WORKSPACE_ROOT": str(tmp_path / "ws"), "DISABLE_CLOUD_MONITORING": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(entrypoint, "_firestore_client", lambda _settings: FakeFirestore())
    settings = Settings.from_env()
    worker = entrypoint.build_worker(WorkerConfig.from_env(settings), settings)
    wired = worker.control.startup_call_options
    assert wired["timeout"] == 10 and wired["retry"].timeout == 30, wired


# ---------------------------------------------------------------------------
# the DNS preflight
# ---------------------------------------------------------------------------


def _answers(host: str) -> Any:
    return [(2, 1, 6, "", ("10.0.0.7", 443))]


def test_the_preflight_reports_each_host_it_resolved():
    from agent_worker.startup import dns_preflight

    report = dns_preflight(["a.example", "b.example"], budget_seconds=2, resolve=_answers)
    assert [r["host"] for r in report] == ["a.example", "b.example"]
    assert all(r["ok"] and r["addresses"] == ["10.0.0.7"] for r in report), report


def test_the_preflight_reports_a_resolver_error_with_its_text():
    import socket

    from agent_worker.startup import dns_preflight

    def nxdomain(host: str) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    [result] = dns_preflight(["gone.example"], budget_seconds=2, resolve=nxdomain)
    assert not result["ok"]
    assert "gaierror" in result["error"] and "Name or service not known" in result["error"]


def test_lookups_that_never_answer_share_one_deadline():
    """Two dropped lookups cost one budget, not two, and an answer is still reported."""
    from agent_worker.startup import dns_preflight

    never = threading.Event()

    def resolve(host: str) -> Any:
        if host.startswith("dropped"):
            never.wait(30)
            raise AssertionError("a dropped lookup was answered")
        return _answers(host)

    started = time.monotonic()
    report = dns_preflight(
        ["dropped-1.example", "ok.example", "dropped-2.example"],
        budget_seconds=0.5,
        resolve=resolve,
    )
    took = time.monotonic() - started
    never.set()
    assert took < 2.0, f"the preflight took {took:.2f}s on a 0.5s budget"
    by_host = {r["host"]: r for r in report}
    assert by_host["ok.example"]["ok"]
    for host in ("dropped-1.example", "dropped-2.example"):
        assert not by_host[host]["ok"]
        assert "no answer within 0.5s" in by_host[host]["error"], by_host[host]


# -- the retries (owner, 2026-09-25) -----------------------------------------


class _Resolver:
    """Fails each name a set number of times, then answers. Counts every ask."""

    def __init__(self, failures: dict[str, int]) -> None:
        self.failures = dict(failures)
        self.asked: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, host: str) -> Any:
        import socket

        with self._lock:
            self.asked.append(host)
            left = self.failures.get(host, 0)
            if left:
                self.failures[host] = left - 1
        if left:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return _answers(host)


def test_one_failed_lookup_is_retried_after_a_backoff_and_the_preflight_passes():
    from agent_worker.startup import dns_preflight_with_retries

    resolver = _Resolver({"firestore.example": 1})
    slept: list[float] = []
    failed: list[tuple[int, list[str], float]] = []

    outcome = dns_preflight_with_retries(
        ["metadata.example", "firestore.example"],
        attempts=3,
        budget_seconds=2,
        backoff=(2.0, 5.0),
        resolve=resolver,
        sleep=slept.append,
        on_failed_attempt=lambda n, bad, wait: failed.append((n, [r["host"] for r in bad], wait)),
    )

    assert outcome.ok, outcome
    assert outcome.attempts == 2
    assert slept == [2.0]
    assert failed == [(1, ["firestore.example"], 2.0)]
    # A name that answered is not asked again.
    assert resolver.asked.count("metadata.example") == 1
    assert resolver.asked.count("firestore.example") == 2
    assert [r["host"] for r in outcome.results] == ["metadata.example", "firestore.example"]


def test_a_resolver_that_keeps_failing_gets_every_attempt_and_every_backoff_then_is_reported():
    from agent_worker.startup import dns_preflight_with_retries

    resolver = _Resolver({"firestore.example": 99})
    slept: list[float] = []
    failed: list[int] = []

    outcome = dns_preflight_with_retries(
        ["firestore.example"],
        attempts=3,
        budget_seconds=2,
        backoff=(2.0, 5.0),
        resolve=resolver,
        sleep=slept.append,
        on_failed_attempt=lambda n, _bad, _wait: failed.append(n),
    )

    assert not outcome.ok
    assert outcome.attempts == 3
    assert resolver.asked == ["firestore.example"] * 3
    # No sleep after the last attempt: the answer is final, and waiting would
    # only hold the slot longer.
    assert slept == [2.0, 5.0]
    # The last failure is the caller's error line, not a retry warning.
    assert failed == [1, 2]
    [result] = outcome.unreachable
    assert result["host"] == "firestore.example" and "gaierror" in result["error"]


def test_the_retry_window_is_inside_what_was_asked_and_inside_the_dispatch_deadline():
    """3-4 attempts over about 30-45 s (owner, 2026-09-25), and well before the reconciler's clock.

    A worker that has not heartbeated is judged by its lease's dispatch
    deadline alone, 300 s after admission (`reconciler.detect.detect_stale_leases`).
    The worker's own "cannot start" has to arrive long before that, or the
    reconciler reclaims the lease as silent and the cause is lost again.
    """
    from agent_worker import startup
    from swarm_common.config import Settings

    assert 3 <= startup.DNS_PREFLIGHT_ATTEMPTS <= 4
    assert len(startup.DNS_PREFLIGHT_BACKOFF_SECONDS) >= startup.DNS_PREFLIGHT_ATTEMPTS - 1
    window = (
        startup.DNS_PREFLIGHT_ATTEMPTS * startup.DNS_PREFLIGHT_BUDGET_SECONDS
        + sum(startup.DNS_PREFLIGHT_BACKOFF_SECONDS[: startup.DNS_PREFLIGHT_ATTEMPTS - 1])
    )
    assert startup.DNS_PREFLIGHT_WINDOW_SECONDS == window
    assert 30 <= window <= 45, window
    dispatch_deadline = Settings.__dataclass_fields__["dispatch_timeout_seconds"].default
    assert window < dispatch_deadline / 4, (window, dispatch_deadline)


@pytest.mark.parametrize(
    ("env", "hosts"),
    [
        ({}, ["metadata.google.internal", "firestore.googleapis.com"]),
        # What the GKE worker env sets (PR #55): an address, which needs no DNS.
        ({"GCE_METADATA_HOST": "169.254.169.254"}, ["169.254.169.254", "firestore.googleapis.com"]),
        ({"GCE_METADATA_HOST": "metadata.internal:8080"}, ["metadata.internal", "firestore.googleapis.com"]),
        ({"GCE_METADATA_ROOT": "legacy.metadata"}, ["legacy.metadata", "firestore.googleapis.com"]),
        ({"GOOGLE_APPLICATION_CREDENTIALS": "/keys/sa.json"}, ["firestore.googleapis.com"]),
        ({"FIRESTORE_EMULATOR_HOST": "firestore:8080"}, ["firestore"]),
        ({"FIRESTORE_EMULATOR_HOST": "[::1]:8080"}, ["::1"]),
    ],
)
def test_the_preflight_resolves_what_the_first_firestore_call_will_need(env, hosts):
    from agent_worker.startup import preflight_hosts

    assert preflight_hosts(env) == hosts


# ---------------------------------------------------------------------------
# signals, and what the first lines may print
# ---------------------------------------------------------------------------


def test_rerouting_signals_keeps_the_sigterm_stack_dump_armed():
    """`signal.signal` silently disarms faulthandler; `route_signals` re-arms it."""
    import faulthandler

    from agent_worker.startup import route_signals

    previous = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def first(_signum: int, _frame: Any) -> None:
        pass

    def second(_signum: int, _frame: Any) -> None:
        pass

    try:
        assert route_signals(first)
        assert route_signals(second)
        assert signal.getsignal(signal.SIGTERM) is second
        # True only if the dump was still registered after the second route.
        assert faulthandler.unregister(signal.SIGTERM) is True
    finally:
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, previous_int)


def test_disarming_the_stack_dump_leaves_the_python_handler_in_place():
    """Once the runner exists the dump is disarmed, and the lifecycle's handler still runs."""
    import faulthandler

    from agent_worker import startup

    previous = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def handler(_signum: int, _frame: Any) -> None:
        pass

    try:
        assert startup.route_signals(handler)
        assert startup.stack_dump_armed()
        assert startup.disarm_stack_dump() is True
        assert not startup.stack_dump_armed()
        assert signal.getsignal(signal.SIGTERM) is handler
        # False: nothing left registered to unregister.
        assert faulthandler.unregister(signal.SIGTERM) is False
        # Idempotent: an in-place restart of the runner disarms again.
        assert startup.disarm_stack_dump() is False
    finally:
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, previous_int)


def test_a_sigterm_once_the_runner_exists_is_one_info_line(db, worker_factory, log_stream):
    """The handler in its last window: set the flag, say so at INFO, raise nothing.

    Called the way the interpreter calls it, on the main thread, with the
    worker in the window a running agent is in. `test_startup_is_loud.py`
    sends a real SIGTERM to a real running worker and checks stderr.
    """
    seed_attempt(db)
    worker, _, _ = worker_factory()
    previous = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    try:
        worker._install_signal_handlers()
        worker.phases.enter("runner")
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, previous_int)

    assert worker._interrupted is True
    said = [r for r in _records(log_stream) if "SIGTERM" in str(r.get("message", ""))]
    assert len(said) == 1, said
    assert said[0]["message"] == "stopping: SIGTERM in phase runner", said[0]
    assert said[0]["severity"] == "INFO", said[0]
    assert db.writes == [], "the handler wrote something; the supervision loop owns the stop"


def test_a_sigterm_exits_even_when_the_logger_is_mid_line():
    """The handler must not wait for a lock the interrupted main thread holds."""
    from agent_worker.logs import StructuredLogger
    from agent_worker.startup import Phases

    logger = StructuredLogger(stream=io.StringIO())
    codes: list[int] = []
    phases = Phases(logger, hard_exit=codes.append)
    logger._lock.acquire()  # the main thread was inside `log` when the signal came
    try:
        started = time.monotonic()
        phases.exit_on_signal(signal.SIGTERM)
        took = time.monotonic() - started
    finally:
        logger._lock.release()
    assert codes == [INTERRUPTED]
    assert took < 3, f"the handler waited {took:.1f}s on the logger"


def test_the_first_line_prints_the_attempts_identity_and_nothing_else_from_the_env():
    from agent_worker.startup import Phases, bootstrap_logger

    stream = io.StringIO()
    env = {
        "TASK_ID": "task_1",
        "ATTEMPT_ID": "att_1",
        "GENERATION": "3",
        "ANTHROPIC_API_KEY": "sk-ant-must-never-be-printed-0123456789",
        "GIT_TOKEN": "ghp_must-never-be-printed",
    }
    Phases(bootstrap_logger(env, stream=stream)).started(execution="pod-1")
    [line] = stream.getvalue().splitlines()
    record = json.loads(line)
    assert record["message"] == "worker process started"
    assert record["labels"] == {"task_id": "task_1", "attempt_id": "att_1", "generation": 3}
    assert "must-never-be-printed" not in line
