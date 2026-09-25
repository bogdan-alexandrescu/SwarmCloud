"""The startup window, run through Firestore's own transaction code.

Every other lifecycle test runs transactions through `FakeTransactionRunner`,
which calls the body and nothing else. Production runs them through
`firestore.transactional`, which begins with an RPC, commits with another,
and on ANY exception rolls back before it re-raises. What the rollback raises
replaces the exception that was unwinding (see `library_transaction`). Two
review findings on PR #57 live exactly there, and no in-memory runner could
show either:

  * **A SIGTERM inside `advance_to_running`'s transaction.** The handler
    raises `StartupInterrupted`. If it lands before `begin_transaction` has
    returned, the library's rollback raises `ValueError("... cannot be rolled
    back.")`. If it lands after, and Firestore refuses the rollback, the
    rollback RPC's error is raised instead. Either replaced the interrupt, and
    `run()`'s crash handler then wrote FAILED over the task, emitted FAILED,
    released the lease and exited 1. The PR said this window writes no task,
    lease, pool or event and exits 143.
  * **A control plane that goes away after the generation check.** The
    startup budget turns a 45 s outage into a `RetryError` in 30 s, and
    `run()`'s crash handler turned that into a terminal FAILED. The reconciler
    never requeues FAILED, so `max_attempts` was never consulted. The same
    outage one step earlier, at the generation check, exits 78 and the task is
    retried.

And one undelivered part of item (4): the transaction's own RPCs (begin,
commit, rollback), its reads, the STARTING/RUNNING events and the first
heartbeat kept the library's 60 s and 300 s defaults, and so did the tenant
read and an upstream step's task read. The budget tests below hold every
Firestore call before the runner to the budget, and every call after it to
the defaults.

The SIGTERM is delivered by calling the handler `run()` installed, from
inside the RPC or read it interrupts, as `test_startup_budgets_and_signals.py`
explains.
"""

from __future__ import annotations

import copy
import io
import json
import signal
from typing import Any, Callable

import pytest
from fakes import ExplodingChildProcess
from library_transaction import TransactionalFirestore

from agent_worker import lifecycle
from agent_worker.control import ControlPlane, FirestoreTransactionRunner
from agent_worker.errors import ExitCode, FencedWriteRefused
from agent_worker.startup import StartupInterrupted
from swarm_common.states import TaskState

from conftest import TENANT, build_worker, seed_attempt, seed_tenant

#: 128 + SIGTERM, restated: the number is the contract with the pod's reader.
INTERRUPTED = 143
TASK, LEASE, ATTEMPT = "tasks/task_1", "leases/lease_1", "attempts/att_1"


@pytest.fixture(autouse=True)
def _restore_signal_handlers() -> Any:
    """`Worker.run` installs its SIGTERM and SIGINT handlers in this process."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def _budget() -> dict[str, Any]:
    """The budget the entrypoint gives the control plane: real `Retry` objects."""
    from agent_worker.__main__ import firestore_startup_call_options

    return firestore_startup_call_options()


def _worker(
    db: TransactionalFirestore,
    store: Any,
    tmp_path: Any,
    log_stream: io.StringIO,
    *,
    budgeted: bool,
    task_id: str = "task_1",
    attempt_id: str = "att_1",
    lease_id: str = "lease_1",
    **build_kwargs: Any,
) -> Any:
    """A worker whose transactions go through the production runner and the library."""
    runner = FirestoreTransactionRunner(db)
    worker, _, _ = build_worker(
        db, store, tmp_path, log_stream,
        task_id=task_id, attempt_id=attempt_id, lease_id=lease_id, txn_runner=runner,
        **build_kwargs,
    )
    if budgeted:
        worker.control = ControlPlane(
            db,
            task_id=task_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            tenant_id=TENANT,
            generation=1,
            logger=worker.log,
            txn_runner=runner,
            startup_call_options=_budget(),
        )
    return worker


def _deliver_sigterm() -> None:
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), f"no Python SIGTERM handler is installed: {handler!r}"
    handler(signal.SIGTERM, None)


def _raiser(error: BaseException) -> Callable[..., None]:
    def _raise(*_args: Any) -> None:
        raise error

    return _raise


def _records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


def _owned(db: TransactionalFirestore) -> dict[str, Any]:
    """What the attempt must leave alone: the task, its event stream, the lease, the pools."""
    return copy.deepcopy({
        path: doc for path, doc in db.documents.items()
        if path.startswith(("tasks/", "leases/", "pools/"))
    })


def _mark(db: TransactionalFirestore, mark: dict[str, Any]) -> None:
    if not mark:
        mark["writes"] = len(db.writes)
        mark["owned"] = _owned(db)


def _assert_left_alone(db: TransactionalFirestore, mark: dict[str, Any]) -> None:
    written = sorted({path for _, path, _ in db.writes[mark["writes"]:]})
    assert set(written) <= {ATTEMPT}, f"written after the phase was interrupted: {written}"
    now = _owned(db)
    changed = sorted(
        path for path in set(now) | set(mark["owned"]) if now.get(path) != mark["owned"].get(path)
    )
    assert not changed, f"the task, its events, the lease or a pool changed: {changed}"


def _unreachable() -> BaseException:
    from google.api_core import exceptions as core

    return core.RetryError(
        "Timeout of 30.0s exceeded",
        cause=core.ServiceUnavailable("failed to connect to all addresses"),
    )


# ---------------------------------------------------------------------------
# a SIGTERM inside advance_to_running's transactions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budgeted", [False, True], ids=["library-defaults", "startup-budget"])
def test_a_sigterm_while_a_transaction_begins_exits_143_and_leaves_the_task(
    store, tmp_path, log_stream, monkeypatch, budgeted
):
    """Before `begin_transaction` returns there is no transaction id to roll back.

    The library's `_rollback` raises `ValueError("... cannot be rolled back.")`
    for that, and the ValueError is what used to reach `run()`.
    """
    db = TransactionalFirestore()
    seed_attempt(db)
    worker = _worker(db, store, tmp_path, log_stream, budgeted=budgeted)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    mark: dict[str, Any] = {}

    def sigterm_in_begin() -> None:
        if worker.phases.current == "advance_to_running" and not mark:
            _mark(db, mark)
            _deliver_sigterm()

    db.rpcs.on_begin = sigterm_in_begin

    exit_code = worker.run()

    assert mark, "the SIGTERM was never delivered"
    assert exit_code == INTERRUPTED, _records(log_stream)[-4:]
    _assert_left_alone(db, mark)
    attempt = db.doc(ATTEMPT)
    assert attempt["exit_code"] == INTERRUPTED
    assert "advance_to_running" in attempt["error"], attempt["error"]
    said = [r for r in _records(log_stream) if "SIGTERM" in r["message"]]
    assert said and said[-1]["phase"] == "advance_to_running", said


@pytest.mark.parametrize("budgeted", [False, True], ids=["library-defaults", "startup-budget"])
def test_a_sigterm_while_a_transaction_reads_exits_143_even_when_the_rollback_fails(
    store, tmp_path, log_stream, monkeypatch, budgeted
):
    """Once the transaction has begun, the library does roll it back, over the network.

    Firestore refuses the rollback here with UNAVAILABLE, the likely reason the
    read was slow enough to be interrupted. That error used to replace the
    interrupt.
    """
    from google.api_core import exceptions as core

    db = TransactionalFirestore()
    seed_attempt(db)
    worker = _worker(db, store, tmp_path, log_stream, budgeted=budgeted)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    mark: dict[str, Any] = {}

    def sigterm_in_a_transactional_read(ref: Any, transactional: bool) -> None:
        if transactional and worker.phases.current == "advance_to_running" and not mark:
            _mark(db, mark)
            _deliver_sigterm()

    db.on_read = sigterm_in_a_transactional_read
    db.rpcs.on_rollback = _raiser(core.ServiceUnavailable("the rollback could not be sent"))

    exit_code = worker.run()

    assert mark, "the SIGTERM was never delivered"
    assert "rollback" in db.rpcs.names(), "the rollback this test is about was never made"
    assert exit_code == INTERRUPTED, _records(log_stream)[-4:]
    _assert_left_alone(db, mark)
    assert db.doc(ATTEMPT)["exit_code"] == INTERRUPTED


# ---------------------------------------------------------------------------
# a control plane that goes away after the generation check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budgeted", [False, True], ids=["library-defaults", "startup-budget"])
@pytest.mark.parametrize(
    "phase", ["advance_to_running", "restore_checkpoint", "revalidate_generation", "quota_preflight"]
)
def test_a_control_plane_outage_before_the_runner_leaves_the_task_for_the_reconciler(
    store, tmp_path, log_stream, monkeypatch, phase, budgeted
):
    """Exit 78 with the task, the lease and the stream untouched, as at the generation check.

    In `advance_to_running` the outage is in `begin_transaction`, and without
    the budget the library's rollback replaces the `RetryError` with its own
    ValueError. That has to be recognised by what it replaced. In the other
    phases it is the phase's own read of the task.
    """
    db = TransactionalFirestore()
    seed_attempt(db)
    worker = _worker(db, store, tmp_path, log_stream, budgeted=budgeted)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    mark: dict[str, Any] = {}

    def outage_in_begin() -> None:
        if worker.phases.current == phase:
            _mark(db, mark)
            raise _unreachable()

    def outage_in_the_phases_read(ref: Any, transactional: bool) -> None:
        if not transactional and ref.path == TASK and worker.phases.current == phase:
            _mark(db, mark)
            raise _unreachable()

    if phase == "advance_to_running":
        db.rpcs.on_begin = outage_in_begin
    else:
        db.on_read = outage_in_the_phases_read

    exit_code = worker.run()

    assert mark, f"Firestore never went away in {phase}"
    assert exit_code == ExitCode.CONFIG, _records(log_stream)[-4:]
    _assert_left_alone(db, mark)
    assert db.doc(TASK)["state"] != TaskState.FAILED.value
    attempt = db.doc(ATTEMPT)
    assert attempt["exit_code"] == ExitCode.CONFIG
    assert phase in attempt["error"], attempt["error"]
    errors = [r for r in _records(log_stream) if r["severity"] == "ERROR"]
    assert errors and errors[-1]["phase"] == phase, errors[-1:]
    assert errors[-1]["exit_code"] == ExitCode.CONFIG


def test_a_refusal_before_the_runner_still_fails_the_attempt(
    store, tmp_path, log_stream, monkeypatch
):
    """Only unavailability is left to the reconciler. A refusal is an answer.

    PERMISSION_DENIED means Firestore was reached and said no, and it will say
    no again on the next attempt. The attempt owns its task (the generation
    check passed), so it fails it with the reason, as it did before this
    change. Pinned so that the line between the two is a decision.
    """
    from google.api_core import exceptions as core

    db = TransactionalFirestore()
    seed_attempt(db)
    worker = _worker(db, store, tmp_path, log_stream, budgeted=True)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    def refused(ref: Any, transactional: bool) -> None:
        if not transactional and ref.path == TASK and worker.phases.current == "restore_checkpoint":
            raise core.PermissionDenied("Missing or insufficient permissions.")

    db.on_read = refused

    assert worker.run() == ExitCode.FAILED
    task = db.doc(TASK)
    assert task["state"] == TaskState.FAILED.value
    assert "insufficient permissions" in task["last_error"], task["last_error"]


# ---------------------------------------------------------------------------
# the budget on every call before the runner, and on none after it
# ---------------------------------------------------------------------------


def _is_budget(kwargs: dict[str, Any], budget: dict[str, Any]) -> bool:
    return (
        set(kwargs) == {"retry", "timeout"}
        and kwargs["retry"] is budget["retry"]
        and kwargs["timeout"] == budget["timeout"]
    )


def test_every_firestore_call_before_the_runner_carries_the_startup_budget_and_none_after(
    store, tmp_path, log_stream, monkeypatch
):
    """Every read, write and transaction RPC from the generation check to the runner.

    That covers the generation check and its re-check, `advance_to_running`'s
    state read and its three transactions (begin, both reads, commit), the
    STARTING and RUNNING events, the heartbeats and the first HEARTBEAT event,
    the checkpoint restore's task read, an upstream step's task for input
    staging, and the quota preflight's task and lease reads. The attempt
    stages an upstream artifact so the upstream read happens. The tenant and
    quota documents, which the keyless mock runner never reads, are the next
    test's.

    A commit is held to the budget's deadline, but it is retried only on
    errors that mean it was not applied. DEADLINE_EXCEEDED can arrive after a
    commit has landed, and a second commit of the same transaction is refused,
    which would report a transition that had in fact been made.

    After the runner starts, every call has the library's defaults again. How
    long a running agent tolerates a Firestore outage is a separate decision.
    """
    from google.api_core import exceptions as core

    db = TransactionalFirestore()
    # An upstream step that succeeded and uploaded summary.md, run through the
    # in-memory runner. Only what the downstream attempt does is measured.
    seed_attempt(
        db, task_id="task_up", attempt_id="att_up", lease_id="lease_up",
        task_input={"prompt": "upstream", "steps": 1, "sleep_seconds": 0.01,
                    "artifact_name": "summary.md", "artifact_text": "found it\n"},
    )
    upstream, _, _ = build_worker(
        db, store, tmp_path, log_stream, task_id="task_up", attempt_id="att_up", lease_id="lease_up"
    )
    assert upstream.run() == ExitCode.OK

    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "downstream", "steps": 1, "sleep_seconds": 0.05},
    )
    db.doc("tasks/task_2")["metadata"] = {"input_from": {"task_up": "summary.md"}}
    worker = _worker(
        db, store, tmp_path, log_stream, budgeted=True,
        task_id="task_2", attempt_id="att_2", lease_id="lease_2",
    )
    budget = worker.control.startup_call_options

    started_at: list[tuple[int, int]] = []
    real_child = lifecycle.ChildProcess

    class _MarkedChild(real_child):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started_at.append((len(db.calls), len(db.rpcs.calls)))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "ChildProcess", _MarkedChild)
    db.calls.clear()
    db.rpcs.calls.clear()

    assert worker.run() == ExitCode.OK, _records(log_stream)[-4:]
    assert started_at, "the runner never started"
    calls_before, rpcs_before = started_at[0]
    before = [c for c in db.calls[:calls_before] if not c[0].startswith(("txn.set", "txn.update"))]
    after = [c for c in db.calls[calls_before:] if not c[0].startswith(("txn.set", "txn.update"))]

    unbudgeted = [(kind, path, kwargs) for kind, path, kwargs in before if not _is_budget(kwargs, budget)]
    assert not unbudgeted, f"called before the runner without the startup budget: {unbudgeted}"

    rpc_unbudgeted = [
        (name, kwargs) for name, kwargs in db.rpcs.calls[:rpcs_before]
        if name != "commit" and not _is_budget(kwargs, budget)
    ]
    assert not rpc_unbudgeted, f"transaction RPCs without the startup budget: {rpc_unbudgeted}"
    commits = [kwargs for name, kwargs in db.rpcs.calls[:rpcs_before] if name == "commit"]
    assert len(commits) >= 3, db.rpcs.calls[:rpcs_before]
    for kwargs in commits:
        assert kwargs.get("timeout") == budget["timeout"], kwargs
        retry = kwargs.get("retry")
        assert retry is not None and retry.timeout == budget["retry"].timeout, kwargs
        assert retry._predicate(core.ServiceUnavailable("x"))
        assert not retry._predicate(core.DeadlineExceeded("x")), "a commit that may have landed was retried"

    # Proof that each kind of call was made, and so was measured.
    seen = {(kind, path.split("/")[0] if "/events/" not in path else "events") for kind, path, _ in before}
    for expected in [
        ("get", "tasks"), ("get", "leases"), ("get", "attempts"),
        ("txn.get", "tasks"), ("txn.get", "leases"),
        ("set", "attempts"), ("set", "events"), ("update", "leases"),
    ]:
        assert expected in seen, f"{expected} was never called before the runner: {sorted(seen)}"
    assert any(kind == "get" and path == "tasks/task_up" for kind, path, _ in before), (
        "the upstream step's task was never read"
    )
    assert db.rpcs.names()[:rpcs_before].count("begin") >= 3

    leaked = [(kind, path, kwargs) for kind, path, kwargs in after if kwargs]
    assert not leaked, f"the startup budget outlived the startup: {leaked}"
    rpc_leaked = [(name, kwargs) for name, kwargs in db.rpcs.calls[rpcs_before:] if kwargs]
    assert not rpc_leaked, f"the startup budget outlived the startup: {rpc_leaked}"
    assert after, "nothing was called after the runner started; the second half measured nothing"


class _StopAtTheRunner(BaseException):
    """Raised where the runner would be built. Not an Exception, so nothing handles it."""


def test_the_tenant_and_quota_reads_before_the_runner_carry_the_budget_too(
    store, tmp_path, log_stream, monkeypatch
):
    """The two reads a keyless mock runner never makes.

    A profile that needs a provider credential reads the tenant document
    (`secrets.load_tenant`) and, in the quota preflight, the provider's quota
    document. The attempt is stopped where the runner would be built: what
    the agent would do after that is not the question here.
    """
    db = TransactionalFirestore()
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker = _worker(db, store, tmp_path, log_stream, budgeted=True, runner_profile="claude-code")
    budget = worker.control.startup_call_options

    def stop(*_args: Any, **_kwargs: Any) -> None:
        raise _StopAtTheRunner()

    monkeypatch.setattr(lifecycle, "ChildProcess", stop)
    db.calls.clear()

    with pytest.raises(_StopAtTheRunner):
        worker.run()

    reads = {(kind, path) for kind, path, _ in db.calls}
    assert ("get", f"tenants/{TENANT}") in reads, sorted(reads)
    assert ("get", f"quota/anthropic:{TENANT}") in reads, sorted(reads)
    unbudgeted = [
        (kind, path, kwargs) for kind, path, kwargs in db.calls
        if not kind.startswith(("txn.set", "txn.update")) and not _is_budget(kwargs, budget)
    ]
    assert not unbudgeted, f"called before the runner without the startup budget: {unbudgeted}"


# ---------------------------------------------------------------------------
# the budgeted transaction, on its own
# ---------------------------------------------------------------------------


def test_a_budgeted_transaction_makes_each_rpc_within_the_budget():
    from google.api_core import exceptions as core

    db = TransactionalFirestore()
    budget = _budget()

    assert FirestoreTransactionRunner(db).run(lambda txn: "done", call_options=budget) == "done"

    assert db.rpcs.names() == ["begin", "commit"]
    (_, begin), (_, commit) = db.rpcs.calls
    assert _is_budget(begin, budget), begin
    assert commit["timeout"] == budget["timeout"]
    assert commit["retry"].timeout == budget["retry"].timeout
    assert commit["retry"]._predicate(core.ResourceExhausted("x"))
    assert not commit["retry"]._predicate(core.InternalServerError("x"))


@pytest.mark.parametrize("kind", ["sigterm", "retry-budget-spent"])
def test_an_error_while_a_budgeted_transaction_begins_leaves_as_itself(kind):
    """No transaction id, so nothing to roll back, and nothing replaces the error."""
    db = TransactionalFirestore()
    error = (
        StartupInterrupted(signal.SIGTERM, "advance_to_running") if kind == "sigterm"
        else _unreachable()
    )
    db.rpcs.on_begin = _raiser(error)

    with pytest.raises(BaseException) as caught:
        FirestoreTransactionRunner(db).run(lambda txn: None, call_options=_budget())

    assert caught.value is error, f"{error!r} left as {caught.value!r}"
    assert db.rpcs.names() == ["begin"], "a transaction that never began was rolled back"


def test_a_refused_body_is_rolled_back_within_the_budget_and_leaves_as_itself():
    db = TransactionalFirestore()
    budget = _budget()
    refusal = FencedWriteRefused(1, 2, "task has moved to a newer generation", write="transition")

    with pytest.raises(FencedWriteRefused) as caught:
        FirestoreTransactionRunner(db).run(_raiser(refusal), call_options=budget)

    assert caught.value is refusal
    assert db.rpcs.names() == ["begin", "rollback"]
    assert _is_budget(db.rpcs.calls[1][1], budget)


def test_under_a_sigterm_a_failed_rollback_is_one_short_try_and_does_not_replace_it():
    """The process is leaving. One try, then the interrupt goes on, with a note."""
    from google.api_core import exceptions as core

    db = TransactionalFirestore()
    budget = _budget()
    interrupt = StartupInterrupted(signal.SIGTERM, "advance_to_running")
    db.rpcs.on_rollback = _raiser(core.ServiceUnavailable("the rollback could not be sent"))

    with pytest.raises(StartupInterrupted) as caught:
        FirestoreTransactionRunner(db).run(_raiser(interrupt), call_options=budget)

    assert caught.value is interrupt
    [rollback] = [kwargs for name, kwargs in db.rpcs.calls if name == "rollback"]
    assert rollback["retry"] is None, "a rollback under SIGTERM was retried"
    assert rollback["timeout"] == budget["timeout"]
    notes = getattr(interrupt, "__notes__", [])
    assert any("ServiceUnavailable" in note for note in notes), notes


def test_without_a_budget_the_library_replaces_an_interrupt_with_its_own_error():
    """Why the lifecycle cannot rely on the exception type reaching it.

    This is the library's behaviour, and this test passes before and after
    the change. It pins what `Worker._execute` has to survive: the interrupt
    survives only as the ValueError's `__context__`.
    """
    db = TransactionalFirestore()
    interrupt = StartupInterrupted(signal.SIGTERM, "advance_to_running")
    db.rpcs.on_begin = _raiser(interrupt)

    with pytest.raises(ValueError, match="cannot be rolled back") as caught:
        FirestoreTransactionRunner(db).run(lambda txn: None)

    assert caught.value.__context__ is interrupt
