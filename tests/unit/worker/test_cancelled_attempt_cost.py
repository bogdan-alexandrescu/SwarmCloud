"""A cancelled attempt records what it cost and when it ended (#627).

The 2026-10-05 history analysis: cancelled attempts recorded no cost at all
(outcomes "cancelled: reporting 0 of 87", about $23 unpriced). Two causes:

  * a CLI agent stopped on SIGTERM prints no `result` event, and that event is
    the only place the CLI reports `total_cost_usd` -- so every stopped run read
    "not reported", however long it had worked;
  * a SIGTERM to the WORKER (the backend cancelling its execution, which the
    API's cancel route now asks for directly) took the platform-reclaim path and
    PARKED a task somebody had cancelled.

Pinned here:

  * a stopped CLI run's cost is estimated from the per-message usage its stream
    carried, at list prices, and recorded as `cost_usd` with the summary saying
    it is an estimate;
  * an unknown model prices nothing: tokens are recorded, cost stays unreported
    rather than wrong;
  * a SIGTERM on a task whose cancel was requested ends it CANCELLED, with the
    runner's spend and the attempt's end on its document.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from agent_worker.lifecycle import _stream_spend_estimate
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt
from test_spend_on_every_exit import (
    COST,
    a_long_mock_run,
    once_the_runner_is_working,
)

#: A stand-in `claude` that works for two messages and is then stopped. The
#: first message is printed twice, as stream-json does per content block, with
#: the output count growing: the last copy is the message's usage.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, pathlib, sys, time
MARKER = pathlib.Path(MARKER_PATH)
MODEL = MODEL_NAME
def say(event):
    print(json.dumps(event), flush=True)
session = "sess-" + "0" * 12
say({"type": "system", "subtype": "init", "session_id": session, "model": MODEL})
def usage(i, cc, cr, o):
    return {"input_tokens": i, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr, "output_tokens": o}
say({"type": "assistant", "session_id": session, "message": {
    "id": "msg_a", "model": MODEL, "usage": usage(1000, 2000, 10000, 100),
    "content": [{"type": "text", "text": "thinking about it"}]}})
say({"type": "assistant", "session_id": session, "message": {
    "id": "msg_a", "model": MODEL, "usage": usage(1000, 2000, 10000, 500),
    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}})
say({"type": "user", "session_id": session, "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
say({"type": "assistant", "session_id": session, "message": {
    "id": "msg_b", "model": MODEL, "usage": usage(200, 0, 12000, 300),
    "content": [{"type": "text", "text": "still going"}]}})
MARKER.write_text("1")
time.sleep(60)
sys.exit(0)
'''

#: claude-sonnet-4-6 at $3 in / $15 out / $3.75 cache write / $0.30 cache read
#: per million: 1200 in, 800 out, 2000 written, 22000 read.
EXPECTED_COST = (1200 * 3 + 800 * 15 + 2000 * 3.75 + 22000 * 0.30) / 1_000_000


@pytest.fixture
def stopped_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def install(model: str) -> Path:
        marker = tmp_path / "worked"
        binary = tmp_path / "fake-claude"
        binary.write_text(
            FAKE_CLAUDE.replace("MARKER_PATH", repr(str(marker)), 1)
            .replace("MODEL_NAME", repr(model), 1)
        )
        binary.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
        monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
        return marker

    return install


def _cancel_once(db: Any, marker: Path) -> threading.Thread:
    def watch() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        db.doc("tasks/task_1")["cancel_requested"] = True

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def _seed_claude(db: Any) -> None:
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "work"})
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]


def test_a_cancelled_cli_run_records_its_estimated_cost(db, worker_factory, stopped_claude):
    marker = stopped_claude("claude-sonnet-4-6")
    _seed_claude(db)
    worker, _, _ = worker_factory(
        runner_profile="claude-code", control_poll_seconds=1, timeout_seconds=60
    )

    thread = _cancel_once(db, marker)
    code = worker.run()
    thread.join()

    assert code == ExitCode.CANCELLED
    attempt = db.doc("attempts/att_1")
    assert attempt.get("cost_usd") == pytest.approx(EXPECTED_COST)
    assert attempt.get("input_tokens") == 1200
    assert attempt.get("output_tokens") == 800
    assert attempt.get("cache_creation_input_tokens") == 2000
    assert attempt.get("cache_read_input_tokens") == 22000
    assert attempt.get("completed_at") is not None
    assert db.doc("tasks/task_1")["result_summary"].get("cost_estimated") is True


def test_an_unknown_model_records_tokens_and_no_cost(db, worker_factory, stopped_claude):
    marker = stopped_claude("some-model-nobody-priced")
    _seed_claude(db)
    worker, _, _ = worker_factory(
        runner_profile="claude-code", control_poll_seconds=1, timeout_seconds=60
    )

    thread = _cancel_once(db, marker)
    assert worker.run() == ExitCode.CANCELLED
    thread.join()

    attempt = db.doc("attempts/att_1")
    assert attempt.get("cost_usd") is None, "a guessed price is a wrong figure"
    assert attempt.get("output_tokens") == 800


def test_the_estimate_reads_the_last_copy_of_each_message(tmp_path):
    lines = [
        {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5-20251001",
                                          "usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5-20251001",
                                          "usage": {"input_tokens": 10, "output_tokens": 40}}},
        {"type": "result", "subtype": "nothing priced here"},
    ]
    path = tmp_path / "claude-code.stdout.log"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    estimate = _stream_spend_estimate(path)

    assert estimate["input_tokens"] == 10
    assert estimate["output_tokens"] == 40
    assert estimate["total_cost_usd"] == pytest.approx((10 * 1 + 40 * 5) / 1_000_000)


def test_a_link_in_place_of_the_capture_is_not_read(tmp_path):
    target = tmp_path / "elsewhere.log"
    target.write_text(json.dumps({"type": "assistant", "message": {
        "id": "m1", "model": "claude-haiku-4-5", "usage": {"input_tokens": 5}}}) + "\n")
    link = tmp_path / "claude-code.stdout.log"
    link.symlink_to(target)

    assert _stream_spend_estimate(link) == {}


def test_a_sigterm_on_a_cancelled_task_ends_it_cancelled_with_its_spend(db, worker_factory):
    """The backend cancelled the execution because the task's cancel was
    requested: the SIGTERM is that cancel arriving, not a reclaim to retry."""
    a_long_mock_run(db)
    # No control poll during the run: the SIGTERM path, not the poll, sees it.
    worker, _, _ = worker_factory(control_poll_seconds=600, timeout_seconds=30)

    def cancel_then_terminate() -> None:
        db.doc("tasks/task_1")["cancel_requested"] = True
        worker._interrupted = True

    thread = once_the_runner_is_working(worker, cancel_then_terminate)
    code = worker.run()
    thread.join()

    assert code == ExitCode.CANCELLED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value
    assert EventType.PARKED.value not in db.event_types("task_1")
    attempt = db.doc("attempts/att_1")
    assert attempt.get("cost_usd") == pytest.approx(COST)
    assert attempt.get("completed_at") is not None
    assert db.doc("leases/lease_1")["released_at"] is not None
