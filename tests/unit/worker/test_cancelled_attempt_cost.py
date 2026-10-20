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
    carried and recorded as `cost_usd` with the summary saying it is an
    estimate. SWARM-API PRICES IT (#1481, owner decision 2026-10-17): the
    worker sends the usage per model to `spend-estimate` and records the
    figure it gets back; it holds no price table of its own;
  * a model swarm-api has no price for prices nothing: tokens are recorded,
    cost stays unreported rather than wrong -- and so does a run whose
    estimate could not be asked for;
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
from agent_worker import lifecycle
from agent_worker.lifecycle import _stream_usage
from swarm_common.states import EventType, TaskState

from worker_seeds import TENANT, seed_attempt
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

#: What the stand-in run used: 1200 in, 800 out, 2000 written, 22000 read.
USAGE = {"input_tokens": 1200, "output_tokens": 800,
         "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 22000}
#: What the worker sends for it: the same counts, and split by each request's
#: own prompt (PRICES-TIER-PROMPT). msg_a's prompt is 13,000 tokens and
#: msg_b's 12,200: both in bucket 13 (12,001 to 13,000).
SENT = {**USAGE, "prompt_buckets": [[13, 1200, 800, 2000, 22000]]}
#: What swarm-api answers for it. Any figure: the worker records the answer,
#: it does not compute one (tests/unit/control_plane/test_model_prices.py
#: pins swarm-api's arithmetic).
EXPECTED_COST = 0.0371


class Pricer:
    """swarm-api's `spend-estimate`, as the worker's `SpendPricer` asks it."""

    def __init__(self, answer: float | None, *, tier_estimated: bool = False) -> None:
        self.answer = answer
        self.tier_estimated = tier_estimated
        self.asked: list[dict[str, Any]] = []

    def estimate(self, usage_by_model: dict[str, Any]) -> tuple[float | None, bool]:
        self.asked.append(usage_by_model)
        return self.answer, self.tier_estimated and self.answer is not None


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
    worker.spend_pricer = pricer = Pricer(EXPECTED_COST)

    thread = _cancel_once(db, marker)
    code = worker.run()
    thread.join()

    assert code == ExitCode.CANCELLED
    assert pricer.asked == [{"claude-sonnet-4-6": SENT}]
    attempt = db.doc("attempts/att_1")
    assert attempt.get("cost_usd") == pytest.approx(EXPECTED_COST)
    assert attempt.get("input_tokens") == 1200
    assert attempt.get("output_tokens") == 800
    assert attempt.get("cache_creation_input_tokens") == 2000
    assert attempt.get("cache_read_input_tokens") == 22000
    assert attempt.get("completed_at") is not None
    assert db.doc("tasks/task_1")["result_summary"].get("cost_estimated") is True
    assert "tier_estimated" not in db.doc("tasks/task_1")["result_summary"]


def test_a_tier_picked_from_summed_counts_is_marked_on_the_summary(db, worker_factory,
                                                                   stopped_claude):
    """swarm-api's `tier_estimated` -- a tiered model priced by the summed rule
    -- is carried onto the task's summary beside `cost_estimated`."""
    marker = stopped_claude("claude-haiku-5-5")
    _seed_claude(db)
    worker, _, _ = worker_factory(
        runner_profile="claude-code", control_poll_seconds=1, timeout_seconds=60
    )
    worker.spend_pricer = Pricer(EXPECTED_COST, tier_estimated=True)

    thread = _cancel_once(db, marker)
    assert worker.run() == ExitCode.CANCELLED
    thread.join()

    summary = db.doc("tasks/task_1")["result_summary"]
    assert summary.get("cost_estimated") is True
    assert summary.get("tier_estimated") is True


def test_an_unpriced_model_records_tokens_and_no_cost(db, worker_factory, stopped_claude):
    marker = stopped_claude("some-model-nobody-priced")
    _seed_claude(db)
    worker, _, _ = worker_factory(
        runner_profile="claude-code", control_poll_seconds=1, timeout_seconds=60
    )
    worker.spend_pricer = pricer = Pricer(None)

    thread = _cancel_once(db, marker)
    assert worker.run() == ExitCode.CANCELLED
    thread.join()

    assert pricer.asked == [{"some-model-nobody-priced": SENT}]
    attempt = db.doc("attempts/att_1")
    assert attempt.get("cost_usd") is None, "a guessed price is a wrong figure"
    assert attempt.get("output_tokens") == 800
    assert not db.doc("tasks/task_1")["result_summary"].get("cost_estimated")


def test_with_no_way_to_ask_swarm_api_the_cost_is_not_reported(db, worker_factory,
                                                                stopped_claude):
    """No SWARM_API_URL, no attempt key: nothing is priced here instead."""
    marker = stopped_claude("claude-opus-5-5")
    _seed_claude(db)
    worker, _, _ = worker_factory(
        runner_profile="claude-code", control_poll_seconds=1, timeout_seconds=60
    )

    thread = _cancel_once(db, marker)
    assert worker.run() == ExitCode.CANCELLED
    thread.join()

    attempt = db.doc("attempts/att_1")
    assert attempt.get("cost_usd") is None
    assert attempt.get("input_tokens") == 1200


def test_the_worker_holds_no_price_table():
    """#1481: prices are data in swarm-api, not code in the worker."""
    assert not hasattr(lifecycle, "_LIST_PRICES")
    assert not hasattr(lifecycle, "_list_price")


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

    usage = _stream_usage(path)

    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 40
    assert "total_cost_usd" not in usage, "the worker prices nothing"
    assert usage["usage_by_model"] == {"claude-haiku-4-5-20251001": {
        "input_tokens": 10, "output_tokens": 40,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "prompt_buckets": [[1, 10, 40, 0, 0]]}}


def test_the_estimate_splits_each_models_counts_by_each_requests_own_prompt(tmp_path):
    """PRICES-TIER-PROMPT: a tiered model is priced per request by its own
    prompt, so each request is counted in the 1,000-token bucket its prompt
    (input + cache written + cache read) falls in, never by the summed one."""
    def say(msg_id: str, model: str, i: int, cw: int, cr: int, o: int) -> dict[str, Any]:
        return {"type": "assistant", "message": {"id": msg_id, "model": model, "usage": {
            "input_tokens": i, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "output_tokens": o}}}

    lines = [
        say("m1", "claude-haiku-5-5", 60_000, 0, 40_000, 7),   # exactly 100,000: bucket 100
        say("m2", "claude-haiku-5-5", 60_001, 0, 40_000, 9),   # 100,001: bucket 101
        say("m3", "claude-haiku-5-5", 500, 1_000, 98_500, 3),  # 100,000 again: bucket 100
        say("m4", "claude-opus-5-5", 0, 0, 0, 4),              # an empty prompt: bucket 0
    ]
    path = tmp_path / "claude-code.stdout.log"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    by_model = _stream_usage(path)["usage_by_model"]

    assert by_model["claude-haiku-5-5"]["prompt_buckets"] == [
        [100, 60_500, 10, 1_000, 138_500],
        [101, 60_001, 9, 0, 40_000],
    ]
    assert by_model["claude-haiku-5-5"]["input_tokens"] == 120_501
    assert by_model["claude-opus-5-5"]["prompt_buckets"] == [[0, 0, 4, 0, 0]]


def test_a_model_past_the_bucket_limit_is_sent_as_its_sums(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_MAX_PROMPT_BUCKETS", 1)
    lines = [{"type": "assistant", "message": {"id": f"m{n}", "model": "claude-haiku-5-5",
                                               "usage": {"input_tokens": n * 5_000}}}
             for n in (1, 2)]
    path = tmp_path / "claude-code.stdout.log"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    sent = _stream_usage(path)["usage_by_model"]["claude-haiku-5-5"]

    assert "prompt_buckets" not in sent
    assert sent["input_tokens"] == 15_000


def test_a_link_in_place_of_the_capture_is_not_read(tmp_path):
    target = tmp_path / "elsewhere.log"
    target.write_text(json.dumps({"type": "assistant", "message": {
        "id": "m1", "model": "claude-haiku-4-5", "usage": {"input_tokens": 5}}}) + "\n")
    link = tmp_path / "claude-code.stdout.log"
    link.symlink_to(target)

    assert _stream_usage(link) == {}


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


class _Children:
    """`ChildPath` as `SpendPricer` reads it: an API client and an attempt key."""

    def __init__(self, answer: Any, *, can_sign: bool = True, api: bool = True) -> None:
        self.answer = answer
        self.can_sign = can_sign
        self.api = self if api else None
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []

    def sign_request(self, method: str, path: str, body: bytes) -> dict[str, str]:
        return {"X-Swarm-Attempt-Proof": "signed:" + str(len(body))}

    def retrying(self, fn):
        return fn()

    def call(self, method, path, *, body=None, headers=None):
        self.calls.append((method, path, json.loads(body), dict(headers or {})))
        if isinstance(self.answer, Exception):
            raise self.answer
        return 200, self.answer


class _Log:
    def warning(self, *_a, **_k) -> None:
        pass

    info = warning


class _Cfg:
    tenant_id, task_id, attempt_id, lease_id, generation = "eng", "task_1", "att_1", "lease_1", 3


def test_the_pricer_sends_usage_per_model_and_records_swarm_apis_figure():
    from agent_worker.spendprice import SpendPricer

    children = _Children({"cost_usd": 0.5, "currency": "USD", "unpriced_models": []})
    pricer = SpendPricer(_Cfg(), children=children, logger=_Log())

    assert pricer.price({"claude-opus-5-5": SENT}) == 0.5
    (method, path, body, headers), = children.calls
    assert (method, path) == ("POST", "/v1/attempts/att_1/spend-estimate")
    assert body == {"tenant_id": "eng", "task_id": "task_1", "attempt_id": "att_1",
                    "lease_id": "lease_1", "generation": 3,
                    "usage_by_model": {"claude-opus-5-5": SENT}}
    assert "X-Swarm-Attempt-Proof" in headers


@pytest.mark.parametrize("answer, expected", [
    ({"cost_usd": 0.5, "tier_estimated": True}, (0.5, True)),
    ({"cost_usd": 0.5, "tier_estimated": False}, (0.5, False)),
    # swarm-api from before PRICES-TIER-PROMPT says nothing of tiers.
    ({"cost_usd": 0.5}, (0.5, False)),
    ({"cost_usd": None, "tier_estimated": True}, (None, False)),
], ids=["estimated", "exact", "older-api", "unpriced"])
def test_the_pricer_returns_swarm_apis_tier_estimated_word(answer, expected):
    from agent_worker.spendprice import SpendPricer

    pricer = SpendPricer(_Cfg(), children=_Children(answer), logger=_Log())

    assert pricer.estimate({"claude-haiku-5-5": SENT}) == expected


def test_a_body_past_swarm_apis_limit_is_sent_without_its_buckets(monkeypatch):
    from agent_worker import spendprice

    monkeypatch.setattr(spendprice, "MAX_BODY_BYTES", 200)
    children = _Children({"cost_usd": 0.5, "tier_estimated": True})
    pricer = spendprice.SpendPricer(_Cfg(), children=children, logger=_Log())

    assert pricer.estimate({"claude-haiku-5-5": SENT}) == (0.5, True)
    (_, _, body, _), = children.calls
    assert body["usage_by_model"] == {"claude-haiku-5-5": USAGE}


@pytest.mark.parametrize("children", [
    _Children({"cost_usd": None, "unpriced_models": ["x"]}),
    _Children({"cost_usd": "0.5"}),
    _Children({"cost_usd": 0.5}, can_sign=False),
    _Children({"cost_usd": 0.5}, api=False),
], ids=["unpriced", "not-a-number", "no-key", "no-api"])
def test_the_pricer_reports_nothing_when_swarm_api_gave_no_figure(children):
    from agent_worker.spendprice import SpendPricer

    assert SpendPricer(_Cfg(), children=children, logger=_Log()).price(
        {"claude-opus-5-5": USAGE}) is None


def test_a_refusal_prices_nothing():
    from agent_worker.children import ChildApiError
    from agent_worker.spendprice import SpendPricer

    children = _Children(ChildApiError(403, "child_submit_unproven", "", retryable=False))

    assert SpendPricer(_Cfg(), children=children, logger=_Log()).price(
        {"claude-opus-5-5": USAGE}) is None
