"""`RemoteControlPlane`: `ControlPlane`'s methods, over swarm-api's state routes.

Lane ISO-WORKER-CORE (docs/design/tenant-settings-integrity.md §4, §6; part of
#1457). The red-first rules this file holds:

  * `state/open` is the first state call, and no other is SENT before it;
  * a 409 is the fence: `FencedError` at the gate, `FencedWriteRefused` on a
    write, fenced `ControlSignals` on a poll, and nothing is written;
  * a `RemoteControlPlane` passes the `ControlPlane` method tests -- the walk
    to RUNNING, the heartbeat, the checkpoint record, the usage, each park,
    the finish, the retryable failure, the events, the parents, the restore's
    attempt, the carry -- through a fake transport (`state_routes_harness`:
    the worker's own `ChildApi`, answered in process by swarm-api's real app).

Each test reads the documents the routes wrote, the way the direct path's
tests read the documents `ControlPlane` wrote. Offline: no credentials, no
emulator.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from agent_worker.control import ControlPlane, ControlSignals, RemoteControlPlane
from agent_worker.errors import (
    ControlPlaneError,
    FencedError,
    FencedWriteRefused,
    TenantMismatchError,
)
from agent_worker.hardening import FAILED as MEMORY_FAILED
from swarm_common.models import EndCause, utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from state_routes_harness import (
    ATTEMPT,
    LEASE,
    POOLS,
    TASK,
    TENANT,
    Recorder,
    RoutesOpener,
    api_client,
    events,
    remote_control,
    seed_attempt,
)

DIGEST = "ab" * 32


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


@pytest.fixture
def opener(api_db) -> RoutesOpener:
    return RoutesOpener(api_client(api_db))


def _task(db: ApiFirestore, task_id: str = TASK) -> dict:
    return db.docs[f"tasks/{task_id}"]


def _lease(db: ApiFirestore) -> dict:
    return db.docs[f"leases/{LEASE}"]


def _running(api_db, opener, **extra) -> RemoteControlPlane:
    """A worker that opened its attempt and walked it to RUNNING."""
    seed_attempt(api_db, **extra)
    control = remote_control(opener)
    control.validate_generation()
    control.advance_to_running()
    assert _task(api_db)["state"] == "RUNNING"
    return control


def _fence(db: ApiFirestore) -> None:
    """The reconciler moved the task to a newer generation."""
    _task(db)["current_generation"] = 2


# ---------------------------------------------------------------------------
# state/open comes first
# ---------------------------------------------------------------------------


def test_state_open_is_the_first_state_call_and_registers_the_key(api_db, opener):
    seed_attempt(api_db)
    control = remote_control(opener)

    signals = control.validate_generation()

    assert opener.calls[0] == ("POST", "open")
    assert signals.state is TaskState.LEASED and signals.generation == 1
    assert not signals.is_fenced(1)
    assert control.opened and control.attempt_key is not None
    registered = [d for p, d in api_db.docs.items() if p.startswith("child_keys/")]
    assert len(registered) == 1
    assert registered[0]["public_key"] == control.attempt_key.public
    assert sorted(registered[0]["purpose"]) == ["child", "state"]
    assert control.registration["registered"] == "key"


def test_open_carries_no_proof_and_every_later_call_carries_one(api_db, opener):
    seed_attempt(api_db)
    control = remote_control(opener)
    control.validate_generation()
    control.heartbeat()

    def has_proof(headers: dict) -> bool:
        return any(k.lower() == "x-swarm-attempt-proof" for k in headers)

    assert not has_proof(opener.headers[0])
    assert all(has_proof(h) for h in opener.headers[1:]) and len(opener.headers) == 2


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.heartbeat(),
        lambda c: c.emit(EventType.HEARTBEAT, {}),
        lambda c: c.poll(),
        lambda c: c.fetch_task(),
        lambda c: c.ensure_owner(write="checkpoint"),
        lambda c: c.advance_to_running(),
        lambda c: c.record_attempt_start(backend="cloud_run", execution_name=None),
        lambda c: c.record_checkpoint(checkpoint_id="c1", uri="file:///x", size_bytes=1,
                                      seq=1, archive_sha256=DIGEST),
        lambda c: c.finish(state=TaskState.SUCCEEDED, exit_code=0),
        lambda c: c.fail_retryably(exit_code=1, error="e", cause="runner"),
        lambda c: c.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
                         next_eligible_at=utcnow()),
        lambda c: c.release_lease("terminal:FAILED"),
        lambda c: c.fetch_parent_states(["task_p"]),
    ],
)
def test_no_state_call_is_sent_before_state_open(api_db, opener, call):
    seed_attempt(api_db)
    control = remote_control(opener)
    before = {p: dict(d) for p, d in api_db.docs.items()}

    with pytest.raises((ControlPlaneError, FencedError)):
        call(control)

    assert opener.calls == []
    assert {p: dict(d) for p, d in api_db.docs.items()} == before


def test_an_unprotected_heap_registers_a_state_only_key(api_db, opener):
    seed_attempt(api_db)
    control = remote_control(opener, memory_status=MEMORY_FAILED)

    control.validate_generation()

    registered = [d for p, d in api_db.docs.items() if p.startswith("child_keys/")]
    assert registered[0]["purpose"] == ["state"] and registered[0]["unprotected"] is True
    control.heartbeat()  # the key still signs this attempt's state


def test_a_restore_attempt_is_served_by_a_second_open_with_the_same_key(api_db, opener):
    earlier = "att_remote_0"
    seed_attempt(api_db, latest_checkpoint=(
        f"gs://bucket/tenants/{TENANT}/tasks/{TASK}/attempts/{earlier}/checkpoints/c1/"))
    api_db.docs[f"attempts/{earlier}"] = {
        "attempt_id": earlier, "task_id": TASK, "tenant_id": TENANT, "generation": 1,
        "checkpoints": ["c1"], "checkpoint_sha256": {"c1": DIGEST},
    }
    control = remote_control(opener)

    control.validate_generation()

    assert opener.routes() == ["open", "open"]
    restored = control.fetch_attempt(earlier)
    assert restored["checkpoint_sha256"] == {"c1": DIGEST}
    assert control.fetch_attempt("att_never_named") is None
    assert len([p for p in api_db.docs if p.startswith("child_keys/")]) == 1


# ---------------------------------------------------------------------------
# A 409 is the fence (invariant 5)
# ---------------------------------------------------------------------------


def test_open_on_a_superseded_attempt_raises_fenced_and_sends_nothing_else(api_db, opener):
    seed_attempt(api_db)
    _fence(api_db)
    control = remote_control(opener)

    with pytest.raises(FencedError):
        control.validate_generation()

    assert opener.routes() == ["open"]


def test_a_write_after_the_fence_is_refused_and_writes_nothing(api_db, opener):
    control = _running(api_db, opener)
    _fence(api_db)
    task_before, lease_before = dict(_task(api_db)), dict(_lease(api_db))
    pools_before = {n: api_db.docs[f"pools/{n}"]["active"] for n in POOLS}
    events_before = len(events(api_db))

    with pytest.raises(FencedWriteRefused):
        control.finish(state=TaskState.SUCCEEDED, exit_code=0)
    with pytest.raises(FencedWriteRefused):
        control.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
                     next_eligible_at=utcnow() + timedelta(minutes=5))
    with pytest.raises(FencedWriteRefused):
        control.fail_retryably(exit_code=1, error="boom", cause="runner_error")
    with pytest.raises(FencedWriteRefused):
        control.record_checkpoint(checkpoint_id="c1", uri="file:///tmp/c1", size_bytes=1,
                                  seq=1, archive_sha256=DIGEST)

    assert _task(api_db) == task_before
    assert _lease(api_db) == lease_before
    assert {n: api_db.docs[f"pools/{n}"]["active"] for n in POOLS} == pools_before
    assert len(events(api_db)) == events_before


def test_the_gate_and_the_poll_read_the_fence(api_db, opener):
    control = _running(api_db, opener)
    _fence(api_db)

    with pytest.raises(FencedError):
        control.validate_generation()
    signals = control.poll()
    assert isinstance(signals, ControlSignals) and signals.is_fenced(1)
    with pytest.raises(FencedWriteRefused):
        control.ensure_owner(write="checkpoint")


def test_a_heartbeat_after_the_fence_is_refused_with_nothing_written(api_db, opener):
    control = _running(api_db, opener)
    _fence(api_db)
    lease_before = dict(_lease(api_db))

    assert control.heartbeat() is False
    assert _lease(api_db) == lease_before


# ---------------------------------------------------------------------------
# The ControlPlane method tests, through the routes
# ---------------------------------------------------------------------------


def test_the_walk_to_running_writes_each_state_and_one_event_for_each(api_db, opener):
    seed_attempt(api_db)
    control = remote_control(opener)
    control.validate_generation()

    control.advance_to_running(starting_detail={"phase": "child_key"})

    task = _task(api_db)
    assert task["state"] == "RUNNING" and task["started_at"] is not None
    kinds = [e["type"] for e in sorted(events(api_db), key=lambda e: e["at"])]
    assert kinds == ["dispatched", "starting", "running"]
    (starting,) = events(api_db, kind="starting")
    assert starting["detail"]["phase"] == "child_key"
    assert all(e["generation"] == 1 and e["attempt_id"] == ATTEMPT for e in events(api_db))
    control.advance_to_running()  # already RUNNING: nothing more
    assert len(events(api_db)) == 3


def test_advance_to_starting_owes_no_starting_event(api_db, opener):
    seed_attempt(api_db)
    control = remote_control(opener)
    control.validate_generation()

    assert control.advance_to_starting() is False
    assert _task(api_db)["state"] == "STARTING"
    assert len(events(api_db, kind="starting")) == 1


def test_a_transition_the_routes_do_not_walk_is_refused_before_sending(api_db, opener):
    control = _running(api_db, opener)
    sent = len(opener.calls)

    with pytest.raises(ControlPlaneError):
        control.transition(TaskState.PARKED, fields={"park_reason": "X"})

    assert len(opener.calls) == sent


def test_record_attempt_start_creates_the_attempt_document(api_db, opener):
    seed_attempt(api_db)
    control = remote_control(opener)
    control.validate_generation()

    control.record_attempt_start(backend="cloud_run", execution_name="exec-1")

    doc = api_db.docs[f"attempts/{ATTEMPT}"]
    assert doc["tenant_id"] == TENANT and doc["task_id"] == TASK and doc["lease_id"] == LEASE
    assert doc["backend"] == "cloud_run" and doc["execution_name"] == "exec-1"
    assert doc["completed_at"] is None and doc["checkpoints"] == []


def test_a_heartbeat_extends_this_lease(api_db, opener):
    control = _running(api_db, opener)
    before = _lease(api_db)["expires_at"]

    assert control.heartbeat() is True

    lease = _lease(api_db)
    assert lease["heartbeat_at"] is not None and lease["expires_at"] > before


def test_a_checkpoint_records_its_digest_the_pointer_and_the_event(api_db, opener):
    control = _running(api_db, opener)
    uri = f"gs://bucket/tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/checkpoints/c1/"

    control.record_checkpoint(checkpoint_id="c1", uri=uri, size_bytes=10, seq=1,
                              archive_sha256=DIGEST)

    attempt = api_db.docs[f"attempts/{ATTEMPT}"]
    assert attempt["checkpoints"] == ["c1"] and attempt["checkpoint_sha256"] == {"c1": DIGEST}
    assert _task(api_db)["latest_checkpoint"] == uri
    (event,) = events(api_db, kind="checkpoint_completed")
    assert event["detail"]["checkpoint_id"] == "c1"


def test_a_checkpoint_without_a_digest_is_refused_before_anything_is_sent(api_db, opener):
    control = _running(api_db, opener)
    sent = len(opener.calls)

    with pytest.raises(ValueError):
        control.record_checkpoint(checkpoint_id="c1", uri="file:///tmp/c1", size_bytes=1,
                                  seq=1, archive_sha256=None)  # type: ignore[arg-type]

    assert len(opener.calls) == sent


def test_usage_lands_on_the_attempt_as_typed_fields(api_db, opener):
    control = _running(api_db, opener)
    control.record_attempt_start(backend="cloud_run", execution_name=None)

    control.record_resource_usage(peak_rss_bytes=100, peak_disk_bytes=200, oom_near_miss=True)
    control.record_cpu_usage({"cpu_seconds": 3, "cpu_limit_cores": 2.0,
                              "cpu_limit_source": "cgroup", "unknown": 9})
    control.record_spend({"input_tokens": 5, "output_tokens": True, "total_cost_usd": 0.25,
                          "num_turns": 3})

    doc = api_db.docs[f"attempts/{ATTEMPT}"]
    assert (doc["peak_rss_bytes"], doc["peak_disk_bytes"], doc["oom_near_miss"]) == (100, 200, True)
    assert doc["cpu_seconds"] == 3.0 and doc["cpu_limit_source"] == "cgroup"
    assert doc["cpu_measured_at"] is not None and "unknown" not in doc
    assert doc["input_tokens"] == 5 and doc["cost_usd"] == 0.25
    assert "output_tokens" not in doc and "num_turns" not in doc


def test_a_limit_alone_and_an_empty_spend_send_nothing(api_db, opener):
    control = _running(api_db, opener)
    sent = len(opener.calls)

    control.record_cpu_usage({"cpu_limit_cores": 2.0})
    control.record_spend({})
    control.record_spend({"num_turns": 3})

    assert len(opener.calls) == sent


def test_an_event_lands_in_this_tasks_stream_once(api_db, opener):
    control = _running(api_db, opener)

    control.emit(EventType.HEARTBEAT, {"rss": 1})

    (event,) = events(api_db, kind="heartbeat")
    assert event["detail"] == {"rss": 1} and event["lease_id"] == LEASE


def test_an_event_lost_to_an_outage_is_retried_and_written_once(api_db, opener):
    control = _running(api_db, opener)
    opener.fail_next = 2

    control.emit(EventType.HEARTBEAT, {"n": 1})

    assert len(events(api_db, kind="heartbeat")) == 1


def test_a_park_parks_releases_and_records_the_end(api_db, opener):
    control = _running(api_db, opener)
    control.record_attempt_start(backend="cloud_run", execution_name=None)
    until = utcnow() + timedelta(minutes=10)

    control.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED, next_eligible_at=until,
                 detail={"provider": "anthropic"},
                 announce=[(EventType.QUOTA_EXHAUSTED, {"provider": "anthropic"})])

    task = _task(api_db)
    assert task["state"] == "PARKED" and task["park_reason"] == "PROVIDER_QUOTA_EXHAUSTED"
    assert task["current_lease_id"] is None
    assert _lease(api_db)["released_at"] is not None
    assert all(api_db.docs[f"pools/{n}"]["active"] == 0 for n in POOLS)
    attempt = api_db.docs[f"attempts/{ATTEMPT}"]
    assert attempt["exit_code"] == 75 and attempt["error"] == "PROVIDER_QUOTA_EXHAUSTED"
    kinds = {e["type"] for e in events(api_db)}
    assert {"parked", "quota_exhausted", "lease_released"} <= kinds
    assert control.release_lease("parked:again") is False  # idempotent, nothing sent


def test_a_park_records_its_uploads_for_the_next_attempt(api_db, opener):
    control = _running(api_db, opener)
    artifacts = [{"name": "out.txt", "key": f"tenants/{TENANT}/tasks/{TASK}/out.txt"}]

    control.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
                 next_eligible_at=utcnow() + timedelta(minutes=1),
                 detail={"artifacts": artifacts})

    record = api_db.docs[f"tasks/{TASK}/carry/{ATTEMPT}"]
    assert record["artifacts"] == artifacts and record["tenant_id"] == TENANT


def test_the_next_attempt_reads_the_carry_state_open_served(api_db, opener):
    seed_attempt(api_db)
    api_db.docs[f"tasks/{TASK}/carry/parks"] = {
        "tenant_id": TENANT, "task_id": TASK, "attempts": [{"attempt_id": "att_old"}]}
    api_db.docs[f"tasks/{TASK}/carry/att_old"] = {
        "tenant_id": TENANT, "task_id": TASK, "attempt_id": "att_old", "at": utcnow(),
        "artifacts": [{"name": "a"}]}
    control = remote_control(opener)
    control.validate_generation()

    assert control.parked_uploads() == [("att_old", [{"name": "a"}])]


def test_an_await_park_refunds_the_attempt(api_db, opener):
    control = _running(api_db, opener)

    refunded = control.park_awaiting_children(max_resumes=4, detail={"children": 2})

    task = _task(api_db)
    assert refunded is True and task["state"] == "PARKED"
    assert task["park_reason"] == "CHILDREN_INCOMPLETE" and task["attempt_count"] == 0
    assert task["metadata"]["child_await_resumes"] == 1
    assert _lease(api_db)["released_at"] is not None


def test_a_ci_park_records_what_it_waits_on(api_db, opener):
    control = _running(api_db, opener)

    refunded = control.park_ci_pending(code="checks_pending", head="abc", pull_request=7,
                                       pending=["ci"], max_wakes=60, fallback_seconds=900)

    task = _task(api_db)
    assert refunded is True and task["park_reason"] == "CI_PENDING"
    wait = task["metadata"]["merge_wait"]
    assert (wait["code"], wait["head"], wait["pull_request"], wait["pending"]) == (
        "checks_pending", "abc", 7, ["ci"])
    assert wait["wakes"] == 1


def test_a_human_park_refunds_and_reports_no_return(api_db, opener):
    control = _running(api_db, opener)

    refunded, requeued = control.park_human_required(detail={"why": "login"})

    assert (refunded, requeued) == (True, False)
    assert _task(api_db)["park_reason"] == "HUMAN_REQUIRED"


def test_finish_ends_the_task_releases_and_rings_the_wake(api_db, opener):
    announcer = Recorder()
    seed_attempt(api_db)
    control = remote_control(opener, finish_announcer=announcer)
    control.validate_generation()
    control.advance_to_running()

    control.finish(state=TaskState.SUCCEEDED, exit_code=0,
                   result_summary={"rounds": [[1, 2]], "ok": True})

    task = _task(api_db)
    assert task["state"] == "SUCCEEDED" and task["completed_at"] is not None
    assert task["end_cause"] is None and task["current_lease_id"] is None
    assert task["result_summary"]["ok"] is True
    assert _lease(api_db)["released_at"] is not None
    assert all(api_db.docs[f"pools/{n}"]["active"] == 0 for n in POOLS)
    assert api_db.docs[f"attempts/{ATTEMPT}"]["exit_code"] == 0
    assert {"succeeded", "lease_released"} <= {e["type"] for e in events(api_db)}
    assert announcer.announced == [(TASK, TENANT, TaskState.SUCCEEDED)]
    sent = len(opener.calls)
    assert control.release_lease("terminal:SUCCEEDED") is False
    assert len(opener.calls) == sent


def test_finish_carries_the_end_cause(api_db, opener):
    control = _running(api_db, opener)

    control.finish(state=TaskState.FAILED, exit_code=1, error="x" * 9000,
                   end_cause=EndCause.RUNNER_ERROR)

    task = _task(api_db)
    assert task["state"] == "FAILED" and task["end_cause"] == "runner_error"
    assert len(task["last_error"]) == 8192


def test_a_finish_lost_to_an_outage_is_written_once(api_db, opener):
    control = _running(api_db, opener)
    opener.fail_next = 1

    control.finish(state=TaskState.SUCCEEDED, exit_code=0)

    assert len(events(api_db, kind="succeeded")) == 1


@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, TaskState.READY), ({"attempt_count": 3}, TaskState.FAILED),
     ({"cancel_requested": True}, TaskState.CANCELLED)],
)
def test_fail_retryably_picks_the_state_the_direct_path_picks(api_db, opener, extra, expected):
    control = _running(api_db, opener, **extra)

    target = control.fail_retryably(exit_code=1, error="boom", cause="runner_error",
                                    retry_delay_seconds=30)

    assert target is expected
    task = _task(api_db)
    assert task["state"] == expected.value and task["current_lease_id"] is None
    assert _lease(api_db)["released_at"] is not None
    if expected is TaskState.READY:
        assert task["next_eligible_at"] > utcnow()
    else:
        assert task["completed_at"] is not None


def test_a_cause_outside_the_routes_alphabet_is_written_not_refused(api_db, opener):
    control = _running(api_db, opener)

    target = control.fail_retryably(exit_code=1, error="boom", cause="runner said: no way")

    assert target is TaskState.READY
    (event,) = events(api_db, kind="retrying")
    assert event["detail"]["cause"] == "runner_said:_no_way"


def test_fetch_task_reads_the_live_fence_over_the_opened_spec(api_db, opener):
    control = _running(api_db, opener, input={"prompt": "2026-01-01T00:00:00+00:00"})
    _task(api_db)["cancel_requested"] = True

    task = control.fetch_task()

    assert task["cancel_requested"] is True and task["state"] == "RUNNING"
    # An ISO string in the input stays a string: the spec signature covers it.
    assert task["input"] == {"prompt": "2026-01-01T00:00:00+00:00"}
    assert task["created_at"].tzinfo is not None  # an instant is an instant again
    assert control.fetch_task_snapshot()[1] is None


def test_poll_reads_this_tenants_quota(api_db, opener):
    control = _running(api_db, opener)
    api_db.docs[f"quota/anthropic:{TENANT}"] = {"state": "EXHAUSTED", "retry_after_seconds": 60}

    signals = control.poll("anthropic")

    assert signals.provider_paused is True and signals.retry_after_seconds == 60
    assert not signals.is_fenced(1)


def test_parent_states_are_read_only_for_declared_parents(api_db, opener):
    seed_attempt(api_db, depends_on=["task_parent"])
    from control_plane.conftest import seed_task

    seed_task(api_db, task_id="task_parent", tenant_id=TENANT, state="SUCCEEDED")
    control = remote_control(opener)
    control.validate_generation()

    assert control.fetch_parent_states(["task_parent"]) == {"task_parent": TaskState.SUCCEEDED}
    with pytest.raises(TenantMismatchError):
        control.fetch_parent_states(["task_not_declared"])


def test_a_transaction_helper_is_refused_rather_than_reaching_firestore(api_db, opener):
    control = _running(api_db, opener)

    with pytest.raises(ControlPlaneError):
        control._run_transaction(lambda txn: control._fenced_task(txn, write="merge slot"))


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

#: `ControlPlane`'s public methods that never touch Firestore, which
#: `RemoteControlPlane` inherits unchanged.
_PURE = {"pin_verified_max_attempts", "startup_budget", "update_quota_state", "cancel_cause"}


def test_every_public_control_plane_method_is_spoken_over_the_routes():
    public = {name for name, member in vars(ControlPlane).items()
              if not name.startswith("_") and inspect.isfunction(member)}
    own = {name for name, member in vars(RemoteControlPlane).items()
           if not name.startswith("_") and inspect.isfunction(member)}
    # The control: the sweep saw the interface it is about.
    assert {"finish", "heartbeat", "validate_generation", "park"} <= public
    assert public - own - _PURE == set()
    for name in public - _PURE:
        base = inspect.signature(getattr(ControlPlane, name)).parameters
        mine = inspect.signature(getattr(RemoteControlPlane, name)).parameters
        assert list(base)[: len(base)] == list(mine)[: len(base)], name


def test_an_outage_past_the_budget_is_an_outage_the_lifecycle_reads(api_db, opener):
    from google.api_core import exceptions as core_exceptions

    control = _running(api_db, opener)
    opener.fail_next = 10_000

    with control.startup_budget():
        with pytest.raises(core_exceptions.ServiceUnavailable):
            control.emit(EventType.HEARTBEAT, {})
