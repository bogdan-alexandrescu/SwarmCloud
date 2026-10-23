"""How the bridge paces its calls to swarm-api (BRIDGE-PACING, part of #1334).

Five findings from the review of PR 1455 (BRIDGE-POOL) and a 2026-10-20
measurement, one test (or a pair) each:

1. A quick-lane GET sat about 3 minutes in a 429 storm: `Retry-After` was
   honoured up to 60 s, three times, and a single-flight leader held its
   waiters as long. A quick read now gives up after
   `QUICK_RETRY_BUDGET_SECONDS` of waiting, and its waiters get the same 429.
2. `workflows.fetch` re-reads through a copy of the client (`reader`), as
   `check._readers` does. Both copies must take tokens from the one gate and
   share the one read cache (keyed by tenant), or every tenant the bridge
   names is a principal it does not pace.
3. `SWARM_MCP_MAX_HOLDS`, `SWARM_MCP_RATE_PER_SECOND` and
   `SWARM_MCP_RATE_BURST` were documented only in code comments.
4. `RateGate.take`'s waiters all slept to the same refill moment and woke
   together: 256 holds, 256 wakes for every token.
5. A step row could end FAILED with no `last_error` although the task held one.

Offline: the real `SwarmClient` over a replaced `client._open`; the retry's
sleep is recorded, not slept.
"""

from __future__ import annotations

import email.message
import io
import json
import re
import threading
import time
from pathlib import Path

import pytest
import urllib.error

from swarm_mcp import check, compact, server, workflows
from swarm_mcp import client as mcp_client
from swarm_mcp.client import RateGate, SwarmClient, SwarmError

_ROOT = Path(__file__).resolve().parents[3]


class _Response:
    status = 200

    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _too_many(retry_after: str):
    def _raise(url: str) -> urllib.error.HTTPError:
        headers = email.message.Message()
        headers["Retry-After"] = retry_after
        body = json.dumps({"detail": "rate limit exceeded"}).encode()
        return urllib.error.HTTPError(url, 429, "Too Many Requests", headers, io.BytesIO(body))
    return _raise


@pytest.fixture()
def answers(monkeypatch):
    """A queue of answers for the transport: a payload, or a callable raising."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Answers", (), {})()
    state.queue = []
    state.sent = []
    lock = threading.Lock()

    def _opener(req, timeout=None):  # noqa: ARG001
        with lock:
            state.sent.append((req.get_method(), req.full_url, req.get_header("X-swarm-tenant")))
            answer = state.queue.pop(0) if state.queue else {"task": {"task_id": "t1", "state": "RUNNING"}}
        if callable(answer):
            raise answer(req.full_url)
        return _Response(answer)

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return state


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.002)


# -- 1. a quick read gives up after a bounded wait ------------------------------

def test_a_quick_get_stops_retrying_once_its_wait_budget_is_spent(answers):
    sleeps: list[float] = []
    client = SwarmClient(base_url="http://api.invalid")
    client.pace_for_bridge(environ={}, sleep=sleeps.append)
    answers.queue = [_too_many("15"), _too_many("15"), {"attempts": []}]
    with pytest.raises(SwarmError) as caught:
        client.attempts("t1")  # the test's own thread is a quick-lane thread
    assert caught.value.status == 429
    # MUTATION: drop the budget check in `_request_retrying` and the second
    # 15 s Retry-After is slept too (30 s), and the third try succeeds.
    assert sleeps == [15.0]
    assert sum(sleeps) <= mcp_client.QUICK_RETRY_BUDGET_SECONDS
    assert len(answers.sent) == 2
    # The budget is about 20 s, and it covers the default backoff whole.
    assert 15.0 <= mcp_client.QUICK_RETRY_BUDGET_SECONDS <= 30.0
    worst_backoff = sum(mcp_client.RETRY_429_DELAYS) * (1 + mcp_client.RETRY_JITTER)
    assert worst_backoff <= mcp_client.QUICK_RETRY_BUDGET_SECONDS


def test_a_hold_keeps_the_longer_retry(answers):
    """A hold is a row waiting in the background; it may wait the full cap."""
    sleeps: list[float] = []
    client = SwarmClient(base_url="http://api.invalid")
    client.pace_for_bridge(environ={}, sleep=sleeps.append)
    answers.queue = [_too_many("15"), _too_many("15"), {"attempts": []}]
    got: list = []
    held = threading.Thread(target=lambda: (mcp_client.enter_hold_lane(), got.append(client.attempts("t1"))))
    held.start()
    held.join(10)
    # MUTATION: apply the quick budget to holds too and this hold gives up
    # after one sleep with a 429 instead of its answer.
    assert got == [[]]
    assert sleeps == [15.0, 15.0]


def test_a_quick_leaders_waiters_get_its_429_when_it_gives_up(answers):
    sleeps: list[float] = []
    client = SwarmClient(base_url="http://api.invalid")
    sleep_hook: list = []
    client.pace_for_bridge(environ={}, sleep=lambda seconds: sleep_hook[0](seconds))
    entered = [0]
    real_read = client._shared.read

    def _counting_read(*args, **kwargs):
        entered[0] += 1
        return real_read(*args, **kwargs)

    client._shared.read = _counting_read
    waiter_saw: list = []

    def _waiter() -> None:
        try:
            waiter_saw.append(client.task("t1"))
        except SwarmError as exc:
            waiter_saw.append(exc)

    waiter = threading.Thread(target=_waiter, daemon=True)

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if not waiter.is_alive() and not waiter_saw:
            waiter.start()
            _until(lambda: entered[0] >= 2, "the waiter never joined the read")
            time.sleep(0.05)  # from entering `read` to waiting on the flight

    sleep_hook.append(_sleep)
    answers.queue = [_too_many("15"), _too_many("15")]
    with pytest.raises(SwarmError) as caught:
        client.task("t1")
    waiter.join(10)
    # MUTATION: drop the budget check and the leader sleeps a second 15 s,
    # tries a third time, and both callers are answered RUNNING after 30 s.
    assert caught.value.status == 429
    assert len(waiter_saw) == 1 and waiter_saw[0] is caught.value, "the waiter got another answer"
    assert len(answers.sent) == 2, "the waiter read for itself instead of taking the leader's answer"
    assert sum(sleeps) <= mcp_client.QUICK_RETRY_BUDGET_SECONDS


# -- 2. every client the bridge builds shares the one gate ------------------------

def test_the_tenant_rereads_share_the_bridges_gate_and_read_cache(answers, monkeypatch):
    client = SwarmClient(base_url="http://api.invalid", tenant="t-first")
    client.pace_for_bridge(environ={})
    gate, shared = client._gate, client._shared

    # workflows.fetch's re-read, in the tenant the bridge launched it in.
    workflows._remember_launch({"workflow_id": "wf_x", "tenant_id": "t-launch"})
    not_found = urllib.error.HTTPError(
        "u", 404, "Not Found", email.message.Message(),
        io.BytesIO(json.dumps({"detail": {"code": "workflow_not_in_tenant"}}).encode()),
    )
    answers.queue = [lambda url: not_found, {"workflow": {"workflow_id": "wf_x"}}]
    taken: list = []
    real_take = RateGate.take

    def _take(self, *, hold=False):
        taken.append(self)
        return real_take(self, hold=hold)

    monkeypatch.setattr(RateGate, "take", _take)
    workflows.fetch(client, "wf_x")
    # MUTATION: give the copy its own gate (`reader._gate = RateGate(...)`) in
    # `workflows.fetch` and the second take is not the bridge's gate.
    assert taken == [gate, gate]
    assert answers.sent[1][2] == "t-launch"

    # check._readers' per-tenant copies.
    answers.queue = [[{"tenant_id": "t-first"}, {"tenant_id": "t-other"}]]
    readers = check._readers(client, "all")
    assert [r.tenant for r in readers] == ["t-first", "t-other"]
    # MUTATION: build a new SwarmClient per tenant in `_readers` and these
    # are None -- an unpaced principal per tenant.
    assert all(r._gate is gate and r._shared is shared for r in readers)

    # The one cache is keyed by tenant: the copy is never handed the
    # original's answer for the same path.
    answers.queue = [{"task": {"task_id": "t1", "state": "RUNNING"}},
                     {"task": {"task_id": "t1", "state": "FAILED"}}]
    assert readers[0].task("t1")["state"] == "RUNNING"
    assert readers[1].task("t1")["state"] == "FAILED"


def test_the_bridge_builds_its_client_in_one_place():
    """Every `SwarmClient(` under swarm_mcp that the bridge's server can reach
    is `serve._client`'s; the others are `sc`/`swarm` command entry points."""
    package = _ROOT / "apps" / "swarm-mcp" / "swarm_mcp"
    built = sorted(
        f"{path.name}"
        for path in package.glob("*.py")
        for line in path.read_text().splitlines()
        if re.search(r"\bSwarmClient\(", line) and not line.lstrip().startswith(("#", "\"", "`"))
    )
    # MUTATION: construct a second SwarmClient in server.py, workflows.py,
    # compact.py or follow.py and this list grows.
    assert built == ["cli.py", "cli.py", "sc.py", "sc.py", "sc.py", "server.py"]


# -- 3. the three settings are documented where a user reads them ---------------

@pytest.mark.parametrize(
    "name, default",
    [
        ("SWARM_MCP_MAX_HOLDS", server.HOLD_CALLS_DEFAULT),
        ("SWARM_MCP_RATE_PER_SECOND", mcp_client.RATE_PER_SECOND_DEFAULT),
        ("SWARM_MCP_RATE_BURST", mcp_client.RATE_BURST_DEFAULT),
    ],
)
def test_the_plugin_readme_lists_each_pacing_setting_with_its_default(name, default):
    readme = (_ROOT / "plugin" / "README.md").read_text()
    rows = [line for line in readme.splitlines() if line.startswith("|") and f"`{name}`" in line]
    # MUTATION: delete the row, or change the default in code without the
    # README, and this fails.
    assert len(rows) == 1, f"plugin/README.md has no table row for {name}"
    cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
    assert len(cells) == 4, "a row is: setting | what it bounds | default | why"
    assert cells[2] == f"{default:g}", f"{name}'s default in the README is not the code's"
    assert len(cells[1]) > 20 and len(cells[3]) > 20


# -- 4. a refill wakes one waiter, not the herd -----------------------------------

def test_waiting_holds_are_woken_one_at_a_time():
    """30 holds queued on an empty bucket of one token at 100/s. Each wake
    reads the clock once; a herd wakes every waiter at every refill, about
    30 + 29 + ... = 465 reads, where a queue wakes about one per token."""
    reads = [0]

    def _clock() -> float:
        reads[0] += 1
        return time.monotonic()

    gate = RateGate(100, 1, clock=_clock)  # reserve 0
    gate.take()
    reads[0] = 0
    holds = 30
    threads = [
        threading.Thread(target=gate.take, kwargs={"hold": True}, daemon=True) for _ in range(holds)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    # MUTATION: wait every waiter on the one condition with its own refill
    # timeout (the herd) and the reads run to hundreds.
    assert reads[0] <= 4 * holds, f"{reads[0]} wakes for {holds} tokens: the waiters woke as a herd"


def test_a_quick_caller_still_goes_before_queued_holds():
    clock = [1000.0]
    gate = RateGate(10, 1, clock=lambda: clock[0])  # reserve 0, refills only when moved
    gate.take()
    order: list[str] = []
    hold = threading.Thread(target=lambda: (gate.take(hold=True), order.append("hold")), daemon=True)
    hold.start()
    time.sleep(0.05)  # the hold is waiting for the token
    quick = threading.Thread(target=lambda: (gate.take(), order.append("quick")), daemon=True)
    quick.start()
    _until(lambda: gate._quick_waiting == 1, "the quick call never queued")
    clock[0] += 0.1  # one token
    _until(lambda: order, "nobody took the refilled token")
    # MUTATION: serve the queues in arrival order and the hold takes it.
    assert order == ["quick"]
    clock[0] += 0.1
    _until(lambda: len(order) == 2, "the hold was never served")
    assert order == ["quick", "hold"]


# -- 5. a FAILED row always carries the task's last_error -------------------------

class _Tasks:
    """`client.task` answering the given documents in turn, the last for good."""

    def __init__(self, *docs) -> None:
        self.docs = list(docs)

    def task(self, task_id: str):  # noqa: ARG002
        return dict(self.docs.pop(0) if len(self.docs) > 1 else self.docs[0])


def test_a_failed_row_carries_the_tasks_last_error_when_the_outcome_has_none(monkeypatch):
    reason = "gke_create_job_forbidden (attempt att_1)"
    failed = {"task_id": "task_a", "state": "FAILED", "step_id": "review", "last_error": reason}
    client = _Tasks(failed)
    # The outcome the row relays came back without the reason (the measured
    # rows, 2026-10-20); the follow read of the task had it.
    monkeypatch.setattr(compact, "outcome", lambda _c, task: {"state": task["state"], "last_error": None})
    monkeypatch.setattr(compact, "progress_line", lambda _c, task, _now: (f"{task['state']}", task["state"]))
    reply = compact.watch_progress(client, ["task_a"], step_id="review")
    assert reply["stop"] is True
    # MUTATION: read `last_error` from the relayed outcome only in
    # `step_result` and this is "".
    assert reply["result"]["state"] == "FAILED"
    assert reply["result"]["last_error"] == reason


def test_the_outcomes_own_last_error_still_wins():
    row = {"state": "FAILED", "last_error": "the follow read's",
           "outcome": {"state": "FAILED", "last_error": "the outcome's"}}
    # MUTATION: prefer the row's read over the outcome and this is the follow read's.
    assert compact.step_result(row)["last_error"] == "the outcome's"
    assert compact.step_result({"state": "FAILED", "last_error": "the follow read's",
                                "outcome": {"state": "FAILED"}})["last_error"] == "the follow read's"
