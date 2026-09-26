"""What an attempt SPENT is recorded on every exit that measured it.

WHAT WAS WRONG. `control.record_spend` was called from exactly one place:
`lifecycle._finalise`, the path an attempt takes when its runner exits on its
own. Every other way out -- a park on a provider rate limit, a cancellation, a
SIGTERM, a worker crash -- wrote nothing, and even on `_finalise` a FAILED run
recorded nothing because the CLI runner raised before it ever parsed the JSON
that carried the numbers.

So the attempts that burned tokens and then stopped -- a 429 halfway through a
long agent run is the ordinary case -- all read as "not reported" in every cost
figure the platform produces, while the successful ones were counted. A spend
report built on that is wrong in the direction that looks cheap.

These tests run the production `Worker` against a real child process on the
`claude-code` profile (a stand-in `claude` binary, installed the way the Job
installs the real one) so that the number travels the whole way: the CLI's
JSON -> the runner's result.json -> the worker -> the attempt document.

The numbers are chosen to contain no "429" and no credential marker, so that a
FAILED run is not re-read as a rate limit or a revoked key by the runner's own
text heuristics.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from agent_worker.runners.mock import PROGRESS_DIR
from swarm_common.models import ProviderState
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt

PROFILE = "claude-code"

USAGE = {
    "input_tokens": 1234,
    "output_tokens": 567,
    "cache_read_input_tokens": 89,
    "cache_creation_input_tokens": 10,
}
COST = 0.4213

#: A stand-in for `claude --print --output-format json`. It reads its plan from
#: the prompt (the last argv element), which is the only channel a real caller
#: has into an agent, and prints ONE JSON object -- including when it fails,
#: which is what the real CLI does and is the whole point here.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time

# Only the leading JSON value: every CLI prompt now ends with the platform's
# line naming $SWARM_ARTIFACTS_DIR (#184).
try:
    plan, _end = json.JSONDecoder().raw_decode(sys.argv[-1])
except (ValueError, IndexError):
    plan = {}

work = pathlib.Path(os.environ.get("SWARM_WORK_DIR") or os.getcwd())
key =os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""

binary = plan.get("binary_artifact")
if binary:
    target = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"]) / binary["name"]
    body = b"\x89PNG\r\n\x1a\n\xff\xfe\x00"
    if binary.get("with_key"):
        body += key.encode("utf-8")
    body += b"\x00\xff\xfe" * 64
    target.write_bytes(body)

if plan.get("hang_seconds"):
    time.sleep(float(plan["hang_seconds"]))

marker = work / ".rate-limited"
seen = int(marker.read_text() or 0) if marker.exists() else 0
if seen < int(plan.get("rate_limit_times", 0)):
    marker.write_text(str(seen + 1))
    out = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "API Error: 429 rate_limit_error. retry-after: %d" % plan.get("retry_after", 3600),
    }
    usage = plan.get("rate_limit_usage", plan.get("usage"))
    cost = plan.get("rate_limit_cost", plan.get("cost_usd"))
    if usage is not None:
        out["usage"] = usage
    if cost is not None:
        out["total_cost_usd"] = cost
    print(json.dumps(out))
    sys.exit(1)

# Past its rate limits, so this run is the RETRY. It says so on a path the test
# chose, then works for a while -- long enough for the test to change the world
# around it -- and stops early if its runner is killed, rather than lingering
# as an orphan: the CLI runner starts this in its own session, so a SIGKILL of
# the runner never reaches it.
if plan.get("retry_hang_seconds"):
    if plan.get("retry_started_marker"):
        pathlib.Path(plan["retry_started_marker"]).write_text("running")
    parent = os.getppid()
    end = time.monotonic() + float(plan["retry_hang_seconds"])
    while time.monotonic() < end and os.getppid() == parent:
        time.sleep(0.1)

out = {"type": "result", "subtype": "success", "is_error": False,
       "result": plan.get("say", "done")}
if plan.get("fail"):
    out.update(subtype="error_during_execution", is_error=True, result=plan["fail"])
if plan.get("usage") is not None:
    out["usage"] = plan["usage"]
if plan.get("cost_usd") is not None:
    out["total_cost_usd"] = plan["cost_usd"]
print(json.dumps(out))
sys.exit(1 if plan.get("fail") else 0)
"""


@pytest.fixture
def agent_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def seed(db: Any, *, attempt_id: str = "att_1", lease_id: str = "lease_1",
         generation: int = 1, latest_checkpoint: str | None = None,
         **plan: Any) -> None:
    seed_attempt(
        db,
        attempt_id=attempt_id,
        lease_id=lease_id,
        generation=generation,
        runner_profile=PROFILE,
        latest_checkpoint=latest_checkpoint,
        task_input={"prompt": json.dumps(plan)},
    )
    # Without the provider named, `resolve_credentials` parks the task before
    # the agent starts and every assertion below would be vacuous.
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]


def spend_on(db: Any, attempt_id: str = "att_1") -> dict[str, Any]:
    document = db.doc(f"attempts/{attempt_id}")
    return {
        key: document.get(key)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cost_usd",
        )
    }


def assert_recorded(db: Any, attempt_id: str = "att_1") -> None:
    spend = spend_on(db, attempt_id)
    assert spend["input_tokens"] == USAGE["input_tokens"], spend
    assert spend["output_tokens"] == USAGE["output_tokens"], spend
    assert spend["cache_read_input_tokens"] == USAGE["cache_read_input_tokens"], spend
    assert spend["cache_creation_input_tokens"] == USAGE["cache_creation_input_tokens"], spend
    assert spend["cost_usd"] == pytest.approx(COST), spend


# ---------------------------------------------------------------------------
# the exits that used to record nothing
# ---------------------------------------------------------------------------


def test_a_rate_limited_run_that_parks_records_what_it_spent(db, worker_factory, agent_cli):
    """The ordinary expensive case: the agent worked, then hit a 429.

    The park is correct (invariant 4). Losing the tokens it burned first is not.
    """
    seed(db, rate_limit_times=1, retry_after=3600, usage=USAGE, cost_usd=COST)
    worker, _, _ = worker_factory(runner_profile=PROFILE)

    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    assert_recorded(db)


def test_a_failed_run_records_what_it_spent(db, worker_factory, agent_cli):
    """A CLI that fails still prints its usage; the runner used to raise first."""
    seed(db, fail="the agent loop gave up on the task", usage=USAGE, cost_usd=COST)
    worker, _, _ = worker_factory(runner_profile=PROFILE)

    assert worker.run() == ExitCode.FAILED
    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value
    assert_recorded(db)


def test_a_worker_crash_after_the_agent_finished_still_records_spend(
    db, worker_factory, agent_cli
):
    """The crash path is `_safe_finish`, and it never reached `record_spend`.

    Driven by making the metrics export raise, which is one of the calls that
    sat between the agent finishing and the old, single `record_spend` call.
    """
    seed(db, usage=USAGE, cost_usd=COST)
    worker, _, exporter = worker_factory(runner_profile=PROFILE)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the metrics client blew up")

    exporter.export = explode  # type: ignore[method-assign]

    assert worker.run() == ExitCode.FAILED
    assert_recorded(db)


def test_a_cancelled_attempt_records_what_its_runner_reported(db, worker_factory):
    """Cancellation mid-run: the runner stops cleanly and reports; keep it.

    The mock runner is used because it honours SIGTERM and writes its result on
    the way out -- which is the only way a cancelled run has a measurement.
    """
    seed_attempt(
        db,
        task_input={
            "prompt": "cancel me",
            "steps": 40,
            "sleep_seconds": 8.0,
            "spend": {"usage": USAGE, "total_cost_usd": COST},
        },
    )
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def cancel() -> None:
        time.sleep(1.5)
        db.doc("tasks/task_1")["cancel_requested"] = True

    thread = threading.Thread(target=cancel)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.CANCELLED
    assert_recorded(db)


# ---------------------------------------------------------------------------
# the backstop in `_cleanup`, and the two parks that stop a live runner
#
# Two exits cannot record spend in `_upload_outputs`: a generation fenced
# mid-run uploads nothing, and a crash while the runner is alive has not reaped
# the runner, so there is nothing to record yet. `_cleanup` collects and
# records for both, and these are the tests that fail without it. The SIGTERM
# and backpressure parks do pass through `_upload_outputs`, and had no test.
# ---------------------------------------------------------------------------

#: What the mock runner reports on every exit, including when it is stopped.
MOCK_SPEND = {"usage": USAGE, "total_cost_usd": COST}


def a_long_mock_run(db: Any) -> None:
    """A mock runner that works for about eight seconds, reporting MOCK_SPEND."""
    seed_attempt(
        db,
        task_input={"prompt": "long", "steps": 40, "sleep_seconds": 8.0, "spend": MOCK_SPEND},
    )


def runner_is_working(worker: Any) -> bool:
    """The runner is alive and has finished a step.

    A finished step means its SIGTERM handler is installed, so a runner that
    is stopped now writes its result on the way out. A trigger fired before
    that would kill a runner that never had the chance, and the test would be
    measuring startup timing instead of the worker.
    """
    ws = worker.ws
    child = worker._child
    return (
        ws is not None
        and child is not None
        and child.poll() is None
        and (ws.work / PROGRESS_DIR / "step-0001.txt").exists()
    )


def once_the_runner_is_working(worker: Any, action: Any) -> threading.Thread:
    def watch() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if runner_is_working(worker):
                action()
                return
            time.sleep(0.05)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def test_a_generation_fenced_mid_run_records_what_its_runner_reported(db, worker_factory):
    """A fenced worker uploads nothing and writes no state; only `_cleanup`
    can record what its runner spent. It writes the attempt's OWN document --
    the lease, which invariant 5 puts out of this worker's reach, is untouched.
    """
    a_long_mock_run(db)
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def fence() -> None:
        db.doc("tasks/task_1")["current_generation"] = 99

    thread = once_the_runner_is_working(worker, fence)
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None
    assert_recorded(db)


def test_a_worker_crash_while_the_runner_is_alive_records_what_it_reported(
    db, worker_factory
):
    """The crash handler writes FAILED while the runner is still running, so
    nothing has been collected when `_upload_outputs` records. `_cleanup` then
    stops the runner -- which reports on the way out -- and records that.

    Driven by the heartbeat write raising mid-run, the way a Firestore outage
    reaches the supervision loop.
    """
    a_long_mock_run(db)
    worker, _, _ = worker_factory(timeout_seconds=30)
    real_heartbeat = worker.control.heartbeat

    def heartbeat(*args: Any, **kwargs: Any) -> Any:
        if runner_is_working(worker):
            raise RuntimeError("the heartbeat write failed")
        return real_heartbeat(*args, **kwargs)

    worker.control.heartbeat = heartbeat  # type: ignore[method-assign]

    assert worker.run() == ExitCode.FAILED
    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value
    assert_recorded(db)


def test_a_worker_sigterm_park_records_what_its_runner_reported(db, worker_factory):
    """The platform takes the sandbox away mid-run; the worker parks.

    `_interrupted` is set directly, which is all the SIGTERM handler does --
    sending the test process a real SIGTERM would kill it wherever the handler
    is not installed.
    """
    a_long_mock_run(db)
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def interrupt() -> None:
        worker._interrupted = True

    thread = once_the_runner_is_working(worker, interrupt)
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.PARKED
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"].get("cause") == "worker_interrupted", parked
    assert_recorded(db)


def test_a_backpressure_park_records_what_the_attempt_had_spent(
    db, worker_factory, agent_cli, tmp_path
):
    """A short 429 is retried in place; while the retry runs, the quota broker
    marks the provider exhausted for forty minutes, and the worker parks.

    The retry is a CLI runner stopped on SIGTERM, which reports nothing -- so
    what the attempt spent is the first run's, collected when that run ended.
    The park must record it.
    """
    started = tmp_path / "retry-started"
    seed(
        db,
        rate_limit_times=1,
        retry_after=1,
        usage=USAGE,
        cost_usd=COST,
        retry_hang_seconds=20,
        retry_started_marker=str(started),
    )
    worker, _, _ = worker_factory(runner_profile=PROFILE, timeout_seconds=40)

    def exhaust_the_provider() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.05)
        db.seed(
            f"quota/anthropic:{TENANT}",
            {
                "provider": "anthropic",
                "tenant_id": TENANT,
                "state": ProviderState.EXHAUSTED.value,
                "retry_after_seconds": 2400,
                "updated_at": None,
            },
        )

    thread = threading.Thread(target=exhaust_the_provider, daemon=True)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.PARKED
    assert started.exists(), "the retry never started, so this was not a backpressure park"
    exhausted = [e for e in db.events("task_1") if e["type"] == EventType.QUOTA_EXHAUSTED.value]
    assert exhausted and exhausted[-1]["detail"]["park_phase"] == "backpressure", exhausted
    assert_recorded(db)


# ---------------------------------------------------------------------------
# counting each run exactly once
# ---------------------------------------------------------------------------


def test_a_short_rate_limit_retried_in_place_adds_both_runs(db, worker_factory, agent_cli):
    """One attempt, two runner runs: the attempt spent the SUM.

    The first run's result.json is overwritten by the second, so recording only
    at the end counted the retry and silently dropped the run that hit the 429.
    """
    seed(
        db,
        rate_limit_times=1,
        retry_after=1,
        rate_limit_usage={"input_tokens": 100, "output_tokens": 20},
        rate_limit_cost=0.01,
        usage={"input_tokens": 1000, "output_tokens": 300},
        cost_usd=0.25,
    )
    worker, _, _ = worker_factory(runner_profile=PROFILE)

    assert worker.run() == ExitCode.OK
    spend = spend_on(db)
    assert spend["input_tokens"] == 1100, spend
    assert spend["output_tokens"] == 320, spend
    assert spend["cost_usd"] == pytest.approx(0.26), spend


def park_on_a_rate_limit_then_resume(db: Any, worker_factory: Any, **second_plan: Any):
    """Attempt 1 hits a 429 and parks; attempt 2 is built on its checkpoint.

    The provider's quota document is cleared in between, standing for the
    window having reopened -- otherwise attempt 2 parks on the pre-flight check
    before its runner starts, and nothing below would be exercised.
    """
    seed(db, rate_limit_times=1, retry_after=3600, usage=USAGE, cost_usd=COST)
    first, _, _ = worker_factory(runner_profile=PROFILE)
    assert first.run() == ExitCode.PARKED

    checkpoint = db.doc("tasks/task_1")["latest_checkpoint"]
    assert checkpoint, "the park must have left a checkpoint to resume from"
    db.documents.pop(f"quota/anthropic:{TENANT}", None)

    seed(
        db,
        attempt_id="att_2",
        lease_id="lease_2",
        generation=2,
        latest_checkpoint=checkpoint,
        **second_plan,
    )


def test_a_resumed_attempt_is_not_parked_again_by_the_previous_attempts_429(
    db, worker_factory, agent_cli
):
    """quota.json lives in `work/`, and `work/` is what a checkpoint restores.

    The runner writes quota.json when it is rate-limited, the park that follows
    checkpoints it, and the next attempt restores it. `_quota_from_child` then
    read that file after the NEW runner exited -- whatever it exited with -- and
    parked the task again on a 429 from a previous attempt. Every resumed
    attempt did its work and re-parked, so a task that was rate-limited once
    could never finish.
    """
    park_on_a_rate_limit_then_resume(db, worker_factory, usage=USAGE, cost_usd=COST)
    second, _, _ = worker_factory(
        runner_profile=PROFILE, attempt_id="att_2", lease_id="lease_2", generation=2
    )

    assert second.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def test_a_resumed_attempt_does_not_inherit_the_previous_attempts_spend(
    db, worker_factory, agent_cli
):
    """result.json lives in `work/` too, so it is restored the same way.

    A runner killed before it writes its own result leaves the PREVIOUS
    attempt's in place, and a worker that read it would book attempt 1's tokens
    a second time against attempt 2. The runner here is killed on the attempt's
    timeout, which is the ordinary way a runner dies without writing anything.
    """
    park_on_a_rate_limit_then_resume(db, worker_factory, hang_seconds=6)
    second, _, _ = worker_factory(
        runner_profile=PROFILE,
        attempt_id="att_2",
        lease_id="lease_2",
        generation=2,
        timeout_seconds=3,
        termination_grace_seconds=1,
    )
    assert second.run() == ExitCode.TIMEOUT

    assert spend_on(db, "att_2") == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": None,
    }, "attempt 2 was charged for attempt 1's run"
    # And attempt 1 kept its own, which is what makes the line above a test of
    # double counting rather than of nothing being recorded anywhere.
    assert_recorded(db, "att_1")


# ---------------------------------------------------------------------------
# redaction that did not happen is said out loud (docs/audits 02 §4)
# ---------------------------------------------------------------------------


def test_an_unscrubbable_artifact_that_holds_the_key_is_reported(
    db, worker_factory, agent_cli
):
    """`scrub_file` leaves a binary file alone -- deliberately -- and returned a
    value nobody read, so the file was uploaded with no signal at all.

    Leaving it unscrubbed is the recorded trade. Saying nothing is not: the
    result summary now names the file, why it was not rewritten, and whether
    its raw bytes hold a registered secret.
    """
    seed(db, binary_artifact={"name": "screenshot.png", "with_key": True})
    worker, _, _ = worker_factory(runner_profile=PROFILE)

    assert worker.run() == ExitCode.OK
    summary = db.doc("tasks/task_1")["result_summary"]
    skipped = summary.get("redaction_skipped") or []
    entry = next((e for e in skipped if e.get("file", "").endswith("screenshot.png")), None)
    assert entry is not None, summary
    assert entry["reason"] == "not_text"
    assert entry["secret_found"] is True


def test_an_unscrubbable_artifact_without_the_key_is_not_reported(
    db, worker_factory, agent_cli
):
    """The overshoot guard. Every PNG an agent writes is "not text"; one whose
    bytes were scanned and hold no registered value is as clean as a rewritten
    text file, and reporting it would bury the entry that matters."""
    seed(db, binary_artifact={"name": "diagram.png", "with_key": False})
    worker, _, _ = worker_factory(runner_profile=PROFILE)

    assert worker.run() == ExitCode.OK
    summary = db.doc("tasks/task_1")["result_summary"]
    skipped = summary.get("redaction_skipped") or []
    assert not any(e.get("file", "").endswith("diagram.png") for e in skipped), skipped
